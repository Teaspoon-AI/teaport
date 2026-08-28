#
# test_suite.py — one-command safety net over the standalone test scripts.
#
# The test_*.py files in this directory are self-contained scripts (each has a
# __main__ that exits nonzero on failure). This wrapper lets `pytest test_suite.py`
# run them all — the refactor gate — without rewriting them as pytest natives.
# Runs on the appliance (model files + venv deps live there):
#   ~/teaport-venv/bin/python3 -m pytest test_suite.py -v
#
import os
import subprocess
import sys

import pytest

import appliance

HERE = os.path.dirname(os.path.abspath(__file__))

# Discovered, not listed. A hand-maintained roster is the one thing this file has
# reliably got wrong: test_followup_injection.py sat on disk unlisted long enough for
# its import to break in a refactor with nothing noticing, and an audit on 2026-08-28
# turned up four more (consult_progress, raw_llm_capture, repeat_cut,
# tts_speech_hold) that pass in about a second each and had simply never been added.
# The same drift hit CI's `-k` denylist and the README's "hermetic" list; all three
# are now derived rather than remembered.
#
# Adding a test_*.py here is therefore all it takes to have it run. Excluding one is
# deliberate and must say why.
DEFAULT_TIMEOUT_S = 120

# {script: reason}. Empty on purpose — a script that cannot run belongs in
# appliance.py's skip path (which reports as skipped), not silently dropped here.
EXCLUDED: dict[str, str] = {}

# Overrides for anything the default doesn't fit.
TIMEOUTS: dict[str, int] = {}


def _discover():
    names = sorted(
        f for f in os.listdir(HERE)
        if f.startswith("test_") and f.endswith(".py") and f != "test_suite.py"
    )
    return [(n, TIMEOUTS.get(n, DEFAULT_TIMEOUT_S)) for n in names if n not in EXCLUDED]


SCRIPTS = _discover()


def test_every_script_on_disk_is_collected():
    """The roster is derived, so this only has to catch a stale EXCLUDED entry —
    a script deleted or renamed while its exclusion stayed behind."""
    missing = sorted(set(EXCLUDED) - set(os.listdir(HERE)))
    assert not missing, f"EXCLUDED names scripts that no longer exist: {missing}"
    assert SCRIPTS, "no test scripts discovered — is this running from brain/tests?"


@pytest.mark.parametrize("script,timeout", SCRIPTS,
                         ids=[s for s, _ in SCRIPTS])
def test_script(script, timeout):
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, script)],
        cwd=HERE, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "HF_HUB_OFFLINE": "1"},
    )
    # A script whose dependency is genuinely absent exits SKIP_EXIT (see
    # appliance.py). Skipping is what keeps the suite readable off the appliance:
    # it used to FAIL there, so `pytest brain/tests/` could never be green while
    # this suite's README called green pytest the merge gate — and a real
    # regression in those files was indistinguishable from not owning a Jetson.
    if proc.returncode == appliance.SKIP_EXIT:
        reason = next((ln for ln in proc.stdout.splitlines() if ln.startswith("SKIP:")),
                      f"{script} reported its dependency as unavailable")
        pytest.skip(reason)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + "\n" + proc.stderr).splitlines()[-25:])
        pytest.fail(f"{script} exited {proc.returncode}\n{tail}")
