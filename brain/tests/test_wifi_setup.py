"""Wi-Fi setup: the setup service (wifi_setup.py — name, password, nmcli parsing, the
page, the join/rollback/timeout flow against a fake NetworkManager and a real HTTP
server) and its voice half (wifi_voice.py — the phrase, confirming, speaking the
details digit by digit, following status.json, repeat and cancel). No radio needed."""
import re
import asyncio
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import pytest

from teaport_brain import display, i18n, wifi
from teaport_brain import wifi_setup as ws
from teaport_brain import wifi_voice as wv
from tempdirs import tempdir


# ------------------------------------------------------------------ identity

def test_ssid_from_the_mac_and_an_override():
    assert ws.setup_ssid("f8:3d:c6:1f:9e:35") == "teaport-9e35"
    assert ws.setup_ssid("F8:3D:C6:1F:9E:35") == "teaport-9e35"
    assert ws.setup_ssid("") == "teaport-0000"
    assert ws.setup_ssid("f8:3d:c6:1f:9e:35", "Kitchen Teaport") == "Kitchen Teaport"


@pytest.mark.parametrize("override", ["12345678", "CafeBabe", " deadbeef "])
def test_an_all_hex_override_is_refused_with_a_warning(override, capsys):
    # A ZXing-based scanner reads an all-hex SSID in the QR code as raw bytes.
    assert ws.setup_ssid("f8:3d:c6:1f:9e:35", override) == "teaport-9e35"
    assert "all hex digits" in capsys.readouterr().out
    assert ws.setup_ssid("f8:3d:c6:1f:9e:35", "Cafe-1") == "Cafe-1"   # not all hex: kept


DEFAULT = re.compile(r"[a-hj-km-npr-z2-9]{4}-[a-hj-km-npr-z2-9]{4}-[a-hj-km-npr-z2-9]{4}")


def is_default(pw: str) -> bool:
    """The default password's form: three groups of four, no 0/o/1/l/i."""
    return bool(DEFAULT.fullmatch(pw)) and not set(pw) & set("0o1liq")


def test_the_flash_time_password_wins_when_valid():
    assert ws.setup_password("47190352") == "47190352"
    assert ws.setup_password(" Tito2017 ") == "Tito2017"
    assert ws.setup_password("k7mq-x4tn-p9wr") == "k7mq-x4tn-p9wr"  # the insert's form
    for bad in ("", "1234567", "x" * 64, "pässwörd1"):
        assert is_default(ws.setup_password(bad)), bad


def test_the_default_password_is_three_groups_from_the_csprng_and_fresh_each_run(monkeypatch):
    assert set(ws.PASSWORD_ALPHABET) == set("abcdefghjkmnprstuvwxyz23456789")
    assert not set(ws.PASSWORD_ALPHABET) & set("0o1liq")  # look-alikes, and q (ja: 9)
    many = [ws.setup_password("") for _ in range(500)]
    assert all(is_default(pw) and len(pw) == 14 for pw in many)
    assert len(set(many)) == len(many)              # fresh every run
    assert set("".join(many).replace("-", "")) == set(ws.PASSWORD_ALPHABET)  # all used
    drawn = []

    class Csprng:
        @staticmethod
        def choice(seq):
            drawn.append(seq)
            return seq[0]

    monkeypatch.setattr(ws, "secrets", Csprng)
    assert ws.setup_password("") == "aaaa-aaaa-aaaa"
    assert drawn == [ws.PASSWORD_ALPHABET] * 12     # the secrets module, one draw a character


def test_the_qr_code_and_the_screen_carry_the_password_as_it_is_typed():
    assert ws.wifi_qr("teaport-9e35", "k7mq-x4tn-p9wr") == "WIFI:T:WPA;S:teaport-9e35;P:k7mq-x4tn-p9wr;;"
    assert ws.wifi_qr("Home;Net", 'a;b,c:d"e\\f') == 'WIFI:T:WPA;S:Home\\;Net;P:a\\;b\\,c\\:d\\"e\\\\f;;'
    assert len(ws.wifi_qr("teaport-9e35", "k7mq-x4tn-p9wr").encode()) <= 53  # the avatar's QR
    # One line that does not scroll: the 14 characters whole, after a shorter label.
    assert ws.password_line("k7mq-x4tn-p9wr") == "Pass  k7mq-x4tn-p9wr"
    assert ws.password_line("47190352") == "Password  47190352"
    assert len(ws.password_line("k7mq-x4tn-p9wr")) <= ws.SCREEN_LINE_CHARS


# ------------------------------------------------------------------ nmcli output

def test_terse_lines_unescape_colons_and_backslashes():
    assert wifi.split_terse(r"Stefano’s iPhone\: 13:75:WPA2") == ["Stefano’s iPhone: 13", "75", "WPA2"]
    assert wifi.split_terse(r"a\\b:1:") == ["a\\b", "1", ""]


def test_scan_keeps_each_named_network_once_strongest_first():
    nets = wifi.parse_scan("home:50:WPA2\ncafe:70:\nhome:80:WPA2 WPA3\n:90:WPA2\nx:bad:WPA2\n")
    assert [n["ssid"] for n in nets] == ["home", "cafe", "x"]
    assert nets[0] == {"ssid": "home", "signal": 80, "open": False, "security": "WPA2 WPA3"}
    assert nets[1]["open"] and nets[1]["security"] == "open"


# ------------------------------------------------------------------ the page

def test_the_page_carries_both_logos_inline():
    page = ws.render_page([])
    # The marketing site's switch: the dark-background logo in dark mode, else the light.
    assert '<source srcset="data:image/svg+xml;base64,' in page
    assert 'media="(prefers-color-scheme: dark)"' in page
    assert '<img src="data:image/svg+xml;base64,' in page and 'alt="Teaport"' in page
    assert "/config/logo.svg" not in page          # offline: nothing to fetch


def test_the_page_escapes_network_names():
    page = ws.render_page([{"ssid": '<script>"x"</script>', "signal": 60, "open": False}])
    assert "<script>" not in page and "&lt;script&gt;" in page and "&quot;x&quot;" in page


def test_the_form_is_validated():
    q = urllib.parse.urlencode
    assert ws.parse_form(q({"ssid": "home", "password": "secret123"}).encode()) == ("home", "secret123", False, "")
    assert ws.parse_form(q({"ssid": "", "other": " Hidden ", "password": ""}).encode()) == ("Hidden", "", True, "")
    assert ws.parse_form(q({"ssid": "", "other": ""}).encode())[3]
    assert "8 to 63" in ws.parse_form(q({"ssid": "home", "password": "short"}).encode())[3]
    assert ws.parse_form(q({"ssid": "x" * 33}).encode())[3]


# ------------------------------------------------------------------ joining

