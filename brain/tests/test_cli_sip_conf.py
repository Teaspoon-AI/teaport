#
# cli/teaport's SIP conf writers (issues #68, #64): the staged temps that hold the SIP
# password are removed on INT/TERM; a new conf written by root goes to the gateway
# unit's User=; an adopt that fails says whether it could not READ the source or could
# not WRITE the destination; the units' keys are forced in one pass that rewrites a conf
# exactly as the old one-key-at-a-time sip_conf_set did; `sip aec on|off` checks its
# preconditions once. And (#139's leftovers) the unit's User=/Group= read as systemd
# reads them (empty = root, numbers, unknown = never starts); one sip_conf_owner answer
# for preflight and staging; temps listed before a signal can act; the conf's directory
# made for the gateway; one cleanup trap that nests; one message per refusal. And (#155)
# a re-configure keeps the gateway settings the wizard does not set — sip_source_filter,
# every sip_source_allow line, the rest — test-registers them, and never prints a value.
#
# The CLI's functions are taken as they are (everything above its dispatch) and run by
# bash with TEAPORT_SECRETS_DIR in a temp dir and systemctl/sudo/id stubbed where a test
# needs them (teaport-sip.service always: UNIT_STUB). Nothing outside a temp dir is
# touched; no service is.
#
# Run: python test_cli_sip_conf.py   (or via pytest test_suite.py)
#
import grp
import os
import pathlib
import pwd
import re
import signal
import subprocess
import time

from tempdirs import tempdir

CLI = pathlib.Path(__file__).resolve().parents[2] / "cli" / "teaport"
DISPATCH = 'cmd="${1:-}"; shift || true'

# The one-key rewrite as it was before #68 (sip_conf_set's grep count + awk), run once
# per key: the oracle the single pass has to match byte for byte.
OLD_SET = r'''
old_set() {
  local f="$1" k="$2" v="$3" n
  n="$(grep -cE "^[[:space:]]*${k}[[:space:]]*=" "$f" 2>/dev/null || true)"; n="${n:-0}"
  awk -v k="$k" -v v="$v" -v n="$n" '
    $0 ~ ("^[ \t]*" k "[ \t]*=") { if (++seen == n) print k "=" v; next }
    { print }
    END { if (n == 0) print k "=" v }' "$f" > "$f.new" && mv -f "$f.new" "$f"
}
'''


def _functions() -> str:
    text = CLI.read_text()
    return text[:text.index(DISPATCH)]


# teaport-sip.service as the CLI reads it, so no run sees the host's own unit (on the
# box it is installed, and names its own conf): installed (UNIT_LOAD), with UNIT_USER,
# UNIT_GROUP, and a ConditionPathExists=UNIT_CONF when one is set — followed, as on a
# box with the line held off, by the SIP guard's drop-in and its own condition.
UNIT_STUB = r'''
systemctl() { case "$*" in
  "show -p LoadState --value teaport-sip.service") printf '%s\n' "${UNIT_LOAD-loaded}" ;;
  "show -p User --value teaport-sip.service") printf '%s\n' "${UNIT_USER-}" ;;
  "show -p Group --value teaport-sip.service") printf '%s\n' "${UNIT_GROUP-}" ;;
  "cat teaport-sip.service") [ -n "${UNIT_CONF-}" ] && printf '%s\n' \
      "# /etc/systemd/system/teaport-sip.service" "[Unit]" "ConditionPathExists=$UNIT_CONF" "" \
      "# /run/systemd/system/teaport-sip.service.d/zz-fake-sip-guard.conf" "[Unit]" \
      "ConditionPathExists=/nonexistent/fake-sip-guard" ;;
  *) command systemctl "$@" ;;
esac; }
'''


def _run(secrets, steps, stubs="", env_extra=None):
    script = _functions() + OLD_SET + UNIT_STUB + stubs + "\n" + steps + "\n"
    env = dict(os.environ, TEAPORT_SECRETS_DIR=str(secrets), HOME=str(secrets), **(env_extra or {}))
    env.pop("TEAPORT_SIP_CONF", None)
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def _secrets():
    return pathlib.Path(tempdir("teaport-cli-"))


def _temps(d):
    return sorted(p.name for p in d.iterdir() if p.name.startswith("teaport-sip.conf."))


CONF = """# operator's conf
bind_addr=0.0.0.0
sip_port=5070
id_uri=sip:alice@example.com
registrar_uri=sip:sbc.example.com
username=alice
password=hunter2
uds_path=/tmp/old.sock
register = false
"""

# Shapes the rewrite has to keep: spaced and indented keys, duplicates (the LAST one
# wins in the gateway's parser), commented-out keys, near-miss names, missing keys,
# blank lines, a value with `=` in it, and a last line with no newline.
SHAPES = {
    "plain": CONF,
    "dupes": "sip_port=1\n# c\nsip_port = 2\n  sip_port\t=3\nother=x\nuds_path=/a\nuds_path=/b\n",
    "missing": "id_uri=sip:a@b\npassword=p=q=r\n",
    "near": "#sip_port=1\nsip_port_extra=2\nregister_uri=x\nauto_answer_x=1\n\n\nregister=yes",
    "empty": "",
    "all": "auto_answer=true\nregister=0\nuds_path=x\nsip_port=1\n",
}


def test_one_pass_rewrites_exactly_as_four_sip_conf_set_calls():
    d = _secrets()
    for name, text in SHAPES.items():
        a, b = d / f"{name}.old", d / f"{name}.src"
        a.write_text(text); b.write_text(text)
        rc, out = _run(d, f'''
          old_set {a} sip_port 5060; old_set {a} uds_path /run/teaport/teaport-sip.sock
          old_set {a} register true; old_set {a} auto_answer false
          sip_adopt_conf_keys 5060 /run/teaport/teaport-sip.sock < {b} > {b}.new''')
        assert rc == 0, out
        assert pathlib.Path(f"{b}.new").read_bytes() == a.read_bytes(), name


def test_sip_conf_set_still_rewrites_one_key_as_before_and_clears_its_trap():
    d = _secrets()
    for name, text in SHAPES.items():
        a, b = d / f"{name}.old", d / f"{name}.conf"
        a.write_text(text); b.write_text(text); os.chmod(b, 0o640)
        rc, out = _run(d, f'old_set {a} sip_port 5099; sip_conf_set {b} sip_port 5099; trap -p EXIT INT TERM')
        assert rc == 0, out
        assert b.read_bytes() == a.read_bytes(), name
        assert oct(b.stat().st_mode & 0o777) == "0o640", name      # mode kept
        assert "trap" not in out, out                                  # traps cleared
    assert not [p for p in d.iterdir() if ".conf." in p.name]


def test_a_signal_mid_rewrite_removes_the_staged_temp():
    for sig, code in (("TERM", 143), ("INT", 130)):
        d = _secrets()
        conf = d / "teaport-sip.conf"
        conf.write_text(CONF); os.chmod(conf, 0o600)
        # Stopped between staging and the rename: the temp exists, holding the password.
        stub = f'mv() {{ ls {d} | grep -q "teaport-sip.conf\\." && echo STAGED; kill -{sig} $$; sleep 1; }}'
        rc, out = _run(d, f"sip_conf_set {conf} aec false", stub)
        assert rc == code and "STAGED" in out, (sig, rc, out)
        assert _temps(d) == [], (sig, _temps(d))
        assert conf.read_text() == CONF


ADOPT_STUBS = r'''
sip_refuse_hand_launched() { :; }
sip_test_register_conf() { eval "${PROBE_HOOK:-:}"; }
sip_turn_on() { echo TURN_ON; }
sip_wait_registered() { :; }
'''


