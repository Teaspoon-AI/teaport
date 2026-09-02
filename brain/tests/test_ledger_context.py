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
# The fix, in three parts. Fillers are MARKED: tools.py pushes them with
# append_to_context=False, tts_service stamps that onto their TTSStartedFrame, and
# the ledger charts nothing for a marked context — it can neither open a turn nor
# be folded into one. A DIFFERENT unmarked context starting mid-turn is the next
# genuine reply chained with no gap: the open turn is charted and a new one opened
# (rejecting it made the whole second reply vanish). And audio is stricter than
# words: in a ctx-tagged turn only audio NAMING that context counts, because the
# output transport rebuilds audio without its context_id, so an untagged copy may
# be a foreign filler's playout just as well as the reply's own (word frames with
# no ctx are still accepted — sherpa's single whole-reply frame).
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
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.utils.text.base_text_aggregator import AggregationType  # noqa: E402

from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

REPLY = "I have queued the request; anything else you'd like?"  # 9 words
FILLER = "Still working on it."  # 4 words — must NOT land in the reply's utterance
SR = 24000


def audio(secs, ctx):
    return TTSAudioRawFrame(b"\x00\x00" * int(secs * SR), SR, 1, context_id=ctx)


def filler_started(ctx):
    # The live filler shape: tools.py pushes the narrator/tool-ack TTSSpeakFrames
    # with append_to_context=False and tts_service stamps that onto the context's
    # TTSStartedFrame (the ledger's filler marker).
    return TTSStartedFrame(context_id=ctx, append_to_context=False)


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
        (filler_started("F"), 4.0),
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
        (filler_started("F"), 2.6),
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


# --- the marked-filler / chained-context / strict-audio contract ---

async def test_b_filler_opening_first_does_not_steal_the_reply():
    """Adoption inversion: the narrator fires in the LLM's silence BEFORE the
    reply's own TTS context starts, mid-generation. The filler is marked
    (append_to_context=False), so it must not open the turn — the reply's context
    must, and a barge-in mid-reply must read as a genuine cut. Unmarked, the
    filler's context owned the turn, every reply frame was rejected as foreign,
    and the same barge-in reported heard_fraction 1.00 with the FULL reply as
    heard — the exact bug class the ledger exists to prevent, mirrored."""
    # The transport plays in push order: the filler's 1.5s (pushed at 0.3) fills
    # 0.9-2.4 of the window that opens at 0.9, and the reply's audio (pushed at 0.8,
    # queued behind it) plays 2.4-5.4 — so its words are scheduled from 2.5 and the
    # mid-reply barge-in is at 3.9. (This used to schedule the words from 1.0 and
    # barge at 2.4, and asserted a heard fraction that counted the FILLER's seconds
    # as the reply's — the credit the ledger's audio_start now refuses to give.)
    L = await feed([
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(REPLY), 0.1),
        (filler_started("F"), 0.3),           # narrator, while the LLM streams
        (audio(1.5, "F"), 0.3),
        (word("Still ", 0.4, "F"), 0.35), (word("working ", 0.6, "F"), 0.35),
        (TTSStartedFrame(context_id="R"), 0.8),
        (audio(3.0, "R"), 0.8),
        (BotStartedSpeakingFrame(), 0.9),     # the FILLER's playout begins
        (word("I ", 2.5, "R"), 0.85), (word("have ", 2.7, "R"), 0.85),
        (word("queued ", 3.0, "R"), 0.85), (word("the ", 3.3, "R"), 0.85),
        (word("request; ", 3.6, "R"), 0.85),
        (LLMFullResponseEndFrame(), 1.0),
        (InterruptionFrame(), 3.9),           # barge-in ~half way into the reply
    ])
    u = _bot_utterances(L)[0]
    assert u.text == REPLY, u.text
    assert u.interrupted and u.cut_short, (
        f"a mid-reply barge-in read as fully heard (frac={u.heard_fraction:.2f}) — "
        f"the filler's context stole the turn")
    assert 0.2 < u.heard_fraction < 0.8, u.heard_fraction
    assert "still" not in (u.heard_text or "").lower(), u.heard_text
    assert u.heard_text.split()[:2] == ["I", "have"], u.heard_text


