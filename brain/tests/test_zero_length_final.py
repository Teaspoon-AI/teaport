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
# corrector, the TranscriptLedger, UnspokenReplyTap, and -- for SIP -- the ReplyHoldGate
# with its TurnMerge; where a test turns on what the TTS does with a reply, the real
# EngineTTSService too. The LLM is a stand-in that keeps its reply in flight until the
# test lets it go, once the tail has been decided -- no race against a timer. The stock start
# strategy is run as well, as the control: that is today's shape, on each wiring
# (Talk/room: the reply is cut at 0% and the context gets two user messages; SIP: the
# gate drops the unplayed reply and merges, and the model is asked twice). If pipecat
# ever stops starting a turn on such words, the control goes red and the rule can go.
#
# The rule is barge_pause.py's WORDS NOBODY SPOKE (PauseAwareMinWordsStrategy); the cases
# that must NOT be taken as a tail -- a VAD start since, or an onset where the reply hold
# is, the final of speech whose VAD stop the turn ended on, any stop word among the
# words ("Okay, wait."), the bot audible
# (from then on every word is counted as one over the bot), the reply ended unspoken or
# overdue, no turn committed yet, a turn whose words were taken -- are pinned below on
# the real controller; words only the onset test heard are a tail on Talk and the room,
# which have no reply hold (the owner's call, 2026-11-06), on the controller and through
# the pipeline.
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
    DataFrame,
    FunctionCallFromLLM,
    FunctionCallInProgressFrame,
    BotStoppedSpeakingFrame,
    FunctionCallResultFrame,
    FunctionCallResultProperties,
    FunctionCallsStartedFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
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
from pipecat.tests.utils import run_test  # noqa: E402
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop.base_user_turn_stop_strategy import (  # noqa: E402
    BaseUserTurnStopStrategy,
)
from pipecat.turns.user_turn_controller import UserTurnController  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from stt_harness import WireRecorder  # noqa: E402
from test_tts_stop_frame import Probe, _fake_segments  # noqa: E402
from turn_harness import running_controller  # noqa: E402

from teaport_brain.agent_session import user_aggregator_params  # noqa: E402
from teaport_brain.barge_pause import (  # noqa: E402
    REPLY_WAIT_S,
    PauseAwareMinWordsStrategy,
    UnspokenReplyTap,
)
from teaport_brain.echo_guard import EchoGuard  # noqa: E402
from teaport_brain.engine_tts import EngineTTSService  # noqa: E402
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
from teaport_brain.services import FunctionCallsDispatchedFrame  # noqa: E402
from teaport_brain.taken_turn import TakenTurnCloser, TakenWords, taken  # noqa: E402
from teaport_brain.tools import no_inference  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402
from teaport_brain.wake_gate import WakeGate  # noqa: E402

QUESTION = "What's a good rule for watering a fiddle?"
TAIL = "Fake."
# A stop word among other words, said in the reply's wait with no speech heard for it:
# 'Okay, wait.' [35.3-35.3] is one of #22's own zero-length lines.
STOP_PHRASES = ("Okay, wait.", "No, wait.", "Wait, no.", "Okay, stop.")
ANSWER = "Water it when the top inch of soil is dry."

SR = 16000
CHUNK = b"\x00\x02" * int(SR * 0.02)       # 20 ms of caller audio
STARTUP_S = 0.7
# The longest a pipeline test waits for what it waits on: a bound on a stuck pipeline,
# not a window anything has to land in.
WAIT_S = 5.0


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
    """Where the LLM sits: records each context it is asked on and answers it once
    `release` is set -- the reply in flight until then, as long as the test needs. Each
    answer is the next of `answers` (ANSWER once they run out); an empty one is an empty
    200. With `audio` it stands for the TTS as well: words come with their audio, as
    below a TTS. An interruption that reaches it first cuts the reply before a word of
    it was spoken: counted in `cuts`."""

    def __init__(self, audio=True):
        super().__init__()
        self.audio = audio
        self.contexts = []
        self.cuts = 0
        self.answered = 0
        self.answers = []
        self.release = asyncio.Event()
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
        await self.release.wait()
        self._task = None
        words = self.answers.pop(0) if self.answers else ANSWER
        await self.push_frame(LLMFullResponseStartFrame())
        if words:
            await self.push_frame(LLMTextFrame(words))
            if self.audio:
                await self.push_frame(TTSAudioRawFrame(audio=b"\x00\x00" * 160,
                                                       sample_rate=SR, num_channels=1))
        await self.push_frame(LLMFullResponseEndFrame())
        self.answered += 1


