#
# No command that runs as root has /etc/teaport's environment.
#
# /etc/teaport/*.env are written by config_apply.py on the run user's behalf (the config
# page's sudoers line), so whatever they hold is the run user's to choose. A unit that
# loads one and runs anything as root (no User=, or an Exec line with a "+" or "!" prefix)
# would hand that choice to root: LD_PRELOAD in brain.env, and teaport-wifi-setup's old
# ExecStartPre=+ ran the attacker's library as root. What has to hold:
#   - no unit template that runs anything as root has an EnvironmentFile=;
#   - teaport-wifi-setup runs nothing as root, and Requires= its root side,
#     teaport-wifi-setup-net (up when it starts, down after it stops, however it ended);
#   - install.sh renders that root side wherever it renders teaport-wifi-setup, so a
#     --only brain re-render never leaves the unit requiring a unit that is not there;
#   - a box upgraded by the package alone (apt, the updater), whose unit predates the
#     split, gets the same: the package ships the root side, and postinst points the old
#     unit at it with its own root steps off, reloaded before anything restarts;
#   - config_apply.py refuses to write a key that changes how programs load code, and
#     still writes every other line.
# Units this repo does not hold (teaport-appliance's, oled-avatar's) are not checked here.
#
# Run: python test_wifi_root_env.py   (or via pytest test_suite.py)
#
import os
import pathlib
import re

import appliance
from tempdirs import tempdir

ROOT = pathlib.Path(__file__).resolve().parents[2]
UNITS = ROOT / "systemd"
INSTALL = ROOT / "install.sh"


def _directives(text: str) -> list[tuple[str, str, str]]:
    """(section, key, value) for each directive of a unit file (a line ending in a
    backslash continues on the next, as systemd reads it)."""
    out, section = [], ""
    for line in re.sub(r"\\\n", " ", text).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        key, _, value = line.partition("=")
        out.append((section, key.strip(), value.strip()))
    return out


def _runs_as_root(directives) -> bool:
    """Whether any command of the unit (with its drop-ins appended, in order) runs as root:
    no User= or an empty one, User=root, PermissionsStartOnly= (every step but ExecStart
    as root), or an Exec line prefixed "+" or "!". An empty Exec...= resets that list, as
    a drop-in's does."""
    svc = [(k, v) for s, k, v in directives if s == "Service"]
    users = [v for k, v in svc if k == "User"]
    if not users or users[-1] in ("", "root", "0"):
        return True
    starts_only = [v for k, v in svc if k == "PermissionsStartOnly"]
    if starts_only and starts_only[-1].lower() in ("yes", "true", "on", "1"):
        return True
    execs: dict[str, list[str]] = {}
    for k, v in svc:
        if k.startswith("Exec"):
            execs[k] = execs.get(k, []) + [v] if v else []
    for lines in execs.values():
        for v in lines:
            prefix = re.match(r"^[-@:+!]*", v).group(0)
            if "+" in prefix or "!" in prefix:
                return True
    return False


def test_no_unit_that_runs_as_root_loads_an_environment_file():
    checked = 0
    for tpl in sorted(UNITS.glob("*.service.in")):
        d = _directives(tpl.read_text())
        if _runs_as_root(d):
            checked += 1
            env = [v for s, k, v in d if k == "EnvironmentFile"]
            assert not env, f"{tpl.name} runs as root and loads {env}"
    assert checked >= 1   # teaport-wifi-setup-net, at least


def test_wifi_setup_runs_nothing_as_root_and_requires_its_root_side():
    user = _directives((UNITS / "teaport-wifi-setup.service.in").read_text())
    assert not _runs_as_root(user)
    assert ("Unit", "Requires", "teaport-wifi-setup-net.service") in user
    assert ("Unit", "After", "teaport-wifi-setup-net.service") in user
    assert not any("wifi-setup-net" in v for s, k, v in user if k.startswith("Exec"))
    net = _directives((UNITS / "teaport-wifi-setup-net.service.in").read_text())
    want = {("Unit", "StopWhenUnneeded", "yes"), ("Service", "Type", "oneshot"),
            ("Service", "RemainAfterExit", "yes"),
            ("Service", "ExecStart", "/usr/local/lib/teaport/wifi-setup-net up"),
            ("Service", "ExecStopPost", "-/usr/local/lib/teaport/wifi-setup-net down")}
    assert want <= set(net), set(net)
    assert not any(k in ("User", "EnvironmentFile", "Environment") for s, k, v in net), net