async def test_b_untagged_transport_copies_do_not_inflate_the_denominator():
    """The LIVE frame shape: the output transport re-pushes every audio chunk
    REBUILT WITHOUT context_id. The filler's tagged copy is rejected, but its
    untagged transport copy used to land in the reply's audio bucket — same
    denominator inflation the ctx filter claims to fix, through the side door.
    A ctx-tagged turn must count only audio naming its context."""
    short = "Okay, all done."  # 3 words -> text_dur ~1.08s, under the 1.5s of audio
    L = await feed([
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(short), 0.1),
        (LLMFullResponseEndFrame(), 0.2),
        (TTSStartedFrame(context_id="R"), 1.0),
        (audio(1.5, "R"), 1.0),                 # TTS service's tagged copy
        (BotStartedSpeakingFrame(), 1.1),
        (audio(1.5, None), 1.15),               # transport's untagged rebuild of it
        (word("Okay, ", 1.2, "R"), 1.05), (word("all ", 1.5, "R"), 1.05),
        (word("done. ", 1.9, "R"), 1.05),
        (filler_started("F"), 2.6),
        (audio(3.0, "F"), 2.6),                 # filler: tagged copy, rejected
        (audio(3.0, None), 2.7),                # filler: untagged transport copy
        (InterruptionFrame(), 3.5),             # cut AFTER the reply finished playing
    ])
    u = _bot_utterances(L)[0]
    assert u.text == short
    assert u.heard_fraction >= 0.99, (
        f"heard_fraction {u.heard_fraction:.2f} — an untagged transport copy "
        f"inflated the reply's denominator")
    assert not u.cut_short


async def test_b_foreign_stop_frame_does_not_collapse_the_denominator():
    """TTSStoppedFrame carries a context_id too: a foreign context's stop (the
    filler's) says nothing about the reply, but used to set synth_done — full_dur
    then collapsed to the samples synthesized so far and a barge-in a third of the
    way in read as fully heard, so the unheard tail was never reconciled."""
    L = await feed([
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(REPLY), 0.1),             # 9 words -> text_dur ~3.24s
        (LLMFullResponseEndFrame(), 0.2),
        (TTSStartedFrame(context_id="R"), 1.0),
        (audio(1.0, "R"), 1.0),                 # only 1.0s synthesized so far
        (BotStartedSpeakingFrame(), 1.1),
        (word("I ", 1.2, "R"), 1.05), (word("have ", 1.5, "R"), 1.05),
        (word("queued ", 1.8, "R"), 1.05),
        (filler_started("F"), 1.9),
        (TTSStoppedFrame(context_id="F"), 2.0),  # the filler's stop, mid-reply
        (InterruptionFrame(), 2.1),
    ])
    u = _bot_utterances(L)[0]
    assert u.interrupted and u.cut_short, (
        f"a barge-in a third of the way in read as heard "
        f"(frac={u.heard_fraction:.2f}) — a foreign stop frame collapsed full_dur")
    assert u.heard_fraction < 0.5, u.heard_fraction


ACK = "Sure, let me check that."  # 5 words
ANSWER = "The bakery opens at eight and closes at six."  # 9 words


async def test_b_chained_second_reply_is_charted_and_cut_properly():
    """Fast tool flow: a short spoken ack (context R1) chains straight into the
    answer (context R2) with no BotStopped between — and the answer's LLM turn
    ends while the ack's turn is still open. Rejecting R2's frames as foreign made
    the ENTIRE second reply vanish from the ledger (and closing R1 used to destroy
    the answer's pending text with it): a barge-in mid-answer then had no cut
    event, so the context kept the full answer as said-and-heard."""
    L = await feed([
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(ACK), 0.05),
        (LLMFullResponseEndFrame(), 0.1),
        (TTSStartedFrame(context_id="R1"), 0.5),
        (audio(1.5, "R1"), 0.5),
        (BotStartedSpeakingFrame(), 0.6),
        (word("Sure, ", 0.7, "R1"), 0.55), (word("let ", 0.9, "R1"), 0.55),
        (word("me ", 1.1, "R1"), 0.55), (word("check ", 1.3, "R1"), 0.55),
        (word("that. ", 1.5, "R1"), 0.55),
        (LLMFullResponseStartFrame(), 1.0),      # the answer's completion…
        (LLMTextFrame(ANSWER), 1.1),
        (LLMFullResponseEndFrame(), 1.2),        # …ends while R1 is still open
        (TTSStartedFrame(context_id="R2"), 2.0),  # chains: no BotStopped between
        (audio(3.24, "R2"), 2.0),
        (word("The ", 2.2, "R2"), 2.05), (word("bakery ", 2.5, "R2"), 2.05),
        (word("opens ", 2.8, "R2"), 2.05), (word("at ", 3.1, "R2"), 2.05),
        (word("eight ", 3.3, "R2"), 2.05), (word("and ", 3.7, "R2"), 2.05),
        (word("closes ", 4.0, "R2"), 2.05), (word("at ", 4.3, "R2"), 2.05),
        (word("six. ", 4.6, "R2"), 2.05),
        (InterruptionFrame(), 3.6),              # barge-in mid-answer
    ])
    utts = _bot_utterances(L)
    assert len(utts) == 2, (
        f"{len(utts)} assistant utterances — the chained second reply vanished")
    ack, ans = utts
    assert ack.text == ACK and not ack.cut_short, (ack.text, ack.heard_fraction)
    assert ans.text == ANSWER, ans.text
    assert ans.interrupted and ans.cut_short, (
        f"the cut answer read as heard (frac={ans.heard_fraction:.2f})")
    assert 0.2 < ans.heard_fraction < 0.8, ans.heard_fraction
    assert ans.heard_text.split()[:2] == ["The", "bakery"], ans.heard_text
    assert "sure" not in (ans.heard_text or "").lower(), ans.heard_text


