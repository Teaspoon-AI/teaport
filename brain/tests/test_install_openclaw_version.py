#!/usr/bin/env python3
# test_install_openclaw_version.py — install.sh against the OpenClaw release this one is
# tested with (manifest openclaw.version): check_openclaw_version warns when the box's
# OpenClaw, on the host or in the NemoClaw sandbox, is another, and openclaw_spec is what
# the hints tell people to install. The functions are taken out of install.sh as they are
# and run by bash, `openclaw` and `nemoclaw` stand-ins answering --version.
#
# Run: python test_install_openclaw_version.py   (or via pytest test_suite.py)
#
import os
import pathlib
import subprocess

import appliance
from tempdirs import tempdir

INSTALL = pathlib.Path(__file__).resolve().parents[2] / "install.sh"

STUBS = r'''
set -euo pipefail
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
timeout() { shift; "$@"; }
# The manifest's openclaw.version: $WANT, or none at all when it is empty.
mget() { [ "$1" = openclaw.version ] && [ -n "${WANT:-}" ] && printf '%s\n' "$WANT"; }
resolve_nemoclaw() { NEMOCLAW="$T/nemoclaw"; }
'''


def _fn(name: str) -> str:
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _run(mode, want="2026.9.7", says="OpenClaw 2026.9.7 (c074824)"):
    t = tempdir()
    for name in ("openclaw", "nemoclaw"):
        p = pathlib.Path(t, name)
        p.write_text(f"#!/bin/sh\nprintf '%s\\n' '{says}'\n")
        p.chmod(0o755)
    script = (STUBS + _fn("openclaw_spec") + _fn("check_openclaw_version")
              + f'AGENT_MODE="{mode}" OPENCLAW="$T/openclaw" SANDBOX=box\n'
              + 'check_openclaw_version; echo "SPEC=$(openclaw_spec)"\n')
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={**os.environ, "T": t, "WANT": want})
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def test_the_tested_release_is_logged_and_named_in_the_hints():
    out = _run("openclaw")
    assert "LOG OpenClaw 2026.9.7 (the release this one is tested with)" in out
    assert "WARN" not in out and "SPEC=openclaw@2026.9.7" in out


def test_another_release_on_the_host_is_warned_about_with_the_fix():
    out = _run("openclaw", says="OpenClaw 2026.10.9 (abc1234)")
    assert "WARN OpenClaw 2026.10.9 is installed; this release of teaport is tested " \
           "with OpenClaw 2026.9.7" in out
    assert "sudo npm install -g openclaw@2026.9.7" in out


def test_the_sandbox_is_read_through_nemoclaw_and_its_fix_is_nemoclaws():
    out = _run("nemoclaw", says="OpenClaw 2026.10.7 (fed0001)")
    assert "WARN OpenClaw 2026.10.7 is installed" in out
    assert "NemoClaw release that ships OpenClaw 2026.9.7" in out


def test_an_unreadable_version_warns_and_voice_only_or_no_pin_says_nothing():
    assert "could not read the OpenClaw version" in _run("openclaw", says="command not found")
    assert "OpenClaw" not in _run("").replace("SPEC=openclaw@2026.9.7", "")
    out = _run("openclaw", want="", says="OpenClaw 2026.10.9 (abc1234)")
    assert "WARN" not in out and "LOG" not in out and "SPEC=openclaw\n" in out


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
