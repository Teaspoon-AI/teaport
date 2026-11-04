#
# teaport — Wi-Fi setup: a temporary setup network and a phone page (teaport-wifi-setup).
#
# How a box with no internet gets onto one, with no LLM involved (the cloud model needs
# the internet the box does not have yet):
#
#   1. scan the networks in range (one radio: it cannot scan while it is an access point)
#   2. become an access point, teaport-ab12 (the last four hex digits of the Wi-Fi MAC),
#      WPA2 with a password -- the one written at flash time for the paper insert
#      (WIFI_SETUP_PASSWORD in /etc/teaport/wifi-setup.env), else a fresh random one each
#      time: twelve characters in three groups of four, "k7mq-x4tn-p9wr"
#      (setup_password), the hyphens part of it -- what the screen shows is what is typed
#   3. serve the setup page at 10.42.0.1 (wifi.AP_ADDRESS, pinned in the setup profile),
#      and only there — never on the box's other networks: on PAGE_PORT, which the
#      unit's iptables rule makes port 80 for that address (port 80 itself is the front
#      door's — Caddy's http->https redirect). NM's dnsmasq answers every name with that
#      address (/etc/NetworkManager/dnsmasq-shared.d/teaport-captive.conf, put there by
#      the unit for as long as it runs), so a
#      phone's captive-portal probe lands here and the OS opens the page by itself;
#      http://teaport-ab12.local is the fallback (avahi-publish, while the network is up)
#   4. the user picks a network and types its password on the phone; the access point
#      goes down and the box joins it. Internet confirmed: done. Otherwise the network's
#      new profile is dropped, the access point comes back and the page says why.
#      The page also takes the key of the owner's own AI account (an OpenRouter key,
#      optional: left empty, the box keeps the account it has). It goes with the Wi-Fi
#      choice, in memory only, and is checked once the box is online through the network
#      picked -- setup mode itself has no internet -- by the one way every page takes a
#      key (ai_account.adopt): saved only if the provider accepts it. How that went rides
#      in the "connected" status, and the voice says it (wifi_voice.py); a key never
#      leaves this process but to the provider and the brain's key file.
#   5. ten minutes without success, or the unit stopped (a spoken "cancel", SIGTERM):
#      the access point goes down, a half-done join is undone, and the connection the
#      box had before is brought back up.
#
# The setup network's name and password, and then how the join goes, are also on the
# box's display if it has one (display.py: the OLED avatar's screens), with a QR code
# that a phone camera joins the setup network from (wifi_qr) — the text in English and
# ASCII, which its built-in font draws (network names folded into it); the voice and
# the page speak the user's language.
#
# The brain (wifi_voice.py) starts this unit, points at the screen (or, without one,
# speaks the network, the password and the address), and follows status.json in the unit's RuntimeDirectory (phase: scanning,
# ap_up, joining, connected, failed, timeout, error) to say what happened — the phone
# has left the page by then. Each run has its own run id, so a stale file is never read
# as this run's news.
#
# No Pipecat import: this runs alone, briefly, on a box that is short of memory. The
# password is never logged, and the page and status file show only what the user is
# already told aloud.
#
import argparse
import html
import json
import os
import random
import re
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from teaport_brain import ai_account, conversation_log, display, i18n
from teaport_brain.i18n import N_
from teaport_brain.settings import setting
from teaport_brain.wifi import AP_ADDRESS, CONNECT_WAIT_SECS, NM, PAGE_CSS, logo_html