class Session:
    """One session's turn path as agent_session builds it, from the STT to the assistant
    aggregator, minus the TTS and the audio I/O. `front_end`: "talk" (no reply hold),
    "room" (the same, with the room's wake gate and echo guard on the STT), or "sip"
    (the reply hold and its TurnMerge; the onset test's speech is speech heard).
    `stock`: pipecat's MinWords start strategy, the control, in place of the brain's.
    `tts`: the real EngineTTSService below the tap, as agent_session places it. `llm`:
    another stand-in in InFlightLLM's place (test_bare_stop's playing reply)."""

    def __init__(self, front_end: str, stock: bool, tts: bool = False, llm=None):
        self.context = LLMContext([{"role": "system", "content": "sys"}])
        self.stt = WiredSTT()
        if front_end == "room":
            self.stt.wake_gate = WakeGate(["hey teaport"], asleep=False)
            self.stt.echo_guard = EchoGuard()
        hold = front_end == "sip"
        self.taken = TakenWords()
        self.appended = []
        if stock:
            self.start = MinWordsUserTurnStartStrategy(min_words=2)
        else:
            def _tail_words(words):
                self.appended.append(words)
                return append_to_last_turn(self.context, words)

            # As agent_session wires it: the onset is speech only where the hold is.
            self.start = PauseAwareMinWordsStrategy(
                min_words=2, on_tail_words=_tail_words, onset_is_speech=hold)
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
        if not stock:
            # As agent_session wires it: a turn that cut a reply and is only a stop is
            # held at its commit (barge_pause.py A BARE STOP).
            self.fence.holds = self.start.hold_bare_stop
        self.ledger = TranscriptLedger()
        self.merge = TurnMerge() if hold else None
        self.gate = ReplyHoldGate(merge=self.merge) if hold else None
        if self.gate is not None:
            self.fence.unplayed_reply = lambda: self.gate.armed
        self.corrector = HeardContextCorrector(self.ledger, self.context, merge=self.merge)
        self.llm = llm or InFlightLLM(audio=not tts)
        # Directly above the TTS, as agent_session places it.
        self.tap = None if stock else UnspokenReplyTap(self.start.reply_unspoken)
        self.tts = None
        if tts:
            self.tts = EngineTTSService(voice="af_heart")
            self.tts._synth_text = _fake_segments       # test_pipecat_premises._tts
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
            self.llm, self.tap, self.tts, self.gate, self.pair.assistant(),
            self.fence.tail,
        ] if p is not None]), observers=[self.ledger], cancel_on_idle_timeout=False)

    async def audio(self, secs):
        for _ in range(round(secs / 0.02)):
            await self.stt.queue_frame(InputAudioRawFrame(audio=CHUNK, sample_rate=SR,
                                                          num_channels=1))
            await asyncio.sleep(0.02)

    async def until(self, pred, secs=WAIT_S):
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

    async def onset(self):
        """Speech the onset test heard and the VAD's start did not: told to the onset
        listeners agent_session registers (the start strategy, and the reply hold's gate
        where there is one), as the VAD analyzer tells them."""
        for started in (True, False):
            await self.start_onset(started)
            if self.gate is not None:
                await self.gate.on_onset(started)

    async def start_onset(self, started):
        if hasattr(self.start, "on_onset"):
            await self.start.on_onset(started)

    async def tail(self, text=TAIL):
        """The engine's trailing words: no VAD start, no VAD stop -- an interim, then the
        engine closing the segment on its own ("reason": "vad"), so the final answers no
        stop of ours (NO_STOP)."""
        await self.audio(0.1)
        await self.stt._handle_message({"type": "transcription.delta", "delta": text})
        await self.audio(0.1)
        await self.stt._handle_message({"type": "transcription.done", "text": text,
                                        "reason": "vad"})


async def _the_call(front_end: str, stock: bool, *, onset=False, text=TAIL) -> Session:
    """The question, then the tail `text` while its reply is in flight (after an onset,
    if `onset`). The reply goes out once the tail is decided: joined to the turn, or a
    turn that asked the model again."""
    s = Session(front_end, stock)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(s.task))
    await asyncio.sleep(STARTUP_S)
    s.stt._websocket = WireRecorder()
    try:
        await s.question()
        if onset:
            await s.onset()
        await s.tail(text)
        assert await s.until(lambda: s.appended or len(s.llm.contexts) >= 2), (
            "the tail was neither joined to the turn nor asked on")
        s.llm.release.set()
        assert await s.until(lambda: ("assistant", ANSWER) in _roles(s)), _roles(s)
        await s.audio(0.3)          # nothing else is on its way
    finally:
        s.stt._websocket = None
        await s.task.cancel()
        await run
    return s