class FakeRun:
    """nmcli as a script: (argv prefix) -> (rc, out, err); every call is recorded."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv, timeout=60):
        self.calls.append(argv)
        for prefix, result in self.table:
            if argv[1:1 + len(prefix)] == list(prefix):
                return result(argv) if callable(result) else result
        return 0, "", ""


def _esc(v):
    return v.replace("\\", "\\\\").replace(":", "\\:")


class FakeNmcli:
    """nmcli against a pretend NetworkManager: saved profiles by UUID, the networks in
    the air (ssid -> password, None for open), and the radio's scan list — empty to
    begin with, as it is right after access-point mode."""

    def __init__(self, air, profiles=(), visible=()):
        self.air, self.visible, self.calls = dict(air), set(visible), []
        self.profiles = {}
        for p in profiles:
            self.profiles[p.get("uuid") or f"uuid-{len(self.profiles)}"] = {
                "id": p["ssid"], "mode": "infrastructure", "key_mgmt": "wpa-psk",
                "psk": "", "psk_flags": "0", **p}
        self.active = None

    def _new(self, **p):
        uuid = f"uuid-new-{len(self.calls)}"
        self.profiles[uuid] = {"mode": "infrastructure", "psk_flags": "0", **p}
        return uuid

    def _works(self, uuid):
        p = self.profiles[uuid]
        want = self.air.get(p["ssid"], "absent")
        ok = want != "absent" and (want is None if p["key_mgmt"] == "" else p["psk"] == want)
        self.active = uuid if ok else None
        return ok

    def __call__(self, argv, timeout=60):
        self.calls.append(argv)
        a = argv[1:]
        if a[:5] == ["-t", "-f", "UUID,TYPE", "connection", "show"]:
            return 0, "".join(f"{u}:802-11-wireless\n" for u in self.profiles), ""
        if "connection" in a and "show" in a and "uuid" in a:
            p = self.profiles.get(a[-1])
            if not p:
                return 10, "", "Error: no such profile"
            values = {"connection.id": p["id"], "802-11-wireless.ssid": p["ssid"],
                      "802-11-wireless.mode": p["mode"]}
            if p["key_mgmt"]:
                values["802-11-wireless-security.key-mgmt"] = p["key_mgmt"]
                values["802-11-wireless-security.psk-flags"] = p["psk_flags"]
                if a[0] == "-s":
                    values["802-11-wireless-security.psk"] = p["psk"]
            fields = a[a.index("-f") + 1].split(",")
            return 0, "".join(f"{f}:{_esc(values[f])}\n" for f in fields if f in values), ""
        if a[:2] == ["-t", "-f"] and "wifi" in a and "list" in a:
            if a[-1] == "yes":
                self.visible = set(self.air)
            return 0, "".join(f"{_esc(s)}:70:WPA2\n" for s in sorted(self.visible)), ""
        if a[:3] == ["connection", "modify", "uuid"]:
            p, kv = self.profiles[a[3]], a[4:]
            for k, v in zip(kv[::2], kv[1::2]):
                p[{"wifi-sec.psk": "psk", "wifi-sec.psk-flags": "psk_flags"}[k]] = v
            return 0, "", ""
        if a[2:5] == ["connection", "up", "uuid"]:
            return (0, "", "") if self._works(a[5]) else (4, "", "Error: Secrets were required")
        if a[2:5] == ["device", "wifi", "connect"]:
            ssid = a[5]
            hidden = a[-2:] == ["hidden", "yes"]
            if ssid not in self.visible and not hidden:
                return 10, "", f"Error: No network with SSID '{ssid}' found."
            pw = a[a.index("password") + 1] if "password" in a else ""
            uuid = self._new(id=a[a.index("name") + 1], ssid=ssid,
                             key_mgmt="wpa-psk" if pw else "", psk=pw)
            return (0, f"Device 'wlan0' successfully activated with '{uuid}'.", "") if self._works(uuid) \
                else (4, "", "Error: Connection activation failed: Secrets were required")
        if a[:3] == ["connection", "delete", "uuid"]:
            self.profiles.pop(a[3], None)
            return 0, "", ""
        return 0, "", ""


def test_saved_profiles_are_found_by_their_real_ssid():
    nm = FakeNmcli({}, profiles=[{"ssid": "my\\net:5G", "uuid": "u1"}, {"ssid": "ap", "mode": "ap", "uuid": "u2"}])
    assert wifi.NM(nm).saved_profile("my\\net:5G") == {"uuid": "u1", "key_mgmt": "wpa-psk"}
    assert wifi.NM(nm).saved_profile("ap") is None             # an access point is not joined
    assert wifi.NM(nm).saved_profile(" my\\net:5G") is None    # spaces are part of a name


def test_join_uses_a_saved_profile_without_a_password():
    nm = FakeNmcli({"home": "homepass1"}, profiles=[{"ssid": "home", "psk": "homepass1", "uuid": "u1"}])
    assert wifi.NM(nm).join("wlan0", "home", "", False) == (True, "", None)
    assert ["nmcli", "--wait", "45", "connection", "up", "uuid", "u1"] in nm.calls
    assert ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", "wlan0",
            "--rescan", "yes"] in nm.calls   # the empty list after AP mode is refreshed first


def test_a_wrong_new_password_puts_the_old_one_back_exactly():
    # A password with the characters nmcli escapes, and spaces at both ends.
    old = r" pa:ss\word "
    nm = FakeNmcli({"home": "realpass1"},
                   profiles=[{"ssid": "home", "psk": old, "psk_flags": "1", "uuid": "u1"}])
    ok, why, created = wifi.NM(nm).join("wlan0", "home", "newpass99", False)
    assert (ok, created) == (False, None) and why == "the password did not work"
    assert nm.profiles["u1"]["psk"] == old and nm.profiles["u1"]["psk_flags"] == "1"
    assert nm.profiles["u1"]["key_mgmt"] == "wpa-psk"
    ok, _, _ = wifi.NM(nm).join("wlan0", "home", "realpass1", False)
    assert ok and nm.profiles["u1"]["psk"] == "realpass1" and len(nm.profiles) == 1


@pytest.mark.parametrize("kind", ["", "wpa-eap", "owe"])
def test_open_and_enterprise_profiles_are_never_changed(kind):
    nm = FakeNmcli({"cafe": "cafepass1"}, profiles=[{"ssid": "cafe", "key_mgmt": kind, "uuid": "u1"}])
    before = dict(nm.profiles["u1"])
    ok, why, created = wifi.NM(nm).join("wlan0", "cafe", "wrongpass", False)
    assert not ok and created and created != "u1"            # a separate profile, to drop
    assert nm.profiles["u1"] == before
    assert not any(c[1:3] == ["connection", "modify"] for c in nm.calls)


def test_a_wpa3_profile_keeps_its_key_management():
    nm = FakeNmcli({"home": "newpass99"}, profiles=[{"ssid": "home", "key_mgmt": "sae", "psk": "oldpass99", "uuid": "u1"}])
    assert wifi.NM(nm).join("wlan0", "home", "newpass99", False) == (True, "", None)
    assert nm.profiles["u1"]["key_mgmt"] == "sae" and nm.profiles["u1"]["psk"] == "newpass99"


def test_a_new_network_is_reported_by_uuid_and_a_namesake_survives_its_removal():
    # The user's own profile is named "cafe" but saved for another network: deleting the
    # failed new one by name would take it too.
    nm = FakeNmcli({"cafe": "cafepass1"}, profiles=[{"id": "cafe", "ssid": "cafe-old", "uuid": "mine"}])
    n = wifi.NM(nm)
    ok, why, created = n.join("wlan0", "cafe", "badpass1", False)
    assert (ok, why) == (False, "the password did not work") and created.startswith("uuid-new")
    n.delete(created)
    assert list(nm.profiles) == ["mine"]
    assert not any("id" in c and "delete" in c for c in nm.calls)


def test_a_stale_scan_list_is_refreshed_and_the_join_tried_again():
    nm = FakeNmcli({"cafe": "cafepass1"})
    n = wifi.NM(nm)
    n.visible = lambda dev, ssid: True     # the list looked fine, but "not found" anyway
    ok, _, created = n.join("wlan0", "cafe", "cafepass1", False)
    assert ok and created and sum("connect" in c for c in nm.calls) == 2
    nm2 = FakeNmcli({})
    ok, why, created = wifi.NM(nm2).join("wlan0", "x", "", True)   # hidden, and not there
    connect = [c for c in nm2.calls if "connect" in c][0]
    assert connect[-2:] == ["hidden", "yes"]


def test_a_name_nmcli_would_take_for_an_option_is_refused():
    nm = FakeNmcli({"-s": "pass12345"})
    assert wifi.NM(nm).join("wlan0", "-s", "pass12345", False) == (False, "it would not connect", None)
    assert not any("connect" in c for c in nm.calls)


def test_undo_drops_what_an_interrupted_join_made():
    nm = FakeNmcli({"cafe": "cafepass1"})
    n = wifi.NM(nm)
    n._pending = ("new", set(), "cafe")
    nm._new(id="cafe", ssid="cafe", key_mgmt="wpa-psk", psk="x")
    n.undo()
    assert nm.profiles == {} and n._pending is None
    nm = FakeNmcli({}, profiles=[{"ssid": "home", "psk": "old:pass", "uuid": "u1"}])
    n = wifi.NM(nm)
    n._pending = ("psk", "u1", "0", "old:pass")
    nm.profiles["u1"]["psk"] = "half-done"
    n.undo()
    assert nm.profiles["u1"]["psk"] == "old:pass"


def test_online_means_through_the_wifi(monkeypatch):
    def run(state):
        return FakeRun([(("networking", "connectivity", "check"), (0, "full\n", "")),
                        (("-t", "-f", "GENERAL.IP4-CONNECTIVITY"), (0, f"GENERAL.IP4-CONNECTIVITY:{state}\n", ""))])
    probes = []
    monkeypatch.setattr(wifi, "_reachable", lambda host, dev: probes.append((host, dev)) or False)
    # NM says the box is online (Ethernet), but not through wlan0: not online.
    assert wifi.NM(run("1 (none)")).online("wlan0") is False
    assert probes and all(dev == "wlan0" for _, dev in probes)
    assert wifi.NM(run("4 (full)")).online("wlan0") is True


def test_the_reachability_probe_is_bound_to_the_device(monkeypatch):
    seen = []

    class Sock:
        def __init__(self, *a): pass
        def settimeout(self, t): pass
        def setsockopt(self, level, opt, value): seen.append((opt, value))
        def connect(self, addr): seen.append(addr)
        def close(self): pass
    monkeypatch.setattr(wifi.socket, "socket", Sock)
    assert wifi._reachable("1.1.1.1", "wlan0")
    assert seen == [(wifi.socket.SO_BINDTODEVICE, b"wlan0"), ("1.1.1.1", 443)]

    class Unprivileged(Sock):
        def setsockopt(self, *a): raise PermissionError
    monkeypatch.setattr(wifi.socket, "socket", Unprivileged)
    assert wifi._reachable("1.1.1.1", "wlan0") is False   # cannot prove the device: no


class _SwitchNM:
    def __init__(self, join, online=True, after=None):
        self._join, self._online, self.events = join, online, []
        self.now = {"name": "home", "uuid": "u-home"}
        self._after = after  # what the device is on once the join is over
    def active(self, dev):
        return self.now
    def join(self, dev, ssid, pw, hidden):
        self.now = self._after
        return self._join
    def online(self, dev): return self._online
    def delete(self, p): self.events.append(("delete", p))
    def up(self, p, wait=45): self.events.append(("up", p)); return True


def test_an_online_switch_that_fails_puts_the_old_network_back():
    good = _SwitchNM((True, "", "u-cafe"), after={"name": "cafe", "uuid": "u-cafe"})
    assert wifi.switch(good, "wlan0", "cafe", "pw123456", False) == (True, "")
    assert good.events == []
    bad = _SwitchNM((False, "the password did not work", "u-cafe"))
    assert wifi.switch(bad, "wlan0", "cafe", "x" * 8, False) == (False, "the password did not work")
    assert bad.events == [("delete", "u-cafe"), ("up", "u-home")]
    dead = _SwitchNM((True, "", "u-cafe"), online=False, after={"name": "cafe", "uuid": "u-cafe"})
    ok, why = wifi.switch(dead, "wlan0", "cafe", "x" * 8, False)
    assert not ok and "no internet" in why and ("up", "u-home") in dead.events


def test_a_failed_new_password_for_the_network_it_is_on_brings_that_back():
    # The profile "home" is both the one to put back and the one whose password failed:
    # compared by UUID, it is brought back up (it was skipped when compared by name).
    same = _SwitchNM((False, "the password did not work", None), after=None)
    assert not wifi.switch(same, "wlan0", "home", "wrongpass", False)[0]
    assert same.events == [("up", "u-home")]
    back = _SwitchNM((False, "the password did not work", None), after={"name": "home", "uuid": "u-home"})
    wifi.switch(back, "wlan0", "home", "wrongpass", False)
    assert back.events == []   # NM has it up again already


# ------------------------------------------------------------------ the whole run

class FakeNM:
    """NetworkManager as a state: the AP, the joins, the connectivity."""

    def __init__(self, join_results, online=True, previous="old-wifi", addresses=None):
        self.join_results, self._online, self.previous = list(join_results), online, previous
        # What `nmcli device show` lists, one call after another (the last repeats).
        self._addresses = list(addresses or [[wifi.AP_ADDRESS]])
        self.events = []

    def wifi_device(self): return "wlan0"
    def mac(self, dev): return "f8:3d:c6:1f:9e:35"
    def scan(self, dev): return [{"ssid": "home", "signal": 80, "open": False, "security": "WPA2"}]
    def active(self, dev): return {"name": self.previous, "uuid": "u-" + self.previous} if self.previous else None
    def addresses(self, dev):
        return self._addresses.pop(0) if len(self._addresses) > 1 else self._addresses[0]
    def ap_up(self, dev, ssid, pw): self.events.append(("ap_up", ssid)); return True, ""
    def ap_down(self): self.events.append(("ap_down",))
    def ap_delete(self): self.events.append(("ap_delete",))
    def up(self, profile, wait=45): self.events.append(("up", profile, wait)); return True
    def delete(self, profile): self.events.append(("delete", profile))
    def undo(self): self.events.append(("undo",))
    def online(self, dev): return self._online

    def join(self, dev, ssid, pw, hidden):
        self.events.append(("join", ssid, pw, hidden))
        result = self.join_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _Recorded(ws.Status):
    """status.json as written, plus every phase in order (the fake NM is instant, so a
    phase can be gone before anyone reads the file)."""

    def __init__(self, directory):
        super().__init__(directory, "test")
        self.history = []

    def set(self, phase, **fields):
        super().set(phase, **fields)
        self.history.append(dict(self.data))


def _no_avahi(setup, avahi=None):
    """No real avahi-publish from a test: it would announce a .local name on this machine.
    `avahi`: a FakeAvahi standing in for it (None: not installed)."""
    setup.publish = (lambda name: None) if avahi is None else (lambda name: avahi.start(setup, name))
    setup.unpublish = lambda: None


class FakeAvahi:
    """avahi-publish as a real, live process (a sleep) that a test can kill, so page_url's
    poll() sees it as on the box: running until killed."""

    def __init__(self):
        self.procs = []

    def start(self, setup, name):
        proc = subprocess.Popen(["sleep", "30"])
        self.procs.append(proc)
        setup.avahi, setup.avahi_name = proc, name
        return proc

    def kill(self):
        for proc in self.procs:
            proc.kill()
            proc.wait()


def _run_setup(nm, minutes=1.0, post=None, screen=None, ssid="teaport-9e35", avahi=None,
               lang=None, page_lang=None):
    """Run a Setup in a thread on a free port; post(form) each time the AP comes up. The
    page is bound to 127.0.0.1 here (on the box: wifi.AP_ADDRESS). The return code is
    the exception instead when the run raised one. `lang`: the language the brain handed
    over (an i18n.T); `page_lang`: the page's, posted as its form does (?lang=)."""
    status = _Recorded(tempdir())
    setup = ws.Setup(nm, status, port=0, minutes=minutes, screen=screen, bind="127.0.0.1",
                     lang=lang)
    _no_avahi(setup, avahi)
    result = {}

    def run():
        try:
            result["rc"] = setup.run(ssid, "47190352")
        except Exception as e:
            result["rc"] = e

    t = threading.Thread(target=run)
    t.start()
    posted, deadline = 0, time.time() + 15
    while t.is_alive() and time.time() < deadline:
        time.sleep(0.02)
        ups = sum(1 for s in list(status.history) if s["phase"] == "ap_up")
        if post and posted < len(post) and ups > posted:
            page = urllib.request.urlopen(f"http://127.0.0.1:{setup.port}/hotspot-detect.html").read().decode()
            assert 'value="home"' in page and ssid not in page
            body = urllib.parse.urlencode(post[posted]).encode()
            query = f"?lang={page_lang}" if page_lang else ""
            reply = urllib.request.urlopen(f"http://127.0.0.1:{setup.port}/connect{query}",
                                           body).read().decode()
            assert i18n.get(page_lang or "en")._("Teaport is joining") in reply
            posted += 1
    t.join(5)
    assert setup.server is None or setup.server.server_address[0] == "127.0.0.1"
    return result.get("rc"), status.history


