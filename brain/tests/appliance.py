#
# appliance.py — "this test needs hardware we don't have here", said once.
#
# Most of brain/tests is hermetic. Two scripts are not: one needs a live engine
# WebSocket, one a live LLM. Off the appliance they FAILED, which was wrong in a way
# that got steadily more expensive:
#
#   * `pytest brain/tests/` could never be green, while brain/tests/README.md called
#     green pytest the merge gate. A suite that is permanently two-red teaches
#     everyone to read past red, which is the whole value of the other twenty-one.
#   * A real regression in those two files looked EXACTLY like the environmental
#     failure — same red, same place. The one signal they could give was unreadable.
#   * CI worked around it with a hand-maintained denylist
#     (`-k "not test_engine_text_stream and not test_remember_tool"`) whose comment
#     said "the other five are hermetic" long after there were twenty-three. Every new
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
import threading
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
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError as e:
        # A bad port string (urlparse raises ValueError, not OSError, for that) is a
        # broken test/config, not an absent dependency — fail loud rather than skip,
        # and rather than let urlparse's own exception crash this with no context.
        raise ValueError(f"{url!r} is not a usable URL ({e}) for {what}") from e
    # An omitted port is NOT malformed — the scheme supplies it, and `wss://host/path`
    # is exactly what you get pointing ENGINE_TTS_STREAM_URL at a TLS-terminated
    # engine. Treating that as unusable rejected a valid config with a message
    # claiming the URL was broken, so resolve the scheme's default before giving up.
    port = port or {"ws": 80, "wss": 443, "http": 80, "https": 443}.get(parsed.scheme)
    if not host or not port:
        # Now genuinely unprobeable: no host, or a scheme we have no default for.
        # That is a broken test/config, not an absent dependency — fail loud, don't
        # skip, and don't silently return unchecked (which would hide the breakage
        # this module exists to expose behind whatever raw error comes downstream).
        raise ValueError(f"{url!r} has no host:port to probe for {what}")
    # A daemon thread, not a bare create_connection(): create_connection's timeout
    # bounds the TCP connect but not the DNS lookup it does first, so a stuck
    # resolver could otherwise turn a "clean skip" into an indefinite hang. Daemon so
    # that if it's still stuck when we give up, it can't also block process exit.
    outcome: list = []
    thread = threading.Thread(target=lambda: outcome.append(_probe(host, port, timeout)),
                               daemon=True)
    thread.start()
    thread.join(timeout)
    if not outcome or isinstance(outcome[0], OSError):
        detail = outcome[0].__class__.__name__ if outcome else f"no response within {timeout}s"
        skip(f"nothing listening at {host}:{port} ({detail}) — this test "
             f"needs {what}. It runs on the appliance; see brain/tests/README.md.")
    outcome[0].close()
    return url


def _probe(host: str, port: int, timeout: float):
    """Connect or return the OSError — run on a worker thread by require_reachable."""
    try:
        return socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        return e
