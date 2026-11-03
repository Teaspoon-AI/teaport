#
# install.sh's engine license plumbing (teagram-engine#107, engine 1.7+): --license-file
# checks a key on this device with this engine before installing it as $ETC/license.json
# (install_license), every full run reports the key the engine will see (license_report),
# the accepted license text must be the one the engine carries (eula_matches_engine), and
# an engine without license keys is left alone. Plus the unit and env-file wiring, and
# eula_gate's summary coming from the manifest rather than the script.
#
# The functions are taken out of install.sh as they are and run by bash against a temp
# dir, with a fake engine that answers --help, --license, --license-status and
# --device-id the way #107's does. Nothing outside a temp dir is touched.
#
# Run: python test_install_license.py   (or via pytest test_suite.py)
#
import getpass
import grp
import json
import os
import pathlib
import subprocess

import appliance
from tempdirs import tempdir
from test_install_brain_only import _fn

ROOT = pathlib.Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"
UNIT = ROOT / "systemd" / "teaport-engine.service.in"
EULA_TEXT = "Teaport Engine License 1.0\n\nThe full text.\n"

# #107's --help lines for license keys and the embedded text (main.c / voxtral_license_key.c).
HELP_KEYS = ("  --license-file <p>  License file (default: $VOX_LICENSE_FILE, else /etc/teaport/license.json)\n"
             "  --license-status    Print the license and whether it is valid here; exit 0/1\n"
             "  --device-id         Print this device's id (what a license is issued for)\n"
             "  --license     Print the Teaport Engine License and exit\n")

# The fake engine. A key "is valid on this device" when its file holds VALID-HERE.
# --license-status prints #107's labels; Enforcement follows VOX_LICENSE_MODE (default warn).
FAKE_ENGINE = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$T/engine.calls"
case "$1" in
  --help) printf 'Usage: voxtral -d <model_dir> [options]\n'; [ "${OLD_ENGINE:-0}" = 1 ] || cat "$T/help_keys"; exit 0 ;;
  --license) [ "${OLD_ENGINE:-0}" = 1 ] && { echo "Unknown option: --license" >&2; exit 1; }; cat "$T/embedded_eula"; exit 0 ;;
  --device-id) echo tpdev2-0123456789abcdef0123456789abcdef; echo "(from: gpu-uuid,dt-serial)" >&2; exit 0 ;;
  --license-status)
    f=/etc/teaport/license.json; [ "${2:-}" = --license-file ] && f="$3"
    echo "License file:   $f"
    echo "Enforcement:    ${VOX_LICENSE_MODE:-warn} (build default; VOX_LICENSE_MODE unset)"
    echo "This device:    tpdev2-0123456789abcdef0123456789abcdef (from: gpu-uuid,dt-serial)"
    if [ -r "$f" ] && grep -q VALID-HERE "$f"; then
      printf 'License id:     TS-2026-0042\nLicensee:       Pat <pat@example.com>\nTier:           commercial\nExpires:        never\nResult:         VALID\n'
      exit 0
    elif [ -e "$f" ]; then echo "Result:         INVALID: bound to another device"; exit 1
    else echo "Result:         INVALID: no license file -- $f: No such file or directory"; exit 1; fi ;;
