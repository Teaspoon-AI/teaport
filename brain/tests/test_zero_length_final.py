#
# A final nobody was heard saying is the committed turn's, not a new turn (#22, shape 2).
#
# The failure, from the 2026-09-04 runbook: after a committed turn the STT returns a
# trailing final with no VAD span -- 'Okay.' [23.3-23.3], 'Fake.' [11.0-11.0] (the "fig"
# of "fiddle fig") -- and it becomes a new user turn. The bot is not speaking yet, so one
# word is enough (MinWordsUserTurnStartStrategy), the turn start broadcasts the
# interruption, and the reply in flight is cut before a word of it plays (LEDGER "CUT
# heard~0%", "cut turn left no committed message"). The model is asked again, a second
# or so later, on the question plus the fragment.
#
# Driven here through the REAL pieces: TeaportSTTService (its engine messages fed in, its
# commits recorded off the wire), the user aggregator with the brain's start and stop
# strategies, keep_barge_in_reachable and CutReplyFence as agent_session wires them, the
# corrector, the TranscriptLedger, and -- for SIP -- the ReplyHoldGate with its TurnMerge.
# The LLM is a stand-in that keeps its reply in flight long enough for the tail to land.
# The stock start strategy is run as well, as the control: that is today's shape, on each
# wiring (Talk/room: the reply is cut at 0% and the context gets two user messages; SIP:
# the gate drops the unplayed reply and merges, and the model is asked twice). If pipecat
# ever stops starting a turn on such words, the control goes red and the rule can go.
#
# The rule is barge_pause.py's WORDS NOBODY SPOKE (PauseAwareMinWordsStrategy); the cases
# that must NOT be taken as a tail -- a VAD start or an onset since, the final of speech
# whose VAD stop the turn ended on, the bot audible (a stop word cuts it), the reply
# played, no turn committed yet, a turn whose words were taken -- are pinned below on the
# real controller.
#
# All utterances are synthetic.
#
# Run: python test_zero_length_final.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.audio.turn.smart_turn.base_smart_turn import (  # noqa: E402
    BaseSmartTurn,
    SmartTurnParams,
)
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
)
from pipecat.processors.frame_processor import FrameProcessor  # noqa: E402
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop.base_user_turn_stop_strategy import (  # noqa: E402
    BaseUserTurnStopStrategy,
)
from pipecat.turns.user_turn_controller import UserTurnController  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from stt_harness import WireRecorder  # noqa: E402
from turn_harness import running_controller  # noqa: E402

from teaport_brain.agent_session import user_aggregator_params  # noqa: E402
from teaport_brain.barge_pause import PauseAwareMinWordsStrategy  # noqa: E402
from teaport_brain.echo_guard import EchoGuard  # noqa: E402
from teaport_brain.endpointing import (  # noqa: E402
    NO_STOP,
    CutReplyFence,
    LateStartTurnStopStrategy,
    SegmentDoneFrame,
    SegmentDoneSink,
    keep_barge_in_reachable,
)
from teaport_brain.followup_gate import SYSTEM_NOTICE_TAG  # noqa: E402
from teaport_brain.heard_context import (  # noqa: E402
    HeardContextCorrector,
    append_to_last_turn,
)
from teaport_brain.reply_hold import ReplyHoldGate, TurnMerge  # noqa: E402
from teaport_brain.stt import FinalTranscriptionFrame, TeaportSTTService  # noqa: E402
from teaport_brain.taken_turn import TakenTurnCloser, TakenWords, taken  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402
from teaport_brain.wake_gate import WakeGate  # noqa: E402

QUESTION = "What's a good rule for watering a fiddle?"
TAIL = "Fake."
ANSWER = "Water it when the top inch of soil is dry."

SR = 16000
CHUNK = b"\x00\x02" * int(SR * 0.02)       # 20 ms of caller audio
STARTUP_S = 0.7
# How long the stand-in LLM keeps its reply in flight: the tail lands inside it, as it
# did live (the reply was cut before a word of it played).
REPLY_S = 0.8


class Complete(BaseSmartTurn):
    """Smart Turn calling every stop COMPLETE: the turn commits at its own final."""

    def _predict_endpoint(self, audio_array):
        return {"prediction": 1, "probability": 0.98}


