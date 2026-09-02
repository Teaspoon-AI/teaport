#
# Regression test: a cut reply must not be charted a second time by a filler's playout,
# and the turn a filler opens must never claim the NEXT reply under the old text.
#
# Found by brain/formal/Ledger.tla (rows ledger_once and ledger_wrongText; the traces are
# in brain/formal/README.md) and reproduced here against the real ledger and corrector.
#
# The shape: a reply is barged over during synthesis. The ledger charts it cut (heard 0),
# correctly. But nothing resets what it holds -- InterruptionFrame only calls _finish_bot,
# and the cancelled completion's finally (pipecat 1.7.0 base_llm.py:571-573) then pushes
# LLMFullResponseEndFrame with the partial text, which re-arms _pending_gen. The next
# filler to play (a tool ack, a narrator line) comes back from the output transport as an
# UNTAGGED TTSAudioRawFrame (base_output rebuilds audio without its context_id), which
# _is_filler cannot recognise, and _ensure_bot opens a turn on the cut reply's text.
#
#   A  If that turn closes on the filler's BotStoppedSpeaking, the cut reply is charted
#      AGAIN, complete, heard 1.0 -- during a window in which only the filler played.
#      Inert today (HeardContextCorrector acts on cut turns only), but wrong.
#   B  If the next reply's TTS starts while that turn is still open (the answer chaining
#      into the ack's window -- the fast tool flow), its TTSStarted takes the adopt-ctx
#      branch instead of opening a turn, and the answer is charted under the OLD text
#      with no audio_start. Barged, the corrector receives heard_text "" and DELETES the
#      answer's committed message: the model then has no record that it answered.
#   C  The same wrong-text chart on a pre-existing path: _new_bot prefers the in-flight
#      generation, so a reply whose TTS starts after the NEXT completion has begun
#      streaming is charted under the next completion's text.
#
# Every assertion states the behaviour the ledger SHOULD have. As of PR #13 all three
# fail; this file is red until the ledger is fixed.
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
