#
# Unit test: a connect the engine REJECTS is retried; an unreachable host is not.
#
# The engine serves ONE STT session. Both front-ends free it by cancelling the previous
# pipeline and then handing the gap to a bare `await asyncio.sleep(0.3)` — a timing
# assumption standing in for synchronization, since nothing observes that the engine has
# actually processed the close. _connect_websocket used to make a SINGLE attempt and set
# _stt_available = False on it, and greet() polls only until the tri-state RESOLVES, so a
# 503 resolved to False in milliseconds: the user was told "the voice assistant is busy
# with another session" — and on SIP hung up on — while nothing was using it at all.
#
# TLC finds that with one OpenClaw session and one SIP call: brain/formal/SttSlot.tla,
# MODE = "fixedSettle", invariant NoFalseBusy.
#
# The retry must be narrow. A refused or blackholed host will not fix itself, and each
# attempt costs the websockets open timeout (~10s), which would blow through greet()'s
# resolution window and leave the caller in silence rather than hearing the warning. So
# these tests pin BOTH directions.
#
# _is_slot_busy is duck-typed because websockets is deliberately unpinned (see
# brain/pyproject.toml) and the status field moved between releases, so the real
# exception objects from the installed version are exercised here rather than mocks.
#
# Run: python test_stt_connect_retry.py
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pinned_pipecat  # noqa: F401,E402  — refuse to pass against the wrong pipecat

import websockets  # noqa: E402
from websockets.datastructures import Headers  # noqa: E402
from websockets.exceptions import InvalidStatus  # noqa: E402
from websockets.http11 import Response  # noqa: E402

from teaport_brain import stt as stt_mod  # noqa: E402
from teaport_brain import agent_session  # noqa: E402
from teaport_brain.agent_session import AgentSession  # noqa: E402

# Don't sit through greet()'s real 12s resolution window in the unresolved case.
agent_session._STT_RESOLVE_POLLS = 2
agent_session._STT_RESOLVE_INTERVAL_S = 0.01
from teaport_brain.stt import _CONNECT_ATTEMPTS, TeaportSTTService, _is_slot_busy  # noqa: E402


def rejection(status=503):
    """A real rejection object from the INSTALLED websockets, not a stand-in."""
    return InvalidStatus(Response(status, "Service Unavailable", Headers(), None))


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        pass


def _service():
    svc = TeaportSTTService(url="ws://127.0.0.1:1/none")
    svc._websocket = None
    return svc


async def _run_connect(failures, service=None):
    """Drive _connect_websocket with `failures` queued exceptions, then success.
    Returns (service, attempts, elapsed)."""
    svc = service or _service()
    queue = list(failures)
    attempts = []

    async def fake_connect(url, *a, **kw):
        attempts.append(url)
        if queue:
            raise queue.pop(0)
        return _FakeSocket()

    real, websockets.connect = websockets.connect, fake_connect
    # stt.py holds its own module-global reference to the websockets module.
    real_mod, stt_mod.websockets = stt_mod.websockets, websockets
    try:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await svc._connect_websocket()
        elapsed = loop.time() - t0
    finally:
        websockets.connect = real
        stt_mod.websockets = real_mod
    return svc, len(attempts), elapsed


async def test_a_rejection_is_retried_and_can_succeed():
    """The whole point: the slot frees up a moment later and the caller still gets in."""
    svc, attempts, _ = await _run_connect([rejection(), rejection()])
    assert attempts == 3, f"made {attempts} attempt(s); the retry did not happen"
    assert svc.stt_available is True, (
        "declared itself deaf even though a retry connected — this is the false-busy bug")


async def test_a_rejection_that_never_clears_still_gives_up():
    """A genuinely busy engine must still reach the spoken warning, not hang."""
    svc, attempts, _ = await _run_connect([rejection()] * (_CONNECT_ATTEMPTS + 2))
    assert attempts == _CONNECT_ATTEMPTS, f"made {attempts}, expected {_CONNECT_ATTEMPTS}"
    assert svc.stt_available is False, "must resolve the tri-state so greet() can warn"