class WiredSTT(TeaportSTTService):
    """The real service in a real pipeline: real push_frame, real queues. Only the wire
    is recorded (installed after the doomed connect settles), and the commit reasons."""

    def __init__(self):
        super().__init__(url="ws://127.0.0.1:1/none")
        self.whys = []

    async def _send_commit(self, final=True, why="other"):
        await super()._send_commit(final=final, why=why)
        self.whys.append(why)       # after: the commit is on the wire and owed its done


class InFlightLLM(FrameProcessor):
    """Where the LLM sits: records each context it is asked on and answers it REPLY_S
    later -- the reply in flight. An interruption that reaches it first cuts that reply
    before a word of it was spoken: counted in `cuts`."""

    def __init__(self):
        super().__init__()
        self.contexts = []
        self.cuts = 0
        self._task = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            self.contexts.append([dict(m) for m in frame.context.get_messages()])
            self._task = self.create_task(self._answer())
            return
        if isinstance(frame, InterruptionFrame) and self._task is not None:
            self.cuts += 1
            await self.cancel_task(self._task)
            self._task = None
        await self.push_frame(frame, direction)

    async def _answer(self):
        await asyncio.sleep(REPLY_S)
        self._task = None
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(ANSWER))
        await self.push_frame(LLMFullResponseEndFrame())


class Session:
    """One session's turn path as agent_session builds it, from the STT to the assistant
    aggregator, minus the TTS and the audio I/O. `front_end`: "talk" (no reply hold),
    "room" (the same, with the room's wake gate and echo guard on the STT), or "sip"
    (the reply hold and its TurnMerge). `stock`: pipecat's MinWords start strategy, the
    control, in place of the brain's."""

    def __init__(self, front_end: str, stock: bool):
        self.context = LLMContext([{"role": "system", "content": "sys"}])
        self.stt = WiredSTT()
        if front_end == "room":
            self.stt.wake_gate = WakeGate(["hey teaport"], asleep=False)
            self.stt.echo_guard = EchoGuard()
        self.taken = TakenWords()
        if stock:
            self.start = MinWordsUserTurnStartStrategy(min_words=2)
        else:
            self.start = PauseAwareMinWordsStrategy(
                min_words=2,
                # As agent_session wires it.
                on_tail_words=lambda words: append_to_last_turn(self.context, words))
            self.start.taken_words = self.taken
        self.stop = LateStartTurnStopStrategy(
            turn_analyzer=Complete(sample_rate=SR, params=SmartTurnParams(stop_secs=1.0)))
        self.pair = LLMContextAggregatorPair(
            self.context,
            user_params=user_aggregator_params(
                user_turn_strategies=UserTurnStrategies(start=[self.start], stop=[self.stop]),
            ),
        )
        user = self.pair.user()
        keep_barge_in_reachable(user._user_turn_controller)
        self.fence = CutReplyFence()
        self.fence.hold_commits(user)
        self.fence.close_owed = self.stop.close_owed
        self.ledger = TranscriptLedger()
        self.merge = TurnMerge() if front_end == "sip" else None
        self.gate = ReplyHoldGate(merge=self.merge) if front_end == "sip" else None
        if self.gate is not None:
            self.fence.unplayed_reply = lambda: self.gate.armed
        self.corrector = HeardContextCorrector(self.ledger, self.context, merge=self.merge)
        self.llm = InFlightLLM()
        self.turns_started = 0

        @user.event_handler("on_user_turn_started")
        async def _started(_a, _s):
            self.turns_started += 1

        if self.gate is not None:
            @user.event_handler("on_user_turn_message_added")
            async def _arm(_a, _m):
                await self.gate.arm()

        self.task = PipelineTask(Pipeline([p for p in [
            self.stt, user, self.fence.head, SegmentDoneSink(), self.corrector,
            self.llm, self.gate, self.pair.assistant(), self.fence.tail,
        ] if p is not None]), observers=[self.ledger], cancel_on_idle_timeout=False)

    async def audio(self, secs):
        for _ in range(round(secs / 0.02)):
            await self.stt.queue_frame(InputAudioRawFrame(audio=CHUNK, sample_rate=SR,
                                                          num_channels=1))
            await asyncio.sleep(0.02)

    async def until(self, pred, secs=3.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + secs
        while loop.time() < deadline and not pred():
            await self.audio(0.02)
        return pred()

    async def question(self):
        """The caller's question: VAD start, speech, its interim, the VAD stop; Smart
        Turn's COMPLETE crosses up to the STT, which commits; the engine answers the
        commit with the final, and the turn is committed on it."""
        await self.stt.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await self.audio(0.3)
        await self.stt._handle_message({"type": "transcription.delta", "delta": QUESTION})
        await self.audio(0.3)
        await self.stt.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        assert await self.until(lambda: self.stt.whys), "the verdict never reached the STT"
        assert self.stt.whys == ["verdict"], self.stt.whys
        await self.stt._handle_message({"type": "transcription.done", "text": QUESTION,
                                        "reason": "commit"})
        assert await self.until(lambda: self.llm.contexts), "the question was never asked"

    async def tail(self):
        """The engine's trailing words: no VAD start, no VAD stop -- an interim, then the
        engine closing the segment on its own ("reason": "vad"), so the final answers no
        stop of ours (NO_STOP)."""
        await self.audio(0.1)
        await self.stt._handle_message({"type": "transcription.delta", "delta": TAIL})
        await self.audio(0.1)
        await self.stt._handle_message({"type": "transcription.done", "text": TAIL,
                                        "reason": "vad"})


async def _the_call(front_end: str, stock: bool) -> Session:
    s = Session(front_end, stock)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(s.task))
    await asyncio.sleep(STARTUP_S)
    s.stt._websocket = WireRecorder()
    try:
        await s.question()
        await s.tail()
        # The reply's REPLY_S, the stop strategy's no-VAD fallback (~0.3 s) and a second
        # request's own REPLY_S, with room to spare.
        await s.until(lambda: False, secs=2 * REPLY_S + 1.0)
    finally:
        s.stt._websocket = None
        await s.task.cancel()
        await run
    return s