def test_the_root_check_itself():
    assert _runs_as_root(_directives("[Service]\nExecStart=/bin/x\n"))
    assert _runs_as_root(_directives("[Service]\nUser=root\nExecStart=/bin/x\n"))
    assert _runs_as_root(_directives("[Service]\nUser=bob\nExecStartPre=-+/bin/x\n"))
    assert _runs_as_root(_directives("[Service]\nUser=bob\nExecStart=\\\n  !/bin/x\n"))
    assert _runs_as_root(_directives("[Service]\nUser=\nExecStart=/bin/x\n"))
    assert _runs_as_root(_directives("[Service]\nUser=bob\nPermissionsStartOnly=yes\nExecStart=/bin/x\n"))
    assert not _runs_as_root(_directives("[Service]\nUser=bob\nExecStart=-/bin/x\n"))
    # A drop-in's empty ExecStartPre= drops the "+" line before it.
    assert not _runs_as_root(_directives("[Service]\nUser=bob\nExecStartPre=+/bin/x\nExecStart=/bin/y\n"
                                         "[Service]\nExecStartPre=\n"))


def test_install_renders_the_root_side_wherever_it_renders_wifi_setup():
    lines = INSTALL.read_text().splitlines()
    renders = [i for i, ln in enumerate(lines)
               if re.search(r"render_unit teaport-wifi-setup\.service\.in", ln)]
    assert len(renders) >= 2, renders   # phase_wifi_setup and brain_render_units
    for i in renders:
        before = "\n".join(lines[max(0, i - 3):i])
        assert "render_unit teaport-wifi-setup-net.service.in teaport-wifi-setup-net.service" in before, (i, before)


def test_config_apply_refuses_keys_that_load_code():
    import sys
    sys.path.insert(0, str(ROOT / "brain"))
    from teaport_brain import config_apply
    etc = tempdir("teaport-config-apply-")
    saved = config_apply.ETC
    config_apply.ETC = etc
    path = os.path.join(etc, "brain.env")
    pathlib.Path(path).write_text("LLM_MODEL=a\n")
    os.chmod(path, 0o640)
    try:
        for line in ("LD_PRELOAD=/home/u/x.so", "export LD_LIBRARY_PATH=/tmp", "PYTHONPATH=/tmp",
                     "  PATH = /tmp:/usr/bin", "BASH_ENV=/tmp/x", "NODE_OPTIONS=--require /tmp/x.js",
                     "GCONV_PATH=/tmp", "PERL5OPT=-Mx", "XTABLES_LIBDIR=/home/u/xt", "OPENSSL_CONF=/tmp/c",
                     "IPTABLES_LIB_DIR=/home/u/xt", "IP6TABLES_LIB_DIR=/home/u/xt"):
            try:
                config_apply.write("brain.env", f"LLM_MODEL=b\n{line}\n")
            except SystemExit as e:
                assert "changes how programs load code" in str(e), (line, e)
            else:
                raise AssertionError(line)
            assert pathlib.Path(path).read_text() == "LLM_MODEL=a\n"   # nothing written
        # Every other line still goes: settings, comments, a commented-out unsafe key,
        # names that merely start like one.
        ok = "# a note\nLLM_MODEL=b\n# LD_PRELOAD=x\nPATHFINDER=1\nPYTHONIC=yes\nTTS_PACING=lead\n"
        config_apply.write("brain.env", ok)
        assert pathlib.Path(path).read_text() == ok
    finally:
        config_apply.ETC = saved


def _fn(name: str) -> str:
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _bash(script, env=None):
    import subprocess
    stubs = "set -euo pipefail\nwarn() { printf 'WARN %s\\n' \"$*\"; }\ndie() { printf 'DIE %s\\n' \"$*\"; exit 1; }\n"
    p = subprocess.run(["bash", "-c", stubs + script], capture_output=True, text=True, env={**os.environ, **(env or {})})
    return p.returncode, p.stdout + p.stderr


def test_a_host_name_from_brain_env_is_a_host_name_or_nothing():
    fn = _fn("host_name_ok")
    for name in ("teaport.local", "teaport-ab12.local", "box.example.com", "a"):
        assert _bash(fn + f"host_name_ok '{name}'")[0] == 0, name
    for name in ("x;id>/tmp/p|e;s|a|b.local", "a b.local", "-x.local", "x-.local", "a..local", "",
                 "a\nb.local", "x{.local", "A.local", "a" * 64 + ".local", "x|y.local"):
        assert _bash(fn + 'host_name_ok "$N"', {"N": name})[0] != 0, name
    # ASCII only, whatever the locale sudo passes on: under en_US.UTF-8 bash's [a-z] alone
    # takes these (where that locale is installed; C.UTF-8 never did).
    for loc in ("en_US.UTF-8", "C.UTF-8"):
        for name in ("bøx.local", "é.local", "a²", "teaport.lоcal"):   # the last o is Cyrillic
            assert _bash(fn + 'host_name_ok "$N"', {"N": name, "LC_ALL": loc})[0] != 0, (loc, name)
        assert _bash(fn + "host_name_ok teaport.local", {"LC_ALL": loc})[0] == 0, loc
    text = INSTALL.read_text()
    assert 'host_name_ok "$FRONTDOOR_HOST" || die' in text and 'if [ -n "$had" ] && ! host_name_ok "$had"; then' in text


