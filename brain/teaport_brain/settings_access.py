#
# teaport — Settings from the owner's phone (#279): "open my settings" by voice, a
# one-time code, and a session the config page takes in place of its token.
#
# The config page (config_ui.py, static/config.html) is plain HTTP on the brain's port
# and asks for GATEWAY_TOKEN, which lives in /etc/teaport/brain.env: changing the AI key
# took SSH. This is the owner's way in, on the front door (Caddy, `tls internal` with the
# box's own CA, install.sh write_caddyfile), the way pairing's short links are served
# (pairing.py):
#
#   1. "open my settings", a fixed phrase (SettingsPhrase, heard by the session's
#      PhraseVoice, wifi_voice.py) on the owner's surfaces: the room and Talk, never a
#      phone call nor a bridge that relays someone far away (agent_session.py). It needs
#      no model.
#   2. start(): a code, pairing's kind (pairing.new_code_text: six characters a reader
#      cannot confuse, pairing.OFFER_SECS, single use, pairing.MAX_WRONG wrong ones end
#      it). One at a time: asking again replaces it. The screen shows it as a QR code of
#      https://<front door>/settings/<code> (display.Screen "settings"), the code itself
#      under it; a box without a screen that draws one reads the address and the code
#      aloud (the page at /settings takes the code typed).
#   3. /settings/<code>: a GET spends nothing (a link preview, a prefetch); its button
#      (a POST) spends the code and answers with a session cookie and a redirect to the
#      page, /settings/config. The cookie (COOKIE) is HttpOnly, Secure, SameSite=Strict,
#      Path=/settings, and lasts SESSION_SECS.
#   4. config_ui's routes take the session in place of the token (authorized()), under
#      the /settings prefix gateway_server mounts them at. The token keeps working, on
#      the brain's own port, for operators.
#
# The session is a signed expiry, not a server-side record: an HMAC under a key derived
# from GATEWAY_TOKEN (_key), so it outlives a brain restart (the page restarts the
# brain), ends when the token is changed, and is checked in constant time. With no
# token the page is open on the LAN anyway; a per-process random key then signs.
#
# What keeps a session to the owner's phone:
#   - the code: shown on the box's screen or said aloud in the room, five minutes, once;
#   - only what came through the front door over HTTPS is answered here at all
#     (pairing.via_front_door): the brain's own port, open to the LAN, gets a 404 that is
#     no guess, and a Secure cookie is never sent to it in cleartext;
#   - another origin cannot use the cookie: SameSite=Strict keeps the browser from
#     sending it on any cross-site request, and a request that changes anything must
#     also carry the front door's own Origin (same_origin) -- a browser sets that header
#     itself and a page cannot forge it.
#
# The pages are in the phone's language (Accept-Language, i18n.py). The steps to trust
# the box's certificate are pairing's own (pairing.trust), in English for now, as on the
# pairing pages.
#
import collections
import hashlib
import hmac
import html
import math
import secrets
import time
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from loguru import logger

from teaport_brain import display, i18n, pairing
from teaport_brain.i18n import N_
from teaport_brain.settings import setting

PREFIX = "/settings/"
PAGE_PATH = "/settings/config"
COOKIE = "teaport_settings"
SESSION_SECS = 3600
SCREEN_ID, SCREEN_TITLE = "settings", "Settings"
# The screen after the code was used: the QR code is of no more use.
USED_SCREEN_SECS = 10.0
# "open my settings" said as a request of its own, not inside a longer sentence.
PHRASE_MAX_WORDS = 6

_clock = time.monotonic  # the code's clock; replaced in tests
_wall = time.time        # the session's (a cookie outlives the process)

# What the voice says (SettingsPhrase). The quoted words are a phrase in each catalog
# one of its own patterns hears (tests/test_i18n.py).
SHOWN_TEXT = N_("I've put a code on my screen. Scan it with your phone's camera, then tap "
                "the link that appears to get to my settings.")