async def test_an_unreachable_host_is_not_retried():
    """Each attempt would cost the websockets open timeout (~10s) for nothing, and
    greet() would stop waiting before the warning could be spoken."""
    for exc, label in [(ConnectionRefusedError(), "refused"),
                       (OSError("no route to host"), "unreachable"),
                       (TimeoutError(), "blackhole")]:
        svc, attempts, _ = await _run_connect([exc] * 5)
        assert attempts == 1, f"{label}: retried {attempts} times; must not retry"
        assert svc.stt_available is False, label


async def test_the_retry_fits_inside_the_greeting_window():
    """greet() waits ~12s for the tri-state to resolve. If the retry budget outran that,
    the caller would hear nothing at all instead of the busy line."""
    _, _, elapsed = await _run_connect([rejection()] * (_CONNECT_ATTEMPTS + 2))
    assert elapsed < 10.0, f"retry budget took {elapsed:.1f}s; greet() gives up at ~12s"


async def test_a_failed_handshake_does_not_leak_the_slot():
    """An OPEN socket abandoned after a failed handshake holds the engine's single slot
    until TCP notices — 'talks but can't hear' for every later session."""
    closed = []

    class _RefusingSocket(_FakeSocket):
        async def send(self, payload):
            raise OSError("handshake write failed")

        async def close(self):
            closed.append(True)

    svc = _service()

    async def fake_connect(url, *a, **kw):
        return _RefusingSocket()

    real, websockets.connect = websockets.connect, fake_connect
    real_mod, stt_mod.websockets = stt_mod.websockets, websockets
    try:
        await svc._connect_websocket()
    finally:
        websockets.connect = real
        stt_mod.websockets = real_mod
    assert closed, "the half-open socket was dropped without closing — leaks the slot"
    assert svc._websocket is None
    assert svc.stt_available is False


def test_is_slot_busy_against_the_installed_websockets():
    assert _is_slot_busy(rejection(503)) is True, "a real 503 must be retryable"
    assert _is_slot_busy(rejection(500)) is True
    for exc in (ConnectionRefusedError(), OSError("no route"), TimeoutError(),
                Exception("unrecognised")):
        assert _is_slot_busy(exc) is False, f"{type(exc).__name__} must NOT be retryable"


# --- what the caller is TOLD -----------------------------------------------------
#
# "busy with another session" and "the engine is down" are different problems with
# different remedies, and greet() used to speak the first for both. A caller whose
# engine service is dead was sent to the FAQ's "stop the local brain so the line wins
# the slot" — a fix for a problem they do not have. slot_busy exists to tell them
# apart, and it is exactly the distinction the connect retry already has to make.


class _FakeStt:
    def __init__(self, available, slot_busy):
        self.stt_available = available
        self.slot_busy = slot_busy


class _FakeTask:
    def __init__(self):
        self.spoken = []

    async def queue_frames(self, frames):
        for f in frames:
            self.spoken.append(getattr(f, "text", None))


def _session(available, slot_busy):
    task = _FakeTask()
    return AgentSession(task=task, context=None, stt=_FakeStt(available, slot_busy),
                        ledger=None, followup_gate=None), task


async def test_a_held_slot_says_busy():
    session, task = _session(False, True)
    await session.greet()
    assert session.should_end is True
    assert any("busy with another session" in (t or "") for t in task.spoken), task.spoken


async def test_an_unreachable_engine_does_not_blame_another_session():
    session, task = _session(False, False)
    await session.greet()
    assert session.should_end is True
    said = " ".join(t or "" for t in task.spoken)
    assert "speech recognition isn't available" in said, said
    assert "busy with another session" not in said, (
        "an unreachable engine was reported as another session holding the slot — "
        "that sends the user to the wrong remedy entirely")


async def test_an_unresolved_tristate_is_not_reported_as_busy():
    """still-None after the poll window means we never got an answer, which is a
    down/unreachable engine, not a contended slot."""
    session, task = _session(None, None)
    await session.greet()
    assert session.should_end is True
    said = " ".join(t or "" for t in task.spoken)
    assert "busy with another session" not in said, said


def main():
    sync = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    for fn in sync:
        fn()
        print(f"  ok {fn.__name__}")

    async def run():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
