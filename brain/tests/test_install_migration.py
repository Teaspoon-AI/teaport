#
# install.sh's move from two brain processes to one (issue #58): retiring the old
# teaport-sip-brain unit, and carrying the per-path tuning an appliance kept in systemd
# drop-ins into brain.env as per-front-end settings (sip_brain_detach / _retire /
# _restore, migrate_brain_dropins).
#
# The functions are taken out of install.sh as they are and run by bash against a fixture
# tree shaped like the appliance's (its real drop-ins, dummy values), with the systemd
# paths pointed into it and systemctl/sudo stubbed. Nothing outside a temp dir is touched.
#
# Run: python test_install_migration.py   (or via pytest test_suite.py)
#
import os
import pathlib
import stat
import subprocess
import tempfile

INSTALL = pathlib.Path(__file__).resolve().parents[2] / "install.sh"
START = "# --- one brain process (issue #58): retiring teaport-sip-brain"
END = "# teaport-sip telephony (opt-in). The GPL C++ gateway"

STUBS = r'''
set -euo pipefail
DRY_RUN=0
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
die()  { printf 'DIE %s\n' "$*"; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf 'DRY %s\n' "$*"; else "$@"; fi; }
# As install.sh's: through run, so a dry run changes nothing. systemctl is recorded, and
# STOP_HOOK runs when the old unit is stopped (a test kills the run there).
SUDO() { case "$1" in
           systemctl) printf 'SYSTEMCTL %s\n' "${*:2}"
                      if [ "${*:2}" = "stop teaport-sip-brain.service" ]; then eval "${STOP_HOOK:-:}"; fi ;;
           *) run "$@" ;;
         esac; }
systemctl() { return 1; }
'''


def _functions(root: str) -> str:
    text = INSTALL.read_text()
    body = text[text.index(START):text.index(END)]
    # sudo_python runs the step as root on the box; here, as us.
    body = body.replace('sudo python3 -I "$@"', 'python3 -I "$@"')
    return body.replace("/etc/systemd/system", f"{root}/sys")


def _fixture(adversarial=False):
    root = tempfile.mkdtemp(prefix="teaport-mig-")
    sysd, etc = os.path.join(root, "sys"), os.path.join(root, "etc")
    sipd = os.path.join(sysd, "teaport-sip-brain.service.d")
    talkd = os.path.join(sysd, "teaport-brain.service.d")
    for d in (sipd, talkd, etc):
        os.makedirs(d)

    def w(path, text, mode=0o644):
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)
    # The appliance's own, as found on it (values dummy where they could be anything).
    w(f"{talkd}/ab-stopsecs.conf",
      f"[Service]\n# Blind A/B 2026-09-18\nEnvironmentFile=-{etc}/brain-talk.env\n")
    w(f"{sipd}/interrupt-min-words.conf",
      "# 2026-10-02 test\n[Service]\nEnvironment=TEAPORT_INTERRUPT_MIN_WORDS=2\n")
    w(f"{sipd}/stt-makeup-db.conf", "[Service]\nEnvironment=SIP_STT_MAKEUP_DB=6\n")
    w(f"{etc}/brain-talk.env", "# talk A/B\nENDPOINT_STOP_SECS=0.2\nSMARTTURN_STOP_SECS=0.6\n")
    w(f"{etc}/brain.env", "GATEWAY_TOKEN=tok\nSIP_HALF_DUPLEX=0\n", 0o640)
    w(f"{sysd}/teaport-sip-brain.service", "[Unit]\n")
    w(f"{sysd}/teaport-sip.service",
      "[Unit]\nUpholds=teaport-sip-brain.service\nPartOf=teaport-sip-brain.service\n")
    if adversarial:
        # A secret beside a knob; quoting; an unbalanced quote; an empty value; the same
        # key twice (the later drop-in wins); a knob brain.env shadowed (it won in
        # systemd); and a brain.env whose last line has no newline.
        w(f"{talkd}/override.conf",
          "[Service]\nEnvironment=OPENAI_API_KEY=sk-SECRET123 SMARTTURN_STOP_SECS=0.7\n")
        w(f"{sipd}/multi.conf",
          '[Service]\nEnvironment="SIP_FOO=a b=c" SIP_BAR="x y"\nEnvironment="SIP_BROKEN=\n')
        w(f"{sipd}/stt-makeup-db.conf", "[Service]\nEnvironment=SIP_STT_MAKEUP_DB=\n")
        w(f"{sipd}/zz-later.conf", "[Service]\nEnvironment=SIP_BAR=last\n")
        w(f"{sipd}/shadowed.conf", "[Service]\nEnvironment=ENDPOINT_STOP_SECS=0.3\n")
        w(f"{etc}/brain.env", "GATEWAY_TOKEN=tok\nENDPOINT_STOP_SECS=0.5\nLAST_NO_NL=1", 0o640)
    return root


