#
# The Wi-Fi helper (wifi_apply.py) is the run user's only way to change NetworkManager:
# the user is the agent's, so what it takes must be Wi-Fi setup's requests and nothing
# else. What has to hold:
#   - a request that is not one of those, or not well-formed, is refused before nmcli runs:
#     a device that is not Wi-Fi, a name or password Wi-Fi does not take, the setup
#     network while setup is not running, any profile but a saved Wi-Fi client one to
#     bring up, any profile it did not create to delete;
#   - a password goes on stdin, never on a command line;
#   - a stop in the middle of a join puts back what the join changed;
#   - the polkit rule grants a scan and nothing else, the package ships the helper, and
#     sudoers-sync writes its line and narrows a wider rule an earlier install left, only
#     once the line is in;
#   - the setup unit can use sudo (no NoNewPrivileges=), and an old one gets a drop-in.
#
# Run: python test_wifi_apply.py   (or via pytest test_suite.py)
#
import io
import json
import os
import pathlib
import re
import subprocess

import pytest

from teaport_brain import wifi, wifi_apply
from test_wifi_setup import FakeNmcli

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _handle(nm, verb, req, created=None, setup_running=True):
    return wifi_apply.handle(wifi_apply.Ops(nm), verb, req, created=created or wifi_apply.Created(),
                             setup_running=lambda: setup_running)


def _changes(nm):
    """The nmcli calls that change something."""
    return [c for c in nm.calls if any(w in c for w in ("add", "modify", "up", "down", "delete", "connect"))]


@pytest.mark.parametrize("verb, req, why", [
    ("bogus", {}, "unknown request"),
    ("join", ["not", "an", "object"], "JSON object"),
    ("join", {"dev": "eth0", "ssid": "home", "password": "homepass1"}, "not a Wi-Fi device"),
    ("join", {"dev": "wlan0", "ssid": "x" * 33, "password": "homepass1"}, "at most 32 bytes"),
    ("join", {"dev": "wlan0", "ssid": "", "password": "homepass1"}, "ssid must be"),
    ("join", {"dev": "wlan0", "ssid": "home", "password": "short"}, "8 to 63"),
    ("join", {"dev": "wlan0", "ssid": "home", "password": "pässwörd1"}, "8 to 63"),
    ("join", {"dev": "wlan0", "ssid": "home", "password": "g" * 64}, "8 to 63"),
    ("join", {"dev": "wlan0", "ssid": "home", "password": 12345678}, "must be a string"),
    ("join", {"dev": "wlan0", "ssid": "home", "password": "homepass1", "hidden": "yes"}, "true or false"),
    ("ap-up", {"dev": "wlan0", "ssid": "teaport-ab12"}, "needs a password"),
    ("up", {"uuid": "u1", "wait": 9999}, "wait must be"),
    ("up", {"uuid": "u1", "wait": True}, "wait must be"),
])
def test_a_request_wifi_setup_does_not_make_is_refused_before_nmcli_changes_anything(verb, req, why):
    nm = FakeNmcli({"home": "homepass1"}, profiles=[{"ssid": "home", "psk": "homepass1", "uuid": "u1"}])
    with pytest.raises(wifi_apply.Refused, match=why):
        _handle(nm, verb, req)
    assert not _changes(nm), nm.calls


def test_the_setup_network_only_while_setup_runs():
    nm = FakeNmcli({})
    req = {"dev": "wlan0", "ssid": "teaport-ab12", "password": "k7mq-x4tn-p9wr"}
    with pytest.raises(wifi_apply.Refused, match="not running"):
        _handle(nm, "ap-up", req, setup_running=False)
    assert not _changes(nm)
    _handle(nm, "ap-up", req)
    add = next(c for c in nm.calls if c[1:3] == ["connection", "add"])
    # Always our own profile: a WPA2 access point on the pinned address, never autoconnected.
    assert add[add.index("con-name") + 1] == wifi_apply.AP_CONNECTION
    assert add[add.index("802-11-wireless.mode") + 1] == "ap" and add[add.index("autoconnect") + 1] == "no"
    assert add[add.index("ipv4.addresses") + 1] == "10.42.0.1/24"


def test_only_a_saved_wifi_client_profile_is_brought_up():
    nm = FakeNmcli({"home": "homepass1"}, profiles=[
        {"ssid": "home", "psk": "homepass1", "uuid": "u1"},
        {"ssid": "hotspot", "mode": "ap", "uuid": "u-ap"},
        {"id": "teaport-setup", "ssid": "teaport-ab12", "uuid": "u-setup"}])
    assert _handle(nm, "up", {"uuid": "u1", "wait": 0}) == {"ok": True}
    for uuid in ("u-ap", "u-setup", "eth-uuid", "-s"):
        with pytest.raises(wifi_apply.Refused, match="not a saved Wi-Fi network"):
            _handle(nm, "up", {"uuid": uuid})


