#
# Unit test: the ledger through the PRODUCTION sightings -- what it sees as pipecat
# 1.7.0's TTS service and output transport actually present frames, which the other
# ledger scripts (hermetic, no processor identity) leave out.
#
# Three things only the live pipeline shows, and this file drives all of them:
#
#   - Every frame is sighted once per processor, and the ledger is told which
#     processor the TTS and the output transport are. It reads the LLM stream at the
#     TTS's sighting -- the text the TTS is handed, after LLMTextGuard has mutated the
#     frame -- not the LLM's raw push above the guard.
#   - The TTS holds a response's LLMFullResponseEndFrame and re-pushes the SAME frame
#     once the response's context has drained (tts_service._maybe_reset_word_timestamps).
#     Its second sighting, below the TTS, tells the ledger which response a context
#     spoke and that its synthesis is over. The engine TTS pushes no TTSStoppedFrame
#     at all (push_stop_frames is False), so this is the only completion signal live.
#   - Untagged audio comes from two sources the ledger must tell apart: the
#     thinking-sound bed, pushed INTO the transport (first sighted at the transport),
#     and the transport's own rebuild of every chunk it played (first sighted below it).
#
# Each case is one of the review's findings on PR #13's ledger, in the frame shape the
# pipeline produces (a shape the TTS cannot produce -- a context starting while an
# earlier one is still synthesizing -- is not scripted: it drains one at a time).
#
# Run: python test_ledger_playout.py   (or via pytest test_suite.py)
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
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSTextFrame,
)
from pipecat.utils.text.base_text_aggregator import AggregationType  # noqa: E402

from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

SR = 24000

# The processors, as identities only: the ledger compares them with `is`.
ABOVE = SimpleNamespace(name="LLMTextGuard#0")          # between the LLM and the TTS
TTS = SimpleNamespace(name="EngineTTSService#0")
BELOW = SimpleNamespace(name="EndpointDebug#0")         # first to see the TTS's pushes
OUT = SimpleNamespace(name="FastAPIWebsocketOutputTransport#0")
AFTER = SimpleNamespace(name="FollowupGate#0")          # first to see the transport's pushes

R1 = "The forecast for tomorrow is sunny with a high of seventy two."  # 12 words
R2 = "It is Tuesday the second."                                        # 5 words
ANSWER = "The bakery opens at eight and closes at six."                 # 9 words


def ledger():
    return TranscriptLedger(tts=TTS, output=OUT)


async def feed(L, seq):
    """seq: (frame, t) or (frame, t, processor). No processor = a hermetic sighting."""
    for f, t, *proc in seq:
        await L.on_process_frame(SimpleNamespace(
            frame=f, timestamp=int(t * 1e9), processor=proc[0] if proc else None))
    return L


def bot(L):
    return [e for e in L.events if e.speaker == "assistant"]


def audio(secs, ctx, proc=BELOW):
    """The TTS's tagged push, first sighted just below the TTS."""
    return (TTSAudioRawFrame(b"\x00\x00" * int(secs * SR), SR, 1, context_id=ctx), None, proc)


def copy(secs):
    """The transport's rebuild of audio it played: untagged, first sighted below it."""
    return TTSAudioRawFrame(b"\x00\x00" * int(secs * SR), SR, 1)


def bed(secs):
    """A thinking-sound chunk, pushed INTO the transport: untagged, sighted there."""
    return TTSAudioRawFrame(b"\x00\x00" * int(secs * SR), SR, 1)


def at(item, t):
    f, _, proc = item
    return (f, t, proc)


def started(ctx, filler=False):
    return TTSStartedFrame(context_id=ctx, append_to_context=not filler)


def word(s, pts_s, ctx, ifs=False):
    f = TTSTextFrame(s, aggregated_by=AggregationType.WORD)
    f.pts = int(pts_s * 1e9)
    f.context_id = ctx
    f.includes_inter_frame_spaces = ifs
    return f


def response(text, t0, step=0.05):
    """An LLM response as the TTS sees it. Returns (frames, end) -- `end` is the End
    frame, to be re-pushed (drained) later under the same id."""
    end = LLMFullResponseEndFrame()
    frames = [(LLMFullResponseStartFrame(), t0, TTS),
              (LLMTextFrame(text), t0 + step, TTS),
              (end, t0 + 2 * step, TTS)]
    return frames, end


