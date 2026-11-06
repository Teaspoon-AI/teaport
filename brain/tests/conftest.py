#
# conftest.py — checked before any pytest run under brain/tests starts (#220).
#
# The venv against brain/uv.lock (see locked_venv.py): one that has drifted from the lock
# stops the run here, with one line naming the venv, what differs, and what to do about
# it, instead of failing test by test on ModuleNotFoundError where each failure looks
# like a regression. A conftest rather than test_suite.py's own top: an error raised
# while a test file is collected is reported as a collection error, a traceback around
# the line, and this covers a direct `pytest brain/tests/test_x.py` too.
#
# A passing check says so in pytest's header (shown unless -q), so a CI log shows it ran;
# packages the lock does not have are named on a line of their own and do not stop it.
#
import sys
from functools import cache

import pytest

import locked_venv

_check = cache(locked_venv.check)


def pytest_report_header(config):
    return locked_venv.header(_check())


def pytest_sessionstart(session):
    c = _check()
    if c is None:
        return
    if (stop := locked_venv.problem(c)) is not None:
        raise pytest.UsageError(stop)
    if (note := locked_venv.extras(c)) is not None:
        tr = session.config.pluginmanager.get_plugin("terminalreporter")
        if tr is not None:
            tr.write_line(f"WARNING: {note}", yellow=True)
        else:
            print(f"WARNING: {note}", file=sys.stderr)