# Where the page listens. teaport-wifi-setup.service sends AP_ADDRESS:80 here with an
# iptables DNAT rule while it runs (packaging/wifi-setup/wifi-setup-net names this port too).
PAGE_PORT = 7869
# The page is open to anyone on the setup network: a form is a few hundred bytes, a
# request that dawdles is dropped, and only so many are served at once.
MAX_FORM_BYTES = 4096
REQUEST_TIMEOUT_SECS = 10
MAX_CLIENTS = 32
# How long the setup network's address may take to show on the device once NM says the
# network is up (only a warning when it does not: the page is bound to it regardless).
ADDRESS_WAIT_SECS = 5.0
SSID_PREFIX = "teaport-"
MINUTES = 10
# The default password (setup_password): GROUPS groups of GROUP_LEN characters joined by
# "-", from lowercase letters and digits a reader cannot confuse (no 0/o, no 1/l/i) nor a
# listener (no q: in Japanese it is キュー, which sounds as 9, きゅう). 30^12, about
# 2^58.9: out of reach of an offline guess at a captured WPA2 handshake, which
# eight digits (10^8) were not. Scanned from the QR code, it costs nothing to type; read
# from the screen it is three short groups, and spoken, each character and "dash".
PASSWORD_ALPHABET = "abcdefghjkmnprstuvwxyz23456789"
GROUPS, GROUP_LEN = 3, 4
STATUS_FILE = "status.json"
# The unit's RuntimeDirectory=teaport-wifi-setup (kept after the run with
# RuntimeDirectoryPreserve=yes, so the brain reads how it ended).
STATUS_DIR = "/run/teaport-wifi-setup"
# The display's lines: what wifi_voice says aloud, for reading off the box.
SCREEN_TITLE = "Setup mode"
# After it: the box has left setup mode and is on the user's network.
CONNECTED_TITLE = "Wi-Fi"
CONNECTED_SCREEN_SECS = 10
# How long avahi-publish must stay up before its .local name is shown.
PUBLISH_SETTLE_SECS = 0.2
# The status reason when the page's server cannot start; English only (see _run).
PAGE_FAILED = "the Wi-Fi page could not start"
# The status reason when the box has no Wi-Fi device: nothing a retry can change.
NO_WIFI_DEVICE = N_("this box has no Wi-Fi device")
# Read by the unit (EnvironmentFile=-/etc/teaport/wifi-setup.env), which the flasher
# writes so the paper insert can carry the password. Unset: a fresh one per setup.
PASSWORD = setting("WIFI_SETUP_PASSWORD", default="")
SSID = setting("WIFI_SETUP_SSID", default="")


def log(msg: str) -> None:
    print(f"[wifi-setup] {msg}", flush=True)


# ------------------------------------------------------------------ the box's identity

def setup_ssid(mac: str, override: str = "") -> str:
    """teaport-ab12 from the Wi-Fi MAC's last four hex digits (two boxes never clash).
    An override that is all hex digits ("12345678", "cafe") is refused: ZXing-based
    scanners (many Android camera apps) read such an SSID in the QR code as raw bytes,
    and join a network that does not exist."""
    name = override.strip()[:32]
    if re.fullmatch(r"[0-9A-Fa-f]+", name):
        log(f"WIFI_SETUP_SSID {name!r} is all hex digits, which some phones read as bytes "
            "— using the MAC-based name")
        name = ""
    if name:
        return name
    hexdigits = re.sub(r"[^0-9a-f]", "", mac.lower())
    return SSID_PREFIX + (hexdigits[-4:] if len(hexdigits) >= 4 else "0000")


# The symbols the voice can name when it spells the password (wifi_voice._SYMBOLS; kept
# apart because this module must not import Pipecat). Any other symbol would reach the
# voice bare, and a comma or a slash read aloud is a pause or nothing.
PASSWORD_SYMBOLS = frozenset("-_.@!#&* ")


def setup_password(fixed: str = "") -> str:
    """The flash-time password if valid -- 8 to 63 characters (WPA2), every one an ASCII
    letter, a digit or one of PASSWORD_SYMBOLS, so the voice can spell it -- else a fresh
    one from the system's CSPRNG (secrets): GROUPS groups of GROUP_LEN characters of
    PASSWORD_ALPHABET, joined by "-"."""
    fixed = fixed.strip()
    if fixed:
        if 8 <= len(fixed) <= 63 and all(
                (c.isascii() and c.isalnum()) or c in PASSWORD_SYMBOLS for c in fixed):
            return fixed
        log("WIFI_SETUP_PASSWORD is not 8-63 letters, digits and - _ . @ ! # & * or space "
            "— using a random one")
    return "-".join("".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(GROUP_LEN))
                    for _ in range(GROUPS))


# ------------------------------------------------------------------ status for the brain