def test_a_good_join_ends_connected():
    nm = FakeNM([(True, "", "home")])
    rc, seen = _run_setup(nm, post=[{"ssid": "home", "password": "homepass1"}])
    assert rc == 0
    assert [s["phase"] for s in seen] == ["scanning", "ap_up", "joining", "connected"]
    assert seen[1]["password"] == "47190352" and seen[1]["url"] == f"http://{wifi.AP_ADDRESS}"
    assert ("join", "home", "homepass1", False) in nm.events and nm.events[-1] == ("ap_delete",)


def test_a_failed_join_brings_the_setup_network_back_with_the_reason():
    nm = FakeNM([(False, "the password did not work", "home"), (True, "", "home")])
    rc, seen = _run_setup(nm, post=[{"ssid": "home", "password": "wrongpass"},
                                    {"ssid": "home", "password": "homepass1"}])
    assert rc == 0
    phases = [s["phase"] for s in seen]
    assert phases == ["scanning", "ap_up", "joining", "failed", "ap_up", "joining", "connected"]
    assert "password did not work" in seen[4]["error"]
    assert ("delete", "home") in nm.events          # the wrong-password profile is dropped
    assert not any(e[0] == "up" for e in nm.events)  # the old network stays down: retrying


def test_joined_without_internet_counts_as_failed():
    nm = FakeNM([(True, "", "home")], online=False)
    rc, seen = _run_setup(nm, minutes=0.02, post=[{"ssid": "home", "password": "homepass1"}])
    assert any(s["phase"] == "failed" and "no internet" in s["reason"] for s in seen)