def _roles(s):
    return [(m["role"], m.get("content")) for m in s.context.get_messages()]


def _the_tail_is_zero_length(s, text=TAIL):
    """The ledger charts the tail as the issue's logs did: a line with no span."""
    lines = [u for u in s.ledger.events if u.speaker == "user"]
    assert [u.text for u in lines] == [QUESTION, text], lines
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


async def _the_tail_joins_the_turn(front_end, *, onset=False):
    s = await _the_call(front_end, stock=False, onset=onset)
    _the_tail_is_zero_length(s)
    assert s.llm.cuts == 0, "the tail cut the reply in flight"
    assert len(s.llm.contexts) == 1, (
        f"the model was asked {len(s.llm.contexts)} times: the tail became a turn")
    assert s.turns_started == 1, s.turns_started
    # The reply asked on the question plays, and the record holds what the caller said.
    assert _roles(s) == [("system", "sys"), ("user", f"{QUESTION} {TAIL}"),
                         ("assistant", ANSWER)], _roles(s)
    assert s.appended == [TAIL], s.appended
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


async def test_words_only_the_onset_heard_join_the_committed_turn_talk():
    """The owner's call (2026-11-06): with no reply hold to hold and merge for them,
    words the onset test alone heard are a tail too -- not a turn that cuts the reply
    at 0% and leaves a second user message."""
    await _the_tail_joins_the_turn("talk", onset=True)


async def test_words_only_the_onset_heard_join_the_committed_turn_room():
    await _the_tail_joins_the_turn("room", onset=True)


async def test_words_only_the_onset_heard_still_start_a_turn_and_merge_on_sip():
    """SIP keeps its shape: the hold holds the reply for the onset, and the words start
    a turn whose interruption drops it unplayed and merges them into the question --
    one message, the model asked once more on the whole."""
    s = await _the_call("sip", stock=False, onset=True)
    _the_tail_is_zero_length(s)
    assert s.gate.holds == 1, "the onset must hold the reply, as the hold always has"
    assert s.turns_started == 2 and len(s.llm.contexts) == 2, (s.turns_started,
                                                               len(s.llm.contexts))
    assert s.appended == [], s.appended
    assert _roles(s) == [("system", "sys"), ("user", f"{QUESTION} {TAIL}"),
                         ("assistant", ANSWER)], _roles(s)


async def _a_stop_word_among_the_words_cuts_the_reply(front_end):
    """Each of STOP_PHRASES in the reply's wait, as a zero-length line: on SIP with no
    speech heard, on Talk and the room after an onset (no speech there). It starts the
    turn and cuts the reply in flight, as on main -- and, being only a stop, is held at
    its commit (barge_pause.py A BARE STOP): the model is not asked again, and the
    context still ends on the question. On SIP the reply hold dropped the reply
    unplayed, so the merge it asked for waits for the caller's next words."""
    for said in STOP_PHRASES:
        s = await _the_held_call(front_end, said, onset=front_end != "sip")
        _the_tail_is_zero_length(s, said)
        assert s.llm.cuts == 1 and len(s.llm.contexts) == 1, (
            said, s.llm.cuts, len(s.llm.contexts))
        assert s.turns_started == 2, (said, s.turns_started)
        assert s.appended == [], (said, s.appended)
        assert _roles(s) == [("system", "sys"), ("user", QUESTION)], (said, _roles(s))
        assert s.taken.why(said) is not None, "the held words are not on record"
        if s.merge is not None:
            assert s.merge.pending, "the dropped reply's merge must wait for the next words"


async def _the_held_call(front_end, said, *, onset=False) -> Session:
    """The question, then `said` (a tail-shaped line, after an onset if `onset`) while
    its reply is in flight; returns once that turn has ended, with the model asked on
    the question alone or on `said` as well."""
    s = Session(front_end, stock=False)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(s.task))
    await asyncio.sleep(STARTUP_S)
    s.stt._websocket = WireRecorder()
    try:
        await s.question()
        if onset:
            await s.onset()
        await s.tail(said)
        assert await s.until(lambda: s.start.deliberately_silent()
                             or len(s.llm.contexts) >= 2), (
            f"{said!r} was neither held nor asked on")
        await s.audio(0.3)          # nothing else is on its way
    finally:
        s.stt._websocket = None
        await s.task.cancel()
        await run
    return s


