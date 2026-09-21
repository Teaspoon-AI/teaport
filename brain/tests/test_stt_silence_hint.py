#
# Unit test: the commit tells the engine how much trailing silence the VAD has already
# established (teaport#53, teagram-engine#37).
#
# The engine right-pads every final commit with ~1 s of zeros to flush its delayed
# decoder, and since teagram-engine#36 credits trailing silence it has fed against that
# pad -- counted by its own VAD, which on a phone line hears the noise floor as speech
# and credits next to nothing. Silero, upstream, has already decided: the VAD stop this
# service commits on IS the finding that the last stop_secs were silence, and everything
# sent between that stop and the commit is silence too. So the commit carries
# `trailing_silence_ms`: the audio sent since Silero's speech end, less a margin.
#
# What these pin, in order:
#
#   * the count is a SAMPLE position on the service's own send clock -- the stop's
#     silence window plus what went out after it -- never a wall-clock difference,
#     and the margin comes off it;
#   * every commit that answers a VAD stop carries it: the verdict's, the ceiling's,
#     the flush over the bot's voice, the raw stop in vad-stop mode, the hold's
#     expiry, the tail after the engine's own close;
#   * a stop whose count cannot be trusted carries nothing: the caller resumed (the
#     next stop places its own), or audio passed the service UNSENT since the caller
#     last started speaking (the one way the speech end could be placed earlier than
#     it was -- the over-claim direction);
#   * the backstop -- a segment whose stop never came -- has no speech end to count
#     from and carries nothing; nor does a count the margin swallows;
#   * the switch takes the field off the wire and nothing else;
#   * the segment line reports what was claimed and what the engine credited;
#   * and the debug skew probe reads how far the service's clock had run past the
#     VAD's position when the stop reached it, from the two processors' audio counts.
#
# Run: python test_stt_silence_hint.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from loguru import logger  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import FrameProcessed, FramePushed  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from stt_harness import WireRecorder  # noqa: E402

import teaport_brain.stt as stt_mod  # noqa: E402
from teaport_brain.endpoint_debug import VadStopSkew  # noqa: E402
from teaport_brain.endpointing import TurnVerdictFrame  # noqa: E402
from teaport_brain.stt import TeaportSTTService  # noqa: E402

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)
MARGIN = 100          # what the service holds back (patched below, see main)
CEILING = 0.1         # the analyzer's ceiling and the backstop's quiet, shortened
QUIET = 0.05

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


class Recorder(TeaportSTTService):
    """The real service, its wire recorded; audio driven through the real frame path so
    process_audio_frame (the gap watch) is exercised, not bypassed."""

    def __init__(self, **kwargs):
        kwargs.setdefault("commit_on", "verdict")
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


async def audio(s, ms):
    for _ in range(ms // CHUNK_MS):
        await s.process_frame(InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE,
                                                 num_channels=1), DOWN)


async def vad_stop(s, stop_secs=0.5):
    await s.process_frame(VADUserStoppedSpeakingFrame(stop_secs=stop_secs), UP)


async def vad_start(s):
    await s.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2), UP)


async def verdict(s, complete, source="verdict"):
    await s.process_frame(TurnVerdictFrame(complete=complete, source=source), UP)


async def delta(s, text):
    await s._handle_message({"type": "transcription.delta", "delta": text})


def hints(s):
    """trailing_silence_ms per commit on the wire, None where the commit carried none."""
    return [m.get("trailing_silence_ms") for m in s._websocket.commits()]


# ---- the count ------------------------------------------------------------------------

async def test_the_verdicts_commit_claims_the_stops_window_plus_what_followed_less_margin():
    s = Recorder()
    await vad_start(s)
    await audio(s, 1000)                       # the utterance
    await vad_stop(s, stop_secs=0.5)           # ...whose last 500 ms were silence
    await audio(s, 200)                        # the model's inference, more silence
    await verdict(s, True)
    assert hints(s) == [500 + 200 - MARGIN], (
        f"the hint must be the stop's window plus the audio sent since, less the "
        f"margin; got {hints(s)}")
    assert s._websocket.commits()[0]["final"] is True


async def test_the_count_is_a_sample_position_not_a_clock():
    """No audio after the stop, however long the wait: the claim does not grow."""
    s = Recorder()
    await vad_start(s)
    await audio(s, 600)
    await vad_stop(s, stop_secs=0.5)
    await asyncio.sleep(0.15)                  # wall time passes, no samples do
    await verdict(s, True)
    assert hints(s) == [500 - MARGIN]


