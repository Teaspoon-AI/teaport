"""teaport logs: this boot for plain viewing, anything else passed through as given
(python brain/tests/test_cli_logs.py, or pytest)."""

import os
import pathlib
import subprocess

import appliance
from tempdirs import tempdir

CLI = pathlib.Path(__file__).resolve().parents[2] / "cli" / "teaport"


def _logs(*args):
    """The journalctl command `teaport logs <args>` runs, from a stub that prints it."""
    tmp = tempdir("teaport-logs-cli-")
    stub = pathlib.Path(tmp, "journalctl")
    stub.write_text('#!/bin/sh\necho "journalctl $*"\n')
    stub.chmod(0o755)
    fn = subprocess.run(["sed", "-n", "/^cmd_logs() {/,/^}/p", str(CLI)],
                        capture_output=True, text=True, check=True).stdout
    assert fn.startswith("cmd_logs() {"), "cmd_logs not found in cli/teaport"
    p = subprocess.run(["bash", "-c", fn + 'cmd_logs "$@"', "teaport", *args],
                       capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def test_plain_viewing_shows_this_boot():
    # the journal is persistent and keeps every boot: a bare `teaport logs` must not open
    # at the oldest line kept
    assert _logs() == "journalctl -u teaport-brain.service -b"
    assert _logs("-f") == "journalctl -u teaport-brain.service -b -f"
    assert _logs("-n", "50") == "journalctl -u teaport-brain.service -b -n 50"
    assert _logs("sip", "-p", "err", "-f") == "journalctl -u teaport-sip.service -b -p err -f"
    assert _logs("engine", "--lines=20") == "journalctl -u teaport-engine.service -b --lines=20"


def test_a_boot_time_pattern_or_match_of_the_callers_own_passes_through_as_given():
    for args in (["-b", "-1"], ["--since", "today"], ["-g", "-bus"], ["-xS", "yesterday"],
                 ["_BOOT_ID=abc"], ["--list-boots"]):
        assert _logs(*args) == "journalctl -u teaport-brain.service " + " ".join(args), args


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