def test_nobody_comes_and_the_previous_connection_is_put_back():
    nm = FakeNM([])
    rc, seen = _run_setup(nm, minutes=0.01)
    assert rc == 2 and seen[-1]["phase"] == "timeout"
    assert ("up", "u-old-wifi", 45) in nm.events and nm.events[-1] == ("ap_delete",)


def test_a_stop_in_the_middle_of_a_join_puts_everything_back():
    # SIGTERM (a spoken "cancel": systemctl stop) arriving while nmcli joins.
    nm = FakeNM([ws.Stopped()])
    rc, seen = _run_setup(nm, post=[{"ssid": "home", "password": "homepass1"}])
    assert rc == 3 and seen[-1]["phase"] == "stopped"
    after = nm.events[nm.events.index(("join", "home", "homepass1", False)) + 1:]
    # The half-done join undone, the old network asked for without waiting (systemd is).
    assert after == [("ap_down",), ("undo",), ("up", "u-old-wifi", 0), ("ap_delete",)]


def test_a_stop_before_setup_began_is_a_clean_stop_with_nothing_to_put_back():
    class StoppedEarly(FakeNM):
        def active(self, dev): raise ws.Stopped()
    nm = StoppedEarly([])
    rc, seen = _run_setup(nm)
    assert rc == 3 and [s["phase"] for s in seen] == ["stopped"]
    assert nm.events == [("ap_delete",)]   # the old network was never touched


def test_a_stop_during_the_clean_up_finishes_it_and_the_result_stands():
    class StoppedInCleanUp(FakeNM):
        def ap_delete(self):
            super().ap_delete()
            if self.events.count(("ap_delete",)) == 1:
                raise ws.Stopped()
    nm = StoppedInCleanUp([(True, "", "home")])
    rc, seen = _run_setup(nm, post=[{"ssid": "home", "password": "homepass1"}])
    assert rc == 0 and seen[-1]["phase"] == "connected"
    assert nm.events[-2:] == [("ap_delete",), ("ap_delete",)]


def _screen(avatar=True, events=None, qr=True, qr_max=53, unicode=False):
    """A Screen recording what it sends (into `events` too, as ("screen", ...), when
    given); `avatar`: whether a daemon that draws screens is there; `qr`: and QR codes,
    up to `qr_max` bytes; `unicode`: and every script (its fallback fonts, #207)."""
    sent = []

    def send(ev, path):
        sent.append(ev["screen"])
        if events is not None:
            events.append(("screen", ev["screen"]))
        return True

    feats = {"screen": 1, **({"qr": 1, "qr_max_bytes": qr_max} if qr else {}),
             **({"unicode": 1} if unicode else {})} if avatar else {}
    return display.Screen("wifi-setup", send=send, features=lambda path: feats), sent


def test_the_display_shows_the_details_then_the_join_and_keeps_connected_a_while():
    screen, sent = _screen()
    rc, seen = _run_setup(FakeNM([(True, "", "home")]), screen=screen,
                          post=[{"ssid": "home", "password": "homepass1"}])
    assert rc == 0
    assert seen[1]["phase"] == "ap_up" and seen[1]["screen"] is True   # the voice says so
    assert seen[1]["qr"] is True
    shown = [s for s in sent if s.get("lines")]
    assert shown[0]["qr"] == "WIFI:T:WPA;S:teaport-9e35;P:47190352;;"   # a phone joins
    assert all("qr" not in s for s in shown[1:])                         # only then
    assert shown[0]["lines"] == ["Network  teaport-9e35", "Password  47190352",
                                 f"http://{wifi.AP_ADDRESS}"]
    assert shown[0]["title"] == "Setup mode" and shown[0]["ttl"] == display.HOLD_TTL_SECS
    assert shown[1]["lines"] == ["Joining", "home"]
    # The last word: up for its seconds after the unit has exited, not taken down.
    assert sent[-1]["lines"] == ["Connected to", "home"]
    assert sent[-1]["title"] == "Wi-Fi"   # setup mode is over
    assert sent[-1]["ttl"] == ws.CONNECTED_SCREEN_SECS
    assert "homepass1" not in json.dumps(sent)       # the user's own password: never