OLD_WIFI_UNIT = ("[Unit]\nDescription=old\n[Service]\nUser=bob\nEnvironmentFile=-/etc/teaport/brain.env\n"
                 "ExecStartPre=+/usr/local/lib/teaport/wifi-setup-net up\nExecStart=/opt/teaport/brain/bin/python -m x\n"
                 "ExecStopPost=+/usr/local/lib/teaport/wifi-setup-net down\n")


def _postinst(tmp, systemd: bool):
    """postinst with its paths moved under tmp (the package's copy of the root unit in
    tmp/usr, /etc/systemd/system's in tmp/etc); under "systemd", systemctl is a stub that
    logs its arguments to tmp/systemctl.log and calls setup active while tmp/active exists."""
    unit = pathlib.Path(tmp, "teaport-wifi-setup.service")
    stopgap = pathlib.Path(tmp, "d/60-teaport-no-root-steps.conf")
    for d in ("usr", "etc"):
        pathlib.Path(tmp, d).mkdir(exist_ok=True)
    pathlib.Path(tmp, "usr/teaport-wifi-setup-net.service").write_text(
        (UNITS / "teaport-wifi-setup-net.service.in").read_text())
    text = (ROOT / "packaging/deb/postinst").read_text()
    for old, new in (("/etc/systemd/system/teaport-wifi-setup.service.d/60-teaport-no-root-steps.conf", str(stopgap)),
                     ("/etc/systemd/system/teaport-wifi-setup.service.d/61-teaport-wifi-helper.conf",
                      str(stopgap.parent / "61-teaport-wifi-helper.conf")),
                     ("/etc/systemd/system/teaport-wifi-setup.service", str(unit)),
                     ("/etc/systemd/system/$net", tmp + "/etc/$net"), ("/usr/lib/systemd/system/$net", tmp + "/usr/$net"),
                     ("/usr/lib/teaport/sudoers-sync", "true"), ("/opt/teaport", tmp + "/opt"),
                     ("/usr/local/bin/teaport", tmp + "/no-cli"),
                     ("/run/systemd/system", tmp if systemd else tmp + "/no-systemd")):
        assert old in text, old
        text = text.replace(old, new)
    script = pathlib.Path(tmp, "postinst")
    script.write_text(text)
    bin_ = pathlib.Path(tmp, "bin")
    bin_.mkdir(exist_ok=True)
    stub = bin_ / "systemctl"
    stub.write_text(f'#!/bin/sh\necho "$*" >> {tmp}/systemctl.log\n'
                    f'[ "$1" != is-active ] || [ -e {tmp}/active ]\n')
    stub.chmod(0o755)
    return unit, stopgap, script, {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}


def test_an_old_unit_left_by_a_package_only_update_runs_no_root_step():
    import subprocess
    tmp = tempdir("teaport-postinst-wifi-")
    unit, stopgap, script, env = _postinst(tmp, systemd=False)
    unit.write_text(OLD_WIFI_UNIT)
    assert _runs_as_root(_directives(OLD_WIFI_UNIT))
    p = subprocess.run(["sh", str(script), "configure"], capture_output=True, text=True, env=env)
    assert p.returncode == 0 and "now run from teaport-wifi-setup-net" in p.stderr, p.stderr
    # The old unit with the drop-in: nothing as root, and the root side comes from the unit
    # the package ships (which loads no environment: test above).
    both = _directives(OLD_WIFI_UNIT + stopgap.read_text())
    assert not _runs_as_root(both), both
    assert ("Unit", "Requires", "teaport-wifi-setup-net.service") in both
    assert ("Unit", "After", "teaport-wifi-setup-net.service") in both
    # The root unit is in /etc too, so a rollback to a package without it leaves the
    # drop-in's Requires= met; a copy already there (install.sh's) is left as it is.
    etc_net = pathlib.Path(tmp, "etc/teaport-wifi-setup-net.service")
    assert etc_net.read_text() == (UNITS / "teaport-wifi-setup-net.service.in").read_text()
    etc_net.write_text("# install.sh's\n")
    p = subprocess.run(["sh", str(script), "configure"], capture_output=True, text=True, env=env)
    assert p.returncode == 0 and etc_net.read_text() == "# install.sh's\n", p.stderr
    # The new unit: nothing to turn off.
    stopgap.unlink()
    unit.write_text((UNITS / "teaport-wifi-setup.service.in").read_text())
    p = subprocess.run(["sh", str(script), "configure"], capture_output=True, text=True, env=env)
    assert p.returncode == 0 and not stopgap.exists(), p.stderr


