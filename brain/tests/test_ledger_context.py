#
# Unit test: a bot turn folds in ONLY its own TTS context — a filler that plays
# inside a reply is not counted as part of that reply.
#
# The ledger keys a bot turn on BotStarted/BotStopped, and "stopped" means 0.35s of
# silence (pipecat's BOT_VAD_STOP_SECS). Anything the bot says back-to-back with no
# gap is one playout. The consult narrator ("Still working on it.") fires on a fixed
# countdown with no regard for whether the bot is mid-reply, so it can begin its own
# TTS context 2s into a reply and play straight after it — no gap, no BotStopped
# between them. Before this fix the narrator's words and samples were folded into the
# reply's utterance.
#
# Confirmed live: brain/formal (LEDGER_TRACE, 2026-08-29 23:21:42). A 29-word reply's
# utterance closed with spoken=32 words and audio_dur=10.88s, both including the
# "Still working on it." filler. Had the user barged in during the filler, the
# filler's words would have been written into the assistant's context message by
# HeardContextCorrector — the bot then "remembering" it said something it never
# meant as part of that reply.
#
# The fix: a turn adopts the context_id of the frame that opens it and rejects text
# and audio that name a different context. Frames with NO context_id are still
# accepted (sherpa's single whole-reply frame, the transport's resampled copy).
#
# Run: python test_ledger_context.py   (or via pytest test_suite.py)
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
    TTSTextFrame,
)
from pipecat.utils.text.base_text_aggregator import AggregationType  # noqa: E402

from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

REPLY = "I have queued the request; anything else you'd like?"  # 9 words
FILLER = "Still working on it."  # 4 words — must NOT land in the reply's utterance
SR = 24000


def audio(secs, ctx):
    return TTSAudioRawFrame(b"\x00\x00" * int(secs * SR), SR, 1, context_id=ctx)


def word(s, pts_s, ctx):
    f = TTSTextFrame(s, aggregated_by=AggregationType.WORD)
    f.pts = int(pts_s * 1e9)
    f.context_id = ctx
    return f


async def feed(seq):
    L = TranscriptLedger()
    for f, t in seq:
        await L.on_process_frame(SimpleNamespace(frame=f, timestamp=int(t * 1e9)))
    return L


def _bot_utterances(L):
    return [e for e in L.events if e.speaker == "assistant"]


# The live shape: reply context "R" opens and plays; filler context "F" begins
# mid-reply and plays straight after, no BotStopped between; then the reply's playout
# ends. One utterance, and it must be the reply alone.
def _chained(cut=None):
    seq = [
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(REPLY), 0.1),
        (LLMFullResponseEndFrame(), 0.2),
        (TTSStartedFrame(context_id="R"), 1.0),
        (audio(3.0, "R"), 1.0),
        (BotStartedSpeakingFrame(), 1.1),
        (word("I ", 1.2, "R"), 1.05), (word("have ", 1.4, "R"), 1.05),
        (word("queued ", 1.7, "R"), 1.05), (word("the ", 2.0, "R"), 1.05),
        (word("request; ", 2.3, "R"), 1.05), (word("anything ", 2.6, "R"), 1.05),
        (word("else ", 2.9, "R"), 1.05), (word("you'd ", 3.2, "R"), 1.05),
        (word("like? ", 3.5, "R"), 1.05),
        # filler starts its OWN context mid-reply, plays right after the reply's audio
        (TTSStartedFrame(context_id="F"), 4.0),
        (audio(1.5, "F"), 4.0),
        (word("Still ", 4.2, "F"), 4.05), (word("working ", 4.5, "F"), 4.05),
        (word("on ", 4.8, "F"), 4.05), (word("it. ", 5.0, "F"), 4.05),
    ]
    if cut is None:
        seq.append((BotStoppedSpeakingFrame(), 6.0))
    else:
        seq.append((InterruptionFrame(), cut))
    return seq


async def test_a_full_playout_records_only_the_reply():
    L = await feed(_chained())
    utts = _bot_utterances(L)
    assert len(utts) == 1, f"{len(utts)} assistant utterances; the filler was charted separately"
    u = utts[0]
    assert u.text == REPLY, u.text
    assert "working on it" not in (u.heard_text or "").lower(), (
        f"the filler's words landed in the reply's heard text: {u.heard_text!r}")
    assert u.heard_text.split()[:2] == ["I", "have"], u.heard_text


async def test_a_fillers_audio_does_not_inflate_the_denominator():
    """The heard_fraction bug the exclusion prevents: a short reply, FULLY spoken,
    then a longer filler in its own context, and a barge-in during the filler. If the
    filler's samples are folded into the reply's audio bucket, the reply's full_dur
    becomes reply+filler and a reply the user heard in full is reported as cut. Same
    class as the 2026-08-25 resampled-copy bug — one context over instead of one rate
    over. The reply text is short so its word-count duration floor does not mask the
    effect (that floor, not the filler, is what a long reply would trip)."""
    short = "Okay, all done."  # 3 words -> text_dur ~1.08s, under the 1.5s of audio
    L = await feed([
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(short), 0.1),
        (LLMFullResponseEndFrame(), 0.2),
        (TTSStartedFrame(context_id="R"), 1.0),
        (audio(1.5, "R"), 1.0),                 # reply: 1.5s, plays 1.1-2.6
        (BotStartedSpeakingFrame(), 1.1),
        (word("Okay, ", 1.2, "R"), 1.05), (word("all ", 1.5, "R"), 1.05),
        (word("done. ", 1.9, "R"), 1.05),
        (TTSStartedFrame(context_id="F"), 2.6),
        (audio(3.0, "F"), 2.6),                 # filler: 3.0s, foreign context
        (word("Still ", 2.8, "F"), 2.65),
        (InterruptionFrame(), 3.5),             # cut AFTER the reply finished playing
    ])
    u = _bot_utterances(L)[0]
    # Reply audio is 1.5s and it finished at ~2.6s, well before the 3.5s cut -> fully
    # heard. Folding in the filler's 3.0s makes full_dur ~4.5s and frac ~0.53.
    assert u.text == short
    assert u.heard_fraction >= 0.99, (
        f"heard_fraction {u.heard_fraction:.2f} — the filler's audio inflated the "
        f"reply's denominator and a fully-heard reply reads as cut")
    assert not u.cut_short


async def test_a_barge_in_during_the_filler_does_not_attribute_it_to_the_reply():
    # The user speaks while the filler is playing (t=4.6, after 2 filler words).
    # The reply was fully spoken; the filler is foreign. The reply's heard text must
    # be the full reply and must not gain the filler's words.
    L = await feed(_chained(cut=4.6))
    u = _bot_utterances(L)[0]
    assert u.text == REPLY, u.text
    assert "working" not in (u.heard_text or "").lower() and "still" not in (u.heard_text or "").lower(), (
        f"barge-in during the filler wrote the filler into the reply: {u.heard_text!r}")


async def test_no_context_ids_behaves_as_before():
    # sherpa / legacy: no context_id anywhere. Nothing is rejected; the single
    # whole-reply word frame path is unchanged.
    L = await feed([
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(REPLY), 0.1),
        (LLMFullResponseEndFrame(), 0.2),
        (TTSStartedFrame(), 1.0),
        (TTSAudioRawFrame(b"\x00\x00" * int(3.0 * SR), SR, 1), 1.0),
        (BotStartedSpeakingFrame(), 1.1),
        (BotStoppedSpeakingFrame(), 4.2),
    ])
    u = _bot_utterances(L)
    assert len(u) == 1 and u[0].text == REPLY and u[0].heard_fraction >= 0.99


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
