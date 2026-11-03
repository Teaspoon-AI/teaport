#
# teaport status / doctor: the engine's license key (teagram-engine#107, engine 1.7+).
# status reads the running engine's latest license line from this invocation's journal
# (license_line); doctor has the engine check the key it reads (license_check). Both name
# a start the engine refused (exit 78), and both stay quiet about license keys for an
# engine that has none.
#
# The functions are taken out of cli/teaport as they are and run by bash with systemctl
# and journalctl stubbed and a fake engine, against a temp dir.
#
# Run: python test_cli_license.py   (or via pytest test_suite.py)
#
import getpass
import os
import re
import pathlib
import subprocess

import appliance
from tempdirs import tempdir
from test_install_license import FAKE_ENGINE, HELP_KEYS

CLI = pathlib.Path(__file__).resolve().parents[2] / "cli" / "teaport"


def _fn(name):
    lines = CLI.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(rf"{name}\(\)\s*\{{", ln))
    if lines[start].rstrip().endswith("}"):
        return lines[start] + "\n"
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


# systemctl show answers from $T/unit.<Prop>; journalctl prints $T/journal newest first, as
# -r does, and records its arguments.
STUBS = r'''
set -euo pipefail
c() { printf '[%s]%s' "$1" "$2"; }
GREEN='1;32'; RED='1;31'; YEL='1;33'
ok()    { printf 'OK %s\n' "$*"; }
warnc() { printf 'WARNC %s\n' "$*"; }
failc() { printf 'FAILC %s\n' "$*"; RC=1; }
systemctl() { [ "$1" = show ] && cat "$T/unit.$3" 2>/dev/null || true; }
journalctl() { printf '%s\n' "$*" >> "$T/journal.calls"; tac "$T/journal" 2>/dev/null || true; }
'''

FNS = ("envval", "engine_prop", "engine_refused", "engine_has", "engine_license_line",
       "license_line", "license_check")


def _box(old_engine=False, journal=(), props=None, engine_env=None, key=None):
    t = pathlib.Path(tempdir("teaport-cli-license-"))
    (t / "opt" / "bin").mkdir(parents=True)
    (t / "etc").mkdir()
    eng = t / "opt" / "bin" / "voxtral"
    eng.write_text(FAKE_ENGINE)
    eng.chmod(0o755)
    (t / "help_keys").write_text(HELP_KEYS)
    (t / "journal").write_text("".join(f"{ln}\n" for ln in journal))
    props = {"InvocationID": "abc123", "Result": "success", "ExecMainStatus": "0",
             "User": getpass.getuser(), **(props or {})}
    for k, v in props.items():
        (t / f"unit.{k}").write_text(v + "\n" if v else "")
    # As install.sh writes it: VOX_LICENSE_FILE names $ETC/license.json.
    if engine_env is None:
        engine_env = f"ENGINE_PORT=8000\nVOX_LICENSE_FILE={t}/etc/license.json\n"
    (t / "etc" / "engine.env").write_text(engine_env)
    if key is not None:
        (t / "etc" / "license.json").write_text(key)
    return t, old_engine


def _run(box, script):
    t, old = box
    body = (STUBS + "".join(_fn(f) for f in FNS) +
            f'PREFIX="{t}/opt"; ETC="{t}/etc"; RC=0\n' + script + "\necho DONE")
    env = {**os.environ, "T": str(t), "OLD_ENGINE": str(int(old))}
    env.pop("VOX_LICENSE_MODE", None)
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)
    assert p.returncode == 0 and "DONE" in p.stdout, p.stdout + p.stderr
    return p.stdout


def _status(journal=(), **kw):
    out = _run(_box(journal=journal, **kw), "license_line")
    return out.split("license key (engine)", 1)[1].split("\n", 1)[0].strip()


# ---- teaport status ----

def test_status_shows_the_startup_check():
    assert _status(["Teaport Engine v1.7 — ...", "License key: TS-2026-0042 (commercial) for Pat, expires never",
                    "Listening on :8000"]) == "[1;32]valid: TS-2026-0042 (commercial) for Pat, expires never"


def test_status_shows_the_newest_re_check_over_the_startup_line():
    assert _status(["License key: TS-1 (personal) for Pat, expires 2027-01-01",
                    "LICENSE KEY: re-check FAILED: no valid license key: expired -- at 2027-01-01 (enforcement on)"]) \
        == "[1;31]no valid key: no valid license key: expired -- at 2027-01-01 (enforcement on)"
    assert _status(["* LICENSE KEY: no valid license key for this device.",
                    "License key: re-check: TS-2 (commercial) for Pat is valid"]) \
        == "[1;32]valid: TS-2 (commercial) for Pat is valid"


