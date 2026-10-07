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
#   3. serve the setup page on port 80 at 10.42.0.1, where NetworkManager's shared mode
#      puts us. NM's dnsmasq answers every name with that address
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
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AP_CONNECTION = "teaport-setup"
AP_ADDRESS = "10.42.0.1"  # NetworkManager's ipv4.method=shared default
SSID_PREFIX = "teaport-"
MINUTES = 10
PASSWORD_DIGITS = 8
STATUS_FILE = "status.json"
# The unit's RuntimeDirectory=teaport-wifi-setup (kept after the run with
# RuntimeDirectoryPreserve=yes, so the brain reads how it ended).
STATUS_DIR = "/run/teaport-wifi-setup"
CONNECT_WAIT_SECS = 45
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


# ------------------------------------------------------------------ NetworkManager

def split_terse(line: str) -> list[str]:
    """One line of `nmcli -t` output: ':'-separated, with '\\:' and '\\\\' escaped."""
    fields, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    fields.append("".join(cur))
    return fields


def parse_scan(out: str) -> list[dict]:
    """`nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list` -> one entry per named network,
    its strongest access point, strongest first."""
    best: dict[str, dict] = {}
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) < 3 or not f[0].strip():
            continue
        ssid, sec = f[0], f[2].strip()
        try:
            signal = int(f[1])
        except ValueError:
            signal = 0
        open_ = sec in ("", "--")
        if ssid not in best or signal > best[ssid]["signal"]:
            best[ssid] = {"ssid": ssid, "signal": signal, "open": open_,
                          "security": "open" if open_ else sec}
    return sorted(best.values(), key=lambda n: -n["signal"])


