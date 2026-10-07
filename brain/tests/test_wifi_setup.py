"""Wi-Fi setup: the setup service (wifi_setup.py — name, password, nmcli parsing, the
page, the join/rollback/timeout flow against a fake NetworkManager and a real HTTP
server) and its voice half (wifi_voice.py — the phrase, confirming, speaking the
details digit by digit, following status.json, repeat and cancel). No radio needed."""
import asyncio
import json
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import pytest

from teaport_brain import wifi_setup as ws
from teaport_brain import wifi_voice as wv


# ------------------------------------------------------------------ identity

def test_ssid_from_the_mac_and_an_override():
    assert ws.setup_ssid("f8:3d:c6:1f:9e:35") == "teaport-9e35"
    assert ws.setup_ssid("F8:3D:C6:1F:9E:35") == "teaport-9e35"
    assert ws.setup_ssid("") == "teaport-0000"
    assert ws.setup_ssid("f8:3d:c6:1f:9e:35", "Kitchen Teaport") == "Kitchen Teaport"


def test_the_flash_time_password_wins_when_valid():
    assert ws.setup_password("47190352") == "47190352"
    assert ws.setup_password(" Tito2017 ") == "Tito2017"
    for bad in ("", "1234567", "x" * 64, "pässwörd1"):
        pw = ws.setup_password(bad)
        assert pw.isdigit() and len(pw) == ws.PASSWORD_DIGITS, bad
    assert len({ws.setup_password("") for _ in range(20)}) > 1  # fresh each time


# ------------------------------------------------------------------ nmcli output

def test_terse_lines_unescape_colons_and_backslashes():
    assert ws.split_terse(r"Stefano’s iPhone\: 13:75:WPA2") == ["Stefano’s iPhone: 13", "75", "WPA2"]
    assert ws.split_terse(r"a\\b:1:") == ["a\\b", "1", ""]


def test_scan_keeps_each_named_network_once_strongest_first():
    nets = ws.parse_scan("home:50:WPA2\ncafe:70:\nhome:80:WPA2 WPA3\n:90:WPA2\nx:bad:WPA2\n")
    assert [n["ssid"] for n in nets] == ["home", "cafe", "x"]
    assert nets[0] == {"ssid": "home", "signal": 80, "open": False, "security": "WPA2 WPA3"}
    assert nets[1]["open"] and nets[1]["security"] == "open"


# ------------------------------------------------------------------ the page

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


def test_join_uses_a_saved_profile_without_a_password():
    run = FakeRun([(("-t", "-f", "NAME,TYPE"), (0, "home:802-11-wireless\n", "")),
                   (("-t", "-g", "802-11-wireless.ssid"), (0, "home\n", ""))])
    assert ws.NM(run).join("wlan0", "home", "", False) == (True, "", None)
    assert ["nmcli", "--wait", "45", "connection", "up", "id", "home"] in run.calls


def test_a_wrong_new_password_on_a_saved_profile_puts_the_old_one_back():
    run = FakeRun([(("-t", "-f", "NAME,TYPE"), (0, "home:802-11-wireless\n", "")),
                   (("-t", "-g", "802-11-wireless.ssid"), (0, "home\n", "")),
                   (("-s", "-t", "-g"), (0, "oldpass99\n", "")),
                   (("--wait", "45", "connection", "up"), (4, "", "Secrets were required"))])
    ok, why, created = ws.NM(run).join("wlan0", "home", "newpass99", False)
    assert (ok, created) == (False, None) and "password" in why
    modifies = [c for c in run.calls if c[1:3] == ["connection", "modify"]]
    assert modifies[0][-1] == "newpass99" and modifies[-1][-1] == "oldpass99"


def test_a_new_network_is_created_and_reported_for_cleanup():
    run = FakeRun([(("--wait", "45", "device", "wifi", "connect"),
                    (10, "", "Error: Connection activation failed: Secrets were required"))])
    ok, why, created = ws.NM(run).join("wlan0", "cafe", "badpass1", False)
    assert (ok, created) == (False, "cafe") and why == "the password did not work"
    run2 = FakeRun([(("--wait", "45", "device", "wifi", "connect"), (10, "", "Error: No network with SSID 'x' found."))])
    assert ws.NM(run2).join("wlan0", "x", "", True)[1] == "the network was not found"
    connect = [c for c in run2.calls if "connect" in c][0]
    assert connect[-2:] == ["hidden", "yes"]


# ------------------------------------------------------------------ the whole run

class FakeNM:
    """NetworkManager as a state: the AP, the joins, the connectivity."""

    def __init__(self, join_results, online=True, previous="old-wifi"):
        self.join_results, self._online, self.previous = list(join_results), online, previous
        self.events = []

    def wifi_device(self): return "wlan0"
    def mac(self, dev): return "f8:3d:c6:1f:9e:35"
    def scan(self, dev): return [{"ssid": "home", "signal": 80, "open": False, "security": "WPA2"}]
    def active_wifi(self, dev): return self.previous
    def ap_up(self, dev, ssid, pw): self.events.append(("ap_up", ssid)); return True, ""
    def ap_down(self): self.events.append(("ap_down",))
    def ap_delete(self): self.events.append(("ap_delete",))
    def up(self, profile): self.events.append(("up", profile)); return True
    def delete(self, profile): self.events.append(("delete", profile))
    def online(self): return self._online

    def join(self, dev, ssid, pw, hidden):
        self.events.append(("join", ssid, pw, hidden))
        return self.join_results.pop(0)


