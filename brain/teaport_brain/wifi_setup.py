#
# teaport — Wi-Fi setup: a temporary setup network and a phone page (teaport-wifi-setup).
#
# How a box with no internet gets onto one, with no LLM involved (the cloud model needs
# the internet the box does not have yet):
#
#   1. scan the networks in range (one radio: it cannot scan while it is an access point)
#   2. become an access point, teaport-ab12 (the last four hex digits of the Wi-Fi MAC),
#      WPA2 with a password of digits — the one written at flash time for the paper
#      insert (WIFI_SETUP_PASSWORD in /etc/teaport/wifi-setup.env), else fresh random
#      digits each time
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
# The brain (wifi_voice.py) starts this unit, speaks the network, the password and the
# address, and follows status.json in the unit's RuntimeDirectory (phase: scanning,
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

from teaport_brain import display, i18n
from teaport_brain.i18n import N_
from teaport_brain.wifi import AP_ADDRESS, CONNECT_WAIT_SECS, NM, PAGE_CSS, logo_html

# Where the page listens. teaport-wifi-setup.service redirects AP_ADDRESS:80 here with
# an iptables rule for as long as it runs; phones only ever see port 80.
PAGE_PORT = 7869
# The page is open to anyone on the setup network: a form is a few hundred bytes, a
# request that dawdles is dropped, and only so many are served at once.
MAX_FORM_BYTES = 4096
REQUEST_TIMEOUT_SECS = 10
MAX_CLIENTS = 32
SSID_PREFIX = "teaport-"
MINUTES = 10
PASSWORD_DIGITS = 8
STATUS_FILE = "status.json"
# The unit's RuntimeDirectory=teaport-wifi-setup (kept after the run with
# RuntimeDirectoryPreserve=yes, so the brain reads how it ended).
STATUS_DIR = "/run/teaport-wifi-setup"
# The display's lines: what wifi_voice says aloud, for reading off the box.
SCREEN_TITLE = "Wi-Fi setup"
CONNECTED_SCREEN_SECS = 10
# Read by the unit (EnvironmentFile=-/etc/teaport/wifi-setup.env), which the flasher
# writes so the paper insert can carry the password. Unset: fresh digits per setup.
PASSWORD = os.getenv("WIFI_SETUP_PASSWORD", "")
SSID = os.getenv("WIFI_SETUP_SSID", "")


def log(msg: str) -> None:
    print(f"[wifi-setup] {msg}", flush=True)


# ------------------------------------------------------------------ the box's identity

def setup_ssid(mac: str, override: str = "") -> str:
    """teaport-ab12 from the Wi-Fi MAC's last four hex digits (two boxes never clash)."""
    if override.strip():
        return override.strip()[:32]
    hexdigits = re.sub(r"[^0-9a-f]", "", mac.lower())
    return SSID_PREFIX + (hexdigits[-4:] if len(hexdigits) >= 4 else "0000")


# The symbols the voice can name when it spells the password (wifi_voice._SYMBOLS; kept
# apart because this module must not import Pipecat). Any other symbol would reach the
# voice bare, and a comma or a slash read aloud is a pause or nothing.
PASSWORD_SYMBOLS = frozenset("-_.@!#&* ")


def setup_password(fixed: str = "") -> str:
    """The flash-time password if valid -- 8 to 63 characters (WPA2), every one an ASCII
    letter, a digit or one of PASSWORD_SYMBOLS, so the voice can spell it -- else digits."""
    fixed = fixed.strip()
    if fixed:
        if 8 <= len(fixed) <= 63 and all(
                (c.isascii() and c.isalnum()) or c in PASSWORD_SYMBOLS for c in fixed):
            return fixed
        log("WIFI_SETUP_PASSWORD is not 8-63 letters, digits and - _ . @ ! # & * or space "
            "— using random digits")
    return "".join(secrets.choice("0123456789") for _ in range(PASSWORD_DIGITS))


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