def test_a_signal_mid_adopt_removes_the_staged_copy():
    for sig, code in (("TERM", 143), ("INT", 130)):
        d = _secrets()
        src = d / "src.conf"; src.write_text(CONF)
        stub = ADOPT_STUBS + f'mv() {{ ls {d} | grep -q "teaport-sip.conf\\." && echo STAGED; kill -{sig} $$; sleep 1; }}'
        rc, out = _run(d, f"sip_adopt_conf {src}", stub)
        assert rc == code and "STAGED" in out and "TURN_ON" not in out, (sig, rc, out)
        assert _temps(d) == [] and not (d / "teaport-sip.conf").exists(), _temps(d)


def test_adopt_says_read_or_write_and_leaves_nothing_behind():
    d = _secrets()
    src = d / "src.conf"; src.write_text(CONF)
    installed = d / "teaport-sip.conf"; installed.write_text("password=old\n"); os.chmod(installed, 0o600)
    # The source goes unreadable after the probe passed.
    rc, out = _run(d, f"PROBE_HOOK='rm -f {src}'; sip_adopt_conf {src}", ADOPT_STUBS)
    assert rc == 1 and f"could not read {src}" in out and "could not write" not in out, out
    assert installed.read_text() == "password=old\n" and _temps(d) == []
    # The rename fails.
    src.write_text(CONF)
    rc, out = _run(d, f"sip_adopt_conf {src}", ADOPT_STUBS + "mv() { return 1; }")
    assert rc == 1 and f"could not write {installed}" in out and "could not read" not in out, out
    assert installed.read_text() == "password=old\n" and _temps(d) == []
    # The copy itself: an unreadable source is 2, an unwritable destination 1 — with
    # pipefail and without (the verdict reads both statuses, not the pipeline's).
    for pf in ("-o", "+o"):
        rc, out = _run(d, f'''set {pf} pipefail
          sip_adopt_copy 5060 /s {d}/nope > {d}/x || echo "rc=$?"
          sip_adopt_copy 5060 /s {src} > /dev/full || echo "rc=$?"
          sip_adopt_copy 5060 /s {src} > {d}/no/such/dir || echo "rc=$?"
          sip_adopt_copy 5060 /s {src} > {d}/ok && echo "rc=0"
          set -e; sip_adopt_copy 5060 /s {src} > {d}/ok2; echo "rc=0 under set -e"''')
        assert rc == 0, (pf, out)
        assert (out.count("rc=2"), out.count("rc=1"), out.count("rc=0")) == (1, 2, 2), (pf, out)
    # And a clean adopt installs the forced keys, 0600, with no temp left.
    rc, out = _run(d, f"sip_adopt_conf {src}", ADOPT_STUBS)
    assert rc == 0 and "TURN_ON" in out, out
    body = installed.read_text()
    assert "sip_port=5060" in body and "register=true" in body and "auto_answer=false" in body
    assert "password=hunter2" in body and _temps(d) == []


ROOT_STUBS = r'''
id() { if [ "${1:-}" = -u ] && [ $# = 1 ]; then echo 0; else command id "$@"; fi; }
'''
ME = pwd.getpwuid(os.getuid()).pw_name
IDS = f"{os.getuid()}:{os.getgid()}"


def test_a_new_conf_written_by_root_goes_to_the_units_user_or_is_refused():
    d = _secrets()
    dest = d / "teaport-sip.conf"
    rc, out = _run(d, f'UNIT_USER={ME}; sip_conf_install {dest} "" echo x; stat -c "%U %a" {dest}', ROOT_STUBS)
    assert rc == 0 and out.strip() == f"{ME} 600", out
    # The units not installed (nothing says who the gateway runs as), or a User= this host
    # does not have.
    for unit in ("UNIT_LOAD=not-found", "UNIT_USER=no-such-user-xyz"):
        d = _secrets()
        rc, out = _run(d, f'{unit}; sip_conf_install {d}/teaport-sip.conf "" echo x || echo "REFUSED $?: $SIP_WHY"', ROOT_STUBS)
        assert "REFUSED 3" in out and "gateway cannot open" in out, out
        assert _temps(d) == [] and not (d / "teaport-sip.conf").exists(), _temps(d)


def test_adopt_as_root_refuses_an_ownerless_new_conf_before_the_test_registration():
    d = _secrets()
    src = d / "src.conf"; src.write_text(CONF)
    stubs = ADOPT_STUBS + ROOT_STUBS
    rc, out = _run(d, f"UNIT_LOAD=not-found; PROBE_HOOK='echo PROBED'; sip_adopt_conf {src}", stubs)
    assert rc == 1 and "gateway cannot open" in out and "nothing tested" in out, out
    assert "PROBED" not in out and not (d / "teaport-sip.conf").exists() and _temps(d) == [], out
    # An installed conf keeps its owner whoever runs this: nothing to ask the unit then.
    (d / "teaport-sip.conf").write_text("password=old\n"); os.chmod(d / "teaport-sip.conf", 0o600)
    rc, out = _run(d, f"UNIT_LOAD=not-found; PROBE_HOOK='echo PROBED'; sip_adopt_conf {src}", stubs)
    assert rc == 0 and "PROBED" in out and "TURN_ON" in out, out


AEC_STUBS = r'''
installed() { echo CHECK; return 0; }
sip_enabled() { return 0; }
SUDO() { echo "SUDO $*"; }
sip_wait_registered() { :; }
'''


def test_aec_toggle_checks_once_and_says_not_configured_once():
    d = _secrets()
    rc, out = _run(d, "sip_aec off", AEC_STUBS)
    assert rc == 1 and out.count("telephony is not configured") == 1, out
    conf = d / "teaport-sip.conf"; conf.write_text(CONF + "aec=true\n"); os.chmod(conf, 0o600)
    rc, out = _run(d, "sip_aec off", AEC_STUBS)
    assert rc == 0 and out.count("CHECK") == 1, out
    assert "aec: true -> false" in out and "SUDO systemctl restart teaport-sip.service" in out, out
    assert "aec=false" in conf.read_text()
    # Showing the setting needs the conf only, not the units.
    rc, out = _run(d, "sip_aec", AEC_STUBS)
    assert rc == 0 and out.startswith("aec=false") and "CHECK" not in out, out
    rc, out = _run(d, "sip_restart", AEC_STUBS)
    assert rc == 0 and out.count("CHECK") == 1 and "SUDO systemctl restart" in out, out


# The wizard (`sip configure` without --conf, issue #134) installs through the same
# sip_conf_install as the adopt path: staged, given to the unit's User= when root writes a
# new one, renamed into place, and 0600 whatever the installed conf was.
WIZARD_STUBS = r'''
sip_refuse_hand_launched() { :; }
sip_test_register() { eval "${PROBE_HOOK:-:}"; }
sip_turn_on() { echo TURN_ON; }
sip_wait_registered() { :; }
'''
WIZARD = "sip_configure --host sbc.example.com --user bob --password s3cret --yes"


