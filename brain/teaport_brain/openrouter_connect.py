#
# teaport — "Connect your OpenRouter account" (#282): OpenRouter's sign-in (OAuth PKCE)
# from the owner's Settings, so nobody has to find, copy and paste a key on a phone.
# Pasting a key stays (config_ui's AI account step), for power users and for when this
# does not work.
#
# OpenRouter's flow (https://openrouter.ai/docs/use-cases/oauth-pkce, read 2026-11-04):
# the browser goes to https://openrouter.ai/auth?callback_url=..&code_challenge=..
# &code_challenge_method=S256&state=.., the owner signs in and approves, and OpenRouter
# sends the browser to callback_url with ?code=..&state=.. (state "returned unchanged").
# The code is single-use, lasts ten minutes, and is worth nothing without the
# code_verifier the challenge was made from: the box keeps that, and exchanges code plus
# verifier for a key (ai_account.exchange_code).
#
# Where the browser comes back to. The docs name two kinds of callback, localhost on any
# port (a phone is not the box) and a public URL; a box on a home network has neither. So
# the callback is a small static page Teaspoon hosts (TEAPORT_OPENROUTER_CONNECT_URL,
# web/connect/index.html in the repo): it reads the box's Settings origin from `state`
# and sends the phone on to <origin>/settings/openrouter with the code and the state. It
# holds no secret and keeps nothing. It goes on only to an HTTPS address on the home
# network (a .local name or a private IP), at that one path: a state someone else made
# cannot turn it into a way to send codes to a server of theirs. Every browser lets a
# page send the phone on to a private address as a top-level navigation (Chrome's Local
# Network Access and Firefox's both leave the main frame alone; Safari has no such
# rule), so the page does that itself, and also shows a Continue link and the code to
# copy for when it cannot (the phone off the home Wi-Fi, a .local name the phone does not
# resolve).
#
# The flow on the box:
#
#   1. Settings' "Connect your OpenRouter account" (POST /settings/api/openrouter/start,
#      the owner's Settings session: settings_access.authorized) makes a verifier, its
#      S256 challenge and a nonce: one Pending at a time (a new start replaces it), for
#      PENDING_SECS -- long enough to make an OpenRouter account first. `state` is the
#      Settings origin and the nonce, signed with the Settings session's key
#      (make_state). The answer is the sign-in address, which the page goes to, and a
#      fresh session cookie: the round trip must not outlast the session.
#   2. GET /settings/openrouter?code=..&state=.. (from the static page): a page with a
#      Finish button, nothing spent -- the same rule as Settings' own code links. No
#      session is asked for here: SameSite=Strict keeps the browser from sending the
#      cookie on a navigation that started on another site.
#   3. Its POST (the button: same origin, so the cookie comes along) needs the session,
#      the Settings origin (Origin, Sec-Fetch-Site), a state this box signed for its own
#      Settings origin, and the live Pending's nonce. The Pending is spent there, success
#      or not: a code works once. The code and the verifier are exchanged for a key, and
#      the key is saved as a pasted one is (config_ui.save_values: checked with
#      check_key, then ai_account.save_key, which hands OpenClaw the same key and ends
#      the trial-credit warnings).
#   4. The fallback: the static page shows the code; pasted in Settings (POST
#      /settings/api/openrouter/code, the session), it is finished the same way against
#      the live Pending.
#
# Every failure says so in plain words, and the paste field stays. No key or code is
# logged (exchange_code marks the key never_log; gateway_server's access-log filter
# blanks code= and state=).
#
# The button is offered only on the Settings site (the sign-in comes back there, never
# to the operator's /config), for an OpenRouter endpoint, and while the static page
# answers as itself (ready: a GET from the box, the result kept for a while).
#
import asyncio
import base64
import hashlib
import hmac
import html
import secrets
import time
import urllib.parse
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger

from teaport_brain import ai_account, config_ui, i18n, pairing, settings_access
from teaport_brain.i18n import N_
from teaport_brain.settings import setting

AUTH_URL = "https://openrouter.ai/auth"
# Where the static page sends the phone, on the box's Settings origin (the page has the
# same path: web/connect/index.html, PATH).
LANDING_PATH = "/settings/openrouter"
# Signing up at OpenRouter first takes a while; the code it then gives lasts ten minutes
# on its own.
PENDING_SECS = 1200.0
# The new key's name in the owner's OpenRouter account.
KEY_LABEL = "Teaport"
# The static page says it is that page with this tag (ready).
PAGE_MARK = b'<meta name="teaport-connect" content="1">'
PROBE_TIMEOUT_SECS = 4.0
PROBE_OK_SECS = 6 * 3600.0
PROBE_FAILED_SECS = 300.0

_clock = time.monotonic  # replaced in tests

# What the pages and the Settings page say (in the phone's language).
CONNECTED_HEADING = N_("Your OpenRouter account is connected")
CONNECTED_TEXT = N_("Teaport uses your OpenRouter account from your next conversation.")
FAILED_HEADING = N_("Teaport couldn't connect your OpenRouter account")
FAILED_TEXT = N_("Teaport couldn't connect your OpenRouter account: {reason}. You can try "
                 "again from Teaport's settings, or paste a key there instead.")