def test_only_a_profile_the_helper_created_is_deleted():
    nm = FakeNmcli({"cafe": "cafepass1"}, profiles=[{"ssid": "home", "psk": "homepass1", "uuid": "u-home"}])
    created = wifi_apply.Created()
    with pytest.raises(wifi_apply.Refused, match="not a profile this helper created"):
        _handle(nm, "delete", {"uuid": "u-home"}, created)
    assert "u-home" in nm.profiles
    r = _handle(nm, "join", {"dev": "wlan0", "ssid": "cafe", "password": "wrongpass"}, created)
    assert r["ok"] is False and r["why"] == wifi_apply.BAD_PASSWORD and r["created"] in nm.profiles
    _handle(nm, "delete", {"uuid": r["created"]}, created)
    assert r["created"] not in nm.profiles
    with pytest.raises(wifi_apply.Refused):   # once
        _handle(nm, "delete", {"uuid": r["created"]}, created)


def test_the_created_list_is_roots_alone(tmp_path):
    path = tmp_path / "run/created"
    c = wifi_apply.Created(str(path))
    c.add("u-1")
    c.add("u-2")
    c.discard("u-1")
    assert "u-2" in wifi_apply.Created(str(path)) and "u-1" not in wifi_apply.Created(str(path))
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)
    assert oct(path.parent.stat().st_mode & 0o777) == oct(0o700)
    path.write_text("not json")
    assert "u-2" not in wifi_apply.Created(str(path))   # unreadable: nothing may be deleted


def test_main_refuses_without_root_and_reads_its_request_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr(wifi_apply.os, "geteuid", lambda: 1000)
    assert wifi_apply.main(["connectivity"]) == 2
    assert "root" in json.loads(capsys.readouterr().out)["refused"]
    monkeypatch.setattr(wifi_apply.os, "geteuid", lambda: 0)
    for stdin, why in (("x" * (wifi_apply.MAX_REQUEST + 1), "too long"), ("{nope", "not JSON")):
        monkeypatch.setattr(wifi_apply.sys, "stdin", io.StringIO(stdin))
        assert wifi_apply.main(["join"]) == 2
        assert why in json.loads(capsys.readouterr().out)["refused"]
    assert wifi_apply.main(["join", "extra"]) == 2
    assert "usage" in json.loads(capsys.readouterr().out)["refused"]


