"""install.sh setup_logs: the journal as the one log store, its config install.sh's own and
kept in step by every run, never failing the install
(python brain/tests/test_install_logs.py, or pytest)."""

import pathlib
import subprocess

import appliance
from tempdirs import tempdir
from test_install_brain_only import _fn

# SUDO and systemctl are recorded in $T/calls. SUDO install acts under $T only (the drop-in,
# the marker, the journal directory), unless INSTALL_FAILS=1; a restart fails with
# RESTART_FAILS=1. systemctl answers is-enabled with $TIMER and is-active with $ACTIVE.
STUBS = r'''
set -euo pipefail
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*" >&2; }   # stderr, as install.sh's
SUDO() {
  printf '%s\n' "$*" >> "$T/calls"
  [ "$DRY_RUN" = 1 ] && return 0
  local last="${*: -1}"
  case "$1" in
    install)
      [ "${INSTALL_FAILS:-0}" = 0 ] || return 1
      case "$last" in "$T"/*) ;; *) return 0 ;; esac
      if [ "$2" = -d ]; then mkdir -p "$last"
      else mkdir -p "$(dirname "$last")"; command cp "${@: -2:1}" "$last"; fi ;;   # install -D
    systemctl) [ "$2" != restart ] || [ "${RESTART_FAILS:-0}" = 0 ] ;;
  esac
}
systemctl() {
  printf 'systemctl %s\n' "$*" >> "$T/calls"
  case "$1" in
    is-enabled) [ -n "${TIMER:-}" ] && echo "$TIMER" ;;
    is-active) [ "${ACTIVE:-1}" = 1 ] ;;
  esac
}
sudo() { printf 'sudo %s\n' "$*" >> "$T/calls"; return 1; }   # never needed: reads are plain
'''


def _setup_logs(tmp, max_use="2G", install_fails=False, restart_fails=False, timer="enabled",
                active=True, dry_run=False, journal_dir_exists=False):
    confd, journal = pathlib.Path(tmp, "journald.conf.d"), pathlib.Path(tmp, "journal")
    if journal_dir_exists:
        journal.mkdir(exist_ok=True)
    calls = pathlib.Path(tmp, "calls")
    calls.unlink(missing_ok=True)
    script = (STUBS + _fn("install_stdin") + _fn("setup_logs") +
              f'DRY_RUN={int(dry_run)}; JOURNAL_CONF_D="{confd}"; JOURNAL_DROPIN="{confd}/teaport.conf"; '
              f'JOURNAL_DIR="{journal}"; JOURNAL_APPLIED="{tmp}/state/journald.applied"; '
              f'JOURNAL_MAX_USE={max_use}; setup_logs; echo DONE')
    env = {"T": str(tmp), "PATH": "/usr/bin:/bin", "INSTALL_FAILS": str(int(install_fails)),
           "RESTART_FAILS": str(int(restart_fails)), "TIMER": timer, "ACTIVE": str(int(active))}
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    return (p.returncode, p.stdout + p.stderr,
            calls.read_text().splitlines() if calls.exists() else [], confd / "teaport.conf")


def _index(calls, prefix):
    return next(i for i, c in enumerate(calls) if c.startswith(prefix))


RESTART = "systemctl restart systemd-journald"


def test_a_first_run_writes_the_drop_in_and_sets_up_the_journal_before_restarting():
    tmp = tempdir("teaport-logs-")
    rc, out, calls, dropin = _setup_logs(tmp)
    assert rc == 0 and "DONE" in out, out
    text = dropin.read_text()
    for key in ("[Journal]", "Storage=persistent", "SystemMaxUse=2G", "ForwardToSyslog=no"):
        assert key in text, text
    assert "zz-local.conf" in text, text                       # the override it documents
    # the journal directory made (setgid systemd-journal) and systemd's tmpfiles rules for it,
    # THEN journald restarted and flushed: left to journald it would be root's alone
    order = [_index(calls, f"install -d -m 2755 -o root -g systemd-journal {tmp}/journal"),
             _index(calls, f"systemd-tmpfiles --create --prefix {tmp}/journal"),
             _index(calls, RESTART), _index(calls, "journalctl --flush")]
    assert order == sorted(order), calls
    assert "Storage=persistent" in pathlib.Path(tmp, "state/journald.applied").read_text()


def test_the_config_directory_is_kept_at_0755_so_it_reads_back_without_sudo():
    rc, out, calls, _ = _setup_logs(tempdir("teaport-logs-"))
    assert rc == 0 and any(c.startswith("install -d -m 0755") for c in calls), calls
    assert not any(c.startswith("sudo ") for c in calls), calls


def test_an_existing_journal_directory_is_not_made_again():
    tmp = tempdir("teaport-logs-")
    rc, out, calls, _ = _setup_logs(tmp, journal_dir_exists=True)
    assert rc == 0 and not any(c.startswith("install -d -m 2755") for c in calls), calls
    assert any(c.startswith("systemd-tmpfiles --create --prefix") for c in calls), calls


def test_a_rerun_with_nothing_changed_restarts_nothing():
    tmp = tempdir("teaport-logs-")
    _setup_logs(tmp)
    rc, out, calls, _ = _setup_logs(tmp)
    assert rc == 0 and "journald has these settings already" in out, out
    assert RESTART not in calls and not any(c.startswith("install -D") for c in calls), calls


def test_a_comment_only_change_rewrites_the_file_but_restarts_nothing():
    tmp = tempdir("teaport-logs-")
    _, _, _, dropin = _setup_logs(tmp)
    dropin.write_text(dropin.read_text().replace("teaport (install.sh", "an older header ("))
    rc, out, calls, _ = _setup_logs(tmp)
    assert rc == 0 and "teaport (install.sh" in dropin.read_text(), out
    assert RESTART not in calls, calls


def test_an_operators_edit_to_the_drop_in_is_put_back_and_applied():
    tmp = tempdir("teaport-logs-")
    _, _, _, dropin = _setup_logs(tmp)
    dropin.write_text(dropin.read_text().replace("SystemMaxUse=2G", "SystemMaxUse=8G"))
    rc, out, calls, _ = _setup_logs(tmp)
    assert rc == 0 and "SystemMaxUse=2G" in dropin.read_text() and RESTART in calls, (out, calls)


def test_a_restart_that_failed_is_tried_again_by_the_next_run():
    tmp = tempdir("teaport-logs-")
    rc, out, _, _ = _setup_logs(tmp, restart_fails=True)
    assert rc == 0 and "did not restart" in out, out
    assert not pathlib.Path(tmp, "state/journald.applied").exists()
    rc, out, calls, _ = _setup_logs(tmp)                  # the repair run
    assert rc == 0 and RESTART in calls and pathlib.Path(tmp, "state/journald.applied").exists(), calls


def test_a_drop_in_that_went_missing_is_applied_again_whatever_the_marker_says():
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


def test_a_dry_run_changes_nothing_and_never_runs_sudo():
    tmp = tempdir("teaport-logs-")
    rc, out, calls, dropin = _setup_logs(tmp, dry_run=True)
    assert rc == 0 and "DONE" in out and not dropin.exists(), out
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
