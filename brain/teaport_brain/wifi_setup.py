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
#   3. serve the setup page at 10.42.0.1, where NetworkManager's shared mode puts us:
#      on PAGE_PORT, which the unit's iptables rule makes port 80 for that address (port
#      80 itself is the front door's — Caddy's http->https redirect). NM's dnsmasq answers every name with that address
#      (/etc/NetworkManager/dnsmasq-shared.d/teaport-captive.conf, from install.sh), so a
#      phone's captive-portal probe lands here and the OS opens the page by itself;
#      http://teaport-ab12.local is the fallback (avahi-publish, while the network is up)
#   4. the user picks a network and types its password on the phone; the access point
#      goes down and the box joins it. Internet confirmed: done. Otherwise the network's
#      new profile is dropped, the access point comes back and the page says why.
#   5. ten minutes without success: the access point goes down and the connection the
#      box had before is brought back up.
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
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from teaport_brain import i18n
from teaport_brain.i18n import N_
from teaport_brain.wifi import AP_ADDRESS, NM, PAGE_CSS, logo_html

# Where the page listens. teaport-wifi-setup.service redirects AP_ADDRESS:80 here with
# an iptables rule for as long as it runs; phones only ever see port 80.
PAGE_PORT = 7869
SSID_PREFIX = "teaport-"
MINUTES = 10
PASSWORD_DIGITS = 8
STATUS_FILE = "status.json"
# The unit's RuntimeDirectory=teaport-wifi-setup (kept after the run with
# RuntimeDirectoryPreserve=yes, so the brain reads how it ended).
STATUS_DIR = "/run/teaport-wifi-setup"
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


def setup_password(fixed: str = "") -> str:
    """The flash-time password if valid (WPA2: 8 to 63 printable ASCII), else digits."""
    fixed = fixed.strip()
    if fixed:
        if 8 <= len(fixed) <= 63 and all(32 <= ord(c) < 127 for c in fixed):
            return fixed
        log("WIFI_SETUP_PASSWORD is not 8-63 printable characters — using random digits")
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

PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head><body><main>
<div class="logo">{logo}</div>
<h1>{heading}</h1>
<p>{intro}</p>
{error}
<form method="post" action="/connect">
{networks}
<label class="net"><input type="radio" name="ssid" value="" {other_checked}>
<span>{other}</span></label>
<input type="text" name="other" placeholder="{name}" autocomplete="off" autocapitalize="none">
<label class="f" for="pw">{password}</label>
<input type="password" id="pw" name="password" autocomplete="off" autocapitalize="none" spellcheck="false">
<label class="show"><input type="checkbox" onclick="pw.type=this.checked?'text':'password'"> {show}</label>
<button type="submit">{connect}</button>
</form></main></body></html>"""

JOINING = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>{css}</style></head><body><main>
<div class="logo">{logo}</div>
<h1>{heading}</h1>
<p>{body}</p></main></body></html>"""


def _lang_attr(t: i18n.T) -> str:
    return t.lang.replace("_", "-")


def render_page(networks: list[dict], error: str = "", t: i18n.T | None = None) -> str:
    """The page, in t's language. `error` is already in that language."""
    t = t or i18n.get(i18n.SOURCE_LANG)
    rows = []
    for i, n in enumerate(networks):
        lock = t._("open") if n["open"] else "🔒"
        rows.append(
            f'<label class="net"><input type="radio" name="ssid" value="{html.escape(n["ssid"], quote=True)}"'
            f'{" checked" if i == 0 else ""}><span>{html.escape(n["ssid"])}</span>'
            f"<small>{lock} {n['signal']}%</small></label>")
    return PAGE.format(
        lang=_lang_attr(t), css=PAGE_CSS, logo=logo_html(),
        title=html.escape(t._("Teaport Wi-Fi setup")),
        heading=html.escape(t._("Connect to Wi-Fi")),
        intro=html.escape(t._("Pick your network and type its password. Teaport will leave "
                              "this setup network and join yours, and say out loud whether "
                              "that worked.")),
        error=f'<p class="err">{html.escape(error)}</p>' if error else "",
        networks="\n".join(rows) or f"<p>{html.escape(t._('No networks found — use Other.'))}</p>",
        other_checked="" if networks else "checked",
        other=html.escape(t._("Other (hidden) network")),
        name=html.escape(t._("Network name"), quote=True),
        password=html.escape(t._("Password")),
        show=html.escape(t._("Show password")),
        connect=html.escape(t._("Connect")))


