#
# A bare stop holds: "Okay, wait." cuts the reply and the model is not asked on it
# (barge_pause.py, A BARE STOP).
#
# The failure: a question, its answer starting, and the caller's "Okay, wait." -- the
# barge-in rules cut the answer, and then the "Okay, wait." was committed as a turn like
# any other, so the model answered the question it had just been stopped from answering.
# On the room, on Talk and on a phone call alike.
#
# Driven through the real turn path of test_zero_length_final.Session (the STT, the user
# aggregator with the brain's strategies, keep_barge_in_reachable, CutReplyFence with its
# hold wired as agent_session wires it, the corrector, the ledger and -- on SIP -- the
# reply hold with its TurnMerge), with the LLM a stand-in whose requests are counted.
# Shapes: the stop over the playing reply on each front end; the stop in the reply's
# wait is test_zero_length_final's (_a_stop_word_among_the_words_cuts_the_reply). The
# controls: a stop with a question in it is answered, a backchannel over the bot cuts
# nothing, the caller's next words after a held stop are a turn, a quick "Wait." that
# answers the bot's own question after it ended is the model's, and so is "No, wait."
# while the incoming-call question is open. Then the rule's edges on the strategy, the
# fence and the session's wiring.
#
# All utterances are synthetic.
#
# Run: python test_bare_stop.py
#
import asyncio
import os
import sys

# A session built whole (the call prompt's wiring) needs somewhere to point: nowhere real.
os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from loguru import logger  # noqa: E402

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
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
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.turns.user_stop.base_user_turn_stop_strategy import (  # noqa: E402
    BaseUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402
from pipecat.utils.string import TextPartForConcatenation  # noqa: E402

from stt_harness import WireRecorder  # noqa: E402
from test_zero_length_final import (  # noqa: E402
    ANSWER,
    QUESTION,
    SR,
    STARTUP_S,
    InFlightLLM,
    Rig,
    Session,
)
from turn_harness import running_controller  # noqa: E402

import teaport_brain.turn_timing as turn_timing  # noqa: E402
from teaport_brain.agent_session import user_aggregator_params  # noqa: E402
from teaport_brain.barge_pause import (  # noqa: E402
    PauseAwareMinWordsStrategy,
    is_bare_stop,
)
from teaport_brain.endpointing import CutReplyFence  # noqa: E402
from teaport_brain.taken_turn import TakenWords  # noqa: E402

STOP = "Okay, wait."
NEXT = "And the tallest in Europe?"


class PlayingLLM(InFlightLLM):
    """InFlightLLM whose answer starts playing and never ends by itself: its first words
    and their audio go out once `release` is set, and the End waits on `finish`. An
    interruption before then cuts it (`cuts`), over the playing bot."""

    def __init__(self):
        super().__init__(audio=True)
        self.finish = asyncio.Event()

    async def _answer(self):
        await self.release.wait()
        words = self.answers.pop(0) if self.answers else ANSWER
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(words))
        await self.push_frame(TTSAudioRawFrame(audio=b"\x00\x00" * 160, sample_rate=SR,
                                               num_channels=1))
        await self.finish.wait()
        self._task = None
        await self.push_frame(LLMFullResponseEndFrame())
        self.answered += 1


def _session(front_end) -> Session:
    return Session(front_end, stock=False, llm=PlayingLLM())


async def _say(s, text):
    """Speech the VAD hears: its start, the words as an interim, its stop; the STT's
    commit, and the engine's final for it."""
    n = len(s.stt.whys)
    await s.stt.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
    await s.audio(0.3)
    await s.stt._handle_message({"type": "transcription.delta", "delta": text})
    await s.audio(0.3)
    await s.stt.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.5))
    assert await s.until(lambda: len(s.stt.whys) > n), f"{text!r} was never committed"
    await s.stt._handle_message({"type": "transcription.done", "text": text,
                                 "reason": "commit"})


