"""install.sh setup_logs: the journal as the one log store, kept in step by every install
run, an operator's own settings left alone, never failing the install
(python brain/tests/test_install_logs.py, or pytest)."""

import pathlib
import subprocess

import appliance
from tempdirs import tempdir
from test_install_brain_only import _fn

# SUDO and systemctl are recorded in $T/calls. SUDO install really writes under $T (so the
# drop-in can be read back) and only records anything else (/var/log/journal), unless
# INSTALL_FAILS=1. systemctl answers is-enabled with $TIMER and is-active with $ACTIVE.
STUBS = r'''
set -euo pipefail
DRY_RUN=0
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
contains() { case "$2" in *"$1"*) return 0 ;; *) return 1 ;; esac; }
# read_env_file's privileged read of a file the plain read can't open (as root would)
sudo() {
  printf 'sudo %s\n' "$*" >> "$T/calls"
  [ "$1" = cat ] && [ -e "$2" ] || return 1
  chmod u+r "$2"; command cat "$2"; chmod 000 "$2"
}
SUDO() {
  printf '%s\n' "$*" >> "$T/calls"
  [ "$1" = install ] || return 0
  [ "${INSTALL_FAILS:-0}" = 0 ] || return 1
  local last="${*: -1}"
  case "$last" in "$T"/*) ;; *) return 0 ;; esac
  if [ "$2" = -d ]; then mkdir -p "$last"; else command cp "${@: -2:1}" "$last"; fi
}
systemctl() {
  printf 'systemctl %s\n' "$*" >> "$T/calls"
  case "$1" in
    is-enabled) [ -n "${TIMER:-}" ] && echo "$TIMER" ;;
    is-active) [ "${ACTIVE:-1}" = 1 ] ;;
  esac
}
'''


def _setup_logs(tmp, max_use="2G", install_fails=False, timer="enabled", active=True,
                operator=None, operator_name="50-local.conf", unreadable=False, on_disk=True):
    confd = pathlib.Path(tmp, "journald.conf.d")
    if operator:   # an operator's own drop-in
        confd.mkdir(exist_ok=True)
        f = confd / operator_name
        f.write_text(operator)
        if unreadable:
            f.chmod(0)
    machine = pathlib.Path(tmp, "journal-machine")   # journald writing to /var/log/journal
    if on_disk:
        machine.mkdir(exist_ok=True)
    calls = pathlib.Path(tmp, "calls")
    calls.unlink(missing_ok=True)
    script = (STUBS + _fn("read_env_file") + _fn("install_lines") + _fn("journal_operator_keys") +
              _fn("setup_logs") +
              f'JOURNAL_CONF="{tmp}/journald.conf"; JOURNAL_CONF_D="{confd}"; '
              f'JOURNAL_OPERATOR_DIRS=("{confd}"); JOURNAL_MACHINE_DIR="{machine}"; '
              f'JOURNAL_DROPIN="{confd}/teaport.conf"; JOURNAL_MAX_USE={max_use}; '
              'setup_logs; echo DONE')
    env = {"T": str(tmp), "PATH": "/usr/bin:/bin", "INSTALL_FAILS": str(int(install_fails)),
           "TIMER": timer, "ACTIVE": str(int(active))}
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    return (p.returncode, p.stdout + p.stderr,
            calls.read_text().splitlines() if calls.exists() else [], confd / "teaport.conf")


def _index(calls, prefix):
    return next(i for i, c in enumerate(calls) if c.startswith(prefix))


def test_a_first_run_writes_the_drop_in_and_sets_up_the_journal_before_restarting():
    rc, out, calls, dropin = _setup_logs(tempdir("teaport-logs-"))
    assert rc == 0 and "DONE" in out, out
    text = dropin.read_text()
    for key in ("[Journal]", "Storage=persistent", "SystemMaxUse=2G", "ForwardToSyslog=no"):
        assert key in text, text
    # /var/log/journal made (setgid systemd-journal), its ACLs from tmpfiles, THEN journald
    # restarted and flushed: left to journald the directory would be root's alone
    mkdir = _index(calls, "install -d -m 2755 -o root -g systemd-journal /var/log/journal")
    tmpfiles = _index(calls, "systemd-tmpfiles --create --prefix /var/log/journal")
    restart = _index(calls, "systemctl restart systemd-journald")
    flush = _index(calls, "journalctl --flush")
    assert mkdir < tmpfiles < restart < flush, calls


