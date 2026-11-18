#
# teaport — the one way the run user changes NetworkManager: the Wi-Fi changes setup
# (wifi_setup.py) and the config page (config_ui.py, through wifi.py) make, as root,
# behind sudo, and nothing else.
#
# The run user is the one the agent runs as. A polkit rule used to give it NetworkManager's
# network-control and settings.modify.system: every system profile (Ethernet, DNS, routes,
# proxies, VPN), the secrets saved in them, and every device up or down. Now the rule
# grants wifi.scan alone (packaging/wifi-setup/50-teaport-wifi-setup.rules.in), and every
# change comes through here, where a request names a Wi-Fi device, a network and a
# password, and is checked:
#   ap-up         the setup network (teaport-setup), only while teaport-wifi-setup runs;
#   ap-down       the setup network down; ap-delete: its profile gone;
#   join          a Wi-Fi network by name: a saved profile as it is, or a password. A saved
#                 WPA2/WPA3-Personal profile takes the new password and gets its old one
#                 back if that fails; anything else gets a new profile, reported so the
#                 caller can drop it;
#   up            a saved Wi-Fi client profile (not an access point), to put one back;
#   delete        a profile this helper created (kept in CREATED_PATH until boot), only;
#   connectivity  NetworkManager's connectivity check, which needs network-control.
# What that leaves the run user: moving the box's Wi-Fi to a network whose password it has,
# and the setup network while setup runs. That is what Wi-Fi setup is.
#
# The verb is the only argument; the rest is a JSON object on stdin, so a password never
# goes on a command line, where any user's ps can read it. The reply is a JSON object on
# stdout; a request this does not do gets {"refused": why} and exit 2. A stop (SIGTERM:
# systemd stopping setup sends it to every process of the unit, sudo passes it on) undoes
# a join half-done before this exits.
#
# Run as the sudoers line (packaging/deb/sudoers-sync) names it:
#   /usr/bin/python3 /usr/lib/teaport/wifi_apply.py <verb>
# the root-owned copy the teaport-brain package ships, on the SYSTEM python, never the
# venv's (like config_apply.py). Standard library only. In the brain's tree it is also
# what wifi.py reads NetworkManager with, as the run user, and tests run all of it
# in-process (handle()) against a pretend nmcli.
#
import json
import os
import re
import signal
import subprocess
import sys

AP_CONNECTION = "teaport-setup"
# The setup network's address, pinned in its profile (ipv4.addresses) rather than left to
# NetworkManager's pick: teaport-wifi-setup-net's iptables DNAT and the captive DNS answer
# (packaging/wifi-setup/) name it before the network exists. NM's own shared-mode default.
AP_ADDRESS = "10.42.0.1"
AP_PREFIX = 24
CONNECT_WAIT_SECS = 45
WIFI_TYPE = "802-11-wireless"
# Key managements whose secret is a passphrase (802-11-wireless-security.psk): a saved
# profile of these takes a new password, and gets its old one back if that fails.
PSK_KINDS = ("wpa-psk", "sae")
SETUP_UNIT = "teaport-wifi-setup.service"
CREATED_PATH = "/run/teaport-wifi-apply/created"
MAX_REQUEST = 4096
# Why a join failed, as codes; wifi.py turns them into the words people see.
SAVED_FAILED, NO_CONNECT, BAD_PASSWORD, NOT_FOUND = "saved_failed", "no_connect", "password", "not_found"
# nmcli's error text is matched (below), so it must be English; and a PATH of our own.
NMCLI_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


class Refused(Exception):
    """A request this helper does not do."""


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


def terse_fields(out: str) -> dict[str, str]:
    """`nmcli -t -f a,b ... show` (one "field:value" per line) -> {field: value}, unescaped.
    A setting the profile does not have is simply missing (nmcli leaves it out)."""
    fields = {}
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= 2:
            fields[f[0]] = ":".join(f[1:])
    return fields


def run_nmcli(argv: list[str], timeout: float = 60) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=NMCLI_ENV)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out"