def drained(end, t):
    """pipecat's post-drain re-push of the response's End frame, sighted below the TTS."""
    return (end, t, BELOW)


def siblings(cls, t):
    """A BotStarted/BotStopped broadcast pair: the downstream frame, then the upstream
    one with its own id, each naming the other (the ledger sees both)."""
    down, up = cls(), cls()
    down.broadcast_sibling_id, up.broadcast_sibling_id = up.id, down.id
    return [(down, t, AFTER), (up, t + 0.001, BELOW)]


# ------------------------------------------------------------------ the cases

async def test_a_the_drain_confirms_the_context_and_drops_a_stranded_response():
    """The queue of expected contexts is a HEURISTIC (the TTS opens them in order);
    the re-push of a response's End frame after its context drains is the fact.
    R1's text reaches the TTS but the TTS opens no context for it; the next context
    to start claims R1 by order. When that context drains, R2's End is what comes
    back, so the turn is R2's: charted once, under R2's words, and R1 is dropped as
    never synthesized -- it does not shift every later turn's text by one."""
    f1, end1 = response(R1, 0.0)
    f2, end2 = response(R2, 0.3)
    L = await feed(ledger(), f1 + f2 + [
        (started("C"), 0.6, BELOW),                 # claims R1 by order...
        at(audio(1.0, "C"), 0.6),
        drained(end2, 0.65),                        # ...but the drain says: this was R2
        *siblings(BotStartedSpeakingFrame, 0.7),
        (copy(1.0), 1.7, AFTER),
        *siblings(BotStoppedSpeakingFrame, 2.05),
    ])
    utts = bot(L)
    assert [u.text for u in utts] == [R2], [u.text[:24] for u in utts]
    assert utts[0].heard_fraction >= 0.99 and not utts[0].interrupted
    assert not L._queue, "the drained response was left queued for a later turn"


async def test_b_a_fillers_window_with_a_reply_pending_opens_no_turn():
    """The phantom class, in the production shape: a reply's text is pending (its
    context queued behind two fillers) while the fillers play, each in a window of
    its own, with the transport's untagged copies coming back. BotStarted is
    anonymous and opens nothing; the reply is charted once, in ITS window, from
    its own audio's playout start."""
    f1, end1 = response(R2, 0.0)
    L = await feed(ledger(), f1 + [
        (started("F1", filler=True), 0.2, BELOW),
        at(audio(1.0, "F1"), 0.2),
        *siblings(BotStartedSpeakingFrame, 0.3),
        (copy(1.0), 1.3, AFTER),
        *siblings(BotStoppedSpeakingFrame, 1.65),
        (started("F2", filler=True), 2.0, BELOW),
        at(audio(1.0, "F2"), 2.0),
        *siblings(BotStartedSpeakingFrame, 2.1),
        (copy(1.0), 3.1, AFTER),
        *siblings(BotStoppedSpeakingFrame, 3.45),
        (started("R", ), 4.0, BELOW),               # the reply's context, at last
        at(audio(1.5, "R"), 4.0),
        drained(end1, 4.05),
        *siblings(BotStartedSpeakingFrame, 4.1),
        (copy(1.5), 5.6, AFTER),
        *siblings(BotStoppedSpeakingFrame, 5.95),
    ])
    utts = bot(L)
    assert len(utts) == 1 and utts[0].text == R2, [(u.text[:20], u.t_start) for u in utts]
    assert utts[0].heard_fraction >= 0.99
    assert abs(utts[0].t_start - 4.1) < 0.01 and abs(utts[0].t_end - 5.6) < 0.01, (
        utts[0].t_start, utts[0].t_end)


async def test_c_a_context_that_drains_with_no_audio_is_never_played_and_the_next_is_itself():
    """The engine synthesized nothing for R1 (every clause failed): its context
    opened, timed out, and drained with no chunk. The turn must not wait for a
    window forever -- nor block the next reply's turn: R1 is charted heard 0 (a
    cut, so the corrector reconciles it), and R2 is charted complete, as R2."""
    f1, end1 = response(R1, 0.0)
    f2, end2 = response(R2, 5.0)
    L = await feed(ledger(), f1 + [
        (started("R1"), 0.2, BELOW),
        drained(end1, 15.2),                        # the stop-frame timeout: nothing came
    ] + f2 + [
        (started("R2"), 15.3, BELOW),
        at(audio(1.0, "R2"), 15.3),
        drained(end2, 15.35),
        *siblings(BotStartedSpeakingFrame, 15.4),
        *siblings(BotStoppedSpeakingFrame, 16.75),
    ])
    utts = bot(L)
    assert [u.text for u in utts] == [R1, R2], [u.text[:24] for u in utts]
    assert utts[0].interrupted and utts[0].heard_fraction == 0.0 and utts[0].heard_text == ""
    assert not utts[1].interrupted and utts[1].heard_fraction >= 0.99