async def test_every_commit_that_answers_a_stop_carries_it():
    # The ceiling's, after a hold: the whole hold is silence too.
    s = Recorder()
    await vad_start(s)
    await audio(s, 400)
    await vad_stop(s, stop_secs=0.5)
    await verdict(s, False)
    await audio(s, 1000)
    await verdict(s, True, source="ceiling")
    assert hints(s) == [500 + 1000 - MARGIN]

    # The flush over the bot's voice, at the stop itself.
    s = Recorder()
    await s.process_frame(BotStartedSpeakingFrame(), UP)
    await vad_start(s)
    await audio(s, 400)
    await vad_stop(s, stop_secs=0.5)
    assert hints(s) == [500 - MARGIN], "the barge-in flush must claim the stop's window"
    await s.process_frame(BotStoppedSpeakingFrame(), UP)

    # The raw stop, in vad-stop mode.
    s = Recorder(commit_on="vad-stop")
    await vad_start(s)
    await audio(s, 400)
    await vad_stop(s, stop_secs=0.2)
    assert hints(s) == [200 - MARGIN]

    # The hold's expiry: nothing answered the stop, the silence is still silence.
    s = Recorder()
    await vad_start(s)
    await audio(s, 400)
    await vad_stop(s, stop_secs=0.5)
    await verdict(s, False)
    await audio(s, 200)
    await asyncio.sleep(CEILING + stt_mod._HOLD_SLACK_SECS + 0.05)
    assert hints(s) == [500 + 200 - MARGIN]

    # The tail after the engine's own close: the stop commits it at once, with the
    # window it found.
    s = Recorder()
    await vad_start(s)
    await audio(s, 400)
    await s._handle_message({"type": "transcription.done", "text": "hello", "reason": "vad"})
    await audio(s, 600)
    await vad_stop(s, stop_secs=0.5)
    assert hints(s) == [500 - MARGIN]


# ---- when the stop's count cannot be trusted --------------------------------------------

async def test_a_resume_voids_the_stops_placement_and_the_next_stop_places_its_own():
    s = Recorder()
    await vad_start(s)
    await audio(s, 400)
    await vad_stop(s, stop_secs=0.5)
    await verdict(s, False)
    await vad_start(s)                         # the caller resumed: that silence is over
    await audio(s, 2000)                       # ...and this is speech
    await vad_stop(s, stop_secs=0.5)
    await audio(s, 200)
    await verdict(s, True)
    assert hints(s) == [500 + 200 - MARGIN], (
        "the claim must count from the SECOND stop only; the first's silence was "
        "followed by two seconds of speech")


async def test_audio_that_passed_unsent_withholds_the_hint_until_the_caller_next_starts():
    """A stop placed on a send clock that missed audio the VAD heard would put the
    speech end earlier than it was -- the over-claim direction. Two ways audio passes
    this service unsent: run_stt finds no socket, and the base class buffers for a
    reconnect before run_stt is reached. Both are caught at the seam."""
    for gap in ("no-socket", "reconnecting"):
        s = Recorder()
        await vad_start(s)
        await audio(s, 400)
        ws = s._websocket
        if gap == "no-socket":
            s._websocket = None
        else:
            s._reconnecting = True
        await audio(s, 100)                    # heard downstream, never on the wire
        s._websocket = ws
        s._reconnecting = False
        await audio(s, 400)
        await vad_stop(s, stop_secs=0.5)
        await verdict(s, True)
        assert hints(s) == [None], f"[{gap}] a stop after a gap must carry no hint"
        # The next utterance starts clean: the gap is behind its speech end.
        await vad_start(s)
        await audio(s, 600)
        await vad_stop(s, stop_secs=0.5)
        await verdict(s, True)
        assert hints(s) == [None, 500 - MARGIN], f"[{gap}] a VAD start must forgive the gap"


async def test_a_dropped_send_is_a_gap_too():
    class Dead:
        async def send(self, raw):
            raise OSError("socket gone")

    s = Recorder()
    await vad_start(s)
    await audio(s, 200)
    live = s._websocket
    s._websocket = Dead()
    await audio(s, 20)
    s._websocket = live
    await audio(s, 600)
    await vad_stop(s, stop_secs=0.5)
    await verdict(s, True)
    assert hints(s) == [None]