def _roles(s):
    return [(m["role"], m.get("content")) for m in s.context.get_messages()]


def _the_tail_is_zero_length(s):
    """The ledger charts the tail as the issue's logs did: a line with no span."""
    lines = [u for u in s.ledger.events if u.speaker == "user"]
    assert [u.text for u in lines] == [QUESTION, TAIL], lines
    assert lines[1].t_start == lines[1].t_end, (
        f"the tail must be a zero-length line, as live: {lines[1]!r}")


def _cut_and_asked_again(s):
    assert len(s.llm.contexts) == 2 and s.llm.cuts == 1, (
        f"expected today's shape: the reply cut and the model asked again "
        f"(asked {len(s.llm.contexts)}x, cut {s.llm.cuts}x)")
    assert s.turns_started == 2, s.turns_started


async def test_the_stock_strategy_cuts_the_reply_on_a_final_nobody_spoke_talk():
    """The control, Talk (and the room: no reply hold): today's shape."""
    s = await _the_call("talk", stock=True)
    _the_tail_is_zero_length(s)
    _cut_and_asked_again(s)
    assert _roles(s) == [("system", "sys"), ("user", QUESTION), ("user", TAIL),
                         ("assistant", ANSWER)], _roles(s)


async def test_the_stock_strategy_cuts_the_reply_on_a_final_nobody_spoke_sip():
    """The control, SIP: the gate drops the unplayed reply and the corrector merges --
    one message, but the reply is cut and the model asked a second time."""
    s = await _the_call("sip", stock=True)
    _the_tail_is_zero_length(s)
    _cut_and_asked_again(s)
    assert s.gate.dropped == 0 and s.gate.holds == 0, "no speech: the gate never held"
    assert _roles(s) == [("system", "sys"), ("user", f"{QUESTION} {TAIL}"),
                         ("assistant", ANSWER)], _roles(s)


async def _the_tail_joins_the_turn(front_end):
    s = await _the_call(front_end, stock=False)
    _the_tail_is_zero_length(s)
    assert s.llm.cuts == 0, "the tail cut the reply in flight"
    assert len(s.llm.contexts) == 1, (
        f"the model was asked {len(s.llm.contexts)} times: the tail became a turn")
    assert s.turns_started == 1, s.turns_started
    # The reply asked on the question plays, and the record holds what the caller said.
    assert _roles(s) == [("system", "sys"), ("user", f"{QUESTION} {TAIL}"),
                         ("assistant", ANSWER)], _roles(s)
    assert s.taken.why(TAIL) is not None, "the tail's words are not on record"
    if s.merge is not None:
        assert not s.merge.pending, "no reply was dropped: nothing to merge"
    return s