def _run(root, steps, extra=""):
    script = STUBS + f'ETC="{root}/etc"\n' + _functions(root) + extra + "\n" + steps + "\n"
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def _env(root):
    return dict(line.split("=", 1) for line in
                pathlib.Path(root, "etc/brain.env").read_text().splitlines() if "=" in line)


def test_the_box_migrates_its_tuning_and_retires_the_old_unit():
    root = _fixture()
    mode = stat.S_IMODE(os.stat(f"{root}/etc/brain.env").st_mode)
    rc, out = _run(root, "sip_brain_detach; sip_brain_retire")
    assert rc == 0, out
    env = _env(root)
    assert env["SIP_STT_MAKEUP_DB"] == "6" and env["SIP_INTERRUPT_MIN_WORDS"] == "2"
    assert env["TALK_ENDPOINT_STOP_SECS"] == "0.2" and env["TALK_SMARTTURN_STOP_SECS"] == "0.6"
    assert env["GATEWAY_TOKEN"] == "tok"
    assert stat.S_IMODE(os.stat(f"{root}/etc/brain.env").st_mode) == mode
    assert list(pathlib.Path(root, "etc").glob("brain.env.bak-*"))         # a backup
    sysd = pathlib.Path(root, "sys")
    assert not (sysd / "teaport-sip-brain.service").exists()
    assert list(sysd.glob("teaport-sip-brain.service.d.retired-*"))
    assert list((sysd / "teaport-brain.service.d").glob("ab-stopsecs.conf.retired-2*"))
    assert "Upholds" not in (sysd / "teaport-sip.service").read_text()
    # Idempotent: a second run finds nothing to do.
    rc, out2 = _run(root, "sip_brain_detach; sip_brain_retire")
    assert rc == 0 and "drop-in tuning" not in out2, out2


def test_secrets_quoting_empties_precedence_and_a_missing_newline():
    root = _fixture(adversarial=True)
    rc, out = _run(root, "sip_brain_detach; sip_brain_retire")
    assert rc == 0, out
    assert "SECRET123" not in out and "sk-" not in out                 # names, never values
    env = _env(root)
    assert env["LAST_NO_NL"] == "1"                                    # not glued to a key
    assert env["SIP_FOO"] == "a b=c"
    assert env["SIP_BAR"] == "last"                                    # the later drop-in
    assert "SIP_STT_MAKEUP_DB" not in env                              # empty: not moved
    assert "SIP_ENDPOINT_STOP_SECS" not in env                         # brain.env won in systemd
    assert env["TALK_SMARTTURN_STOP_SECS"] == "0.6"                    # the EnvironmentFile wins
    assert "unreadable Environment= line" in out                       # the bad quote, named
    assert "OPENAI_API_KEY" in out and "has no per-front-end form" in out
    talkd = pathlib.Path(root, "sys/teaport-brain.service.d")
    assert (talkd / "override.conf").exists()                          # it does more: stays


def test_a_failed_migration_stops_before_anything_is_swapped():
    if os.geteuid() == 0:
        return  # root writes through the read-only dir this failure is staged with
    root = _fixture()
    os.chmod(f"{root}/etc", 0o500)                                     # brain.env cannot be replaced
    try:
        rc, out = _run(root, "sip_brain_detach; echo SWAPPED")
    finally:
        os.chmod(f"{root}/etc", 0o755)
    assert rc != 0 and "SWAPPED" not in out and "DIE" in out, out
    sysd = pathlib.Path(root, "sys")
    assert (sysd / "teaport-sip-brain.service").exists()
    assert "Upholds" in (sysd / "teaport-sip.service").read_text()     # untouched
    assert (sysd / "teaport-brain.service.d/ab-stopsecs.conf").exists()


