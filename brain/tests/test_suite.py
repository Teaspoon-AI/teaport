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

HERE = os.path.dirname(os.path.abspath(__file__))

# (script, timeout_s).
SCRIPTS = [
    ("test_captions.py", 120),
    ("test_user_transcript.py", 120),
    ("test_engine_text_stream.py", 120),
    ("test_heard_truncate.py", 120),
    ("test_ledger_words.py", 120),
    ("test_memory_recall.py", 120),
    ("test_remember_tool.py", 120),
    ("test_llm_text_guard.py", 120),
    ("test_endpointing.py", 120),
    ("test_stt_final.py", 120),
    ("test_final_survives_turn_start.py", 120),
    ("test_tool_call_indices.py", 120),
    ("test_tool_schema.py", 120),
    ("test_heard_fraction.py", 120),
    # Was absent from this list, and its import of _make_consult_followup broke when
    # the agent factory moved to agent_session.py — nothing ran it, so nothing noticed.
    ("test_followup_injection.py", 120),
    ("test_followup_trigger.py", 120),
    ("test_stt_connect_retry.py", 120),
    ("test_sip_call_lifecycle.py", 120),
    ("test_sip_output_framing.py", 120),
]


@pytest.mark.parametrize("script,timeout", SCRIPTS,
                         ids=[s for s, _ in SCRIPTS])
def test_script(script, timeout):
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, script)],
        cwd=HERE, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "HF_HUB_OFFLINE": "1"},
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + "\n" + proc.stderr).splitlines()[-25:])
        pytest.fail(f"{script} exited {proc.returncode}\n{tail}")
