#
# teaport — Settings from the owner's phone (#279): "open my settings" by voice, a
# one-time code, and a session the config page takes in place of its token.
#
# The config page (config_ui.py, static/config.html) is plain HTTP on the brain's port
# and asks for GATEWAY_TOKEN, which lives in /etc/teaport/brain.env: changing the AI key
# took SSH. This is the owner's way in, on the front door (Caddy, `tls internal` with the
# box's own CA, install.sh write_caddyfile), the way pairing's short links are served
# (pairing.py) -- but on an origin of its own: https://<front door>:<SETTINGS_PORT>
# (TEAPORT_SETTINGS_PORT, 8443). The front door's default port is the OpenClaw gateway's,
# whose Control UI and whatever pages the agent serves run there; sharing that origin, a
# script of theirs could read the page or change a setting while the owner's session is
# live, and neither SameSite nor an Origin check could tell it from the page. Another port
# is another origin. Plain http://<front door>/settings (a typed address, the one the
# voice reads) is redirected there by Caddy.
#
#   1. "open my settings", a fixed phrase (SettingsPhrase, heard by the session's
#      PhraseVoice, wifi_voice.py) on the owner's surfaces: the room and Talk, never a
#      phone call nor a bridge that relays someone far away (agent_session.py). It needs
#      no model.
#   2. start(): a code, pairing's kind (pairing.new_code_text: six characters a reader
#      cannot confuse, pairing.OFFER_SECS, single use, pairing.MAX_WRONG wrong ones end
#      it). One at a time: asking again replaces it. The screen shows it as a QR code of
#      https://<front door>:8443/settings/<code> (display.Screen "settings"), the code itself
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
#   - only what came through the Settings site over HTTPS is answered here at all
#     (on_settings_site: Caddy over loopback saying https, for the Settings port): the
#     brain's own port, open to the LAN, gets a 404 that is no guess, and a Secure cookie
#     is never sent to it in cleartext;
#   - another origin cannot use the cookie: SameSite=Strict keeps the browser from
#     sending it on any cross-site request; a request that changes anything must carry
#     the Settings origin itself as its Origin (same_origin), and one a browser marks as
#     from another origin (Sec-Fetch-Site) is refused even to read -- the gateway's pages,
#     same site but another origin, included. A browser sets both headers itself.
#
# The pages are in the phone's language (Accept-Language, i18n.py). The steps to trust
# the box's certificate are pairing's own (pairing.trust), in English for now, as on the
# pairing pages.
#
import hashlib
import hmac
import html
import math
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from loguru import logger

from teaport_brain import conversation_log, display, i18n, pairing
from teaport_brain.i18n import N_
from teaport_brain.settings import setting

PREFIX = "/settings/"
PAGE_PATH = "/settings/config"
SETTINGS_PORT = setting("TEAPORT_SETTINGS_PORT")
COOKIE = "teaport_settings"
SESSION_SECS = 3600
SCREEN_ID, SCREEN_TITLE = "settings", "Settings"
# The screen after the code was used: the QR code is of no more use.
USED_SCREEN_SECS = 10.0
# "open my settings" said as a request of its own, not inside a longer sentence.
PHRASE_MAX_WORDS = 6

_clock = time.monotonic  # the code's clock; replaced in tests (a LiveCode reads it)
_wall = time.time        # the session's (a cookie outlives the process)

# What the voice says (SettingsPhrase). The quoted words are a phrase in each catalog
# one of its own patterns hears (tests/test_i18n.py).
SHOWN_TEXT = N_("I've put a code on my screen. Scan it with your phone's camera, then tap "
                "the link that appears to get to my settings.")
SPOKEN_TEXT = N_("In your phone's browser, go to {address}, and enter the code {code}. It "
                 "works once, for the next five minutes.")
# No network at all (the phone reaches the box over the home network, not the internet).
OFFLINE_TEXT = N_("I need to be online for that. Say \"set up Wi-Fi\" to get me online.")
UNAVAILABLE_TEXT = N_("Sorry, my settings can't be reached from a phone on this box.")


