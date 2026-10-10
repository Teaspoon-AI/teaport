#
# Unit test: the STT follows the engine's transcription report (teagram-engine's
# transcription.progress) and tells the stop strategy when the caller's speech is
# transcribed through (stt.py SPEECH_DECODED, endpointing.SpeechDecodedFrame).
#
# What these pin, in order:
#
#   * the handshake opts in (session.update "transcription_progress": true) -- to our
#     engine, not to the streaming backend, which validates its session -- and every
#     session.created tells the strategy whether this engine session gives the report
#     (its "capabilities"), so an engine without it leaves the strategy on its timer;
#   * the report goes onto the hint's clock: samples this service has sent, counted
#     from the session's start, so a reconnect -- whose engine counts from zero again
#     -- is placed where it began, and a report from the old session is forgotten;
#   * SpeechDecodedFrame goes out once the report passes the speech end placed at the
#     VAD stop plus the hint's margin -- not before, once per stop, and also at the stop
#     itself when the engine was already past it;
#   * a stop with no speech end (audio passed unsent; a commit already sent for it) gets
#     no frame, and neither does a session without the report.
#
# Run: python test_stt_transcription_progress.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from stt_harness import WireRecorder  # noqa: E402

import teaport_brain.stt as stt_mod  # noqa: E402
from teaport_brain.endpointing import (  # noqa: E402
    SpeechDecodedFrame,
    SttProgressFrame,
    TurnVerdictFrame,
)
from teaport_brain.stt import TeaportSTTService  # noqa: E402

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)
MARGIN = 100          # the hint's margin, which the report is held to too (see main)

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


class Recorder(TeaportSTTService):
    """The real service, its wire recorded and its pushed frames kept."""

    def __init__(self, **kwargs):
        super().__init__(url="ws://127.0.0.1:1/none", **kwargs)
        self._websocket = WireRecorder()
        self.pushed = []

    async def push_frame(self, frame, direction=DOWN):
        self.pushed.append(frame)

    async def start_processing_metrics(self):
        pass

    async def stop_processing_metrics(self):
        pass

    def create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro)

    async def cancel_task(self, task, timeout=None):
        task.cancel()

    def decoded(self):
        return [f.stop_n for f in self.pushed if isinstance(f, SpeechDecodedFrame)]

    def reported(self):
        return [f.reported for f in self.pushed if isinstance(f, SttProgressFrame)]


async def audio(s, ms):
    for _ in range(ms // CHUNK_MS):
        await s.process_frame(InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE,
                                                 num_channels=1), DOWN)


async def vad_stop(s, stop_secs=0.5):
    await s.process_frame(VADUserStoppedSpeakingFrame(stop_secs=stop_secs), UP)


async def vad_start(s):
    await s.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2), UP)


async def created(s, capabilities=("transcription.progress",)):
    msg = {"type": "session.created", "id": "sess_1", "created": 1}
    if capabilities is not None:
        msg["capabilities"] = list(capabilities)
    await s._handle_message(msg)


async def progress(s, ms):
    await s._handle_message({"type": "transcription.progress", "decoded_through_ms": ms})


async def connected(s):
    """A connect as _connect_websocket makes it, onto a recorder instead of a socket."""
    async def connect(url, **kwargs):
        return WireRecorder()
    real, stt_mod.websockets.connect = stt_mod.websockets.connect, connect
    try:
        s._websocket = None
        await s._connect_websocket()
    finally:
        stt_mod.websockets.connect = real


# ---- the handshake ----------------------------------------------------------------------

async def test_the_handshake_opts_in_and_the_session_says_whether_it_reports():
    s = Recorder()
    await connected(s)
    assert s._websocket.sent[0] == {"type": "session.update", "model": s._model,
                                    "transcription_progress": True}
    await created(s)
    await created(s, capabilities=None)        # an engine without the report
    await created(s, capabilities=[])
    assert s.reported() == [True, False, False], s.reported()


async def test_the_streaming_backend_is_not_asked():
    s = Recorder(streaming_backend=True)
    await connected(s)
    assert s._websocket.sent[0] == {"type": "session.update", "model": s._model}


# ---- the clock and the frame ----------------------------------------------------------

async def test_the_frame_goes_out_once_the_report_passes_the_speech_end_and_its_margin():
    s = Recorder()
    await created(s)
    await vad_start(s)
    await audio(s, 2000)
    await vad_stop(s, stop_secs=0.5)           # speech ended at 1500 ms of this session
    await progress(s, 1500 + MARGIN - 20)
    assert s.decoded() == [], "short of the margin past the speech end"
    await progress(s, 1500 + MARGIN)
    assert s.decoded() == [1], "the speech is transcribed through: VAD stop 1"
    await audio(s, 400)
    await progress(s, 2300)
    assert s.decoded() == [1], "once per stop"


async def test_a_report_already_past_the_speech_end_is_told_at_the_stop():
    s = Recorder()
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await vad_stop(s, stop_secs=0.5)
    await vad_start(s)                         # the caller resumes, then stops again
    await audio(s, 300)
    await progress(s, 900)                     # past the first stop's end, too late for it
    assert s.decoded() == [], "the resumed stop placed no speech end to pass"
    await audio(s, 2000)
    await progress(s, 2700)
    await vad_stop(s, stop_secs=1.0)           # speech ended at 2300 ms
    assert s.decoded() == [2], "the engine was past it when the stop came"


async def test_the_clock_counts_from_the_session_and_a_reconnect_starts_it_again():
    s = Recorder()
    await created(s)
    await audio(s, 3000)                       # 3 s of the first session
    await progress(s, 2000)
    s._websocket = None                        # the recorder has no close handshake
    await s._disconnect_websocket()
    assert s._decoded_through is None, "the old session's report is on its own clock"
    await connected(s)                         # the new session's clock starts at 3 s
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await vad_stop(s, stop_secs=0.5)           # speech ended 500 ms into this session
    await progress(s, 500 + MARGIN - 20)
    assert s.decoded() == []
    await progress(s, 500 + MARGIN)
    assert s.decoded() == [1], "the engine's 600 ms is this service's 3.6 s"


async def test_no_frame_without_a_speech_end_or_without_the_report():
    # audio passed unsent: the stop is not placed
    s = Recorder()
    await created(s)
    await vad_start(s)
    await audio(s, 600)
    ws, s._websocket = s._websocket, None
    await audio(s, 200)                        # buffered, not sent
    s._websocket = ws
    await audio(s, 400)
    await vad_stop(s, stop_secs=0.5)
    await progress(s, 5000)
    assert s.decoded() == [], "no speech end placed, nothing to pass"
    # a COMPLETE verdict commits at once: the stop is answered, nothing to tell
    s = Recorder()
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await vad_stop(s, stop_secs=0.5)
    await s.process_frame(TurnVerdictFrame(complete=True, source="verdict"), UP)
    await progress(s, 5000)
    assert s.decoded() == []
    # an engine session without the report: the field is never there to read
    s = Recorder()
    await created(s, capabilities=None)
    await vad_start(s)
    await audio(s, 1000)
    await vad_stop(s, stop_secs=0.5)
    assert s.decoded() == [] and s.reported() == [False]


def main():
    stt_mod._HINT_MARGIN_MS = MARGIN
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