def test_a_stop_in_the_middle_of_a_join_puts_the_old_password_back(monkeypatch, capsys):
    nm = FakeNmcli({}, profiles=[{"ssid": "home", "psk": "old:pass1", "uuid": "u1"}])
    monkeypatch.setattr(wifi_apply.os, "geteuid", lambda: 0)
    monkeypatch.setattr(wifi_apply.sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(wifi_apply, "Ops", lambda: _Ops(nm))
    monkeypatch.setattr(wifi_apply.signal, "signal", lambda *a: None)
    monkeypatch.setattr(wifi_apply, "_lock", lambda: 0)

    def stopped_mid_join(ops, verb, req, **kw):
        ops._pending = ("psk", "u1", "0", "old:pass1")
        nm.profiles["u1"]["psk"] = "half-done"
        raise wifi_apply._Stopped
    monkeypatch.setattr(wifi_apply, "handle", stopped_mid_join)
    assert wifi_apply.main(["join"]) == 3
    assert nm.profiles["u1"]["psk"] == "old:pass1"
    assert json.loads(capsys.readouterr().out)["stopped"] is True


_Ops = wifi_apply.Ops


def test_a_stop_while_the_old_password_is_being_put_back_still_puts_it_back():
    # The restore itself cut short (the run user can SIGTERM its own sudo): it stays to do.
    nm = FakeNmcli({}, profiles=[{"ssid": "home", "psk": "old:pass1", "uuid": "u1"}])
    ops = wifi_apply.Ops(nm)
    ops._pending = ("psk", "u1", "0", "old:pass1")
    nm.profiles["u1"]["psk"] = "wrongpass1"
    real = ops.nmcli

    def stopped_mid_modify(*args, **kw):
        if "modify" in args:
            raise wifi_apply._Stopped
        return real(*args, **kw)
    ops.nmcli = stopped_mid_modify
    with pytest.raises(wifi_apply._Stopped):
        ops.undo()
    assert ops._pending is not None   # not cleared before it happened
    ops.nmcli = real
    ops.undo()
    assert nm.profiles["u1"]["psk"] == "old:pass1" and ops._pending is None


def test_a_stopped_request_leaves_no_profile_it_made(monkeypatch, capsys, tmp_path):
    nm = FakeNmcli({"cafe": "cafepass1"})
    monkeypatch.setattr(wifi_apply.os, "geteuid", lambda: 0)
    monkeypatch.setattr(wifi_apply.sys, "stdin", io.StringIO(json.dumps(
        {"dev": "wlan0", "ssid": "cafe", "password": "cafepass1"})))
    monkeypatch.setattr(wifi_apply, "Ops", lambda: _Ops(nm))
    monkeypatch.setattr(wifi_apply.signal, "signal", lambda *a: None)
    monkeypatch.setattr(wifi_apply, "_lock", lambda: 0)
    monkeypatch.setattr(wifi_apply, "CREATED_PATH", str(tmp_path / "created"))
    real_handle = wifi_apply.handle

    def stopped_after_the_join(*a, **kw):   # the stop lands before the reply goes out
        real_handle(*a, **kw)
        raise wifi_apply._Stopped
    monkeypatch.setattr(wifi_apply, "handle", stopped_after_the_join)
    assert wifi_apply.main(["join"]) == 3
    assert nm.profiles == {}, nm.profiles          # the caller never learned its UUID
    assert wifi_apply.Created(str(tmp_path / "created"))._load() == set()
    assert json.loads(capsys.readouterr().out)["stopped"] is True


def test_the_caller_waits_for_the_lock_and_a_whole_join():
    # A caller that gave up first would leave a join running that it never hears of.
    assert wifi.HELPER_SECS >= wifi_apply.LOCK_WAIT_SECS + wifi_apply.JOIN_SECS + 30


def test_one_change_at_a_time(tmp_path):
    # Two joins at once would each take the other's half-done password for the old one.
    path = str(tmp_path / "run/lock")
    held = wifi_apply._lock(path)
    try:
        with pytest.raises(wifi_apply._Busy):
            wifi_apply._lock(path, wait=0.3)
    finally:
        os.close(held)
    os.close(wifi_apply._lock(path, wait=0.3))   # free again once the first is done
    assert oct(os.stat(path).st_mode & 0o777) == oct(0o600)
    assert oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == oct(0o700)


def test_main_takes_the_lock_for_every_change_and_says_busy(monkeypatch, capsys):
    monkeypatch.setattr(wifi_apply.os, "geteuid", lambda: 0)
    monkeypatch.setattr(wifi_apply.signal, "signal", lambda *a: None)
    taken = []

    def busy():
        taken.append(1)
        raise wifi_apply._Busy
    monkeypatch.setattr(wifi_apply, "_lock", busy)
    for verb in ("join", "ap-up", "ap-down", "up", "delete", "keep"):
        monkeypatch.setattr(wifi_apply.sys, "stdin", io.StringIO("{}"))
        assert wifi_apply.main([verb]) == 2
        assert "another Wi-Fi change" in json.loads(capsys.readouterr().out)["refused"]
    assert len(taken) == 6


def test_a_kept_network_can_no_longer_be_deleted():
    nm = FakeNmcli({"cafe": "cafepass1"})
    created = wifi_apply.Created()
    r = _handle(nm, "join", {"dev": "wlan0", "ssid": "cafe", "password": "cafepass1"}, created)
    assert r["ok"] and r["created"]
    _handle(nm, "keep", {"uuid": r["created"]}, created)
    with pytest.raises(wifi_apply.Refused, match="not a profile this helper created"):
        _handle(nm, "delete", {"uuid": r["created"]}, created)
    assert r["created"] in nm.profiles


def _fake_sudo(tmp_path, reply, rc=0):
    tool = tmp_path / "sudo"
    tool.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" > {tmp_path}/argv\ncat > {tmp_path}/stdin\n'
                    f"printf '%s\\n' '{reply}'\nexit {rc}\n")
    tool.chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}