esac
echo "Unknown option: $1" >&2; exit 1
'''

# install.sh's helpers, the privileged ones run as the test user. SUDO records each call
# and drops install's -o/-g (only root can chown), so a test reads the owner and group
# asked for from $T/sudo.log.
STUBS = r'''
set -euo pipefail
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
die()  { printf 'DIE %s\n' "$*"; exit 1; }
contains() { case "$2" in *"$1"*) return 0 ;; *) return 1 ;; esac; }
read_env_file() { if [ -r "$1" ]; then cat "$1"; elif [ "${2:-}" = --lenient ]; then :; else return 1; fi; }
sudo() { printf 'sudo %s\n' "$*" >> "$T/sudo.log"; shift 3; "$@"; }   # sudo -u USER -- cmd...
SUDO() {
  printf '%s\n' "$*" >> "$T/sudo.log"
  if [ "$1" = install ]; then
    shift; local args=()
    while [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; done
    command install "${args[@]}"
  else "$@"; fi
}
'''

FNS = ("mget", "mget_lines", "engine_has", "as_run_user", "license_src_check", "env_value", "status_field",
       "eula_matches_engine", "install_license", "license_report")


def _box(old_engine=False, embedded=EULA_TEXT, manifest_eula=EULA_TEXT, summary=None):
    """A temp install: $T/opt/bin/voxtral (the fake), $T/etc, a manifest."""
    t = pathlib.Path(tempdir("teaport-license-"))
    (t / "opt" / "bin").mkdir(parents=True)
    (t / "etc").mkdir()
    eng = t / "opt" / "bin" / "voxtral"
    eng.write_text(FAKE_ENGINE)
    eng.chmod(0o755)
    (t / "help_keys").write_text(HELP_KEYS)
    (t / "embedded_eula").write_text(embedded)
    import hashlib
    eula = {"version": "1.0", "url": "https://example.invalid/eula.txt",
            "sha256": hashlib.sha256(manifest_eula.encode()).hexdigest()}
    if summary is not None:
        eula["summary"] = summary
    (t / "manifest.json").write_text(json.dumps({"engine": {"eula": eula}}))
    return t, old_engine


def _run(box, script, license_src="", dry_run=False, run_user=None, env=None):
    t, old = box
    body = (STUBS + "".join(_fn(f) for f in FNS) +
            f'PREFIX="{t}/opt"; ETC="{t}/etc"; LICENSE_FILE="$ETC/license.json"; MF="{t}/manifest.json"; '
            f'DRY_RUN={int(dry_run)}; RUN_USER="{run_user or getpass.getuser()}"; '
            f'LICENSE_SRC="{license_src}"\n' + script + "\necho DONE")
    e = {**os.environ, "T": str(t), "OLD_ENGINE": str(int(old)), **(env or {})}
    e.pop("VOX_LICENSE_MODE", None)
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=e)
    return p.returncode, p.stdout + p.stderr


def _key(box, name="key.json", valid=True):
    f = box[0] / name
    f.write_text('{"payload": {}, "signature": "%s"}\n' % ("VALID-HERE" if valid else "elsewhere"))
    return f


def _sudo_log(box):
    f = box[0] / "sudo.log"
    return f.read_text().splitlines() if f.exists() else []


def _engine_calls(box):
    f = box[0] / "engine.calls"
    return f.read_text().splitlines() if f.exists() else []


# ---- install_license / license_report ----

def test_a_valid_key_is_checked_on_this_device_then_installed_root_owned_0640():
    box = _box()
    key = _key(box)
    rc, out = _run(box, "install_license", license_src=str(key))
    assert rc == 0 and "DONE" in out, out
    lic = box[0] / "etc" / "license.json"
    assert lic.read_text() == key.read_text()
    assert oct(lic.stat().st_mode & 0o777) == "0o640", oct(lic.stat().st_mode)
    group = grp.getgrgid(os.getgid()).gr_name
    installs = [c for c in _sudo_log(box) if c.startswith("install -o")]
    assert installs == [f"install -o root -g {group} -m 0640 {key} {lic}.new"], installs
    # checked BEFORE it took the real name: staged, verified there, then moved
    calls = _engine_calls(box)
    assert f"--license-status --license-file {lic}.new" in calls, calls
    assert not (box[0] / "etc" / "license.json.new").exists()
    assert "LOG license key installed" in out
    assert "LOG engine license key: TS-2026-0042, commercial, Pat <pat@example.com>; expires never" in out, out


def test_a_key_for_another_device_stops_the_run_and_installs_nothing():
    box = _box()
    old = box[0] / "etc" / "license.json"
    old.write_text("the key already here VALID-HERE\n")
    key = _key(box, valid=False)
    rc, out = _run(box, "install_license", license_src=str(key))
    assert rc == 1 and "DONE" not in out, out
    assert "DIE the license key" in out and "bound to another device" in out and "nothing was installed" in out, out
    assert old.read_text() == "the key already here VALID-HERE\n"
    assert sorted(p.name for p in (box[0] / "etc").iterdir()) == ["license.json"]


def test_a_different_key_replacing_one_keeps_the_old_beside_it():
    box = _box()
    lic = box[0] / "etc" / "license.json"
    lic.write_text("an older key VALID-HERE\n")
    key = _key(box)
    rc, out = _run(box, "install_license", license_src=str(key))
    assert rc == 0, out
    kept = [p for p in (box[0] / "etc").iterdir() if p.name.startswith("license.json.replaced-")]
    assert len(kept) == 1 and kept[0].read_text() == "an older key VALID-HERE\n", kept
    assert lic.read_text() == key.read_text()
    assert "kept as" in out


def test_the_same_key_again_keeps_no_copy():
    box = _box()
    key = _key(box)
    (box[0] / "etc" / "license.json").write_text(key.read_text())
    rc, out = _run(box, "install_license", license_src=str(key))
    assert rc == 0, out
    assert sorted(p.name for p in (box[0] / "etc").iterdir()) == ["license.json"]


def test_a_root_run_user_gets_root_root_0600():
    box = _box()
    key = _key(box)
    rc, out = _run(box, "install_license", license_src=str(key), run_user="root")
    assert rc == 0, out
    assert any(c.startswith("install -o root -g root -m 0600 ") for c in _sudo_log(box)), _sudo_log(box)
    # the check ran as the engine's user, not whoever runs the installer
    assert any(c.startswith("sudo -u root -- ") and "--license-status" in c for c in _sudo_log(box)), _sudo_log(box)


def test_no_key_under_warn_is_reported_with_the_device_id_and_the_run_goes_on():
    box = _box()
    rc, out = _run(box, "install_license")
    assert rc == 0 and "DONE" in out, out
    assert "WARN no valid engine license key on this device (INVALID: no license file" in out, out
    assert "the engine runs anyway (enforcement: warn" in out
    assert "tpdev2-0123456789abcdef0123456789abcdef" in out
    assert not (box[0] / "etc" / "license.json").exists()
    assert not any(c.startswith("install") for c in _sudo_log(box))


def test_no_key_under_the_operators_on_stops_before_any_restart():
    box = _box()
    (box[0] / "etc" / "engine.env").write_text("ENGINE_PORT=8000\nVOX_LICENSE_MODE=on\n")
    rc, out = _run(box, "install_license")
    assert rc == 1, out
    assert "DIE no valid engine license key on this device" in out and "it would refuse to start" in out, out
    assert "Nothing was restarted" in out


def test_a_quoted_mode_reads_as_systemd_gives_it_to_the_engine():
    box = _box()
    (box[0] / "etc" / "engine.env").write_text('VOX_LICENSE_MODE=on\nVOX_LICENSE_MODE="warn"\n')
    rc, out = _run(box, "install_license")
    assert rc == 0 and "WARN no valid engine license key" in out and "enforcement: warn" in out, out


def test_engine_env_whitespace_is_dropped_as_systemd_drops_it():
    for text in ('VOX_LICENSE_MODE=warn \n', 'VOX_LICENSE_MODE=warn\r\n', 'VOX_LICENSE_MODE="warn" \n',
                 '  VOX_LICENSE_MODE = warn\n'):
        box = _box()
        (box[0] / "etc" / "engine.env").write_text(text)
        rc, out = _run(box, 'printf "[%s]\\n" "$(env_value "$ETC/engine.env" VOX_LICENSE_MODE)"')
        assert rc == 0 and "[warn]" in out, (text, out)
    box = _box()
    (box[0] / "etc" / "engine.env").write_text("  VOX_LICENSE_MODE=on\n")
    rc, out = _run(box, "install_license")
    assert rc == 1 and "would refuse to start" in out, out


def test_a_relative_license_file_with_spaces_and_cdpath_set():
    box = _box()
    d = box[0] / "my keys"
    d.mkdir()
    key = _key(box, name="my keys/lic.json")
    decoy = box[0] / "cdpath" / "my keys"
    decoy.mkdir(parents=True)
    rc, out = _run(box, f'cd "{box[0]}"; export CDPATH="{box[0]}/cdpath"; license_src_check; echo "SRC=$LICENSE_SRC"',
                   license_src="my keys/lic.json")
    assert rc == 0 and f"SRC={key}\n" in out, out


def test_the_installers_own_shell_exports_do_not_reach_the_check():
    box = _box()
    rc, out = _run(box, "export VOX_LICENSE_MODE=on VOX_LICENSE_FILE=/nonexistent; install_license")
    assert rc == 0 and "enforcement: warn" in out and "DIE" not in out, out


def test_a_missing_license_file_stops_the_run_before_anything_else():
    box = _box()
    rc, out = _run(box, "license_src_check", license_src=str(box[0] / "no-such.json"))
    assert rc == 1 and "DIE --license-file: no such file" in out, out
    key = _key(box)
    rel = os.path.relpath(key, os.getcwd())
    rc, out = _run(box, 'license_src_check; echo "SRC=$LICENSE_SRC"', license_src=rel)
    assert rc == 0 and f"SRC={key}" in out, out
    main = _fn("main")
    order = [ln.strip() for ln in main.splitlines()]
    assert order.index("license_src_check") < order.index("fetch_manifest"), order


def test_an_engine_without_license_keys_is_left_alone():
    box = _box(old_engine=True)
    rc, out = _run(box, "install_license")
    assert rc == 0 and "DONE" in out, out
    assert "WARN" not in out and "license key" not in out, out
    assert _engine_calls(box) == ["--help"], _engine_calls(box)


def test_a_key_for_an_engine_without_license_keys_is_refused():
    box = _box(old_engine=True)
    rc, out = _run(box, "install_license", license_src=str(_key(box)))
    assert rc == 1 and "DIE --license-file: this engine release does not read license keys" in out, out
    assert not (box[0] / "etc" / "license.json").exists()


def test_a_dry_run_runs_no_engine_and_writes_nothing():
    box = _box()
    rc, out = _run(box, "install_license", license_src=str(_key(box)), dry_run=True)
    assert rc == 0, out
    assert "[dry-run] check" in out and "[dry-run] report the engine license key state" in out, out
    assert _engine_calls(box) == [] and _sudo_log(box) == []


# ---- eula_matches_engine ----

def test_the_engine_carrying_the_accepted_text_passes():
    box = _box()
    rc, out = _run(box, "eula_matches_engine")
    assert rc == 0 and "LOG engine license text matches the accepted one" in out, out


def test_an_engine_carrying_another_text_stops_the_run():
    box = _box(embedded="Teaport Engine License 2.0\n")
    rc, out = _run(box, "eula_matches_engine")
    assert rc == 1 and "is not the one accepted" in out, out


def test_an_engine_without_its_own_text_is_not_checked():
    box = _box(old_engine=True, embedded="something else")
    rc, out = _run(box, "eula_matches_engine")
    assert rc == 0 and "DONE" in out and "matches" not in out, out


# ---- eula_gate's summary ----

def test_the_summary_comes_from_the_manifest_as_lines():
    box = _box(summary=["first line", "second line"])
    rc, out = _run(box, "mget_lines engine.eula.summary")
    assert rc == 0 and out.startswith("first line\nsecond line\n"), out


def test_a_missing_or_malformed_summary_reads_as_none():
    for summary in (None, "one string", ["two\nlines"], [1]):
        box = _box(summary=summary)
        rc, out = _run(box, 'mget_lines engine.eula.summary && echo GOT || echo NONE')
        assert rc == 0 and "NONE" in out and "GOT" not in out, (summary, out)


def test_the_gate_states_no_terms_of_its_own():
    gate = _fn("eula_gate")
    for words in ("personal use", "commercial", "@teaspoon.tech", "free"):
        assert words not in gate, words
    assert "mget_lines engine.eula.summary" in gate


def test_every_published_manifest_summary_is_a_list_of_lines():
    for name in ("stable.json", "beta.json"):
        m = json.loads((ROOT / "manifest" / name).read_text())
        s = m["engine"]["eula"].get("summary")
        assert s is None or (isinstance(s, list) and all(isinstance(x, str) and "\n" not in x for x in s)), (name, s)


# ---- wiring ----

def test_the_unit_does_not_retry_a_refused_start_and_engine_env_names_the_key():
    unit = UNIT.read_text()
    assert "\nRestartPreventExitStatus=78\n" in unit
    services = _fn("phase_services")
    assert '"VOX_LICENSE_FILE=$LICENSE_FILE"' in services
    assert "VOX_LICENSE_MODE=" not in services.replace("VOX_LICENSE_MODE is not written", "")
    main = _fn("main")
    order = [ln.strip() for ln in main.splitlines()]
    assert order.index("phase_engine") < order.index("eula_matches_engine") < order.index("install_license") < order.index("phase_brain") < order.index("phase_services")


def test_license_file_is_parsed_and_kept_out_of_brain_only_runs():
    # Every call here ends in the argument parser (a usage error, or --help), before main.
    def parse(*args):
        p = subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True,
                           env={**os.environ, "TEAPORT_LICENSE_FILE": ""})
        return p.returncode, p.stdout + p.stderr
    assert parse("--license-file")[0] == 2
    assert parse("--license-file=")[0] == 2
    rc, out = parse("--license-file", "/x", "--only", "brain")
    assert rc == 2 and "--license-file is for a full install" in out, out
    rc, out = parse("--license-file=/x", "--rollback", "brain")
    assert rc == 2 and "--license-file is for a full install" in out, out
    rc, out = parse("--license-file=/x", "--help")
    assert rc == 0 and "--license-file PATH" in out, out


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
