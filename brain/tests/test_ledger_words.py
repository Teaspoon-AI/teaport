#
# Unit test: the ledger uses per-word TTSTextFrames (engine TTS) for an EXACT heard
# boundary, overriding the played-fraction estimate — and falls back to the
# estimate when no word frames are present (sherpa).
#
# Run: python test_ledger_words.py
#

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the PACKAGE (not just a submodule) before pipecat: teaport_brain/__init__.py
# sets HF_HUB_OFFLINE, and that only guards imports that come after it runs.
import teaport_brain  # noqa: E402, F401

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.utils.text.base_text_aggregator import AggregationType  # noqa: E402

from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

INTENDED = "Hello world, how are you today, my friend?"  # 8 words


def audio(secs, sr=24000):
    return TTSAudioRawFrame(b"\x00\x00" * int(secs * sr), sr, 1)


def W(s):  # a per-word TTSTextFrame, as the base class schedules for the per-word TTS path
    return TTSTextFrame(s, aggregated_by=AggregationType.WORD)


def Wp(s, pts_s):  # word frame carrying its scheduled playout time (pts)
    f = TTSTextFrame(s, aggregated_by=AggregationType.WORD)
    f.pts = int(pts_s * 1e9)
    return f


async def test_pts_filter_keeps_played_words():
    # The real failure mode: frames ARRIVE clustered (all before the barge-in)
    # but carry their playout pts. Only words with pts <= cut time were heard.
    e = await feed(llm() + [
        (TTSStartedFrame(), 1.0), (audio(3.0), 1.0), (BotStartedSpeakingFrame(), 1.0),
        (Wp("Hello ", 1.2), 1.05), (Wp("world, ", 1.5), 1.05),     # arrive at 1.05…
        (Wp("how ", 2.0), 1.05), (Wp("are ", 2.4), 1.05),          # …all clustered
        (InterruptionFrame(), 1.7),  # cut@1.7s -> pts<=1.7: Hello(1.2), world(1.5)
    ])
    assert e.heard_text == "Hello world,", repr(e.heard_text)
    assert "how" not in e.heard_text and "are" not in e.heard_text
    print(f"  PASS pts-filter -> {e.heard_text!r} (dropped later words despite clustered arrival)")


async def feed(seq):
    L = TranscriptLedger()
    for f, t in seq:
        await L.on_process_frame(SimpleNamespace(frame=f, timestamp=int(t * 1e9)))
    return [e for e in L.events if e.speaker == "assistant"][-1]


def llm():
    return [(LLMFullResponseStartFrame(), 0.0), (LLMTextFrame(INTENDED), 0.0),
            (LLMFullResponseEndFrame(), 0.0)]


async def test_wordlevel_overrides_estimate():
    # 2 words played; timing would ESTIMATE ~6 words. Exact must win.
    e = await feed(llm() + [
        (TTSStartedFrame(), 1.0), (audio(3.0), 1.0), (BotStartedSpeakingFrame(), 1.1),
        (W("Hello "), 1.4), (W("world, "), 1.7),
        (InterruptionFrame(), 3.5),  # heard ~2.4s of 3s -> estimate would be ~6 words
    ])
    assert e.interrupted
    assert e.heard_text == "Hello world,", repr(e.heard_text)
    assert "how" not in e.heard_text, "must NOT include un-played words from the estimate"
    print(f"  PASS word-level cut -> exact {e.heard_text!r} (frac est. was ~{e.heard_fraction:.2f})")


async def test_fallback_to_estimate_without_word_frames():
    # sherpa: no per-word frames -> played-fraction estimate
    e = await feed(llm() + [
        (TTSStartedFrame(), 1.0), (audio(3.0), 1.0), (BotStartedSpeakingFrame(), 1.1),
        (InterruptionFrame(), 2.5),  # ~1.4s of 3s
    ])
    assert e.interrupted and e.heard_text
    assert INTENDED.startswith(e.heard_text.split(",")[0].split()[0])  # a real prefix
    assert e.heard_text != INTENDED
    print(f"  PASS fallback (no word frames) -> estimate {e.heard_text!r}")


