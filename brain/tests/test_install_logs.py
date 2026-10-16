"""install.sh setup_logs: the journal as the one log store, seeded once, never failing
the install (python -m pytest brain/tests/test_install_logs.py)."""

import pathlib
import subprocess

from tempdirs import tempdir
from test_install_brain_only import _fn

# SUDO and systemctl recorded in $T/calls; `install` really copies (so the drop-in can be
# read back) unless INSTALL_FAILS=1; DRY_RUN=0 and no /var/log/syslog to rotate.
STUBS = r'''
set -euo pipefail
DRY_RUN=0
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
SUDO() {
  printf '%s\n' "$*" >> "$T/calls"
  case "$1" in
    install) [ "${INSTALL_FAILS:-0}" = 0 ] || return 1; command cp "${@: -2:1}" "${@: -1}" ;;
  esac
}
systemctl() { printf 'systemctl %s\n' "$*" >> "$T/calls"; case "$1" in is-enabled) echo enabled ;; esac; }
du() { return 1; }
'''


def _setup_logs(tmp, dropin_exists=False, install_fails=False):
    dropin = pathlib.Path(tmp, "teaport.conf")
    if dropin_exists:
        dropin.write_text("[Journal]\nSystemMaxUse=4G\n")   # an operator's edit
    script = (STUBS + _fn("install_lines") + _fn("setup_logs") +
              f'JOURNAL_DROPIN="{dropin}"; JOURNAL_MAX_USE=2G; SYSLOG_ROTATE_NOW_MB=256; '
              'setup_logs; echo DONE')
    env = {"T": str(tmp), "PATH": "/usr/bin:/bin", "INSTALL_FAILS": str(int(install_fails))}
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    calls = pathlib.Path(tmp, "calls")
    return (p.returncode, p.stdout + p.stderr,
            calls.read_text().splitlines() if calls.exists() else [], dropin)


def test_a_first_run_seeds_the_drop_in_then_applies_it():
    rc, out, calls, dropin = _setup_logs(tempdir("teaport-logs-"))
    assert rc == 0 and "DONE" in out, out
    text = dropin.read_text()
    assert "[Journal]" in text and "Storage=persistent" in text, text
    assert "SystemMaxUse=2G" in text and "ForwardToSyslog=no" in text, text
    # /var/log/journal from systemd's tmpfiles rule BEFORE journald is restarted
    order = [c.split()[0] if not c.startswith("systemctl") else c for c in calls]
    tmpfiles = next(i for i, c in enumerate(calls) if c.startswith("systemd-tmpfiles"))
    restart = next(i for i, c in enumerate(calls) if c == "systemctl restart systemd-journald")
    flush = next(i for i, c in enumerate(calls) if c == "journalctl --flush")
    assert tmpfiles < restart < flush, (order, calls)


def test_a_repair_leaves_an_operators_drop_in_and_does_not_restart_journald():
    rc, out, calls, dropin = _setup_logs(tempdir("teaport-logs-"), dropin_exists=True)
    assert rc == 0 and "DONE" in out and "left as it is" in out, out
    assert dropin.read_text() == "[Journal]\nSystemMaxUse=4G\n"
    assert not any(c.startswith(("install", "systemd-tmpfiles", "journalctl")) or
                   c == "systemctl restart systemd-journald" for c in calls), calls


def test_a_drop_in_that_cannot_be_written_warns_and_the_install_goes_on():
    rc, out, calls, dropin = _setup_logs(tempdir("teaport-logs-"), install_fails=True)
    assert rc == 0 and "DONE" in out, out                  # set -e did not end the install
    assert "WARN logs: could not write" in out, out
    assert not dropin.exists()
    assert "systemctl restart systemd-journald" not in calls, calls   # nothing to apply
