#
# Unit test: the SIP per-call lifecycle — call_id-guarded teardown, a receive loop
# that keeps reading, and serialized sends on the one socket.
#
# Three concurrency findings meet on the single AF_UNIX SEQPACKET connection that
# carries both call control and caller audio. All three are modelled in
# brain/formal/ and each counterexample is a test here.
#
#   1. `disconnected` ignored call_id and tore down whatever pipeline was running, so
#      a stale disconnected for a finished call killed the call that had REPLACED it.
#      The caller on the survivor is left connected to a gateway with no brain: no
#      audio, no hangup, and no further confirmed ever coming.
#      -> SipCall.tla, MODE = "asWritten", invariant NoWrongTeardown (9 steps).
#
#   2. on_call_state is dispatched sync=True, INLINE in the connection's only receive
#      loop, and it did the whole bring-up there — model construction plus greet()'s
#      12s STT poll plus the can't-hear branch's wait_until_delivered. For that long
#      nothing was read: not caller audio, and not a disconnected sitting in the queue.
#      -> SipCall.tla, invariant NoBlockedWithPendingControl.
#
#   3. SipConnection.send() issued concurrent loop.sock_sendall() on one fd. asyncio
#      keeps at most ONE writer per fd, so the second call cancels the first's handle
#      and the first future is never resolved — the audio MediaSender blocks forever
#      and the call goes silent with nothing logged.
#
# And one that is not concurrency but lives in the same teardown:
#
#   4. The per-call teardown never ran the session-end memory reclaim the OpenClaw path
#      runs in talk()'s finally. MemoryReclaim skips empty_cache per turn on purpose, so
#      the CUDA cache and the freed glibc arena pages are ONLY returned at session end;
#      on a phone-dedicated box (teaport-brain disabled) that made every call leak, until
#      the 8 GB unified pool OOMs. It must NOT run on a superseded teardown, where the
#      replacement call's bring-up is already starting behind it.
#
# Run: python test_sip_call_lifecycle.py
#
import asyncio
import os
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pinned_pipecat  # noqa: F401,E402  — refuse to pass against the wrong pipecat

from teaport_brain import sip_server  # noqa: E402
from teaport_brain.sip_serializer import encode_control  # noqa: E402
from teaport_brain.sip_transport import SipConnection  # noqa: E402


# --- 3. concurrent sends on one fd ----------------------------------------------

