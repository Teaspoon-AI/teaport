#
# Regression test: a cut reply must not be charted a second time by a filler's playout,
# and the turn a filler opens must never claim the NEXT reply under the old text.
#
# Found by brain/formal/Ledger.tla (rows ledger_once and ledger_wrongText; the traces are
# in brain/formal/README.md) and reproduced here against the real ledger and corrector.
#
# The shape: a reply is barged over during synthesis. The ledger charts it cut (heard 0),
# correctly. But the ledger of PR #13 reset nothing it held: the cancelled completion's
# finally (pipecat 1.7.0 base_llm.py:571-573) then pushed LLMFullResponseEndFrame with
# the partial text, which re-armed the pending text, and the next filler to play (a tool
# ack, a narrator line) came back from the output transport as an UNTAGGED
# TTSAudioRawFrame (base_output rebuilds audio without its context_id), which it could
# not recognise as a filler's and which opened a turn on the cut reply's text.
#
#   A  If that turn closed on the filler's BotStoppedSpeaking, the cut reply was charted
#      AGAIN, complete, heard 1.0 -- during a window in which only the filler played.
#      Inert (HeardContextCorrector acts on cut turns only), but wrong.
#   B  If the next reply's TTS started while that turn was still open (the answer
#      chaining into the ack's window -- the fast tool flow), its TTSStarted was folded
#      into the open turn instead of opening one, and the answer was charted under the
#      OLD text with no audio_start. Barged, the corrector received heard_text "" and
#      DELETED the answer's committed message: the model had no record it answered.
#   C  The same wrong-text chart on a pre-existing path: the in-flight generation was
#      preferred, so a reply whose TTS started after the NEXT completion had begun
#      streaming was charted under the next completion's text.
#
# The ledger now drops everything it holds at an interruption, opens turns only on the
# TTS's own frames (never on the transport's untagged copies), and claims the oldest
# expected context for each -- see transcript_ledger.py's header.
#
# Cases d-g were added while fixing: the PR's own filler-only shape WITH the untagged
# copy it omits (d), and three more paths brain/formal/Ledger.tla found once the first
# fixes were in -- a filler's BotStopped closing a reply still awaiting its slow first
# chunk (e), a single pending slot losing the older of two completed replies (f), and a
# reply's late End frame re-queueing text a live turn had already spoken (g). Every
# assertion states the behaviour the ledger should have. (The frame shapes are what
# pipecat 1.7.0's TTS service and output transport produce; test_ledger_playout.py
# carries the same ledger through the production sightings -- the TTS's own, and the
# post-drain re-push of a response's End frame -- which these hermetic scripts omit.)
#
# Run: python test_ledger_phantom_cascade.py
#
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402
require_pinned()

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
)

from teaport_brain.heard_context import HeardContextCorrector  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

R1 = "The forecast for tomorrow is sunny with a high of seventy two."
R2 = "It is Tuesday the second."
SR = 24000


def audio(secs, ctx):
    return TTSAudioRawFrame(b"\x00\x00" * int(secs * SR), SR, 1, context_id=ctx)


def filler_started(ctx):
    # The live filler shape (tools.py pushes the ack/narrator TTSSpeakFrames with
    # append_to_context=False; tts_service stamps it onto the context's started frame).
    return TTSStartedFrame(context_id=ctx, append_to_context=False)


async def feed_into(L, seq):
    for f, t in seq:
        await L.on_process_frame(SimpleNamespace(frame=f, timestamp=int(t * 1e9)))
    return L


def _bot(L):
    return [e for e in L.events if e.speaker == "assistant"]


# R1 streams, its TTS starts, the user cuts it during synthesis (a cough: no transcript,
# so no new completion), and the cancelled completion's finally pushes the End frame.
CUT_R1 = [
    (LLMFullResponseStartFrame(), 0.0),
    (LLMTextFrame(R1), 0.1),
    (TTSStartedFrame(context_id="R1"), 0.2),
    (InterruptionFrame(), 0.3),
    (LLMFullResponseEndFrame(), 0.35),
]

# A filler plays: tagged audio from the TTS service, then the transport's untagged copy.
NARRATOR = [
    (filler_started("F"), 5.0),
    (audio(1.5, "F"), 5.0),
    (BotStartedSpeakingFrame(), 5.1),
    (audio(1.5, None), 5.2),
]
NARRATOR_END = [
    (TTSStoppedFrame(context_id="F"), 6.7),
    (BotStoppedSpeakingFrame(), 6.8),
]


async def test_a_cut_reply_is_charted_once_not_again_by_a_filler_window():
    L = await feed_into(TranscriptLedger(), CUT_R1 + NARRATOR + NARRATOR_END)
    charted = [e for e in _bot(L) if e.text == R1]
    assert len(charted) == 1, (
        f"the cut reply was charted {len(charted)} times: "
        + "; ".join(f"t=[{e.t_start:.1f},{e.t_end:.1f}] heard={e.heard_fraction:.2f}" for e in charted)
        + " -- the second is the filler's window, in which none of it played")
    assert charted[0].interrupted and charted[0].heard_fraction == 0.0