FINISH_HEADING = N_("Connect your OpenRouter account")
FINISH_TEXT = N_("Tap Finish to connect your OpenRouter account to Teaport.")
FINISH = N_("Finish")
NOT_CONNECTED_TEXT = N_("OpenRouter didn't connect the account. You can try again from "
                        "Teaport's settings, or paste a key there instead.")
EXPIRED_TEXT = N_("This connection has expired. Go back to Teaport's settings and connect "
                  "again.")
SESSION_ENDED_TEXT = N_("Teaport's settings have closed on this phone. Say \"open my "
                        "settings\", then connect again.")
BACK = N_("Back to settings")


# ------------------------------------------------------------------ the static page

def callback_url() -> str | None:
    """The static page's address (TEAPORT_OPENROUTER_CONNECT_URL), None when off or not
    HTTPS: OpenRouter returns to no plain-HTTP page but localhost."""
    url = (setting("TEAPORT_OPENROUTER_CONNECT_URL") or "").strip()
    return url if url.lower().startswith("https://") else None


def _fetch(url: str) -> tuple[int, bytes]:
    """A plain GET within PROBE_TIMEOUT_SECS, no redirect followed. Replaced in tests."""
    return ai_account._deadline(lambda: ai_account._request(url, None, PROBE_TIMEOUT_SECS),
                                PROBE_TIMEOUT_SECS)


_probe: tuple[str, bool, float] | None = None  # (url, answered as the page, when)


def ready() -> str | None:
    """The static page's address while it answers as that page (PAGE_MARK), else None.
    Asked again after PROBE_OK_SECS, or PROBE_FAILED_SECS while it does not answer (a page
    not hosted yet, the box offline). Blocking: callers run it in a thread."""
    global _probe
    url = callback_url()
    if url is None:
        return None
    now = _clock()
    if _probe is not None and _probe[0] == url and \
            now - _probe[2] < (PROBE_OK_SECS if _probe[1] else PROBE_FAILED_SECS):
        return url if _probe[1] else None
    try:
        status, body = _fetch(url)
        ok = 200 <= status < 300 and PAGE_MARK in body
    except (OSError, ValueError) as e:
        logger.info(f"openrouter connect: the sign-in page did not answer ({type(e).__name__})")
        ok = False
    else:
        if not ok:
            logger.info(f"openrouter connect: {url} is not the sign-in page (HTTP {status})")
    _probe = (url, ok, now)
    return url if ok else None


# ------------------------------------------------------------------ the pending sign-in

@dataclass(eq=False)
class Pending:
    nonce: str
    verifier: str
    expires_at: float

    def live(self) -> bool:
        return _clock() < self.expires_at


_pending: Pending | None = None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(head: str, nonce: str) -> str:
    return settings_access._sign(f"openrouter connect v1.{head}.{nonce}")[:32]


def make_state(origin: str, nonce: str) -> str:
    """"<origin, base64url>.<nonce>.<signature>": what the static page reads the box's
    address from, and what the box knows for its own when it comes back."""
    head = _b64(origin.encode())
    return f"{head}.{nonce}.{_signature(head, nonce)}"


def read_state(state: str) -> str | None:
    """The nonce of a state this box signed for its own Settings origin, else None."""
    try:
        head, nonce, sig = (state or "").split(".")
        origin = _unb64(head).decode()
    except ValueError:  # the wrong number of parts, bad base64 or UTF-8
        return None
    if not hmac.compare_digest(sig.encode(), _signature(head, nonce).encode()):
        return None
    return nonce if origin == settings_access.origin() else None


def start(callback: str) -> str:
    """A new sign-in (the one before can no longer finish): its address at OpenRouter."""
    global _pending
    verifier = secrets.token_urlsafe(48)  # 64 characters: RFC 7636 asks for 43 to 128
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    nonce = secrets.token_urlsafe(16)
    _pending = Pending(nonce, verifier, _clock() + PENDING_SECS)
    logger.info(f"openrouter connect: a sign-in was started, {PENDING_SECS / 60:.0f} min to finish it")
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "callback_url": callback, "code_challenge": challenge, "code_challenge_method": "S256",
        "state": make_state(settings_access.origin(), nonce), "key_label": KEY_LABEL})


def _live(nonce: str | None) -> Pending | None:
    """The live Pending -- for `nonce` when given -- or None."""
    p = _pending
    if p is None or not p.live():
        return None
    if nonce is not None and not hmac.compare_digest(nonce.encode(), p.nonce.encode()):
        return None
    return p


def _take(nonce: str | None) -> Pending | None:
    """_live(nonce), spent: no second finish with it, whatever this one comes to."""
    global _pending
    p = _live(nonce)
    if p is not None:
        _pending = None
    return p


# The exchange (ai_account.exchange_code); replaced in tests: none talks to OpenRouter.
exchange = ai_account.exchange_code


def _failed(t: i18n.T, reason: str) -> str:
    return t._(FAILED_TEXT).format(reason=reason.strip().rstrip("."))