class Status:
    """status.json in the RuntimeDirectory, replaced atomically per phase."""

    def __init__(self, directory: str, run_id: str):
        self._path = os.path.join(directory, STATUS_FILE)
        self.run_id = run_id
        self.data: dict = {}

    def set(self, phase: str, **fields) -> None:
        self.data = {"run_id": self.run_id, "phase": phase, "time": time.time(), **fields}
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self._path)
        except OSError as e:
            log(f"status not written ({e})")
        log(f"phase: {phase}" + (f" ({fields.get('target') or fields.get('reason')})"
                                 if fields.get("target") or fields.get("reason") else ""))


# ------------------------------------------------------------------ the page

PAGE = """<!doctype html><html lang="{lang}" dir="{dir}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head><body><main>
<div class="top"><div class="logo">{logo}</div>{picker}</div>
<h1>{heading}</h1>
<p>{intro}</p>
{error}
<form method="post" action="/connect?lang={lang_q}">
{networks}
<label class="net"><input type="radio" name="ssid" value="" {other_checked}>
<span>{other}</span></label>
<input type="text" name="other" placeholder="{name}" autocomplete="off" autocapitalize="none">
<label class="f" for="pw">{password}</label>
<input type="password" id="pw" name="password" autocomplete="off" autocapitalize="none" spellcheck="false">
<label class="show"><input type="checkbox" onclick="pw.type=this.checked?'text':'password'"> {show}</label>
{account}
<button type="submit">{connect}</button>
</form></main></body></html>"""

JOINING = """<!doctype html><html lang="{lang}" dir="{dir}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>{css}</style></head><body><main>
<div class="logo">{logo}</div>
<h1>{heading}</h1>
<p>{body}</p></main></body></html>"""


def _lang_attr(t: i18n.T) -> str:
    return t.lang.replace("_", "-")


def render_picker(t: i18n.T) -> str:
    """The language picker: the phone's language is the default, this the override.
    A GET form, so it works without script; the script only saves the button press."""
    options = "".join(
        f'<option value="{code}"{" selected" if code == t.lang else ""}>{html.escape(name)}</option>'
        for code, name in i18n.ENDONYMS.items() if i18n.chosen(code))
    label = html.escape(t._("Language"), quote=True)
    return (f'<form class="lang" method="get" action="/">'
            f'<select name="lang" aria-label="{label}" onchange="this.form.submit()">{options}</select>'
            f'<noscript><button type="submit">→</button></noscript></form>')


# What the account step says (render_account).
ACCOUNT_HELP = N_("Leave this empty to keep the account Teaport has now. To use your own "
                  "OpenRouter account, paste its key here. Teaport checks the key once it's "
                  "online, and says out loud whether it worked.")


# Signing in with OpenRouter instead (#282) cannot happen here: the phone on the setup
# network has no internet to reach OpenRouter, and the box none to finish the sign-in.
# Settings can, once both are back on the home network.
SIGN_IN_LATER = N_("To connect your OpenRouter account by signing in instead, finish "
                   "setting up Wi-Fi, then say \"open my settings\".")


def sign_in_later() -> bool:
    """Whether the account step points at Settings for OpenRouter's sign-in: the box has
    a Settings site (settings_access.available's two settings, read here without its
    import, which this unit cannot afford) and the sign-in is not turned off
    (openrouter_connect.callback_url's setting)."""
    return (bool((setting("TEAPORT_FRONTDOOR_HOST") or "").strip())
            and bool(setting("TEAPORT_SETTINGS_PORT"))
            and (setting("TEAPORT_OPENROUTER_CONNECT_URL") or "").lower().startswith("https://"))


def render_account(t: i18n.T) -> str:
    """The AI account step: the owner's own key, optional, closed until tapped. OpenRouter's
    sign-in is for later, in Settings (SIGN_IN_LATER)."""
    later = f'<p>{html.escape(t._(SIGN_IN_LATER))}</p>' if sign_in_later() else ""
    return (f'<details class="acct"><summary>{html.escape(t._("AI account (optional)"))}</summary>'
            f'<p>{html.escape(t._(ACCOUNT_HELP))}</p>{later}'
            f'<label class="f" for="ai_key">{html.escape(t._("OpenRouter key"))}</label>'
            '<input type="password" id="ai_key" name="ai_key" autocomplete="off" '
            f'autocapitalize="none" spellcheck="false" maxlength="{ai_account.KEY_MAX_CHARS}">'
            '</details>')


