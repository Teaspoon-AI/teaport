#
# Regression test: a reply's TTSStoppedFrame ends "bot speaking" when its audio ends.
#
# pipecat 1.7.0's output transport ends bot-speaking on the TTSStoppedFrame it finds
# queued behind the last audio chunk -- or, when no such frame ever comes, on its
# BOT_VAD_STOP_FALLBACK_SECS (3 s) idle timeout. EngineTTSService never asked for stop
# frames (push_stop_frames defaults to False), so on the appliance every reply ended
# 3.0 s after its audio (journal 2026-09-04: `Bot stopped speaking` 150x, `based on
# TTSStoppedFrame` 0x, TRACE TTSStoppedFrame 0x; BotStoppedSpeaking = audio end +
# 2.9..3.0 s on 8/8 replies measured). pipecat's assistant aggregator waits for
# BotStoppedSpeaking before running the post-tool-call completion, so every tool
# answer carried 3 s of dead air after its filler -- long enough for the user to ask
# again (S12 rerun) -- and the 2-word barge-in guard and the follow-up gate read the
# same 3 s as "still talking".
#
# Real EngineTTSService (synthesis stubbed: no engine), real pipecat TTS base, real
# BaseOutputTransport (the socket write stubbed: no pacing, no network). Hermetic.
# Two runs of the same pipeline: with the stop frame (the fix) bot-speaking ends
# right behind the last chunk; with it switched off (pipecat's default, the old
# behaviour) the transport is shown to sit in its 3 s fallback -- so the assertion is
# on the mechanism, not on a timing that a faster box could satisfy by accident.
# A probe between the TTS and the transport sees exactly what the transcript ledger
# sees (it reads at the TTS's output) and pins the order the ledger relies on: the
# reply's audio, then its stop frame naming the same context, and the reply's End
# still re-pushed to mark the drain.
#
# Run: python test_tts_stop_frame.py   (or via the suite)
#

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the PACKAGE (not just a submodule) before pipecat: teaport_brain/__init__.py
# sets HF_HUB_OFFLINE, and that only guards imports that come after it runs.
import teaport_brain  # noqa: E402, F401
from pinned_pipecat import require_pinned  # noqa: E402

import numpy as np  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.transports.base_output import (  # noqa: E402
    BOT_VAD_STOP_FALLBACK_SECS,
    BaseOutputTransport,
)
from pipecat.transports.base_transport import TransportParams  # noqa: E402

from teaport_brain.engine_tts import EngineTTSService, _SAMPLE_RATE  # noqa: E402

REPLY = "Lima's population is about twelve million people."
AUDIO_SECS = 0.6            # the stubbed clip; short, so the run is quick
DEADLINE_SECS = BOT_VAD_STOP_FALLBACK_SECS + 3.0   # room for the fallback AND slack