class Ops:
    """NetworkManager through nmcli. `run(argv, timeout=) -> (rc, stdout, stderr)`.

    Profiles are named by UUID throughout: names repeat (NM names a new profile after its
    network, and `connection delete id X` deletes every profile called X)."""

    def __init__(self, run=run_nmcli):
        self._run = run
        # What the join in progress has changed, for undo(): ("psk", uuid, flags, psk)
        # or ("new", uuids before it, ssid). None when nothing is half-done.
        self._pending: tuple | None = None

    def nmcli(self, *args: str, timeout: float = 60) -> tuple[int, str, str]:
        return self._run(["nmcli", *args], timeout=timeout)

    # --- reads (no privilege needed, but wifi.scan for a rescan) ---------------------

    def wifi_devices(self) -> list[str]:
        _, out, _ = self.nmcli("-t", "-f", "DEVICE,TYPE", "device")
        return [f[0] for f in map(split_terse, out.splitlines()) if len(f) >= 2 and f[1] == "wifi"]

    def rescan(self, dev: str) -> str:
        """A fresh scan; `nmcli -t -f SSID,SIGNAL,SECURITY device wifi list` output."""
        _, out, _ = self.nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                               "ifname", dev, "--rescan", "yes", timeout=45)
        return out

    def visible(self, dev: str, ssid: str) -> bool:
        """`ssid` is in the radio's last scan (no new scan)."""
        _, out, _ = self.nmcli("-t", "-f", "SSID", "device", "wifi", "list", "ifname", dev,
                               "--rescan", "no")
        return any(split_terse(line)[0] == ssid for line in out.splitlines())

    def fields(self, uuid: str, *names: str, secrets: bool = False) -> dict[str, str] | None:
        """Profile `uuid`'s settings `names`, unescaped; None if nmcli fails."""
        rc, out, _ = self.nmcli(*(["-s"] if secrets else []), "-t", "-f", ",".join(names),
                                "connection", "show", "uuid", uuid)
        return terse_fields(out) if rc == 0 else None

    def wifi_profiles(self) -> list[str]:
        """Every saved Wi-Fi profile's UUID."""
        _, out, _ = self.nmcli("-t", "-f", "UUID,TYPE", "connection", "show")
        return [f[0] for f in map(split_terse, out.splitlines())
                if len(f) >= 2 and f[1] == WIFI_TYPE]

    def _is_client(self, f: dict) -> bool:
        return f.get("connection.id") != AP_CONNECTION and f.get("802-11-wireless.mode") != "ap"

    def saved_profile(self, ssid: str) -> dict | None:
        """A saved profile for joining `ssid` (not an access point of ours or anyone's):
        {"uuid", "key_mgmt"}, key_mgmt "" for an open network."""
        for uuid in self.wifi_profiles():
            f = self.fields(uuid, "connection.id", "802-11-wireless.ssid", "802-11-wireless.mode",
                            "802-11-wireless-security.key-mgmt") or {}
            if self._is_client(f) and f.get("802-11-wireless.ssid") == ssid:
                return {"uuid": uuid, "key_mgmt": f.get("802-11-wireless-security.key-mgmt", "")}
        return None

    def client_profile(self, uuid: str) -> bool:
        """`uuid` is a saved Wi-Fi profile for joining a network (not an access point)."""
        if uuid not in self.wifi_profiles():
            return False
        f = self.fields(uuid, "connection.id", "802-11-wireless.mode")
        return f is not None and self._is_client(f)

    def connectivity(self) -> str:
        _, out, _ = self.nmcli("networking", "connectivity", "check", timeout=20)
        return out.strip()

    # --- changes (root, through handle()) ----------------------------------------------

    def ap_up(self, dev: str, ssid: str, password: str) -> tuple[bool, str]:
        self.ap_delete()  # a leftover from a crash
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
        """Join `ssid`. Returns (ok, why-not code, UUID of a profile created for it — the
        caller drops it if it does not keep it).

        A saved profile and no password: just bring it up. A saved WPA2/WPA3-Personal
        profile and a password: change its password, and put the old one back if it does
        not work. Any other saved profile (open, enterprise, WEP, OWE) is never changed —
        a password for it makes a separate new profile instead."""
        self._pending = None
        if not hidden and not self.visible(dev, ssid):
            # Just out of access-point mode the radio's scan list is empty (seen on a
            # Jetson: 9 networks before, 0 right after), and nmcli then says "not found".
            self.rescan(dev)
        saved = self.saved_profile(ssid)
        if saved and not password:
            ok = self.up(saved["uuid"])
            return ok, "" if ok else SAVED_FAILED, None
        if saved and saved["key_mgmt"] in PSK_KINDS:
            return self._new_password(saved["uuid"], password)
        return self._create(dev, ssid, password, hidden)

    def _new_password(self, uuid: str, password: str) -> tuple[bool, str, None]:
        sec = "802-11-wireless-security"
        old = self.fields(uuid, f"{sec}.psk", f"{sec}.psk-flags", secrets=True)
        if old is None:  # no copy of the old password: leave the profile alone
            return False, NO_CONNECT, None
        flags = (old.get(f"{sec}.psk-flags") or "0").split()[0]
        self._pending = ("psk", uuid, flags, old.get(f"{sec}.psk", ""))
        rc, _, _ = self.nmcli("connection", "modify", "uuid", uuid,
                              "wifi-sec.psk-flags", "0", "wifi-sec.psk", password)
        if rc == 0 and self.up(uuid):
            self._pending = None
            return True, "", None
        self.undo()
        return False, (BAD_PASSWORD if rc == 0 else NO_CONNECT), None

    def _create(self, dev: str, ssid: str, password: str, hidden: bool) -> tuple[bool, str, str | None]:
        if ssid.startswith("-"):
            # nmcli reads a leading dash here as one of its own options (--ask,
            # --show-secrets) and has no "--" to stop that.
            return False, NO_CONNECT, None
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
            self.rescan(dev)
            rc, _, err = self.nmcli(*args, timeout=CONNECT_WAIT_SECS + 15)
        created = next(iter(self._new_profiles(before, ssid)), None)
        self._pending = None
        if rc == 0:
            return True, "", created
        why = BAD_PASSWORD if re.search(r"secrets|password|802-1x|psk", err, re.I) else (
            NOT_FOUND if re.search(r"not found|No network", err, re.I) else NO_CONNECT)
        return False, why, created

    def _new_profiles(self, before: set[str], ssid: str) -> list[str]:
        """The Wi-Fi profiles for `ssid` that were not there `before`."""
        return [uuid for uuid in self.wifi_profiles() if uuid not in before
                and (self.fields(uuid, "802-11-wireless.ssid") or {}).get("802-11-wireless.ssid") == ssid]

    def undo(self) -> None:
        """Put back what the join in progress changed — after a password that did not
        work, or when a stop came in the middle of a join: the profile's old password, or
        no profile created for it."""
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