def render_page(networks: list[dict], error: str = "", t: i18n.T | None = None) -> str:
    """The page, in t's language. `error` is already in that language."""
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
        title=html.escape(t._("Teaport Wi-Fi setup")),
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
        connect=html.escape(t._("Connect")))


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
                 bind: str | None = None, screen: display.Screen | None = None):
        self.nm, self.status, self.port = nm, status, port
        self.screen = screen
        # Where the page listens: the setup network's own address (read from NM once it
        # is up), never 0.0.0.0 — the box's other networks must not reach a page that
        # changes its Wi-Fi without a password. `bind` is for tests (127.0.0.1).
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
                self._send(200, render_page(setup.networks, setup.error_text(t), t), extra=remember)

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
                    return self._send(200, render_page(setup.networks, t._(error), t), extra=remember)
                self._send(200, render_joining(ssid, t), extra=remember)
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
        exe = shutil.which("avahi-publish")
        if not exe:
            log("avahi-publish not installed — only the captive page and 10.42.0.1 work")
            return
        try:
            self.avahi = subprocess.Popen([exe, "-a", "-R", f"{name}.local", self.address],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log(f"avahi-publish failed ({e})")

    def unpublish(self) -> None:
        if self.avahi and self.avahi.poll() is None:
            self.avahi.terminate()
        self.avahi = None

    def run(self, ssid: str, password: str) -> int:
        dev = self.nm.wifi_device()
        if not dev:
            self.status.set("error", reason=N_("this box has no Wi-Fi device"))
            return 1
        self.previous = self.nm.active(dev)
        try:
            return self._run(dev, ssid, password)
        except Stopped:
            log("stopped — putting the previous connection back")
            self.status.set("stopped")
            self._put_back(quick=True)
            return 3
        finally:
            if self.screen:
                self.screen.close()  # a "connected" screen stays its few seconds
            self.unpublish()
            if self.server:
                self.server.shutdown()
            self.nm.ap_delete()

    def _show(self, lines: list[str], secs: float | None = None, qr: str | None = None) -> bool:
        return bool(self.screen and self.screen.show(SCREEN_TITLE, lines, secs, qr=qr))

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
        url = f"http://{ssid}.local"
        # The display's font draws ASCII only: a name outside it is folded, and the
        # address shown is then the one that needs no name.
        shown_ssid = display.fold_ascii(ssid)
        while True:
            ok, err = self.nm.ap_up(dev, ssid, password)
            if not ok:
                log(f"setup network failed: {err}")
                self.status.set("error", reason=N_("the setup network would not start"))
                self._put_back()
                return 1
            if not self.server:
                self.address = self.nm.address(dev) or AP_ADDRESS
                if self.address != AP_ADDRESS:
                    log(f"the setup network came up at {self.address}, not {AP_ADDRESS}: "
                        "the port-80 redirect and the captive DNS answer will miss it")
                try:
                    self.serve()
                except OSError as e:
                    log(f"the setup page could not start: {e}")
                    self.status.set("error", reason=N_("the setup page could not start"))
                    self._put_back()
                    return 1
            self.publish(ssid)
            # On the display first: the status says whether it got there, and with a code
            # to scan, and the voice points at it only then. The code carries the real
            # name, whatever the font can draw.
            shown = self._show([f"Network  {shown_ssid}", f"Password  {password}",
                                url if shown_ssid == ssid else f"http://{self.address}"],
                               qr=wifi_qr(ssid, password))
            self.status.set("ap_up", ssid=ssid, password=password, url=url,
                            address=self.address, error=self.error, screen=shown,
                            qr=shown and self.screen.draws("qr"))
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
                self.status.set("connected", target=target)
                self._show(["Connected to", display.fold_ascii(target)], secs=CONNECTED_SCREEN_SECS)
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
    nm = NM()
    dev = nm.wifi_device()
    ssid = setup_ssid(nm.mac(dev) if dev else "", SSID)
    password = setup_password(PASSWORD)
    run_id = args.run_id or f"{int(time.time())}-{random.randrange(1 << 16):04x}"
    os.makedirs(args.status_dir, exist_ok=True)
    status = Status(args.status_dir, run_id)
    log(f"run {run_id}: setup network {ssid}, page on port {args.port}, "
        f"{'flash-time' if PASSWORD.strip() == password else 'random'} password")
    setup = Setup(nm, status, port=args.port, minutes=args.minutes,
                  screen=display.Screen("wifi-setup"))
    signal.signal(signal.SIGTERM, _on_sigterm)
    return setup.run(ssid, password)


if __name__ == "__main__":
    sys.exit(main())
