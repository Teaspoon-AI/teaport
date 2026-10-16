"""install.sh setup_logs: the journal as the one log store, kept in step by every install
run, an operator's own settings left alone, never failing the install, never sudo in a dry
run (python brain/tests/test_install_logs.py, or pytest)."""

import pathlib
import subprocess

import appliance
from tempdirs import tempdir
from test_install_brain_only import _fn

# SUDO, sudo and systemctl are recorded in $T/calls. SUDO install/mkdir really act under $T
# (so the drop-in and the marker can be read back) and only record anything else
# (/var/log/journal), unless INSTALL_FAILS=1. sudo (read_root_conf, journal_operator_keys)
# reads and lists what the plain read can't, as root would. systemctl answers is-enabled with
# $TIMER, is-active with $ACTIVE, and a restart fails with RESTART_FAILS=1.
STUBS = r'''
set -euo pipefail
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*" >&2; }   # stderr, as install.sh's
contains() { case "$2" in *"$1"*) return 0 ;; *) return 1 ;; esac; }
# as root would: <dir> opened for the one command, its mode put back after
as_root() {
  local d="$1" m rc=0; shift
  m="$(stat -c %a "$d")"; chmod u+rwx "$d"
  "$@" || rc=$?
  chmod "$m" "$d"; return "$rc"
}
SUDO() {
  printf '%s\n' "$*" >> "$T/calls"
  [ "$DRY_RUN" = 1 ] && return 0
  local last="${*: -1}"
  case "$1" in
    install)
      [ "${INSTALL_FAILS:-0}" = 0 ] || return 1
      case "$last" in "$T"/*) ;; *) return 0 ;; esac
      if [ "$2" = -d ]; then mkdir -p "$last"
      else mkdir -p "$(dirname "$last")"   # install -D makes the parents
           as_root "$(dirname "$last")" command cp "${@: -2:1}" "$last"; fi ;;
    systemctl) [ "$2" != restart ] || [ "${RESTART_FAILS:-0}" = 0 ] ;;
  esac
}
sudo() {
  printf 'sudo %s\n' "$*" >> "$T/calls"
  case "$1" in
    cat) as_root "$(dirname "$2")" sh -c '[ -e "$1" ] && { chmod u+r "$1"; cat "$1"; }' _ "$2" ;;
    find) as_root "$2" command find "${@:2}" ;;
    *) return 1 ;;
  esac
}
systemctl() {
  printf 'systemctl %s\n' "$*" >> "$T/calls"
  case "$1" in
    is-enabled) [ -n "${TIMER:-}" ] && echo "$TIMER" ;;
    is-active) [ "${ACTIVE:-1}" = 1 ] ;;
  esac
}
'''


def _setup_logs(tmp, max_use="2G", install_fails=False, restart_fails=False, timer="enabled",
                active=True, operator=None, operator_name="50-local.conf", unreadable=False,
                unreadable_dir=False, dry_run=False):
    confd = pathlib.Path(tmp, "journald.conf.d")
    if operator:   # an operator's own drop-in
        confd.mkdir(exist_ok=True)
        f = confd / operator_name
        f.write_text(operator)
        if unreadable:
            f.chmod(0)
    if unreadable_dir and confd.exists():
        confd.chmod(0)   # root-only: can't be listed or entered by the installing user
    calls = pathlib.Path(tmp, "calls")
    calls.unlink(missing_ok=True)
    script = (STUBS + _fn("read_env_file") + _fn("read_root_conf") + _fn("install_lines") + _fn("journal_operator_keys") +
              _fn("setup_logs") +
              f'DRY_RUN={int(dry_run)}; STATE="{tmp}/state"; '
              f'JOURNAL_CONF="{tmp}/journald.conf"; JOURNAL_CONF_D="{confd}"; '
              f'JOURNAL_OPERATOR_DIRS=("{confd}"); JOURNAL_DROPIN="{confd}/teaport.conf"; '
              f'JOURNAL_APPLIED="{tmp}/state/journald.applied"; JOURNAL_MAX_USE={max_use}; '
              'setup_logs; echo DONE')
    env = {"T": str(tmp), "PATH": "/usr/bin:/bin", "INSTALL_FAILS": str(int(install_fails)),
           "RESTART_FAILS": str(int(restart_fails)), "TIMER": timer, "ACTIVE": str(int(active))}
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    if unreadable_dir and confd.exists():
        confd.chmod(0o755)
    return (p.returncode, p.stdout + p.stderr,
            calls.read_text().splitlines() if calls.exists() else [], confd / "teaport.conf")