async def test_c_a_reply_queued_behind_a_filler_starts_after_it_and_closes_with_the_window():
    """Synthesis runs ahead of playout: the reply's audio is pushed while the filler
    still plays, and plays after it in the same window. The turn's start is where
    its chunk lands in the layout, and it closes complete only when the window does
    -- never at synthesis time with no sample played."""
    f1, end1 = response(R2, 0.0)
    L = await feed(ledger(), [
        (started("F", filler=True), 0.2, BELOW),
        at(audio(1.5, "F"), 0.2),
        *siblings(BotStartedSpeakingFrame, 0.3),   # the filler plays 0.3-1.8
    ] + f1 + [
        (started("R"), 0.5, BELOW),
        at(audio(1.0, "R"), 0.6),                   # queued behind the filler: 1.8-2.8
        drained(end1, 0.65),
        (copy(1.5), 1.8, AFTER),
        (copy(1.0), 2.8, AFTER),
        *siblings(BotStoppedSpeakingFrame, 3.15),
    ])
    utts = bot(L)
    assert len(utts) == 1 and utts[0].text == R2
    assert abs(utts[0].t_start - 1.8) < 0.01 and abs(utts[0].t_end - 2.8) < 0.01, (
        utts[0].t_start, utts[0].t_end)
    assert utts[0].heard_fraction >= 0.99


async def test_d_a_spoken_notice_is_charted_as_itself_and_the_reply_keeps_its_text():
    """A TTSSpeakFrame that is NOT a filler (llm_error_speaker's notice; the
    STT-busy line) is an assistant utterance in its own right, and its context
    must claim ITS text -- not the reply queued behind it. Here the notice plays
    while the next reply streams, and the reply chains behind it in the same
    window: both charted, each under its own words, and the reply's text is not
    lost to a latched claim."""
    notice = "Sorry, the model is not responding."
    f1, end1 = response(R1, 0.7)
    L = await feed(ledger(), [
        (TTSSpeakFrame(notice), 0.1, ABOVE),        # the LLM's sighting: not the TTS's
        (TTSSpeakFrame(notice), 0.2, TTS),
        (started("N"), 0.5, BELOW),
        at(audio(1.0, "N"), 0.5),
        *siblings(BotStartedSpeakingFrame, 0.6),
    ] + f1[:2] + [                                  # R1 streams while the notice plays
        (started("R1"), 1.0, BELOW),
        at(audio(2.0, "R1"), 1.0),                  # chains after the notice: 1.6-3.6
    ] + f1[2:] + [
        drained(end1, 1.2),
        *siblings(BotStoppedSpeakingFrame, 3.95),
    ])
    utts = bot(L)
    assert [u.text for u in utts] == [notice, R1], [u.text[:24] for u in utts]
    assert all(u.heard_fraction >= 0.99 for u in utts)
    assert abs(utts[1].t_start - 1.6) < 0.01, utts[1].t_start


async def test_e_the_thinking_bed_takes_playout_time_but_opens_no_turn():
    """The bed is untagged audio pushed INTO the transport (thinking_sound.py):
    it belongs to no reply, so it opens no turn on the replies queued during the
    consult -- and it occupies the queue, so a reply whose audio lands behind the
    bed's last chunks starts after them: a barge-in half way into the reply reads
    half, not the bed's seconds plus."""
    f1, end1 = response(R1, 0.0)
    f2, end2 = response(R2, 0.3)                    # two completions before any TTS
    L = await feed(ledger(), f1 + f2 + [
        (bed(0.5), 0.0, OUT), *siblings(BotStartedSpeakingFrame, 0.05),
        (bed(0.5), 0.5, OUT), (bed(0.5), 1.0, OUT), (bed(0.5), 1.5, OUT),
        (copy(0.5), 0.55, AFTER), (copy(0.5), 1.05, AFTER), (copy(0.5), 1.55, AFTER),
        (started("R1"), 1.6, BELOW),
        at(audio(2.0, "R1"), 1.6),                  # behind the bed: plays 2.05-4.05
        drained(end1, 1.7),
        (copy(0.5), 2.05, AFTER),
        (InterruptionFrame(), 3.05),                # half way into R1
    ])
    utts = bot(L)
    assert [u.text for u in utts] == [R1], [u.text[:24] for u in utts]
    assert utts[0].interrupted and 0.45 < utts[0].heard_fraction < 0.55, utts[0].heard_fraction
    assert abs(utts[0].t_start - 2.05) < 0.01, utts[0].t_start