def test_no_avatar_and_the_status_says_no_screen():
    screen, _ = _screen(avatar=False)
    _, seen = _run_setup(FakeNM([]), minutes=0.01, screen=screen)
    assert seen[1]["phase"] == "ap_up" and seen[1]["screen"] is False
    _, seen = _run_setup(FakeNM([]), minutes=0.01)                     # no Screen at all
    assert seen[1]["screen"] is False and seen[1]["qr"] is False


def test_an_avatar_without_qr_codes_and_the_status_says_text_only():
    screen, _ = _screen(qr=False)
    _, seen = _run_setup(FakeNM([]), minutes=0.01, screen=screen)
    assert seen[1]["screen"] is True and seen[1]["qr"] is False


def test_the_join_string_escapes_what_it_must():
    assert ws.wifi_qr("teaport-9e35", "47190352") == "WIFI:T:WPA;S:teaport-9e35;P:47190352;;"
    assert ws.wifi_qr('a;b:c,d\\e"f', "p;w") == 'WIFI:T:WPA;S:a\\;b\\:c\\,d\\\\e\\"f;P:p\\;w;;'
    assert ws.wifi_qr("Café ☕", "x") == "WIFI:T:WPA;S:Café ☕;P:x;;"   # the real name


@pytest.mark.parametrize("join, minutes", [([ws.Stopped()], 1.0), ([], 0.01)])
def test_a_stop_or_a_timeout_takes_the_screen_down(join, minutes):
    screen, sent = _screen()
    _run_setup(FakeNM(join), minutes=minutes, screen=screen,
               post=[{"ssid": "home", "password": "homepass1"}] if join else None)
    assert sent[0]["lines"][0] == "Network  teaport-9e35"
    assert sent[-1] == {"id": "wifi-setup"}


@pytest.mark.parametrize("join, minutes", [([ws.Stopped()], 1.0), ([], 0.01)])
def test_the_screen_goes_before_the_old_network_is_waited_for(join, minutes):
    # nm.up can wait up to 45 s: the password (or "Joining") must not stay up meanwhile.
    nm = FakeNM(join)
    screen, _ = _screen(events=nm.events)
    _run_setup(nm, minutes=minutes, screen=screen,
               post=[{"ssid": "home", "password": "homepass1"}] if join else None)
    take_down = nm.events.index(("screen", {"id": "wifi-setup"}))
    up = next(i for i, e in enumerate(nm.events) if e[0] == "up")
    assert take_down < up
    assert not any(e[0] == "screen" for e in nm.events[take_down + 1:])


def test_a_failed_join_is_shown_until_the_setup_network_is_back():
    screen, sent = _screen()
    nm = FakeNM([(False, "the password did not work", "home"), (True, "", "home")])
    rc, _ = _run_setup(nm, screen=screen, post=[{"ssid": "home", "password": "wrongpass"},
                                                {"ssid": "home", "password": "homepass1"}])
    assert rc == 0
    shown = [(s["lines"], s["ttl"]) for s in sent if s.get("lines")]
    details = ["Network  teaport-9e35", "Password  47190352", f"http://{wifi.AP_ADDRESS}"]
    hold = display.HOLD_TTL_SECS
    # Refreshes aside, in order: each screen replaces the one before, nothing in between.
    order = [x for i, x in enumerate(shown) if i == 0 or x != shown[i - 1]]
    assert order == [(details, hold), (["Joining", "home"], hold),
                     (["Could not join", "home"], hold),
                     (details, hold), (["Joining", "home"], hold),
                     (["Connected to", "home"], ws.CONNECTED_SCREEN_SECS)]
    assert {"id": "wifi-setup"} not in sent


def test_an_unexpected_error_takes_the_screen_down():
    screen, sent = _screen()
    rc, _ = _run_setup(FakeNM([RuntimeError("nmcli fell over")]), screen=screen,
                       post=[{"ssid": "home", "password": "homepass1"}])
    assert isinstance(rc, RuntimeError)
    assert sent[-2]["lines"] == ["Joining", "home"] and sent[-1] == {"id": "wifi-setup"}
    screen._thread.join(timeout=5)
    assert not screen._thread.is_alive()


def test_network_names_reach_the_display_in_ascii():
    screen, sent = _screen()
    rc, seen = _run_setup(FakeNM([(True, "", "Café ☕")]), screen=screen, ssid="東京-box",
                          post=[{"ssid": "Café ☕", "password": "homepass1"}])
    assert rc == 0 and seen[1]["ssid"] == "東京-box"     # the voice and page keep the name
    shown = [s["lines"] for s in sent if s.get("lines")]
    # The name folded would read wrong ("-box"): the code beside it carries the real one.
    assert shown[0] == ["Network  (scan the code)", "Password  47190352", f"http://{wifi.AP_ADDRESS}"]
    assert ["Joining", "Cafe"] in shown and shown[-1] == ["Connected to", "Cafe"]


def test_a_folded_name_without_a_code_is_shown_folded():
    screen, sent = _screen(qr=False)
    _, seen = _run_setup(FakeNM([]), minutes=0.01, screen=screen, ssid="東京-box")
    assert seen[1]["qr"] is False
    assert sent[0]["lines"][0] == "Network  -box" and "qr" not in sent[0]
    # The address the screen shows is the one the status carries for the voice.
    assert sent[0]["lines"][2] == seen[1]["url"] == f"http://{wifi.AP_ADDRESS}"


# ------------------------------------------------------------------ the screen's script (#207)

RU = i18n.get("ru")


def _screens(sent):
    """(title, lines) of each screen shown, refreshes aside."""
    shown = [(s["title"], s["lines"]) for s in sent if s.get("lines")]
    return [x for i, x in enumerate(shown) if i == 0 or x != shown[i - 1]]


@pytest.mark.parametrize("qr", [True, False])
def test_without_unicode_every_screen_is_ascii_english_whatever_the_language(qr):
    """An avatar without fallback fonts gets today's screens, word for word, whatever
    the voice's language and the page's, and whatever the names are written in."""
    screen, sent = _screen(qr=qr)
    nm = FakeNM([(False, "the password did not work", "Дом"), (True, "", "Café ☕")])
    rc, seen = _run_setup(nm, screen=screen, ssid="Кухня", lang=RU, page_lang="ja",
                          post=[{"ssid": "Дом", "password": "wrongpass"},
                                {"ssid": "Café ☕", "password": "homepass1"}])
    assert rc == 0 and seen[1]["unicode"] is False
    details = ["Network  " + ("(scan the code)" if qr else "your network"),
               "Password  47190352", f"http://{wifi.AP_ADDRESS}"]
    assert _screens(sent) == [
        ("Setup mode", details), ("Setup mode", ["Joining", "your network"]),
        ("Setup mode", ["Could not join", "your network"]), ("Setup mode", details),
        ("Setup mode", ["Joining", "Cafe"]), ("Wi-Fi", ["Connected to", "Cafe"])]
    assert all((s.get("title") or "").isascii() and all(line.isascii() for line in s.get("lines", []))
               for s in sent)