async def test_the_backstop_carries_nothing_and_so_does_a_count_the_margin_swallows():
    s = Recorder()
    await delta(s, "stop there")               # interims, no VAD stop ever
    await asyncio.sleep(QUIET * 3)
    assert [m["type"] for m in s._websocket.sent] == ["input_audio_buffer.commit"]
    assert hints(s) == [None], "a segment whose stop never came has no speech end to count from"

    s = Recorder()
    await vad_start(s)
    await audio(s, 400)
    await vad_stop(s, stop_secs=0.0)           # a window the margin more than covers
    await verdict(s, True)
    assert hints(s) == [None], "a non-positive claim must not be sent"


async def test_the_switch_takes_the_field_off_the_wire():
    stt_mod.STT_SILENCE_HINT = False
    try:
        s = Recorder()
        await vad_start(s)
        await audio(s, 400)
        await vad_stop(s, stop_secs=0.5)
        await verdict(s, True)
        assert hints(s) == [None]
        assert s._websocket.commits() == [{"type": "input_audio_buffer.commit", "final": True}]
    finally:
        stt_mod.STT_SILENCE_HINT = True


# ---- what the journal says --------------------------------------------------------------

async def test_the_segment_line_reports_the_claim_and_the_credit():
    sink = []
    handle = logger.add(lambda m: sink.append(m.record["message"]), level="DEBUG")
    try:
        s = Recorder()
        await vad_start(s)
        await audio(s, 400)
        await vad_stop(s, stop_secs=0.5)
        await verdict(s, True)
        await s._handle_message({"type": "transcription.done", "text": "hello",
                                 "reason": "commit", "credited_ms": 320})
        seg = [x for x in sink if "segment" in x and "commit=verdict" in x]
        assert len(seg) == 1, sink
        assert f"hint {500 - MARGIN}ms" in seg[0] and "credited 320ms" in seg[0], seg[0]
        # An engine without #37 sends no credited_ms; the claim is still on the line.
        s = Recorder()
        await vad_start(s)
        await audio(s, 400)
        await vad_stop(s, stop_secs=0.5)
        await verdict(s, True)
        await s._handle_message({"type": "transcription.done", "text": "hello"})
        seg = [x for x in sink if "segment" in x and "commit=verdict" in x]
        assert len(seg) == 2 and "hint" in seg[1] and "credited" not in seg[1], seg[1]
    finally:
        logger.remove(handle)


async def test_the_skew_probe_reads_the_two_clocks():
    """The aggregator pushes the stop upstream inside the frame that tripped its VAD;
    by the time the STT processes it, the STT has begun two more frames: +40 ms."""
    sink = []
    handle = logger.add(lambda m: sink.append(m.record["message"]), level="INFO")
    try:
        stt = Recorder()
        aggregator = object()
        between = object()
        probe = VadStopSkew(stt)
        frame = InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1)
        for _ in range(50):
            await probe.on_process_frame(FrameProcessed(processor=stt, frame=frame,
                                                        direction=DOWN, timestamp=0))
        for _ in range(48):
            await probe.on_process_frame(FrameProcessed(processor=aggregator, frame=frame,
                                                        direction=DOWN, timestamp=0))
        stop = VADUserStoppedSpeakingFrame(stop_secs=0.5)
        await probe.on_push_frame(FramePushed(source=aggregator, destination=between,
                                              frame=stop, direction=UP, timestamp=0))
        # The processor between re-pushes the same frame: it must not become the origin.
        await probe.on_push_frame(FramePushed(source=between, destination=stt,
                                              frame=stop, direction=UP, timestamp=0))
        await probe.on_process_frame(FrameProcessed(processor=stt, frame=stop,
                                                    direction=UP, timestamp=0))
        lines = [x for x in sink if "VAD-STOP skew" in x]
        assert len(lines) == 1 and "+40ms" in lines[0], sink
    finally:
        logger.remove(handle)


def main():
    stt_mod.SMARTTURN_STOP_SECS = CEILING
    stt_mod._STRANDED_INTERIM_SECS = QUIET
    stt_mod._HINT_MARGIN_MS = MARGIN
    stt_mod.STT_SILENCE_HINT = True
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