def test_wizard_as_root_gives_a_new_conf_to_the_units_user_0600():
    me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    d = _secrets()
    # Not really root, so the hand-over is to ourselves: the chown is what shows it asked.
    stubs = WIZARD_STUBS + ROOT_STUBS + f'chown() {{ echo "$1" >> {d}/chown.log; command chown "$@"; }}'
    rc, out = _run(d, f"UNIT_USER={me}; PROBE_HOOK='echo PROBED'; {WIZARD}", stubs)
    assert rc == 0 and "PROBED" in out and "TURN_ON" in out, out
    assert (d / "chown.log").read_text() == f"{IDS}\n"        # by number (#139 used "name:")
    conf = d / "teaport-sip.conf"
    st = subprocess.run(["stat", "-c", "%U %a", conf], capture_output=True, text=True).stdout.strip()
    assert st == f"{me} 600", st
    body = conf.read_text()
    assert "username=bob" in body and "password=s3cret" in body and "sip_port=5060" in body, body
    assert _temps(d) == [], _temps(d)


def test_wizard_as_root_refuses_an_ownerless_new_conf_before_the_test_registration():
    for unit in ("UNIT_LOAD=not-found", "UNIT_USER=no-such-user-xyz"):
        d = _secrets()
        rc, out = _run(d, f"{unit}; PROBE_HOOK='echo PROBED'; {WIZARD}", WIZARD_STUBS + ROOT_STUBS)
        assert rc == 1 and "gateway cannot open" in out and "nothing tested" in out, out
        assert out.count("gateway cannot open") == 1, out
        assert "PROBED" not in out and "TURN_ON" not in out, out
        assert not (d / "teaport-sip.conf").exists() and _temps(d) == [], _temps(d)


def test_a_signal_mid_wizard_write_removes_the_staged_conf():
    for sig, code in (("TERM", 143), ("INT", 130)):
        for installed in (False, True):
            d = _secrets()
            conf = d / "teaport-sip.conf"
            if installed:
                conf.write_text("password=old\n"); os.chmod(conf, 0o600)
            stub = WIZARD_STUBS + f'mv() {{ ls {d} | grep -q "teaport-sip.conf\\." && echo STAGED; kill -{sig} $$; sleep 1; }}'
            rc, out = _run(d, WIZARD, stub)
            assert rc == code and "STAGED" in out and "TURN_ON" not in out, (sig, rc, out)
            assert _temps(d) == [], (sig, _temps(d))
            assert conf.exists() == installed and (not installed or conf.read_text() == "password=old\n")


def test_wizard_replaces_an_installed_conf_by_rename_and_leaves_it_0600():
    d = _secrets()
    conf = d / "teaport-sip.conf"
    conf.write_text("password=old\n"); os.chmod(conf, 0o640)
    # A failed rename leaves the old conf as it was, mode and all, and no temp.
    rc, out = _run(d, WIZARD, WIZARD_STUBS + "mv() { return 1; }")
    assert rc == 1 and f"could not write {conf}" in out and "TURN_ON" not in out, out
    assert conf.read_text() == "password=old\n" and oct(conf.stat().st_mode & 0o777) == "0o640"
    assert _temps(d) == [], _temps(d)
    # A good one installs a new inode (a reader holding the old conf still reads the old
    # bytes: never truncated in place) and forces 0600 over the operator's 0640.
    old_ino = conf.stat().st_ino
    with open(conf) as held:
        rc, out = _run(d, WIZARD, WIZARD_STUBS)
        assert rc == 0 and "TURN_ON" in out and "mode 600" in out, out
        assert held.read() == "password=old\n"
    assert conf.stat().st_ino != old_ino and oct(conf.stat().st_mode & 0o777) == "0o600"
    assert "password=s3cret" in conf.read_text() and _temps(d) == []


def test_wizard_fills_its_temp_only_once_it_is_0600():
    d = _secrets()
    conf = d / "teaport-sip.conf"
    conf.write_text("password=old\n"); os.chmod(conf, 0o640)
    # The filler notes the mode of the temp it is writing the new password into.
    fill = f'sip_emit_conf() {{ stat -c %a {d}/teaport-sip.conf.?????? > {d}/fill.mode; echo password=new; }}'
    rc, out = _run(d, WIZARD, WIZARD_STUBS + fill)
    assert rc == 0, out
    assert (d / "fill.mode").read_text() == "600\n"
    assert conf.read_text() == "password=new\n" and oct(conf.stat().st_mode & 0o777) == "0o600"


def test_wizard_refuses_an_ownerless_root_conf_before_prompting():
    d = _secrets()
    stubs = WIZARD_STUBS + ROOT_STUBS + 'read() { echo PROMPTED; }'
    rc, out = _run(d, "UNIT_LOAD=not-found; PROBE_HOOK='echo PROBED'; sip_configure", stubs)
    assert rc == 1 and "gateway cannot open" in out and "nothing tested" in out, out
    assert "PROMPTED" not in out and "PROBED" not in out, out
    assert not (d / "teaport-sip.conf").exists() and _temps(d) == [], _temps(d)


def test_a_root_owned_conf_goes_to_the_units_user_when_root_rewrites_it():
    # A box the pre-#134 wizard left with a root:root conf: as root, the rewrite hands it
    # to the unit's User=; as anyone else it is refused with the sudo -E hint. Not really
    # root, so the conf's owner is what stat reports.
    me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    d = _secrets()
    conf = d / "teaport-sip.conf"
    conf.write_text("password=old\n"); os.chmod(conf, 0o600)
    root_owned = f'stat() {{ [ "$*" = "-c %u:%g {conf.resolve()}" ] && echo 0:0 || command stat "$@"; }}\n'
    chown_log = f'chown() {{ echo "$1" >> {d}/chown.log; command chown "$@"; }}\n'
    rc, out = _run(d, WIZARD, WIZARD_STUBS + root_owned)
    assert rc == 1 and "sudo -E" in out and "TURN_ON" not in out, out
    assert conf.read_text() == "password=old\n" and _temps(d) == []
    rc, out = _run(d, f"UNIT_USER={me}; {WIZARD}", WIZARD_STUBS + ROOT_STUBS + root_owned + chown_log)
    assert rc == 0 and "TURN_ON" in out, out
    assert (d / "chown.log").read_text() == f"{IDS}\n"
    assert "password=s3cret" in conf.read_text() and _temps(d) == []


def _fake_gateway(d):
    # Stands in for the gateway binary: a probe that reaches it leaves a mark.
    gw = d / "gw"
    gw.write_text(f"#!/bin/sh\ntouch {d}/probed\n"); os.chmod(gw, 0o755)
    return gw


def test_a_filler_failing_with_2_never_names_the_wizards_password():
    # Only a source file is ever named in "could not read": the wizard's filler arguments
    # hold the password, so a 2 from it reads as a failed write.
    d = _secrets()
    fail = "sip_emit_conf() { return 2; }"
    probe = f"SIP_GATEWAY={_fake_gateway(d)}; sip_test_register sbc.example.com sbc.example.com bob s3cret"
    rc, out = _run(d, probe, fail)
    assert rc == 1 and "could not write a probe conf" in out and "s3cret" not in out, out
    assert not (d / "probed").exists()
    rc, out = _run(d, WIZARD, WIZARD_STUBS + fail)
    assert rc == 1 and f"could not write {d}/teaport-sip.conf" in out and "s3cret" not in out, out
    assert "TURN_ON" not in out and _temps(d) == []