def test_with_unicode_the_names_and_the_readers_words_reach_the_avatar_unchanged():
    screen, sent = _screen(unicode=True)
    nm = FakeNM([(False, "the password did not work", "Дом"), (True, "", "Дом")])
    rc, seen = _run_setup(nm, screen=screen, ssid="Кухня", lang=RU, page_lang="ru",
                          post=[{"ssid": "Дом", "password": "wrongpass"},
                                {"ssid": "Дом", "password": "homepass1"}])
    assert rc == 0 and seen[1]["unicode"] is True
    title = RU.p("screen", "Setup mode")
    # The name as it is: no "(scan the code)" in its place, though the code is there.
    details = [f"{RU.p('screen', 'Network')}  Кухня", f"{RU.p('screen', 'Password')}  47190352",
               f"http://{wifi.AP_ADDRESS}"]
    assert _screens(sent) == [
        (title, details), (title, [RU.p("screen", "Joining"), "Дом"]),
        (title, [RU.p("screen", "Could not join"), "Дом"]), (title, details),
        (title, [RU.p("screen", "Joining"), "Дом"]),
        (RU.p("screen", "Wi-Fi"), [RU.p("screen", "Connected to"), "Дом"])]
    assert title == "Режим подключения" and details[0] == "Сеть  Кухня"   # translated
    assert sent[0]["qr"] == "WIFI:T:WPA;S:Кухня;P:47190352;;"


def test_the_screens_follow_the_voice_then_the_page_that_asked_for_the_join():
    """Before any phone has asked for anything, the voice's language is all there is;
    the screens after a join request are read by the phone's holder, in the language of
    the page they asked from (chosen there, or their phone's own)."""
    ja = i18n.get("ja")
    screen, sent = _screen(unicode=True)
    rc, _ = _run_setup(FakeNM([(True, "", "home")]), screen=screen, lang=RU, page_lang="ja",
                       post=[{"ssid": "home", "password": "homepass1"}])
    assert rc == 0
    shown = _screens(sent)
    # The words themselves, not just what the catalogs say: a catalog gone missing (the
    # English msgid back) fails here.
    assert shown[0][0] == "Режим подключения"
    assert shown[1] == ("セットアップモード", ["接続中", "home"])
    assert shown[2] == (ja.p("screen", "Wi-Fi"), ["接続済み", "home"])
    # Nothing handed over: English, as before.
    screen, sent = _screen(unicode=True)
    _run_setup(FakeNM([]), minutes=0.01, screen=screen)
    assert sent[0]["title"] == "Setup mode" and sent[0]["lines"][0] == "Network  teaport-9e35"


def test_the_password_line_fits_in_the_readers_words():
    ja = display.ScreenText(True, i18n.get("ja"))
    assert ws.password_line("47190352", ja) == "パスワード  47190352"
    assert ws.password_line("k7mq-x4tn-p9wr", ja) == "パス  k7mq-x4tn-p9wr"   # 2 columns a kana
    for lang in i18n.catalogs():
        s = display.ScreenText(True, i18n.get(lang))
        for pw in ("47190352", "k7mq-x4tn-p9wr"):
            assert display.columns(ws.password_line(pw, s)) <= ws.SCREEN_LINE_CHARS, (lang, pw)
    # English to an avatar without the fonts, whatever the reader's language.
    assert ws.password_line("47190352", display.ScreenText(False, RU)) == "Password  47190352"


def test_the_language_the_brain_hands_over_is_for_one_start_only():
    path = os.path.join(tempdir(), "state", ws.LANG_FILE)
    assert ws.handed_language(path).lang == "en"                  # none: English
    ws.hand_language("pt_BR", path)
    assert ws.handed_language(path).lang == "pt_BR"
    # Taken as it is read: the next start (the config page, a systemctl) has none.
    assert not os.path.exists(path) and ws.handed_language(path).lang == "en"
    ws.hand_language("xx", path)
    assert ws.handed_language(path).lang == "en"                  # no such catalog
    # A handover no start took in time (a start that failed) is not the next one's.
    ws.hand_language("ja", path)
    later = time.time() + ws.LANG_FRESH_SECS + 1
    assert ws.handed_language(path, now=lambda: later).lang == "en"
    assert os.listdir(os.path.dirname(path)) == []                # no temporary left either
    assert ws.lang_path().endswith(os.path.join(".local", "state", "teaport", ws.LANG_FILE))


def test_a_name_with_a_newline_is_one_line_on_any_screen():
    """A hidden network's name is typed on the page: a newline in it must not become a
    second line drawn over the next, where every script is drawn either."""
    screen, sent = _screen(unicode=True)
    rc, _ = _run_setup(FakeNM([(True, "", "Evil\nLine")]), screen=screen,
                       post=[{"ssid": "", "other": "Evil\nLine", "password": "homepass1"}])
    assert rc == 0
    assert _screens(sent)[-1] == ("Wi-Fi", ["Connected to", "Evil Line"])


def test_the_voice_hands_its_language_over_before_it_starts_the_unit(monkeypatch):
    monkeypatch.setattr(wv, "POLL_SECS", 0.02)

    async def run():
        v = _Voice(os.path.join(tempdir(), "s.json"), lang_fn=lambda: "es")
        calls = []
        v._hand_language = lambda lang: calls.append(("hand", lang))
        run_unit = v._run

        async def run_and_note(action, unit):
            calls.append((action, unit))
            return await run_unit(action, unit)

        v._run = run_and_note
        await v._heard("set up wifi")
        await v._heard("sí")
        await v._stop_following()
        return calls

    assert asyncio.run(run())[:2] == [("hand", "es"), ("restart", wv.UNIT)]


def test_a_name_the_screen_draws_is_pointed_at():
    """A "unicode" screen shows the name as it is: the voice points at it, as for ASCII."""
    st = {"ssid": "Кухня", "password": "47190352", "screen": True}
    assert "on my screen" not in wv.instructions(st, EN)      # folded to nothing: spelled
    assert "shown on my screen" in wv.instructions(dict(st, unicode=True), EN)


LOCAL_SAID = "go to teaport dash nine E three five dot local, and pick your Wi-Fi."
BARE_SAID = "go to one zero dot four two dot zero dot one, and pick your Wi-Fi."


def test_with_avahi_up_the_local_name_is_everywhere():
    screen, sent = _screen()
    avahi = FakeAvahi()
    _, seen = _run_setup(FakeNM([]), minutes=0.01, screen=screen, avahi=avahi)
    avahi.kill()
    assert sent[0]["lines"][2] == seen[1]["url"] == "http://teaport-9e35.local"
    assert wv.instructions(seen[1], EN, spelled=True).endswith(LOCAL_SAID)


def test_without_avahi_the_bare_address_is_everywhere():
    screen, sent = _screen()
    _, seen = _run_setup(FakeNM([]), minutes=0.01, screen=screen)        # not installed
    assert sent[0]["lines"][2] == seen[1]["url"] == f"http://{wifi.AP_ADDRESS}"
    said = wv.instructions(seen[1], EN, spelled=True)
    assert said.endswith(BARE_SAID) and "local" not in said


