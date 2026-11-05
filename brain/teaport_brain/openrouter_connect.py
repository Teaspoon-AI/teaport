#
# teaport — "Connect your OpenRouter account" (#282): OpenRouter's sign-in (OAuth PKCE)
# from the owner's Settings, so nobody has to find, copy and paste a key on a phone.
# Pasting a key stays (config_ui's AI account step), for power users and for when this
# does not work.
#
# OpenRouter's flow (https://openrouter.ai/docs/use-cases/oauth-pkce, read 2026-11-04):
# the browser goes to https://openrouter.ai/auth?code_challenge=..&code_challenge_method=
# S256 (with callback_url=.. and state=.. when there is somewhere to come back to), the
# owner signs in and approves, and OpenRouter either sends the browser to callback_url
# with ?code=..&state=.. ("returned unchanged") or, with no callback_url, "displays the
# authorization code on screen instead of redirecting" for the owner to copy (state is
# "not returned ... in headless mode"; a code_challenge is required there). Either code is
# exchanged "exactly as usual": single-use, ten minutes, and worth nothing without the
# code_verifier the challenge was made from. The box keeps that, and exchanges code plus
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
# resolve). While that page does not answer as itself (ready), the sign-in starts with no
# callback_url, and the code OpenRouter shows is pasted into Settings: one flow, only the
# way back differs.
#
# The flow on the box:
#
#   1. Settings' "Connect your OpenRouter account" (POST /settings/api/openrouter/start,
#      the owner's Settings session: settings_access.authorized) makes a verifier, its
#      S256 challenge and a nonce: one Pending at a time (a new start replaces it), for
#      PENDING_SECS -- long enough to make an OpenRouter account first. With the page,
#      `state` is the Settings origin and the nonce, signed with the Settings session's
#      key (make_state). The answer is the sign-in address, which the page goes to,
#      whether a code comes back to paste, and a fresh session cookie: the round trip
#      must not outlast the session.
#   2. GET /settings/openrouter?code=..&state=.. (from the static page): a page with a
#      Finish button, nothing spent -- the same rule as Settings' own code links. No
#      session is asked for here: SameSite=Strict keeps the browser from sending the
#      cookie on a navigation that started on another site. The page's referrer policy is
#      same-origin: under no-referrer a form posts with `Origin: null`, which the next
#      step refuses.
#   3. Its POST (the button: same origin, so the cookie comes along) needs the session,
#      the Settings origin (Origin, Sec-Fetch-Site), a state this box signed for its own
#      Settings origin, and the live Pending's nonce. The Pending is spent there, success
#      or not: a code works once. The code and the verifier are exchanged for a key, and
#      the key is saved as a pasted one is (config_ui.save_values: checked with check_key,
#      then ai_account.save_key, which hands OpenClaw the same key and ends the
#      trial-credit warnings). A check that gets no answer does not stop it: OpenRouter
#      gave the key a moment ago.
#   4. The code pasted in Settings (POST /settings/api/openrouter/code, the session): the
#      code OpenRouter shows with no callback, or the one the static page shows when it
#      could not send the phone on. It finishes the same live Pending, the same way; a
#      paste that is no code spends nothing.
#
# A key the sign-in gave that could not be saved (OpenClaw did not take it: both or
# neither, ai_account.save_key; or the env file could not be written) is the owner's new
# key at OpenRouter all the same, and signing in again would make another. So it is held,
# in this process's memory only, for HELD_SECS, as the setup page holds a typed key until
# the box is online: Settings offers "Save it again" for that key (POST
# /settings/api/openrouter/retry, the session). It is never written anywhere else and
# never logged (never_log).
#
# Every failure says so in plain words, and the paste field stays. No key or code is
# logged (exchange_code marks the key never_log; gateway_server's access-log filter
# blanks code= and state=).
#
# The button is offered on the Settings site only (the sign-in comes back there, never
# to the operator's /config), for an OpenRouter endpoint, and not where the key is set
# where Settings cannot change it (ai_account.shadowed: ELSEWHERE). The end of Wi-Fi
# setup offers it too (offer_after_setup), through "open my settings"' own code.
#
# The static page's words are this module's (PAGE_TEXT), translated in the catalogs and
# written into the page by `python -m teaport_brain.openrouter_connect` (--check: is it
# current), with its CSP hashes.
#
import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger

from teaport_brain import ai_account, config_ui, conversation_log, i18n, pairing, settings_access
from teaport_brain.i18n import N_
from teaport_brain.settings import setting

AUTH_URL = "https://openrouter.ai/auth"
# Where the static page sends the phone, on the box's Settings origin (the page has the
# same path: web/connect/index.html, PATH).
LANDING_PATH = "/settings/openrouter"
# Signing up at OpenRouter first takes a while; the code it then gives lasts ten minutes
# on its own.
PENDING_SECS = 1200.0
# A key the sign-in gave that could not be saved, kept for "Save it again".
HELD_SECS = 600.0
# The new key's name in the owner's OpenRouter account.
KEY_LABEL = "Teaport"
# The static page says it is that page with this tag (ready).
PAGE_MARK = b'<meta name="teaport-connect" content="1">'
PROBE_TIMEOUT_SECS = 4.0
PROBE_OK_SECS = 6 * 3600.0
PROBE_FAILED_SECS = 300.0
# The Finish form: a code and a state, well past the longest of each (test).
FORM_MAX_BYTES = 4096

_clock = time.monotonic  # replaced in tests

# What the pages and the Settings page say (in the phone's language).
CONNECTED_HEADING = N_("Your OpenRouter account is connected")
CONNECTED_TEXT = N_("Teaport uses your OpenRouter account from your next conversation.")
FAILED_HEADING = N_("Teaport couldn't connect your OpenRouter account")
FAILED_TEXT = N_("Teaport couldn't connect your OpenRouter account: {reason}. You can try "
                 "again from Teaport's settings, or paste a key there instead.")
# The reason when the save itself broke (an env file not written): its detail is logged.
SAVE_BROKE = N_("Teaport couldn't save the key")
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
# On the Settings page (code_route, retry_route).
NOT_A_CODE_TEXT = N_("That doesn't look like a code from OpenRouter. Check it and paste it "
                     "again.")
NO_SIGN_IN_TEXT = N_("There's no sign-in waiting for a code. Tap Connect your OpenRouter "
                     "account to start a new one.")
NOT_HELD_TEXT = N_("Teaport no longer has that key. Tap Connect your OpenRouter account to "
                   "sign in again.")

# The static page's words (web/connect/index.html, written by main()). {field} is the
# Settings field's name (config_ui.ACCOUNT_TEXT["code"]).
PAGE_TEXT = {
    "title": N_("Connecting your OpenRouter account to Teaport"),
    "t-going": N_("Your phone needs to be on the same Wi-Fi as Teaport. If it doesn't go on "
                  "by itself, tap the button."),
    "continue": N_("Continue to Teaport"),
    "t-warn": N_("If your phone warns that the connection isn't private, it's the same "
                 "warning you saw when you opened Teaport's settings."),
    "t-elsewhere": N_("This link doesn't lead to a Teaport on a home network, so this page "
                      "won't go on with it."),
    "t-carry": N_("If that doesn't work: copy this code, open Teaport's settings on this "
                  "phone, and paste it into {field}. It works once, in the next ten minutes."),
    "copy": N_("Copy code"),
    "copied": N_("Copied"),
    "t-nothing": NOT_CONNECTED_TEXT,
}


# ------------------------------------------------------------------ the static page

def callback_url() -> str | None:
    """The static page's address (TEAPORT_OPENROUTER_CONNECT_URL), None when not HTTPS:
    OpenRouter returns to no plain-HTTP page but localhost."""
    url = (setting("TEAPORT_OPENROUTER_CONNECT_URL") or "").strip()
    return url if url.lower().startswith("https://") else None


def _fetch(url: str) -> tuple[int, bytes]:
    """A plain GET within PROBE_TIMEOUT_SECS, no redirect followed. Replaced in tests."""
    return ai_account._deadline(lambda: ai_account._request(url, None, PROBE_TIMEOUT_SECS),
                                PROBE_TIMEOUT_SECS)