def test_a_conf_someone_else_owns_is_refused_before_prompts_or_probe():
    # As anyone but root, a conf another user owns (root:root, say) cannot be rewritten:
    # said up front, not after the password prompt and the ~8 s test registration. The
    # probe is the real one, up to a stand-in gateway. Not really root's, so the conf's
    # owner is what stat reports.
    d = _secrets()
    conf = d / "teaport-sip.conf"
    conf.write_text("password=old\n"); os.chmod(conf, 0o600)
    src = d / "src.conf"; src.write_text(CONF)
    gw = _fake_gateway(d)
    stubs = (f'stat() {{ [ "$*" = "-c %u:%g {conf.resolve()}" ] && echo 0:0 || command stat "$@"; }}\n'
             'sip_refuse_hand_launched() { :; }\nread() { echo PROMPTED; }\n')
    for steps in ("sip_configure", WIZARD, f"sip_adopt_conf {src}"):
        rc, out = _run(d, f"SIP_GATEWAY={gw}; {steps}", stubs)
        assert rc == 1 and "sudo -E" in out and "nothing tested" in out, (steps, out)
        assert "PROMPTED" not in out and "test-registering" not in out, (steps, out)
        assert not (d / "probed").exists(), steps
        assert conf.read_text() == "password=old\n" and _temps(d) == [], steps


def test_adopt_says_which_file_it_installed():
    d = _secrets()
    src = d / "src.conf"; src.write_text(CONF)
    rc, out = _run(d, f"sip_adopt_conf {src}", ADOPT_STUBS)
    assert rc == 0 and f"installed {src} -> {d}/teaport-sip.conf (" in out, out
    rc, out = _run(d, WIZARD, WIZARD_STUBS)
    assert rc == 0 and f"installed {d}/teaport-sip.conf (" in out and "->" not in out, out


def _root_owned(conf):
    return f'stat() {{ [ "$*" = "-c %u:%g {conf.resolve()}" ] && echo 0:0 || command stat "$@"; }}\n'


def test_root_reads_the_units_user_as_systemd_does():
    # Checked with transient units on systemd 255: an empty User= runs as root (which
    # reads a root:root conf: nothing to refuse); an unknown User= (name or number) fails
    # every start with 217/USER, an unknown Group= with 216/GROUP; a unit that is not
    # installed shows an empty User= too, and is no answer.
    unused_uid = next(u for u in range(4_000_000, 4_001_000) if not _has(pwd.getpwuid, u))
    cases = [  # (unit, root:root conf installed?, refused with)
        ("UNIT_USER=''", False, None), ("UNIT_USER=''", True, None),
        ("UNIT_USER=no-such-user-xyz", True, "217/USER"), ("UNIT_USER=no-such-user-xyz", False, "217/USER"),
        (f"UNIT_USER={unused_uid}", True, "217/USER"),
        (f"UNIT_USER={ME} UNIT_GROUP=no-such-group-xyz", True, "216/GROUP"),
        ("UNIT_LOAD=not-found", True, "not installed"), ("UNIT_LOAD=not-found", False, "not installed"),
    ]
    for unit, installed, refused in cases:
        d = _secrets()
        conf = d / "teaport-sip.conf"
        stubs = WIZARD_STUBS + ROOT_STUBS
        if installed:
            conf.write_text("password=old\naec=true\n"); os.chmod(conf, 0o600)
            stubs += _root_owned(conf)
        rc, out = _run(d, f"{unit}; PROBE_HOOK='echo PROBED'; sip_conf_preflight && echo PREFLIGHT_OK", stubs)
        if refused is None:
            assert rc == 0 and "PREFLIGHT_OK" in out and "error" not in out, (unit, installed, out)
            continue
        assert rc == 1 and refused in out and "nothing tested" in out, (unit, installed, out)
        rc, out = _run(d, f"{unit}; PROBE_HOOK='echo PROBED'; {WIZARD}", stubs)
        assert rc == 1 and refused in out and "PROBED" not in out and "TURN_ON" not in out, (unit, out)
        if installed:   # `sip aec` too, with no probe to come before it: one message, conf as it was
            rc, out = _run(d, f"{unit}; sip_aec off", stubs + AEC_STUBS)
            assert rc == 1 and refused in out and "aec left at true" in out, (unit, out)
            assert out.count("error:") == 1 and "SUDO" not in out, out
            assert conf.read_text() == "password=old\naec=true\n" and _temps(d) == []


def _has(lookup, key):
    try:
        lookup(key)
        return True
    except KeyError:
        return False


def test_a_numeric_unit_user_and_group_are_given_by_number():
    # `chown 1000:` (the login-group form) is "invalid spec" for a number. Not really root,
    # so the user is ourselves, by uid; the group, when the unit names one, one of ours.
    others = [g for g in os.getgroups() if g != os.getgid()]
    gid = others[0] if others else os.getgid()
    for unit, want_gid in ((f"UNIT_USER={os.getuid()}", os.getgid()),
                           (f"UNIT_USER={os.getuid()} UNIT_GROUP={gid}", gid),
                           (f"UNIT_USER={ME} UNIT_GROUP={grp.getgrgid(gid).gr_name}", gid)):
        d = _secrets()
        rc, out = _run(d, f"{unit}; {WIZARD}", WIZARD_STUBS + ROOT_STUBS)
        assert rc == 0 and "TURN_ON" in out, (unit, out)
        st = (d / "teaport-sip.conf").stat()
        assert (st.st_uid, st.st_gid, st.st_mode & 0o777) == (os.getuid(), want_gid, 0o600), unit


def test_preflight_and_staging_give_one_answer():
    # sip_conf_preflight (before the ~8 s registration) and the staging (sip aec, the
    # install) ask ONE sip_conf_owner, so they refuse the same confs. Not really in a
    # foreign group, so the conf's group is what stat reports.
    foreign_gid = next(g for g in range(4_000_000, 4_001_000) if g not in os.getgroups())
    cases = [  # (name, setup, refused with)
        ("mine", "", None),
        ("read-only to its owner", "chmod 400 {conf}", None),    # a rename does not need it
        ("someone else's", "own 0:0", "sudo -E"),
        ("a group I am not in", f"own {os.getuid()}:{foreign_gid}", "which"),
        ("a directory I cannot write", "chmod 500 {d}", "cannot write to"),
    ]
    for name, setup, refused in cases:
        d = _secrets()
        conf = d / "teaport-sip.conf"
        conf.write_text("password=old\naec=true\n"); os.chmod(conf, 0o600)
        stubs = WIZARD_STUBS + AEC_STUBS
        if setup.startswith("own "):
            stubs += f'stat() {{ [ "$*" = "-c %u:%g {conf.resolve()}" ] && echo {setup[4:]} || command stat "$@"; }}\n'
        elif setup:
            subprocess.run(setup.format(conf=conf, d=d), shell=True, check=True)
        rc1, pre = _run(d, "sip_conf_preflight && echo PREFLIGHT_OK", stubs)
        rc2, aec = _run(d, "sip_aec off", stubs)
        rc3, wiz = _run(d, f"PROBE_HOOK='echo PROBED'; {WIZARD}", stubs)
        if refused is None:
            assert rc1 == 0 and "PREFLIGHT_OK" in pre, (name, pre)
            assert rc2 == 0 and "aec: true -> false" in aec, (name, aec)
            assert rc3 == 0 and "PROBED" in wiz and "TURN_ON" in wiz, (name, wiz)
        else:
            assert rc1 == 1 and refused in pre and "nothing tested" in pre, (name, pre)
            assert rc2 == 1 and refused in aec and "aec left at true" in aec, (name, aec)
            assert rc3 == 1 and refused in wiz and "PROBED" not in wiz, (name, wiz)
            assert conf.read_text() == "password=old\naec=true\n", name
        os.chmod(d, 0o700)
        assert _temps(d) == [], (name, _temps(d))
    # The read-only one kept its mode through `sip aec` (and the wizard's 0600 after).
    d = _secrets()
    conf = d / "teaport-sip.conf"
    conf.write_text("aec=true\n"); os.chmod(conf, 0o400)
    rc, out = _run(d, "sip_aec off", AEC_STUBS)
    assert rc == 0 and conf.read_text() == "aec=false\n" and oct(conf.stat().st_mode & 0o777) == "0o400", out


