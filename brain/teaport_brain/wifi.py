#
# teaport — Wi-Fi: what the config page's Wi-Fi section (config_ui.py, online: scan and
# switch networks behind the page's token) and the setup unit (wifi_setup.py, offline:
# a temporary setup network and a captive page) share — the NetworkManager calls, and
# the look of a page that has to render with no web fonts and no internet.
#
# Everything here shells out to nmcli as the run user; the polkit rule install.sh lays
# down (50-teaport-wifi-setup.rules) is what lets that user change the Wi-Fi. No
# Pipecat import: the setup unit runs this alone on a box short of memory.
#
import base64
import functools
import os
import re
import socket
import subprocess

from teaport_brain.i18n import N_

AP_CONNECTION = "teaport-setup"
AP_ADDRESS = "10.42.0.1"  # NetworkManager's ipv4.method=shared default
CONNECT_WAIT_SECS = 45


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
            return ok, "" if ok else N_("the saved settings for it did not work"), None
        if saved:
            _, old, _ = self.nmcli("-s", "-t", "-g", "802-11-wireless-security.psk",
                                   "connection", "show", "id", saved)
            self.nmcli("connection", "modify", "id", saved, "wifi-sec.key-mgmt", "wpa-psk",
                       "wifi-sec.psk", password)
            if self.up(saved):
                return True, "", None
            if old.strip():
                self.nmcli("connection", "modify", "id", saved, "wifi-sec.psk", old.strip())
            return False, N_("the password did not work"), None
        args = ["--wait", str(CONNECT_WAIT_SECS), "device", "wifi", "connect", ssid,
                "ifname", dev, "name", ssid]
        if password:
            args += ["password", password]
        if hidden:
            args += ["hidden", "yes"]
        rc, _, err = self.nmcli(*args, timeout=CONNECT_WAIT_SECS + 15)
        if rc == 0:
            return True, "", ssid
        why = N_("the password did not work") if re.search(
            r"secrets|password|802-1x|psk", err, re.I) else (
            N_("the network was not found") if re.search(r"not found|No network", err, re.I)
            else N_("it would not connect"))
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



def switch(nm: "NM", dev: str, ssid: str, password: str, hidden: bool) -> tuple[bool, str]:
    """Move an ONLINE box to another network, or leave it where it was: join, confirm
    internet, and on any failure drop the new profile and bring the old connection back."""
    previous = nm.active_wifi(dev)
    ok, why, created = nm.join(dev, ssid, password, hidden)
    if ok and nm.online():
        return True, ""
    if ok:
        why = N_("it joined but there is no internet through it")
    if created:
        nm.delete(created)
    if previous and previous != ssid:
        nm.up(previous)
    return False, why


@functools.cache
def logo_html() -> str:
    """The Teaport logo as the marketing site shows it: the on-light-background version,
    and the on-dark-background one when the phone is in dark mode (a <picture>, as on
    teaport.astro). Both SVGs ride inside the page as data URIs: the setup page is
    served offline by its own little server, so it carries its images itself (and two
    inline SVGs would collide on their shared gradient ids)."""
    static = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    uris = {}
    for key, name in (("light", "teaport-logo.svg"), ("dark", "teaport-logo-dark.svg")):
        try:
            with open(os.path.join(static, name), "rb") as f:
                uris[key] = "data:image/svg+xml;base64," + base64.b64encode(f.read()).decode()
        except OSError:
            pass
    if "light" not in uris:
        return ""
    dark = (f'<source srcset="{uris["dark"]}" media="(prefers-color-scheme: dark)">'
            if "dark" in uris else "")
    return f'<picture>{dark}<img src="{uris["light"]}" alt="Teaport" width="89" height="50"></picture>'


# The setup page's look: the config page's tokens (static/config.html), with system
# fonts — the box is offline when this page is shown, so no web font can load.
PAGE_CSS = """
:root{--bg:#F4F2EC;--surface:#fbfaf6;--text:#020617;--muted:#475569;--faint:#64748B;
--border:#E2E8F0;--border-strong:#CBD5E1;--accent:#0891B2;--err-ink:#B91C1C;--err-bg:#FEE2E2;
--btn-bg:#023176;--btn-fg:#fff;--radius:8px;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#0b1120;--surface:#111a2e;--text:#e2e8f0;
--muted:#94a3b8;--faint:#7c8aa0;--border:#1e293b;--border-strong:#334155;--accent:#22d3ee;
--err-ink:#fca5a5;--err-bg:#3b1414;--btn-bg:#38bdf8;--btn-fg:#04121f}}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 var(--sans)}
main{max-width:32rem;margin:0 auto;padding:28px 16px 48px}
.logo{margin:0 0 18px}.logo img{display:block;height:50px;width:auto}
h1{font-size:28px;line-height:1.15;margin:6px 0 8px}
p{color:var(--muted);margin:0 0 16px}
.net{display:flex;align-items:center;gap:12px;padding:12px 14px;background:var(--surface);
border:1px solid var(--border-strong);border-radius:var(--radius);margin:8px 0;cursor:pointer}
.net span{flex:1;overflow-wrap:anywhere}.net small{color:var(--faint)}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;height:48px;padding:0 14px;
border:1px solid var(--border-strong);border-radius:var(--radius);background:var(--surface);color:inherit;font:inherit}
label.f{display:block;margin:18px 0 6px;font-weight:600}
button{margin-top:20px;width:100%;height:48px;border:0;border-radius:var(--radius);
background:var(--btn-bg);color:var(--btn-fg);font:inherit;font-weight:700;cursor:pointer}
.err{color:var(--err-ink);background:var(--err-bg);padding:10px 14px;border-radius:var(--radius);font-weight:600}
.show{display:flex;gap:8px;align-items:center;color:var(--muted);margin-top:8px}
"""