_probe: tuple[str, bool, float] | None = None  # (url, answered as the page, when)
_probing = threading.Lock()


def ready() -> str | None:
    """The static page's address while it answers as that page (PAGE_MARK), else None.
    Asked again after PROBE_OK_SECS, or PROBE_FAILED_SECS while it does not answer (a page
    not hosted yet, the box offline, an answer that is no HTTP). One ask at a time: while
    one runs, the others take what was last known. Blocking: callers run it in a thread."""
    global _probe
    url = callback_url()
    if url is None:
        return None
    known = _probe if _probe is not None and _probe[0] == url else None
    if known is not None and \
            _clock() - known[2] < (PROBE_OK_SECS if known[1] else PROBE_FAILED_SECS):
        return url if known[1] else None
    if not _probing.acquire(blocking=False):
        return url if known is not None and known[1] else None
    try:
        try:
            status, body = _fetch(url)
            ok = 200 <= status < 300 and PAGE_MARK in body
            if not ok:
                logger.info(f"openrouter connect: {url} is not the sign-in page (HTTP {status})")
        except Exception as e:  # noqa: BLE001 -- whatever it was, the page is not there
            logger.info(f"openrouter connect: the sign-in page did not answer ({type(e).__name__})")
            ok = False
        _probe = (url, ok, _clock())
        return url if ok else None
    finally:
        _probing.release()


# ------------------------------------------------------------------ who may connect

def connectable(brain_env: dict[str, str]) -> bool:
    """Whether a key from the sign-in could be saved: the endpoint is OpenRouter's, and
    no key is set where Settings cannot change it (ai_account.shadowed: ELSEWHERE; a copy
    in brain.env is dropped by the save)."""
    return (ai_account.is_openrouter(config_ui.endpoint(brain_env))
            and ai_account.shadowed(brain_env) != ai_account.ELSEWHERE)


async def offer_after_setup(voice, lead: str) -> bool:
    """The end of Wi-Fi setup with no key typed (wifi_voice, owner decision on #282):
    where "open my settings" is usable here and the box has no key of the owner's, say
    `lead` and offer to connect the account -- the Settings code, through the phrase's own
    path (settings_access.SettingsPhrase.open). False, nothing said, otherwise."""
    phrase = next((p for p in getattr(voice, "phrases", ())
                   if isinstance(p, settings_access.SettingsPhrase)), None)
    if phrase is None or not voice.settings_usable:
        return False
    env = await config_ui.brain_env()
    if not ai_account.sign_in_after_setup(config_ui.endpoint(env), env,
                                          settings_access.available()):
        return False
    logger.info("openrouter connect: offered at the end of Wi-Fi setup")
    await phrase.open(voice, lead=lead, connect=True)
    return True


# ------------------------------------------------------------------ the pending sign-in

@dataclass(eq=False)
class Pending:
    nonce: str
    verifier: str
    expires_at: float

    def live(self) -> bool:
        return _clock() < self.expires_at


_pending: Pending | None = None
_held: tuple[str, float] | None = None  # (a key the sign-in gave that was not saved, until)


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


def start(callback: str | None) -> str:
    """A new sign-in (the one before can no longer finish): its address at OpenRouter.
    With `callback` (the static page), OpenRouter sends the phone there; without, it shows
    the code to copy (no state then: it is not returned)."""
    global _pending, _held
    _held = None  # a new sign-in: the key an older one left is not saved after it
    verifier = secrets.token_urlsafe(48)  # 64 characters: RFC 7636 asks for 43 to 128
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    nonce = secrets.token_urlsafe(16)
    _pending = Pending(nonce, verifier, _clock() + PENDING_SECS)
    logger.info(f"openrouter connect: a sign-in was started "
                f"({'through the page' if callback else 'with a code to paste'}), "
                f"{PENDING_SECS / 60:.0f} min to finish it")
    query = {"code_challenge": challenge, "code_challenge_method": "S256",
             "key_label": KEY_LABEL}
    if callback:
        query.update(callback_url=callback,
                     state=make_state(settings_access.origin(), nonce))
    return AUTH_URL + "?" + urllib.parse.urlencode(query)


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


