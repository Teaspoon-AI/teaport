#
# test_suite.py — one-command safety net over the standalone test scripts.
#
# The test_*.py files in this directory are self-contained scripts (each has a
# __main__ that exits nonzero on failure). This wrapper lets `pytest test_suite.py`
# run them all — the refactor gate — without rewriting them as pytest natives.
# Run it from a venv built off the lock (brain/tests/README.md):
#   uv run --project brain python -m pytest brain/tests/test_suite.py -q
# On the appliance, the deployed venv has the model files and the pinned deps already:
#   /opt/teaport/venv/bin/python3 -m pytest test_suite.py -v
#
import ast
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
TIMEOUTS: dict[str, int] = {
    # Four calls through the real SIP brain, at real time (~75 s on a desktop).
    "test_sip_fake_gateway.py": 300,
    # Seven calls and five Talk/room sessions through the real brain (~3.5 min).
    "test_call_experience.py": 600,
}

# Scripts whose core assertions need real hardware/network (they call
# appliance.require_env / appliance.require_reachable at their own top level, which
# this file can't see just by discovering them). This one IS still hand-maintained —
# unlike SCRIPTS/EXCLUDED/TIMEOUTS above there's no signal here to derive it from
# without instrumenting appliance.py itself — but declaring it here makes the
# requirement enumerable instead of only readable by opening each script, and
# test_declared_appliance_scripts_match_their_guards below closes the gap that
# matters: a script gains a guard and isn't added here, or is listed here after its
# guard is removed, fails the suite instead of drifting unnoticed.
APPLIANCE_ONLY = {"test_engine_text_stream.py", "test_remember_tool.py"}


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
    empty_reasons = sorted(name for name, reason in EXCLUDED.items() if not reason.strip())
    assert not empty_reasons, f"EXCLUDED entries need a real reason: {empty_reasons}"
    assert SCRIPTS, "no test scripts discovered — is this running from brain/tests?"


def _runs_its_tests(path: str) -> bool:
    """A script that defines test_* functions must run them when executed: test_script
    runs each file as `python <file>`, and a pytest-style file with no __main__ exits 0
    having run nothing — a silent pass for every test in it (test_local_audio.py and
    test_wifi_setup.py both shipped like that). A file with no test_* functions is a
    plain script and runs top to bottom anyway."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    has_tests = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
                    for n in tree.body)
    has_main = any(isinstance(n, ast.If) and "__main__" in ast.unparse(n.test) for n in tree.body)
    return has_main or not has_tests


def test_every_script_runs_its_tests_when_executed():
    silent = sorted(name for name, _ in SCRIPTS if not _runs_its_tests(os.path.join(HERE, name)))
    assert not silent, (
        f"these define test_* functions but have no `if __name__ == \"__main__\":` to run "
        f"them, so the gate passes them without running a single test: {silent}")


def _calls_appliance_guard(path: str) -> bool:
    """True if the script at `path` actually calls appliance.require_env /
    require_reachable — including through `from appliance import require_env as x`.
    Parsed with ast rather than a substring search on the script's text: a substring
    match both false-positives (the words appearing in a comment or docstring) and
    false-negatives (an aliased import, which contains no "appliance.require_"
    substring at all)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    guard_names = {"require_env", "require_reachable"}
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "appliance":
            aliases.update(alias.asname or alias.name
                            for alias in node.names if alias.name in guard_names)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in guard_names:
            return True
        if isinstance(func, ast.Name) and func.id in aliases:
            return True
    return False


def test_declared_appliance_scripts_match_their_guards():
    """APPLIANCE_ONLY and each script's own appliance.require_* call have to agree,
    or a hardware-dependent test can silently FAIL in CI instead of SKIP — the same
    'someone has to remember' failure this file exists to eliminate, one level down.
    """
    guarded = {name for name, _ in SCRIPTS if _calls_appliance_guard(os.path.join(HERE, name))}
    assert guarded == APPLIANCE_ONLY, (
        f"APPLIANCE_ONLY and the scripts that actually call appliance.require_* "
        f"disagree — guarded but undeclared: {sorted(guarded - APPLIANCE_ONLY)}, "
        f"declared but unguarded: {sorted(APPLIANCE_ONLY - guarded)}"
    )


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
