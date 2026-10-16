"""teaport logs: a bare call shows this boot, any argument goes to journalctl as given
(python brain/tests/test_cli_logs.py, or pytest)."""

import os
import pathlib
import subprocess

import appliance
from tempdirs import tempdir

CLI = pathlib.Path(__file__).resolve().parents[2] / "cli" / "teaport"


def _logs(*args):
    """The journalctl command `teaport logs <args>` runs (a stub prints it), under the CLI's
    own strict mode."""
    tmp = tempdir("teaport-logs-cli-")
    stub = pathlib.Path(tmp, "journalctl")
    stub.write_text('#!/bin/sh\necho "journalctl $*"\n')
    stub.chmod(0o755)
    fn = subprocess.run(["sed", "-n", "/^cmd_logs() {/,/^}/p", str(CLI)],
                        capture_output=True, text=True, check=True).stdout
    assert fn.startswith("cmd_logs() {"), "cmd_logs not found in cli/teaport"
    p = subprocess.run(["bash", "-c", "set -euo pipefail\n" + fn + 'cmd_logs "$@"', "teaport", *args],
                       capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def test_a_bare_call_shows_this_boot():
    # the journal is persistent and keeps every boot: a bare `teaport logs` must not open
    # at the oldest line kept
    assert _logs() == "journalctl -u teaport-brain.service -b"
    assert _logs("sip") == "journalctl -u teaport-sip.service -b"
    assert _logs("engine") == "journalctl -u teaport-engine.service -b"


def test_any_argument_goes_to_journalctl_as_given():
    for args in (["-f"], ["-n", "50"], ["-b", "-1"], ["--since", "today"], ["-g", "-bus"],
                 ["-n", "--since=today"], ["_BOOT_ID=abc"], ["-l"]):
        assert _logs(*args) == "journalctl -u teaport-brain.service " + " ".join(args), args
    assert _logs("sip", "-f") == "journalctl -u teaport-sip.service -f"


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