# The current code (pairing.LiveCode), kept with pairing's own CodeBook machinery: one
# at a time, single use, pairing.MAX_WRONG wrong guesses lock it.
_codes = pairing.CodeBook()
_live: pairing.LiveCode | None = None
_screen: display.Screen | None = None  # the screen showing the current code


def heard_here() -> bool:
    """Whether "open my settings" is listened for: the box has a front door. Where its
    Settings site is not set up yet, the phrase says so (available)."""
    return bool(pairing.FRONTDOOR_HOST)


def usable(phrases) -> bool:
    """Is "open my settings" usable where `phrases` (a session's fixed phrases) are heard:
    the phrase is among them -- the room and Talk, not a bridge, never a phone call -- and
    the Settings site is set up (available). The one test for any line that tells someone
    to say it (the no-model line, the setup-mode key line, the trial credit's)."""
    return available() and any(isinstance(p, SettingsPhrase) for p in (phrases or ()))


def available() -> bool:
    """Whether Settings can be offered: the front door's name is known, and its Settings
    site is set up -- TEAPORT_SETTINGS_PORT, which only the full installer writes, as it
    renders Caddy's site for it. An `install.sh --only brain` update of an older box
    leaves both out (it touches no system config), and a QR code to a port nothing
    listens on would be a dead end: the phrase says Settings cannot be reached instead,
    and nothing else points at it."""
    return bool(pairing.FRONTDOOR_HOST) and bool(SETTINGS_PORT)


def origin() -> str:
    """The Settings origin: https://<front door>:<SETTINGS_PORT>."""
    port = "" if SETTINGS_PORT == 443 else f":{SETTINGS_PORT}"
    return f"https://{pairing.FRONTDOOR_HOST}{port}"


def start() -> pairing.Offer:
    """A new code (the one before can no longer be used). Raises
    pairing.PairingUnavailable when the front door is unknown or no host name. A link too
    long for the screen's QR code is still made: the voice reads it out instead."""
    global _live
    pairing.check_host(pairing.FRONTDOOR_HOST, PREFIX, qr=False)
    code = _codes.issue()
    offer = pairing.Offer(code, f"{origin()}{PREFIX}{code}", _clock() + pairing.OFFER_SECS)
    _live = pairing.LiveCode(offer, clock=lambda: _clock())
    logger.info(f"settings: a code was made, {pairing.OFFER_SECS:.0f} s to use it")
    return offer


def _find(code: str) -> str:
    return _codes.find(code, _live, "settings")


def _spend() -> None:
    _codes.spend(_live)
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
    """The request names the Settings origin as its own: a browser sets Origin itself on
    a request that changes anything, and a page elsewhere cannot make it say ours."""
    got = (request.headers.get("origin") or "").strip().lower().rstrip("/")
    return available() and got == origin().lower()


def on_settings_site(request: Request) -> bool:
    """Through Caddy over HTTPS (pairing.via_front_door), for the Settings port: Caddy
    passes the browser's Host on, so a request the gateway's site handed over (were it
    ever routed here) names another port and is refused."""
    if not available() or not pairing.via_front_door(request):
        return False
    _, _, port = (request.headers.get("host") or "").rpartition(":")
    return (int(port) if port.isdigit() else 443) == SETTINGS_PORT


def authorized(request: Request) -> bool:
    """Is this a Settings session from the owner's phone: on the Settings site, a valid
    session cookie, nothing a browser marks as from another origin, and for anything but
    a read, from the Settings page itself."""
    if not on_settings_site(request) or not session_ok(request.cookies.get(COOKIE)):
        return False
    if (request.headers.get("sec-fetch-site") or "same-origin").lower() not in (
            "same-origin", "none"):
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

# No page may frame Settings (clickjacking): the gateway's pages are same-site, so a frame
# of theirs would carry the Strict cookie and its fetches would pass the origin checks --
# a click on Restart, "Set up Wi-Fi" or "Save anyway" under someone else's page.
FRAME_GUARD = ((b"content-security-policy", b"frame-ancestors 'none'"),
               (b"x-frame-options", b"DENY"))


