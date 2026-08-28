#
# appliance.py — "this test needs hardware we don't have here", said once.
#
# Most of brain/tests is hermetic. Two scripts are not: one needs a live engine
# WebSocket, one a live LLM. Off the appliance they FAILED, which was wrong in a way
# that got steadily more expensive:
#
#   * `pytest brain/tests/` could never be green, while brain/tests/README.md called
#     green pytest the merge gate. A suite that is permanently two-red teaches
#     everyone to read past red, which is the whole value of the other seventeen.
#   * A real regression in those two files looked EXACTLY like the environmental
#     failure — same red, same place. The one signal they could give was unreadable.
#   * CI worked around it with a hand-maintained denylist
#     (`-k "not test_engine_text_stream and not test_remember_tool"`) whose comment
#     said "the other five are hermetic" long after there were seventeen. Every new
#     appliance-dependent test had to remember to edit that string, and one that
#     forgot would fail CI rather than skip.
#
# So a script whose dependency is genuinely absent exits SKIP_EXIT and test_suite.py
# turns that into a pytest skip. The distinction is the point: absent dependency ->
# skip, dependency present and the test unhappy -> fail. Nothing here ever swallows a
# real failure, because every check below probes only for REACHABILITY, never for
# whether the test would pass.
#
# 77 is the GNU autotools convention for "skipped", chosen so it cannot collide with
# the 0/1 the scripts already use, nor with 124 (timeout) or 130 (SIGINT).
#
import os
import socket
import sys
from urllib.parse import urlparse

SKIP_EXIT = 77


def skip(reason: str):
    """Report the test as skipped, not failed, and exit."""
    print(f"SKIP: {reason}")
    sys.exit(SKIP_EXIT)


def require_env(name: str, what: str):
    """Skip unless `name` is set to something non-empty."""
    value = (os.getenv(name) or "").strip()
    if not value:
        skip(f"{name} is not set — this test needs {what}. "
             f"It runs on the appliance; see brain/tests/README.md.")
    return value


def require_reachable(url: str, what: str, timeout: float = 3.0):
    """Skip unless a TCP connection to `url`'s host:port succeeds.

    Reachability ONLY. If the port answers, the test runs and any failure after that
    is a real one — the engine being wrong is exactly what these tests are for.
    """
    parsed = urlparse(url)
    host, port = parsed.hostname, parsed.port
    if not host or not port:
        # Don't skip on something we failed to parse — that is a broken test, and
        # silently skipping it would hide the breakage this module exists to expose.
        return url
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as e:
        skip(f"nothing listening at {host}:{port} ({e.__class__.__name__}) — this test "
             f"needs {what}. It runs on the appliance; see brain/tests/README.md.")
    return url