SPOKEN_TEXT = N_("In your phone's browser, go to {address}, and enter the code {code}. It "
                 "works once, for the next five minutes.")
OFFLINE_TEXT = N_("I need to be online for that. Say \"set up Wi-Fi\" to get me online.")
UNAVAILABLE_TEXT = N_("Sorry, my settings can't be reached from a phone on this box.")


@dataclass(frozen=True)
class Offer:
    code: str
    link: str          # https://<front door>/settings/<code>
    expires_at: float  # _clock() deadline


_offer: Offer | None = None
_dead = False      # the current code was used or locked (expiry is the clock's)
_wrong = 0
_spent: collections.deque = collections.deque(maxlen=8)    # used lately: "already used"
_recent: collections.deque = collections.deque(maxlen=32)  # issued lately: never a guess
_screen: display.Screen | None = None  # the screen showing the current code


def available() -> bool:
    """Whether Settings can be offered: the front door's name is known."""
    return bool(pairing.FRONTDOOR_HOST)


def start() -> Offer:
    """A new code (the one before can no longer be used). Raises
    pairing.PairingUnavailable when the front door is unknown or no host name. A link too
    long for the screen's QR code is still made: the voice reads it out instead."""
    global _offer, _dead, _wrong
    host = pairing.check_host(pairing.FRONTDOOR_HOST, PREFIX, qr=False)
    code = pairing.new_code_text()
    _recent.append(code)
    _offer = Offer(code, pairing.link(host, code, PREFIX), _clock() + pairing.OFFER_SECS)
    _dead, _wrong = False, 0
    logger.info(f"settings: a code was made, {pairing.OFFER_SECS:.0f} s to use it")
    return _offer


def _usable() -> bool:
    return _offer is not None and not _dead and _clock() < _offer.expires_at


def _find(code: str) -> str:
    """"live", "used" or "expired" for a code at /settings/. A code that is neither the
    current one nor one issued lately is a wrong guess while the current one can be used;
    pairing.MAX_WRONG of them end it."""
    global _dead, _wrong
    code = code.strip().upper()
    if any(hmac.compare_digest(code.encode(), c.encode()) for c in _spent):
        return "used"
    if _offer is not None and hmac.compare_digest(code.encode(), _offer.code.encode()):
        return "live" if _usable() else "expired"
    if any(hmac.compare_digest(code.encode(), c.encode()) for c in _recent):
        return "expired"
    if _usable():
        _wrong += 1
        if _wrong >= pairing.MAX_WRONG:
            logger.warning(f"settings: {_wrong} wrong codes; the code is locked")
            _dead = True
    return "expired"


def _spend() -> None:
    global _dead
    _dead = True
    _spent.append(_offer.code)
    logger.info("settings: the code was used; a session was opened")
    if _screen is not None:
        _screen.show(SCREEN_TITLE, ["Go on", "on your phone"], secs=USED_SCREEN_SECS)


# ------------------------------------------------------------------ the session

def _key() -> bytes:
    token = (setting("GATEWAY_TOKEN") or "").encode("utf-8", "surrogateescape")
    return hmac.new(token or _PROCESS_KEY, b"teaport settings session v1", hashlib.sha256).digest()


_PROCESS_KEY = secrets.token_bytes(32)


def _sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def session_cookie() -> str:
    """A new session: "<expiry>.<nonce>.<signature>"."""
    payload = f"{int(_wall()) + SESSION_SECS}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload)}"


def session_ok(value: str | None) -> bool:
    """A session this box signed that has not run out."""
    try:
        expiry, nonce, sig = (value or "").split(".")
        until = int(expiry)
    except ValueError:
        return False
    if not hmac.compare_digest(sig.encode(), _sign(f"{expiry}.{nonce}").encode()):
        return False
    return 0 < until - _wall() <= SESSION_SECS