class NM:
    """The nmcli calls setup needs. `run(argv) -> (rc, stdout, stderr)` is injectable."""

    def __init__(self, run=None):
        self._run = run or self._subprocess

    @staticmethod
    def _subprocess(argv: list[str], timeout: float = 60) -> tuple[int, str, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", f"{argv[0]} timed out"

    def nmcli(self, *args: str, timeout: float = 60) -> tuple[int, str, str]:
        return self._run(["nmcli", *args], timeout=timeout)

    def wifi_device(self) -> str | None:
        rc, out, _ = self.nmcli("-t", "-f", "DEVICE,TYPE", "device")
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) >= 2 and f[1] == "wifi":
                return f[0]
        return None

    def mac(self, dev: str) -> str:
        try:
            with open(f"/sys/class/net/{dev}/address") as f:
                return f.read().strip()
        except OSError:
            return ""

    def scan(self, dev: str) -> list[dict]:
        _, out, _ = self.nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                               "ifname", dev, "--rescan", "yes", timeout=45)
        return parse_scan(out)

    def active_wifi(self, dev: str) -> str | None:
        """The connection profile `dev` is on now (to put back), if any."""
        rc, out, _ = self.nmcli("-t", "-f", "GENERAL.CONNECTION", "device", "show", dev)
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) >= 2 and f[0] == "GENERAL.CONNECTION" and f[1] not in ("", "--"):
                return f[1]
        return None

    def saved_profile(self, ssid: str) -> str | None:
        """A saved Wi-Fi profile for `ssid` (joined without a password), if any."""
        rc, out, _ = self.nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) < 2 or f[1] != "802-11-wireless" or f[0] == AP_CONNECTION:
                continue
            _, ssid_out, _ = self.nmcli("-t", "-g", "802-11-wireless.ssid", "connection",
                                        "show", "id", f[0])
            if ssid_out.strip().replace("\\:", ":") == ssid:
                return f[0]
        return None

    def ap_up(self, dev: str, ssid: str, password: str) -> tuple[bool, str]:
        self.nmcli("connection", "delete", "id", AP_CONNECTION)  # a leftover from a crash
        rc, _, err = self.nmcli(
            "connection", "add", "type", "wifi", "ifname", dev, "con-name", AP_CONNECTION,
            "autoconnect", "no", "ssid", ssid,
            "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
            "ipv4.method", "shared", "ipv6.method", "disabled",
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn",
            "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp",
            "wifi-sec.psk", password)
        if rc != 0:
            return False, err.strip() or "could not create the setup network"
        rc, _, err = self.nmcli("--wait", "30", "connection", "up", "id", AP_CONNECTION,
                                timeout=45)
        return rc == 0, err.strip()

    def ap_down(self) -> None:
        self.nmcli("--wait", "15", "connection", "down", "id", AP_CONNECTION, timeout=25)

    def ap_delete(self) -> None:
        self.nmcli("connection", "delete", "id", AP_CONNECTION)

    def up(self, profile: str) -> bool:
        rc, _, _ = self.nmcli("--wait", str(CONNECT_WAIT_SECS), "connection", "up", "id",
                              profile, timeout=CONNECT_WAIT_SECS + 15)
        return rc == 0

    def join(self, dev: str, ssid: str, password: str, hidden: bool) -> tuple[bool, str, str | None]:
        """Join `ssid`. Returns (ok, why-not, profile created for it — dropped on failure).
        A saved profile and no password: just bring it up. A saved profile and a new
        password: update it (and put the old one back on failure)."""
        saved = self.saved_profile(ssid)
        if saved and not password:
            ok = self.up(saved)
            return ok, "" if ok else "the saved settings for it did not work", None
        if saved:
            _, old, _ = self.nmcli("-s", "-t", "-g", "802-11-wireless-security.psk",
                                   "connection", "show", "id", saved)
            self.nmcli("connection", "modify", "id", saved, "wifi-sec.key-mgmt", "wpa-psk",
                       "wifi-sec.psk", password)
            if self.up(saved):
                return True, "", None
            if old.strip():
                self.nmcli("connection", "modify", "id", saved, "wifi-sec.psk", old.strip())
            return False, "the password did not work", None
        args = ["--wait", str(CONNECT_WAIT_SECS), "device", "wifi", "connect", ssid,
                "ifname", dev, "name", ssid]
        if password:
            args += ["password", password]
        if hidden:
            args += ["hidden", "yes"]
        rc, _, err = self.nmcli(*args, timeout=CONNECT_WAIT_SECS + 15)
        if rc == 0:
            return True, "", ssid
        why = "the password did not work" if re.search(
            r"secrets|password|802-1x|psk", err, re.I) else (
            "the network was not found" if re.search(r"not found|No network", err, re.I)
            else "it would not connect")
        return False, why, ssid

    def delete(self, profile: str) -> None:
        self.nmcli("connection", "delete", "id", profile)

    def online(self) -> bool:
        """Internet, not just a link: NM's own check, else a TCP connect to a resolver."""
        rc, out, _ = self.nmcli("networking", "connectivity", "check", timeout=20)
        if out.strip() == "full":
            return True
        for host in ("1.1.1.1", "8.8.8.8"):
            try:
                socket.create_connection((host, 443), timeout=4).close()
                return True
            except OSError:
                continue
        return False


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

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Teaport Wi-Fi setup</title><style>
:root{{--bg:#faf8f5;--fg:#1d1b18;--mute:#6b665e;--line:#e2ddd5;--acc:#2f6b4f;--warn:#9a3b25}}
@media (prefers-color-scheme:dark){{:root{{--bg:#191816;--fg:#efebe4;--mute:#a39d93;--line:#34312c;--acc:#7cc3a0;--warn:#e08a6d}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif}}
main{{max-width:30rem;margin:0 auto;padding:1.5rem 1rem 3rem}}
h1{{font-size:1.4rem;margin:0 0 .25rem}} p{{color:var(--mute);margin:.25rem 0 1rem}}
.net{{display:flex;align-items:center;gap:.6rem;padding:.75rem;border:1px solid var(--line);
border-radius:.6rem;margin:.4rem 0;cursor:pointer}} .net span{{flex:1;overflow-wrap:anywhere}}
.net small{{color:var(--mute)}} input[type=text],input[type=password]{{width:100%;box-sizing:border-box;
padding:.7rem;border:1px solid var(--line);border-radius:.5rem;background:transparent;color:inherit;font:inherit}}
label.f{{display:block;margin:1rem 0 .3rem;font-weight:600}}
button{{margin-top:1.2rem;width:100%;padding:.8rem;border:0;border-radius:.6rem;background:var(--acc);
color:var(--bg);font:inherit;font-weight:600}} .err{{color:var(--warn);font-weight:600}}
.show{{display:flex;gap:.4rem;align-items:center;color:var(--mute);margin-top:.4rem}}
</style></head><body><main>
<h1>Connect Teaport to Wi-Fi</h1>
<p>Pick your network and type its password. Teaport will leave this setup network and
join yours; it says out loud whether that worked.</p>
{error}
<form method="post" action="/connect">
{networks}
<label class="net"><input type="radio" name="ssid" value="" {other_checked}>
<span>Other (hidden) network</span></label>
<input type="text" name="other" placeholder="Network name" autocomplete="off" autocapitalize="none">
<label class="f" for="pw">Password</label>
<input type="password" id="pw" name="password" autocomplete="off" autocapitalize="none" spellcheck="false">
<label class="show"><input type="checkbox" onclick="pw.type=this.checked?'text':'password'"> Show password</label>
<button type="submit">Connect</button>
</form></main></body></html>"""

JOINING = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Teaport is joining</title>
<style>body{{margin:0;font:16px/1.45 system-ui,sans-serif;background:#faf8f5;color:#1d1b18}}
@media (prefers-color-scheme:dark){{body{{background:#191816;color:#efebe4}}}}
main{{max-width:30rem;margin:0 auto;padding:1.5rem 1rem}}</style></head><body><main>
<h1>Joining {ssid}…</h1>
<p>Teaport is leaving this setup network now. Switch your phone back to
<strong>{ssid}</strong>. Teaport will say out loud whether it connected; if it could not,
the setup network comes back and you can try again.</p></main></body></html>"""


def render_page(networks: list[dict], error: str = "") -> str:
    rows = []
    for i, n in enumerate(networks):
        lock = "open" if n["open"] else "🔒"
        rows.append(
            f'<label class="net"><input type="radio" name="ssid" value="{html.escape(n["ssid"], quote=True)}"'
            f'{" checked" if i == 0 else ""}><span>{html.escape(n["ssid"])}</span>'
            f"<small>{lock} {n['signal']}%</small></label>")
    return PAGE.format(
        error=f'<p class="err">{html.escape(error)}</p>' if error else "",
        networks="\n".join(rows) or "<p>No networks found — use Other.</p>",
        other_checked="" if networks else "checked")


def parse_form(body: bytes) -> tuple[str, str, bool, str]:
    """(ssid, password, hidden, error) from the posted form."""
    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    ssid = (form.get("ssid") or [""])[0]
    other = (form.get("other") or [""])[0].strip()
    password = (form.get("password") or [""])[0]
    hidden = not ssid and bool(other)
    ssid = ssid or other
    if not ssid:
        return "", "", False, "Pick a network, or type its name under Other."
    if len(ssid.encode()) > 32:
        return "", "", False, "That network name is too long."
    if password and not 8 <= len(password) <= 63:
        return "", "", False, "Wi-Fi passwords are 8 to 63 characters."
    return ssid, password, hidden, ""


class Setup:
    """One run: the access point, the page, and the join attempts."""

    def __init__(self, nm: NM, status: Status, port: int = 80, minutes: float = MINUTES):
        self.nm, self.status, self.port = nm, status, port
        self.deadline = time.monotonic() + minutes * 60
        self.networks: list[dict] = []
        self.error = ""
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

            def do_GET(self):
                # Every path is the page: the captive-portal probes (Apple's
                # hotspot-detect, Android's generate_204, Windows' connecttest) expect
                # something else, so the phone shows this as a sign-in page.
                if self.path.startswith("/generate_204") or self.path.startswith("/gen_204"):
                    return self._send(302, "", extra=[("Location", f"http://{AP_ADDRESS}/")])
                self._send(200, render_page(setup.networks, setup.error))

            def do_POST(self):
                length = min(int(self.headers.get("Content-Length") or 0), 4096)
                ssid, password, hidden, error = parse_form(self.rfile.read(length))
                if error:
                    return self._send(200, render_page(setup.networks, error))
                self._send(200, JOINING.format(ssid=html.escape(ssid)))
                setup.request = (ssid, password, hidden)
                setup.wake.set()

        return Handler

    def serve(self) -> None:
        self.server = ThreadingHTTPServer(("0.0.0.0", self.port), self.handler())
        self.port = self.server.server_address[1]  # the real one when asked for port 0
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

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
            self.status.set("error", reason="this box has no Wi-Fi device")
            return 1
        previous = self.nm.active_wifi(dev)
        self.status.set("scanning")
        self.networks = [n for n in self.nm.scan(dev) if n["ssid"] != ssid]
        log(f"{len(self.networks)} networks in range; previous connection: {previous or 'none'}")
        try:
            self.serve()
        except OSError as e:
            self.status.set("error", reason=f"the setup page could not start ({e.strerror})")
            return 1
        url = f"http://{ssid}.local"
        try:
            while True:
                ok, err = self.nm.ap_up(dev, ssid, password)
                if not ok:
                    log(f"setup network failed: {err}")
                    self.status.set("error", reason="the setup network would not start")
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
                    why = "it joined but there is no internet through it"
                if created:
                    self.nm.delete(created)
                self.error = f"Could not join {target}: {why}. Try again."
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
    ap.add_argument("--port", type=int, default=80)
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