class FrameGuard:
    """ASGI middleware (gateway_server adds it): every answer under /settings carries
    FRAME_GUARD, whatever Caddyfile fronts it (Caddy's Settings site adds them too)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == "/settings" or path.startswith(PREFIX)):
            return await self.app(scope, receive, send)

        async def guarded(message):
            if message["type"] == "http.response.start":
                message["headers"] = [h for h in message.get("headers", [])
                                      if h[0].lower() not in dict(FRAME_GUARD)] + list(FRAME_GUARD)
            await send(message)

        await self.app(scope, receive, guarded)


async def _gate(request: Request, post: bool) -> tuple[Response | None, dict]:
    """pairing's checks for a code page (front door, small form), on the Settings site."""
    if not on_settings_site(request):
        return _NOT_FOUND, {}
    return await pairing.front_door_request(request, post)


def _redeemed(t: i18n.T, state: str, wrong: Response) -> Response:
    """A code looked at for spending: the session, or why not (`wrong` for a live-less
    "expired")."""
    if state == "live":
        _spend()
        return _signed_in()
    return _used(t) if state == "used" else wrong


@router.get("/settings")
@router.get("/settings/")
async def enter(request: Request) -> Response:
    refused, _ = await _gate(request, post=False)
    if refused is not None:
        return refused
    if session_ok(request.cookies.get(COOKIE)):
        return RedirectResponse(PAGE_PATH, status_code=303, headers=pairing.NO_STORE)
    return _enter(_t(request))


@router.post("/settings")
@router.post("/settings/")
async def enter_code(request: Request) -> Response:
    """A code read aloud (or under the QR code), typed in."""
    refused, form = await _gate(request, post=True)
    if refused is not None:
        return refused
    t = _t(request)
    typed = "".join((form.get("code") or [""])[0].split())
    if not typed:
        return _enter(t)
    return _redeemed(t, _find(typed), _enter(
        t, t._("That code didn't work. Check it, or ask Teaport for a new one.")))


@router.get("/settings/trust")
async def trust_steps(request: Request) -> Response:
    """How to trust the box's certificate on each kind of device (pairing's steps), for
    the Settings page to link to: no code needed, nothing spent."""
    refused, _ = await _gate(request, post=False)
    if refused is not None:
        return refused
    if pairing.TRUSTED_CERT:  # nothing to trust: devices already do
        return RedirectResponse(PAGE_PATH, status_code=303, headers=pairing.NO_STORE)
    t = _t(request)
    return _page(t, t._("Settings"), _trust(t))


@router.get("/settings/{code}")
async def landing(code: str, request: Request) -> Response:
    """Unauthenticated: the phone has no session yet; the code is the grant. A GET
    spends nothing."""
    refused, _ = await _gate(request, post=False)
    if refused is not None:
        return refused
    t = _t(request)
    state = _find(code)
    if state == "live":
        return _landing(t, _live.code.code)
    return _used(t) if state == "used" else _expired(t)


@router.post("/settings/{code}")
async def redeem(code: str, request: Request) -> Response:
    refused, _ = await _gate(request, post=True)
    if refused is not None:
        return refused
    t = _t(request)
    return _redeemed(t, _find(code), _expired(t))


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
        on_network = getattr(voice, "box_on_network", None)
        if on_network is not None and not await on_network():
            # Spoken Wi-Fi setup is here and the box is on no network at all: no phone
            # reaches it. (No internet is no reason: the phone needs the home network.)
            logger.info("settings: asked for, but the box is on no network")
            await voice.say(t._(OFFLINE_TEXT))
            return
        if not available():
            logger.warning("settings: no Settings site on this box (TEAPORT_SETTINGS_PORT "
                           "unset) -- run install.sh without --only to set it up")
            await voice.say(t._(UNAVAILABLE_TEXT))
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
        code = wifi_voice.spell(offer.code.lower(), t)
        # The code is the owner's access until used: never in the journal, whatever
        # TEAPORT_LOG_CONVERSATIONS says (pipecat's "Generating TTS [...]", the ledger).
        conversation_log.never_log(code, sep=t.p("spell", ", "),
                                   secs=max(0.0, offer.expires_at - _clock()) + 60)
        await voice.say(t._(SPOKEN_TEXT).format(address=address, code=code))