def test_avahi_dying_mid_setup_switches_the_next_show_to_the_bare_address():
    """The address is asked at every show: avahi gone between the first one and the
    re-up after a failed join, the screen, the status and the voice all move to the IP."""
    screen, sent = _screen()
    avahi = FakeAvahi()
    nm = FakeNM([(False, "the password did not work", "home"), (True, "", "home")])
    join = nm.join

    def join_and_lose_avahi(*a):
        avahi.kill()                       # the one running now dies...
        avahi.start = lambda setup, name: (setattr(setup, "avahi", None),
                                           setattr(setup, "avahi_name", name))  # ...for good
        return join(*a)
    nm.join = join_and_lose_avahi
    rc, seen = _run_setup(nm, screen=screen, avahi=avahi,
                          post=[{"ssid": "home", "password": "wrongpass"},
                                {"ssid": "home", "password": "homepass1"}])
    assert rc == 0
    ups = [s for s in seen if s["phase"] == "ap_up"]
    shown = [s["lines"][2] for s in sent if s.get("lines", [""])[0].startswith("Network")]
    assert ups[0]["url"] == shown[0] == "http://teaport-9e35.local"
    assert ups[1]["url"] == shown[-1] == f"http://{wifi.AP_ADDRESS}"
    assert wv.instructions(ups[0], EN, spelled=True).endswith(LOCAL_SAID)
    assert wv.instructions(ups[1], EN, spelled=True).endswith(BARE_SAID)


def test_an_avahi_publish_that_exits_at_once_is_not_shown(monkeypatch):
    """No avahi-daemon: avahi-publish exits within milliseconds, after Popen returns.
    A real process here (/bin/false), so the race is the real one."""
    setup = ws.Setup(FakeNM([]), _Recorded(tempdir()), port=0)
    monkeypatch.setattr(ws.shutil, "which", lambda name: "/bin/false")
    setup.publish("teaport-9e35")
    assert setup.page_url("teaport-9e35") == f"http://{wifi.AP_ADDRESS}"
    assert setup.avahi is None


# A name the screen draws as it is (a "unicode" avatar) is still not an address to type:
# the .local name is for an ASCII host name only (page_url).
@pytest.mark.parametrize("ssid", ["東京-box", "mybox", "teaport-東京", "teaport-my box"])
def test_a_folded_or_custom_name_gets_the_bare_address_even_with_avahi(ssid):
    screen, sent = _screen(unicode=True)
    avahi = FakeAvahi()
    _, seen = _run_setup(FakeNM([]), minutes=0.01, screen=screen, ssid=ssid, avahi=avahi)
    avahi.kill()
    assert sent[0]["lines"][2] == seen[1]["url"] == f"http://{wifi.AP_ADDRESS}"
    assert wv.instructions(seen[1], EN, spelled=True).endswith(BARE_SAID)


@pytest.mark.parametrize("qr_max", [0, 37])
def test_a_code_too_long_for_the_avatar_is_not_sent_nor_claimed(qr_max):
    # The join string here is 38 bytes: over the avatar's limit it would draw a code no
    # phone scans, or none at all, while the voice says to scan it.
    screen, sent = _screen(qr_max=qr_max)
    _, seen = _run_setup(FakeNM([]), minutes=0.01, screen=screen)
    assert seen[1]["screen"] is True and seen[1]["qr"] is False
    assert all("qr" not in s for s in sent)
    assert sent[0]["lines"][0] == "Network  teaport-9e35"


def test_sigterm_becomes_one_clean_stop():
    import signal
    old = signal.signal(signal.SIGTERM, ws._on_sigterm)
    try:
        with pytest.raises(ws.Stopped):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN   # a second one waits
    finally:
        signal.signal(signal.SIGTERM, old)


# ------------------------------------------------------------------ the page's server

def _setup_server():
    setup = ws.Setup(FakeNM([]), _Recorded(tempdir()), port=0, bind="127.0.0.1")
    setup.serve()
    return setup


def _raw(port, request):
    import socket
    with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
        c.sendall(request)
        return c.recv(200).decode(errors="replace")


def test_a_bad_content_length_is_refused_not_read():
    setup = _setup_server()
    try:
        for length, code in (("-1", "400"), ("abc", "400"), (str(ws.MAX_FORM_BYTES + 1), "413")):
            reply = _raw(setup.port, b"POST /connect HTTP/1.1\r\nHost: x\r\nContent-Length: "
                         + length.encode() + b"\r\n\r\n")
            assert reply.startswith(f"HTTP/1.0 {code}"), (length, reply)
        assert setup.request is None
    finally:
        setup.server.shutdown()


def test_a_slow_request_times_out_and_connections_are_capped(monkeypatch):
    import socket
    monkeypatch.setattr(ws, "REQUEST_TIMEOUT_SECS", 0.3)
    monkeypatch.setattr(ws, "MAX_CLIENTS", 2)
    setup = _setup_server()
    try:
        idle = [socket.create_connection(("127.0.0.1", setup.port)) for _ in range(2)]
        time.sleep(0.1)
        with socket.create_connection(("127.0.0.1", setup.port), timeout=5) as third:
            assert third.recv(10) == b""          # full: dropped at once
        time.sleep(0.5)                           # the idle two time out and free their slots
        assert _raw(setup.port, b"GET / HTTP/1.0\r\n\r\n").startswith("HTTP/1.0 200")
        for c in idle:
            c.close()
    finally:
        setup.server.shutdown()


def test_the_page_is_bound_to_the_setup_network_only():
    setup = ws.Setup(FakeNM([]), _Recorded(tempdir()), port=0)
    assert setup.bind is None and setup.address == wifi.AP_ADDRESS   # never 0.0.0.0
    setup.serve()   # IP_FREEBIND: bound although this machine has no such address
    try:
        assert setup.server.server_address[0] == wifi.AP_ADDRESS
    finally:
        setup.server.shutdown()


def _bound(nm):
    setup = ws.Setup(nm, _Recorded(tempdir()), port=0, minutes=0.005)
    _no_avahi(setup)
    assert setup.run("teaport-9e35", "47190352") == 2      # nobody came: timeout
    return setup.server.server_address[0], setup.status.history[1]["address"]


def test_the_page_binds_the_pinned_address_while_nm_still_lists_the_old_one(capsys):
    # 2026-10-07: just after the setup network came up NM still listed the home
    # address first; the page bound that, and no phone on the setup network reached it.
    nm = FakeNM([], addresses=[["192.168.1.105"], ["192.168.1.105"], [wifi.AP_ADDRESS]])
    assert _bound(nm) == (wifi.AP_ADDRESS, wifi.AP_ADDRESS)
    assert "phones may not reach" not in capsys.readouterr().out


def test_an_address_that_never_arrives_is_logged_and_the_page_still_bound(monkeypatch, capsys):
    monkeypatch.setattr(ws, "ADDRESS_WAIT_SECS", 0.3)
    assert _bound(FakeNM([], addresses=[["192.168.1.105"]])) == (wifi.AP_ADDRESS, wifi.AP_ADDRESS)
    assert "10.42.0.1 is not on wlan0" in capsys.readouterr().out


def test_names_are_isolated_whole_and_once():
    out = ws._isolate("Could not join R&D: the password did not work. Try again.", ["R&D", "amp", "a", "bdi"])
    assert out == "Could not join <bdi>R&amp;D</bdi>: the password did not work. Try again."
    assert ws._isolate("homeに接続できません", ["home"]) == "<bdi>home</bdi>に接続できません"
    assert ws._isolate("<x> & a", ["<x>"]) == "<bdi>&lt;x&gt;</bdi> &amp; a"


