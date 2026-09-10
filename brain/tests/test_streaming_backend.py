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
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import teaport_brain  # noqa: E402, F401
from teaport_brain.stt import TeaportSTTService  # noqa: E402


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def types(self):
        return [m["type"] for m in self.sent]

    def commits(self):
        return [m for m in self.sent if m["type"] == "input_audio_buffer.commit"]


def _svc(streaming):
    s = TeaportSTTService.__new__(TeaportSTTService)
    s._name = "TeaportSTTService#test"      # BaseObject.__str__ reads this; run_stt logs
    s._streaming = streaming
    s._gen_started = False
    s._websocket = _WS()
    s._interim_buffer = ""
    s._seg_bytes = 0
    s._commit_at = None
    s._commit_why = ""
    s._seg_interims = 0
    s.handled = []

    async def handle(msg):
        s.handled.append(msg)
        s._interim_buffer = ""          # the real handler clears it; mirror that
    s._handle_message = handle
    return s


async def _audio(s, n=3):
    for _ in range(n):
        async for _f in TeaportSTTService.run_stt(s, b"\x11\x22" * 160):
            pass


async def test_streaming_starts_generation_once_and_only_once():
    s = _svc(True)
    s.start_processing_metrics = lambda: asyncio.sleep(0)
    await _audio(s, 4)
    commits = s._websocket.commits()
    assert len(commits) == 1, f"{len(commits)} commits sent; generation starts ONCE"
    assert commits[0]["final"] is False, (
        "generation was started with final=True — that is vLLM's end-of-stream "
        "sentinel, so the call transcribes nothing from here on")


async def test_streaming_never_sends_a_segment_commit():
    """The VAD stop must not reach the wire: it would close the stream for the call."""
    s = _svc(True)
    s.start_processing_metrics = lambda: asyncio.sleep(0)
    await _audio(s, 2)
    s._interim_buffer = "Stop."
    await TeaportSTTService._send_commit(s, final=True, why="vad-stop")
    finals = [c for c in s._websocket.commits() if c["final"] is True]
    assert not finals, (
        "a commit(final=True) went to the server — on vLLM that ends the session and "
        "every later segment comes back empty")


async def test_the_segment_boundary_is_drawn_from_the_interim_buffer():
    s = _svc(True)
    s.start_processing_metrics = lambda: asyncio.sleep(0)
    await _audio(s, 1)
    s._interim_buffer = "  Okay, stop.  "
    await TeaportSTTService._send_commit(s, final=True, why="vad-stop")
    assert len(s.handled) == 1, s.handled
    msg = s.handled[0]
    assert msg["type"] == "transcription.done"
    assert msg["text"] == "Okay, stop.", msg
    assert msg.get("streaming_boundary") is True


async def test_the_synthesised_final_carries_its_text_for_the_segment_log():
    """_log_segment reads the message, not the buffer. An empty one would log every
    healthy streaming segment as 'EMPTY -- audio in, no words out' and drive the
    empty-final run detector into warning about a working system."""
    s = _svc(True)
    s.start_processing_metrics = lambda: asyncio.sleep(0)
    s._interim_buffer = "Try it again."
    await TeaportSTTService._send_commit(s, final=True, why="vad-stop")
    assert s.handled[0]["text"] == "Try it again."


async def test_the_incumbent_path_is_untouched():
    s = _svc(False)
    s.start_processing_metrics = lambda: asyncio.sleep(0)
    await _audio(s, 3)
    assert not s._websocket.commits(), "streaming logic leaked into the incumbent path"
    await TeaportSTTService._send_commit(s, final=True, why="vad-stop")
    commits = s._websocket.commits()
    assert len(commits) == 1 and commits[0]["final"] is True, (
        f"the incumbent must still get its per-segment commit(final=True): {commits}")
    assert not s.handled, "the incumbent must not synthesise finals; the engine sends them"


async def test_a_reconnect_restarts_generation():
    """A new session needs a new start commit, or the stream is dead for the call."""
    s = _svc(True)
    s.start_processing_metrics = lambda: asyncio.sleep(0)
    await _audio(s, 2)
    assert s._gen_started is True
    s._websocket = _WS()                    # reconnected
    s._gen_started = False                  # exactly what _connect_websocket resets
    await _audio(s, 2)
    commits = s._websocket.commits()
    assert len(commits) == 1 and commits[0]["final"] is False, (
        f"generation was not restarted after a reconnect: {commits}")


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