async def test_b_next_reply_chaining_into_the_filler_window_keeps_its_own_text():
    """The answer to the next question chains into the ack's window (fast tool flow)
    and is barged. It must be charted as ITSELF, with the playout it had -- and the
    corrector must not remove its message from the context."""
    L = TranscriptLedger()
    msgs = [{"role": "user", "content": "what's the forecast"},
            {"role": "assistant", "content": ""}]          # R1: cut before TTS text committed
    ctx = SimpleNamespace(get_messages=lambda: msgs,
                          set_messages=lambda m: msgs.__setitem__(slice(None), m))
    C = HeardContextCorrector(L, ctx, mode="truncate")

    await feed_into(L, CUT_R1)
    C._reconcile()                                          # R1's cut is settled here

    msgs += [{"role": "user", "content": "what day is it"},
             {"role": "assistant", "content": R2}]          # pipecat commits R2's spoken text
    await feed_into(L, NARRATOR + [
        (LLMFullResponseStartFrame(), 5.3),
        (LLMTextFrame(R2), 5.4),
        (LLMFullResponseEndFrame(), 5.5),
        (TTSStartedFrame(context_id="R2"), 5.6),            # chains into the ack's window
        (audio(1.0, "R2"), 5.6),
        (TTSStoppedFrame(context_id="F"), 6.7),
        (InterruptionFrame(), 7.0),                         # "wait --"
    ])
    msgs.append({"role": "user", "content": "wait"})
    cut = _bot(L)[-1]
    assert cut.text == R2, (
        f"the barged answer was charted under the previous reply's text: {cut.text[:40]!r}")
    assert cut.heard_fraction > 0.0, "the answer played before the barge-in; heard must not be 0"

    C._reconcile()
    kept = [m for m in msgs if m["role"] == "assistant" and m["content"]]
    assert kept and R2.startswith(kept[-1]["content"]), (
        "the corrector removed the answer's message from the context; the model now has "
        f"no record it answered. context: {[m['content'][:24] for m in msgs]}")


async def test_c_reply_started_after_the_next_completion_streams_keeps_its_own_text():
    """Text plus a tool call in one completion: the answer starts streaming before the
    first reply's TTS begins. The first reply is charted as the first reply."""
    L = await feed_into(TranscriptLedger(), [
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(R1), 0.1),
        (LLMFullResponseEndFrame(), 0.2),
        (LLMFullResponseStartFrame(), 0.3),
        (LLMTextFrame(R2), 0.4),                            # already streaming...
        (TTSStartedFrame(context_id="R1"), 0.5),            # ...when R1's TTS begins
        (audio(2.0, "R1"), 0.5),
        (BotStartedSpeakingFrame(), 0.6),
        (InterruptionFrame(), 1.2),
    ])
    cut = _bot(L)[0]
    assert cut.text == R1, (
        f"R1's playout was charted under the NEXT completion's text: {cut.text!r}")


async def test_d_filler_window_with_its_untagged_copy_does_not_claim_a_pending_reply():
    """test_b_filler_only_playout_never_becomes_a_turn, plus the frame it omits: the
    transport's untagged rebuild of the filler's audio. A reply's text is pending
    (its TTS delayed) while the narrator plays alone; that copy must not open a turn
    on the pending text, and the delayed reply must still be charted, once, itself."""
    L = await feed_into(TranscriptLedger(), [
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(R2), 0.1),
        (LLMFullResponseEndFrame(), 0.2),                   # pending; the TTS is slow
        (filler_started("F"), 0.5),
        (audio(1.5, "F"), 0.5),
        (BotStartedSpeakingFrame(), 0.6),
        (audio(1.5, None), 0.7),                            # the transport's untagged copy
        (TTSStoppedFrame(context_id="F"), 2.0),
        (BotStoppedSpeakingFrame(), 2.2),
        (TTSStartedFrame(context_id="R2"), 2.5),            # the delayed reply, own window
        (audio(1.0, "R2"), 2.5),
        (BotStartedSpeakingFrame(), 2.6),
        (BotStoppedSpeakingFrame(), 3.7),
    ])
    utts = _bot(L)
    assert len(utts) == 1 and utts[0].text == R2, (
        f"{[(u.text[:20], round(u.t_start, 1)) for u in utts]} -- the filler's untagged "
        "copy opened a turn on the pending reply")
    assert utts[0].t_start >= 2.5 and utts[0].heard_fraction >= 0.99


