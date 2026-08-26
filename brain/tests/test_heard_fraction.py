#
# Unit test: a resampled copy of the reply must not lengthen it.
#
# The TTS emits 24 kHz and the pipeline runs at 16 kHz, so the output transport resamples
# and the SAME audio reaches the ledger twice. The copies carry different frame ids, so
# the ledger's id-dedup cannot catch them, and bucketing per SAMPLE RATE does not catch
# them either: the resampled frames still report sample_rate=24000 while num_frames counts
# 16 kHz samples. Both then land in one bucket and inflate the reply's duration by
# (24000 + 16000) / 24000 = 1.67x, which deflates the played fraction by the same factor,
# so every barge-in under-credits what the user actually heard by about 40%.
#
# What makes the accounting sound is that every processor sees the whole reply exactly
# once. So the reply's duration is the LONGEST any single processor saw — never the sum,
# and independent of what rate any one of them labels its frames with.
#
# Live 2026-08-25: asked to count to twenty and interrupted 6.15s into a 9.5s clip, the
# ledger measured audio_dur=15.8, frac=0.39, and credited "eight" — where the user had
# heard past twelve. HeardContextCorrector then truncated the reply to that in the
# context, so the agent insisted it had stopped at eight and argued the point.
#
# Run: on the appliance only — see pinned_pipecat.py.
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
)
from pipecat.observers.base_observer import FrameProcessed  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

CLIP_SECS = 9.5
TTS_RATE = 24000
PIPELINE_RATE = 16000
COUNT = ("One, two, three, four, five, six, seven, eight, nine, ten, eleven, twelve, "
         "thirteen, fourteen, fifteen, sixteen, seventeen, eighteen, nineteen, twenty.")
HEARD_SECS = 6.15          # the live barge-in: 20:42:50.336 against audio_start 20:42:44.184


class _Source:
    """Stands in for a pushing processor — the ledger keys audio on its name."""

    def __init__(self, name):
        self.name = name


# (pushing processor, rate the frame REPORTS, rate the samples were actually counted at)
TTS_ONLY = ((_Source("EngineTTSService#2"), TTS_RATE, TTS_RATE),)
# The live pipeline: the transport's resampled copy still reports the TTS rate.
MISLABELLED = TTS_ONLY + ((_Source("OutputTransport#1"), TTS_RATE, PIPELINE_RATE),)
# The same duplication, but with the copy honestly labelled.
RELABELLED = TTS_ONLY + ((_Source("OutputTransport#1"), PIPELINE_RATE, PIPELINE_RATE),)


async def _run(sources):
    """Drive the REAL observer: generate a reply, play it, barge in, read the verdict.

    Through on_process_frame rather than poking internals, so this exercises the
    arithmetic the live pipeline uses and fails on the real defect rather than on a
    renamed field.
    """
    ledger = TranscriptLedger()

    async def feed(frame, t, processor=None):
        await ledger.on_process_frame(
            FrameProcessed(processor=processor, frame=frame,
                           direction=FrameDirection.DOWNSTREAM, timestamp=int(t * 1e9)))

    await feed(LLMFullResponseStartFrame(), 0.0)
    await feed(LLMTextFrame(text=COUNT), 0.1)
    await feed(LLMFullResponseEndFrame(), 0.2)
    await feed(TTSStartedFrame(), 0.3)
    await feed(BotStartedSpeakingFrame(), 0.3)
    for processor, reported_rate, counted_at in sources:
        samples = int(CLIP_SECS * counted_at)
        await feed(TTSAudioRawFrame(b"\x00\x00" * samples, reported_rate, 1), 0.4, processor)
    await feed(InterruptionFrame(), 0.3 + HEARD_SECS)
    assistant = [u for u in ledger.events if u.speaker == "assistant"]
    assert assistant, "no assistant utterance was charted"
    return assistant[-1]


async def test_a_resampled_copy_must_not_shorten_what_was_heard():
    """The incident: 6.15s of a 9.5s count is 12-13 of 20 words, not 8."""
    u = await _run(MISLABELLED)
    words = len(u.heard_text.split())
    assert words >= 12, (
        f"credited only {words} of 20 words — the resampled copy inflated the reply's "
        f"duration and deflated the played fraction"
    )


async def test_a_correctly_labelled_copy_is_also_not_summed():
    """Bucketing must not depend on the copy reporting a different rate."""
    assert len((await _run(RELABELLED)).heard_text.split()) >= 12


async def test_a_single_source_is_unchanged():
    """The fix must not change the ordinary case."""
    assert len((await _run(TTS_ONLY)).heard_text.split()) >= 12


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run_aio():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
