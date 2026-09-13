#
# Unit test: the streaming STT backend (vLLM serving Voxtral-Mini-4B-Realtime).
#
# The two servers speak the same events with the same field names, which makes them look
# interchangeable by URL. They are not: `final` on a commit means opposite things.
#
#   our engine   commit(final=True)  -> transcribe the buffer now, one done per segment
#   vLLM         commit(final=False) -> START generating; deltas stream from then on
#                commit(final=True)  -> END OF STREAM. Nothing more is transcribed for
#                                       the rest of the session.
#
# Sending our per-segment commit(final=True) to vLLM therefore kills transcription for
# the whole call on the first VAD stop -- measured 2026-09-10, where it presented as a
# server that accepted the handshake, accepted 110 audio frames, accepted the commit and
# then returned absolutely nothing.
#
# So in streaming mode there is no per-segment commit at all: generation is started once,
# and the segment boundary is drawn locally from the interim buffer.
#
# Run: python test_streaming_backend.py   (or via the suite)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import teaport_brain  # noqa: E402, F401
from stt_harness import WireRecorder  # noqa: E402
from teaport_brain.stt import TeaportSTTService  # noqa: E402


class Recorder(TeaportSTTService):
    """A real service (see stt_harness): a recording socket, the boundary timer as a
    plain asyncio task, and the frames and synthesised finals captured."""

    def __init__(self, streaming):
        super().__init__(url="ws://127.0.0.1:1/none", streaming_backend=streaming)
        self._websocket = WireRecorder()
        self.handled = []
        self.pushed = []

    async def start_processing_metrics(self):
        pass

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)

    # create_task/cancel_task come from BaseObject and need the task manager setup()
    # installs; outside a pipeline they are plain asyncio tasks, so the boundary timer
    # can be driven deterministically instead of slept through.
    def create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro)

    async def cancel_task(self, task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, msg):
        self.handled.append(msg)
        self._interim_buffer = ""          # the real handler clears it; mirror that


def _svc(streaming):
    return Recorder(streaming)


async def _audio(s, n=3):
    for _ in range(n):
        async for _f in s.run_stt(b"\x11\x22" * 160):
            pass


async def test_streaming_starts_generation_once_and_only_once():
    s = _svc(True)
    await _audio(s, 4)
    commits = s._websocket.commits()
    assert len(commits) == 1, f"{len(commits)} commits sent; generation starts ONCE"
    assert commits[0]["final"] is False, (
        "generation was started with final=True — that is vLLM's end-of-stream "
        "sentinel, so the call transcribes nothing from here on")


async def test_streaming_never_sends_a_segment_commit():
    """The VAD stop must not reach the wire: it would close the stream for the call."""
    s = _svc(True)
    await _audio(s, 2)
    s._interim_buffer = "Stop."
    await s._send_commit(final=True, why="vad-stop")
    await asyncio.sleep(0.02)
    finals = [c for c in s._websocket.commits() if c["final"] is True]
    assert not finals, (
        "a commit(final=True) went to the server — on vLLM that ends the session and "
        "every later segment comes back empty")


async def test_the_segment_boundary_waits_for_the_trailing_deltas():
    """The live bug, 2026-09-10 22:14. The model transcribes on a 480 ms delay, so at
    the VAD stop the end of the utterance has not arrived yet. Cutting immediately gave
    'Hey, can you' / 'hear me? Hey, can you hear' / 'me?' -- one question split over
    three turns, each carrying the previous one's tail."""
    import teaport_brain.stt as stt_mod
    real, stt_mod._STREAM_TAIL_SECS = stt_mod._STREAM_TAIL_SECS, 0.05
    try:
        s = _svc(True)
        await _audio(s, 1)
        s._interim_buffer = "Hey, can you"
        await s._send_commit(final=True, why="vad-stop")
        assert not s.handled, (
            "the boundary was drawn at the VAD stop — the trailing deltas have not "
            "arrived yet and the tail of this utterance will land in the next segment")
        s._interim_buffer += " hear me?"        # the late deltas
        await asyncio.sleep(0.12)
        assert len(s.handled) == 1, s.handled
        assert s.handled[0]["text"] == "Hey, can you hear me?", s.handled[0]
    finally:
        stt_mod._STREAM_TAIL_SECS = real