# mktemp as it would be if the signal landed just after it made the file: the parent is
# signalled, mktemp still prints the name.
SIGNAL_IN_MKTEMP = r'''
mktemp() { local f; f="$(command mktemp "$@")" || return; case " $* " in *" -u "*) ;; *) kill -"$MKTEMP_SIG" $$; sleep 0.2 ;; esac; printf '%s\n' "$f"; }
'''


def test_a_signal_while_a_temp_is_made_leaves_nothing():
    # `t="$(mktemp)"` with the trap set after it returned: a TERM in between (as root, a
    # systemctl call long) kept the temp. Every temp is listed before a signal can act.
    for sig, code in (("TERM", 143), ("INT", 130)):
        d = _secrets()
        conf = d / "teaport-sip.conf"
        conf.write_text(CONF); os.chmod(conf, 0o600)
        rc, out = _run(d, f"MKTEMP_SIG={sig}; sip_conf_set {conf} aec false", SIGNAL_IN_MKTEMP)
        assert rc == code and _temps(d) == [] and conf.read_text() == CONF, (sig, rc, out, _temps(d))
        # The probe's throwaway conf and its log, in TMPDIR.
        tmp = d / "tmp"; tmp.mkdir()
        probe = f"MKTEMP_SIG={sig}; SIP_GATEWAY={_fake_gateway(d)}; sip_test_register h d u p"
        rc, out = _run(d, probe, SIGNAL_IN_MKTEMP, {"TMPDIR": str(tmp)})
        assert rc == code and list(tmp.iterdir()) == [] and not (d / "probed").exists(), (sig, rc, out)


def test_a_signal_in_the_probe_leaves_nothing_and_no_gateway():
    # The probe's log was made and only then trapped (after a printf), and its gateway is
    # started with SIGINT ignored: both are on the cleanup list from the moment they exist.
    for sig, code, hook in (("TERM", 143, "confval"), ("INT", 130, "confval"), ("TERM", 143, "sleep")):
        d = _secrets()
        tmp = d / "tmp"; tmp.mkdir()
        gw = d / "gw"
        gw.write_text(f"#!/bin/sh\necho $$ > {d}/gw.pid\nexec sleep 30\n"); os.chmod(gw, 0o755)
        # confval runs while the label is printed, before the gateway starts; sleep, in the
        # wait for its 200 OK, once the gateway is up.
        wait = f"while [ ! -s {d}/gw.pid ]; do command sleep 0.05; done; " if hook == "sleep" else ""
        stub = f'{hook}() {{ {wait}kill -{sig} $$; command sleep 0.2; }}\n'
        rc, out = _run(d, f"SIP_GATEWAY={gw}; sip_test_register h d u p", stub, {"TMPDIR": str(tmp)})
        assert rc == code and list(tmp.iterdir()) == [], (sig, hook, rc, out, list(tmp.iterdir()))
        assert (d / "gw.pid").exists() == (hook == "sleep"), hook
        if hook == "sleep":
            pid = (d / "gw.pid").read_text().strip()
            gone = "for i in $(seq 20); do kill -0 %s 2>/dev/null || exit 0; sleep 0.05; done; exit 1" % pid
            assert subprocess.run(["bash", "-c", gone]).returncode == 0, (hook, pid)


def test_a_writer_inside_a_probe_keeps_the_probes_cleanup():
    # One trap over one list: a writer that runs while the probe's conf is listed adds
    # and drops its own temp, and the probe's conf is still removed by a later signal.
    # Per-writer traps replaced the probe's and then cleared it.
    d = _secrets()
    tmp = d / "tmp"; tmp.mkdir()
    conf = d / "teaport-sip.conf"
    conf.write_text("aec=true\n"); os.chmod(conf, 0o600)
    nest = f'nest() {{ sip_conf_set {conf} aec false; kill -TERM $$; sleep 1; }}\n'
    rc, out = _run(d, f'SIP_GATEWAY={_fake_gateway(d)}; sip_probe_with label "" nest', nest, {"TMPDIR": str(tmp)})
    assert rc == 143 and list(tmp.iterdir()) == [], (rc, out, list(tmp.iterdir()))
    assert conf.read_text() == "aec=false\n" and _temps(d) == []
    rc, out = _run(d, f"sip_mktemp a; sip_mktemp b; sip_drop $b; rm -f $b; trap -p EXIT; echo $a > {d}/a",
                   "", {"TMPDIR": str(tmp)})
    assert rc == 0 and "sip_cleanup" in out and list(tmp.iterdir()) == [], (out, list(tmp.iterdir()))


def test_a_refused_rewrite_is_one_message():
    # A note from the staging and then "could not write …" said it twice.
    d = _secrets()
    conf = d / "teaport-sip.conf"
    conf.write_text("password=old\naec=true\n"); os.chmod(conf, 0o600)
    rc, out = _run(d, "sip_aec off", AEC_STUBS + _root_owned(conf))
    out = out.replace("CHECK\n", "")        # the stubbed units check
    assert rc == 1 and out.count("\n") == 1 and out.count("error:") == 1 and "note:" not in out, out
    assert "sudo -E" in out and "aec left at true" in out, out
    # The install itself (past the preflight: an owner that changed since, say).
    rc, out = _run(d, "sip_go_live 600 '' sip_emit_conf h d u p", WIZARD_STUBS + _root_owned(conf))
    assert rc == 1 and out.count("\n") == 1 and "sudo -E" in out and "unchanged" in out, out
    assert "could not write" not in out and "note:" not in out, out
    assert conf.read_text() == "password=old\naec=true\n" and _temps(d) == []


def test_a_missing_conf_directory_is_made_for_the_gateway():
    # As root, `mkdir -p "$SECRETS"` made it root:root 0700, which the gateway cannot get
    # into to open the conf given to it. Every directory made goes to the conf's owner
    # (by number, -h: never through a link), the conf's own 0700. Not really root, so the
    # owner is ourselves: the chown is what shows it asked.
    d = _secrets()
    secrets = d / "home" / ".config" / "teaport"
    log = f'chown() {{ echo "$*" >> {d}/chown.log; command chown "$@"; }}\n'
    rc, out = _run(secrets, f"UNIT_USER={ME}; {WIZARD}", WIZARD_STUBS + ROOT_STUBS + log)
    assert rc == 0 and "TURN_ON" in out, out
    made = f"-h {IDS} -- {secrets} {secrets.parent} {d / 'home'}"
    assert made in (d / "chown.log").read_text().splitlines(), (d / "chown.log").read_text()
    assert oct(secrets.stat().st_mode & 0o777) == "0o700" and (secrets / "teaport-sip.conf").exists()
    # A directory that exists is left as the operator set it (not re-chmodded 0700).
    os.chmod(secrets, 0o750)
    rc, out = _run(secrets, WIZARD, WIZARD_STUBS)
    assert rc == 0 and oct(secrets.stat().st_mode & 0o777) == "0o750", out