def test_a_rerun_with_nothing_changed_restarts_nothing():
    tmp = tempdir("teaport-logs-")
    _setup_logs(tmp)
    rc, out, calls, _ = _setup_logs(tmp)
    assert rc == 0 and "unchanged" in out, out
    assert not any(c.startswith(("install", "systemd-tmpfiles", "journalctl", "systemctl restart"))
                   for c in calls), calls


def test_a_release_that_changes_the_drop_in_reaches_a_box_that_has_it():
    tmp = tempdir("teaport-logs-")
    _setup_logs(tmp, max_use="1G")
    rc, out, calls, dropin = _setup_logs(tmp, max_use="2G")
    assert rc == 0 and "SystemMaxUse=2G" in dropin.read_text(), out
    assert "systemctl restart systemd-journald" in calls, calls


def test_a_key_the_operator_set_is_left_out():
    rc, out, _, dropin = _setup_logs(tempdir("teaport-logs-"),
                                     operator="[Journal]\nSystemMaxUse=500M\n#Storage=volatile\n")
    text = dropin.read_text()
    assert rc == 0 and "SystemMaxUse" not in text, text            # theirs, left out
    assert "Storage=persistent" in text and "ForwardToSyslog=no" in text, text   # a comment is not a setting
    assert "SystemMaxUse is the operator's" in out, out


def test_an_operator_drop_in_the_installer_cannot_read_is_read_with_sudo_and_the_install_goes_on():
    # sorting last (zz-), 0000: the plain read fails; under set -euo pipefail that must
    # neither end the install nor hide the operator's setting
    rc, out, calls, dropin = _setup_logs(tempdir("teaport-logs-"), operator="[Journal]\nSystemMaxUse=500M\n",
                                         operator_name="zz-local.conf", unreadable=True)
    assert rc == 0 and "DONE" in out, out
    assert "SystemMaxUse" not in dropin.read_text(), dropin.read_text()
    assert "SystemMaxUse is the operator's" in out, out


def test_an_operators_storage_keeps_their_forwarding_too():
    # Storage=volatile of theirs + ForwardToSyslog=no of ours would keep nothing on disk
    rc, out, _, dropin = _setup_logs(tempdir("teaport-logs-"), operator="[Journal]\nStorage=volatile\n")
    text = dropin.read_text()
    assert rc == 0 and "Storage" not in text and "ForwardToSyslog" not in text, text
    assert "SystemMaxUse=2G" in text, text
    assert "persistent" not in out.split("left out")[0].split("logs: the journal")[-1], out   # no overclaim
    assert "capped at 2G" in out and "no copy to /var/log/syslog" not in out, out


def test_a_repair_applies_the_settings_again_while_journald_is_not_on_disk():
    tmp = tempdir("teaport-logs-")
    _setup_logs(tmp, on_disk=False)                        # written, restart "failed"
    rc, out, calls, _ = _setup_logs(tmp, on_disk=False)    # the repair run
    assert rc == 0 and "applying it again" in out, out
    assert "systemctl restart systemd-journald" in calls, calls
    rc, out, calls, _ = _setup_logs(tmp, on_disk=True)     # on disk now: nothing to do
    assert "systemctl restart systemd-journald" not in calls, calls


def test_a_drop_in_that_cannot_be_written_warns_and_the_install_goes_on():
    rc, out, calls, dropin = _setup_logs(tempdir("teaport-logs-"), install_fails=True)
    assert rc == 0 and "DONE" in out and "WARN logs: could not write" in out, out
    assert not dropin.exists() and "systemctl restart systemd-journald" not in calls, calls


def test_each_logrotate_timer_state_gets_its_own_advice():
    cases = {("masked", True): "unmask", ("disabled", True): "is disabled",
             ("not-found", True): "--reinstall logrotate", ("", True): "--reinstall logrotate",
             ("enabled", False): "systemctl start", ("indirect", False): "systemctl start",
             ("masked-runtime", True): "unmask --runtime", ("bad", True): "systemctl status"}
    for (timer, active), advice in cases.items():
        rc, out, _, _ = _setup_logs(tempdir("teaport-logs-"), timer=timer, active=active)
        assert rc == 0 and advice in out, (timer, active, out)
    for timer in ("enabled", "static", "enabled-runtime", "indirect"):
        rc, out, _, _ = _setup_logs(tempdir("teaport-logs-"), timer=timer, active=True)
        assert rc == 0 and "logrotate.timer" not in out, (timer, out)


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