def same_origin(request: Request) -> bool:
    """The request names the front door as its origin: a browser sets Origin itself on a
    request that changes anything, and a page elsewhere cannot make it say ours."""
    origin = (request.headers.get("origin") or "").strip().lower().rstrip("/")
    want = f"https://{pairing.FRONTDOOR_HOST}".lower()
    return bool(pairing.FRONTDOOR_HOST) and origin in (want, want + ":443")


def authorized(request: Request) -> bool:
    """Is this a Settings session from the owner's phone: through the front door, a
    valid session cookie, and for anything but a read, from the front door's own page."""
    if not available() or not pairing.via_front_door(request):
        return False
    if not session_ok(request.cookies.get(COOKIE)):
        return False
    return request.method in ("GET", "HEAD") or same_origin(request)


# ------------------------------------------------------------------ the pages

def _t(request: Request) -> i18n.T:
    return i18n.for_accept_language(request.headers.get("accept-language"))


def _page(t: i18n.T, heading: str, body: str, status: int = 200, **kw) -> Response:
    return pairing.page(heading, body, status, lang=t.lang.replace("_", "-"), dir=t.dir, **kw)


def _trust(t: i18n.T) -> str:
    """How to trust the box first (nothing once devices trust its certificate)."""
    before, _, after = t._("If your phone warns that the connection is not private, install "
                           "{certificate} first, then ask Teaport for a new code.").partition(
        "{certificate}")
    return pairing.trust(before, pairing.ca_link(t._("Teaport's certificate")), after)


def _again(t: i18n.T) -> str:
    return pairing.para(t._("Ask Teaport for a new one: say \"open my settings\"."))


def _form(t: i18n.T, action: str, typed: bool) -> str:
    field = (f'<label class="f" for="code">{html.escape(t._("Code"))}</label>'
             '<input type="text" id="code" name="code" autocomplete="one-time-code" '
             'autocapitalize="characters" spellcheck="false" maxlength="12">') if typed else ""
    return (f'<form method="post" action="{html.escape(action, quote=True)}">{field}'
            f'<button type="submit">{html.escape(t._("Continue"))}</button></form>')


def _enter(t: i18n.T, error: str = "") -> Response:
    """/settings with no session: the code read aloud (or under the QR code) typed in."""
    return _page(t, t._("Settings"), "".join((
        f'<p class="err">{html.escape(error)}</p>' if error else "",
        pairing.para(t._("Enter the code Teaport gave you. It works once.")),
        _form(t, "/settings", typed=True), _trust(t))))


def _landing(t: i18n.T, code: str) -> Response:
    return _page(t, t._("Settings"), "".join((
        pairing.para(t._("Tap Continue to get to Teaport's settings on this phone. The code "
                         "works once.")),
        _form(t, PREFIX + code, typed=False), _trust(t))))


def _used(t: i18n.T) -> Response:
    return _page(t, t._("This code was already used"), _again(t) + _trust(t), 410)


def _expired(t: i18n.T) -> Response:
    return _page(t, t._("This code has expired"), _again(t) + _trust(t), 410)


def _signed_in() -> Response:
    """The code is spent: the session, and on to the page."""
    answer = RedirectResponse(PAGE_PATH, status_code=303, headers=pairing.NO_STORE)
    answer.set_cookie(COOKIE, session_cookie(), max_age=SESSION_SECS, path="/settings",
                      secure=True, httponly=True, samesite="strict")
    return answer


_NOT_FOUND = PlainTextResponse("Not Found", status_code=404)
router = APIRouter()


@router.get("/settings")
@router.get("/settings/")
async def enter(request: Request) -> Response:
    if not available() or not pairing.via_front_door(request):
        return _NOT_FOUND
    if session_ok(request.cookies.get(COOKIE)):
        return RedirectResponse(PAGE_PATH, status_code=303, headers=pairing.NO_STORE)
    return _enter(_t(request))


