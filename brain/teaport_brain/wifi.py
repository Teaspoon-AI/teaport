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
# The setup network's address, pinned in its profile (ipv4.addresses) rather than left to
# NetworkManager's pick: the unit's iptables DNAT (systemd/teaport-wifi-setup.service.in)
# and the captive DNS answer (packaging/wifi-setup/teaport-captive.conf) name it before the
# network exists. It is NM's own shared-mode default; change all three together.
AP_ADDRESS = "10.42.0.1"
AP_PREFIX = 24
CONNECT_WAIT_SECS = 45
WIFI_TYPE = "802-11-wireless"
# Key managements whose secret is a passphrase (802-11-wireless-security.psk): a saved
# profile of these takes a new password, and gets its old one back if that fails.
PSK_KINDS = ("wpa-psk", "sae")


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


def _terse_fields(out: str) -> dict[str, str]:
    """`nmcli -t -f a,b ... show` (one "field:value" per line) -> {field: value}, unescaped.
    A setting the profile does not have is simply missing (nmcli leaves it out)."""
    fields = {}
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= 2:
            fields[f[0]] = ":".join(f[1:])
    return fields


def _reachable(host: str, dev: str) -> bool:
    """A TCP connect to `host`:443 that can only leave through `dev`. Unprivileged
    SO_BINDTODEVICE needs Linux 5.7+ (JetPack 7 runs 6.8); without it nothing here
    could prove which network the answer came through, so the answer is no."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(4)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, dev.encode())
        except OSError:
            return False
        s.connect((host, 443))
        return True
    except OSError:
        return False
    finally:
        s.close()


class NM:
    """The nmcli calls setup needs. `run(argv) -> (rc, stdout, stderr)` is injectable.

    Profiles are named by UUID throughout: names repeat (NM names a new profile after its
    network, and `connection delete id X` deletes every profile called X)."""

    def __init__(self, run=None):
        self._run = run or self._subprocess
        # What the join in progress has changed, for undo(): ("psk", uuid, flags, psk)
        # or ("new", uuids before it, ssid). None when nothing is half-done.
        self._pending: tuple | None = None

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

    def visible(self, dev: str, ssid: str) -> bool:
        """`ssid` is in the radio's last scan (no new scan)."""
        _, out, _ = self.nmcli("-t", "-f", "SSID", "device", "wifi", "list", "ifname", dev,
                               "--rescan", "no")
        return any(split_terse(line)[0] == ssid for line in out.splitlines())

    def active(self, dev: str) -> dict | None:
        """The profile `dev` is on now, {"name", "uuid"} (to put back), if any."""
        _, out, _ = self.nmcli("-t", "-f", "GENERAL.CONNECTION,GENERAL.CON-UUID",
                               "device", "show", dev)
        f = _terse_fields(out)
        name, uuid = f.get("GENERAL.CONNECTION", ""), f.get("GENERAL.CON-UUID", "")
        if uuid in ("", "--"):
            return None
        return {"name": name, "uuid": uuid}

    def active_wifi(self, dev: str) -> str | None:
        """The name of the profile `dev` is on now, for showing."""
        now = self.active(dev)
        return now["name"] if now else None

    def addresses(self, dev: str) -> list[str]:
        """`dev`'s IPv4 addresses now. Just after the setup network comes up this can
        still be the old network's: the setup network's arrives a moment later."""
        _, out, _ = self.nmcli("-g", "IP4.ADDRESS", "device", "show", dev)
        return [a for line in out.splitlines() for a in
                (part.strip().split("/")[0] for part in line.split("|")) if a]

    def fields(self, uuid: str, *names: str, secrets: bool = False) -> dict[str, str] | None:
        """Profile `uuid`'s settings `names`, unescaped; None if nmcli fails."""
        rc, out, _ = self.nmcli(*(["-s"] if secrets else []), "-t", "-f", ",".join(names),
                                "connection", "show", "uuid", uuid)
        return _terse_fields(out) if rc == 0 else None

    def wifi_profiles(self) -> list[str]:
        """Every saved Wi-Fi profile's UUID."""
        _, out, _ = self.nmcli("-t", "-f", "UUID,TYPE", "connection", "show")
        return [f[0] for f in map(split_terse, out.splitlines())
                if len(f) >= 2 and f[1] == WIFI_TYPE]

    def saved_profile(self, ssid: str) -> dict | None:
        """A saved profile for joining `ssid` (not an access point of ours or anyone's):
        {"uuid", "key_mgmt"}, key_mgmt "" for an open network."""
        for uuid in self.wifi_profiles():
            f = self.fields(uuid, "connection.id", "802-11-wireless.ssid", "802-11-wireless.mode",
                            "802-11-wireless-security.key-mgmt") or {}
            if f.get("connection.id") == AP_CONNECTION or f.get("802-11-wireless.mode") == "ap":
                continue
            if f.get("802-11-wireless.ssid") == ssid:
                return {"uuid": uuid, "key_mgmt": f.get("802-11-wireless-security.key-mgmt", "")}
        return None

    def ap_up(self, dev: str, ssid: str, password: str) -> tuple[bool, str]:
        self.nmcli("connection", "delete", "id", AP_CONNECTION)  # a leftover from a crash
        rc, _, err = self.nmcli(
            "connection", "add", "type", "wifi", "ifname", dev, "con-name", AP_CONNECTION,
            "autoconnect", "no", "ssid", ssid,
            "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
            "ipv4.method", "shared", "ipv4.addresses", f"{AP_ADDRESS}/{AP_PREFIX}",
            "ipv6.method", "disabled",
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

    def up(self, uuid: str, wait: int = CONNECT_WAIT_SECS) -> bool:
        """Activate profile `uuid`; wait=0 asks for it and returns at once."""
        rc, _, _ = self.nmcli("--wait", str(wait), "connection", "up", "uuid", uuid,
                              timeout=wait + 15)
        return rc == 0

    def delete(self, uuid: str) -> None:
        self.nmcli("connection", "delete", "uuid", uuid)

    def join(self, dev: str, ssid: str, password: str, hidden: bool) -> tuple[bool, str, str | None]:
        """Join `ssid`. Returns (ok, why-not, UUID of a profile created for it — the
        caller drops it if it does not keep it).

        A saved profile and no password: just bring it up. A saved WPA2/WPA3-Personal
        profile and a password: change its password, and put the old one back if it does
        not work. Any other saved profile (open, enterprise, WEP, OWE) is never changed —
        a password for it makes a separate new profile instead."""
        self._pending = None
        if not hidden and not self.visible(dev, ssid):
            # Just out of access-point mode the radio's scan list is empty (seen on a
            # Jetson: 9 networks before, 0 right after), and nmcli then says "not found".
            self.scan(dev)
        saved = self.saved_profile(ssid)
        if saved and not password:
            ok = self.up(saved["uuid"])
            return ok, "" if ok else N_("the saved settings for it did not work"), None
        if saved and saved["key_mgmt"] in PSK_KINDS:
            return self._new_password(saved["uuid"], password)
        return self._create(dev, ssid, password, hidden)

    def _new_password(self, uuid: str, password: str) -> tuple[bool, str, None]:
        sec = "802-11-wireless-security"
        old = self.fields(uuid, f"{sec}.psk", f"{sec}.psk-flags", secrets=True)
        if old is None:  # no copy of the old password: leave the profile alone
            return False, N_("it would not connect"), None
        flags = (old.get(f"{sec}.psk-flags") or "0").split()[0]
        self._pending = ("psk", uuid, flags, old.get(f"{sec}.psk", ""))
        rc, _, _ = self.nmcli("connection", "modify", "uuid", uuid,
                              "wifi-sec.psk-flags", "0", "wifi-sec.psk", password)
        if rc == 0 and self.up(uuid):
            self._pending = None
            return True, "", None
        self.undo()
        return False, (N_("the password did not work") if rc == 0 else N_("it would not connect")), None

    def _create(self, dev: str, ssid: str, password: str, hidden: bool) -> tuple[bool, str, str | None]:
        if ssid.startswith("-"):
            # nmcli reads a leading dash here as one of its own options (--ask,
            # --show-secrets) and has no "--" to stop that.
            return False, N_("it would not connect"), None
        before = set(self.wifi_profiles())
        self._pending = ("new", before, ssid)
        args = ["--wait", str(CONNECT_WAIT_SECS), "device", "wifi", "connect", ssid,
                "ifname", dev, "name", ssid]
        if password:
            args += ["password", password]
        if hidden:
            args += ["hidden", "yes"]
        rc, _, err = self.nmcli(*args, timeout=CONNECT_WAIT_SECS + 15)
        if rc != 0 and not hidden and re.search(r"No network with SSID", err):
            for uuid in self._new_profiles(before, ssid):  # a stale list: once more, freshly
                self.delete(uuid)
            self.scan(dev)
            rc, _, err = self.nmcli(*args, timeout=CONNECT_WAIT_SECS + 15)
        created = next(iter(self._new_profiles(before, ssid)), None)
        self._pending = None
        if rc == 0:
            return True, "", created
        why = N_("the password did not work") if re.search(
            r"secrets|password|802-1x|psk", err, re.I) else (
            N_("the network was not found") if re.search(r"not found|No network", err, re.I)
            else N_("it would not connect"))
        return False, why, created

    def _new_profiles(self, before: set[str], ssid: str) -> list[str]:
        """The Wi-Fi profiles for `ssid` that were not there `before`."""
        return [uuid for uuid in self.wifi_profiles() if uuid not in before
                and (self.fields(uuid, "802-11-wireless.ssid") or {}).get("802-11-wireless.ssid") == ssid]

    def undo(self) -> None:
        """Put back what the join in progress changed — after a password that did not
        work, or when the unit is stopped in the middle of a join: the profile's old
        password, or no profile created for it."""
        pending, self._pending = self._pending, None
        if not pending:
            return
        if pending[0] == "psk":
            _, uuid, flags, psk = pending
            self.nmcli("connection", "modify", "uuid", uuid,
                       "wifi-sec.psk-flags", flags, "wifi-sec.psk", psk)
        else:
            _, before, ssid = pending
            for uuid in self._new_profiles(before, ssid):
                self.delete(uuid)

    def online(self, dev: str) -> bool:
        """Internet through `dev` — not merely somewhere: a box on Ethernet is always
        online, whatever the Wi-Fi joined. NM's per-device check first (after asking for
        a fresh one), else a TCP connect that can only leave through `dev`."""
        self.nmcli("networking", "connectivity", "check", timeout=20)
        _, out, _ = self.nmcli("-t", "-f", "GENERAL.IP4-CONNECTIVITY", "device", "show", dev)
        if "full" in _terse_fields(out).get("GENERAL.IP4-CONNECTIVITY", ""):
            return True
        return any(_reachable(host, dev) for host in ("1.1.1.1", "8.8.8.8"))


def switch(nm: "NM", dev: str, ssid: str, password: str, hidden: bool) -> tuple[bool, str]:
    """Move an ONLINE box to another network, or leave it where it was: join, confirm
    internet through the Wi-Fi, and on any failure drop the new profile and bring the old
    connection back — unless it is already back (the same profile, its password restored
    and NM quicker than us)."""
    previous = nm.active(dev)
    ok, why, created = nm.join(dev, ssid, password, hidden)
    if ok and nm.online(dev):
        return True, ""
    if ok:
        why = N_("it joined but there is no internet through it")
    if created:
        nm.delete(created)
    now = nm.active(dev)
    if previous and (now or {}).get("uuid") != previous["uuid"]:
        nm.up(previous["uuid"])
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
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin:0 0 18px}
.logo{margin:0 0 18px}.top .logo{margin:0}.logo img{display:block;height:50px;width:auto}
.lang select{height:36px;max-width:11rem;padding:0 8px;border:1px solid var(--border-strong);
border-radius:var(--radius);background:var(--surface);color:inherit;font:inherit;font-size:14px}
.lang button{margin:0 0 0 6px;width:auto;height:36px;padding:0 10px}
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
/* The phone's own fonts, per script: the page sets lang, and a Han character must be
   drawn in the Japanese, Chinese or Korean form its reader expects (one code point,
   three glyph styles). Each list names the platform fonts (iOS/macOS, Android = Noto,
   Windows) before the generic fallback; no web font is shipped (the page is offline). */
:lang(ja){font-family:"Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans CJK JP","Noto Sans JP","Yu Gothic UI","Meiryo",var(--sans)}
:lang(zh){font-family:"PingFang SC","Noto Sans CJK SC","Noto Sans SC","Microsoft YaHei",var(--sans)}
:lang(ko){font-family:"Apple SD Gothic Neo","Noto Sans CJK KR","Noto Sans KR","Malgun Gothic",var(--sans);word-break:keep-all}
:lang(ar){font-family:"Geeza Pro","Noto Naskh Arabic","Noto Sans Arabic","Segoe UI",var(--sans)}
:lang(hi){font-family:"Kohinoor Devanagari","Noto Sans Devanagari","Nirmala UI",var(--sans)}
"""
