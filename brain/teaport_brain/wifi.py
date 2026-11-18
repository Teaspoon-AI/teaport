#
# teaport — Wi-Fi: what the config page's Wi-Fi section (config_ui.py, online: scan and
# switch networks behind the page's token) and the setup unit (wifi_setup.py, offline:
# a temporary setup network and a captive page) share — the NetworkManager calls, and
# the look of a page that has to render with no web fonts and no internet.
#
# Reads (devices, scans, profiles) are nmcli as the run user; a scan is the one thing the
# polkit rule install.sh lays down (50-teaport-wifi-setup.rules) still grants it. Every
# change goes through wifi_apply.py as root, behind its sudoers line, which takes only
# Wi-Fi requests and checks them (see there): the run user is the agent's, and may not
# change the box's other networking. No Pipecat import: the setup unit runs this alone
# on a box short of memory.
#
import base64
import functools
import json
import os
import socket
import subprocess

from teaport_brain import wifi_apply
from teaport_brain.i18n import N_
from teaport_brain.wifi_apply import AP_ADDRESS, AP_CONNECTION, AP_PREFIX, CONNECT_WAIT_SECS  # noqa: F401
from teaport_brain.wifi_apply import split_terse, terse_fields as _terse_fields  # noqa: F401

# The root-owned copy the sudoers line names (packaging/deb/sudoers-sync), on the system
# python: never the venv's interpreter, nor this tree's copy of the file.
HELPER = "/usr/lib/teaport/wifi_apply.py"
SYSTEM_PYTHON = "/usr/bin/python3"
# A join can take two connection attempts and two scans; the helper's own nmcli calls
# each have their timeout.
HELPER_SECS = 300
# A stop of the setup unit reaches the helper too, which then undoes a half-done join:
# how long the caller waits for that before giving up on it.
HELPER_STOP_SECS = 30
# The helper's reasons, as people read them (translated where shown).
WHY = {"": "", wifi_apply.SAVED_FAILED: N_("the saved settings for it did not work"),
       wifi_apply.NO_CONNECT: N_("it would not connect"),
       wifi_apply.BAD_PASSWORD: N_("the password did not work"),
       wifi_apply.NOT_FOUND: N_("the network was not found")}


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


