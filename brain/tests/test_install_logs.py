"""install.sh setup_logs: the journal as the one log store, its config install.sh's own and
kept in step by every run, never failing the install
(python brain/tests/test_install_logs.py, or pytest)."""

import pathlib
import subprocess

import appliance
from tempdirs import tempdir
from test_install_brain_only import _fn

# SUDO and systemctl are recorded in $T/calls. SUDO install acts under $T only (the drop-in,
# the marker, the journal directory): -d makes a directory, `-D -m MODE /dev/stdin DEST`
# writes stdin with that mode, as install does whatever the umask. WRITE_FAILS=1 fails a
# write before it starts; WRITE_TRUNCATES=1 empties DEST, then fails (a full disk). A restart
# fails with RESTART_FAILS=1. systemctl answers is-enabled with $TIMER, is-active with $ACTIVE.
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
      if [ "$2" = -d ]; then case "$last" in "$T"/*) mkdir -p "$last" ;; esac; return 0; fi
      local mode; mode="$(printf '%s\n' "$@" | sed -n '/^-m$/{n;p;q}')"
      case "$last" in "$T"/*) ;; *) cat >/dev/null; return 0 ;; esac
      if [ "${WRITE_FAILS:-0}" = 1 ]; then cat >/dev/null; return 1; fi
      if [ "${WRITE_TRUNCATES:-0}" = 1 ]; then cat >/dev/null; [ ! -e "$last" ] || : > "$last"; return 1; fi
      mkdir -p "$(dirname "$last")"; cat > "$last"; chmod "$mode" "$last" ;;
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


def _setup_logs(tmp, max_use="2G", write_fails=False, restart_fails=False, timer="enabled",
                active=True, dry_run=False, journal_dir_exists=False, write_truncates=False,
                umask="022"):
    confd, journal = pathlib.Path(tmp, "journald.conf.d"), pathlib.Path(tmp, "journal")
    if journal_dir_exists:
        journal.mkdir(exist_ok=True)
    calls = pathlib.Path(tmp, "calls")
    calls.unlink(missing_ok=True)
    script = (STUBS + _fn("write_root") + _fn("setup_logs") +
              f'DRY_RUN={int(dry_run)}; JOURNAL_CONF_D="{confd}"; JOURNAL_DROPIN="{confd}/teaport.conf"; '
              f'JOURNAL_DIR="{journal}"; JOURNAL_APPLIED="{tmp}/state/journald.applied"; '
              f'JOURNAL_MAX_USE={max_use}; umask {umask}; setup_logs; echo DONE')
    env = {"T": str(tmp), "PATH": "/usr/bin:/bin", "WRITE_FAILS": str(int(write_fails)),
           "WRITE_TRUNCATES": str(int(write_truncates)),
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
    assert RESTART not in calls and not any("/dev/stdin" in c for c in calls), calls


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
    rc, out, calls, dropin = _setup_logs(tempdir("teaport-logs-"), write_fails=True, timer="masked")
    assert rc == 0 and "DONE" in out and "WARN logs: could not write" in out, out
    assert not dropin.exists() and RESTART not in calls, calls
    assert "logrotate.timer is masked" in out, out   # the rotation is still checked


def test_a_failed_rewrite_still_applies_settings_already_on_disk_that_journald_lacks():
    # an earlier run wrote the right settings but its restart failed; now only the header
    # differs and the write fails: the settings on disk are still applied
    tmp = tempdir("teaport-logs-")
    _, _, _, dropin = _setup_logs(tmp, restart_fails=True)
    dropin.write_text(dropin.read_text().replace("teaport (install.sh", "an older header ("))
    rc, out, calls, _ = _setup_logs(tmp, write_fails=True)
    assert rc == 0 and "could not write" in out and RESTART in calls, (out, calls)


def test_a_strict_umask_still_leaves_files_that_read_back_so_a_rerun_restarts_nothing():
    # sudo under umask 077 would make a tee'd file 0600, unreadable to the plain read: every
    # run would rewrite it and restart journald. install -m 0644 sets the mode itself.
    tmp = tempdir("teaport-logs-")
    _setup_logs(tmp, umask="077")
    rc, out, calls, dropin = _setup_logs(tmp, umask="077")
    assert rc == 0 and "journald has these settings already" in out, out
    assert RESTART not in calls, calls
    assert oct(dropin.stat().st_mode & 0o777) == "0o644"


def test_a_write_that_truncates_the_file_then_fails_restarts_nothing():
    # the old file held the settings, but the failed write emptied it: journald must not be
    # restarted onto what is left, nor the settings recorded as applied
    tmp = tempdir("teaport-logs-")
    _, _, _, dropin = _setup_logs(tmp, restart_fails=True)            # right file, not applied
    dropin.write_text(dropin.read_text().replace("teaport (install.sh", "an older header ("))
    rc, out, calls, _ = _setup_logs(tmp, write_truncates=True)
    assert rc == 0 and "could not write" in out and RESTART not in calls, (out, calls)
    assert not pathlib.Path(tmp, "state/journald.applied").exists()


def test_each_logrotate_timer_state_gets_its_own_advice():
    cases = {("masked", True): "unmask", ("masked-runtime", True): "unmask --runtime",
             ("disabled", True): "is disabled", ("not-found", True): "--reinstall logrotate",
             ("", True): "--reinstall logrotate", ("enabled", False): "systemctl start",
             ("bad", True): "systemctl status", ("static", True): "is static",
             # running now, but nothing starts it at the next boot
             ("enabled-runtime", True): "does not start at boot", ("indirect", True): "does not start at boot",
             ("linked", True): "does not start at boot"}
    for (timer, active), advice in cases.items():
        rc, out, _, _ = _setup_logs(tempdir("teaport-logs-"), timer=timer, active=active)
        assert rc == 0 and advice in out, (timer, active, out)
    for timer in ("enabled", "generated"):
        rc, out, _, _ = _setup_logs(tempdir("teaport-logs-"), timer=timer, active=True)
        assert rc == 0 and "logrotate.timer" not in out, (timer, out)


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