def test_a_run_that_dies_between_detach_and_retire_puts_the_old_pair_back():
    root = _fixture()
    rc, out = _run(root, "sip_brain_detach; die 'the swap failed'")
    assert rc != 0
    sysd = pathlib.Path(root, "sys")
    assert "Upholds" in (sysd / "teaport-sip.service").read_text()
    assert (sysd / "teaport-brain.service.d/ab-stopsecs.conf").exists()
    assert not list((sysd / "teaport-brain.service.d").glob("*.retired-pending"))
    assert (sysd / "teaport-sip-brain.service").exists()


def test_a_drop_in_left_parked_by_an_older_aborted_run_is_judged_again():
    root = _fixture()
    talkd = pathlib.Path(root, "sys/teaport-brain.service.d")
    (talkd / "ab-stopsecs.conf").rename(talkd / "ab-stopsecs.conf.retired-pending")
    rc, out = _run(root, "sip_brain_detach; sip_brain_retire")
    assert rc == 0, out
    assert "putting back" in out
    assert list(talkd.glob("ab-stopsecs.conf.retired-2*"))
    assert _env(root)["TALK_ENDPOINT_STOP_SECS"] == "0.2"


def _tree(root):
    out = {}
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            out[os.path.relpath(p, root)] = (stat.S_IMODE(os.stat(p).st_mode),
                                             pathlib.Path(p).read_bytes())
    return out


def test_a_write_that_fails_part_way_leaves_nothing_behind_and_says_so():
    """A disk that fills mid-write (RLIMIT_FSIZE stands in): brain.env as it was, no
    temp file or backup left over -- above all no readable copy of its secrets -- and the
    run stops (die) instead of saying nothing changed."""
    root = _fixture()
    env = pathlib.Path(root, "etc/brain.env")
    env.write_text("".join(f"KEY_{i}=secret-{i:04d}-{'x' * 40}\n" for i in range(40)))
    os.chmod(env, 0o640)
    before = env.read_bytes()
    rc, out = _run(root, "umask 022; ulimit -f 2; sip_brain_detach; echo SWAPPED")
    assert rc != 0 and "SWAPPED" not in out and "DIE" in out, out
    assert env.read_bytes() == before
    left = [p.name for p in env.parent.iterdir() if p.name.startswith("brain.env.")]
    assert left == [], left                                        # no tmp-*, no bak-*
    # And on a write that succeeds under sudo's umask, nothing it made is world-readable.
    root = _fixture()
    rc, out = _run(root, "umask 022; sip_brain_detach; sip_brain_retire")
    assert rc == 0, out
    for p in pathlib.Path(root, "etc").glob("brain.env*"):
        assert not stat.S_IMODE(os.stat(p).st_mode) & 0o007, p


def test_a_kill_while_the_old_unit_stops_puts_the_old_pair_back():
    root = _fixture()
    rc, out = _run(root, "sip_brain_detach; echo NOT-REACHED",
                   extra='STOP_HOOK="kill -TERM $$"')
    assert rc != 0 and "NOT-REACHED" not in out, out
    sysd = pathlib.Path(root, "sys")
    assert "Upholds" in (sysd / "teaport-sip.service").read_text()   # its ties are back
    assert not (sysd / "teaport-sip.service.pre-detach").exists()
    assert (sysd / "teaport-brain.service.d/ab-stopsecs.conf").exists()
    # Had even the trap not run (SIGKILL), the next run finds the copy and starts from it.
    root = _fixture()
    rc, out = _run(root, "sip_brain_detach; trap - EXIT; exit 9")      # as if killed
    sysd = pathlib.Path(root, "sys")
    assert (sysd / "teaport-sip.service.pre-detach").exists()
    rc, out = _run(root, "sip_brain_detach; sip_brain_restore")
    assert rc == 0 and "putting back" in out, out
    assert "Upholds" in (sysd / "teaport-sip.service").read_text()
    assert not (sysd / "teaport-sip.service.pre-detach").exists()


def test_a_dry_run_changes_nothing_and_reports_what_a_real_run_would_move():
    root = _fixture()
    talkd = pathlib.Path(root, "sys/teaport-brain.service.d")
    (talkd / "ab-stopsecs.conf").rename(talkd / "ab-stopsecs.conf.retired-pending")
    before = _tree(root)
    rc, out = _run(root, "DRY_RUN=1; sip_brain_detach; sip_brain_retire")
    assert rc == 0, out
    assert _tree(root) == before
    assert "TALK_ENDPOINT_STOP_SECS" in out and "TALK_SMARTTURN_STOP_SECS" in out, out
    assert "SIP_STT_MAKEUP_DB" in out


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