async def _over_the_playing_reply(front_end, said, *, then=None):
    """The question; its answer starts playing (its words and audio out, the bot
    audible); `said` over it; then, if given, `then` once the bot is quiet. Returns the
    session once the model has been asked on everything it is going to be asked on."""
    s = _session(front_end)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(s.task))
    await asyncio.sleep(STARTUP_S)
    s.stt._websocket = WireRecorder()
    try:
        await s.question()
        s.llm.release.set()
        assert await s.until(lambda: s.gate is None or not s.gate.armed), (
            "the answer's audio never passed the reply hold")
        # The transport's word that the bot is audible: it reaches the start strategy as
        # the real one does, through the user aggregator.
        await s.stt.queue_frame(BotStartedSpeakingFrame())
        await s.audio(0.1)
        await _say(s, said)
        await s.until(lambda: s.start.deliberately_silent() or len(s.llm.contexts) >= 2)
        await s.audio(0.3)
        if then is not None:
            await s.stt.queue_frame(BotStoppedSpeakingFrame())
            asked = len(s.llm.contexts)
            await _say(s, then)
            assert await s.until(lambda: len(s.llm.contexts) > asked), (
                f"{then!r} after the stop was never asked")
            await s.audio(0.2)
    finally:
        s.stt._websocket = None
        s.llm.finish.set()          # a reply nothing cut ends, rather than be destroyed
        await s.audio(0.1)
        await s.task.cancel()
        await run
    return s


def _users(ctx):
    return [m["content"] for m in ctx if m["role"] == "user"]


async def _a_bare_stop_over_the_playing_reply_is_not_answered(front_end):
    s = await _over_the_playing_reply(front_end, STOP)
    assert s.llm.cuts == 1, f"{front_end}: the stop did not cut the reply ({s.llm.cuts})"
    assert len(s.llm.contexts) == 1, (
        f"{front_end}: the model was asked {len(s.llm.contexts)} times -- the stop was "
        "answered")
    assert s.turns_started == 2 and s.start.deliberately_silent(), s.turns_started
    assert _users(s.context.get_messages()) == [QUESTION], s.context.get_messages()
    assert s.taken.why(STOP) is not None, "the held words are not on record"
    if s.merge is not None:
        assert not s.merge.pending, "the reply had played: nothing to merge"


async def test_a_bare_stop_over_the_playing_reply_is_not_answered_talk():
    await _a_bare_stop_over_the_playing_reply_is_not_answered("talk")


async def test_a_bare_stop_over_the_playing_reply_is_not_answered_room():
    await _a_bare_stop_over_the_playing_reply_is_not_answered("room")


async def test_a_bare_stop_over_the_playing_reply_is_not_answered_sip():
    await _a_bare_stop_over_the_playing_reply_is_not_answered("sip")


async def test_after_a_held_stop_the_next_words_are_a_turn():
    """"Okay, wait." ... "And the tallest in Europe?": the model is asked once more, on
    the question and the new words -- not on the stop."""
    for front_end in ("talk", "sip"):
        s = await _over_the_playing_reply(front_end, STOP, then=NEXT)
        assert len(s.llm.contexts) == 2, (front_end, len(s.llm.contexts))
        assert _users(s.llm.contexts[1]) == [QUESTION, NEXT], (
            front_end, _users(s.llm.contexts[1]))
        assert not s.start.deliberately_silent()


async def test_a_question_with_a_stop_word_over_the_reply_is_answered():
    """The control: "Wait, what time is it?" cuts the reply and is a turn as on main."""
    said = "Wait, what time is it?"
    for front_end in ("talk", "sip"):
        s = await _over_the_playing_reply(front_end, said)
        assert s.llm.cuts == 1 and len(s.llm.contexts) == 2, (
            front_end, s.llm.cuts, len(s.llm.contexts))
        assert _users(s.llm.contexts[1]) == [QUESTION, said], _users(s.llm.contexts[1])
        assert not s.start.deliberately_silent()


async def test_a_backchannel_over_the_reply_is_unchanged():
    """The control: one word over the playing bot cuts nothing and asks nothing."""
    for front_end in ("talk", "sip"):
        s = await _over_the_playing_reply(front_end, "Yeah.")
        assert s.llm.cuts == 0 and len(s.llm.contexts) == 1, (
            front_end, s.llm.cuts, len(s.llm.contexts))
        assert s.turns_started == 1 and not s.start.deliberately_silent()


# ---- the rule's edges -------------------------------------------------------------

def test_what_is_a_bare_stop():
    stops = ["Stop.", "Wait.", "Okay, wait.", "No, wait.", "Wait, wait.", "Wait, no.",
             "Hold on.", "Hold on a second.", "Okay okay, stop!", "Stop, please.",
             "Shhh.", "Um, hold on.", "Sorry, wait a minute.", "Just a sec, wait."]
    others = ["", "Okay.", "No.", "Yeah, wait.", "Wait, what time is it?", "Don't stop.",
              "Stop by the shop.", "Hold the door.", "Wait for me.", "One second."]
    for t in stops:
        assert is_bare_stop(t), t
    for t in others:
        assert not is_bare_stop(t), t
    # The stop entries are the setting's: a custom list, phrases and all.
    assert is_bare_stop("Okay, arrête.", stop_words=("arrête",))
    assert not is_bare_stop("Okay, wait.", stop_words=("arrête",))