async def test_e_two_replies_after_the_bed_are_charted_in_order_under_their_own_text():
    f1, end1 = response(R1, 0.0)
    f2, end2 = response(R2, 0.3)
    L = await feed(ledger(), f1 + f2 + [
        (bed(0.5), 0.0, OUT), *siblings(BotStartedSpeakingFrame, 0.05),
        (bed(0.5), 0.5, OUT), (copy(0.5), 0.55, AFTER), (copy(0.5), 1.05, AFTER),
        *siblings(BotStoppedSpeakingFrame, 1.4),    # the bed stopped; a gap
        (started("R1"), 2.0, BELOW), at(audio(2.0, "R1"), 2.0), drained(end1, 2.05),
        (started("R2"), 2.1, BELOW), at(audio(1.0, "R2"), 2.1), drained(end2, 2.15),
        *siblings(BotStartedSpeakingFrame, 2.2),
        (copy(2.0), 4.2, AFTER), (copy(1.0), 5.2, AFTER),
        *siblings(BotStoppedSpeakingFrame, 5.55),
    ])
    utts = bot(L)
    assert [u.text for u in utts] == [R1, R2], [u.text[:24] for u in utts]
    assert all(u.heard_fraction >= 0.99 and not u.interrupted for u in utts)
    assert abs(utts[0].t_start - 2.2) < 0.01 and abs(utts[1].t_start - 4.2) < 0.01


async def test_f_a_reply_chained_behind_queued_filler_audio_is_credited_only_its_own_playout():
    """Fast tool flow: the spoken ack (a reply, context A), a narrator line queued
    behind it (a marked filler), and the answer (context B) chaining behind both.
    The answer's playout starts after the filler's, and a barge-in 0.8s into it is
    0.8s of the answer -- not the seconds the ack and the filler had been playing."""
    ack = "Sure, let me check that."
    fa, enda = response(ack, 0.0)
    fb, endb = response(ANSWER, 0.8)
    words = [(word(w, 3.7 + i * 0.36, "B"), 1.15, BELOW) for i, w in enumerate(ANSWER.split())]
    L = await feed(ledger(), fa + [
        (started("A"), 0.5, BELOW), at(audio(1.5, "A"), 0.5), drained(enda, 0.55),
        *siblings(BotStartedSpeakingFrame, 0.6),    # the ack plays 0.6-2.1
        (started("F", filler=True), 0.7, BELOW), at(audio(1.5, "F"), 0.7),   # 2.1-3.6
    ] + fb + [
        (started("B"), 1.1, BELOW), at(audio(3.24, "B"), 1.1), drained(endb, 1.2),  # 3.6-6.84
        *words,
        (copy(1.5), 2.1, AFTER), (copy(1.5), 3.6, AFTER),
        (InterruptionFrame(), 4.4),                 # 0.8s into the answer
    ])
    utts = bot(L)
    assert [u.text for u in utts] == [ack, ANSWER], [u.text[:24] for u in utts]
    assert not utts[0].interrupted and utts[0].heard_fraction >= 0.99
    ans = utts[1]
    assert ans.interrupted and 0.2 < ans.heard_fraction < 0.35, ans.heard_fraction
    assert ans.heard_text.split() == ["The", "bakery"], ans.heard_text
    assert abs(ans.t_start - 3.6) < 0.01, ans.t_start


async def test_g_a_cut_before_a_queued_reply_starts_is_heard_zero_never_negative():
    """The reply's chunk is queued behind a filler that is still playing when the
    user barges in: the reply's playout start is in the FUTURE of the cut. Heard is
    0.0 -- not a negative fraction from (cut - future start)."""
    f1, end1 = response(R2, 0.0)
    L = await feed(ledger(), [
        (started("F", filler=True), 0.2, BELOW), at(audio(2.0, "F"), 0.4),
        *siblings(BotStartedSpeakingFrame, 0.5),    # the filler plays 0.5-2.5
    ] + f1 + [
        (started("R"), 0.6, BELOW), at(audio(1.0, "R"), 0.7), drained(end1, 0.75),  # 2.5-3.5
        (InterruptionFrame(), 1.0),
    ])
    utts = bot(L)
    assert len(utts) == 1 and utts[0].text == R2
    assert utts[0].interrupted and utts[0].heard_fraction == 0.0 and utts[0].heard_text == "", (
        utts[0].heard_fraction, utts[0].heard_text)


