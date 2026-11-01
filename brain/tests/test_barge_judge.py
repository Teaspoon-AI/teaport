#
# Barge-in judge (barge_pause.BargeInJudgeGate, #224): words over a paused reply are
# shown to a one-word judge before they start the turn. CONTINUE drops them and the
# reply resumes as it does for a cough; STOP, ANSWER or no verdict lets them on as they
# came, and the turn starts as it always did, that much later.
#
# The gate runs in a real pipeline between two taps, with the session's real start
# strategy deciding which words it judges and a scripted judge. The ledger's view of the
# paused reply is read off the same SIP rig test_barge_pause drives.
#
# All utterances are synthetic.
#
# Run: python test_barge_judge.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain import barge_pause  # noqa: E402
from teaport_brain.barge_pause import (  # noqa: E402
    ANSWER,
    CONTINUE,
    STOP,
    BargeInJudgeGate,
    PauseAwareMinWordsStrategy,
    PlayoutPauseFrame,
    judge_messages,
    judge_verdict,
)
from teaport_brain.endpointing import SegmentDoneFrame  # noqa: E402
from teaport_brain.stt import FinalTranscriptionFrame  # noqa: E402

from test_barge_pause import (  # noqa: E402
    RESUME_S,
    STARTUP_S,
    WORDS,
    Rig,
    Tap,
    _at,
    _pauser_rig,
)

WAIT_S = 0.3


def interim(text):
    return InterimTranscriptionFrame(text, "u", "t")


def final(text, stop_n=3):
    return FinalTranscriptionFrame(text, "u", "t", finalized=True, stop_n=stop_n)


class _Judge:
    """Scripted verdicts by the words asked on, each after `delay`."""

    def __init__(self, verdicts, delay=0.05):
        self.verdicts = verdicts
        self.delay = delay
        self.asked = []

    async def __call__(self, said):
        self.asked.append(said)
        await asyncio.sleep(self.delay)
        return self.verdicts.get(said)


class GateRig:
    """top -> gate -> bottom, the start strategy fed the bot's state by hand."""

    def __init__(self, judge, *, paused=True, min_words=1, wait=WAIT_S):
        self.strategy = PauseAwareMinWordsStrategy(min_words=min_words)
        self.holds = []
        self.judge = judge
        self.gate = BargeInJudgeGate(self.strategy, judge, hold_resume=self.holds.append,
                                     wait_secs=wait)
        self.top, self.bottom = Tap(), Tap()
        self.task = PipelineTask(Pipeline([self.top, self.gate, self.bottom]),
                                 cancel_on_idle_timeout=False)
        self._paused = paused

    async def __aenter__(self):
        self._run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.sleep(STARTUP_S)
        await self.strategy.process_frame(BotStartedSpeakingFrame())
        if self._paused:
            await self.strategy.process_frame(PlayoutPauseFrame(paused=True))
        return self

    async def __aexit__(self, *exc):
        await self.task.cancel()
        await self._run

    def out(self):
        """What reached the aggregator's side: (kind, text or stop_n)."""
        got = []
        for _, d, f in self.bottom.frames:
            if d != FrameDirection.DOWNSTREAM:
                continue
            if isinstance(f, InterimTranscriptionFrame):
                got.append(("interim", f.text))
            elif isinstance(f, TranscriptionFrame):
                got.append(("final", f.text))
            elif isinstance(f, SegmentDoneFrame):
                got.append(("close", f.stop_n))
        return got


async def test_a_backchannel_is_dropped_and_its_final_becomes_a_wordless_close():
    judge = _Judge({"Okay": CONTINUE})
    async with GateRig(judge) as rig:
        await rig.task.queue_frames([interim("Okay"), final("Okay.")])
        await asyncio.sleep(0.25)
        assert judge.asked == ["Okay"], judge.asked
        assert rig.out() == [("close", 3)], (
            f"{rig.out()}: CONTINUE must keep the words from the turn strategies, and "
            "the close must still reach the stop strategy, stamped as the final was")
        assert rig.holds == [True, False], rig.holds
        assert rig.gate.verdicts[CONTINUE] == 1


async def test_a_final_after_the_verdict_is_not_asked_again_nor_cuts_the_resumed_reply():
    judge = _Judge({"Okay": CONTINUE})
    async with GateRig(judge) as rig:
        await rig.task.queue_frame(interim("Okay"))
        await asyncio.sleep(0.15)
        # The verdict resumed the reply; its final comes after that.
        await rig.strategy.process_frame(PlayoutPauseFrame(paused=False))
        await rig.task.queue_frame(PlayoutPauseFrame(paused=False),
                                   FrameDirection.UPSTREAM)
        await rig.task.queue_frame(final("Okay."))
        await asyncio.sleep(0.15)
        assert judge.asked == ["Okay"]
        assert rig.out() == [("close", 3)], rig.out()