def _held_key() -> str | None:
    global _held
    if _held is not None and _clock() >= _held[1]:
        _held = None  # past its time: let go of the string too
    return None if _held is None else _held[0]


def _forget_held(key: str) -> None:
    """After any key was saved, whichever page saved it (ai_account.SAVED_HOOKS): "Save
    it again" must not put an older key back over it."""
    global _held
    _held = None


ai_account.SAVED_HOOKS.append(_forget_held)


# The exchange (ai_account.exchange_code); replaced in tests: none talks to OpenRouter.
exchange = ai_account.exchange_code


def _failed(t: i18n.T, reason: str) -> str:
    return t._(FAILED_TEXT).format(reason=reason.strip().rstrip("."))


async def _save(request: Request, key: str) -> tuple[bool, str]:
    """Save a key the sign-in gave, as a pasted one is (config_ui.save_values, `issued`):
    (saved, what to say). One that could not be saved but may be the owner's good key (not
    refused by the check) is held for "Save it again"."""
    global _held
    t = config_ui._reader(request)
    try:
        status, content = await config_ui.save_values(
            request, {"store": "brain_env", "values": {"LLM_API_KEY": key}}, issued=True)
    except HTTPException as e:
        logger.warning(f"openrouter connect: the key could not be saved ({e.detail})")
        status, content = 500, {"errors": {"LLM_API_KEY": t._(SAVE_BROKE)}}
    if status == 200:
        logger.info("openrouter connect: the owner's OpenRouter account is connected")
        return True, t._(CONNECTED_TEXT)
    check = content.get("key_check")
    if check:  # refused (or set elsewhere): this key will not do
        _held = None
        reason = check.get("reason") or ""
    else:  # OpenClaw did not take it, or the save broke: the key is good, keep it a while
        _held = (key, _clock() + HELD_SECS)
        conversation_log.never_log(key, secs=HELD_SECS + 60)
        reason = " ".join(content.get("errors", {}).values()) or " ".join(
            content.get("constraints", [])) or t._(SAVE_BROKE)
    logger.info("openrouter connect: the key from the sign-in was not saved"
                + ("" if check else "; held for another try"))
    return False, _failed(t, reason)


async def finish(request: Request, code: str, pending: Pending) -> tuple[bool, str]:
    """The code and the pending verifier exchanged for a key, and the key saved (_save):
    (connected, what to say, in the reader's language)."""
    t = config_ui._reader(request)
    key, why = await asyncio.to_thread(exchange, code, pending.verifier)
    if key is None:
        return False, _failed(t, t._(why))
    return await _save(request, key)


async def status(request: Request, brain_env: dict[str, str]) -> dict:
    """For the AI account step (config_ui GET /api/account): whether the button is
    offered here, whether the sign-in comes back through the static page (`page`; without
    it, OpenRouter shows a code to paste), whether a sign-in waits for its code
    (`pending`), and whether a key the sign-in gave waits for "Save it again" (`retry`)."""
    if not (settings_access.on_settings_site(request) and connectable(brain_env)):
        return {"offered": False, "page": False, "pending": False, "retry": False}
    return {"offered": True, "page": await asyncio.to_thread(ready) is not None,
            "pending": _live(None) is not None, "retry": _held_key() is not None}


# ------------------------------------------------------------------ routes

router = APIRouter()


def _owner(request: Request) -> None:
    if not settings_access.authorized(request):
        raise HTTPException(status_code=401, detail="no Settings session")


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="the body must be JSON") from None
    return body if isinstance(body, dict) else {}


@router.post("/settings/api/openrouter/start")
async def start_route(request: Request):
    _owner(request)
    if not connectable(await config_ui.brain_env()):
        raise HTTPException(status_code=409, detail="a key from OpenRouter could not be saved "
                                                    "here (not OpenRouter, or set elsewhere)")
    callback = await asyncio.to_thread(ready)
    answer = JSONResponse({"url": start(callback), "page": callback is not None},
                          headers=pairing.NO_STORE)
    return settings_access.keep_session(answer)