async def _fake_segments(text: str):
    """What _synth_text returns for one clause: a tone (not silence, so the seam trim
    keeps it whole) and per-word start times spread over the clip."""
    n = int(AUDIO_SECS * _SAMPLE_RATE)
    t = np.arange(n, dtype=np.float32) / _SAMPLE_RATE
    audio = (0.3 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    words = text.split()
    step = AUDIO_SECS / max(1, len(words))
    return [(audio, [(w, i * step) for i, w in enumerate(words)])]


class StubOutput(BaseOutputTransport):
    """pipecat's real output transport minus the socket: every audio write succeeds
    at once (no pacing), and its wall time is kept."""

    def __init__(self):
        super().__init__(TransportParams(audio_out_enabled=True,
                                         audio_out_sample_rate=_SAMPLE_RATE))
        self.writes: list[float] = []

    async def start(self, frame):
        # A concrete transport reports ready once its client is connected; that is
        # what creates the media senders (the audio queue, bot-speaking tracking).
        # Nothing to connect here, so ready at once.
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        self.writes.append(time.monotonic())
        return True


class Probe(FrameProcessor):
    """(wall time, frame) for everything that passes, in arrival order."""

    def __init__(self):
        super().__init__()
        self.seen: list[tuple[float, Frame]] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self.seen.append((time.monotonic(), frame))
        await self.push_frame(frame, direction)

    def first(self, cls):
        for ts, f in self.seen:
            if isinstance(f, cls):
                return ts, f
        return None, None

    def index(self, cls, last=False):
        idx = [i for i, (_, f) in enumerate(self.seen) if isinstance(f, cls)]
        return (idx[-1] if last else idx[0]) if idx else None


async def _speak_one_reply(push_stop_frames: bool):
    """One reply through TTS -> ledger-side probe -> transport -> downstream probe."""
    tts = EngineTTSService(voice="af_heart")
    tts._synth_text = _fake_segments        # no engine in this test
    # The fix is the constructor's push_stop_frames=True; the control run flips the
    # flag pipecat reads at runtime back to its default to show what it costs.
    tts._push_stop_frames = push_stop_frames
    ledger_side = Probe()
    out = StubOutput()
    downstream = Probe()
    task = PipelineTask(Pipeline([tts, ledger_side, out, downstream]), observers=[])
    runner = PipelineRunner(handle_sigint=False)
    running = asyncio.create_task(runner.run(task))
    await asyncio.sleep(0.5)  # StartFrame propagation
    await task.queue_frames([LLMFullResponseStartFrame(), LLMTextFrame(REPLY),
                             LLMFullResponseEndFrame()])
    # Wait for the transport's verdict AND the reply's End re-push (the transport
    # releases that one on its clock, at the last word's pts, so it can trail the
    # verdict when the writes take no time at all).
    deadline = time.monotonic() + DEADLINE_SECS
    while time.monotonic() < deadline and (
            downstream.first(BotStoppedSpeakingFrame)[0] is None
            or ledger_side.first(LLMFullResponseEndFrame)[0] is None):
        await asyncio.sleep(0.05)
    await task.cancel()
    await running
    return out, ledger_side, downstream


async def test_stop_frame_ends_bot_speaking_at_audio_end():
    out, ledger_side, downstream = await _speak_one_reply(push_stop_frames=True)
    assert out.writes, "the stubbed clip never reached the transport"
    t_started, _ = downstream.first(BotStartedSpeakingFrame)
    assert t_started is not None, "BotStartedSpeaking never came: no audio played"

    # What the ledger sees: the reply's audio, then a stop frame for the SAME context.
    i_audio = ledger_side.index(TTSAudioRawFrame, last=True)
    i_stop = ledger_side.index(TTSStoppedFrame)
    assert i_audio is not None, "no TTSAudioRawFrame at the TTS's output"
    assert i_stop is not None, (
        "no TTSStoppedFrame at the TTS's output: push_stop_frames is not set on "
        "EngineTTSService, so the transport can only end bot-speaking on its "
        f"{BOT_VAD_STOP_FALLBACK_SECS}s idle fallback")
    assert i_stop > i_audio, "the stop frame must follow the reply's last chunk"
    audio = ledger_side.seen[i_audio][1]
    stop = ledger_side.seen[i_stop][1]
    assert stop.context_id == audio.context_id, (
        f"stop frame names context {stop.context_id!r}, audio is {audio.context_id!r}")
    # The ledger's drain signal (the reply's End re-pushed after its words) survives.
    _, end = ledger_side.first(LLMFullResponseEndFrame)
    assert end is not None, "the reply's LLMFullResponseEndFrame is no longer re-pushed"

    # What the transport did with it: bot-speaking ended right behind the last chunk.
    t_stopped, _ = downstream.first(BotStoppedSpeakingFrame)
    assert t_stopped is not None, (
        f"BotStoppedSpeaking never came within {DEADLINE_SECS:.0f}s")
    gap = t_stopped - out.writes[-1]
    assert gap < 1.0, (
        f"bot-speaking ended {gap:.2f}s after the last chunk was written: that is the "
        f"transport's {BOT_VAD_STOP_FALLBACK_SECS}s idle fallback, not the stop frame")
    print(f"  PASS stop frame ends bot-speaking {gap * 1000:.0f} ms after the last chunk")


async def test_without_stop_frame_transport_waits_out_the_fallback():
    out, ledger_side, downstream = await _speak_one_reply(push_stop_frames=False)
    assert out.writes, "the stubbed clip never reached the transport"
    assert ledger_side.index(TTSStoppedFrame) is None, (
        "control run: pipecat pushed a stop frame with the flag off?")
    t_stopped, _ = downstream.first(BotStoppedSpeakingFrame)
    assert t_stopped is not None, (
        f"control run: BotStoppedSpeaking never came within {DEADLINE_SECS:.0f}s")
    gap = t_stopped - out.writes[-1]
    assert gap >= BOT_VAD_STOP_FALLBACK_SECS - 0.5, (
        f"control run: bot-speaking ended {gap:.2f}s after the last chunk without a "
        f"stop frame -- pipecat's fallback is no longer {BOT_VAD_STOP_FALLBACK_SECS}s; "
        "re-check what the fix buys")
    print(f"  PASS without the stop frame the transport idles {gap:.2f}s first "
          f"(fallback {BOT_VAD_STOP_FALLBACK_SECS}s)")


async def test_a_reply_queued_behind_audio_is_scheduled_from_that_audios_end():
    """pipecat anchors a new context's word schedule at max(now, previous last-word
    pts) -- the previous reply's last word START, minus the caption lead. A reply
    queued behind another's audio actually plays when that audio ENDS, so its
    captions ran 0.8-1.3 s ahead of its voice and the heard ledger over-credited a
    word (live 2026-09-04, issue #19). EngineTTSService raises the baseline to the
    previous context's audio end; after a barge-in nothing is carried."""
    tts = EngineTTSService(voice="af_heart")
    tts._synth_text = _fake_segments
    ledger_side = Probe()
    out = StubOutput()
    task = PipelineTask(Pipeline([tts, ledger_side, out]), observers=[])
    runner = PipelineRunner(handle_sigint=False)
    running = asyncio.create_task(runner.run(task))
    await asyncio.sleep(0.5)

    async def reply(text):
        await task.queue_frames([LLMFullResponseStartFrame(), LLMTextFrame(text),
                                 LLMFullResponseEndFrame()])

    def first_pts(ctx_index):
        seen = {}
        for _, f in ledger_side.seen:
            if isinstance(f, TTSTextFrame) and f.context_id is not None:
                seen.setdefault(f.context_id, f.pts)
        return list(seen.values())[ctx_index] if len(seen) > ctx_index else None

    async def wait_for(ctx_index):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and first_pts(ctx_index) is None:
            await asyncio.sleep(0.05)
        return first_pts(ctx_index)

    try:
        await reply("First reply here.")
        await asyncio.sleep(0.15)                 # queued while the first is "playing"
        await reply("Second reply here.")
        p2 = await wait_for(1)
        p1 = first_pts(0)
        assert p1 is not None and p2 is not None, (
            f"both replies must schedule words (got {p1}, {p2}; "
            f"frames seen: {[type(f).__name__ for _, f in ledger_side.seen][-12:]})")
        clip_ns = AUDIO_SECS * 1e9
        assert p2 >= p1 + clip_ns - 0.05e9, (
            f"second reply scheduled {(p2 - p1) / 1e9:.2f}s after the first's start; its "
            f"audio only starts when the first's {AUDIO_SECS}s clip ends")
        # A barge-in flushes the queue: the next reply is scheduled from now, not from
        # where the flushed audio would have ended.
        await task.queue_frames([InterruptionFrame()])
        await asyncio.sleep(0.1)
        await reply("Third reply here.")
        p3 = await wait_for(2)
        assert p3 is not None, (
            f"the reply after the barge-in never scheduled words; frames seen: "
            f"{[type(f).__name__ for _, f in ledger_side.seen][-12:]}")
        assert p3 < p2 + clip_ns, (
            f"third reply scheduled {(p3 - p2) / 1e9:.2f}s after the second's start -- "
            "chained behind audio a barge-in had already flushed")
    finally:
        await task.cancel()
        await running
    print(f"  PASS queued reply scheduled +{(p2 - p1) / 1e9:.2f}s (clip {AUDIO_SECS}s); "
          f"after a barge-in +{(p3 - p2) / 1e9:.2f}s")


def test_tts_stop_frame():
    require_pinned()

    async def main():
        await test_stop_frame_ends_bot_speaking_at_audio_end()
        await test_without_stop_frame_transport_waits_out_the_fallback()
        await test_a_reply_queued_behind_audio_is_scheduled_from_that_audios_end()
    asyncio.run(main())


if __name__ == "__main__":
    test_tts_stop_frame()
    print("ALL PASS")