# --- the requests ------------------------------------------------------------------


class Created:
    """The profiles this helper created and has not deleted: the only ones `delete` takes.
    In memory (tests), or a root-only file under /run (gone at boot, as is the need: a
    profile kept that long is the box's network)."""

    def __init__(self, path: str | None = None):
        self.path, self._mem = path, set()

    def _load(self) -> set[str]:
        if self.path is None:
            return self._mem
        try:
            with open(self.path) as f:
                return set(json.load(f))
        except (OSError, ValueError, TypeError):
            return set()

    def _save(self, uuids: set[str]) -> None:
        if self.path is None:
            self._mem = uuids
            return
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as f:
            json.dump(sorted(uuids), f)
        os.replace(tmp, self.path)

    def __contains__(self, uuid: str) -> bool:
        return uuid in self._load()

    def add(self, uuid: str) -> None:
        self._save(self._load() | {uuid})

    def discard(self, uuid: str) -> None:
        self._save(self._load() - {uuid})


def _text(req: dict, key: str) -> str:
    v = req.get(key)
    if not isinstance(v, str) or not v or "\x00" in v:
        raise Refused(f"{key} must be a non-empty string")
    return v


def _ssid(req: dict) -> str:
    ssid = _text(req, "ssid")
    if len(ssid.encode()) > 32:
        raise Refused("a Wi-Fi name is at most 32 bytes")
    return ssid