async def test_a_question_after_a_stop_word_in_the_wait_is_answered():
    """The control: a stop word with a question after it ("Wait, what time is it?") is
    not only a stop. It cuts the reply as on main, and the model is asked on it."""
    for front_end in ("talk", "sip"):
        said = "Wait, what time is it?"
        s = await _the_held_call(front_end, said, onset=front_end != "sip")
        _cut_and_asked_again(s)
        assert not s.start.deliberately_silent()
        users = [m["content"] for m in s.llm.contexts[1] if m["role"] == "user"]
        assert users == ([f"{QUESTION} {said}"] if front_end == "sip"
                         else [QUESTION, said]), (front_end, users)


async def test_a_stop_word_among_the_words_cuts_the_reply_sip():
    await _a_stop_word_among_the_words_cuts_the_reply("sip")


async def test_a_stop_word_among_the_words_cuts_the_reply_talk():
    await _a_stop_word_among_the_words_cuts_the_reply("talk")


async def test_a_stop_word_among_the_words_cuts_the_reply_room():
    await _a_stop_word_among_the_words_cuts_the_reply("room")


async def test_after_a_reply_that_ended_unspoken_words_nobody_spoke_are_a_turn_talk():
    """The reply to the question ends with nothing said (an empty 200): there is no
    reply to keep, and words joined to the turn would never be answered. A later
    'Hello?' only the onset test heard is a turn of its own, and the model is asked on
    it. Through the real TTS, which holds a text-less completion's End: the tap reads
    it above the TTS, where it passes."""
    s = Session("talk", stock=False, tts=True)
    s.llm.answers = ["", ANSWER]
    s.llm.release.set()
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(s.task))
    await asyncio.sleep(STARTUP_S)
    s.stt._websocket = WireRecorder()
    try:
        await s.question()
        assert await s.until(lambda: s.llm.answered == 1), "the empty answer never came"
        assert await s.until(lambda: not s.start._reply_due), (
            "the tap never told the strategy the reply ended unspoken")
        await s.onset()
        await s.tail("Hello?")
        assert await s.until(lambda: s.llm.answered == 2), "'Hello?' was never answered"
    finally:
        s.stt._websocket = None
        await s.task.cancel()
        await run
    assert s.appended == [] and s.turns_started == 2, (s.appended, s.turns_started)
    asked = [m.get("content") for m in s.llm.contexts[1] if m["role"] == "user"]
    assert asked == [QUESTION, "Hello?"], asked


# ---- the rule's edges, on the real controller -----------------------------------------

class Rig:
    """The real UserTurnController with the brain's start strategy and the brain's stop
    strategy (Smart Turn: COMPLETE), fed frames as the aggregator would.
    `onset_is_speech`: as on SIP (the default) or, False, on Talk and the room."""

    def __init__(self, *, wired=True, stop=None, onset_is_speech=True):
        self.appended = []
        self.taken = TakenWords()
        self.start = PauseAwareMinWordsStrategy(
            min_words=2,
            on_tail_words=(lambda words: self.appended.append(words) or True)
            if wired else None,
            onset_is_speech=onset_is_speech)
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

    async def onset(self):
        await self.start.on_onset(True)
        await self.start.on_onset(False)

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
    """A real new utterance: its VAD start arrived. It interrupts as it always did --
    with the reply hold or without it."""
    for onset_is_speech in (True, False):
        rig = Rig(onset_is_speech=onset_is_speech)
        async with running_controller(rig.controller):
            await rig.committed_turn()
            await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
            await rig.feed(InterimTranscriptionFrame("Okay", "u", "t"))
            assert rig.starts() == 2, rig.events
            assert rig.appended == []


