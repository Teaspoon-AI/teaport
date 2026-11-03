#
# Unit test: the STT follows the engine's transcription report (teagram-engine's
# transcription.progress) and tells the stop strategy when the caller's speech is
# transcribed through (stt.py SPEECH_DECODED, endpointing.SpeechDecodedFrame).
#
# What these pin, in order:
#
#   * the handshake opts in (session.update "transcription_progress": true) only when the
#     session will use the report (build_agent_session sets transcription_progress with
#     the speculative reply and TEAPORT_SPECULATE_ON_DECODE on) -- to our engine, not to
#     the streaming backend, which validates its session -- and every session.created
#     tells the strategy whether this engine session gives the report (offered in its
#     "capabilities" and asked for), so without it the strategy stays on its timer;
#   * the report goes onto the hint's clock: samples this service has sent, counted
#     from the session's start, so a reconnect -- whose engine counts from zero again
#     -- is placed where it began, and a report from the old session is forgotten;
#   * SpeechDecodedFrame goes out once the report passes the speech end placed at the
#     VAD stop plus the hint's margin -- not before, once per stop, and also at the stop
#     itself when the engine was already past it;
#   * a stop with no speech end (audio passed unsent) gets NoSpeechReportFrame instead,
#     so the strategy decides it with the window; a commit already sent for the stop,
#     or a session without the report, gets neither.
#   * the barge-in pauser's watch (stt.watch_heard, #275) is answered with
#     SpeechHeardFrame once the report passes the onset test's voice end plus the
#     margin -- all the audio sent, with no marks or audio passed unsent -- once, the
#     latest watch only, never across sessions; a sliced session waits on its end bound,
#     and a session without the report says no notice will come.
#
# Run: python test_stt_transcription_progress.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
from types import SimpleNamespace

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
    NoSpeechReportFrame,
    SpeechDecodedFrame,
    SpeechHeardFrame,
    SttProgressFrame,
    TurnVerdictFrame,
)
from teaport_brain.stt import TeaportSTTService  # noqa: E402

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)
MARGIN = 100          # how far past the speech end the report must be (see main)

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


class Recorder(TeaportSTTService):
    """The real service, its wire recorded and its pushed frames kept."""

    def __init__(self, progress=True, **kwargs):
        super().__init__(url="ws://127.0.0.1:1/none", **kwargs)
        self._websocket = WireRecorder()
        self.pushed = []
        self.transcription_progress = progress   # as build_agent_session sets it

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

    def unreported(self):
        return [f.stop_n for f in self.pushed if isinstance(f, NoSpeechReportFrame)]


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


async def test_with_the_speculation_off_or_on_its_window_nothing_is_asked():
    """transcription_progress unset (the speculative reply off, or set to the window):
    the handshake is the one before the report existed, and an engine that offers it
    is not taken up on it -- the strategy stays on the window."""
    s = Recorder(progress=False)
    await connected(s)
    assert s._websocket.sent[0] == {"type": "session.update", "model": s._model}
    await created(s)
    assert s.reported() == [False]
    await vad_start(s)
    await audio(s, 1000)
    ws, s._websocket = s._websocket, None
    await audio(s, 100)                        # unsent: the stop cannot be placed
    s._websocket = ws
    await vad_stop(s, stop_secs=0.5)
    assert s.decoded() == [] and s.unreported() == []


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
    assert s.unreported() == [1], "the strategy is told to decide the stop by the window"
    # a COMPLETE verdict commits at once: the stop is answered, nothing to tell
    s = Recorder()
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await vad_stop(s, stop_secs=0.5)
    await s.process_frame(TurnVerdictFrame(complete=True, source="verdict",
                                           stop_n=s._vad_stops), UP)
    await progress(s, 5000)
    assert s.decoded() == [] and s.unreported() == []
    # an engine session without the report: the field is never there to read
    s = Recorder()
    await created(s, capabilities=None)
    await vad_start(s)
    await audio(s, 1000)
    await vad_stop(s, stop_secs=0.5)
    assert s.decoded() == [] and s.reported() == [False]


# ---- the barge-in pauser's watch (barge_pause.py HEARD OUT, #275) ---------------------

class _Voice:
    """An analyzer whose marks put the caller's last voiced chunk `ago_ms` back."""

    def __init__(self, s, ago_ms):
        self.s, self.ago = s, ago_ms * SAMPLE_RATE // 1000

    def voice_marks(self):
        p = self.s._passed_samples
        return SimpleNamespace(received=p, analyzed=p, voice_end=p - self.ago,
                               onset_end=p - self.ago, in_run=False, paused=True,
                               stop_at=None, speech_end=None)