async def test_e_a_fillers_bot_stopped_does_not_close_a_reply_still_awaiting_its_audio():
    """Found by the model once the other fixes were in: TTSStarted is pushed at
    synthesis START, and when the first chunk is slow a filler queued just before it
    plays out and closes its own window first. That BotStopped must not close the
    reply's turn (it charted the reply complete with no audio, and the audio that
    then arrived opened a second turn on the same text)."""
    L = await feed_into(TranscriptLedger(), [
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(R1), 0.1),
        (filler_started("F"), 0.3),
        (audio(1.5, "F"), 0.3),
        (TTSStoppedFrame(context_id="F"), 0.4),
        (TTSStartedFrame(context_id="R1"), 0.5),            # synthesis starts...
        (BotStartedSpeakingFrame(), 0.6),                   # ...the filler plays 0.6-2.1
        (audio(1.5, None), 0.7),
        (BotStoppedSpeakingFrame(), 2.1),                   # the FILLER's window closes
        (LLMFullResponseEndFrame(), 2.2),
        (audio(3.0, "R1"), 2.4),                            # ...the slow first chunk lands
        (BotStartedSpeakingFrame(), 2.5),                   # the reply's own window
        (InterruptionFrame(), 4.0),                         # cut half way through
    ])
    utts = _bot(L)
    assert len(utts) == 1, (
        f"{[(u.text[:20], u.interrupted, round(u.heard_fraction, 2)) for u in utts]} -- "
        "the filler's BotStopped closed the reply's turn before its audio came")
    assert utts[0].text == R1 and utts[0].interrupted
    assert 0.3 < utts[0].heard_fraction < 0.7, utts[0].heard_fraction


async def test_f_two_completions_before_any_tts_are_spoken_in_order_under_their_own_text():
    """Two replies complete before the first's TTS begins (text plus a tool call, a
    fast tool, a slow first chunk). A single pending slot kept only the newer text,
    and the first reply's context then claimed it. Each context gets its own: the
    first is charted cut under ITS words, and when the barge-in makes the TTS drop
    the queued second (its flush) and the LLM re-runs it, the re-run's context is
    charted complete under the second's. (This used to script R2's playout after
    the cut with no re-run and then check only R1 -- R2 was charted nowhere.)"""
    L = await feed_into(TranscriptLedger(), [
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(R1), 0.1),
        (LLMFullResponseEndFrame(), 0.2),
        (LLMFullResponseStartFrame(), 0.3),
        (LLMTextFrame(R2), 0.4),
        (LLMFullResponseEndFrame(), 0.5),                   # both done; no TTS yet
        (TTSStartedFrame(context_id="R1"), 0.6),
        (audio(2.0, "R1"), 0.6),
        (TTSStoppedFrame(context_id="R1"), 0.65),           # synthesized in full: 2.0s is the length
        (BotStartedSpeakingFrame(), 0.7),
        (InterruptionFrame(), 1.7),                         # cut R1 half way; R2's context is flushed
        (LLMFullResponseStartFrame(), 1.8),                 # the LLM re-runs R2...
        (LLMTextFrame(R2), 1.85),
        (LLMFullResponseEndFrame(), 1.9),
        (TTSStartedFrame(context_id="R2b"), 2.0),           # ...and it is spoken under a new context
        (audio(1.0, "R2b"), 2.0),
        (BotStartedSpeakingFrame(), 2.1),
        (BotStoppedSpeakingFrame(), 3.2),
    ])
    utts = _bot(L)
    assert [u.text for u in utts] == [R1, R2], [u.text[:24] for u in utts]
    assert utts[0].interrupted and 0.3 < utts[0].heard_fraction < 0.7, utts[0].heard_fraction
    assert not utts[1].interrupted and utts[1].heard_fraction >= 0.99, utts[1].heard_fraction


async def test_g_a_reply_played_out_before_its_end_frame_is_not_queued_for_the_next():
    """A short reply's playout finishes before LLMFullResponseEnd arrives (the
    generation stalls past the transport's silence timeout). Its turn is charted
    from the live text; the End frame must not then queue the text as unspoken, or
    the next reply's context takes it."""
    L = await feed_into(TranscriptLedger(), [
        (LLMFullResponseStartFrame(), 0.0),
        (LLMTextFrame(R1), 0.1),
        (TTSStartedFrame(context_id="R1"), 0.2),
        (audio(1.0, "R1"), 0.2),
        (TTSStoppedFrame(context_id="R1"), 0.25),
        (BotStartedSpeakingFrame(), 0.3),
        (BotStoppedSpeakingFrame(), 1.5),                   # played out, charted...
        (LLMFullResponseEndFrame(), 2.0),                   # ...then the End arrives
        (LLMFullResponseStartFrame(), 3.0),
        (LLMTextFrame(R2), 3.1),
        (TTSStartedFrame(context_id="R2"), 3.2),
        (audio(1.0, "R2"), 3.2),
        (BotStartedSpeakingFrame(), 3.3),
        (InterruptionFrame(), 3.8),
    ])
    utts = _bot(L)
    assert [u.text for u in utts] == [R1, R2], [u.text[:24] for u in utts]
    assert not utts[0].interrupted and utts[1].interrupted


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