@router.post("/settings/api/openrouter/code")
async def code_route(request: Request):
    """A code pasted into Settings: the one OpenRouter shows (no static page), or the one
    the static page shows. `pending` in the answer: whether a sign-in still waits."""
    _owner(request)
    t = config_ui._reader(request)
    code = ai_account.code_shape(str((await _json(request)).get("code") or ""))

    def answer(ok: bool, said: str) -> JSONResponse:
        return JSONResponse(status_code=200 if ok else 400, content={
            "ok": ok, "message": said, "pending": _live(None) is not None,
            "retry": _held_key() is not None})

    if code is None:
        return answer(False, t._(NOT_A_CODE_TEXT))  # nothing spent
    pending = _take(None)
    if pending is None:
        return answer(False, t._(NO_SIGN_IN_TEXT))
    return answer(*await finish(request, code, pending))


@router.post("/settings/api/openrouter/retry")
async def retry_route(request: Request):
    """"Save it again": the key the sign-in gave, held after a save that did not go
    through (_save)."""
    _owner(request)
    t = config_ui._reader(request)
    key = _held_key()
    if key is None:
        ok, said = False, t._(NOT_HELD_TEXT)
    else:
        ok, said = await _save(request, key)
    return JSONResponse(status_code=200 if ok else 400, content={
        "ok": ok, "message": said, "retry": _held_key() is not None})


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
    # same-origin, not no-referrer: the form's POST must carry the Settings origin.
    return settings_access._page(t, t._(FINISH_HEADING), pairing.para(t._(FINISH_TEXT)) + form,
                                 script=pairing._ONE_TAP, referrer="same-origin")


@router.post(LANDING_PATH)
async def finish_route(request: Request) -> Response:
    refused, form = await settings_access._gate(request, post=True, limit=FORM_MAX_BYTES)
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


# ------------------------------------------------------------------ the static page's file

WEB_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "web",
                        "connect", "index.html")
_STRINGS = re.compile(r"(/\*strings\*/).*?(/\*end\*/)", re.S)


def page_strings() -> dict[str, dict[str, str]]:
    """PAGE_TEXT in every language, by the page's own language codes (pt-BR, not pt_BR)."""
    field = config_ui.ACCOUNT_TEXT["code"]
    out = {}
    for lang in sorted({i18n.SOURCE_LANG, *i18n.catalogs()}):
        t = i18n.get(lang)
        out[lang.replace("_", "-")] = {k: t._(v).replace("{field}", t._(field))
                                       for k, v in PAGE_TEXT.items()}
    return out


def _csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def render_page(text: str) -> str:
    """The static page with its words (page_strings) and its CSP's hashes current."""
    # Inside <script>: no "<" (a "</script>" in a translation would end it).
    table = json.dumps(page_strings(), ensure_ascii=False, sort_keys=True, indent=0).replace(
        "<", "\\u003c")
    text = _STRINGS.sub(lambda m: m.group(1) + table + m.group(2), text, count=1)
    script = re.search(r"<script>(.*?)</script>", text, re.S).group(1)
    style = re.search(r"<style>(.*?)</style>", text, re.S).group(1)
    text = re.sub(r"script-src '[^']*'", lambda _: "script-src " + _csp_hash(script), text)
    return re.sub(r"style-src '[^']*'", lambda _: "style-src " + _csp_hash(style), text)


def main(argv: list[str]) -> int:
    """Write the static page's words and hashes (--check: exit 1 when it is not current)."""
    with open(WEB_PAGE, encoding="utf-8") as f:
        text = f.read()
    new = render_page(text)
    if "--check" in argv:
        if new != text:
            print(f"{WEB_PAGE} is not current: run python -m teaport_brain.openrouter_connect")
            return 1
        return 0
    if new != text:
        with open(WEB_PAGE, "w", encoding="utf-8") as f:
            f.write(new)
    print(f"{os.path.normpath(WEB_PAGE)}: {'written' if new != text else 'up to date'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