async def finish(request: Request, code: str, pending: Pending) -> tuple[bool, str]:
    """The code and the pending verifier exchanged for a key, and the key saved as a
    pasted one is: (connected, what to say, in the reader's language)."""
    t = config_ui._reader(request)
    key, why = await asyncio.to_thread(exchange, code, pending.verifier)
    if key is None:
        return False, _failed(t, t._(why))
    try:
        status, content = await config_ui.save_values(
            request, {"store": "brain_env", "values": {"LLM_API_KEY": key}})
    except HTTPException as e:
        logger.warning(f"openrouter connect: the key could not be saved ({e.detail})")
        return False, _failed(t, str(e.detail))
    if status == 200:
        logger.info("openrouter connect: the owner's OpenRouter account is connected")
        return True, t._(CONNECTED_TEXT)
    reason = ((content.get("key_check") or {}).get("reason")
              or " ".join(content.get("errors", {}).values())
              or " ".join(content.get("constraints", [])))
    logger.info("openrouter connect: the key from the sign-in was not saved")
    return False, _failed(t, reason)


async def status(request: Request, base: str | None) -> dict:
    """For the AI account step (config_ui GET /api/account): whether the button is
    offered here and now, and whether a sign-in is waiting to be finished (the page then
    offers the code field)."""
    offered = (ai_account.is_openrouter(base) and settings_access.on_settings_site(request)
               and await asyncio.to_thread(ready) is not None)
    return {"offered": offered, "pending": offered and _live(None) is not None}


# ------------------------------------------------------------------ routes

router = APIRouter()


def _owner(request: Request) -> None:
    if not settings_access.authorized(request):
        raise HTTPException(status_code=401, detail="no Settings session")


@router.post("/settings/api/openrouter/start")
async def start_route(request: Request):
    _owner(request)
    if not ai_account.is_openrouter(await config_ui.account_endpoint()):
        raise HTTPException(status_code=409, detail="this box's AI service is not OpenRouter")
    callback = await asyncio.to_thread(ready)
    if callback is None:
        raise HTTPException(status_code=409, detail="the sign-in page is not available")
    answer = JSONResponse({"url": start(callback)}, headers=pairing.NO_STORE)
    return settings_access.keep_session(answer)


@router.post("/settings/api/openrouter/code")
async def code_route(request: Request):
    """The code the static page showed, pasted into Settings."""
    _owner(request)
    body = await request.json()
    code = str(body.get("code") or "") if isinstance(body, dict) else ""
    t = config_ui._reader(request)
    pending = _take(None)
    if pending is None:
        return JSONResponse(status_code=400, content={"ok": False, "message": t._(EXPIRED_TEXT)})
    ok, said = await finish(request, code, pending)
    return JSONResponse(status_code=200 if ok else 400, content={"ok": ok, "message": said})


def _page(t: i18n.T, heading: str, text: str, status: int = 200, extra: str = "") -> Response:
    back = f'<p><a href="{settings_access.PAGE_PATH}">{html.escape(t._(BACK))}</a></p>'
    return settings_access._page(t, t._(heading), pairing.para(text) + extra + back, status)


@router.get(LANDING_PATH)
async def landing(request: Request) -> Response:
    """Where the static page sends the phone: a Finish button, nothing spent."""
    refused, _ = await settings_access._gate(request, post=False)
    if refused is not None:
        return refused
    t = settings_access._t(request)
    code, state = request.query_params.get("code", ""), request.query_params.get("state", "")
    if not code:  # the owner said no at OpenRouter, or it went wrong there
        return _page(t, FAILED_HEADING, t._(NOT_CONNECTED_TEXT))
    nonce = read_state(state)
    if nonce is None or _live(nonce) is None:
        return _page(t, FAILED_HEADING, t._(EXPIRED_TEXT), 410)
    form = (f'<form method="post" action="{LANDING_PATH}">'
            f'<input type="hidden" name="code" value="{html.escape(code, quote=True)}">'
            f'<input type="hidden" name="state" value="{html.escape(state, quote=True)}">'
            f'<button type="submit">{html.escape(t._(FINISH))}</button></form>')
    return settings_access._page(t, t._(FINISH_HEADING),
                                 pairing.para(t._(FINISH_TEXT)) + form, script=pairing._ONE_TAP)


@router.post(LANDING_PATH)
async def finish_route(request: Request) -> Response:
    refused, form = await settings_access._gate(request, post=True)
    if refused is not None:
        return refused
    t = settings_access._t(request)
    if not settings_access.authorized(request):
        return _page(t, FAILED_HEADING, t._(SESSION_ENDED_TEXT), 401)
    code = (form.get("code") or [""])[0]
    pending = _take(read_state((form.get("state") or [""])[0]) or "")
    if pending is None:
        return _page(t, FAILED_HEADING, t._(EXPIRED_TEXT), 410)
    ok, said = await finish(request, code, pending)
    return _page(t, CONNECTED_HEADING if ok else FAILED_HEADING, said, 200 if ok else 400)