def heard(s):
    return [f.watch for f in s.pushed if isinstance(f, SpeechHeardFrame)]


async def test_the_pausers_watch_is_answered_once_the_report_passes_the_voice():
    s = Recorder()
    await created(s)
    await audio(s, 2000)
    s.vad_analyzer = _Voice(s, 500)            # the voice ended at 1500 ms: no VAD stop
    assert await s.watch_heard(7) is True
    await progress(s, 1500 + MARGIN - 20)
    assert heard(s) == [], "short of the margin past the voice's end"
    await progress(s, 1500 + MARGIN)
    assert heard(s) == [7], "every word of the speech is in: the pauser is told"
    await audio(s, 400)
    await progress(s, 2300)
    assert heard(s) == [7], "once per watch"


async def test_a_report_already_past_the_voice_answers_the_watch_at_once():
    s = Recorder()
    await created(s)
    await audio(s, 2000)
    await progress(s, 1900)
    s.vad_analyzer = _Voice(s, 800)
    assert await s.watch_heard(3) is True and heard(s) == [3]


async def test_with_no_marks_or_audio_unsent_the_watch_waits_for_all_audio_sent():
    s = Recorder()
    await created(s)
    await audio(s, 2000)
    assert await s.watch_heard(1) is True      # no marks: all 2000 ms sent so far
    await progress(s, 1900 + MARGIN)
    assert heard(s) == []
    await progress(s, 2000 + MARGIN)
    assert heard(s) == [1]
    # audio passed unsent since the caller's start: the marks cannot be placed
    s = Recorder()
    await created(s)
    await vad_start(s)
    await audio(s, 600)
    ws, s._websocket = s._websocket, None
    await audio(s, 200)                        # buffered, not sent
    s._websocket = ws
    await audio(s, 400)                        # 1000 ms sent, 1200 ms passed down
    s.vad_analyzer = _Voice(s, 900)
    assert await s.watch_heard(2) is True
    await progress(s, 300 + MARGIN)
    assert heard(s) == [], "the voice's end is not placed past a gap: all sent counts"
    await progress(s, 1000 + MARGIN)
    assert heard(s) == [2]


async def test_a_new_watch_replaces_the_last_and_a_new_session_drops_it():
    s = Recorder()
    await created(s)
    await audio(s, 1000)
    assert await s.watch_heard(1)
    await audio(s, 1000)
    assert await s.watch_heard(2)              # the caller spoke again: a later end
    await progress(s, 1000 + MARGIN)
    assert heard(s) == [], "the replaced watch is not answered"
    await progress(s, 2000 + MARGIN)
    assert heard(s) == [2]
    await audio(s, 500)
    assert await s.watch_heard(3) and heard(s) == [2]
    s._websocket = None
    await s._disconnect_websocket()
    await connected(s)
    await created(s)
    await progress(s, 60000)
    assert heard(s) == [2], "the old session's watch: the pauser's bound resumes it"


async def test_no_watch_without_a_report_and_a_sliced_session_waits_on_its_end_bound():
    s = Recorder()
    await created(s, capabilities=None)
    await audio(s, 1000)
    assert await s.watch_heard(1) is False, "no report will say: the pauser's window"
    # Sliced: a word is returned once complete, so the decoder's own position is no
    # bound on it -- the slices' end bound is (stt.py TURN TEXT).
    s = Recorder(progress=False)
    await created(s, capabilities=("transcription.progress", "transcription.preview"))
    s._slicing = True
    await audio(s, 1000)
    assert await s.watch_heard(4) is True
    await s._handle_message({"type": "transcription.progress", "decoded_through_ms": 5000,
                             "audio_through_ms": 5000, "end_through_ms": 1000 + MARGIN - 20})
    assert heard(s) == [], "the decoder is past it, the words ending in it are not all in"
    await s._handle_message({"type": "transcription.progress", "decoded_through_ms": 5000,
                             "audio_through_ms": 5000, "end_through_ms": 1000 + MARGIN})
    assert heard(s) == [4]


def main():
    stt_mod._REPORT_MARGIN_MS = MARGIN
    stt_mod._HINT_MARGIN_MS = 0       # the hint's margin is not the report's
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