async def test_a_stop_with_nothing_to_cut_is_the_models():
    """A lone "Stop." to a box with nothing playing and no reply due is asked as before:
    there it may be about something else (a request in the background)."""
    rig = Rig()
    async with running_controller(rig.controller):
        await rig.committed_turn()
        await rig.feed(BotStartedSpeakingFrame())     # the reply plays, and ends
        await rig.feed(BotStoppedSpeakingFrame())
        await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.feed(InterimTranscriptionFrame("Stop", "u", "t"))
        assert rig.starts() == 2, rig.events
        assert not await rig.start.hold_bare_stop("Stop.")
        assert not rig.start.deliberately_silent()


async def test_a_stop_said_over_the_replys_last_words_is_held():
    """Its speech began over the reply -- the VAD's start, or the onset test alone (a
    short unvoiced "wait" the VAD misses) -- and its words land just after the bot
    stopped: still a stop over the reply."""
    for onset_only in (False, True):
        rig = Rig(onset_is_speech=not onset_only)
        async with running_controller(rig.controller):
            await rig.committed_turn()
            await rig.feed(BotStartedSpeakingFrame())
            if onset_only:
                await rig.start.on_onset(True)
            else:
                await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
            await rig.feed(BotStoppedSpeakingFrame())
            await rig.feed(InterimTranscriptionFrame("Wait", "u", "t"))
            assert rig.starts() == 2, (onset_only, rig.events)
            assert await rig.start.hold_bare_stop("Wait."), onset_only
            assert rig.start.deliberately_silent() and rig.taken.why("Wait.")


async def test_a_quick_stop_word_answer_after_the_bot_stopped_is_the_models():
    """The bot asks "Should I wait?"; it ends, and 0.5 s later the user says "Wait."
    -- the answer, its speech begun after the bot went quiet. Nothing was cut, and the
    model is asked on it."""
    for front_end in ("talk", "sip"):
        s = _session(front_end)
        s.llm.answers = ["Should I wait?"]
        run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(s.task))
        await asyncio.sleep(STARTUP_S)
        s.stt._websocket = WireRecorder()
        try:
            await s.question()
            s.llm.release.set()
            assert await s.until(lambda: s.gate is None or not s.gate.armed)
            await s.stt.queue_frame(BotStartedSpeakingFrame())
            await s.audio(0.1)
            s.llm.finish.set()                       # "Should I wait?" plays out
            assert await s.until(lambda: s.llm.answered == 1)
            await s.stt.queue_frame(BotStoppedSpeakingFrame())
            await s.audio(0.5)
            await _say(s, "Wait.")
            assert await s.until(lambda: len(s.llm.contexts) >= 2), (
                f"{front_end}: the answer 'Wait.' never reached the model")
            await s.audio(0.2)
        finally:
            s.stt._websocket = None
            await s.task.cancel()
            await run
        assert not s.start.deliberately_silent(), front_end
        assert _users(s.llm.contexts[1])[-1] == "Wait.", _users(s.llm.contexts[1])


async def test_while_the_call_question_is_open_a_stop_is_its_answer():
    """"No, wait." over "Someone's calling me -- should I step away?" is the answer
    to the question, which reaches answer_phone_call only through the model: it is not
    held, though it cut the question and is only a stop."""
    s = Session("talk", stock=False, llm=PlayingLLM())
    s.start.never_holds = lambda: True               # the question is open
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(s.task))
    await asyncio.sleep(STARTUP_S)
    s.stt._websocket = WireRecorder()
    try:
        await s.question()
        s.llm.release.set()
        await s.stt.queue_frame(BotStartedSpeakingFrame())
        await s.audio(0.1)
        await _say(s, "No, wait.")
        assert await s.until(lambda: len(s.llm.contexts) >= 2), "the answer was held"
    finally:
        s.stt._websocket = None
        s.llm.finish.set()
        await s.audio(0.1)
        await s.task.cancel()
        await run
    assert s.llm.cuts == 1 and _users(s.llm.contexts[1]) == [QUESTION, "No, wait."]
    assert not s.start.deliberately_silent()