def _index(calls, prefix):
    return next(i for i, c in enumerate(calls) if c.startswith(prefix))


RESTART = "systemctl restart systemd-journald"
MKDIR_JOURNAL = "install -d -m 2755 -o root -g systemd-journal /var/log/journal"


def test_a_first_run_writes_the_drop_in_and_sets_up_the_journal_before_restarting():
    tmp = tempdir("teaport-logs-")
    rc, out, calls, dropin = _setup_logs(tmp)
    assert rc == 0 and "DONE" in out, out
    text = dropin.read_text()
    for key in ("[Journal]", "Storage=persistent", "SystemMaxUse=2G", "ForwardToSyslog=no"):
        assert key in text, text
    # /var/log/journal made (setgid systemd-journal), its ACLs from tmpfiles, THEN journald
    # restarted and flushed: left to journald the directory would be root's alone
    order = [_index(calls, MKDIR_JOURNAL), _index(calls, "systemd-tmpfiles --create --prefix /var/log/journal"),
             _index(calls, RESTART), _index(calls, "journalctl --flush")]
    assert order == sorted(order), calls
    assert "Storage=persistent" in pathlib.Path(tmp, "state/journald.applied").read_text()


def test_a_rerun_with_nothing_changed_restarts_nothing():
    tmp = tempdir("teaport-logs-")
    _setup_logs(tmp)
    rc, out, calls, _ = _setup_logs(tmp)
    assert rc == 0 and "journald has these settings already" in out, out
    assert RESTART not in calls and not any(c.startswith("install") for c in calls), calls


def test_a_comment_only_change_rewrites_the_file_but_restarts_nothing():
    tmp = tempdir("teaport-logs-")
    _, _, _, dropin = _setup_logs(tmp)
    dropin.write_text(dropin.read_text().replace("teaport (install.sh", "an older header ("))
    rc, out, calls, _ = _setup_logs(tmp)
    assert rc == 0 and "teaport (install.sh" in dropin.read_text(), out
    assert RESTART not in calls, calls


def test_a_restart_that_failed_is_tried_again_by_the_next_run():
    tmp = tempdir("teaport-logs-")
    rc, out, _, _ = _setup_logs(tmp, restart_fails=True)
    assert rc == 0 and "did not restart" in out, out
    assert not pathlib.Path(tmp, "state/journald.applied").exists()
    rc, out, calls, _ = _setup_logs(tmp)                  # the repair run
    assert rc == 0 and RESTART in calls, (out, calls)
    assert pathlib.Path(tmp, "state/journald.applied").exists()


def test_a_drop_in_that_went_missing_is_applied_again_whatever_the_marker_says():
    # applied once, then the drop-in deleted (and the box rebooted): journald runs without
    # it, so the next run must restart it, though journald.applied still matches
    tmp = tempdir("teaport-logs-")
    _, _, _, dropin = _setup_logs(tmp)
    dropin.unlink()
    rc, out, calls, _ = _setup_logs(tmp)
    assert rc == 0 and dropin.exists() and RESTART in calls, (out, calls)


def test_a_release_that_changes_the_settings_reaches_a_box_that_has_them():
    tmp = tempdir("teaport-logs-")
    _setup_logs(tmp, max_use="1G")
    rc, out, calls, dropin = _setup_logs(tmp, max_use="2G")
    assert rc == 0 and "SystemMaxUse=2G" in dropin.read_text() and RESTART in calls, (out, calls)