async def test_b_phantom_turn_does_not_swallow_the_next_reply():
    """Pre-existing (reproduced on main): a standalone unmarked context — the
    greeting shape, or an unmarked filler — opens a turn with empty intended in
    the silence before a reply. Closing it used to consume _pending_gen BEFORE
    the empty-intended bail, so the genuine reply that followed was never
    recorded at all."""
    L = await feed([
        (TTSStartedFrame(context_id="G"), 0.5),   # unmarked standalone context
        (audio(1.0, "G"), 0.5),
        (BotStartedSpeakingFrame(), 0.6),
        (LLMFullResponseStartFrame(), 1.0),
        (LLMTextFrame(REPLY), 1.1),
        (LLMFullResponseEndFrame(), 1.2),          # pending text set…
        (TTSStartedFrame(context_id="R"), 1.6),    # …must survive G's close
        (audio(3.0, "R"), 1.6),
        (word("I ", 1.8, "R"), 1.65), (word("have ", 2.0, "R"), 1.65),
        (BotStoppedSpeakingFrame(), 5.0),
    ])
    utts = _bot_utterances(L)
    assert len(utts) == 1, (
        f"{len(utts)} assistant utterances — the phantom turn swallowed the reply")
    assert utts[0].text == REPLY, utts[0].text
    assert utts[0].heard_fraction >= 0.99


async def test_b_filler_only_playout_never_becomes_a_turn():
    """The phantom's side door: a reply's text is pending (its TTS delayed) when
    the narrator plays a line ALONE in the gap. BotStarted is anonymous, so that
    playout window used to open a bot turn that claimed the pending text, was
    charted 'fully heard' off the filler's playout, and consumed the pending —
    the delayed reply then vanished."""
    L = await feed([
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(REPLY), 0.1),
        (LLMFullResponseEndFrame(), 0.2),        # pending set; the TTS is slow
        (filler_started("F"), 0.5),
        (audio(1.5, "F"), 0.5),
        (BotStartedSpeakingFrame(), 0.6),        # the FILLER's playout window
        (word("Still ", 0.7, "F"), 0.65),
        (BotStoppedSpeakingFrame(), 2.2),
        (TTSStartedFrame(context_id="R"), 2.5),  # the delayed reply, own window
        (audio(3.0, "R"), 2.5),
        (BotStartedSpeakingFrame(), 2.6),
        (word("I ", 2.7, "R"), 2.55),
        (BotStoppedSpeakingFrame(), 5.7),
    ])
    utts = _bot_utterances(L)
    assert len(utts) == 1, (
        [u.text for u in utts],
        "the filler's playout window became a bot turn")
    assert utts[0].text == REPLY, utts[0].text
    assert utts[0].heard_fraction >= 0.99
    assert utts[0].t_start >= 2.5, (
        f"the reply's turn starts at {utts[0].t_start:.1f} — inside the filler's window")


async def test_b_reply_window_does_not_span_the_filler():
    """Turn CLOSE is ctx-aware too: the chain-end BotStopped arrives after the
    excluded filler played out, but the reply's utterance must end when ITS audio
    did. Stamping the close time stretched the window across the filler, and a
    user remark during the filler was marked OVERLAP against a reply it never
    touched."""
    L = await feed([
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(REPLY), 0.1),
        (LLMFullResponseEndFrame(), 0.2),
        (TTSStartedFrame(context_id="R"), 1.0),
        (audio(3.0, "R"), 1.0),                   # plays 1.1-4.1
        (BotStartedSpeakingFrame(), 1.1),
        (word("I ", 1.2, "R"), 1.05), (word("have ", 1.8, "R"), 1.05),
        (word("queued ", 2.6, "R"), 1.05), (word("the ", 3.4, "R"), 1.05),
        (filler_started("F"), 4.0),
        (audio(1.5, "F"), 4.0),                   # filler plays 4.1-5.6
        (VADUserStartedSpeakingFrame(), 4.5),     # user speaks during the filler
        (TranscriptionFrame("thanks a lot", "u", "t", None), 5.5),
        (BotStoppedSpeakingFrame(), 5.6),         # chain end, after the filler
    ])
    reply = _bot_utterances(L)[0]
    users = [e for e in L.events if e.speaker == "user"]
    assert users and users[0].text == "thanks a lot"
    assert reply.t_end <= 4.2, (
        f"reply window ends at {reply.t_end:.1f} — it spans the filler's playout")
    assert not reply.overlap and not users[0].overlap, (
        "a remark during the filler was marked as overlapping the reply")


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