def render_page(networks: list[dict], error: str = "", t: i18n.T | None = None,
                account: bool = False) -> str:
    """The page, in t's language. `error` is already in that language. `account`: the AI
    account step is offered (the box's endpoint is OpenRouter's: main)."""
    t = t or i18n.get(i18n.SOURCE_LANG)
    rows = []
    for i, n in enumerate(networks):
        lock = t._("open") if n["open"] else "🔒"
        rows.append(
            f'<label class="net"><input type="radio" name="ssid" value="{html.escape(n["ssid"], quote=True)}"'
            f'{" checked" if i == 0 else ""}><span><bdi>{html.escape(n["ssid"])}</bdi></span>'
            f'<small dir="ltr">{lock} {n["signal"]}%</small></label>')
    return PAGE.format(
        lang=_lang_attr(t), dir=t.dir, css=PAGE_CSS, logo=logo_html(),
        picker=render_picker(t), lang_q=t.lang,
        title=html.escape(t._("Connect Teaport to Wi-Fi")),
        heading=html.escape(t._("Connect to Wi-Fi")),
        intro=html.escape(t._("Pick your network and type its password. Teaport will leave "
                              "this setup network and join yours, and say out loud whether "
                              "that worked.")),
        error=f'<p class="err">{_isolate(error, [n["ssid"] for n in networks])}</p>' if error else "",
        networks="\n".join(rows) or f"<p>{html.escape(t._('No networks found — use Other.'))}</p>",
        other_checked="" if networks else "checked",
        other=html.escape(t._("Other (hidden) network")),
        name=html.escape(t._("Network name"), quote=True),
        password=html.escape(t._("Password")),
        show=html.escape(t._("Show password")),
        connect=html.escape(t._("Connect")),
        account=render_account(t) if account else "")


def _isolate(text: str, names: list[str]) -> str:
    """Escape `text`, wrapping each network name in it in <bdi>: a Latin name inside
    Arabic (right-to-left) text keeps its own direction instead of being reordered.
    One pass over the plain text, longest name first, whole occurrences only (a network
    called "a" is not every "a" in the sentence), escaping as it goes — so a name never
    lands inside an entity or a tag this put in."""
    names = sorted({n for n in names if n}, key=len, reverse=True)
    if not names:
        return html.escape(text)
    # "Whole" by ASCII letters and digits only: a name runs straight into Japanese or
    # Chinese text, which has no spaces, and must still be found there.
    edge = re.compile(r"[A-Za-z0-9]")
    pattern = re.compile("|".join(
        ("(?<![A-Za-z0-9])" if edge.match(n[0]) else "") + re.escape(n)
        + ("(?![A-Za-z0-9])" if edge.match(n[-1]) else "") for n in names))
    out, pos = [], 0
    for m in pattern.finditer(text):
        out += [html.escape(text[pos:m.start()]), f"<bdi>{html.escape(m.group())}</bdi>"]
        pos = m.end()
    return "".join(out) + html.escape(text[pos:])


def render_joining(ssid: str, t: i18n.T) -> str:
    strong = f"<strong><bdi>{html.escape(ssid)}</bdi></strong>"
    return JOINING.format(
        lang=_lang_attr(t), dir=t.dir, css=PAGE_CSS, logo=logo_html(),
        title=html.escape(t._("Teaport is joining")),
        heading=html.escape(t._("Joining {network}…")).format(network=f"<bdi>{html.escape(ssid)}</bdi>"),
        body=html.escape(t._("Teaport is leaving this setup network now. Switch your phone "
                             "back to {network}. Teaport will say out loud whether it "
                             "connected; if it could not, the setup network comes back and "
                             "you can try again.")).format(network=strong))


def parse_key(body: bytes) -> str:
    """The AI account key from the posted form, stripped; "" for none."""
    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return (form.get("ai_key") or [""])[0].strip()


def parse_form(body: bytes) -> tuple[str, str, bool, str]:
    """(ssid, password, hidden, error) from the posted form."""
    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    ssid = (form.get("ssid") or [""])[0]
    other = (form.get("other") or [""])[0].strip()
    password = (form.get("password") or [""])[0]
    hidden = not ssid and bool(other)
    ssid = ssid or other
    if not ssid:
        return "", "", False, N_("Pick a network, or type its name under Other.")
    if len(ssid.encode()) > 32:
        return "", "", False, N_("That network name is too long.")
    if password and not 8 <= len(password) <= 63:
        return "", "", False, N_("Wi-Fi passwords are 8 to 63 characters.")
    return ssid, password, hidden, ""