def test_a_key_the_operator_set_is_left_out():
    rc, out, _, dropin = _setup_logs(tempdir("teaport-logs-"),
                                     operator="[Journal]\nSystemMaxUse=500M\n#Storage=volatile\n")
    text = dropin.read_text()
    assert rc == 0 and "SystemMaxUse" not in text, text                          # theirs
    assert "Storage=persistent" in text and "ForwardToSyslog=no" in text, text   # a comment is not a setting
    assert "SystemMaxUse is the operator's" in out, out


def test_an_operators_storage_leaves_the_journal_as_they_set_it():
    # volatile or auto of theirs: no cap, no forwarding change, no /var/log/journal (which
    # would turn their "auto" persistent), and no claim to have made the journal the store
    for storage in ("volatile", "auto"):
        rc, out, calls, dropin = _setup_logs(tempdir("teaport-logs-"),
                                             operator=f"[Journal]\nStorage={storage}\n")
        text = dropin.read_text()
        assert rc == 0 and "=" not in text.split("[Journal]")[1], (storage, text)
        assert not any(c.startswith(MKDIR_JOURNAL) for c in calls) and RESTART not in calls, (storage, calls)
        assert "left as they set it" in out and "the one store" not in out, (storage, out)


def test_an_operator_drop_in_the_installer_cannot_read_is_read_with_sudo_and_the_install_goes_on():
    # sorting last (zz-), 0000: the plain read fails; under set -euo pipefail that must
    # neither end the install nor hide the operator's setting
    rc, out, calls, dropin = _setup_logs(tempdir("teaport-logs-"), operator="[Journal]\nSystemMaxUse=500M\n",
                                         operator_name="zz-local.conf", unreadable=True)
    assert rc == 0 and "DONE" in out and "SystemMaxUse" not in dropin.read_text(), out
    assert any(c.startswith("sudo cat") for c in calls), calls


def test_a_drop_in_directory_the_installer_cannot_enter_is_listed_and_read_with_sudo():
    tmp = tempdir("teaport-logs-")
    rc, out, calls, _ = _setup_logs(tmp, operator="[Journal]\nSystemMaxUse=500M\n", unreadable_dir=True)
    assert rc == 0 and "DONE" in out and "SystemMaxUse is the operator's" in out, out
    assert any(c.startswith("sudo find") for c in calls), calls
    assert any(c.startswith("sudo cat") and "50-local.conf" in c for c in calls), calls


def test_a_dry_run_never_runs_sudo():
    tmp = tempdir("teaport-logs-")
    rc, out, calls, _ = _setup_logs(tmp, operator="[Journal]\nSystemMaxUse=500M\n",
                                    operator_name="zz-local.conf", unreadable=True, dry_run=True)
    assert rc == 0 and "DONE" in out and "a real run reads it with sudo" in out, out
    assert not any(c.startswith("sudo ") for c in calls), calls


def test_a_drop_in_that_cannot_be_written_warns_and_the_install_goes_on():
    rc, out, calls, dropin = _setup_logs(tempdir("teaport-logs-"), install_fails=True, timer="masked")
    assert rc == 0 and "DONE" in out and "WARN logs: could not write" in out, out
    assert not dropin.exists() and RESTART not in calls, calls
    assert "logrotate.timer is masked" in out, out   # the rotation is still checked


def test_each_logrotate_timer_state_gets_its_own_advice():
    cases = {("masked", True): "unmask", ("masked-runtime", True): "unmask --runtime",
             ("disabled", True): "is disabled", ("not-found", True): "--reinstall logrotate",
             ("", True): "--reinstall logrotate", ("enabled", False): "systemctl start",
             ("indirect", False): "systemctl start", ("bad", True): "systemctl status"}
    for (timer, active), advice in cases.items():
        rc, out, _, _ = _setup_logs(tempdir("teaport-logs-"), timer=timer, active=active)
        assert rc == 0 and advice in out, (timer, active, out)
    for timer in ("enabled", "static", "enabled-runtime", "indirect"):
        rc, out, _, _ = _setup_logs(tempdir("teaport-logs-"), timer=timer, active=True)
        assert rc == 0 and "logrotate.timer" not in out, (timer, out)


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