async def test_single_frame_is_not_treated_as_wordlevel():
    # sherpa pushes ONE whole-reply frame; must NOT be used as 'heard' (use estimate)
    e = await feed(llm() + [
        (TTSStartedFrame(), 1.0), (audio(3.0), 1.0), (BotStartedSpeakingFrame(), 1.1),
        (W(INTENDED), 1.2),  # one whole-text frame
        (InterruptionFrame(), 2.0),
    ])
    assert e.heard_text != INTENDED, "the whole-reply frame must not count as fully heard"
    print(f"  PASS single whole-text frame -> still estimated {e.heard_text!r}")


async def test_full_playout_word_level():
    e = await feed(llm() + [
        (TTSStartedFrame(), 1.0), (audio(3.0), 1.0), (TTSStoppedFrame(), 1.0),
        (BotStartedSpeakingFrame(), 1.1),
        (W("Hello world, "), 1.4), (W("how are you "), 2.0),
        (W("today, my friend?"), 3.0),
        (BotStoppedSpeakingFrame(), 4.2),
    ])
    assert not e.interrupted and e.heard_fraction >= 0.99
    assert e.heard_text == "Hello world, how are you today, my friend?", repr(e.heard_text)
    print(f"  PASS full playout -> heard all {e.heard_text!r}")


async def test_wordlevel_survives_frames_without_trailing_spaces():
    # Reality the mocked tests miss: the engine's per-word frames for number/counting
    # output carry NO trailing space ("One,","two,",...). A bare "".join then collapses
    # them to one whitespace-less token, the exact-timing count reads 1, the ">= est"
    # guard rejects it, and the ledger silently falls back to the coarse fraction.
    # Live 2026-09-02: a barge-in during a count was charted heard-up-to-eight while the
    # user heard ~ten. Engineered so the estimate (~4) and the per-word timing (10)
    # DISAGREE, so the assertion proves the exact boundary is used, not the estimate.
    COUNT = ("One, two, three, four, five, six, seven, eight, "
             "nine, ten, eleven, twelve.")
    words = ["One,", "two,", "three,", "four,", "five,", "six,", "seven,",
             "eight,", "nine,", "ten,", "eleven,", "twelve."]
    frames = [(Wp(w, 1.0 + i * 0.3), 1.05) for i, w in enumerate(words)]  # clustered arrival
    e = await feed(
        [(LLMFullResponseStartFrame(), 0.0), (LLMTextFrame(COUNT), 0.0),
         (LLMFullResponseEndFrame(), 0.0),
         (TTSStartedFrame(), 1.0), (audio(10.0), 1.0),   # long clip -> small played-fraction
         (BotStartedSpeakingFrame(), 1.0)]
        + frames
        + [(InterruptionFrame(), 4.0)])  # heard 3.0/10 -> est ~4; cut-lead 3.8 -> pts<=3.8 = 10 words
    assert e.interrupted
    assert e.heard_text == ("One, two, three, four, five, six, seven, eight, "
                            "nine, ten,"), repr(e.heard_text)
    assert len(e.heard_text.split()) == 10, repr(e.heard_text)   # NOT 1 (collapsed), NOT ~4 (est)
    assert "eleven" not in e.heard_text and "twelve" not in e.heard_text
    print(f"  PASS spaceless word frames -> exact {e.heard_text!r} (beat est, not collapsed)")


async def main():
    for fn in [test_pts_filter_keeps_played_words,
               test_wordlevel_overrides_estimate,
               test_wordlevel_survives_frames_without_trailing_spaces,
               test_fallback_to_estimate_without_word_frames,
               test_single_frame_is_not_treated_as_wordlevel,
               test_full_playout_word_level]:
        await fn()
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