async def test_a_final_nobody_spoke_joins_the_committed_turn_talk():
    await _the_tail_joins_the_turn("talk")


async def test_a_final_nobody_spoke_joins_the_committed_turn_room():
    await _the_tail_joins_the_turn("room")


async def test_a_final_nobody_spoke_joins_the_committed_turn_sip():
    await _the_tail_joins_the_turn("sip")


# ---- the rule's edges, on the real controller -----------------------------------------

class Rig:
    """The real UserTurnController with the brain's start strategy and the brain's stop
    strategy (Smart Turn: COMPLETE), fed frames as the aggregator would."""

    def __init__(self, *, wired=True, stop=None):
        self.appended = []
        self.taken = TakenWords()
        self.start = PauseAwareMinWordsStrategy(
            min_words=2,
            on_tail_words=(lambda words: self.appended.append(words) or True)
            if wired else None)
        self.start.taken_words = self.taken
        self.stop = stop or LateStartTurnStopStrategy(
            turn_analyzer=Complete(sample_rate=SR, params=SmartTurnParams(stop_secs=1.0)))
        # The closer first, as agent_session lists it: a taken final ends its turn empty.
        self.controller = UserTurnController(
            user_turn_strategies=UserTurnStrategies(start=[self.start],
                                                    stop=[TakenTurnCloser(), self.stop]),
            user_turn_stop_timeout=3600)
        self.events = []
        self.resets = 0

        @self.controller.event_handler("on_user_turn_started")
        async def _s(_c, _st, _p):
            self.events.append("start")

        @self.controller.event_handler("on_user_turn_stopped")
        async def _e(_c, _st, _p):
            self.events.append("stop")

        @self.controller.event_handler("on_reset_aggregation")
        async def _r(_c, _st):
            self.resets += 1

        async def _ignore(*_a, **_k):
            pass
        for name in ("on_push_frame", "on_broadcast_frame", "on_user_turn_inference_triggered"):
            self.controller.add_event_handler(name, _ignore)

    async def feed(self, frame):
        await self.controller.process_frame(frame)

    async def audio(self, secs):
        for _ in range(round(secs / 0.02)):
            await self.feed(InputAudioRawFrame(audio=CHUNK, sample_rate=SR, num_channels=1))

    async def committed_turn(self, stop_n=1, text=QUESTION):
        """VAD start, speech, an interim (the turn opens), VAD stop (COMPLETE), and the
        final answering that stop: the turn commits."""
        await self.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await self.audio(0.4)
        await self.feed(InterimTranscriptionFrame(text, "u", "t"))
        await self.feed(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await self.feed(final(text, stop_n))
        assert self.events[-2:] == ["start", "stop"], self.events

    def starts(self):
        return self.events.count("start")


def final(text, stop_n):
    f = FinalTranscriptionFrame(text, "u", "t", stop_n=stop_n)
    f.finalized = True
    return f


async def test_a_tail_interim_and_final_start_no_turn_and_reach_the_turn_before():
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(InterimTranscriptionFrame(TAIL, "u", "t"))
        await rig.feed(final(TAIL, NO_STOP))
        assert rig.starts() == 1, rig.events
        assert rig.appended == [TAIL], rig.appended
        assert rig.resets >= 1, "the tail's words must leave the aggregation"


async def test_without_a_place_for_them_the_tail_words_are_dropped_not_a_turn():
    rig = Rig(wired=False)
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(final(TAIL, NO_STOP))
        assert rig.starts() == 1, rig.events
        assert rig.taken.why(TAIL) is not None


async def test_words_after_a_vad_start_are_a_turn():
    """A real new utterance: its VAD start arrived. It interrupts as it always did."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.feed(InterimTranscriptionFrame("Okay", "u", "t"))
        assert rig.starts() == 2, rig.events
        assert rig.appended == []


async def test_words_after_an_onset_are_a_turn():
    """The onset test heard speech the VAD's start did not (a short word after a
    pause): the caller spoke after the commit, and the reply hold treats it so."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.start.on_onset(True)
        await rig.start.on_onset(False)
        await rig.feed(final("Fig.", NO_STOP))
        assert rig.starts() == 2, rig.events
        assert rig.appended == []