def test_configure_refuses_a_conf_the_unit_does_not_read():
    # Plain `sudo` resets HOME: $SIP_CONF named /root/.config/teaport/teaport-sip.conf, a
    # conf the unit (ConditionPathExists= the user's) never reads.
    d = _secrets()
    src = d / "src.conf"; src.write_text(CONF)
    stubs = WIZARD_STUBS + ADOPT_STUBS + ROOT_STUBS + "read() { echo PROMPTED; }\n"
    for steps in ("sip_configure", WIZARD, f"sip_adopt_conf {src}"):
        rc, out = _run(d, f"UNIT_USER={ME}; UNIT_CONF=/home/someone/.config/teaport/teaport-sip.conf; "
                          f"PROBE_HOOK='echo PROBED'; {steps}", stubs)
        assert rc == 1 and "reads /home/someone/.config/teaport/teaport-sip.conf" in out, (steps, out)
        assert "nothing tested" in out and "PROMPTED" not in out and "PROBED" not in out, (steps, out)
        assert not (d / "teaport-sip.conf").exists() and _temps(d) == [], steps
    # The unit's own conf goes ahead with the SIP guard's drop-in condition after it
    # (UNIT_STUB): every ConditionPathExists= counts, not the last one.
    rc, out = _run(d, f"UNIT_USER={ME}; UNIT_CONF={d}/teaport-sip.conf; {WIZARD}", stubs)
    assert rc == 0 and "TURN_ON" in out, out
    # An empty ConditionPathExists= resets the list, as systemd reads it.
    reset = (f'systemctl() {{ [ "$*" = "cat teaport-sip.service" ] || return 1; printf "%s\\n" '
             f'"ConditionPathExists={d}/teaport-sip.conf" "ConditionPathExists=" "ConditionPathExists=!/x/guard"; }}\n')
    rc, out = _run(d, f"sip_conf_preflight && echo PREFLIGHT_OK", stubs + reset)
    assert rc == 1 and "reads /x/guard," in out and "PREFLIGHT_OK" not in out, out


def _popen(secrets, steps, stubs="", env_extra=None):
    # As _run, in a session of its own, so its whole process group can be signalled as a
    # Ctrl-C would be.
    script = _functions() + OLD_SET + UNIT_STUB + stubs + "\n" + steps + "\n"
    env = dict(os.environ, TEAPORT_SECRETS_DIR=str(secrets), HOME=str(secrets), **(env_extra or {}))
    env.pop("TEAPORT_SIP_CONF", None)
    return subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env, start_new_session=True)


def _wait_for(path, secs=5.0):
    for _ in range(int(secs / 0.02)):
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def test_a_ctrl_c_to_the_group_while_mktemp_runs_leaves_no_temp():
    # A signal to the process group reaches mktemp too: run with INT/TERM as they are, it
    # can die after making the file and before printing its name, and nobody can remove
    # it. sip_mktemp runs it with them ignored, so it finishes and the temp is listed.
    real = subprocess.run(["bash", "-c", "command -v mktemp"], capture_output=True, text=True).stdout.strip()
    for sig, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
        d = _secrets()
        conf = d / "teaport-sip.conf"
        conf.write_text(CONF); os.chmod(conf, 0o600)
        bindir = d / "bin"; bindir.mkdir()
        (bindir / "mktemp").write_text(f'#!/bin/sh\nf="$({real} "$@")" || exit 1\ntouch {d}/made\nsleep 0.5\n'
                                       'printf "%s\\n" "$f"\n')
        os.chmod(bindir / "mktemp", 0o755)
        p = _popen(d, f"sip_conf_set {conf} aec false", env_extra={"PATH": f"{bindir}:{os.environ['PATH']}"})
        _wait_for(d / "made")
        os.killpg(p.pid, sig)
        out, _ = p.communicate(timeout=10)
        assert p.returncode == code and _temps(d) == [], (sig, p.returncode, out, _temps(d))
        assert conf.read_text() == CONF


def _gateway(d, body):
    gw = d / "gw"
    gw.write_text(f"#!/bin/sh\n{body}\n"); os.chmod(gw, 0o755)
    return gw


def test_a_ctrl_c_after_the_probes_gateway_exited_still_cleans_up():
    # The gateway exits (a probe that could not bind :5062) and bash reaps it, so the
    # cleanup's kill fails; under set -e that ended the trap before the rm, and the exit
    # status became 1.
    d = _secrets()
    tmp = d / "tmp"; tmp.mkdir()
    gw = _gateway(d, f"touch {d}/up; sleep 0.3")
    sleep = f'sleep() {{ command sleep 1; [ -e {d}/up ] && kill -INT $$; command sleep 1; }}\n'
    rc, out = _run(d, f"SIP_GATEWAY={gw}; sip_test_register h d u p", sleep, {"TMPDIR": str(tmp)})
    assert rc == 130 and list(tmp.iterdir()) == [], (rc, out, list(tmp.iterdir()))


def test_a_ctrl_c_in_the_probe_removes_the_socket_its_gateway_made():
    # The test socket is the gateway's to make, so it is listed before the gateway starts.
    d = _secrets()
    tmp = d / "tmp"; tmp.mkdir()
    gw = _gateway(d, f'uds="$(sed -n "s/^uds_path=//p" "$1")"; touch "$uds"; touch {d}/up; exec sleep 30')
    sleep = f'sleep() {{ while [ ! -e {d}/up ]; do command sleep 0.05; done; kill -INT $$; command sleep 1; }}\n'
    rc, out = _run(d, f"SIP_GATEWAY={gw}; sip_test_register h d u p", sleep, {"TMPDIR": str(tmp)})
    assert rc == 130 and list(tmp.iterdir()) == [], (rc, out, list(tmp.iterdir()))


def test_a_new_conf_is_refused_when_the_gateway_runs_as_someone_else():
    # Not root: a new conf is the invoker's 0600, which a gateway run as a third user
    # cannot open. Said before the test registration.
    d = _secrets()
    rc, out = _run(d, f"UNIT_USER=nobody; PROBE_HOOK='echo PROBED'; {WIZARD}", WIZARD_STUBS)
    assert rc == 1 and "runs as nobody" in out and "gateway cannot open" in out, out
    assert "PROBED" not in out and not (d / "teaport-sip.conf").exists() and _temps(d) == [], out


def test_a_copy_that_did_not_come_out_right_is_not_installed():
    # A chown the filesystem ignored (vfat; root_squash NFS refuses it): the staged copy
    # is read back, and one that does not have the owner it was given is refused (3).
    d = _secrets()
    conf = d / "teaport-sip.conf"
    conf.write_text("aec=true\n"); os.chmod(conf, 0o600)
    stubs = ROOT_STUBS + _root_owned(conf) + "chown() { :; }\n"
    rc, out = _run(d, f'UNIT_USER=nobody; sip_conf_set {conf} aec false || echo "rc=$? $SIP_WHY"', stubs)
    assert "rc=3 a copy of" in out, out
    assert conf.read_text() == "aec=true\n" and _temps(d) == [], out


def test_a_world_readable_conf_is_closed_to_others_by_a_rewrite():
    d = _secrets()
    conf = d / "teaport-sip.conf"
    conf.write_text("password=p\naec=true\n"); os.chmod(conf, 0o644)
    rc, out = _run(d, "sip_aec off", AEC_STUBS)
    assert rc == 0 and "aec=false" in conf.read_text(), out
    assert oct(conf.stat().st_mode & 0o777) == "0o640", oct(conf.stat().st_mode)


# ---- #155: a re-configure keeps the operator's gateway settings -------------------------

WIZARD2 = "sip_configure --host sbc2.example.com --user carol --password n3w-s3cret --yes"
# What the operator added to a wizard's conf, by hand or with `sip aec`: the source
# allowlist (teaport-sip#33), its comment, a repeated key (one line comma-separated),
# keys this CLI has never written, and a wizard key changed.
OPERATOR = """# trunk call-delivery SBCs, captured 2026-10-12 (teaport-sip#37)
sip_source_filter = enforce   # was log
sip_source_allow = 192.0.2.10, 192.0.2.11
nameserver = 192.0.2.53
sip_source_allow=198.51.100.0/24
capture_dir = /var/tmp/cap
"""
OWNED = ("id_uri", "registrar_uri", "username", "password", "realm", "sip_port", "uds_path",
         "register", "auto_answer")


