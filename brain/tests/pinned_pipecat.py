#
# Refuse to report a pass against the wrong pipecat.
#
# The turn-strategy tests assert on pipecat internals — when a strategy resets, what an
# emptied analyzer buffer returns, which frames reach a stop strategy. Those differ
# between releases, so a green run against a different version means nothing at all.
#
# The appliance pin lives in brain/pyproject.toml and is read from there, never restated
# here: this comment used to name a version of its own and was wrong the day the pin moved
# to 1.7.0, disagreeing with the very message the module prints. A development machine that
# resolves pipecat from an unconstrained environment silently ends up on 0.0.108 instead —
# and a test written and "verified" there can fail the moment it runs where it matters.
# That happened on 2026-08-20 and cost a debugging cycle, which is why this is enforced
# rather than remembered.
#
# (This note used to add "that release is not on the public index". It was true when
# written and is not any more — 1.7.0 publishes to PyPI, so `uv venv --python 3.12` plus
# `uv pip install -e ./brain` gets a dev box onto the exact pin. See brain/tests/README.md.
# The guard still earns its keep: it is what catches the box that skipped that step.)
#
# FAILS LOUD, never open. Both fallbacks here used to `return`, which disabled the check
# in exactly the workflow the error message below tells you to use: copied to /tmp/btests
# on the appliance, the manifest is not beside the tests, the OSError branch fired, and the
# suite went green against whatever pipecat happened to be installed. A guard that cannot
# find its reference has failed, not passed.
#
import os

import pipecat

try:
    import tomllib  # 3.11+; the appliance is 3.12 (brain/pyproject.toml requires-python)
except ModuleNotFoundError:  # an older dev interpreter — fall back, never fail open
    tomllib = None

_PYPROJECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pyproject.toml")

# The pin, copied here ONLY as the fallback for a tests-directory that has been detached
# from the manifest (see above). pyproject.toml remains the source of truth and wins
# whenever it can be read; this exists so a detached copy still refuses rather than
# silently passing. Keep in step with brain/pyproject.toml.
_EXPECTED_FALLBACK = "1.7.0"


def _pinned_version() -> str:
    """The pipecat version brain/pyproject.toml pins, or the recorded fallback."""
    if tomllib is None:
        return _EXPECTED_FALLBACK
    try:
        with open(_PYPROJECT, "rb") as f:
            deps = tomllib.load(f)["project"]["dependencies"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return _EXPECTED_FALLBACK
    # tomllib, not a regex: the previous pattern required an extras bracket
    # (pipecat-ai\[[^\]]*\]==), so dropping "[websocket]" or writing "pipecat-ai == 1.7.0"
    # made it match nothing and the check quietly stopped enforcing anything.
    for dep in deps:
        name, sep, version = dep.partition("==")
        if sep and name.split("[")[0].strip() == "pipecat-ai":
            return version.strip()
    return _EXPECTED_FALLBACK


def require_pinned():
    """Raise unless the installed pipecat matches the pin. Call at import time."""
    want, have = _pinned_version(), pipecat.__version__
    if have != want:
        raise SystemExit(
            f"\nWRONG PIPECAT: installed {have}, this suite is only meaningful against "
            f"{want} (brain/pyproject.toml).\nRun it on the appliance:\n"
            f"  scp tests/*.py teaspoon@<box>:/tmp/btests/ && "
            f"/opt/teaport/venv/bin/python3 /tmp/btests/<test>.py\n"
        )