async def test_an_onset_between_the_stop_and_its_final_still_counts():
    """The short word's onset can come after the committed utterance's VAD stop but
    before that utterance's final lands: it is still speech the final did not carry."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(0.4)
        await rig.feed(InterimTranscriptionFrame(QUESTION, "u", "t"))
        await rig.feed(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await rig.start.on_onset(True)
        await rig.start.on_onset(False)
        await rig.feed(final(QUESTION, 1))
        assert rig.events == ["start", "stop"], rig.events
        await rig.feed(InterimTranscriptionFrame("Fig", "u", "t"))
        assert rig.starts() == 2, rig.events


async def test_a_final_answering_a_vad_stop_after_the_commit_is_a_turn():
    """keep_barge_in_reachable's re-applied stop ends the turn at the resumed speech's
    VAD stop, before that speech's final lands: the final answers a VAD stop of its
    own, and starts its turn (on SIP its interruption is what merges it)."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(0.3)
        await rig.feed(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await rig.feed(final("in Austin on Thursday", 2))
        assert rig.starts() == 2, rig.events
        assert rig.appended == []


class CommitNow(BaseUserTurnStopStrategy):
    """A stop strategy that concludes when told to, whatever the VAD says."""

    async def commit(self):
        await self.trigger_user_turn_stopped()


async def test_a_final_while_the_caller_talks_on_leaves_the_rest_of_their_words_a_turn():
    """The strategy concludes on the caller's first words while the VAD still hears them
    (test_post_tool_under_speech's shape, with another STT's unstamped finals): the turn
    commits, its finalize is refused and re-applied at their VAD stop, and the rest of
    what they said arrives after that -- speech heard, and a turn of its own."""
    stop = CommitNow()
    rig = Rig(stop=stop)
    keep_barge_in_reachable(rig.controller)
    async with running_controller(rig.controller):
        await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.feed(InterimTranscriptionFrame(QUESTION, "u", "t"))
        await rig.feed(final(QUESTION, None))
        await stop.commit()                       # refused: the VAD still hears them
        assert rig.events == ["start"], rig.events
        await rig.feed(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        assert rig.events == ["start", "stop"], "the refused finalize was not re-applied"
        await rig.feed(InterimTranscriptionFrame("what about the metro area", "u", "t"))
        assert rig.starts() == 2 and rig.appended == [], (
            f"the rest of the caller's words were taken as a tail: {rig.events}")


async def test_a_wordless_close_does_not_make_the_speech_it_closed_a_tail():
    """Slice mode's empty provisional: the speech's VAD stop closes wordless and its
    words come after, as new words stamped NO_STOP. They had a VAD span: a turn."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(0.3)
        await rig.feed(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await rig.feed(SegmentDoneFrame(stop_n=2))
        await rig.feed(final("Thursday", NO_STOP))
        assert rig.starts() == 2, rig.events


async def test_the_committed_turns_own_engine_close_clears_its_speech():
    """The common order on the appliance: the engine closes the segment on its own
    before the VAD stop (NO_STOP, with the words), and the stop's commit returns the
    wordless tail. The turn's speech is answered by then: a tail after it is a tail."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(0.4)
        await rig.feed(InterimTranscriptionFrame(QUESTION, "u", "t"))
        await rig.feed(final(QUESTION, NO_STOP))
        await rig.feed(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await rig.feed(SegmentDoneFrame(stop_n=1))
        assert rig.events == ["start", "stop"], rig.events
        await rig.feed(final(TAIL, NO_STOP))
        assert rig.starts() == 1 and rig.appended == [TAIL], (rig.events, rig.appended)


async def test_once_the_reply_plays_a_word_with_no_speech_under_it_is_counted_as_ever():
    """The bot audible: the barge-in rules stand (two words, or a stop word); after the
    reply, one word is a turn. Nothing is a tail outside the reply's wait."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(BotStartedSpeakingFrame())
        await rig.feed(final("Right.", NO_STOP))
        assert rig.starts() == 1, "one word over the bot is under the guard, as ever"
        await rig.feed(final("Stop.", NO_STOP))
        assert rig.starts() == 2, "a lone stop word over the bot must cut it (#216)"
        assert rig.appended == []
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(BotStartedSpeakingFrame())
        await rig.feed(BotStoppedSpeakingFrame())
        await rig.feed(final("Yes.", NO_STOP))
        assert rig.starts() == 2, rig.events
        assert rig.appended == []


async def test_a_tail_segment_begun_in_the_wait_stays_a_tail_once_the_reply_plays():
    """The tail's interim lands in the wait, its final (the backstop's, 1.5 s later)
    after the reply started: still the committed turn's words, not a barge-in -- unless
    it is a stop word, which over the bot always cuts."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(InterimTranscriptionFrame("Okay wait", "u", "t"))
        await rig.feed(BotStartedSpeakingFrame())
        await rig.feed(final("Okay, wait.", 1))
        assert rig.starts() == 1 and rig.appended == ["Okay, wait."], (rig.events,
                                                                       rig.appended)
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(InterimTranscriptionFrame("Stop", "u", "t"))
        await rig.feed(BotStartedSpeakingFrame())
        await rig.feed(final("Stop.", 1))
        assert rig.starts() == 2, "a stop word over the bot must cut it (#216)"


async def test_a_reply_that_never_plays_holds_the_wait_only_so_long():
    """A turn whose reply never became audible (an empty completion, tool calls only):
    words with no speech under them are a tail inside _TAIL_WAIT_S of the commit, and a
    turn of their own after it -- not folded into a message already answered."""
    from teaport_brain.barge_pause import _TAIL_WAIT_S
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        rig.start._due_at -= _TAIL_WAIT_S - 1          # the reply due 9 s ago
        await rig.feed(final(TAIL, NO_STOP))
        assert rig.starts() == 1 and rig.appended == [TAIL], (rig.events, rig.appended)
        rig.start._due_at -= 2                         # and now 11 s ago
        await rig.feed(final("Hello?", NO_STOP))
        assert rig.starts() == 2 and rig.appended == [TAIL], (rig.events, rig.appended)


async def test_before_any_turn_is_committed_words_start_one():
    """Nothing is in flight to cut and no turn to hand the words to: the session's first
    words start a turn however they came (test_final_survives_turn_start's shape)."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.feed(final(QUESTION, NO_STOP))
        assert rig.starts() == 1, rig.events


async def test_a_turn_whose_words_were_taken_leaves_nothing_in_flight():
    """A turn that committed no words -- Wi-Fi setup or run control took its final, and
    the closer dropped the words of its earlier segments with it -- asks the model for
    no reply: words after it are a turn as before, and nothing is appended to the turn
    before it."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(0.4)
        await rig.feed(InterimTranscriptionFrame("hey can you", "u", "t"))
        await rig.feed(final("Hey, can you", NO_STOP))        # the engine's own cut
        await rig.feed(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await rig.feed(taken(final("set up the Wi-Fi", 1)))
        assert rig.events == ["start", "stop"], rig.events
        await rig.feed(final("Yes.", NO_STOP))
        assert rig.starts() == 2, rig.events
        assert rig.appended == []


async def test_the_tail_is_appended_to_the_callers_own_message_in_place():
    """Into the caller's newest message, past an injected order (a user-role message
    that is not theirs) and a tool round; nothing added, removed or moved."""
    order = f"{SYSTEM_NOTICE_TAG} tell the user the consult is back"
    ctx = LLMContext([{"role": "system", "content": "sys"},
                      {"role": "user", "content": QUESTION},
                      {"role": "assistant", "content": "", "tool_calls": [
                          {"id": "c1", "type": "function",
                           "function": {"name": "f", "arguments": "{}"}}]},
                      {"role": "tool", "tool_call_id": "c1", "content": "ok"},
                      {"role": "user", "content": order}])
    before = [id(m) for m in ctx.get_messages()]
    assert append_to_last_turn(ctx, TAIL)
    assert [id(m) for m in ctx.get_messages()] == before
    assert ctx.get_messages()[1]["content"] == f"{QUESTION} {TAIL}"
    assert ctx.get_messages()[4]["content"] == order
    assert not append_to_last_turn(ctx, "  ")
    assert not append_to_last_turn(LLMContext([{"role": "system", "content": "s"}]), TAIL)


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