async def test_a_question_is_let_on_unchanged_once_judged():
    judge = _Judge({"Wait, what?": ANSWER}, delay=0.1)
    async with GateRig(judge) as rig:
        t0 = time.monotonic()
        await rig.task.queue_frames([interim("Wait, what?"), final("Wait, what?")])
        await asyncio.sleep(0.03)
        assert rig.out() == [], "held while the judge decides"
        await asyncio.sleep(0.2)
        assert rig.out() == [("interim", "Wait, what?"), ("final", "Wait, what?")], rig.out()
        let_on = next(t for t, d, f in rig.bottom.frames
                      if isinstance(f, InterimTranscriptionFrame))
        assert let_on - t0 >= 0.1, "let on before the verdict"
        assert rig.holds == [True, False]


async def test_a_stop_verdict_lets_the_words_on_too():
    judge = _Judge({"That's enough for now": STOP})
    async with GateRig(judge) as rig:
        await rig.task.queue_frame(final("That's enough for now"))
        await asyncio.sleep(0.2)
        assert rig.out() == [("final", "That's enough for now")], rig.out()


async def test_words_that_grow_while_judged_are_asked_on_whole():
    judge = _Judge({"Okay": CONTINUE, "Okay, and then what?": ANSWER}, delay=0.1)
    async with GateRig(judge, wait=0.6) as rig:
        await rig.task.queue_frame(interim("Okay"))
        await asyncio.sleep(0.03)
        await rig.task.queue_frames([interim("Okay, and then what?"),
                                     final("Okay, and then what?")])
        await asyncio.sleep(0.35)
        assert judge.asked == ["Okay", "Okay, and then what?"], judge.asked
        assert [k for k, _ in rig.out()] == ["interim", "interim", "final"], rig.out()


async def test_a_stop_word_while_judging_takes_the_turn_at_once():
    judge = _Judge({"Okay": CONTINUE}, delay=0.25)
    async with GateRig(judge) as rig:
        await rig.task.queue_frame(interim("Okay"))
        await asyncio.sleep(0.03)
        await rig.task.queue_frame(final("Stop.", stop_n=4))
        await asyncio.sleep(0.05)
        assert rig.out() == [("interim", "Okay"), ("final", "Stop.")], (
            f"{rig.out()}: a stop must not wait on a verdict for the words before it")
        assert rig.holds == [True, False], rig.holds
        await asyncio.sleep(0.3)
        assert judge.asked == ["Okay"] and len(rig.out()) == 2, "its verdict is dropped"


async def test_words_that_keep_growing_are_not_cut_while_each_verdict_is_in_time():
    words = ["Yeah", "Yeah, now", "Yeah, now you", "Yeah, now you found it"]
    judge = _Judge({w: CONTINUE for w in words}, delay=0.2)
    async with GateRig(judge) as rig:
        for w in words:
            await rig.task.queue_frame(interim(w))
            await asyncio.sleep(0.15)
        await asyncio.sleep(0.4)
        assert judge.asked == words, judge.asked
        assert rig.out() == [] and rig.gate.verdicts[None] == 0, (
            f"{rig.out()}: four quick CONTINUEs over {0.15 * len(words) + 0.2:.1f}s, past "
            f"one {WAIT_S}s wait in all -- a caller still talking is not a slow judge")


async def test_words_whose_segment_closed_empty_do_not_swallow_a_later_utterance():
    judge = _Judge({"Okay sure": CONTINUE})
    async with GateRig(judge) as rig:
        await rig.task.queue_frames([interim("Okay sure"), SegmentDoneFrame(stop_n=3)])
        await asyncio.sleep(0.15)
        assert rig.out() == [("close", 3)], rig.out()
        await rig.strategy.process_frame(BotStoppedSpeakingFrame())
        await rig.task.queue_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await rig.task.queue_frame(final("Okay, sure.", stop_n=4))
        await asyncio.sleep(0.1)
        assert rig.out() == [("close", 3), ("final", "Okay, sure.")], (
            f"{rig.out()}: the judged segment had ended without a final; these words are "
            "a new utterance to a silent bot")