async def test_concurrent_sends_do_not_strand_each_other():
    """Without the lock the FIRST sender hangs forever: asyncio's second _add_writer
    on the same fd cancels the first's handle and its future is never resolved."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    a.setblocking(False)
    conn = SipConnection(a, None)
    # Fill the send buffer so a send must register a writer and wait.
    try:
        while True:
            a.send(b"x" * 8192)
    except OSError:
        pass

    done = []

    async def send(tag):
        await conn.send(encode_control({"type": "probe", "tag": tag}))
        done.append(tag)

    t1 = asyncio.create_task(send("audio"))
    await asyncio.sleep(0)          # let it register its writer
    t2 = asyncio.create_task(send("control"))
    await asyncio.sleep(0.05)
    # Drain the peer so the socket becomes writable again.
    b.setblocking(False)
    for _ in range(4000):
        try:
            b.recv(65536)
        except BlockingIOError:
            break
    try:
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)
    except asyncio.TimeoutError:
        for t in (t1, t2):
            t.cancel()
        raise AssertionError(
            f"a sender hung: completed={done}. Concurrent sock_sendall on one fd "
            f"strands the earlier waiter — send() must serialize.")
    finally:
        a.close()
        b.close()
    assert sorted(done) == ["audio", "control"], done


# --- 1 + 2. the call lifecycle over a real connection ----------------------------

class _FakeTask:
    def __init__(self):
        self.cancelled = False
        self.done = asyncio.Event()

    async def cancel(self):
        self.cancelled = True
        self.done.set()          # lets the fake runner finish, as a real one would


class _FakeGate:
    async def wait_until_delivered(self, start_timeout: float = 10.0):
        return


class _FakeSession:
    """Stands in for AgentSession. `greet_delay` models the real bring-up cost —
    model construction plus greet()'s STT poll — which is what used to block the
    receive loop."""
    greet_delay = 0.0
    built = []          # call ids, in build order
    greeted = []        # call ids that finished greeting
    by_id = {}          # call id -> the session built for it

    def __init__(self, call_id):
        self.call_id = call_id
        _FakeSession.by_id[call_id] = self
        self.task = _FakeTask()
        self.followup_gate = _FakeGate()
        self.should_end = False

    async def greet(self):
        await asyncio.sleep(self.greet_delay)
        _FakeSession.greeted.append(self.call_id)


class _Harness:
    """Runs the real sip_server.run() against a real SEQPACKET socket, with the
    pipeline itself stubbed out."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="teaport-sip-test-")
        self.path = os.path.join(self.dir, "gw.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(self.path)
        self.listener.listen(1)
        self.peer = None
        self.run_task = None
        self._pending = []
        # (was_on_the_event_loop_thread,) per reclaim, newest last.
        self.reclaims = []

    async def start(self):
        _FakeSession.built = []
        _FakeSession.greeted = []
        _FakeSession.by_id = {}
        self.reclaims = []
        self._patch()
        self.run_task = asyncio.create_task(sip_server.run(self.path))
        loop = asyncio.get_running_loop()
        self.peer, _ = await loop.run_in_executor(None, self.listener.accept)
        self.peer.setblocking(False)
        await asyncio.sleep(0.05)

    def _patch(self):
        self._orig_build = sip_server.build_agent_session
        self._orig_runner = sip_server.PipelineRunner
        self._orig_transport = sip_server.SipGatewayTransport
        self._orig_reclaim = sip_server.turn_reclaim

        def reclaim():
            self.reclaims.append(threading.current_thread() is threading.main_thread())
        sip_server.turn_reclaim = reclaim

        def build(transport, **kw):
            call_id = self._pending.pop(0)
            _FakeSession.built.append(call_id)
            return _FakeSession(call_id)
        sip_server.build_agent_session = build

        class _Runner:
            def __init__(self, **kw):
                pass

            async def run(self, task):
                await task.done.wait()      # the pipeline runs until its task is cancelled
        sip_server.PipelineRunner = _Runner

        class _Transport:
            def __init__(self, connection, params):
                pass

            async def send_control(self, msg):
                return
        sip_server.SipGatewayTransport = _Transport

    def restore(self):
        sip_server.build_agent_session = self._orig_build
        sip_server.PipelineRunner = self._orig_runner
        sip_server.SipGatewayTransport = self._orig_transport
        sip_server.turn_reclaim = self._orig_reclaim

    async def call_state(self, call_id, state):
        self._pending.append(call_id)
        self.peer.send(encode_control(
            {"type": "call.state", "call_id": call_id, "state": state}))

    async def send_raw(self, msg):
        self.peer.send(encode_control(msg))

    async def stop(self):
        if self.peer:
            self.peer.close()
        if self.run_task:
            self.run_task.cancel()
            try:
                await self.run_task
            except (asyncio.CancelledError, Exception):
                pass
        self.listener.close()
        self.restore()
        try:
            os.remove(self.path)
        except OSError:
            pass
        os.rmdir(self.dir)


async def test_a_stale_disconnected_does_not_tear_down_the_live_call():
    """The NoWrongTeardown counterexample: confirm A, confirm B (superseding A), then
    A's late disconnected arrives. It must not touch B."""
    h = _Harness()
    try:
        await h.start()
        await h.call_state("A", "confirmed")
        assert await wait_until(lambda: "A" in _FakeSession.built), "call A never built"
        await h.call_state("B", "confirmed")
        assert await wait_until(lambda: "B" in _FakeSession.built), "call B never built"
        assert _FakeSession.built == ["A", "B"], _FakeSession.built
        assert torn_down("A"), "confirming B should have superseded A"
        assert not torn_down("B"), "B was torn down before the stale event even arrived"

        # A's disconnected, arriving late — the exact stale event.
        await h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
        await asyncio.sleep(0.5)   # asserting a NEGATIVE: give it room to misbehave

        assert not torn_down("B"), (
            "a stale disconnected for call A tore down call B — B's caller is left on a "
            "gateway with no brain: no audio, no hangup, no further confirmed")
    finally:
        await h.stop()


async def test_the_matching_disconnected_does_tear_the_call_down():
    """The guard must not become a no-op: the RIGHT disconnected still works."""
    h = _Harness()
    try:
        await h.start()
        await h.call_state("A", "confirmed")
        assert await wait_until(lambda: "A" in _FakeSession.built), "call A never built"
        assert not torn_down("A")
        await h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
        assert await wait_until(lambda: torn_down("A")), (
            "the call's own disconnected did not tear it down")
    finally:
        await h.stop()


async def test_a_slow_bring_up_does_not_block_the_receive_loop():
    """greet() can take ~12s. While the handler ran inline, nothing was read from the
    socket for that whole time — no control, no caller audio."""
    _FakeSession.greet_delay = 1.0
    h = _Harness()
    try:
        await h.start()
        await h.call_state("A", "confirmed")
        assert await wait_until(lambda: "A" in _FakeSession.built), "call A never built"
        assert "A" not in _FakeSession.greeted, "greet finished too early to test this"

        # Mid-bring-up, the gateway reports the caller hung up. A blocked loop would not
        # read this until greet() returned ~1s later; the timeout here is well inside
        # greet_delay, so passing it means the loop really did keep reading.
        await h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
        assert await wait_until(lambda: torn_down("A"), timeout=0.5), (
            "the disconnected was not acted on while the bring-up was still running — "
            "the receive loop is blocked inside on_call_state for the whole bring-up")
        assert "A" not in _FakeSession.greeted, (
            "the bring-up ran to completion for a call that had already hung up")
    finally:
        _FakeSession.greet_delay = 0.0
        await h.stop()


# --- 4. the session-end reclaim --------------------------------------------------

async def test_a_finished_call_reclaims_its_memory_off_the_loop():
    """A call IS a session, so its teardown is the session end — the only point where
    empty_cache and malloc_trim ever run (MemoryReclaim skips both per turn). Without
    this the phone-only box ratchets RSS/VRAM every call until it OOMs."""
    h = _Harness()
    try:
        await h.start()
        await h.call_state("A", "confirmed")
        assert await wait_until(lambda: "A" in _FakeSession.built), "call A never built"
        await h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
        assert await wait_until(lambda: len(h.reclaims) == 1), (
            "the per-call teardown never reclaimed: the call's arena pages and CUDA "
            "cache are held until the process dies")
        assert h.reclaims == [False], (
            "the reclaim ran ON the event loop thread — gc + malloc_trim there stalls "
            "the receive loop, which is also the caller-audio path")
    finally:
        await h.stop()


async def test_a_superseded_call_leaves_the_reclaim_to_its_replacement():
    """The teardown that has a next call pending must skip it: the bring-up starts on
    this same loop right behind it, and empty_cache can contend the CUDA allocator lock
    against the greeting's synth. gateway_server spells this `if not slot_active()`,
    which is inert in THIS process — the SIP path never calls acquire_slot."""
    h = _Harness()
    try:
        await h.start()
        await h.call_state("A", "confirmed")
        assert await wait_until(lambda: "A" in _FakeSession.built), "call A never built"
        await h.call_state("B", "confirmed")
        assert await wait_until(lambda: "B" in _FakeSession.built), "call B never built"
        assert torn_down("A"), "confirming B should have superseded A"
        await asyncio.sleep(0.3)   # asserting a NEGATIVE: give it room to misbehave
        assert h.reclaims == [], (
            f"a superseded teardown reclaimed ({len(h.reclaims)}x) — that stalls the "
            f"new caller's setup for a reclaim their own hangup would have done")

        # ...and the replacement still reclaims when IT ends, so nothing is lost.
        await h.send_raw({"type": "call.state", "call_id": "B", "state": "disconnected"})
        assert await wait_until(lambda: len(h.reclaims) == 1), (
            "the surviving call did not reclaim at its own teardown, so the memory of "
            "BOTH calls is now held")
    finally:
        await h.stop()


async def test_a_teardown_the_call_id_guard_rejects_does_not_reclaim():
    """A stale disconnected tears nothing down, so it must not reclaim either: the
    live call is mid-conversation and a gc + malloc_trim would stall its audio."""
    h = _Harness()
    try:
        await h.start()
        await h.call_state("A", "confirmed")
        assert await wait_until(lambda: "A" in _FakeSession.built), "call A never built"
        await h.call_state("B", "confirmed")
        assert await wait_until(lambda: "B" in _FakeSession.built), "call B never built"
        await h.send_raw({"type": "call.state", "call_id": "A", "state": "disconnected"})
        await asyncio.sleep(0.3)   # asserting a NEGATIVE
        assert not torn_down("B"), "the stale disconnected tore down the live call"
        assert h.reclaims == [], (
            "a stale disconnected reclaimed on top of the live call — audio stall for "
            "a teardown that did not happen")
    finally:
        await h.stop()


async def wait_until(pred, timeout=3.0, what=""):
    """Poll `pred` rather than guessing a sleep. Superseding a call costs
    cancel_active_call's runner wait plus its 0.3s engine settle, so fixed sleeps here
    are a flake waiting to happen."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def torn_down(call_id) -> bool:
    """Was the pipeline built for `call_id` cancelled? Observed on the stub itself
    rather than by reaching into run()'s frame locals — that returned None whether or
    not it found anything, so `assert ... is None` passed for the wrong reason."""
    session = _FakeSession.by_id.get(call_id)
    assert session is not None, f"no pipeline was ever built for call {call_id}"
    return session.task.cancelled


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