def test_status_shows_no_key_under_warn():
    assert _status(["********************************************************************",
                    "* LICENSE KEY: no valid license key for this device.",
                    "*   no license file: /etc/teaport/license.json: No such file or directory",
                    "* Running anyway (enforcement: warn, build default; VOX_LICENSE_MODE unset).",
                    "********************************************************************"]) \
        == "[1;33]no valid key — the engine runs anyway (warn)"


def test_status_shows_a_check_turned_off_and_a_test_key():
    assert _status(["License key: check off (VOX_LICENSE_MODE=off; build default warn)"]) \
        == "[1;33]not checked (VOX_LICENSE_MODE=off; build default warn)"
    assert _status(["License key: TS-T (commercial) for QA, expires never",
                    "License key: signed with the TEST key test-1 -- this build is not a production build"]) \
        == "[1;33]signed with a TEST key — not a production build"


def test_status_shows_a_refused_start():
    assert _status(props={"Result": "exit-code", "ExecMainStatus": "78"}) \
        == "[1;31]engine refused to start: no valid license key (exit 78) — teaport doctor"


def test_status_of_an_engine_that_says_nothing():
    assert _status(["Listening on :8000"]) == "[0]not reported by the running engine"
    assert _status(props={"InvocationID": ""}) == "[0]not reported by the running engine"


def test_status_reads_only_this_invocation_of_the_unit():
    box = _box(journal=["License key: TS-1 (personal) for Pat, expires never"])
    _run(box, "license_line")
    calls = (box[0] / "journal.calls").read_text()
    assert "_SYSTEMD_INVOCATION_ID=abc123" in calls and "-b" in calls.split(), calls


# ---- teaport doctor ----

def test_doctor_passes_a_valid_key():
    out = _run(_box(key="VALID-HERE\n"), "license_check")
    assert "OK engine license key: TS-2026-0042 (commercial), expires never" in out, out
    assert "FAILC" not in out and "WARNC" not in out


def test_doctor_warns_without_a_key_under_warn_and_names_the_device():
    out = _run(_box(), "license_check")
    assert "WARNC no valid engine license key: INVALID: no license file" in out, out
    assert "the engine runs anyway (enforcement: warn" in out
    assert "tpdev2-0123456789abcdef0123456789abcdef" in out
    assert "FAILC" not in out


def test_doctor_fails_without_a_key_under_on():
    out = _run(_box(engine_env="ENGINE_PORT=8000\nVOX_LICENSE_MODE=on\n"), "license_check")
    assert "FAILC no valid engine license key" in out and "will not start without one" in out, out


def test_doctor_without_vox_license_file_checks_the_engines_default():
    out = _run(_box(engine_env="ENGINE_PORT=8000\n", key="VALID-HERE\n"), "license_check")
    assert "no license file -- /etc/teaport/license.json" in out, out


def test_doctor_checks_the_file_engine_env_names():
    t = _box(engine_env="")
    other = t[0] / "elsewhere.json"
    other.write_text("VALID-HERE\n")
    (t[0] / "etc" / "engine.env").write_text(f"VOX_LICENSE_FILE={other}\n")
    out = _run(t, "license_check")
    assert "OK engine license key: TS-2026-0042" in out, out


def test_doctor_names_a_refused_start():
    out = _run(_box(props={"Result": "exit-code", "ExecMainStatus": "78"}), "license_check")
    assert "FAILC the engine refused to start for want of a valid license key (exit 78)" in out, out


def test_doctor_says_when_it_checked_as_someone_else():
    out = _run(_box(key="VALID-HERE\n", props={"User": "teaport-engine-user"}), "license_check")
    assert "WARNC   checked as" in out and "the engine runs as teaport-engine-user" in out, out


def test_doctor_on_an_engine_without_license_keys():
    out = _run(_box(old_engine=True), "license_check")
    assert out.splitlines()[0] == "OK engine license key: not used by this engine release", out
    assert "WARNC" not in out and "FAILC" not in out


def test_status_and_doctor_call_the_license_checks():
    status = _fn("cmd_status")
    doctor = _fn("cmd_doctor")
    assert "installed teaport-engine && license_line" in status
    assert "\n  license_check\n" in doctor


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