async def test_held_segments_are_judged_together_and_the_open_one_closes_on_its_final():
    judge = _Judge({"Okay": CONTINUE, "Okay. so": CONTINUE}, delay=0.1)
    async with GateRig(judge, wait=0.6) as rig:
        await rig.task.queue_frame(interim("Okay"))
        await asyncio.sleep(0.03)
        await rig.task.queue_frames([final("Okay.", stop_n=3), interim("so")])
        await asyncio.sleep(0.3)
        assert judge.asked == ["Okay", "Okay. so"], (
            f"{judge.asked}: asked again on every held word, not the newest segment's")
        await rig.task.queue_frame(final("So.", stop_n=4))
        await asyncio.sleep(0.1)
        assert judge.asked == ["Okay", "Okay. so"], "the open segment's final: no third ask"
        assert rig.out() == [("close", 3), ("close", 4)], rig.out()


async def test_a_backchannels_final_after_the_reply_ended_is_still_its_close():
    judge = _Judge({"Okay sure": CONTINUE})
    async with GateRig(judge) as rig:
        await rig.task.queue_frame(interim("Okay sure"))
        await asyncio.sleep(0.15)
        await rig.strategy.process_frame(BotStoppedSpeakingFrame())
        await rig.task.queue_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await rig.task.queue_frame(final("Okay, sure."))
        await asyncio.sleep(0.1)
        assert rig.out() == [("close", 3)], (
            f"{rig.out()}: the reply ending must not turn the backchannel into a turn")