def _gateway_view(text):
    # The gateway's parser (config.h loadConfig): `#` cuts a line, trimmed, a [section] or
    # a line without `=` skipped, the last of a key wins but for the two that add up.
    vals, lists = {}, {"sip_source_allow": [], "nameserver": []}
    for line in text.split("\n"):
        line = line.split("#", 1)[0].strip(" \t\r\n")
        if not line or line.startswith("[") or "=" not in line:
            continue
        k, v = (x.strip(" \t\r\n") for x in line.split("=", 1))
        if k in lists:
            lists[k] += [x for x in re.split(r"[, \t]+", v) if x]
        else:
            vals[k] = v
    return vals, lists


def _keys(text):
    return [line.split("=", 1)[0].strip() for line in text.split("\n")
            if "=" in line.split("#", 1)[0] and not line.lstrip().startswith("#")]


def _probing_gateway(d):
    # Registers at once, and keeps the conf it was handed: the probe's, to hold up
    # against what is installed.
    return _gateway(d, f'cp "$1" {d}/probe.conf; echo "[reg] REGISTERED code=200 OK"; exec sleep 30')


LIVE_STUBS = "sip_refuse_hand_launched() { :; }\nsip_turn_on() { echo TURN_ON; }\nsip_wait_registered() { :; }\n"


def test_reconfigure_keeps_the_operators_settings_and_tests_them():
    d = _secrets()
    tmp = d / "tmp"; tmp.mkdir()
    conf = d / "teaport-sip.conf"
    rc, out = _run(d, WIZARD, WIZARD_STUBS)
    assert rc == 0, out
    first = conf.read_text()
    conf.write_text(first.replace("aec=true\n", "aec=false\n") + OPERATOR); os.chmod(conf, 0o640)
    before = conf.read_text()
    # The real test registration, up to a stand-in gateway.
    rc, out = _run(d, f"SIP_GATEWAY={_probing_gateway(d)}; {WIZARD2}", LIVE_STUBS, {"TMPDIR": str(tmp)})
    assert rc == 0 and "TURN_ON" in out and "registration OK" in out, out
    after = conf.read_text()
    vals, lists = _gateway_view(after)
    # Kept: every operator line as it was, in its order, the repeated key's every line.
    assert after.endswith(OPERATOR), after
    assert lists["sip_source_allow"] == ["192.0.2.10", "192.0.2.11", "198.51.100.0/24"], lists
    assert lists["nameserver"] == ["192.0.2.53"] and vals["capture_dir"] == "/var/tmp/cap", vals
    assert vals["sip_source_filter"] == "enforce" and vals["aec"] == "false", vals
    # Replaced: the trunk's and the units' keys, each once; the old password gone.
    assert {k: vals[k] for k in OWNED} == {
        "id_uri": "sip:carol@sbc2.example.com", "registrar_uri": "sip:sbc2.example.com",
        "username": "carol", "password": "n3w-s3cret", "realm": "*", "sip_port": "5060",
        "uds_path": "/run/teaport/teaport-sip.sock", "register": "true", "auto_answer": "false"}, vals
    assert all(_keys(after).count(k) == 1 for k in OWNED) and "s3cret\n" not in after.replace("n3w-s3cret\n", ""), after
    # Nothing else changed: the old conf with the four answers' lines rewritten in place.
    old = dict(_gateway_view(before)[0])
    swapped = before
    for k in ("id_uri", "registrar_uri", "username", "password"):
        swapped = swapped.replace(f"{k}={old[k]}\n", f"{k}={vals[k]}\n")
    assert after == swapped, (after, swapped)
    # What was tested is what was installed, but for the probe's port and socket.
    probe = (d / "probe.conf").read_text()
    probe = re.sub(r"(?m)^sip_port=5062$", "sip_port=5060", probe)
    probe = re.sub(r"(?m)^uds_path=.*$", "uds_path=/run/teaport/teaport-sip.sock", probe)
    assert probe == after, (probe, after)
    # Mode and inode as for any wizard install; nothing left in TMPDIR or beside the conf.
    assert oct(conf.stat().st_mode & 0o777) == "0o600" and list(tmp.iterdir()) == [] and _temps(d) == []
    # Said: what it keeps (names, never values), and that enforce stays, with its list.
    assert "keeping the settings in" in out and "sip_source_allow (2 lines)" in out, out
    assert "sip_source_filter = enforce is kept, with its allowlist: 3 sip_source_allow entries" in out, out
    assert "the registrar changes (sip:sbc.example.com -> sip:sbc2.example.com)" in out, out
    for secret in ("s3cret", "192.0.2.", "198.51.100", "/var/tmp/cap"):
        assert secret not in out, (secret, out)


def test_reconfigure_rewrites_only_the_keys_it_owns():
    d = _secrets()
    conf = d / "teaport-sip.conf"
    # Over a wizard's conf nobody touched, the same answers write it byte for byte, and a
    # new password changes its one line.
    rc, out = _run(d, WIZARD, WIZARD_STUBS)
    assert rc == 0, out
    first = conf.read_bytes()
    rc, out = _run(d, WIZARD, WIZARD_STUBS)
    assert rc == 0 and conf.read_bytes() == first, out
    rc, out = _run(d, WIZARD.replace("s3cret", "other"), WIZARD_STUBS)
    assert rc == 0 and conf.read_bytes() == first.replace(b"password=s3cret", b"password=other"), out
    # Over a hand-written one: every owned key set, its LAST line rewritten in place and
    # earlier ones dropped (the parser takes the last), whatever its spacing; the
    # wizard's other keys added where missing; the rest as it was. A hand conf's
    # register = false and auto_answer = true are the units' to set.
    hand = ("[sip]\n# mine\npassword = old1\nbind_addr = 192.168.1.105\nrealm = acme\n"
            "  sip_port\t= 5070   # hand launch\nregister = false\nuds_path=/tmp/old.sock\n"
            "auto_answer=true\npassword=old2\n#password=old3\nlog_level = 5\nsip_source_filter=log\n")
    conf.write_text(hand); os.chmod(conf, 0o600)
    rc, out = _run(d, WIZARD, WIZARD_STUBS)
    assert rc == 0, out
    assert conf.read_text() == (
        "[sip]\n# mine\nbind_addr = 192.168.1.105\nrealm=*\nsip_port=5060\nregister=true\n"
        "uds_path=/run/teaport/teaport-sip.sock\nauto_answer=false\npassword=s3cret\n#password=old3\n"
        "log_level = 5\nsip_source_filter=log\nid_uri=sip:bob@sbc.example.com\n"
        "registrar_uri=sip:sbc.example.com\nusername=bob\nreg_timeout=300\naec=true\naec_tail_ms=256\n"
        "app_log_level=3\n"), conf.read_text()
    # No conf: the template, as before.
    d2 = _secrets()
    rc, out = _run(d2, WIZARD, WIZARD_STUBS)
    assert rc == 0 and (d2 / "teaport-sip.conf").read_bytes() == first and "keeping" not in out, out