# The screen's line for the password: "Password  <it>" while that stays inside the panel's
# width (about 21 characters of the avatar's 10 px font: "Network  teaport-ab12" is 111
# of its 124 px), else "Pass  <it>" -- the default's 14 characters with "Password" would
# scroll sideways, which a password must not.
SCREEN_LINE_CHARS = 21


def password_line(password: str) -> str:
    line = f"Password  {password}"
    return line if len(line) <= SCREEN_LINE_CHARS else f"Pass  {password}"


# What the Wi-Fi join string escapes with a backslash: the backslash itself, ; , : and ".
_QR_SPECIAL = re.compile(r'([\\;,:"])')


def wifi_qr(ssid: str, password: str) -> str:
    """The Wi-Fi join string a phone camera reads off a QR code (the iPhone camera, and
    Android's, offer "Join network ..."), for the WPA setup network."""
    ssid, password = (_QR_SPECIAL.sub(r"\\\1", x) for x in (ssid, password))
    return f"WIFI:T:WPA;S:{ssid};P:{password};;"


class Stopped(Exception):
    """SIGTERM: systemctl stop (a spoken "cancel"), a restart, or RuntimeMaxSec."""


def _on_sigterm(signum, frame):
    # Python's default for SIGTERM is to die on the spot, past every finally: the box
    # would stay off its old network. Raise instead, once — the clean-up must not be
    # interrupted by a second one.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise Stopped()


