#
# cli/teaport's SIP conf writers (issues #68, #64): the staged temps that hold the SIP
# password are removed on INT/TERM; a new conf written by root goes to the gateway
# unit's User=; an adopt that fails says whether it could not READ the source or could
# not WRITE the destination; the units' keys are forced in one pass that rewrites a conf
# exactly as the old one-key-at-a-time sip_conf_set did; `sip aec on|off` checks its
# preconditions once. And (#139's leftovers) the unit's User=/Group= read as systemd
# reads them (empty = root, numbers, unknown = never starts); one sip_conf_owner answer
# for preflight and staging; temps listed before a signal can act; the conf's directory
# made for the gateway; one cleanup trap that nests; one message per refusal.
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
import subprocess

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
# UNIT_GROUP, and a ConditionPathExists=UNIT_CONF when one is set.
UNIT_STUB = r'''
systemctl() { case "$*" in
  "show -p LoadState --value teaport-sip.service") printf '%s\n' "${UNIT_LOAD-loaded}" ;;
  "show -p User --value teaport-sip.service") printf '%s\n' "${UNIT_USER-}" ;;
  "show -p Group --value teaport-sip.service") printf '%s\n' "${UNIT_GROUP-}" ;;
  "cat teaport-sip.service") [ -n "${UNIT_CONF-}" ] && printf 'ConditionPathExists=%s\n' "$UNIT_CONF" ;;
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
    rc, out = _run(d, f"UNIT_USER={ME}; UNIT_CONF={d}/teaport-sip.conf; {WIZARD}", stubs)
    assert rc == 0 and "TURN_ON" in out, out


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