def test_reconfigure_as_root_keeps_the_settings_and_the_hand_over():
    # A pre-#134 root:root conf, rewritten as root: given to the unit's user as before,
    # and its operator settings kept.
    d = _secrets()
    conf = d / "teaport-sip.conf"
    conf.write_text(CONF + OPERATOR); os.chmod(conf, 0o600)
    chown_log = f'chown() {{ echo "$1" >> {d}/chown.log; command chown "$@"; }}\n'
    rc, out = _run(d, f"UNIT_USER={ME}; {WIZARD}", WIZARD_STUBS + ROOT_STUBS + _root_owned(conf) + chown_log)
    assert rc == 0 and "TURN_ON" in out, out
    assert (d / "chown.log").read_text() == f"{IDS}\n"
    body = conf.read_text()
    assert OPERATOR in body and "password=s3cret" in body and "hunter2" not in body, body
    assert oct(conf.stat().st_mode & 0o777) == "0o600" and _temps(d) == []


def test_reconfigure_refuses_a_conf_it_cannot_keep_before_prompting():
    # Writing the template over a conf it could not read is the silent loss itself: said
    # before a password is typed, the conf as it was.
    cases = [("NUL bytes", lambda c: c.write_bytes(b"password=old\nsip_source_filter=enforce\x00\n"), "NUL bytes")]
    if os.geteuid() != 0:   # root reads a 000 file
        cases.append(("unreadable", lambda c: (c.write_text("password=old\n"), os.chmod(c, 0)), "cannot read"))
    for name, setup, said in cases:
        d = _secrets()
        conf = d / "teaport-sip.conf"
        setup(conf)
        was = conf.read_bytes() if name != "unreadable" else None
        stubs = WIZARD_STUBS + "read() { echo PROMPTED; }\n"
        for steps in ("sip_configure", WIZARD):
            rc, out = _run(d, f"PROBE_HOOK='echo PROBED'; {steps}", stubs)
            assert rc == 1 and said in out and "nothing tested, nothing installed" in out, (name, out)
            assert "PROMPTED" not in out and "PROBED" not in out and "TURN_ON" not in out, (name, out)
            assert out.count("error:") == 1 and _temps(d) == [], (name, out)
        os.chmod(conf, 0o600)
        assert was is None or conf.read_bytes() == was, name


def test_reconfigure_refuses_a_conf_changed_during_the_test_registration():
    # The install would put back the conf as it was read before the probe: refused, and
    # the change left in place.
    d = _secrets()
    conf = d / "teaport-sip.conf"
    conf.write_text(CONF + "aec=true\n"); os.chmod(conf, 0o600)
    rc, out = _run(d, f"PROBE_HOOK='sip_conf_set {conf} aec false'; {WIZARD}", WIZARD_STUBS)
    assert rc == 1 and "changed during the test registration" in out and "TURN_ON" not in out, out
    assert conf.read_text() == CONF + "aec=false\n" and _temps(d) == [], conf.read_text()
    # Untouched, it goes ahead (the stamp is the conf's, not the clock's).
    rc, out = _run(d, f"PROBE_HOOK='cat {conf} >/dev/null'; {WIZARD}", WIZARD_STUBS)
    assert rc == 0 and "TURN_ON" in out and "aec=false" in conf.read_text(), out


def test_reconfigure_says_what_it_keeps_by_name_only():
    d = _secrets()
    conf = d / "teaport-sip.conf"
    # A key that is not one (a pasted secret), a mixed-case one, and enforce with no list.
    conf.write_text(CONF + "Tr0ub4dor&3 = horse\nAEC_X = 1\nsip_source_filter = ENFORCE\nnameserver = none\n")
    os.chmod(conf, 0o600)
    rc, out = _run(d, WIZARD, WIZARD_STUBS)
    assert rc == 0, out
    assert "configure does not set: bind_addr, sip_source_filter, nameserver, 2 more line(s) whose key is not shown" in out, out
    assert "sip_source_filter = enforce is kept: SIP requests are taken only from the registrar's addresses" in out, out
    assert "the registrar changes" not in out, out      # the same registrar
    for secret in ("Tr0ub4dor", "horse", "AEC_X", "hunter2", "s3cret"):
        assert secret not in out, (secret, out)
    assert "Tr0ub4dor&3 = horse\n" in conf.read_text() and "hunter2" not in conf.read_text()
    # Not enforce: nothing said about the filter.
    conf.write_text(CONF + "sip_source_filter = log\n"); os.chmod(conf, 0o600)
    rc, out = _run(d, WIZARD, WIZARD_STUBS)
    assert rc == 0 and "is kept:" not in out and "is kept," not in out and "sip_source_filter" in out, out


def test_a_refused_kept_setting_fails_the_test_registration_and_installs_nothing():
    # A kept value the gateway refuses (it exits 78) fails the probe like a bad password,
    # and the message points at the kept settings too.
    d = _secrets()
    tmp = d / "tmp"; tmp.mkdir()
    conf = d / "teaport-sip.conf"
    conf.write_text(CONF + "sip_source_filter = enforcing\n"); os.chmod(conf, 0o600)
    gw = _gateway(d, 'grep -q "^sip_source_filter = enforcing" "$1" && { echo "[config] sip_source_filter = \\"enforcing\\" is not one of enforce, log, off"; exit 78; }; echo "[reg] REGISTERED code=200 OK"; exec sleep 30')
    rc, out = _run(d, f"SIP_GATEWAY={gw}; {WIZARD}", LIVE_STUBS, {"TMPDIR": str(tmp)})
    assert rc == 1 and "registration FAILED" in out and "is not one of enforce, log, off" in out, out
    assert f"or a setting kept from {d}/teaport-sip.conf" in out and "TURN_ON" not in out, out
    assert conf.read_text() == CONF + "sip_source_filter = enforcing\n" and list(tmp.iterdir()) == []
    assert "s3cret" not in out and "hunter2" not in out, out


def test_status_shows_the_source_filter():
    d = _secrets()
    conf = d / "teaport-sip.conf"
    stubs = "svc_line() { :; }\nsip_line() { :; }\nsip_last_reg() { :; }\nactive() { echo inactive; }\n"
    for text, said, rc_want in (
            ("", "log (the default): requests from elsewhere are logged", 0),
            ("sip_source_filter = Log\n", "Log: requests from elsewhere are logged", 0),
            ("sip_source_filter = off # A/B\n", "off: no filter", 0),
            ("sip_source_filter=enforce\nsip_source_allow = 192.0.2.10\n",
             "enforce: SIP requests only from the registrar's addresses and 1 sip_source_allow entry", 0),
            ("sip_source_filter=enforce\nsip_source_allow = 192.0.2.10, 192.0.2.11 192.0.2.12\n"
             "#sip_source_allow = 192.0.2.99\nsip_source_allow=2001:db8::/32\n",
             "and 4 sip_source_allow entries", 0),
            ("sip_source_filter = enforcing\n", "not enforce, log or off: the gateway refuses to start on it", 1)):
        conf.write_text(CONF + text); os.chmod(conf, 0o600)
        rc, out = _run(d, "sip_status", stubs)
        assert rc == 0 and re.search(r"(?m)^  source filter +.*" + re.escape(said), out), (text, out)
        rc, out = _run(d, f'sip_filter_desc {conf} && echo " rc=0" || echo " rc=$?"', stubs)
        assert out.endswith(f" rc={rc_want}\n"), (text, out)
    if os.geteuid() != 0:
        os.chmod(conf, 0)
        rc, out = _run(d, f'sip_filter_desc {conf} && echo " rc=0" || echo " rc=$?"')
        assert "unknown (" in out and out.endswith(" rc=2\n"), out
        os.chmod(conf, 0o600)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