# ------------------------------------------------------------------ the voice half

EN = i18n.get("en")


def test_the_details_are_spelled_for_the_voice():
    assert wv.spell("teaport-9e35", EN) == "teaport, dash, nine, E, three, five"
    assert wv.spell("47190352", EN) == "four, seven, one, nine, zero, three, five, two"
    assert wv.spell("Tito2017", EN) == "capital T, I, T, O, two, zero, one, seven"
    text = wv.instructions({"ssid": "teaport-9e35", "password": "47190352"}, EN)
    assert "teaport, dash, nine, E, three, five" in text
    assert "one zero dot four two dot zero dot one" in text
    assert "screen" not in text                      # no display: not mentioned


SPELLED = ("On your phone, join the Wi-Fi network teaport, dash, nine, E, three, five. "
           "The password is four, seven, one, nine, zero, three, five, two. Then open your "
           "browser, go to one zero dot four two dot zero dot one, and pick your Wi-Fi.")


def test_the_voice_follows_what_the_user_does_screen_first():
    """In the order the user acts: the code (or the screen's network and password), then
    the screen's address in the browser, then their network. The screen always shows the
    address (http://10.42.0.1), so "the address on my screen" holds in both screen
    branches. Nothing counts on the page opening by itself."""
    st = {"ssid": "teaport-9e35", "password": "47190352"}
    qr = wv.instructions(dict(st, screen=True, qr=True), EN)
    assert qr == ("Scan the code on my screen with your phone's camera to join my setup "
                  "network. Then, in your phone's browser, go to the address on my screen "
                  "and pick your Wi-Fi. Say repeat if you'd like me to read the details.")
    text = wv.instructions(dict(st, screen=True), EN)
    assert text == ("On your phone, join the Wi-Fi network shown on my screen, using the "
                    "password shown there. Then, in your phone's browser, go to the address "
                    "on my screen and pick your Wi-Fi. Say repeat if you'd like me to read "
                    "the details.")
    # No screen, or a repeat: spelled, network, password, address.
    assert wv.instructions(dict(st, screen=False), EN) == SPELLED
    for extra in ({"screen": True}, {"screen": True, "qr": True}):
        assert wv.instructions(dict(st, **extra), EN, spelled=True) == SPELLED
    assert all("by itself" not in s and "should open" not in s for s in (qr, text, SPELLED))


def test_a_name_the_screen_folds_is_spelled_without_a_code():
    """Without a code the screen shows the name as the font folds it (Cafe for Café),
    which would join nothing: the voice spells the real one."""
    st = {"ssid": "Café-box", "password": "47190352", "screen": True}
    assert wv.instructions(st, EN).startswith("On your phone, join the Wi-Fi network capital C, A, F, é")
    assert "on my screen" not in wv.instructions(st, EN)
    assert "Scan the code" in wv.instructions(dict(st, qr=True), EN)   # the code has it


@pytest.mark.parametrize("text,hit", [
    ("set up wifi", True), ("Can you set up the Wi-Fi?", True), ("connect to wi-fi please", True),
    ("switch to a different wifi", True), ("wifi setup", True), ("change my wifi", True),
    ("let's try setting up wifi again", True), ("setting up the wifi", True),     # #96
    ("connecting to wifi", True), ("switching the wifi to my hotspot", True),
    ("connect me to wi-fi", True),
    ("I love my wifi", False), ("the wifi is slow here", False), ("what's the weather", False)])
def test_the_phrase(text, hit):
    assert wv.heard("start", text, EN) is hit


class _Voice(wv.WifiSetupVoice):
    """The processor without a pipeline: speech is collected, tasks are asyncio's."""

    def __init__(self, status_path, results=None, lang_fn=lambda: "en-us"):
        self.calls, self.said, self.handed = [], [], []
        results = results or {}

        async def run(action, unit):
            self.calls.append((action, unit))
            return results.get(action, (0, ""))

        async def offline():
            return False
        super().__init__(run=run, status_path=status_path, online=offline, lang_fn=lang_fn,
                         hand_language=self.handed.append)

    async def say(self, text):
        self.said.append(text)

    def _spawn(self, coro):
        return asyncio.get_running_loop().create_task(coro)


def _write(path, age=0.0, **status):
    with open(path, "w") as f:
        json.dump({"time": time.time() - age, "run_id": "t", **status}, f)


def test_the_voice_flow_confirms_starts_speaks_and_finishes(monkeypatch):
    monkeypatch.setattr(wv, "POLL_SECS", 0.02)
    path = os.path.join(tempdir(), "status.json")
    _write(path, age=60, phase="connected", target="stale")   # an earlier run's file

    async def run():
        v = _Voice(path)
        assert await v._heard("hey can you set up the wifi") and v.state == "confirm"
        assert await v._heard("yes please") and v.state == "running"
        assert v.calls == [("restart", wv.UNIT)]
        await asyncio.sleep(0.1)
        assert not any("connected" in s for s in v.said)   # the stale file is ignored
        _write(path, phase="ap_up", ssid="teaport-9e35", password="47190352", url="", error="")
        await asyncio.sleep(0.1)
        assert await v._heard("what was the password")      # repeat: the details again
        assert await v._heard("tell me a joke")             # not for the LLM while running
        _write(path, phase="joining", target="home")
        await asyncio.sleep(0.1)
        _write(path, phase="connected", target="home")
        await asyncio.sleep(0.1)
        assert v.state == "idle"
        assert not await v._heard("tell me a joke")         # the LLM has it again
        return v.said

    said = asyncio.run(run())
    assert said[0].startswith("Do you want to set up Wi-Fi?")
    assert sum("four, seven, one, nine" in s for s in said) == 2   # spoken, then repeated
    # The first time names the state the box is in; a repeat is the details alone.
    spoken = [s for s in said if "four, seven, one, nine" in s]
    assert spoken[0].startswith("I'm in setup mode. On your phone, join")
    assert spoken[1].startswith("On your phone, join")
    assert any("I'm in setup mode. Say repeat" in s for s in said)
    assert any("joining home" in s for s in said) and "online again" in said[-1]


def test_no_and_cancel_and_mumbling(monkeypatch):
    monkeypatch.setattr(wv, "POLL_SECS", 0.02)
    path = os.path.join(tempdir(), "status.json")

    async def run():
        v = _Voice(path)
        await v._heard("set up wifi")
        assert await v._heard("no thanks") and v.state == "idle"
        await v._heard("set up wifi")
        assert await v._heard("hmm") and v.state == "confirm"
        assert not await v._heard("what time is it") and v.state == "idle"  # two unclear: let go
        await v._heard("set up wifi")
        await v._heard("yes")
        assert await v._heard("cancel that") and v.state == "idle"
        return v.calls, v.said

    calls, said = asyncio.run(run())
    assert calls == [("restart", wv.UNIT), ("stop", wv.UNIT)]
    # Cancelled before the setup network was up: never in setup mode, so not "left".
    assert "never mind" in said[1] and said[-1] == "Okay, I've cancelled that. Everything is as it was."


def test_a_unit_that_will_not_start_is_said_not_hidden():
    async def run():
        v = _Voice(os.path.join(tempdir(), "s.json"), results={"restart": (1, "denied")})
        await v._heard("set up wifi")
        await v._heard("yes")
        return v.state, v.said[-1]

    state, last = asyncio.run(run())
    assert state == "idle" and "couldn't go into setup mode" in last


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