def test_through_sudo_a_password_goes_on_stdin_never_on_the_command_line(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", _fake_sudo(tmp_path, '{"ok": false, "why": "password", "created": "u-9"}')["PATH"])
    nm = wifi.NM()   # no run given: the changes go through sudo
    ok, why, created = nm.join("wlan0", "cafe", "s3cret-pass", False)
    assert (ok, why, created) == (False, "the password did not work", "u-9")
    argv = (tmp_path / "argv").read_text().split()
    assert argv == ["-n", "/usr/bin/python3", "/usr/lib/teaport/wifi_apply.py", "join"], argv
    assert json.loads((tmp_path / "stdin").read_text()) == {
        "dev": "wlan0", "ssid": "cafe", "password": "s3cret-pass", "hidden": False}
    # A refusal, or a helper that could not run, is a failure with its reason, never ok.
    monkeypatch.setenv("PATH", _fake_sudo(tmp_path, '{"refused": "not a Wi-Fi device"}', rc=2)["PATH"])
    assert wifi.NM().ap_up("eth0", "x", "k7mq-x4tn-p9wr") == (False, "not a Wi-Fi device")
    monkeypatch.setenv("PATH", _fake_sudo(tmp_path, "sudo: a password is required", rc=1)["PATH"])
    assert wifi.NM().up("u1") is False
    ok, why, created = wifi.NM().join("wlan0", "cafe", "s3cret-pass", False)
    assert not ok and why == "it would not connect" and created is None


def test_the_polkit_rule_grants_a_scan_and_nothing_else():
    rule = (ROOT / "packaging/wifi-setup/50-teaport-wifi-setup.rules.in").read_text()
    code = "\n".join(ln for ln in rule.splitlines() if not ln.lstrip().startswith("//"))
    assert set(re.findall(r'"(org\.freedesktop\.[\w.-]+)"', code)) == {"org.freedesktop.NetworkManager.wifi.scan"}
    assert 'subject.user == "@USER@"' in code


def test_the_package_ships_the_helper_and_the_setup_unit_can_reach_it():
    nfpm = (ROOT / "packaging/nfpm.yaml").read_text()
    assert ("  - src: brain/teaport_brain/wifi_apply.py\n    dst: /usr/lib/teaport/wifi_apply.py\n") in nfpm
    assert ("  - src: packaging/wifi-setup/50-teaport-wifi-setup.rules.in\n"
            "    dst: /usr/lib/teaport/50-teaport-wifi-setup.rules.in\n") in nfpm
    assert wifi.HELPER == "/usr/lib/teaport/wifi_apply.py" and wifi.SYSTEM_PYTHON == "/usr/bin/python3"
    unit = (ROOT / "systemd/teaport-wifi-setup.service.in").read_text()
    assert not re.search(r"^NoNewPrivileges=", unit, re.M), "sudo cannot run under NoNewPrivileges="
    # Standard library only: it runs on the system python, outside the venv.
    imports = set(re.findall(r"^(?:import|from) (\w+)", (ROOT / "brain/teaport_brain/wifi_apply.py").read_text(), re.M))
    assert imports <= {"fcntl", "json", "os", "re", "signal", "subprocess", "sys", "time"}, imports


def _sudoers_sync(tmp_path, rules_text=None, visudo_ok=True, update_rules_writable=True):
    """sudoers-sync with its paths under tmp_path; visudo is a stub."""
    text = (ROOT / "packaging/deb/sudoers-sync").read_text()
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    for name in ("config_apply.py", "wifi_apply.py", "login_apply.py"):
        (lib / name).write_text("#\n")
    (lib / "50-teaport-wifi-setup.rules.in").write_text(
        (ROOT / "packaging/wifi-setup/50-teaport-wifi-setup.rules.in").read_text())
    rules = tmp_path / "50-teaport-wifi-setup.rules"
    if rules_text is not None:
        rules.write_text(rules_text)
    for old, new in (("/etc/sudoers.d/teaport-config", str(tmp_path / "sudoers")),
                     ("/etc/polkit-1/rules.d/50-teaport-wifi-setup.rules", str(rules)),
                     ("/etc/polkit-1/rules.d/50-teaport-update.rules", str(tmp_path / "update-rules.d/50-teaport-update.rules")),
                     ("/usr/lib/teaport/", str(lib) + "/")):
        assert old in text, old
        text = text.replace(old, new)
    assert "/etc/polkit-1" not in text.split("set -eu", 1)[1], "a path left pointing at the real box"
    if not update_rules_writable:   # polkit's directory is there, but the rule cannot go in
        (tmp_path / "update-rules.d").mkdir()
        (tmp_path / "update-rules.d").chmod(0o500)
    script = tmp_path / "sudoers-sync"
    script.write_text(text)
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "visudo").write_text(f"#!/bin/sh\nexit {0 if visudo_ok else 1}\n")
    # install -o root needs root: the stub copies and sets the mode only.
    (bin_ / "install").write_text('#!/bin/sh\nmode=$2; shift 6; cp "$1" "$2"; chmod "$mode" "$2"\n')
    for t in ("visudo", "install"):
        (bin_ / t).chmod(0o755)
    p = subprocess.run(["sh", str(script), "alice"], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"})
    return p, tmp_path / "sudoers", rules


def test_an_update_rule_that_cannot_be_written_does_not_stop_the_wifi_narrowing(tmp_path):
    # The old broad NetworkManager rule must not stay because the update's rule failed.
    p, sudoers, rules = _sudoers_sync(tmp_path, WIDE_RULE, update_rules_writable=False)
    (tmp_path / "update-rules.d").chmod(0o700)
    assert p.returncode == 0 and "the update's polkit rule was not written" in p.stderr, p.stderr
    assert sudoers.exists() and "settings.modify.system" not in rules.read_text().split("polkit.addRule")[1]


WIDE_RULE = 'polkit.addRule(function(action, subject) { if (subject.user == "alice" && (action.id == "org.freedesktop.NetworkManager.settings.modify.system")) return polkit.Result.YES; });\n'


def test_sudoers_sync_writes_the_helpers_lines_and_narrows_a_wider_rule(tmp_path):
    p, sudoers, rules = _sudoers_sync(tmp_path, WIDE_RULE)
    assert p.returncode == 0, p.stderr
    lines = [ln for ln in sudoers.read_text().splitlines() if not ln.startswith("#")]
    assert lines == [f"alice ALL=(root) NOPASSWD: /usr/bin/python3 {tmp_path}/lib/config_apply.py *",
                     f"alice ALL=(root) NOPASSWD: /usr/bin/python3 {tmp_path}/lib/wifi_apply.py *",
                     f"alice ALL=(root) NOPASSWD:NOLOG_INPUT: /usr/bin/python3 {tmp_path}/lib/login_apply.py *"], lines
    narrowed = rules.read_text()
    assert "settings.modify.system" not in narrowed.split("polkit.addRule")[1]
    assert 'subject.user == "alice"' in narrowed and "wifi.scan" in narrowed


def test_the_generated_lines_pass_the_real_visudo(tmp_path):
    # The stub above passes anything: the lines themselves, NOLOG_INPUT included, must be
    # what sudo takes, or sudoers-sync installs none of them.
    visudo = next((p for p in ("/usr/sbin/visudo", "/sbin/visudo") if os.path.exists(p)), None)
    if visudo is None:
        pytest.skip("visudo is not installed here")
    p, sudoers, _ = _sudoers_sync(tmp_path)
    assert p.returncode == 0, p.stderr
    check = subprocess.run([visudo, "-cf", str(sudoers)], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr


def test_sudoers_sync_leaves_the_rule_while_the_lines_cannot_go_in_and_makes_none(tmp_path):
    # Without the lines, the narrowed rule would leave the Wi-Fi unchangeable.
    (tmp_path / "a").mkdir()
    p, sudoers, rules = _sudoers_sync(tmp_path / "a", WIDE_RULE, visudo_ok=False)
    assert p.returncode == 1 and rules.read_text() == WIDE_RULE and not sudoers.exists(), p.stderr
    # A box without Wi-Fi setup has no rule, and gets none.
    (tmp_path / "b").mkdir()
    p, sudoers, rules = _sudoers_sync(tmp_path / "b")
    assert p.returncode == 0 and not rules.exists() and sudoers.exists(), p.stderr


def test_an_old_setup_unit_gets_sudo_back_from_the_package():
    from test_wifi_root_env import OLD_WIFI_UNIT, _postinst
    from tempdirs import tempdir
    tmp = tempdir("teaport-postinst-helper-")
    unit, stopgap, script, env = _postinst(tmp, systemd=True)
    dropin = pathlib.Path(tmp, "d/61-teaport-wifi-helper.conf")
    unit.write_text(OLD_WIFI_UNIT + "NoNewPrivileges=yes\n")
    p = subprocess.run(["sh", str(script), "configure"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert "[Service]\nNoNewPrivileges=no\n" in dropin.read_text()
    log = pathlib.Path(tmp, "systemctl.log").read_text().splitlines()
    assert "daemon-reload" in log, log
    # The new unit has no NoNewPrivileges=: nothing to lift.
    dropin.unlink()
    stopgap.unlink()
    unit.write_text((ROOT / "systemd/teaport-wifi-setup.service.in").read_text())
    p = subprocess.run(["sh", str(script), "configure"], capture_output=True, text=True, env=env)
    assert p.returncode == 0 and not dropin.exists(), p.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