class _Server(ThreadingHTTPServer):
    """The page's server: bound to one address that comes and goes (the setup network is
    down while the box tries a join), with a cap on connections served at once."""

    daemon_threads = True

    def __init__(self, address, handler):
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(address, handler)

    def server_bind(self):
        # IP_FREEBIND (15 on Linux; not in the socket module): bind even while the
        # address is not on any interface. No getfqdn() either, as HTTPServer would do:
        # a reverse lookup on a box with no network is just a wait.
        self.socket.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_FREEBIND", 15), 1)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # full: this one is dropped, not queued
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Setup:
    """One run: the access point, the page, and the join attempts."""

    def __init__(self, nm: NM, status: Status, port: int = PAGE_PORT, minutes: float = MINUTES,
                 bind: str | None = None, screen: display.Screen | None = None,
                 account: str | None = None):
        self.nm, self.status, self.port = nm, status, port
        self.screen = screen
        # The AI endpoint the account step's key is for (LLM_BASE_URL), when the step is
        # offered (an OpenRouter endpoint: main); None, and the page has no such step.
        self.account = account
        self.key = ""  # the key posted with the last Wi-Fi choice (never logged)
        # Where the page listens: the setup network's own address (AP_ADDRESS, pinned in
        # its profile), never 0.0.0.0 — the box's other networks must not reach a page
        # that changes its Wi-Fi without a password. `bind` is for tests (127.0.0.1).
        self.bind = bind
        self.address = AP_ADDRESS
        self.previous: dict | None = None  # the profile to put back, {"name", "uuid"}
        self.created: str | None = None    # a profile the last join made, until dropped
        self.deadline = time.monotonic() + minutes * 60
        self.networks: list[dict] = []
        # The last failed join: (network, reason) — the reason an English msgid, put in
        # the reader's language per request (error_text). "" in the status file for none.
        self.error = ""
        self.failure: tuple[str, str] | None = None
        self.request: tuple[str, str, bool] | None = None
        self.wake = threading.Event()
        self.server: ThreadingHTTPServer | None = None
        self.avahi: subprocess.Popen | None = None
        self.avahi_name = ""  # the name avahi-publish was started for

    # -- the web side (server threads)
    def handler(self):
        setup = self

        class Handler(BaseHTTPRequestHandler):
            timeout = REQUEST_TIMEOUT_SECS  # a socket timeout per request

            def log_message(self, *a):  # no request log: paths are noise, bodies secret
                pass

            def _send(self, code: int, body: str, ctype="text/html; charset=utf-8", extra=()):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _t(self) -> tuple[i18n.T, list]:
                """The reader's language: a choice made on the picker (?lang=, then kept
                in a cookie), else the phone's own (Accept-Language). Returns it and the
                headers that remember a new choice."""
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                picked = i18n.chosen((query.get("lang") or [None])[0])
                if picked:
                    return picked, [("Set-Cookie", f"teaport_lang={picked.lang}; Path=/; SameSite=Lax")]
                # Only a catalog's name gets through: chosen() checks it against them.
                cookie = re.search(r"(?:^|;\s*)teaport_lang=([A-Za-z_-]+)", self.headers.get("Cookie") or "")
                kept = i18n.chosen(cookie.group(1)) if cookie else None
                return kept or i18n.for_accept_language(self.headers.get("Accept-Language")), []

            def do_GET(self):
                # Every path is the page: the captive-portal probes (Apple's
                # hotspot-detect, Android's generate_204, Windows' connecttest) expect
                # something else, so the phone shows this as a sign-in page.
                if self.path.startswith("/generate_204") or self.path.startswith("/gen_204"):
                    return self._send(302, "", extra=[("Location", f"http://{setup.address}/")])
                t, remember = self._t()
                self._send(200, render_page(setup.networks, setup.error_text(t), t,
                                            account=setup.account is not None), extra=remember)

            def do_POST(self):
                raw = (self.headers.get("Content-Length") or "0").strip()
                if not (raw.isascii() and raw.isdigit()):
                    return self.send_error(400)  # read(-1) would read until the client stops
                if int(raw) > MAX_FORM_BYTES:
                    return self.send_error(413)
                try:
                    body = self.rfile.read(int(raw))
                except OSError:  # the timeout, or the phone left
                    return
                ssid, password, hidden, error = parse_form(body)
                t, remember = self._t()
                if error:
                    return self._send(200, render_page(setup.networks, t._(error), t,
                                                       account=setup.account is not None),
                                      extra=remember)
                self._send(200, render_joining(ssid, t), extra=remember)
                setup.key = parse_key(body) if setup.account is not None else ""
                setup.request = (ssid, password, hidden)
                setup.wake.set()

        return Handler

    def serve(self) -> None:
        self.server = _Server((self.bind or self.address, self.port), self.handler())
        self.port = self.server.server_address[1]  # the real one when asked for port 0
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def error_text(self, t: i18n.T) -> str:
        if not self.failure:
            return ""
        network, reason = self.failure
        return t._("Could not join {network}: {reason}. Try again.").format(
            network=network, reason=t._(reason))

    # -- the network side (main thread)
    def publish(self, name: str) -> None:
        """Announce name.local for the page, best effort. Whether it is being announced
        is asked live each time the address is shown (page_url)."""
        self.avahi_name = name
        exe = shutil.which("avahi-publish")
        if not exe:
            log("avahi-publish not installed — only the captive page and 10.42.0.1 work")
            return
        try:
            self.avahi = subprocess.Popen([exe, "-a", "-R", f"{name}.local", self.address],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log(f"avahi-publish failed ({e})")
            return
        # Without avahi-daemon it exits within milliseconds -- after page_url's poll()
        # would already have counted it as running. A short wait catches that; past it,
        # the live poll() takes over (avahi dying later).
        try:
            rc = self.avahi.wait(timeout=PUBLISH_SETTLE_SECS)
        except subprocess.TimeoutExpired:
            return
        log(f"avahi-publish exited at once ({rc}) — only the captive page and 10.42.0.1 work")
        self.avahi = None

    def page_url(self, ssid: str) -> str:
        """The page's address, the one the screen shows, the status carries and the voice
        reads -- decided here only, each time it is shown, so the three never disagree.
        http://teaport-ab12.local while avahi-publish is running for exactly that name
        and the screen's font draws it as it is (an ASCII teaport-... name); otherwise
        the bare address, which always works (avahi missing or gone, a folded or custom
        name)."""
        published = (self.avahi is not None and self.avahi.poll() is None
                     and self.avahi_name == ssid)
        if published and ssid.startswith(SSID_PREFIX) and display.fold_ascii(ssid) == ssid:
            return f"http://{ssid}.local"
        return f"http://{self.address}"

    def unpublish(self) -> None:
        if self.avahi and self.avahi.poll() is None:
            self.avahi.terminate()
        self.avahi = None

    def run(self, ssid: str, password: str) -> int:
        """0 connected, 1 an error, 2 timed out, 3 stopped. A stop (SIGTERM) can land
        anywhere in here, never as a traceback: before there is a result it is 3, and
        during the clean-up the clean-up still finishes and the result stands."""
        rc = 3
        try:
            rc = self._start(ssid, password)
        except Stopped:
            log("stopped before setup began")  # nothing changed yet: nothing to put back
            self.status.set("stopped")
        finally:
            try:
                self._close()
            except Stopped:
                self._close()  # once more, whole: _on_sigterm ignores a second stop
        return rc

    def _start(self, ssid: str, password: str) -> int:
        dev = self.nm.wifi_device()
        if not dev:
            self.status.set("error", reason=NO_WIFI_DEVICE)
            return 1
        self.previous = self.nm.active(dev)
        try:
            return self._run(dev, ssid, password)
        except Stopped:
            log("stopped — putting the previous connection back")
            self.status.set("stopped")
            self._put_back(quick=True)
            return 3

    def _close(self) -> None:
        if self.screen:
            self.screen.close()  # a "connected" screen stays its few seconds
        self.unpublish()
        if self.server:
            self.server.shutdown()
        self.nm.ap_delete()

    def _show(self, lines: list[str], secs: float | None = None, qr: str | None = None,
              title: str = SCREEN_TITLE) -> bool:
        return bool(self.screen and self.screen.show(title, lines, secs, qr=qr))

    def _address_arrives(self, dev: str) -> bool:
        deadline = time.monotonic() + ADDRESS_WAIT_SECS
        while AP_ADDRESS not in self.nm.addresses(dev):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
        return True

    def _adopt_key(self) -> dict | None:
        """The key typed with the Wi-Fi choice, checked now that the box is online and
        saved if it works (ai_account.adopt): {"result": "saved" | "refused" | "unknown",
        "reason": English, "ours": the reason is a msgid}, or None when none was typed.
        The key is forgotten here either way."""
        key, self.key = self.key, ""
        if not key or self.account is None:
            return None
        check = ai_account.adopt_blocking(key, self.account)
        log(f"the AI account key: {check.state}")
        return {"result": "saved" if check.state == ai_account.WORKS else check.state,
                "reason": check.reason, "ours": check.ours}

    def _put_back(self, quick: bool = False) -> None:
        """The way out without a new network (the timeout, a stop, an error): the setup
        network down, a half-done join undone, and the old connection back up. `quick`
        (a stop: systemd is waiting) asks NM for the old connection without waiting.
        The screen goes first: the password must not stay up while NM takes its time."""
        if self.screen:
            self.screen.clear()
        self.unpublish()
        self.nm.ap_down()
        self.nm.undo()
        if self.created:
            self.nm.delete(self.created)
            self.created = None
        if self.previous:
            self.nm.up(self.previous["uuid"], wait=0 if quick else CONNECT_WAIT_SECS)

    def _run(self, dev: str, ssid: str, password: str) -> int:
        self.status.set("scanning")
        self.networks = [n for n in self.nm.scan(dev) if n["ssid"] != ssid]
        log(f"{len(self.networks)} networks in range; previous connection: "
            f"{self.previous['name'] if self.previous else 'none'}")
        # The display's font draws ASCII only: a name outside it is folded.
        shown_ssid = display.fold_ascii(ssid)
        while True:
            ok, err = self.nm.ap_up(dev, ssid, password)
            if not ok:
                log(f"setup network failed: {err}")
                self.status.set("error", reason=N_("the setup network would not start"))
                self._put_back()
                return 1
            if not self.server:
                # The page binds AP_ADDRESS, which the setup profile pins: IP_FREEBIND
                # lets it bind before the address is on the device. Not whatever address
                # NM lists first right after the network comes up — that can still be the
                # old network's, and then no phone reaches the page (seen 2026-10-07: it
                # bound the home address and the captive page never opened).
                if not self._address_arrives(dev):
                    log(f"{AP_ADDRESS} is not on {dev} after {ADDRESS_WAIT_SECS:.0f} s: "
                        "phones may not reach the page")
                try:
                    self.serve()
                except OSError as e:
                    log(f"the Wi-Fi page could not start: {e}")
                    # Not spoken (the voice says it could not go into setup mode and
                    # to try again): the reason is for the log and the model's notice.
                    self.status.set("error", reason=PAGE_FAILED)
                    self._put_back()
                    return 1
            self.publish(ssid)
            url = self.page_url(ssid)  # asked again at every (re-)show: avahi can die
            # On the display first: the status says whether it got there, and with a code
            # to scan, and the voice points at it only then. The code carries the real
            # name, whatever the font can draw -- and is sent only to an avatar that
            # draws one that long at a size a phone scans (fits_qr). A name the font
            # cannot draw as it is (folded, it would read wrong) points at the code
            # when there is one.
            code = wifi_qr(ssid, password)
            code = code if self.screen and self.screen.fits_qr(code) else None
            name = shown_ssid if shown_ssid == ssid or not code else "(scan the code)"
            shown = self._show([f"Network  {name}", password_line(password), url], qr=code)
            self.status.set("ap_up", ssid=ssid, password=password, url=url,
                            address=self.address, error=self.error, screen=shown,
                            qr=shown and code is not None)
            self.request = None
            self.wake.clear()
            while not self.wake.wait(timeout=1.0):
                if time.monotonic() >= self.deadline:
                    self.status.set("timeout")
                    self._put_back()
                    return 2
            target, pw, hidden = self.request
            time.sleep(1.5)  # let the "joining" page reach the phone first
            self.status.set("joining", target=target)
            self._show(["Joining", display.fold_ascii(target)])
            self.unpublish()
            self.nm.ap_down()
            ok, why, self.created = self.nm.join(dev, target, pw, hidden)
            if ok and self.nm.online(dev):
                self.created = None  # kept: it is the box's network now
                account = self._adopt_key()
                self.status.set("connected", target=target,
                                **({"account": account} if account else {}))
                self._show(["Connected to", display.fold_ascii(target)], secs=CONNECTED_SCREEN_SECS,
                           title=CONNECTED_TITLE)
                return 0
            if ok:
                why = N_("it joined but there is no internet through it")
            if self.created:
                self.nm.delete(self.created)
                self.created = None
            self.failure = (target, why)
            self.error = self.error_text(i18n.get(i18n.SOURCE_LANG))
            self.status.set("failed", target=target, reason=why)
            # Held until the setup network is back and its details replace it (or the
            # way out takes it down).
            self._show(["Could not join", display.fold_ascii(target)])
            if time.monotonic() >= self.deadline:
                self.status.set("timeout")
                self._put_back()
                return 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="teaport Wi-Fi setup (run by teaport-wifi-setup.service)")
    ap.add_argument("--port", type=int, default=PAGE_PORT)
    ap.add_argument("--minutes", type=float, default=MINUTES)
    ap.add_argument("--status-dir", default=STATUS_DIR)
    ap.add_argument("--run-id", default="")
    args = ap.parse_args(argv)
    conversation_log.install()
    nm = NM()
    dev = nm.wifi_device()
    ssid = setup_ssid(nm.mac(dev) if dev else "", SSID)
    password = setup_password(PASSWORD)
    run_id = args.run_id or f"{int(time.time())}-{random.randrange(1 << 16):04x}"
    os.makedirs(args.status_dir, exist_ok=True)
    status = Status(args.status_dir, run_id)
    log(f"run {run_id}: setup network {ssid}, page on port {args.port}, "
        f"{'flash-time' if PASSWORD.strip() == password else 'random'} password")
    # The account step only for an OpenRouter endpoint (OpenRouter keys only, #283); the
    # unit reads brain.env for it (teaport-wifi-setup.service).
    base_url = setting("LLM_BASE_URL")
    setup = Setup(nm, status, port=args.port, minutes=args.minutes,
                  screen=display.Screen("wifi-setup"),
                  account=base_url if ai_account.is_openrouter(base_url) else None)
    signal.signal(signal.SIGTERM, _on_sigterm)
    return setup.run(ssid, password)


if __name__ == "__main__":
    sys.exit(main())