async def test_h_word_frames_with_their_own_spacing_are_concatenated():
    """A voice whose word frames are characters (the ja/cmn voices) marks them
    includes_inter_frame_spaces=True: the heard text is their concatenation, a true
    prefix of the reply, and the unheard tail is exact. Engine frames without the
    flag ("One,","two,") are space-joined -- and the tail is still exact, because
    the prefix match ignores whitespace."""
    cjk = "我来查一下天气情况请稍等一会儿好吗"
    f1, end1 = response(cjk, 0.0)
    chars = [(word(c, 1.1 + i * 0.25, "R", ifs=True), 0.55, BELOW) for i, c in enumerate(cjk)]
    L = await feed(ledger(), f1 + [
        (started("R"), 0.5, BELOW), at(audio(4.5, "R"), 0.5), drained(end1, 0.55),
        *siblings(BotStartedSpeakingFrame, 1.0),
        *chars,
        (InterruptionFrame(), 3.6),                 # pts <= 3.4: the first ten characters
    ])
    u = bot(L)[0]
    assert u.heard_text == "我来查一下天气情况请", repr(u.heard_text)
    assert u.unheard_tail() == "稍等一会儿好吗", repr(u.unheard_tail())

    count = "One, two, three, four."
    f2, end2 = response(count, 0.0)
    ws = [(word(w, 1.1 + i * 0.4, "R"), 0.55, BELOW) for i, w in enumerate(count.split())]
    L = await feed(ledger(), f2 + [
        (started("R"), 0.5, BELOW), at(audio(2.0, "R"), 0.5), drained(end2, 0.55),
        *siblings(BotStartedSpeakingFrame, 1.0),
        *ws,
        (InterruptionFrame(), 1.9),                 # pts <= 1.7: two words
    ])
    u = bot(L)[0]
    assert u.heard_text == "One, two,", repr(u.heard_text)
    assert u.unheard_tail() == "three, four.", repr(u.unheard_tail())


async def test_i_a_window_closing_mid_reply_keeps_the_turn_open():
    """Synthesis stalls behind STT on the GPU: the reply's first chunk plays out,
    the transport runs dry for 0.35s and closes the window, the second chunk
    arrives and opens another. One utterance -- from the first chunk's start to
    the second's end -- not a complete turn at the stall plus an orphan."""
    f1, end1 = response(R2, 0.0)
    L = await feed(ledger(), f1 + [
        (started("R"), 0.5, BELOW), at(audio(1.0, "R"), 0.5),
        *siblings(BotStartedSpeakingFrame, 0.6),    # 0.6-1.6
        (copy(1.0), 1.6, AFTER),
        *siblings(BotStoppedSpeakingFrame, 1.95),   # the stall
        at(audio(1.0, "R"), 2.5), drained(end1, 2.55),
        *siblings(BotStartedSpeakingFrame, 2.6),    # 2.6-3.6
        (copy(1.0), 3.6, AFTER),
        *siblings(BotStoppedSpeakingFrame, 3.95),
    ])
    utts = bot(L)
    assert len(utts) == 1 and utts[0].text == R2, [(u.text[:20], u.t_start, u.t_end) for u in utts]
    assert utts[0].heard_fraction >= 0.99 and not utts[0].interrupted
    assert abs(utts[0].t_start - 0.6) < 0.01 and abs(utts[0].t_end - 3.6) < 0.01, (
        utts[0].t_start, utts[0].t_end)


async def test_i_a_cut_in_the_second_window_counts_both_windows():
    f1, end1 = response(R2, 0.0)
    L = await feed(ledger(), f1 + [
        (started("R"), 0.5, BELOW), at(audio(1.0, "R"), 0.5),
        *siblings(BotStartedSpeakingFrame, 0.6),
        *siblings(BotStoppedSpeakingFrame, 1.95),
        at(audio(1.0, "R"), 2.5), drained(end1, 2.55),
        *siblings(BotStartedSpeakingFrame, 2.6),
        (InterruptionFrame(), 3.1),                 # 1.0 + 0.5 of 2.0
    ])
    u = bot(L)[0]
    assert u.interrupted and 0.7 < u.heard_fraction < 0.8, u.heard_fraction