async def test_words_after_an_onset_are_a_turn_where_the_reply_hold_is():
    """SIP: the onset test heard speech the VAD's start did not (a short word after a
    pause). The reply hold holds the reply for it and merges the turn its words start."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.onset()
        await rig.feed(final("Fig.", NO_STOP))
        assert rig.starts() == 2, rig.events
        assert rig.appended == []


async def test_an_onset_between_the_stop_and_its_final_still_counts():
    """SIP: the short word's onset can come after the committed utterance's VAD stop but
    before that utterance's final lands: it is still speech the final did not carry."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(0.4)
        await rig.feed(InterimTranscriptionFrame(QUESTION, "u", "t"))
        await rig.feed(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await rig.onset()
        await rig.feed(final(QUESTION, 1))
        assert rig.events == ["start", "stop"], rig.events
        await rig.feed(InterimTranscriptionFrame("Fig", "u", "t"))
        assert rig.starts() == 2, rig.events


async def test_without_the_reply_hold_words_only_the_onset_heard_are_a_tail():
    """Talk and the room (the owner's call, 2026-11-06): nothing holds or merges for an
    onset there, so its words are the committed turn's -- after the commit, or with the
    onset between the turn's VAD stop and its final."""
    rig = Rig(onset_is_speech=False)
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.onset()
        await rig.feed(InterimTranscriptionFrame("Fig", "u", "t"))
        await rig.feed(final("Fig.", NO_STOP))
        assert rig.starts() == 1 and rig.appended == ["Fig."], (rig.events, rig.appended)
    rig = Rig(onset_is_speech=False)
    async with running_controller(rig.controller):
        await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(0.4)
        await rig.feed(InterimTranscriptionFrame(QUESTION, "u", "t"))
        await rig.feed(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await rig.onset()
        await rig.feed(final(QUESTION, 1))
        await rig.feed(final("Fig.", NO_STOP))
        assert rig.starts() == 1 and rig.appended == ["Fig."], (rig.events, rig.appended)


async def test_without_the_reply_hold_onset_only_words_keep_every_exception():
    """The same exceptions as everywhere: a stop word is a turn; once the bot is audible
    the barge-in count applies; once the reply ended unspoken, one word is a turn."""
    rig = Rig(onset_is_speech=False)
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.onset()
        await rig.feed(final("Stop.", NO_STOP))
        assert rig.starts() == 2 and rig.appended == [], (rig.events, rig.appended)
    rig = Rig(onset_is_speech=False)
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(BotStartedSpeakingFrame())
        await rig.onset()
        await rig.feed(final("Right.", NO_STOP))
        assert rig.starts() == 1 and rig.appended == [], "one word over the bot: guarded"
        await rig.onset()
        await rig.feed(final("No, the other one.", NO_STOP))
        assert rig.starts() == 2 and rig.appended == [], "a barge-in over the bot"
    rig = Rig(onset_is_speech=False)
    async with running_controller(rig.controller):
        await rig.committed_turn()
        rig.start.reply_unspoken("a response with nothing to say")
        await rig.onset()
        await rig.feed(final("Hello?", NO_STOP))
        assert rig.starts() == 2 and rig.appended == [], (rig.events, rig.appended)


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


async def test_a_stop_word_in_the_wait_is_never_a_tail():
    """A SIP caller's "Stop." after the commit, before the bot is audible -- often with
    no VAD start and no onset (#216: real ones at 0.39/0.48 against the onset's 0.6).
    TEAPORT_STOP_WORDS end a reply on their own: it starts the turn, cutting the reply
    in flight, and nothing is joined to the question. An interim counts too."""
    for said in ("Stop.", "Hold on.", "Wait, wait!"):
        rig = Rig()
        async with running_controller(rig.controller):
            await rig.committed_turn()
            await rig.feed(final(said, NO_STOP))
            assert rig.starts() == 2 and rig.appended == [], (said, rig.events,
                                                              rig.appended)
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(InterimTranscriptionFrame("Wait", "u", "t"))
        assert rig.starts() == 2, "the stop word's interim must start the turn"
        await rig.feed(final("Wait.", NO_STOP))
        assert rig.appended == [], rig.appended
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(InterimTranscriptionFrame("Okay", "u", "t"))   # a tail's segment
        await rig.feed(final("Stop.", NO_STOP))
        assert rig.starts() == 2 and rig.appended == [], (rig.events, rig.appended)


async def test_a_stop_word_among_other_words_in_the_wait_is_never_a_tail():
    """"Okay, wait.", "No, wait.", "Wait, no.", "Okay, stop." in the reply's wait: any
    stop word keeps words from being a tail, not only an utterance made of them (that
    rule is for cutting a playing reply). Main cut the reply on every one; so does the
    bot audible. On SIP with no speech heard; on Talk and the room also after an onset,
    which is no speech there. An interim that grows a stop word starts the turn."""
    for said in STOP_PHRASES:
        for onset_is_speech, onset in ((True, False), (False, False), (False, True)):
            rig = Rig(onset_is_speech=onset_is_speech)
            async with running_controller(rig.controller):
                await rig.committed_turn()
                if onset:
                    await rig.onset()
                await rig.feed(final(said, NO_STOP))
                assert rig.starts() == 2 and rig.appended == [], (
                    said, onset_is_speech, onset, rig.events, rig.appended)
    rig = Rig(onset_is_speech=False)
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.onset()
        await rig.feed(InterimTranscriptionFrame("Okay", "u", "t"))   # a tail's, so far
        assert rig.starts() == 1, rig.events
        await rig.feed(InterimTranscriptionFrame("Okay wait", "u", "t"))
        assert rig.starts() == 2, "the stop word's interim must start the turn"
        await rig.feed(final("Okay, wait.", NO_STOP))
        assert rig.appended == [], rig.appended


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


async def test_a_tail_segment_ends_once_the_bot_is_audible():
    """A segment whose interim came in the wait, its final after the reply started: the
    words are counted as any over the bot -- a barge-in of two words or more (#215:
    words over the bot often fire no onset), one word under the guard -- and after the
    bot stops, one word is a turn. Nothing of it is joined to the turn."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(InterimTranscriptionFrame("Okay", "u", "t"))
        assert rig.starts() == 1, "in the wait the interim is a tail's"
        await rig.feed(BotStartedSpeakingFrame())
        await rig.feed(InterimTranscriptionFrame("Okay no that is not what I asked",
                                                 "u", "t"))
        assert rig.starts() == 2, "a barge-in over the playing bot was swallowed"
        await rig.feed(final("Okay, no, that is not what I asked.", NO_STOP))
        assert rig.appended == [], rig.appended
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(InterimTranscriptionFrame("Okay wait", "u", "t"))
        await rig.feed(BotStartedSpeakingFrame())
        await rig.feed(final("Okay, wait.", 1))
        assert rig.starts() == 2 and rig.appended == [], (rig.events, rig.appended)
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(InterimTranscriptionFrame("Okay", "u", "t"))
        await rig.feed(BotStartedSpeakingFrame())
        await rig.feed(final("Okay.", NO_STOP))
        assert rig.starts() == 1 and rig.appended == [], (rig.events, rig.appended)
        assert rig.taken.why("Okay.") == "dropped by the barge-in guard", (
            rig.taken.why("Okay."))
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(InterimTranscriptionFrame("Okay", "u", "t"))
        await rig.feed(BotStartedSpeakingFrame())
        await rig.feed(BotStoppedSpeakingFrame())
        await rig.feed(final("Okay.", NO_STOP))
        assert rig.starts() == 2 and rig.appended == [], (rig.events, rig.appended)
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(InterimTranscriptionFrame("Stop", "u", "t"))
        await rig.feed(BotStartedSpeakingFrame())
        await rig.feed(final("Stop.", 1))
        assert rig.starts() == 2, "a stop word must cut the bot (#216)"


async def test_the_wait_ends_when_the_reply_ends_unspoken():
    """An empty answer, or a tool round that asks for none (UnspokenReplyTap): nothing
    is in flight to cut, and words joined to the turn would go unanswered. A word after
    it is a turn, a segment begun in the wait included."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        rig.start.reply_unspoken("a response with nothing to say")
        await rig.feed(final("Hello?", NO_STOP))
        assert rig.starts() == 2 and rig.appended == [], (rig.events, rig.appended)
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(InterimTranscriptionFrame("Hello", "u", "t"))
        assert rig.starts() == 1, rig.events
        rig.start.reply_unspoken("set_timer asked for no answer")
        await rig.feed(final("Hello?", NO_STOP))
        assert rig.starts() == 2 and rig.appended == [], (rig.events, rig.appended)


async def test_the_wait_ends_ten_seconds_after_the_commit():
    """A reply that never comes at all (a consult with nothing said, an end nothing
    reported): words with no speech under them are a tail inside REPLY_WAIT_S of the
    commit, and a turn of their own after it -- not folded into a message already
    answered. Its own bound: the silent-turn watchdog's setting is a diagnostic."""
    assert PauseAwareMinWordsStrategy(min_words=2).reply_wait_secs == REPLY_WAIT_S == 10.0
    rig = Rig()
    wait = rig.start.reply_wait_secs
    async with running_controller(rig.controller):
        await rig.committed_turn()
        rig.start._due_at -= wait - 1                  # the reply due a second short
        await rig.feed(final(TAIL, NO_STOP))
        assert rig.starts() == 1 and rig.appended == [TAIL], (rig.events, rig.appended)
        rig.start._due_at -= 2                         # and now a second past
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


# ---- the tap that ends the wait --------------------------------------------------------

async def _tapped(*frames):
    told = []
    await run_test(UnspokenReplyTap(told.append), frames_to_send=list(frames))
    return told


def _result(properties=None, result="ok"):
    return FunctionCallResultFrame(function_name="set_timer", tool_call_id="c1",
                                   arguments={}, result=result, properties=properties)


async def test_the_tap_tells_of_a_reply_that_ends_with_nothing_to_play():
    assert await _tapped(LLMFullResponseStartFrame(), LLMFullResponseEndFrame()) == [
        "a response with nothing to say"]
    spoken = await _tapped(LLMFullResponseStartFrame(), LLMTextFrame(ANSWER),
                           LLMFullResponseEndFrame())
    assert spoken == [], spoken
    # Text the TTS would not speak (tts_text.has_speech): nothing of it plays.
    assert await _tapped(LLMFullResponseStartFrame(), LLMTextFrame("\u200b\u200b"),
                         LLMFullResponseEndFrame()) == ["a response with nothing to say"]
    # A tool round: its result runs the model again, and that answer is the reply.
    tool = await _tapped(LLMFullResponseStartFrame(),
                         FunctionCallsStartedFrame(function_calls=[]),
                         LLMFullResponseEndFrame(), _result())
    assert tool == [], tool
    # Unless the result asks for no answer (tools.no_inference), or has none to give.
    told = await _tapped(LLMFullResponseStartFrame(),
                         FunctionCallsStartedFrame(function_calls=[]),
                         LLMFullResponseEndFrame(), _result(no_inference()))
    assert told == ["set_timer asked for no answer"], told
    assert await _tapped(_result(result=None)) == ["set_timer asked for no answer"]
    update = FunctionCallResultProperties(run_llm=False, is_final=False)
    assert await _tapped(_result(update)) == [], "an update: the call goes on"


class _Mark(DataFrame):
    """Queued behind the frames under test: once the probe below has it, everything
    ahead of it has been handled above."""


async def _through_the_tts(*frames):
    """`frames` into the tap and the real EngineTTSService below it, as agent_session
    places them: what the tap told, and what came out below the TTS."""
    told, tts, probe = [], EngineTTSService(voice="af_heart"), Probe()
    tts._synth_text = _fake_segments
    task = PipelineTask(Pipeline([UnspokenReplyTap(told.append), tts, probe]),
                        cancel_on_idle_timeout=False)
    started = asyncio.Event()

    @task.event_handler("on_pipeline_started")
    async def _started(_task, _frame):
        started.set()

    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    try:
        await asyncio.wait_for(started.wait(), WAIT_S)
        await task.queue_frames([*frames, _Mark()])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAIT_S
        while (not any(isinstance(f, _Mark) for _, f in probe.seen)
               and loop.time() < deadline):
            await asyncio.sleep(0.01)
        assert any(isinstance(f, _Mark) for _, f in probe.seen), "the mark never came"
        await asyncio.sleep(0.3)        # a frame the TTS would pass has passed by now
    finally:
        await task.cancel()
        await run
    return told, [f for _, f in probe.seen]


async def test_the_tap_reads_the_reply_above_the_real_tts():
    """The TTS holds the End of a completion with no text (test_pipecat_premises:
    check_a_text_less_completions_end_is_held_by_the_tts): below it, an empty answer
    would end the wait never. Above it the End passes, and the tap tells of it."""
    end = LLMFullResponseEndFrame()
    told, below = await _through_the_tts(LLMFullResponseStartFrame(), end)
    assert told == ["a response with nothing to say"], told
    assert not any(f is end for f in below), (
        "the TTS passed a text-less End: the tap could sit below it again")
    told, below = await _through_the_tts(LLMFullResponseStartFrame(), LLMTextFrame(ANSWER),
                                         LLMFullResponseEndFrame())
    assert told == [], told
    assert any(isinstance(f, TTSAudioRawFrame) for f in below), "the answer was not spoken"
    told, _ = await _through_the_tts(
        LLMFullResponseStartFrame(), FunctionCallsStartedFrame(function_calls=[]),
        LLMFullResponseEndFrame(), _result(no_inference()))
    assert told == ["set_timer asked for no answer"], told


async def _tapped_in_order(*frames):
    """`frames` through the tap one at a time, each sent once the last has come out
    below it: in the order the LLM service pushes them, which a SystemFrame (a
    dispatch, an in-progress frame) could otherwise overtake. What the tap told."""
    told, probe = [], Probe()
    task = PipelineTask(Pipeline([UnspokenReplyTap(told.append), probe]),
                        cancel_on_idle_timeout=False)
    started = asyncio.Event()

    @task.event_handler("on_pipeline_started")
    async def _started(_task, _frame):
        started.set()

    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(started.wait(), WAIT_S)
        for frame in frames:
            await task.queue_frame(frame)
            deadline = loop.time() + WAIT_S
            while not any(f is frame for _, f in probe.seen) and loop.time() < deadline:
                await asyncio.sleep(0.005)
            assert any(f is frame for _, f in probe.seen), f"{frame} never came out"
    finally:
        await task.cancel()
        await run
    return told


def _call(name):
    return FunctionCallFromLLM(function_name=name, tool_call_id="c1", arguments={},
                               context=None)


async def test_the_tap_reads_a_tool_call_from_its_dispatch_not_its_in_progress_frame():
    """The LLM service broadcasts FunctionCallsDispatchedFrame (and pipecat's
    FunctionCallsStartedFrame) before the response's End; FunctionCallInProgressFrame
    comes from the call's own task and can trail the End (services.py). Read from that,
    the tool flag outlived its response: the empty answer after the tool's result went
    unreported, and the wait ran its full bound. And a completion whose only call is a
    silent cancel_<tool> is in pipecat's Started frame not at all, only in the dispatch:
    it is a tool round, not an answer that said nothing, and its follow-up may still
    play."""
    told = await _tapped_in_order(
        LLMFullResponseStartFrame(),
        FunctionCallsDispatchedFrame(function_calls=[_call("set_timer")]),
        FunctionCallsStartedFrame(function_calls=[_call("set_timer")]),
        LLMFullResponseEndFrame(),
        FunctionCallInProgressFrame(function_name="set_timer", tool_call_id="c1",
                                    arguments={}),
        _result(),
        LLMFullResponseStartFrame(), LLMFullResponseEndFrame())     # the answer: empty
    assert told == ["a response with nothing to say"], told
    told = await _tapped_in_order(
        LLMFullResponseStartFrame(),
        FunctionCallsDispatchedFrame(function_calls=[_call("cancel_set_timer")]),
        LLMFullResponseEndFrame())
    assert told == [], "a cancel-only completion told as an answer that said nothing"


async def test_the_tail_is_appended_to_the_callers_own_message_in_place():
    """Into the caller's newest message, past messages that are not theirs (an injected
    order, a Talk client's app event, a connect cue) and a tool round; nothing added,
    removed or moved."""
    order = f"{SYSTEM_NOTICE_TAG} tell the user the consult is back"
    note = '[app event, not spoken by the user] "Timer set for 10 minutes"'
    cue = "(The call/session just connected. Greet me warmly in one short sentence.)"
    ctx = LLMContext([{"role": "system", "content": "sys"},
                      {"role": "user", "content": QUESTION},
                      {"role": "assistant", "content": "", "tool_calls": [
                          {"id": "c1", "type": "function",
                           "function": {"name": "f", "arguments": "{}"}}]},
                      {"role": "tool", "tool_call_id": "c1", "content": "ok"},
                      {"role": "user", "content": order},
                      {"role": "user", "content": note},
                      {"role": "user", "content": cue}])
    before = [id(m) for m in ctx.get_messages()]
    assert append_to_last_turn(ctx, TAIL)
    assert [id(m) for m in ctx.get_messages()] == before
    msgs = ctx.get_messages()
    assert msgs[1]["content"] == f"{QUESTION} {TAIL}"
    assert [m["content"] for m in msgs[4:]] == [order, note, cue], msgs[4:]
    assert not append_to_last_turn(ctx, "  ")
    assert not append_to_last_turn(LLMContext([{"role": "system", "content": "s"}]), TAIL)
    # Only messages that are not the caller's: nothing to join, and nothing changed.
    only_cue = LLMContext([{"role": "system", "content": "s"},
                           {"role": "user", "content": cue}])
    assert not append_to_last_turn(only_cue, TAIL)
    assert only_cue.get_messages()[1]["content"] == cue


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