def render_joining(ssid: str, t: i18n.T) -> str:
    strong = f"<strong>{html.escape(ssid)}</strong>"
    return JOINING.format(
        lang=_lang_attr(t), css=PAGE_CSS, logo=logo_html(),
        title=html.escape(t._("Teaport is joining")),
        heading=html.escape(t._("Joining {network}…")).format(network=html.escape(ssid)),
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


class Setup:
    """One run: the access point, the page, and the join attempts."""

    def __init__(self, nm: NM, status: Status, port: int = PAGE_PORT, minutes: float = MINUTES):
        self.nm, self.status, self.port = nm, status, port
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

            def _t(self) -> i18n.T:
                return i18n.for_accept_language(self.headers.get("Accept-Language"))

            def do_GET(self):
                # Every path is the page: the captive-portal probes (Apple's
                # hotspot-detect, Android's generate_204, Windows' connecttest) expect
                # something else, so the phone shows this as a sign-in page.
                if self.path.startswith("/generate_204") or self.path.startswith("/gen_204"):
                    return self._send(302, "", extra=[("Location", f"http://{AP_ADDRESS}/")])
                t = self._t()
                self._send(200, render_page(setup.networks, setup.error_text(t), t))

            def do_POST(self):
                length = min(int(self.headers.get("Content-Length") or 0), 4096)
                ssid, password, hidden, error = parse_form(self.rfile.read(length))
                t = self._t()
                if error:
                    return self._send(200, render_page(setup.networks, t._(error), t))
                self._send(200, render_joining(ssid, t))
                setup.request = (ssid, password, hidden)
                setup.wake.set()

        return Handler

    def serve(self) -> None:
        self.server = ThreadingHTTPServer(("0.0.0.0", self.port), self.handler())
        self.port = self.server.server_address[1]  # the real one when asked for port 0
        self.server.daemon_threads = True
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
            self.avahi = subprocess.Popen([exe, "-a", "-R", f"{name}.local", AP_ADDRESS],
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
        previous = self.nm.active_wifi(dev)
        self.status.set("scanning")
        self.networks = [n for n in self.nm.scan(dev) if n["ssid"] != ssid]
        log(f"{len(self.networks)} networks in range; previous connection: {previous or 'none'}")
        try:
            self.serve()
        except OSError as e:
            log(f"the setup page could not start: {e}")
            self.status.set("error", reason=N_("the setup page could not start"))
            return 1
        url = f"http://{ssid}.local"
        try:
            while True:
                ok, err = self.nm.ap_up(dev, ssid, password)
                if not ok:
                    log(f"setup network failed: {err}")
                    self.status.set("error", reason=N_("the setup network would not start"))
                    return 1
                self.publish(ssid)
                self.status.set("ap_up", ssid=ssid, password=password, url=url,
                                address=AP_ADDRESS, error=self.error)
                self.request = None
                self.wake.clear()
                while not self.wake.wait(timeout=1.0):
                    if time.monotonic() >= self.deadline:
                        self.status.set("timeout")
                        self.unpublish()
                        self.nm.ap_down()
                        if previous:
                            self.nm.up(previous)
                        return 2
                target, pw, hidden = self.request
                time.sleep(1.5)  # let the "joining" page reach the phone first
                self.status.set("joining", target=target)
                self.unpublish()
                self.nm.ap_down()
                ok, why, created = self.nm.join(dev, target, pw, hidden)
                if ok and self.nm.online():
                    self.status.set("connected", target=target)
                    return 0
                if ok:
                    why = N_("it joined but there is no internet through it")
                if created:
                    self.nm.delete(created)
                self.failure = (target, why)
                self.error = self.error_text(i18n.get(i18n.SOURCE_LANG))
                self.status.set("failed", target=target, reason=why)
                if time.monotonic() >= self.deadline:
                    self.status.set("timeout")
                    if previous:
                        self.nm.up(previous)
                    return 2
        finally:
            self.unpublish()
            if self.server:
                self.server.shutdown()
            self.nm.ap_delete()


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
    return Setup(nm, status, port=args.port, minutes=args.minutes).run(ssid, password)


if __name__ == "__main__":
    sys.exit(main())