async def test_j_two_replies_queued_in_one_window_are_each_cut_by_their_own_playout():
    """Two replies back to back at the transport (the second's context started
    while the first was still playing). A barge-in during the first's tail cuts
    the FIRST -- the second, still queued, is heard 0. Charting the first complete
    the moment the second's context began (the old chained branch) recorded a
    tail the user never heard as heard."""
    f1, end1 = response(R1, 0.0)
    f2, end2 = response(R2, 0.3)
    chain = f1 + f2 + [
        (started("R1"), 0.5, BELOW), at(audio(2.0, "R1"), 0.5), drained(end1, 0.55),
        *siblings(BotStartedSpeakingFrame, 0.6),    # R1 0.6-2.6
        (started("R2"), 0.7, BELOW), at(audio(2.0, "R2"), 0.7), drained(end2, 0.75),  # R2 2.6-4.6
    ]
    L = await feed(ledger(), chain + [(InterruptionFrame(), 2.0)])
    utts = bot(L)
    assert [u.text for u in utts] == [R1, R2], [u.text[:24] for u in utts]
    assert utts[0].interrupted and 0.65 < utts[0].heard_fraction < 0.75, utts[0].heard_fraction
    assert utts[1].interrupted and utts[1].heard_fraction == 0.0

    L = await feed(ledger(), chain + [(copy(2.0), 2.6, AFTER), (InterruptionFrame(), 3.6)])
    utts = bot(L)
    assert [u.text for u in utts] == [R1, R2]
    assert not utts[0].interrupted and utts[0].heard_fraction >= 0.99, utts[0].heard_fraction
    assert abs(utts[0].t_end - 2.6) < 0.01, utts[0].t_end
    assert utts[1].interrupted and 0.45 < utts[1].heard_fraction < 0.55, utts[1].heard_fraction


async def test_k_the_intended_text_is_what_the_tts_receives_not_the_raw_deltas():
    """LLMTextGuard mutates the delta in place (a leaked Harmony token stripped)
    and forwards the same frame; a swallowed completion gets a NEW frame with the
    recovery line. The ledger reads the stream at the TTS, so the utterance -- and
    what HeardContextCorrector writes back -- is the guarded text."""
    raw = LLMTextFrame("Sure thing, <|reserved_200097|> I can help.")
    end = LLMFullResponseEndFrame()
    L = await feed(ledger(), [
        (LLMFullResponseStartFrame(), 0.0, ABOVE),
        (raw, 0.1, ABOVE),                          # the LLM's raw push, above the guard
        (LLMFullResponseStartFrame(), 0.11, TTS),
    ])
    raw.text = "Sure thing, I can help."             # the guard's in-place strip...
    await feed(L, [
        (raw, 0.12, TTS),                           # ...is what the TTS is handed
        (end, 0.2, ABOVE), (end, 0.21, TTS),
        (started("R"), 0.5, BELOW), at(audio(1.0, "R"), 0.5), drained(end, 0.55),
        *siblings(BotStartedSpeakingFrame, 0.6),
        *siblings(BotStoppedSpeakingFrame, 1.95),
    ])
    assert [u.text for u in bot(L)] == ["Sure thing, I can help."], [u.text for u in bot(L)]

    junk, end = LLMTextFrame("......... ... ......"), LLMFullResponseEndFrame()
    L = await feed(ledger(), [
        (LLMFullResponseStartFrame(), 0.0, ABOVE), (LLMFullResponseStartFrame(), 0.01, TTS),
        (junk, 0.1, ABOVE),                         # swallowed by the guard: never reaches the TTS
        (LLMTextFrame("Sorry, let me try that again."), 0.12, TTS),   # the recovery line
        (end, 0.2, ABOVE), (end, 0.21, TTS),
        (started("R"), 0.5, BELOW), at(audio(1.0, "R"), 0.5), drained(end, 0.55),
        *siblings(BotStartedSpeakingFrame, 0.6),
        *siblings(BotStoppedSpeakingFrame, 1.95),
    ])
    assert [u.text for u in bot(L)] == ["Sorry, let me try that again."], [u.text for u in bot(L)]


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
