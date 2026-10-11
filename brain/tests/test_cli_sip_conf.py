#
# cli/teaport's SIP conf writers (issues #68, #64): the staged temps that hold the SIP
# password are removed on INT/TERM; a new conf written by root goes to the gateway
# unit's User=; an adopt that fails says whether it could not READ the source or could
# not WRITE the destination; the units' keys are forced in one pass that rewrites a conf
# exactly as the old one-key-at-a-time sip_conf_set did; `sip aec on|off` checks its
# preconditions once.
#
# The CLI's functions are taken as they are (everything above its dispatch) and run by
# bash with TEAPORT_SECRETS_DIR in a temp dir and systemctl/sudo/id stubbed where a test
# needs them. Nothing outside a temp dir is touched; no service is.
#
# Run: python test_cli_sip_conf.py   (or via pytest test_suite.py)
#
import atexit
import os
import pathlib
import shutil
import subprocess
import tempfile

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


def _run(secrets, steps, stubs=""):
    script = _functions() + OLD_SET + stubs + "\n" + steps + "\n"
    env = dict(os.environ, TEAPORT_SECRETS_DIR=str(secrets), HOME=str(secrets))
    env.pop("TEAPORT_SIP_CONF", None)
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def _secrets():
    d = pathlib.Path(tempfile.mkdtemp(prefix="teaport-cli-"))
    os.chmod(d, 0o700)
    atexit.register(shutil.rmtree, d, True)
    return d


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
          sip_adopt_copy {d}/nope {d}/x 5060 /s || echo "rc=$?"
          sip_adopt_copy {src} /dev/full 5060 /s || echo "rc=$?"
          sip_adopt_copy {src} {d}/no/such/dir 5060 /s || echo "rc=$?"
          sip_adopt_copy {src} {d}/ok 5060 /s && echo "rc=0"
          set -e; sip_adopt_copy {src} {d}/ok2 5060 /s; echo "rc=0 under set -e"''')
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
systemctl() { [ "$*" = "show -p User --value teaport-sip.service" ] && printf '%s\n' "$UNIT_USER"; }
'''


def test_a_new_conf_written_by_root_goes_to_the_units_user_or_is_refused():
    me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    d = _secrets()
    dest = d / "teaport-sip.conf"
    rc, out = _run(d, f'UNIT_USER={me}; t="$(sip_conf_stage {dest})"; stat -c "%U %a" "$t"', ROOT_STUBS)
    assert rc == 0 and out.strip() == f"{me} 600", out
    for user in ("", "no-such-user-xyz"):
        d = _secrets()
        rc, out = _run(d, f'UNIT_USER="{user}"; sip_conf_stage {d}/teaport-sip.conf || echo REFUSED', ROOT_STUBS)
        assert "REFUSED" in out and "gateway cannot open" in out, out
        assert _temps(d) == [], _temps(d)


def test_adopt_as_root_refuses_an_ownerless_new_conf_before_the_test_registration():
    d = _secrets()
    src = d / "src.conf"; src.write_text(CONF)
    stubs = ADOPT_STUBS + ROOT_STUBS
    rc, out = _run(d, f"UNIT_USER=''; PROBE_HOOK='echo PROBED'; sip_adopt_conf {src}", stubs)
    assert rc == 1 and "gateway cannot open" in out and "nothing tested" in out, out
    assert "PROBED" not in out and not (d / "teaport-sip.conf").exists() and _temps(d) == [], out
    # An installed conf keeps its owner whoever runs this: nothing to ask the unit then.
    (d / "teaport-sip.conf").write_text("password=old\n"); os.chmod(d / "teaport-sip.conf", 0o600)
    rc, out = _run(d, f"UNIT_USER=''; PROBE_HOOK='echo PROBED'; sip_adopt_conf {src}", stubs)
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


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