def sudo_helper(verb: str, req: dict) -> dict:
    """wifi_apply <verb> under sudo, the request on stdin: its reply, or {"ok": False,
    "err": ...} when it could not run or refused."""
    try:
        p = subprocess.Popen(["sudo", "-n", SYSTEM_PYTHON, HELPER, verb], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as e:
        return {"ok": False, "err": str(e)}
    try:
        out, err = p.communicate(json.dumps(req), timeout=HELPER_SECS)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return {"ok": False, "err": "the Wi-Fi helper timed out"}
    except BaseException:
        # A stop (wifi_setup's Stopped): the helper had the unit's SIGTERM too and is
        # putting back what it began. Wait for that, so the caller's own clean-up (the old
        # network back up) does not race it.
        try:
            p.wait(timeout=HELPER_STOP_SECS)
        except BaseException:
            p.kill()
        raise
    try:
        reply = json.loads(out)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        return {"ok": False, "err": (err.strip() or f"the Wi-Fi helper exited {p.returncode}")[-300:]}
    if "refused" in reply:
        return {"ok": False, "err": str(reply["refused"])}
    return reply


class NM:
    """The NetworkManager calls setup and the config page need. `run(argv, timeout=) ->
    (rc, stdout, stderr)` is injectable: given one (tests), the changes run in-process,
    through wifi_apply.handle() and that same `run`, rather than through sudo.

    Profiles are named by UUID throughout: names repeat (NM names a new profile after its
    network, and `connection delete id X` deletes every profile called X)."""

    def __init__(self, run=None, helper=None):
        self._run = run or wifi_apply.run_nmcli
        self._ops = wifi_apply.Ops(self._run)   # reads; in-process, the changes too
        if helper is None:
            helper = self._in_process if run is not None else sudo_helper
        self._helper = helper
        self._created = wifi_apply.Created()

    def _in_process(self, verb: str, req: dict) -> dict:
        try:
            return wifi_apply.handle(self._ops, verb, req, created=self._created,
                                     setup_running=lambda: True)
        except wifi_apply.Refused as e:
            return {"ok": False, "err": str(e)}

    def nmcli(self, *args: str, timeout: float = 60) -> tuple[int, str, str]:
        return self._run(["nmcli", *args], timeout=timeout)

    def wifi_device(self) -> str | None:
        devices = self._ops.wifi_devices()
        return devices[0] if devices else None

    def mac(self, dev: str) -> str:
        try:
            with open(f"/sys/class/net/{dev}/address") as f:
                return f.read().strip()
        except OSError:
            return ""

    def scan(self, dev: str) -> list[dict]:
        return parse_scan(self._ops.rescan(dev))

    def visible(self, dev: str, ssid: str) -> bool:
        return self._ops.visible(dev, ssid)

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

    def fields(self, uuid: str, *names: str) -> dict[str, str] | None:
        return self._ops.fields(uuid, *names)

    def wifi_profiles(self) -> list[str]:
        return self._ops.wifi_profiles()

    def saved_profile(self, ssid: str) -> dict | None:
        return self._ops.saved_profile(ssid)

    # --- changes: through the helper ------------------------------------------------

    def ap_up(self, dev: str, ssid: str, password: str) -> tuple[bool, str]:
        r = self._helper("ap-up", {"dev": dev, "ssid": ssid, "password": password})
        return bool(r.get("ok")), str(r.get("err") or "")

    def ap_down(self) -> None:
        self._helper("ap-down", {})

    def ap_delete(self) -> None:
        self._helper("ap-delete", {})

    def up(self, uuid: str, wait: int = CONNECT_WAIT_SECS) -> bool:
        """Activate saved Wi-Fi profile `uuid`; wait=0 asks for it and returns at once."""
        return bool(self._helper("up", {"uuid": uuid, "wait": wait}).get("ok"))

    def delete(self, uuid: str) -> None:
        """Drop a profile a join created (the helper deletes no other)."""
        self._helper("delete", {"uuid": uuid})

    def join(self, dev: str, ssid: str, password: str, hidden: bool) -> tuple[bool, str, str | None]:
        """Join `ssid` (wifi_apply.Ops.join). Returns (ok, why-not, UUID of a profile
        created for it — the caller drops it if it does not keep it)."""
        r = self._helper("join", {"dev": dev, "ssid": ssid, "password": password, "hidden": hidden})
        why = WHY.get(r.get("why", wifi_apply.NO_CONNECT), WHY[wifi_apply.NO_CONNECT])
        if not r.get("ok") and not why:
            why = WHY[wifi_apply.NO_CONNECT]
        created = r.get("created")
        return bool(r.get("ok")), "" if r.get("ok") else why, created if isinstance(created, str) else None

    def undo(self) -> None:
        """Put back a join a stop cut short. Through sudo the helper does this itself as it
        is stopped (it has the same SIGTERM); in-process (tests), here."""
        self._ops.undo()

    def online(self, dev: str) -> bool:
        """Internet through `dev` — not merely somewhere: a box on Ethernet is always
        online, whatever the Wi-Fi joined. NM's per-device check first (after asking for
        a fresh one), else a TCP connect that can only leave through `dev`."""
        self._helper("connectivity", {})
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
:root{--bg:#F1F5F9;--surface:#FFFFFF;--text:#020617;--muted:#475569;--faint:#64748B;
--border:#E2E8F0;--border-strong:#CBD5E1;--accent:#0891B2;--err-ink:#B91C1C;--err-bg:#FEF2F2;
--btn-bg:#023176;--btn-fg:#F8FAFC;--radius:8px;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#020617;--surface:#0F172A;--text:#F8FAFC;
--muted:#CBD5E1;--faint:#94A3B8;--border:#1E293B;--border-strong:#334155;--accent:#22D3EE;
--err-ink:#FCA5A5;--err-bg:#450A0A;--btn-bg:#F8FAFC;--btn-fg:#023176}}
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
details.acct{margin:22px 0 0;padding:10px 14px;border:1px solid var(--border-strong);
border-radius:var(--radius);background:var(--surface)}details.acct summary{cursor:pointer;font-weight:600}
details.acct p{margin:8px 0 0}
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
