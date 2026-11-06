#
# conftest.py — checked before any pytest run under brain/tests starts (#220).
#
# The venv against brain/uv.lock (see locked_venv.py): one that has drifted from the lock
# stops the run here, with one line naming the venv, what differs, and the `uv sync` that
# rebuilds it, instead of failing test by test on ModuleNotFoundError where each failure
# looks like a regression. A conftest rather than test_suite.py's own top: an error
# raised while a test file is collected is reported as a collection error, a traceback
# around the line, and this covers a direct `pytest brain/tests/test_x.py` too.
#
import pytest

import locked_venv


def pytest_sessionstart(session):
    if (stale := locked_venv.problem()) is not None:
        raise pytest.UsageError(stale)