class _Recorded(ws.Status):
    """status.json as written, plus every phase in order (the fake NM is instant, so a
    phase can be gone before anyone reads the file)."""

    def __init__(self, directory):
        super().__init__(directory, "test")
        self.history = []

    def set(self, phase, **fields):
        super().set(phase, **fields)
        self.history.append(dict(self.data))


def _run_setup(nm, minutes=1.0, post=None):
    """Run a Setup in a thread on a free port; post(form) each time the AP comes up."""
    status = _Recorded(tempfile.mkdtemp())
    setup = ws.Setup(nm, status, port=0, minutes=minutes)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("rc", setup.run("teaport-9e35", "47190352")))
    t.start()
    posted, deadline = 0, time.time() + 15
    while t.is_alive() and time.time() < deadline:
        time.sleep(0.02)
        ups = sum(1 for s in list(status.history) if s["phase"] == "ap_up")
        if post and posted < len(post) and ups > posted:
            page = urllib.request.urlopen(f"http://127.0.0.1:{setup.port}/hotspot-detect.html").read().decode()
            assert 'value="home"' in page and "teaport-9e35" not in page
            body = urllib.parse.urlencode(post[posted]).encode()
            reply = urllib.request.urlopen(f"http://127.0.0.1:{setup.port}/connect", body).read().decode()
            assert "Joining" in reply
            posted += 1
    t.join(5)
    return result.get("rc"), status.history


def test_a_good_join_ends_connected():
    nm = FakeNM([(True, "", "home")])
    rc, seen = _run_setup(nm, post=[{"ssid": "home", "password": "homepass1"}])
    assert rc == 0
    assert [s["phase"] for s in seen] == ["scanning", "ap_up", "joining", "connected"]
    assert seen[1]["password"] == "47190352" and seen[1]["url"] == "http://teaport-9e35.local"
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


def test_joined_without_internet_counts_as_failed():
    nm = FakeNM([(True, "", "home")], online=False)
    rc, seen = _run_setup(nm, minutes=0.02, post=[{"ssid": "home", "password": "homepass1"}])
    assert any(s["phase"] == "failed" and "no internet" in s["reason"] for s in seen)


def test_nobody_comes_and_the_previous_connection_is_put_back():
    nm = FakeNM([])
    rc, seen = _run_setup(nm, minutes=0.01)
    assert rc == 2 and seen[-1]["phase"] == "timeout"
    assert ("up", "old-wifi") in nm.events and nm.events[-1] == ("ap_delete",)


# ------------------------------------------------------------------ the voice half

def test_the_details_are_spelled_for_the_voice():
    assert wv.spell("teaport-9e35") == "teaport, dash, nine, E, three, five"
    assert wv.spell("47190352") == "four, seven, one, nine, zero, three, five, two"
    assert wv.spell("Tito2017") == "capital T, I, T, O, two, zero, one, seven"
    text = wv.instructions({"ssid": "teaport-9e35", "password": "47190352"})
    assert "teaport, dash, nine, E, three, five" in text and "teaport dash nine E three five dot local" in text


@pytest.mark.parametrize("text,hit", [
    ("set up wifi", True), ("Can you set up the Wi-Fi?", True), ("connect to wi-fi please", True),
    ("switch to a different wifi", True), ("wifi setup", True), ("change my wifi", True),
    ("I love my wifi", False), ("the wifi is slow here", False), ("what's the weather", False)])
def test_the_phrase(text, hit):
    assert bool(wv.TRIGGER.search(text)) is hit


class _Voice(wv.WifiSetupVoice):
    """The processor without a pipeline: speech is collected, tasks are asyncio's."""

    def __init__(self, status_path, results=None):
        self.calls, self.said = [], []
        results = results or {}

        async def run(action, unit):
            self.calls.append((action, unit))
            return results.get(action, (0, ""))
        super().__init__(run=run, status_path=status_path)

    async def say(self, text):
        self.said.append(text)

    def _spawn(self, coro):
        return asyncio.get_running_loop().create_task(coro)


def _write(path, age=0.0, **status):
    with open(path, "w") as f:
        json.dump({"time": time.time() - age, "run_id": "t", **status}, f)


def test_the_voice_flow_confirms_starts_speaks_and_finishes(monkeypatch):
    monkeypatch.setattr(wv, "POLL_SECS", 0.02)
    path = os.path.join(tempfile.mkdtemp(), "status.json")
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
    assert any("I'm in Wi-Fi setup" in s for s in said)
    assert any("joining home" in s for s in said) and "online again" in said[-1]


def test_no_and_cancel_and_mumbling(monkeypatch):
    monkeypatch.setattr(wv, "POLL_SECS", 0.02)
    path = os.path.join(tempfile.mkdtemp(), "status.json")

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
    assert "never mind" in said[1] and "stopped Wi-Fi setup" in said[-1]


def test_a_unit_that_will_not_start_is_said_not_hidden():
    async def run():
        v = _Voice(os.path.join(tempfile.mkdtemp(), "s.json"), results={"restart": (1, "denied")})
        await v._heard("set up wifi")
        await v._heard("yes")
        return v.state, v.said[-1]

    state, last = asyncio.run(run())
    assert state == "idle" and "couldn't start" in last