def _password(req: dict) -> str:
    """"" (none: an open network, or the saved profile as it is), a WPA passphrase (8 to 63
    printable ASCII characters) or a raw 64-hex-digit key: what the Wi-Fi standard takes."""
    pw = req.get("password", "")
    if not isinstance(pw, str):
        raise Refused("password must be a string")
    if pw and not (8 <= len(pw) <= 63 and all(" " <= c <= "~" for c in pw)) \
            and not re.fullmatch(r"[0-9A-Fa-f]{64}", pw):
        raise Refused("a Wi-Fi password is 8 to 63 printable ASCII characters")
    return pw


def handle(ops: Ops, verb: str, req, *, created: Created, setup_running) -> dict:
    """One request: check it, do it, say how it went. Raises Refused for anything else."""
    if not isinstance(req, dict):
        raise Refused("the request must be a JSON object")
    if verb == "connectivity":
        return {"connectivity": ops.connectivity()}
    if verb == "ap-down":
        ops.ap_down()
        return {"ok": True}
    if verb == "ap-delete":
        ops.ap_delete()
        return {"ok": True}
    if verb == "up":
        uuid, wait = _text(req, "uuid"), req.get("wait", CONNECT_WAIT_SECS)
        if type(wait) is not int or not 0 <= wait <= CONNECT_WAIT_SECS:
            raise Refused(f"wait must be a whole number of seconds, 0 to {CONNECT_WAIT_SECS}")
        if not ops.client_profile(uuid):
            raise Refused("not a saved Wi-Fi network")
        return {"ok": ops.up(uuid, wait)}
    if verb == "delete":
        uuid = _text(req, "uuid")
        if uuid not in created:
            raise Refused("not a profile this helper created")
        ops.delete(uuid)
        created.discard(uuid)
        return {"ok": True}
    if verb not in ("ap-up", "join"):
        raise Refused(f"unknown request {verb!r}")
    dev = _text(req, "dev")
    if dev not in ops.wifi_devices():
        raise Refused("not a Wi-Fi device")
    ssid, password = _ssid(req), _password(req)
    if verb == "ap-up":
        if not password:
            raise Refused("the setup network needs a password")
        if not setup_running():
            raise Refused("Wi-Fi setup is not running")
        ok, err = ops.ap_up(dev, ssid, password)
        return {"ok": ok, "err": err}
    hidden = req.get("hidden", False)
    if not isinstance(hidden, bool):
        raise Refused("hidden must be true or false")
    ok, why, made = ops.join(dev, ssid, password, hidden)
    if made:
        created.add(made)
    return {"ok": ok, "why": why, "created": made}


class _Stopped(BaseException):
    pass


def _on_stop(signum, frame):
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, signal.SIG_IGN)   # one is enough: the clean-up must not be cut short
    raise _Stopped


def _setup_running() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", SETUP_UNIT],
                          env=NMCLI_ENV, check=False).returncode == 0


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if os.geteuid() != 0:
        _reply({"refused": "run as root, through the sudoers line"})
        return 2
    if len(argv) != 1:
        _reply({"refused": "usage: wifi_apply.py <verb> (the request as JSON on stdin)"})
        return 2
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, _on_stop)
    ops = Ops()
    try:
        data = sys.stdin.read(MAX_REQUEST + 1)
        if len(data) > MAX_REQUEST:
            raise Refused("the request is too long")
        try:
            req = json.loads(data) if data.strip() else {}
        except ValueError:
            raise Refused("the request is not JSON") from None
        _reply(handle(ops, argv[0], req, created=Created(CREATED_PATH), setup_running=_setup_running))
        return 0
    except Refused as e:
        _reply({"refused": str(e)})
        return 2
    except _Stopped:
        ops.undo()
        _reply({"ok": False, "why": NO_CONNECT, "stopped": True})
        return 3


if __name__ == "__main__":
    sys.exit(main())