@router.post("/settings")
@router.post("/settings/")
async def enter_code(request: Request) -> Response:
    if not available() or not pairing.via_front_door(request):
        return _NOT_FOUND
    form = await pairing.small_form(request)
    if form is None:
        return PlainTextResponse("Payload Too Large", status_code=413)
    t = _t(request)
    typed = "".join((form.get("code") or [""])[0].split())
    if not typed:
        return _enter(t)
    state = _find(typed)
    if state != "live":
        return _enter(t, t._("That code didn't work. Check it, or ask Teaport for a new one."))
    _spend()
    return _signed_in()


@router.get("/settings/trust")
async def trust_steps(request: Request) -> Response:
    """How to trust the box's certificate on each kind of device (pairing's steps), for
    the Settings page to link to: no code needed, nothing spent."""
    if not available() or not pairing.via_front_door(request):
        return _NOT_FOUND
    if pairing.TRUSTED_CERT:  # nothing to trust: devices already do
        return RedirectResponse(PAGE_PATH, status_code=303, headers=pairing.NO_STORE)
    t = _t(request)
    return _page(t, t._("Settings"), _trust(t))


@router.get("/settings/{code}")
async def landing(code: str, request: Request) -> Response:
    """Unauthenticated: the phone has no session yet; the code is the grant. A GET
    spends nothing."""
    if not available() or not pairing.via_front_door(request):
        return _NOT_FOUND
    t = _t(request)
    state = _find(code)
    if state == "live":
        return _landing(t, _offer.code)
    return _used(t) if state == "used" else _expired(t)


@router.post("/settings/{code}")
async def redeem(code: str, request: Request) -> Response:
    if not available() or not pairing.via_front_door(request):
        return _NOT_FOUND
    if await pairing.small_form(request) is None:
        return PlainTextResponse("Payload Too Large", status_code=413)
    t = _t(request)
    state = _find(code)
    if state != "live":
        return _used(t) if state == "used" else _expired(t)
    _spend()
    return _signed_in()


# ------------------------------------------------------------------ the phrase

class SettingsPhrase:
    """ "open my settings", heard by the session's PhraseVoice (wifi_voice.py) while
    nothing else holds the user's words. `screen`: makes the box's screen (display)."""

    def __init__(self, screen=lambda: display.Screen(SCREEN_ID)):
        self._new_screen = screen

    async def heard(self, voice, text: str) -> bool:
        from teaport_brain import wifi_voice
        t = voice.t
        if (not wifi_voice.heard("settings", text, t) or wifi_voice._QUESTION.search(text)
                or wifi_voice.words(text) > PHRASE_MAX_WORDS):
            return False
        await self.open(voice)
        return True

    async def open(self, voice) -> None:
        """Show a code (or say how to get there without a screen)."""
        global _screen
        from teaport_brain import wifi_voice
        t = voice.t
        online = getattr(voice, "box_online", None)
        if online is not None and not await online():
            # Spoken Wi-Fi setup is here and the box is offline: no phone reaches it.
            logger.info("settings: asked for, but the box is offline")
            await voice.say(t._(OFFLINE_TEXT))
            return
        try:
            offer = start()
        except pairing.PairingUnavailable as e:
            logger.warning(f"settings: no code: {e.reason}")
            await voice.say(t._(UNAVAILABLE_TEXT))
            return
        secs = max(1.0, offer.expires_at - _clock())
        screen = _screen = _screen or self._new_screen()
        minutes = max(1, math.ceil(secs / 60))
        if screen.fits_qr(offer.link) and screen.show(
                SCREEN_TITLE, ["Point your camera", f"Code {offer.code}",
                               f"Open for {minutes} min"], secs=secs, qr=offer.link):
            logger.info("settings: code shown")
            await voice.say(t._(SHOWN_TEXT))
            return
        logger.info("settings: no screen draws the code -- reading it out")
        address = (wifi_voice.spoken_address(pairing.FRONTDOOR_HOST, t) + " "
                   + t.p("spell", "slash") + " settings")
        await voice.say(t._(SPOKEN_TEXT).format(
            address=address, code=wifi_voice.spell(offer.code.lower(), t)))