async def test_the_segment_boundary_is_drawn_from_the_interim_buffer():
    import teaport_brain.stt as stt_mod
    real, stt_mod._STREAM_TAIL_SECS = stt_mod._STREAM_TAIL_SECS, 0.02
    s = _svc(True)
    await _audio(s, 1)
    s._interim_buffer = "  Okay, stop.  "
    await s._send_commit(final=True, why="vad-stop")
    await asyncio.sleep(0.08)
    stt_mod._STREAM_TAIL_SECS = real
    assert len(s.handled) == 1, s.handled
    msg = s.handled[0]
    assert msg["type"] == "transcription.done"
    assert msg["text"] == "Okay, stop.", msg


async def test_the_synthesised_final_carries_its_text_for_the_segment_log():
    """_log_segment reads the message, not the buffer. An empty one would log every
    healthy streaming segment as 'EMPTY -- audio in, no words out' and drive the
    empty-final run detector into warning about a working system."""
    s = _svc(True)
    import teaport_brain.stt as stt_mod
    real, stt_mod._STREAM_TAIL_SECS = stt_mod._STREAM_TAIL_SECS, 0.02
    s._interim_buffer = "Try it again."
    await s._send_commit(final=True, why="vad-stop")
    await asyncio.sleep(0.08)
    stt_mod._STREAM_TAIL_SECS = real
    assert s.handled[0]["text"] == "Try it again."


async def test_the_incumbent_path_is_untouched():
    s = _svc(False)
    await _audio(s, 3)
    assert not s._websocket.commits(), "streaming logic leaked into the incumbent path"
    await s._send_commit(final=True, why="vad-stop")
    commits = s._websocket.commits()
    assert len(commits) == 1 and commits[0]["final"] is True, (
        f"the incumbent must still get its per-segment commit(final=True): {commits}")
    assert not s.handled, "the incumbent must not synthesise finals; the engine sends them"


async def test_a_reconnect_restarts_generation():
    """A new session needs a new start commit, or the stream is dead for the call."""
    s = _svc(True)
    await _audio(s, 2)
    assert s._gen_started is True
    s._websocket = WireRecorder()                    # reconnected
    s._gen_started = False                  # exactly what _connect_websocket resets
    await _audio(s, 2)
    commits = s._websocket.commits()
    assert len(commits) == 1 and commits[0]["final"] is False, (
        f"generation was not restarted after a reconnect: {commits}")


async def test_blank_deltas_are_not_announced_as_hypotheses():
    """The streaming model emits a delta per audio frame -- 12.5 a second, blank ones
    through silence. Each pushed InterimTranscriptionFrame re-arms the aggregator's
    user_turn_stop_timeout, so the turn can never stop and the model is never asked.
    Live 2026-09-10 22:20: 210 interims across one 16.9 s segment, then
    "SILENT TURN ... reached=['nothing']" while the caller said "Hello?" four times."""
    s = _svc(True)
    s._arm_stranded_commit = lambda: asyncio.sleep(0)

    for piece in ("", " ", "\n", "Hello", " there"):
        await TeaportSTTService._handle_message(
            s, {"type": "transcription.delta", "delta": piece})

    pushed = s.pushed
    assert len(pushed) == 2, (
        f"{len(pushed)} interim frames pushed for 5 deltas, 3 of them blank — every "
        "blank one re-arms the turn-stop timeout and starves the turn")
    assert s._interim_buffer == " \nHello there", repr(s._interim_buffer)
    assert pushed[-1].text == " \nHello there", pushed[-1].text


async def test_the_incumbent_still_announces_every_delta():
    """Our engine only sends a delta when it has text, so filtering there would be
    a behaviour change with no cause."""
    s = _svc(False)
    s._arm_stranded_commit = lambda: asyncio.sleep(0)
    for piece in ("", "Hi"):
        await TeaportSTTService._handle_message(
            s, {"type": "transcription.delta", "delta": piece})
    pushed = s.pushed
    assert len(pushed) == 2, f"incumbent path changed: {len(pushed)} frames for 2 deltas"


def main():
    async def run_all():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and asyncio.iscoroutinefunction(fn):
                await fn()
                print(f"  ok {name}")
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
    print("ALL PASS")