async def test_words_of_an_open_user_turn_are_not_judged():
    judge = _Judge({})
    async with GateRig(judge) as rig:
        await rig.task.queue_frame(UserStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        await rig.task.queue_frame(final("Okay."))
        await asyncio.sleep(0.05)
        assert rig.out() == [("final", "Okay.")] and judge.asked == []


async def test_no_verdict_in_time_lets_the_words_on():
    judge = _Judge({"Mm-hmm": CONTINUE}, delay=WAIT_S + 0.3)
    async with GateRig(judge) as rig:
        await rig.task.queue_frame(final("Mm-hmm"))
        await asyncio.sleep(WAIT_S + 0.15)
        assert rig.out() == [("final", "Mm-hmm")], (
            "a slow judge must fall back to the rule as it was, not stall the pause")
        assert rig.gate.verdicts[None] == 1


async def test_a_failing_judge_lets_the_words_on():
    async def broken(_said):
        raise RuntimeError("403 key limit")

    async with GateRig(broken) as rig:
        await rig.task.queue_frame(final("Right."))
        await asyncio.sleep(0.1)
        assert rig.out() == [("final", "Right.")], rig.out()


async def test_stop_words_and_words_over_an_unpaused_bot_are_not_judged():
    judge = _Judge({})
    async with GateRig(judge) as rig:
        await rig.task.queue_frame(final("Stop."))
        await asyncio.sleep(0.05)
        assert rig.out() == [("final", "Stop.")], "a stop word must not wait on the judge"
    async with GateRig(judge, paused=False) as rig:
        await rig.task.queue_frame(final("Okay."))
        await asyncio.sleep(0.05)
        assert rig.out() == [("final", "Okay.")]
    assert judge.asked == []


def _starts(strategy):
    """Whether the start strategy opens the turn on a frame: (feed, started list)."""
    started = []

    async def on_started(_s, *a, **k):
        started.append(True)
    strategy.add_event_handler("on_user_turn_started", on_started)

    async def feed(frame):
        started.clear()
        await strategy.process_frame(frame)
        return bool(started)
    return feed


def _released(rig):
    return [f for _, d, f in rig.bottom.frames
            if d == FrameDirection.DOWNSTREAM and isinstance(f, TranscriptionFrame)]


async def test_a_single_word_is_judged_and_interrupts_on_its_verdict():
    # Under the word guard (2 words) a lone "What?" over the paused reply used to start
    # no turn, and the reply resumed over the question. The judge decides it now.
    judge = _Judge({"What?": ANSWER})
    async with GateRig(judge, min_words=2) as rig:
        feed = _starts(rig.strategy)
        assert not await feed(final("What?")), "unjudged, one word stays under the guard"
        await rig.task.queue_frame(final("What?"))
        await asyncio.sleep(0.2)
        assert judge.asked == ["What?"], judge.asked
        assert rig.out() == [("final", "What?")], rig.out()
        assert await feed(_released(rig)[-1]), (
            "the judge said it needs an answer: it must start the turn at one word")


async def test_a_verdict_on_fewer_words_than_held_does_not_mark_them():
    # The judge is asked on "What"; the words grow to "What a great idea" while it
    # decides. Its ANSWER speaks for "What" alone: the grown words go on unmarked, for the
    # word count to decide (4 >= 3 here: the turn starts as it did before single words
    # were judged) -- not as an ANSWER that would start it at any length.
    judge = _Judge({"What": ANSWER}, delay=0.15)
    async with GateRig(judge, min_words=3) as rig:
        await rig.task.queue_frame(interim("What"))
        await asyncio.sleep(0.05)
        await rig.task.queue_frame(interim("What a"))
        await asyncio.sleep(0.25)
        assert judge.asked == ["What"], judge.asked
        let_on = [f for _, d, f in rig.bottom.frames
                  if d == FrameDirection.DOWNSTREAM and isinstance(f, InterimTranscriptionFrame)]
        assert [f.text for f in let_on] == ["What", "What a"], let_on
        feed = _starts(rig.strategy)
        assert not await feed(let_on[-1]), (
            "a verdict on 'What' must not start the turn on 'What a' at two words")


async def test_a_marked_verdict_starts_the_turn_even_after_the_pause_ended():
    # The pause's cap resumes the reply at the moment the verdict lands: the strategy
    # has seen the resume before the marked words reach it. The verdict still holds.
    judge = _Judge({"What?": ANSWER})
    async with GateRig(judge, min_words=2) as rig:
        await rig.task.queue_frame(final("What?"))
        await asyncio.sleep(0.2)
        await rig.strategy.process_frame(PlayoutPauseFrame(paused=False))
        feed = _starts(rig.strategy)
        assert await feed(_released(rig)[-1])


async def _cap(rig):
    """The pause's cap: the transport says the resume both ways -- the strategy below
    the gate sees it first, then the gate."""
    await rig.strategy.process_frame(PlayoutPauseFrame(paused=False))
    await rig.task.queue_frame(PlayoutPauseFrame(paused=False), FrameDirection.UPSTREAM)


async def test_a_verdict_slower_than_the_cap_still_takes_the_turn():
    # The cap resumes the reply while the judge is still deciding a lone "What?" (the
    # judge's p50 on the box is 0.2-0.3 s). Let on at the cap, the word would go to the
    # word count and be dropped; it waits for the verdict, and the ANSWER takes the turn.
    judge = _Judge({"What": ANSWER}, delay=0.3)
    async with GateRig(judge, min_words=2, wait=1.2) as rig:
        await rig.task.queue_frame(interim("What"))
        await asyncio.sleep(0.1)
        await _cap(rig)
        await asyncio.sleep(0.05)
        await rig.task.queue_frame(final("What"))    # its final, after the cap
        await asyncio.sleep(0.05)
        assert rig.out() == [], "held past the cap until the verdict"
        await asyncio.sleep(0.3)
        assert judge.asked == ["What"] and rig.gate.verdicts[ANSWER] == 1
        feed = _starts(rig.strategy)
        assert await feed(_released(rig)[-1]), "the ANSWER must take the turn"


async def test_past_the_cap_a_judge_that_never_answers_lets_the_words_on_in_its_time():
    judge = _Judge({"What": ANSWER}, delay=5.0)
    async with GateRig(judge, min_words=2, wait=WAIT_S) as rig:
        await rig.task.queue_frame(final("What"))
        await asyncio.sleep(0.05)
        await _cap(rig)
        await asyncio.sleep(WAIT_S + 0.15)
        assert rig.out() == [("final", "What")], rig.out()
        assert rig.gate.verdicts[None] == 1
        feed = _starts(rig.strategy)
        assert not await feed(_released(rig)[-1]), "no verdict: the word count decides"


async def test_a_cap_with_no_verdict_awaited_lets_the_words_on_at_once():
    # A CONTINUE already given: nothing is awaited, so the cap lets on what is held as
    # it always did (here nothing: the CONTINUE dropped it).
    judge = _Judge({"Okay": CONTINUE})
    async with GateRig(judge, min_words=2) as rig:
        await rig.task.queue_frame(interim("Okay"))
        await asyncio.sleep(0.15)
        await _cap(rig)
        await asyncio.sleep(0.05)
        assert not rig.gate._past_pause and rig.gate._held == []


async def test_a_single_backchannel_is_judged_and_the_reply_carries_on():
    judge = _Judge({"Yeah.": CONTINUE})
    async with GateRig(judge, min_words=2) as rig:
        await rig.task.queue_frame(final("Yeah."))
        await asyncio.sleep(0.2)
        assert judge.asked == ["Yeah."]
        assert rig.out() == [("close", 3)], rig.out()


async def test_a_single_word_with_no_verdict_keeps_the_word_count():
    # A slow judge falls back to the rule as it was: one word under the guard is no
    # turn, so a "yeah" never becomes a cut because the judge was late.
    judge = _Judge({"Yeah.": ANSWER}, delay=WAIT_S + 0.3)
    async with GateRig(judge, min_words=2) as rig:
        feed = _starts(rig.strategy)
        await rig.task.queue_frame(final("Yeah."))
        await asyncio.sleep(WAIT_S + 0.15)
        assert rig.out() == [("final", "Yeah.")], rig.out()
        assert not await feed(_released(rig)[-1]), "no verdict: the word count decides"


async def test_an_interruption_while_judging_lets_the_words_on():
    judge = _Judge({"Okay": CONTINUE}, delay=0.2)
    async with GateRig(judge) as rig:
        await rig.task.queue_frame(final("Okay"))
        await asyncio.sleep(0.03)
        await rig.task.queue_frame(InterruptionFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.3)
        assert rig.out() == [("final", "Okay")], (
            "the turn opened some other way: the words belong in it")


async def test_the_pauser_holds_its_resume_while_judging():
    out, p, task, run = await _pauser_rig()
    try:
        from pipecat.frames.frames import BotStartedSpeakingFrame as BS, TTSAudioRawFrame
        await p.process_frame(BS(), FrameDirection.UPSTREAM)
        await p.process_frame(TTSAudioRawFrame(b"\0\0" * 160, 16000, 1, context_id="r"),
                              FrameDirection.DOWNSTREAM)
        await p.on_onset(True)
        assert p.paused
        p.hold_resume(True)
        await asyncio.sleep(0.3)
        await p.on_onset(False)
        await asyncio.sleep(RESUME_S + 0.2)
        assert p.paused, "the quiet window must not resume while the words are judged"
        p.hold_resume(False)
        await asyncio.sleep(RESUME_S + 0.1)
        assert not p.paused, "carried on: the quiet line resumes on the usual window"
    finally:
        await task.cancel()
        await run


async def test_the_ledger_reads_the_paused_reply_and_what_was_heard():
    async with Rig() as rig:
        await rig.speak()
        await _at(rig, 0.65)
        assert rig.ledger.paused_reply() is None, "nothing is paused"
        await rig.pauser.on_onset(True)
        await asyncio.sleep(0.05)
        reply, heard = rig.ledger.paused_reply()
        assert reply == " ".join(WORDS)
        n = len(heard.split())
        assert heard.split() == WORDS[:n] and 2 <= n <= 4, heard
        await rig.task.queue_frame(InterruptionFrame())


def test_the_judges_request_and_its_verdicts():
    m = judge_messages("Count to ten.", "One, two, three, four.", "One, two,", "Mm-hmm.")
    assert m[0]["content"] == barge_pause.JUDGE_PROMPT
    assert ('caller had asked: "Count to ten."' in m[1]["content"]
            and 'was answering it: "One, two, three, four."' in m[1]["content"]
            and 'heard up to: "One, two,"' in m[1]["content"]
            and 'said: "Mm-hmm."' in m[1]["content"]), m[1]["content"]
    long = judge_messages("", "x", " ".join(f"w{i}" for i in range(20)), "Yeah.")
    assert 'heard up to: "…w8 w9' in long[1]["content"], "only the last 12 heard words"
    assert judge_verdict(" CONTINUE") == CONTINUE and judge_verdict("stop.") == STOP
    assert judge_verdict("ANSWER") == ANSWER and judge_verdict("maybe") is None


async def test_the_session_builds_the_gate_above_the_aggregator_only_when_asked():
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
    from teaport_brain.agent_session import build_agent_session
    from teaport_brain.sip_transport import SipGatewayTransport, make_sip_params
    from test_sip_output_framing import _Wire
    import test_service_unusable  # noqa: F401 -- the LLM env a session needs to build

    def above_aggregator(session):
        p = session.tts
        while not isinstance(p, LLMUserAggregator):
            p = p.previous
        return p.previous, p._params.user_turn_strategies.start[0]

    saved = barge_pause.JUDGE
    try:
        for on in (False, True):
            barge_pause.JUDGE = on
            wire = _Wire()
            session = build_agent_session(
                SipGatewayTransport(wire.connection, make_sip_params()))
            gate, start = above_aggregator(session)
            if on:
                assert isinstance(gate, BargeInJudgeGate), gate
                assert gate._strategy is start, "it must ask the session's own strategy"
            else:
                assert not isinstance(gate, BargeInJudgeGate), gate
            wire.connection.close()
            wire.gw_sock.close()
    finally:
        barge_pause.JUDGE = saved


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        if not asyncio.iscoroutinefunction(fn):
            fn()
            print(f"  ok {fn.__name__}")

    async def run_aio():
        for fn in tests:
            if asyncio.iscoroutinefunction(fn):
                await fn()
                print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