def test_the_drop_in_is_loaded_before_a_running_setup_is_restarted():
    # A running unit keeps its old definition until a reload; restarted under it, its root
    # steps would run with brain.env again.
    import subprocess
    tmp = tempdir("teaport-postinst-reload-")
    unit, stopgap, script, env = _postinst(tmp, systemd=True)
    unit.write_text(OLD_WIFI_UNIT)
    p = subprocess.run(["sh", str(script), "configure"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    log = pathlib.Path(tmp, "systemctl.log").read_text().splitlines()
    assert "daemon-reload" in log and "try-restart teaport-wifi-setup.service" in log, log
    first_restart = min(i for i, ln in enumerate(log) if ln.startswith("try-restart"))
    assert log.index("daemon-reload") < first_restart, log
    assert "start teaport-wifi-setup-net.service" not in log, log   # no setup running
    # A setup running: its root side comes up right after the reload, before the brain's
    # restart, which may fail and end the script (the old clean-up step is gone by then).
    pathlib.Path(tmp, "systemctl.log").unlink()
    pathlib.Path(tmp, "active").touch()
    p = subprocess.run(["sh", str(script), "configure"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    log = pathlib.Path(tmp, "systemctl.log").read_text().splitlines()
    first_restart = min(i for i, ln in enumerate(log) if ln.startswith("try-restart"))
    assert log.index("daemon-reload") < log.index("start teaport-wifi-setup-net.service") < first_restart, log
    # Nothing written: the reload the package's own units need (teaport-update,
    # teaport-wifi-setup-net), and no root side started for a setup that is running — the
    # new unit Requires= it already.
    pathlib.Path(tmp, "systemctl.log").unlink()
    stopgap.unlink()
    unit.write_text((UNITS / "teaport-wifi-setup.service.in").read_text())
    p = subprocess.run(["sh", str(script), "configure"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    log = pathlib.Path(tmp, "systemctl.log").read_text().splitlines()
    assert "daemon-reload" in log and "start teaport-wifi-setup-net.service" not in log, log
    pathlib.Path(tmp, "active").unlink()


def test_the_package_ships_the_root_side_as_it_is():
    # nfpm copies the template verbatim, so it must have nothing to substitute.
    net = (UNITS / "teaport-wifi-setup-net.service.in").read_text()
    assert "@" not in net, net
    nfpm = (ROOT / "packaging/nfpm.yaml").read_text()
    assert ("  - src: systemd/teaport-wifi-setup-net.service.in\n"
            "    dst: /usr/lib/systemd/system/teaport-wifi-setup-net.service\n") in nfpm
    # The build stages only SRC_PATHS (build-deb.sh) and runs nfpm there: every relative
    # src: must sit under one of them, or the build cannot find it.
    build = (ROOT / "packaging/build-deb.sh").read_text()
    src_paths = re.search(r"^SRC_PATHS=\(([^)]*)\)", build, re.M).group(1).split()
    for src in re.findall(r"^\s*- src: (\S+)", nfpm, re.M):
        if not src.startswith("/"):
            assert any(src == p or src.startswith(p.rstrip("/") + "/") for p in src_paths), (src, src_paths)
    postrm = (ROOT / "packaging/deb/postrm").read_text()
    assert "rm -f /etc/systemd/system/teaport-wifi-setup.service.d/60-teaport-no-root-steps.conf" in postrm


def test_rendering_the_new_pair_removes_the_stopgap_and_takes_over_a_running_setup():
    tmp = tempdir("teaport-split-")
    stopgap = pathlib.Path(tmp, "stopgap.conf")
    stopgap.write_text("[Service]\n")
    stub = (f'SUDO() {{ printf "SUDO %s\\n" "$*"; [ "$1" = rm ] && command rm -f "${{@: -1}}"; return 0; }}\n'
            f'WIFI_SETUP_STOPGAP="{stopgap}"\nWIFI_SETUP_HELPER_DROPIN="{tmp}/no-helper-dropin"\n')
    rc, out = _bash(stub + 'systemctl() { [ "$1" = is-active ]; }\n' + _fn("wifi_setup_split_done") + "wifi_setup_split_done\n")
    assert rc == 0 and not stopgap.exists() and "SUDO systemctl daemon-reload" in out, out
    assert "SUDO systemctl start teaport-wifi-setup-net.service" in out, out
    rc, out = _bash(stub + 'systemctl() { return 3; }\n' + _fn("wifi_setup_split_done") + "wifi_setup_split_done\n")
    assert rc == 0 and "start" not in out, out   # no setup running: nothing to take over
    text = INSTALL.read_text()
    bru = _fn("brain_render_units")
    assert bru.index("SUDO systemctl daemon-reload") < bru.index("wifi_setup_split_done"), bru


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