async def test_the_session_never_holds_while_its_call_prompt_is_pending():
    """build_agent_session ties the hold to the session's CallPrompt: pending, nothing
    is held; answered (or never asked), a cut "No, wait." is."""
    from test_post_tool_under_speech import TalkTransport
    from teaport_brain.agent_session import build_agent_session
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator

    session = build_agent_session(TalkTransport(), front_end="talk")
    prompt = session.call_prompt
    assert prompt is not None, "a Talk session with a model has the call prompt"
    inner = next(p for p in session.task.pipeline.processors if isinstance(p, Pipeline))
    user = next(p for p in inner.processors if isinstance(p, LLMUserAggregator))
    start = user._user_turn_controller._user_turn_strategies.start[0]
    assert isinstance(start, PauseAwareMinWordsStrategy)
    prompt._pending = asyncio.get_running_loop().create_future()
    assert not await start.hold_bare_stop("No, wait.", dropped=True), "held the answer"
    prompt._pending.set_result(False)
    assert await start.hold_bare_stop("No, wait.", dropped=True)


async def test_a_held_stop_stands_the_speculation_down_and_leaves_no_wait():
    """The speculation asked on the stop's words stands down; and with nothing awaited
    the caller's next words are a turn, not a tail joined to the question."""
    rig = Rig()
    cancelled = []

    class Spec:
        async def cancel(self, reason, detail=""):
            cancelled.append(reason)

    rig.start.speculator = Spec()
    async with running_controller(rig.controller):
        await rig.committed_turn()              # the question; its reply is due
        await rig.feed(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.feed(InterimTranscriptionFrame(STOP, "u", "t"))
        assert rig.starts() == 2, rig.events
        assert await rig.start.hold_bare_stop(STOP)
        assert cancelled == ["bare-stop"], cancelled
        assert not rig.start._waiting(), "a held stop leaves a reply awaited"


async def test_the_fence_holds_a_stop_that_continues_an_unplayed_reply():
    """SIP's continuation commit (no turn start: the refused finalize's second commit)
    of "Okay, wait.": the fence still drops the unplayed reply (its interruption) and
    then holds the words -- nothing committed, nothing asked. A continuation that is not
    only a stop is committed as before."""

    class Tap(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.cuts = 0
            self.asked = 0

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if direction == FrameDirection.DOWNSTREAM:
                name = type(frame).__name__
                self.cuts += name == "InterruptionFrame"
                self.asked += name == "LLMContextFrame"
            await self.push_frame(frame, direction)

    class NoStop(BaseUserTurnStopStrategy):
        pass

    for words, held in ((STOP, True), ("and in Paris", False)):
        context = LLMContext([{"role": "system", "content": "sys"},
                              {"role": "user", "content": QUESTION}])
        start = PauseAwareMinWordsStrategy(min_words=2)
        start.taken_words = TakenWords()
        pair = LLMContextAggregatorPair(context, user_params=user_aggregator_params(
            user_turn_strategies=UserTurnStrategies(start=[start], stop=[NoStop()])))
        user = pair.user()
        fence = CutReplyFence()
        fence.hold_commits(user)
        fence.unplayed_reply = lambda: True           # the reply hold, still armed
        fence.holds = start.hold_bare_stop
        tap = Tap()
        task = PipelineTask(Pipeline([user, fence.head, tap, fence.tail]),
                            cancel_on_idle_timeout=False)
        run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
        await asyncio.sleep(STARTUP_S)
        try:
            user._aggregation = [TextPartForConcatenation(words,
                                                          includes_inter_part_spaces=False)]
            await user._push_aggregation()
            await asyncio.sleep(0.1)
        finally:
            await task.cancel()
            await run
        assert tap.cuts == 1, (words, "the unplayed reply was not dropped")
        assert start.deliberately_silent() is held, words
        assert tap.asked == (0 if held else 1), (words, tap.asked)
        want = [QUESTION] if held else [QUESTION, words]
        assert _users(context.get_messages()) == want, (words, context.get_messages())
        assert (start.taken_words.why(words) is not None) is held, words


async def test_the_silent_turn_watchdog_does_not_warn_on_a_held_stop():
    warned = []
    sink = logger.add(lambda m: warned.append(m), level="WARNING",
                      filter=lambda r: "SILENT TURN" in r["message"])
    saved = turn_timing._SILENT_TURN_SECS
    turn_timing._SILENT_TURN_SECS = 0.01
    try:
        for silent in (True, False):
            warned.clear()
            timer = turn_timing.TurnTimer({}, watchdog=True,
                                          deliberately_silent=lambda: silent)
            await timer._bark(timer._turn)
            assert bool(warned) is not silent, (silent, warned)
    finally:
        turn_timing._SILENT_TURN_SECS = saved
        logger.remove(sink)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
