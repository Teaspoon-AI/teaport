#
# The pipecat premises the formal models rest on, checked against the installed pipecat.
#
# Every model in brain/formal/ takes some of pipecat's behaviour as given: which turn
# start the controller refuses, when the user aggregator commits, what an interruption's
# queue reset keeps, what the TTS re-pushes after a context ends. A fix the model checked
# is only as good as those premises, and a pipecat upgrade can drop one without a line of
# the brain changing (the 1.12 bump moved the user aggregator's commit off the method
# CutReplyFence wrapped). Each check below drives REAL pipecat objects -- the turn
# controller, the user and assistant aggregators, the frame processor's queues, the TTS
# base under EngineTTSService, the output transport -- and asserts the behaviour a model
# assumes. The table in brain/formal/README.md ("pipecat premises") maps each premise to
# its model, its check (here or in another test that already pins it) and its source.
#
# A failure here means a model has drifted from pipecat: re-read the model's premise,
# not just the check. Each check's docstring names the model, the premise and what
# breaks if it goes.
#
# Premises that exist only on a newer pipecat (speculative inference, 1.9+) are checked
# when its API is there and reported as skipped otherwise.
#
# Run: python test_pipecat_premises.py   (or via pytest test_suite.py)
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
    DataFrame,
    EndFrame,
    FunctionCallCancelFrame,
    FunctionCallFromLLM,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallResultProperties,
    FunctionCallsStartedFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    TextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_start.base_user_turn_start_strategy import (  # noqa: E402
    BaseUserTurnStartStrategy,
)
from pipecat.turns.user_stop.base_user_turn_stop_strategy import (  # noqa: E402
    BaseUserTurnStopStrategy,
)
from pipecat.turns.user_turn_controller import UserTurnController  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from test_commit_over_resumed_voice import CHUNK, Scripted  # noqa: E402
from test_reply_hold import Tap, word  # noqa: E402
from test_tts_stop_frame import Probe, StubOutput, _fake_segments  # noqa: E402
from turn_harness import running_controller  # noqa: E402

from teaport_brain.endpointing import CutReplyFence, SegmentDoneFrame  # noqa: E402
from teaport_brain.engine_tts import EngineTTSService  # noqa: E402
from teaport_brain.heard_context import _cancelled_rounds  # noqa: E402
from teaport_brain.services import BoundedOpenAILLMService  # noqa: E402
from teaport_brain.stt import FinalTranscriptionFrame  # noqa: E402
from teaport_brain.tools import no_inference  # noqa: E402

try:  # pipecat 1.9+: speculative inference
    from pipecat.turns.types import UserTurnSpeculation
except ImportError:
    UserTurnSpeculation = None

STARTUP_TIMEOUT_S = 10.0
STEP_TIMEOUT_S = 5.0
DOWN, UP = FrameDirection.DOWNSTREAM, FrameDirection.UPSTREAM


class NotHere(Exception):
    """A premise this pipecat has no API for (a newer release's): skipped, not failed."""


# ---- rigs --------------------------------------------------------------------------


class Run:
    """A PipelineTask over `processors`, running for the `async with` body: entered once
    the StartFrame has reached the pipeline's sink (every processor has started), never
    on a guess at how long a runner takes to start."""

    def __init__(self, *processors, **task_kwargs):
        self.task = PipelineTask(Pipeline(list(processors)), cancel_on_idle_timeout=False,
                                 **task_kwargs)
        self.started = asyncio.Event()

        @self.task.event_handler("on_pipeline_started")
        async def _started(_task, _frame):
            self.started.set()

    async def __aenter__(self):
        self._run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.wait_for(self.started.wait(), STARTUP_TIMEOUT_S)
        return self

    async def __aexit__(self, *exc):
        if not self._run.done():
            await self.task.cancel()
        await self._run

    async def send(self, *frames):
        await self.task.queue_frames(list(frames))

    async def ended(self):
        """Wait for the pipeline to finish on its own (an EndFrame, a cancel)."""
        await asyncio.wait_for(asyncio.shield(self._run), STEP_TIMEOUT_S)


class Mark(DataFrame):
    """A sentinel queued behind the frames under test: by the time a Tap below sees it,
    every processor above has handled everything queued ahead of it. What a check
    asserts did NOT happen is asserted after its Mark arrives, never after a sleep."""


async def _until(predicate, timeout=None):
    """Poll `predicate` until it holds or STEP_TIMEOUT_S passes; return its last value.
    The callers assert on the state themselves, so a timeout fails with their message."""
    deadline = time.monotonic() + (timeout or STEP_TIMEOUT_S)
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return predicate()


async def _marked(run, tap):
    """Queue a Mark and wait for it at `tap`."""
    mark = Mark()
    await run.send(mark)
    assert await _until(lambda: any(f is mark for _, f in tap.down)), "the Mark never arrived"


def _turn_starts(user):
    """The user turns the aggregator has started, counted from its own event. The
    turn start's broadcast_interruption resets the aggregator's process queue first, so
    a scripted frame is sent only once the count has moved: queued behind the transcript
    that opens the turn, it would be flushed with the reset."""
    starts = []

    @user.event_handler("on_user_turn_started")
    async def _started(_aggregator, _strategy):
        starts.append(1)
    return starts


async def _open_turn(run, user_starts, text):
    """Send a final that opens a turn, and wait for the turn to have started."""
    n = len(user_starts)
    await run.send(_final(text))
    assert await _until(lambda: len(user_starts) > n), f"{text!r} opened no turn"


def _kinds(tap, direction=DOWN, of=None):
    """The class names a Tap saw in one direction, optionally only of the given types."""
    seen = tap.down if direction == DOWN else tap.up
    return [type(f).__name__ for _, f in seen if of is None or isinstance(f, of)]


class StopNow(DataFrame):
    """Asks the scripted stop strategy to conclude the turn (inference, then finalize)."""


class InferNow(DataFrame):
    """Asks the scripted stop strategy for the inference half alone."""


class FinalizeNow(DataFrame):
    """Asks the scripted stop strategy for the finalize half alone."""


class ScriptedStop(BaseUserTurnStopStrategy):
    """A stop strategy that concludes only when told, and counts pipecat's per-turn
    calls into it (where the brain's LateStartTurnStopStrategy keeps its state)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started = 0
        self.stopped = 0

    async def handle_user_turn_started(self):
        self.started += 1
        await super().handle_user_turn_started()

    async def handle_user_turn_stopped(self):
        self.stopped += 1
        await super().handle_user_turn_stopped()

    async def process_frame(self, frame):
        if isinstance(frame, StopNow):
            await self.trigger_user_turn_stopped()
        elif isinstance(frame, InferNow):
            await self.trigger_user_turn_inference_triggered()
        elif isinstance(frame, FinalizeNow):
            await self.trigger_user_turn_finalized()
        return await super().process_frame(frame)


class ScriptedStart(BaseUserTurnStartStrategy):
    """A start strategy that opens a turn only when told."""

    async def open(self):
        await self.trigger_user_turn_started()


def _user_params(**kwargs):
    """The user aggregator's params as the session builds them where the brain names
    them (agent_session.user_aggregator_params, since the 1.12 bump), else pipecat's
    with the session's one choice before it (no user mute)."""
    try:
        from teaport_brain.agent_session import user_aggregator_params
    except ImportError:
        return LLMUserAggregatorParams(user_mute_strategies=[], **kwargs)
    return user_aggregator_params(**kwargs)


def _user_pair(stop=None, start=None, **kwargs):
    """A real aggregator pair; the turn opens on any transcript (MinWords at 1 word, the
    bot silent) and ends when ScriptedStop is told."""
    context = LLMContext([{"role": "system", "content": "sys"}])
    stop = stop or ScriptedStop()
    pair = LLMContextAggregatorPair(context, user_params=_user_params(
        user_turn_strategies=UserTurnStrategies(
            start=[start or MinWordsUserTurnStartStrategy(min_words=1)], stop=[stop]),
        **kwargs))
    return context, pair, stop


def _final(text):
    f = TranscriptionFrame(text, "caller", "t")
    f.finalized = True
    return f


def _user_msgs(context):
    return [m.get("content") for m in context.get_messages() if m.get("role") == "user"]


def _controller(stop, start=None, timeout=5.0):
    """A bare UserTurnController with the turn_harness lifecycle; events recorded."""
    controller = UserTurnController(
        user_turn_strategies=UserTurnStrategies(start=[start or ScriptedStart()], stop=[stop]),
        user_turn_stop_timeout=timeout)
    events = []

    async def ignore(*args, **kwargs):
        pass

    for name in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                 "on_user_turn_stop_timeout"):
        controller.add_event_handler(name, ignore)
    for name in ("on_user_turn_started", "on_user_turn_inference_triggered",
                 "on_user_turn_stopped"):
        async def record(*args, _name=name, **kwargs):
            events.append(_name)
        controller.add_event_handler(name, record)
    return controller, events


def _llm(cls=BoundedOpenAILLMService):
    return cls(api_key="test", base_url="http://127.0.0.1:1",
               settings=BoundedOpenAILLMService.Settings(model="x"))


def _tts(segments=_fake_segments, stop_timeout=None):
    tts = EngineTTSService(voice="af_heart")
    tts._synth_text = segments
    if stop_timeout is not None:
        tts._stop_frame_timeout_s = stop_timeout      # the 15 s sweep, shortened
    return tts


def _of(probe, cls, ctx=None):
    return [(i, f) for i, (_, f) in enumerate(probe.seen) if isinstance(f, cls)
            and (ctx is None or getattr(f, "context_id", None) == ctx)]


# ---- UserTurn.tla: the controller ---------------------------------------------------


async def check_a_second_turn_start_is_refused_while_one_is_open():
    """UserTurn.tla, `StartTurn` requires no open turn: UserTurnController.
    _trigger_user_turn_start returns early while `_user_turn` is set ("Prevent two
    consecutive user turn starts"). With the finalize guard below it makes an open,
    spoken-over turn uninterruptible -- the wedge keep_barge_in_reachable
    (MODE retryOnQuiet / retryMerges) exists for. A start also calls every stop
    strategy's handle_user_turn_started, where LateStartTurnStopStrategy applies its
    late-start rule (SttCommit.tla, OpenTurn).

    If it goes: a second start while one is open would broadcast a second
    interruption -- the wrapper's "owed" bookkeeping (cleared only on a start that
    opened a turn) and the late-start rule would both be reasoning about a controller
    that no longer exists. (test_barge_in_survives_an_open_turn.py's
    test_the_stock_controller_still_wedges pins the composite wedge.)"""
    stop, start = ScriptedStop(), ScriptedStart()
    controller, events = _controller(stop, start)
    async with running_controller(controller):
        await start.open()
        await start.open()
        assert events.count("on_user_turn_started") == 1, events
        assert stop.started == 1, f"handle_user_turn_started ran {stop.started}x"


async def check_a_finalize_is_refused_while_the_user_speaks():
    """UserTurn.tla, `Finalize`: _trigger_user_turn_stop refuses while `_user_speaking`
    ("Never finalize while the user is audibly speaking") and leaves the turn open with
    its stop strategy UN-reset (no handle_user_turn_stopped) -- "keep the turn open so
    the next inference re-evaluates", which is EARLY_FINAL's second commit. Once the
    user is quiet the same stop closes the turn and resets the strategy, the hook
    LateStartTurnStopStrategy.handle_user_turn_stopped uses to close a held STT segment
    (SttCommit.tla, TURNSTOP = "verdict").

    If it goes: keep_barge_in_reachable's owed finalization (re-applied at the next
    quiet moment) and the continuation merge (CutReplyFence.hold_commits) answer a
    refusal that no longer happens; if the reset stops running on a closed turn, the
    STT holds an INCOMPLETE segment to its expiry (NoOrphanedHold)."""
    stop = ScriptedStop()
    controller, events = _controller(stop)
    async with running_controller(controller):
        await controller.user_turn_strategies.start[0].open()
        await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await controller.process_frame(StopNow())
        assert controller._user_turn and "on_user_turn_stopped" not in events, events
        assert "on_user_turn_inference_triggered" in events, (
            "the inference half must still run ahead of the refused finalize")
        assert stop.stopped == 0, "a refused finalize reset the stop strategy"
        await controller.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))
        await controller.process_frame(StopNow())
        assert not controller._user_turn and "on_user_turn_stopped" in events, events
        assert stop.stopped == 1, f"handle_user_turn_stopped ran {stop.stopped}x"


async def check_the_stop_watchdog_is_starved_by_speech_and_resets_the_strategy():
    """UserTurn.tla, `Arm`/`ForceStop`: user_turn_stop_timeout is an INACTIVITY timer --
    every VAD edge and transcript re-arms it -- and it stops a turn only while the user
    is quiet. That is why the stock wedge is absorbing (speech holds it shut) and why
    the fix re-applies the stop at the next quiet moment instead of waiting for it. Its
    stop runs the controller's ordinary turn stop: the stop strategies are reset
    (handle_user_turn_stopped), so LateStartTurnStopStrategy closes a held segment on
    the watchdog's stop too (SttCommit.tla, TurnStop with TURNSTOP = "verdict").

    If it goes: a watchdog that fired through speech would itself recover the wedge
    (keep_barge_in_reachable then double-stops); one that skipped the reset would leave
    the STT holding a segment to its expiry on every watchdog stop."""
    stop = ScriptedStop()
    controller, events = _controller(stop, timeout=0.6)
    async with running_controller(controller):
        await controller.user_turn_strategies.start[0].open()
        await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        # A timer that must NOT fire: only a bounded wait can say so -- two and a half
        # periods. Not a whole number of them: the timer runs in phase with the VAD
        # start, and a VAD stop landing on one of its expiries races it (the expiry
        # then finds the user quiet and stops the turn).
        await asyncio.sleep(1.5)
        assert controller._user_turn, "the watchdog stopped a turn the user was speaking in"
        await controller.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))
        for _ in range(12):                      # transcripts keep re-arming it
            await asyncio.sleep(0.1)
            await controller.process_frame(InterimTranscriptionFrame("and", "u", "t"))
        assert controller._user_turn, "transcripts every 0.1 s did not re-arm a 0.6 s watchdog"
        assert await _until(lambda: not controller._user_turn), (
            "the watchdog never stopped a quiet turn")
        assert "on_user_turn_inference_triggered" not in events, (
            "the watchdog's stop has no inference ahead of it")
        assert stop.stopped == 1, f"handle_user_turn_stopped ran {stop.stopped}x"


async def check_min_words_relaxes_to_one_word_only_while_the_bot_is_silent():
    """UserTurn.tla, PAUSE (`OneWord`, `NoLostAnswer`): MinWordsUserTurnStartStrategy
    asks min_words of a transcript while the bot speaks (Bot{Started,Stopped}Speaking
    frames) and one word otherwise. The pause's design rests on both halves: a
    one-word "Yes." over a bot that still counts as speaking is dropped (why the tail is
    re-announced and the would-have-ended rule exists), and one word against a silent
    bot is a turn.

    If it goes: either one-word answers are never turns (lost answers everywhere) or
    any one word cuts the bot (the 2-word guard against STT garble is gone)."""
    def strategy():
        return MinWordsUserTurnStartStrategy(min_words=2)

    controller, events = _controller(ScriptedStop(), strategy())
    async with running_controller(controller):
        await controller.process_frame(BotStartedSpeakingFrame())
        await controller.process_frame(_final("Yes."))
        assert "on_user_turn_started" not in events, "one word cut a speaking bot"
        await controller.process_frame(_final("Yes please."))
        assert events.count("on_user_turn_started") == 1, events
    controller, events = _controller(ScriptedStop(), strategy())
    async with running_controller(controller):
        await controller.process_frame(BotStartedSpeakingFrame())
        await controller.process_frame(BotStoppedSpeakingFrame())
        await controller.process_frame(_final("Yes."))
        assert events.count("on_user_turn_started") == 1, (
            "one word against a silent bot did not start a turn")


# ---- UserTurn.tla: the user aggregator -----------------------------------------------


async def check_a_turn_start_broadcasts_the_interruption():
    """UserTurn.tla, `StartTurn` / `CutLands`: barge-in in this brain is a user turn
    start and nothing else -- the user aggregator's on_user_turn_started broadcasts an
    InterruptionFrame both ways (broadcast_interruption), which then crosses the
    pipeline as a frame of its own. Every model takes `Interrupt` as available on
    that basis (Ledger, LedgerPlayout, Followup).

    If it goes: no barge-in at all -- the bot cannot be cut. (That the commit in the
    same controller call can overtake the queued frame -- CutReplyFence's reason -- is
    pinned by test_barge_commit_order.py, test_without_the_fence_the_turn_overtakes_
    the_cut_reply.)"""
    _, pair, _ = _user_pair()
    top, below = Tap(), Tap()
    async with Run(top, pair.user(), below) as run:
        await run.send(_final("hello there"))
        await _until(lambda: _kinds(below, DOWN, InterruptionFrame)
                     and _kinds(top, UP, InterruptionFrame))
    assert _kinds(below, DOWN, InterruptionFrame), "no interruption went down on a turn start"
    assert _kinds(top, UP, InterruptionFrame), "no interruption went up on a turn start"


async def check_the_commit_runs_before_the_finalize_it_may_lose():
    """UserTurn.tla, `Inference` / `Resume` / `Finalize`: trigger_user_turn_stopped is
    two awaits, and the user aggregator writes the turn to the context (push_aggregation,
    the LLM asked) in the FIRST. A VAD start the aggregator's input task runs inside it
    then has the finalize refused: the words are in the context, the turn stays open.
    That is the only entry to the stranded turn, and why keep_barge_in_reachable owes a
    refused finalize only once its inference has run.

    If it goes: if the commit moved behind the finalize, a refused finalize would no
    longer leave a spent turn (the wrapper's owed re-apply would commit words the turn
    never had); the model's `Resume` gap would be fiction."""
    context, pair, _ = _user_pair()
    user = pair.user()
    controller = user._user_turn_controller
    push_context_frame = user.push_context_frame
    resumed = []

    async def resume_in_the_gap(*args, **kwargs):
        await push_context_frame(*args, **kwargs)
        if not resumed:                         # the caller resumes inside the await
            resumed.append(list(_user_msgs(context)))
            await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
    user.push_context_frame = resume_in_the_gap
    starts, below = _turn_starts(user), Tap()
    async with Run(user, below) as run:
        await _open_turn(run, starts, "hello there")
        await run.send(StopNow())
        await _marked(run, below)               # the stop strategy has run StopNow
        assert resumed == [["hello there"]], (
            f"the words were not in the context when the LLM was asked: {resumed}")
        assert controller._user_turn, "the finalize was not refused after the commit"


async def check_every_commit_passes_the_fenced_writer():
    """UserTurn.tla, FENCE (`CommitStep`, NoCutAfterCommit) and the reply hold's arming:
    every path that writes the caller's words to the context goes through the one writer
    CutReplyFence.hold_commits wraps (push_aggregation on 1.8.1; _push_aggregation since
    1.12) -- the stop strategy's inference, a finalize with words aggregated since, the
    stop watchdog, and the session's end. Driven here through the brain's own
    hold_commits, so the check follows whatever writer the brain wraps.

    If it goes: a commit off the fenced path is not held for the cut it follows -- the
    reply the turn cut lands after the caller's words (the 2026-10-13 count-again
    call), and the gate is armed for a reply the fence never saw. (This is what the 1.12
    bump changed: _maybe_emit_user_turn_stopped stopped calling push_aggregation.)"""
    context, pair, _ = _user_pair(user_turn_stop_timeout=0.3)
    user = pair.user()
    fence = CutReplyFence()
    fence.hold_commits(user)
    fenced = []

    async def wait_for_cuts():
        fenced.append(user.aggregation_string())
        return True
    fence.wait_for_cuts = wait_for_cuts
    starts, below = _turn_starts(user), Tap()

    def written_up_to(n):
        return lambda: len(_user_msgs(context)) >= n
    async with Run(user, below) as run:
        await _open_turn(run, starts, "one")
        await run.send(StopNow())                                        # inference + stop
        await _until(written_up_to(1))
        await _open_turn(run, starts, "two")
        await run.send(InferNow())                                       # the inference alone
        await _until(written_up_to(2))
        await run.send(_final("three"), FinalizeNow())                   # words after it
        await _until(written_up_to(3))
        await _open_turn(run, starts, "four")                            # ... the watchdog
        await _until(written_up_to(4))
        await _open_turn(run, starts, "five")
        await run.send(EndFrame())                                       # the session's end
        await run.ended()
    written = _user_msgs(context)
    assert written == ["one", "two", "three", "four", "five"], written
    assert fenced == written, (
        f"commits that bypassed the fence: {[w for w in written if w not in fenced]}")


async def _session_end(ending):
    """An open turn with words in hand, then the session's end -- `ending` "End" (an
    EndFrame down the pipeline) or "Cancel" (PipelineTask.cancel, a hang-up's
    CancelFrame). Returns the order the aggregator pushed the end frame and ran its
    session-end commit in, and the user messages committed."""
    from pipecat.frames.frames import CancelFrame
    context, pair, _ = _user_pair()
    user = pair.user()
    log = []
    push_frame, emit = user.push_frame, user._maybe_emit_user_turn_stopped

    async def logged_push(frame, direction=DOWN):
        if isinstance(frame, (EndFrame, CancelFrame)):
            log.append(f"{type(frame).__name__} pushed")
        await push_frame(frame, direction)

    async def logged_emit(*args, **kwargs):
        # (strategy, on_session_end, ...): positional or by name, on both versions.
        if kwargs.get("on_session_end") or (len(args) > 1 and args[1]):
            log.append("session-end commit")
        return await emit(*args, **kwargs)
    user.push_frame, user._maybe_emit_user_turn_stopped = logged_push, logged_emit
    starts = _turn_starts(user)
    async with Run(user, Tap()) as run:
        await _open_turn(run, starts, "and one more thing")
        assert await _until(lambda: user.aggregation_string()), "the words were not aggregated"
        if ending == "End":
            await run.send(EndFrame())
        else:
            await run.task.cancel()
        await run.ended()
    return log, _user_msgs(context)


async def check_the_session_end_commit_follows_the_end_frame():
    """UserTurn.tla, `Hangup` (NoLostGoodbye): in cascade (non-realtime) mode the user
    aggregator pushes the EndFrame on and THEN commits an open turn's leftover words,
    through _maybe_emit_user_turn_stopped(on_session_end=True) (LLMUserAggregator.
    process_frame -> _stop). hold_commits' `ending` flag is set from that call's
    on_session_end, so a session-end commit never sends the continuation's interruption
    past the End. Driven on the real aggregator (test_barge_commit_order.py's
    test_the_session_end_commit_never_drops_the_reply_ahead_of_the_end pins the
    brain's half).

    If it goes: a session-end commit that no longer says on_session_end reads as a
    continuation, and its interruption overtakes the End and drops the goodbye queued
    ahead of it; one that stopped committing loses the caller's last words."""
    log, said = await _session_end("End")
    assert log == ["EndFrame pushed", "session-end commit"], log
    assert said == ["and one more thing"], said


async def check_the_session_end_commit_runs_on_a_cancel_too():
    """UserTurn.tla, `Hangup` (NoLostGoodbye), the hang-up's other path: a CancelFrame
    (PipelineTask.cancel) commits the leftover words through the same
    _maybe_emit_user_turn_stopped(on_session_end=True) (LLMUserAggregator._cancel) --
    BEFORE the CancelFrame is pushed on, the reverse of the End's order. The flag is
    what keeps the fence from continuing it; the order does not matter there, since a
    cancel drops everything queued behind it anyway.

    If it goes: as for the End -- a cancel's commit taken for a continuation, or the
    words lost."""
    log, said = await _session_end("Cancel")
    assert log == ["session-end commit", "CancelFrame pushed"], log
    assert said == ["and one more thing"], said


# ---- UserTurn.tla, SttCommit.tla: the frame processor's queues -----------------------


class Holder(FrameProcessor):
    """Passes everything on; records what entered through queue_frame (where
    ReplyHoldGate sees the session's End before it queues)."""

    def __init__(self):
        super().__init__()
        self.entered = []

    async def queue_frame(self, frame, direction=DOWN, callback=None):
        self.entered.append(frame)
        await super().queue_frame(frame, direction, callback)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

    def in_hand(self):
        """The frame the paused process task has dequeued and is holding (pipecat's
        own private slot: the only witness to "dequeued, waiting on the pause")."""
        return getattr(self, "_FrameProcessor__process_current_frame", None)

    async def hold(self, run, frame):
        """Pause, and send `frame` for the process task to take in hand and wait on."""
        await self.pause_processing_frames()
        await run.send(frame)
        assert await _until(lambda: self.in_hand() is frame), "the frame never came in hand"

    async def queue(self, run, *frames):
        """Send `frames` and wait until each has entered this processor, in order --
        before anything sent after them. (Sent together with an InterruptionFrame, the
        task's source would let that SystemFrame overtake them on its way here.)"""
        await run.send(*frames)
        assert await _until(lambda: all(any(e is f for e in self.entered) for f in frames))


def _queued_reply():
    """What sits in a held queue: a reply's frames and its tool round, an LLM run, and
    the STT's bookkeeping frames."""
    return [TTSAudioRawFrame(b"\x01\x00" * 240, 24000, 1, context_id="c"),
            LLMRunFrame(),
            LLMContextFrame(LLMContext([{"role": "user", "content": "q"}])),
            FunctionCallInProgressFrame(function_name="f", tool_call_id="t", arguments={}),
            FunctionCallResultFrame(function_name="f", tool_call_id="t", arguments={},
                                    result={"ok": 1}),
            FinalTranscriptionFrame("the caller's words", "u", "t", stop_n=1),
            SegmentDoneFrame(stop_n=2) if _takes_stop_n(SegmentDoneFrame) else SegmentDoneFrame(),
            LLMTextFrame("more")]


def _takes_stop_n(cls):
    import dataclasses
    return any(f.name == "stop_n" for f in dataclasses.fields(cls))


KEPT = ["FunctionCallInProgressFrame", "FunctionCallResultFrame",
        "FinalTranscriptionFrame", "SegmentDoneFrame"]
QUEUED = (TTSAudioRawFrame, LLMRunFrame, LLMContextFrame, FunctionCallInProgressFrame,
          FunctionCallResultFrame, FinalTranscriptionFrame, SegmentDoneFrame, LLMTextFrame)


async def check_the_interruption_reset_keeps_only_uninterruptible_frames():
    """UserTurn.tla, HOLD = "gateKeepsTool" (NoLateToolRound) and the tool round's
    `Dropped`; SttCommit.tla's `toStrategy` FIFO; Followup.tla's `UserStart` ("the
    interruption flushes the queued LLMRunFrame"): an interruption's queue reset drops
    every queued frame but the uninterruptible ones -- pipecat's function-call frames
    and the brain's FinalTranscriptionFrame and segment frames -- (UserTurn.tla's
    POSTTOOL `CutLands`: a queued LLMContextFrame, the answer a tool's result asked for
    still on its way to the LLM, goes too) -- and, with an
    interruptible frame in hand, cancels and re-creates the process task, which lifts a
    pause.

    If it goes: if the function-call frames were dropped, ReplyHoldGate's own drop of a
    held tool round and its pass-through of a held result would act on frames that never
    come (an orphaned call); if a final were dropped, a MinWords interim's interruption
    would flush the caller's turn -- the SILENT TURN stt.py records; if the LLMRunFrame
    survived, a flushed follow-up would still run; if the LLMContextFrame did, a tool's
    answer asked just before a barge-in would run after it and be answered twice (#17,
    as seen on 1.7); if the pause outlived the reset, a
    processor paused by the reply hold would hold everything after a barge-in."""
    holder, below = Holder(), Tap()
    in_hand = TextFrame("in hand")
    async with Run(holder, below) as run:
        await holder.hold(run, in_hand)
        await holder.queue(run, *_queued_reply())
        assert not _kinds(below, DOWN, QUEUED), "the pause did not hold"
        await run.send(InterruptionFrame())
        # No resume_processing_frames(): only the reset can deliver anything now. A Mark
        # queued after the interruption comes through the re-created task behind
        # whatever the reset kept; if it never comes, the pause outlived the reset.
        mark = Mark()
        await run.send(mark)
        through = await _until(lambda: any(f is mark for _, f in below.down))
        got = _kinds(below, DOWN, QUEUED)
    assert through, ("nothing came through: the reset did not re-create the task, and the "
                     "pause outlived the interruption")
    assert [k for k in KEPT if k not in got] == [], (
        f"uninterruptible frames the reset dropped: {[k for k in KEPT if k not in got]}")
    assert [k for k in got if k not in KEPT] == [], (
        f"interruptible frames the reset kept: {[k for k in got if k not in KEPT]}")
    assert got == KEPT, f"kept out of order: {got}"
    assert not _texts(below, "in hand"), "the interruptible frame in hand was delivered"


async def check_an_uninterruptible_frame_in_hand_keeps_the_pause():
    """UserTurn.tla, HOLD = "gateNoResume" (NoSilencedBot): with an uninterruptible frame
    (a FunctionCallInProgressFrame) already dequeued by a paused process task, the
    interruption only resets the queue and leaves the task waiting on the pause. The
    shipped gate lifts the pause itself on every end of a hold, so it does not depend on
    this; the model's counterexample does.

    If it goes: nothing breaks in production; the gateNoResume row then describes a
    pipecat that no longer exists."""
    holder, below = Holder(), Tap()
    async with Run(holder, below) as run:
        await holder.hold(run, FunctionCallInProgressFrame(
            function_name="f", tool_call_id="t0", arguments={}))
        await holder.queue(run, *_queued_reply())
        await run.send(InterruptionFrame())
        # The interruption is pushed on only after the holder has handled it (the reset).
        assert await _until(lambda: _kinds(below, DOWN, InterruptionFrame))
        # What must NOT happen -- the paused task delivering anything -- has no event to
        # wait for: a bounded wait, many times the task's turnaround.
        await asyncio.sleep(0.1)
        held = _kinds(below, DOWN, QUEUED)
        assert held == [], f"the pause was lifted with an uninterruptible frame in hand: {held}"
        await holder.resume_processing_frames()
        want = ["FunctionCallInProgressFrame"] + KEPT
        await _until(lambda: len(_kinds(below, DOWN, QUEUED)) >= len(want))
    assert _kinds(below, DOWN, QUEUED) == want, _kinds(below, DOWN, QUEUED)


async def check_a_system_frame_passes_a_paused_queue_and_the_end_waits_in_it():
    """UserTurn.tla, `BotStart` ("an InterruptionFrame, a SystemFrame, overtakes every
    reply frame queued behind it") and HOLD = "gateEndWaits" (NoEndBehindHold: "the
    EndFrame, a ControlFrame, queues behind the held reply"): system frames are handled
    on the input task past a paused queue; the End waits in it. Every frame enters a
    processor through queue_frame, which is where ReplyHoldGate drops its hold the
    moment the End arrives.

    If it goes: an interruption queued behind a held reply could not cut it; and if the
    End stopped entering through queue_frame, the gate's drop would never see it and a
    hang-up would wait out the hold (Talk's close took 5.7 s before the fix)."""
    holder, below = Holder(), Tap()
    async with Run(holder, below) as run:
        await holder.hold(run, TextFrame("held"))
        await run.send(InterruptionFrame())
        assert await _until(lambda: _kinds(below, DOWN, InterruptionFrame)), (
            "an interruption waited behind a pause")
        assert not _texts(below, "held"), "the held frame went ahead of the interruption"
        await holder.hold(run, TextFrame("held again"))
        end = EndFrame()
        await holder.queue(run, end)
        # The End must NOT pass the pause, and nothing marks its not passing: a bounded
        # wait, many times a frame's turnaround.
        await asyncio.sleep(0.1)
        assert not _kinds(below, DOWN, EndFrame), "the End overtook a paused queue"
        await holder.resume_processing_frames()
        await run.ended()
    assert _texts(below, "held again") and _kinds(below, DOWN, EndFrame), (
        "the End and the frame ahead of it did not follow the resume")


def _texts(tap, text):
    return [f for _, f in tap.down if getattr(f, "text", None) == text]


# ---- UserTurn.tla, Followup.tla: the tool round --------------------------------------


async def check_an_interruption_cancels_a_running_call_as_CANCELLED():
    """UserTurn.tla, the tool round's `Dropped` (a round still running is cancelled) and
    HOLD = "gateNoFoldCancelled" (NoSplitTurn): an interruption reaching the LLM service
    cancels a running call registered cancel_on_interruption (the default every brain
    tool uses) and the assistant aggregator writes CANCELLED over its IN_PROGRESS --
    exactly the shape HeardContextCorrector._merge_tail folds across
    (heard_context._cancelled_rounds), asserted here in the corrector's own terms. No
    output transport between the two: the race with one (#185) is
    test_pipecat_function_call_race.py's.

    If it goes: a cancelled round no longer reads CANCELLED, the merge stops folding
    across it, and the caller's fragment and continuation stay two turns."""
    llm = _llm()
    entered = asyncio.Event()

    async def lookup(params):
        entered.set()
        await asyncio.Event().wait()
    llm.register_function("lookup", lookup)
    context = LLMContext([{"role": "user", "content": "look it up"}])
    assistant = LLMContextAggregatorPair(context).assistant()
    async with Run(llm, assistant) as run:
        await llm.run_function_calls([FunctionCallFromLLM(
            function_name="lookup", tool_call_id="call_1", arguments={}, context=context)])
        await asyncio.wait_for(entered.wait(), STEP_TIMEOUT_S)

        def tool():
            return [m.get("content") for m in context.get_messages() if m.get("role") == "tool"]
        assert await _until(lambda: tool() == ["IN_PROGRESS"]), context.get_messages()
        await run.send(InterruptionFrame())
        await _until(lambda: tool() != ["IN_PROGRESS"])
    assert tool() == ["CANCELLED"], context.get_messages()
    assert _cancelled_rounds(context.get_messages()), (
        "the corrector does not read this round as cancelled")


async def check_run_llm_resolves_properties_then_frame_then_yes():
    """UserTurn.tla, `ToolDone` (the async consult's placeholder, run_llm=False) and
    the gate's held result (NoOrphanCall); Followup.tla's `ToolEnd` (`_turn` cleared by
    a result that runs no inference): the assistant aggregator asks for an inference
    after a result by properties.run_llm if set, else frame.run_llm if set, else yes.
    tools.no_inference() sets the properties; ReplyHoldGate sets both; FollowupGate
    reads False on either. (test_reply_hold.py, test_a_held_result_whose_call_passed_
    settles_it_without_an_answer, pins the gate's frame through the real aggregator.)

    If it goes: the consult's placeholder runs an inference (the model invents an
    answer it does not have), or the dropped reply's result asks again; or a gate that
    reads False where pipecat runs one waits out a turn nobody is answering."""
    context = LLMContext([{"role": "user", "content": "q"}])
    below = Tap()
    assistant = LLMContextAggregatorPair(context).assistant()
    asked = []                                  # the aggregator's pushes, as it makes them
    push_frame = assistant.push_frame

    async def recording(frame, direction=DOWN):
        if isinstance(frame, LLMContextFrame) and direction == UP:
            asked.append(frame)
        await push_frame(frame, direction)
    assistant.push_frame = recording
    cases = [("neither set", None, None, True),
             ("tools.no_inference()", None, no_inference(), False),
             ("frame.run_llm=False", False, None, False),
             ("properties win over the frame", False,
              FunctionCallResultProperties(run_llm=True), True)]
    got = {}
    async with Run(assistant, below) as run:
        for i, (name, frame_run_llm, props, _) in enumerate(cases):
            cid = f"t{i}"
            before = len(asked)
            # Started is a SystemFrame: sent on its own, so it is not overtaken.
            await run.send(FunctionCallsStartedFrame(function_calls=[FunctionCallFromLLM(
                function_name="f", tool_call_id=cid, arguments={}, context=None)]))
            await _marked(run, below)
            await run.send(
                FunctionCallInProgressFrame(function_name="f", tool_call_id=cid,
                                            arguments={}, cancel_on_interruption=True),
                FunctionCallResultFrame(
                    function_name="f", tool_call_id=cid, arguments={}, result={"ok": i},
                    run_llm=frame_run_llm, properties=props))
            await _marked(run, below)               # the result has been handled
            got[name] = len(asked) > before
    want = {name: ran for name, _, _, ran in cases}
    assert got == want, f"inference after a result: {got}, expected {want}"


async def check_the_answer_to_a_result_is_pushed_up_unless_a_turn_is_open():
    """UserTurn.tla, POSTTOOL (#17), `PushAnswer`: the assistant aggregator asks for the
    answer to a tool's result by pushing an LLMContextFrame UPSTREAM -- past
    FollowupGate, which sits above it -- and skips it for an open user TURN
    (UserStartedSpeakingFrame), however long the VAD has heard the caller. It defers
    the push to BotStoppedSpeakingFrame while the bot is audible, and pushes then under
    the same rule.

    If it goes: if the push stopped going upstream (or went another way), FollowupGate
    would never see the answer and the caller's speech could be talked over again; if
    pipecat started skipping it on the VAD, the gate's hold is dead weight; if it
    stopped skipping it for an open turn, the turn's commit and the answer would both
    run -- the result answered twice."""
    def result(cid):
        return [FunctionCallInProgressFrame(function_name="f", tool_call_id=cid,
                                            arguments={}, cancel_on_interruption=True),
                FunctionCallResultFrame(function_name="f", tool_call_id=cid, arguments={},
                                        result={"ok": cid})]

    def started(cid):
        return FunctionCallsStartedFrame(function_calls=[FunctionCallFromLLM(
            function_name="f", tool_call_id=cid, arguments={}, context=None)])

    context = LLMContext([{"role": "user", "content": "q"}])
    below = Tap()
    assistant = LLMContextAggregatorPair(context).assistant()
    asked = []
    push_frame = assistant.push_frame

    async def recording(frame, direction=DOWN):
        if isinstance(frame, LLMContextFrame):
            asked.append(direction)
        await push_frame(frame, direction)
    assistant.push_frame = recording
    async with Run(assistant, below) as run:
        # The VAD hears the caller; no turn has started.
        await run.send(VADUserStartedSpeakingFrame(), started("a"))
        await _marked(run, below)
        await run.send(*result("a"))
        await _marked(run, below)
        under_vad = list(asked)
        # The bot audible: deferred, then pushed at its stop.
        await run.send(BotStartedSpeakingFrame(), started("b"))
        await _marked(run, below)
        await run.send(*result("b"))
        await _marked(run, below)
        deferred = list(asked)
        await run.send(BotStoppedSpeakingFrame())
        await _marked(run, below)
        at_stop = list(asked)
        # A turn open: skipped.
        await run.send(UserStartedSpeakingFrame(), started("c"))
        await _marked(run, below)
        await run.send(*result("c"))
        await _marked(run, below)
        in_turn = list(asked)
    assert under_vad == [UP], f"under VAD speech, no turn: {under_vad} (expected one push up)"
    assert deferred == [UP], f"with the bot audible the push was not deferred: {deferred}"
    assert at_stop == [UP, UP], f"the deferred push did not go up at the stop: {at_stop}"
    assert in_turn == [UP, UP], f"a turn was open and the answer was asked anyway: {in_turn}"


async def check_a_tool_result_reaches_the_gate_below_the_transport():
    """Followup.tla, `ToolEnd` (LATCH = "turnAware"): the LLM service broadcasts a
    function call's FunctionCallResultFrame, and it passes the TTS and the output
    transport, so FollowupGate (after transport.output(), before the assistant
    aggregator) sees a run_llm=False result and clears `_turn`: the async consult's
    placeholder ends the turn for the injector.

    If it goes: `_turn` stays set after the consult's placeholder until something else
    clears it, and the follow-up injector waits out its max_wait in silence."""
    llm = _llm()

    async def consult(params):
        await params.result_callback({"status": "working"}, properties=no_inference())
    llm.register_function("ask", consult)
    context = LLMContext([{"role": "user", "content": "ask it"}])
    below = Tap()
    async with Run(llm, _tts(), StubOutput(), below):
        await llm.run_function_calls([FunctionCallFromLLM(
            function_name="ask", tool_call_id="call_2", arguments={}, context=context)])
        await _until(lambda: _kinds(below, DOWN, FunctionCallResultFrame), 3.0)
    results = [f for _, f in below.down if isinstance(f, FunctionCallResultFrame)]
    assert results, "the result never got past the TTS and the transport"
    props = results[0].properties
    assert props is not None and props.run_llm is False, (
        f"the placeholder's run_llm did not survive the trip: {props}")


async def check_a_tool_calls_started_frame_is_a_system_frame_ahead_of_the_end():
    """UserTurn.tla, `ToolCall`/`ToolDone` leave the gate `armed` (only `BotStart` and
    `CutLands` disarm it): ReplyHoldGate stays armed across a tool round
    (reply_hold.process_frame) because the LLM broadcasts FunctionCallsStartedFrame, a
    SystemFrame, from inside _process_context (run_function_calls) -- before base_llm's
    `finally` pushes the completion's End. Driven through the real _process_context on
    a scripted stream whose one chunk is a tool call.

    If it goes: if the frame came after the End, or were a queued frame the End could
    pass, the gate would read the bare tool call's End as a reply with no audio and
    disarm, and the tool's answer would play unchecked over a caller who had resumed."""
    from types import SimpleNamespace as NS

    from pipecat.frames.frames import SystemFrame

    async def tool_call_stream():
        yield NS(usage=None, model=None, choices=[NS(finish_reason=None, delta=NS(
            content=None, tool_calls=[NS(index=0, id="call_4", function=NS(
                name="lookup", arguments="{}"))]))])

    class ToolCalling(BoundedOpenAILLMService):
        async def _open_stream(self, context):
            return tool_call_stream()

    llm, below = _llm(ToolCalling), Tap()

    async def lookup(params):
        await params.result_callback({"ok": 1}, properties=no_inference())
    llm.register_function("lookup", lookup)
    async with Run(llm, below) as run:
        await run.send(LLMContextFrame(LLMContext([{"role": "user", "content": "q"}])))
        await _until(lambda: _kinds(below, DOWN, LLMFullResponseEndFrame)
                     and _kinds(below, DOWN, FunctionCallsStartedFrame))
    order = _kinds(below, DOWN, (FunctionCallsStartedFrame, LLMFullResponseEndFrame))
    assert order == ["FunctionCallsStartedFrame", "LLMFullResponseEndFrame"], order
    started = [f for _, f in below.down if isinstance(f, FunctionCallsStartedFrame)][0]
    assert isinstance(started, SystemFrame), "FunctionCallsStartedFrame is no longer a SystemFrame"


async def check_a_cancelled_completion_never_reaches_the_ledger_as_a_finished_one():
    """Ledger.tla / LedgerPlayout.tla, `LlmCancelEnd` and `LlmCancelLost`: an
    interrupted completion's End is pushed from base_llm's `finally` when the
    interruption cancels it -- but the ledger reads the LLM stream only where the TTS
    processes it (TranscriptLedger.on_process_frame), below the LLM, and the LLM cancels
    the completion (its `finally` pushes the End) BEFORE it passes the interruption on.
    So the End reaches the TTS ahead of the interruption, as a ControlFrame on the TTS's
    queue: the TTS processes it (measured on 1.8.1 and 1.12), or the interruption's
    reset flushes it and the ledger never sees it. When it does see it, it must already
    have run _interrupt (at the interruption's first sighting, above the LLM) and so find
    no stream: nothing of the cut reply is queued as a finished response. The models
    have both branches: `LlmCancelEnd` and `LlmCancelLost`.

    Asserted AT that sighting, on the real TranscriptLedger built with the TTS (as
    agent_session builds it): the ledger's state right after it handles the End the TTS
    processed. Read afterwards, it would prove nothing -- the interruption's later
    sightings (at the TTS, below it) run _interrupt again and clear the stream and the
    queue whatever the End did.

    If it goes: an End the TTS processed ahead of the ledger's interrupt would close the
    cut reply as a finished response and queue its partial text for the next context
    to claim (ChartedTextMatchesContext)."""
    from teaport_brain.transcript_ledger import TranscriptLedger

    class Hanging(BoundedOpenAILLMService):
        async def _process_context(self, context):
            await self.push_frame(LLMTextFrame("Partial reply that never"))
            await asyncio.Event().wait()

    llm, tts, below = _llm(Hanging), _tts(), Tap()
    mark = Mark()

    class Watched(TranscriptLedger):
        """The ledger, with its state snapshotted the moment it has handled the End
        where the TTS processed it. Observers are each fed in order through their own
        queue, so this one's own sighting of the Mark below the TTS proves every earlier
        TTS sighting has reached it: an End it has not seen by then never came."""
        cancelled_end = None        # the End the LLM pushed for the cancelled completion
        at_end = None               # (_gen, list(_queue)) right after handling it
        mark_seen = False

        async def on_push_frame(self, data):
            if isinstance(data.frame, LLMFullResponseEndFrame) and data.source is llm:
                self.cancelled_end = data.frame
            await super().on_push_frame(data)

        async def on_process_frame(self, data):
            await super().on_process_frame(data)
            if data.processor is tts and data.frame is self.cancelled_end:
                self.at_end = (self._gen, list(self._queue))
            elif data.frame is mark and data.processor is below:
                self.mark_seen = True

    ledger = Watched(tts=tts)
    async with Run(Tap(), llm, tts, below, observers=[ledger]) as run:
        await run.send(LLMContextFrame(LLMContext([{"role": "user", "content": "q"}])))
        assert await _until(lambda: ledger._gen is not None
                            and ledger._gen["parts"]), "the TTS never saw the stream"
        await run.send(InterruptionFrame())
        assert await _until(lambda: ledger.cancelled_end is not None), (
            "the cancelled completion pushed no End")
        assert await _until(lambda: _kinds(below, DOWN, InterruptionFrame))
        await run.send(mark)
        assert await _until(lambda: ledger.at_end is not None or ledger.mark_seen), (
            "the ledger never caught up with the Mark")
    if ledger.at_end is None:
        print("    (the End never reached the ledger: LlmCancelLost)")
        return
    gen, queued = ledger.at_end
    assert gen is None and queued == [], (
        f"the cut reply reached the ledger as a finished response: stream {gen}, "
        f"queued {queued}")
    print("    (the TTS processed the End after the ledger's interrupt: LlmCancelEnd)")


# ---- SttCommit.tla: where the VAD runs -----------------------------------------------


async def check_vad_edges_are_pushed_upstream_to_the_stt():
    """SttCommit.tla, THE ORDER: the VAD runs in the user aggregator, downstream of the
    STT, and every edge it detects is queued for the aggregator's own strategies AND
    pushed upstream (_queued_broadcast_frame) -- the copy the STT commits on and counts
    (`_vad_stops`, the stamps the receipts and numbered verdicts match).

    If it goes: the STT never sees a VAD stop -- no commit at a stop, every segment
    closed by the backstop or the engine's own endpointer, and the stop counts the
    verdicts are numbered by drift apart."""
    vad = Scripted(stop_secs=0.2)
    _, pair, _ = _user_pair(vad_analyzer=vad)
    above = Tap()
    frame = b"\x00\x02" * CHUNK
    async with Run(above, pair.user(), Tap()) as run:
        for conf, n in ((0.9, 20), (0.0, 20)):
            vad.conf = conf
            for _ in range(n):
                await run.send(InputAudioRawFrame(audio=frame, sample_rate=16000,
                                                  num_channels=1))
                await asyncio.sleep(0.005)
        await _until(lambda: _kinds(above, UP, VADUserStoppedSpeakingFrame))
    edges = _kinds(above, UP, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame))
    assert edges == ["VADUserStartedSpeakingFrame", "VADUserStoppedSpeakingFrame"], edges


# ---- LedgerPlayout.tla, PlayoutClock.tla: the TTS ------------------------------------


async def check_the_tts_repushes_each_responses_own_end_after_its_context():
    """LedgerPlayout.tla, `TtsDrain` and `CanStart` (~Draining); PlayoutClock.tla's End
    at the last word's pts: after a context's TTSStoppedFrame the TTS re-pushes the
    response's OWN LLMFullResponseEndFrame (same id) stamped with the context's last
    word's pts, and only then starts the next context. The ledger names the response a
    context spoke from that re-push (_end_drained); ReplyHoldGate moves the End's pts
    with the words so the aggregator commits after them.

    If it goes: the ledger's drain signal is gone or names the wrong response (charts
    under the wrong text, NoPlayedReplyDeleted); an End stamped ahead of its words is
    released first by the transport and commits a truncated reply (pc_release_words)."""
    tts, probe = _tts(), Probe()
    ends = [LLMFullResponseEndFrame(), LLMFullResponseEndFrame()]
    async with Run(tts, probe) as run:
        await run.send(LLMFullResponseStartFrame(), LLMTextFrame("Alpha reply here."),
                       ends[0], LLMFullResponseStartFrame(),
                       LLMTextFrame("Bravo reply here."), ends[1])
        await _until(lambda: len(_of(probe, LLMFullResponseEndFrame)) >= 2)
    starts = _of(probe, TTSStartedFrame)
    assert len(starts) == 2, [f.context_id for _, f in starts]
    repushed = _of(probe, LLMFullResponseEndFrame)
    assert [f.id for _, f in repushed] == [e.id for e in ends], (
        "the re-pushed Ends are not the responses' own frames")
    for (i_start, start), (i_end, end) in zip(starts, repushed):
        ctx = start.context_id
        stops = _of(probe, TTSStoppedFrame, ctx)
        words = [(i, f) for i, f in _of(probe, TTSTextFrame, ctx) if i < i_end]
        assert stops and stops[0][0] < i_end, f"{ctx}: End re-pushed before its stop"
        assert words and end.pts == words[-1][1].pts, (
            f"{ctx}: End pts {end.pts}, last word's {words and words[-1][1].pts}")
    assert starts[1][0] > repushed[0][0], (
        "the second context started before the first's End was re-pushed")


async def _no_audio(text, eager=True):
    """An engine that yields nothing for the clause: every clause failed (an OOM, a
    restart) -- or nothing synthesizable in it."""
    await asyncio.sleep(0.01)
    yield "end", []


async def check_a_reply_with_no_audio_still_commits_its_text():
    """LedgerPlayout.tla, `said` and `Failed`/`Muted` (UnheardSaidIsCorrected,
    NoPlayedReplyDeleted): a context that ends without audio still has its text
    force-completed (_apply_force_complete) as TTSTextFrames, ahead of its End's
    re-push, and the assistant aggregator commits it. Utterance.committed and
    HeardContextCorrector._resolve count such a reply's message as there.

    If it goes: a failed reply's text is never committed, so the corrector counts one
    message too many and its cut lands on the reply before it -- one the caller heard."""
    text = "Hello there my friend."
    context = LLMContext([{"role": "user", "content": "hi"}])
    tts, probe = _tts(_no_audio), Probe()
    async with Run(tts, probe, LLMContextAggregatorPair(context).assistant()) as run:
        await run.send(LLMFullResponseStartFrame(), LLMTextFrame(text),
                       LLMFullResponseEndFrame())
        await _until(lambda: _of(probe, LLMFullResponseEndFrame)
                     and any(m.get("role") == "assistant" for m in context.get_messages()))
    assert not _of(probe, TTSAudioRawFrame), "the engine stub yielded audio"
    ctx = _of(probe, TTSStartedFrame)[0][1].context_id
    i_end = _of(probe, LLMFullResponseEndFrame)[0][0]
    words = [f for i, f in _of(probe, TTSTextFrame, ctx) if i < i_end]
    assert " ".join(f.text for f in words).split() == text.split(), (
        f"force-completed before the End: {[f.text for f in words]}")
    assert all(f.append_to_context for f in words), "the text is not for the context"
    said = [m.get("content") for m in context.get_messages() if m.get("role") == "assistant"]
    assert said == [text], f"committed: {said}"


async def check_a_timed_out_context_resumes_under_its_own_id():
    """LedgerPlayout.tla, `TtsRestart` (Resume; `ledger_split`'s question settled): a
    context that ends on the stop-frame timeout while the LLM still streams is
    re-created under the SAME context id when the LLM's next sentence reaches the TTS,
    so the ledger's _turn_for(ctx) finds the turn it opened.

    If it goes: one reply is split across two context ids -- the chained-branch premature
    chart Ledger.tla's `ledger_split` row shows, which LedgerPlayout does not model."""
    tts, probe = _tts(stop_timeout=0.4), Probe()
    async with Run(tts, probe) as run:
        await run.send(LLMFullResponseStartFrame(), LLMTextFrame("First sentence here. A"))
        await _until(lambda: _of(probe, TTSStoppedFrame), timeout=3.0)
        assert _of(probe, TTSStoppedFrame), "the context never timed out"
        await run.send(LLMTextFrame("nd the second one."), LLMFullResponseEndFrame())
        await _until(lambda: len(_of(probe, TTSStartedFrame)) >= 2, timeout=3.0)
    ids = [f.context_id for _, f in _of(probe, TTSStartedFrame)]
    assert len(ids) == 2 and ids[0] == ids[1], f"contexts opened: {ids}"


# ---- PlayoutClock.tla, LedgerPlayout.tla: the output transport ---------------------


async def check_the_transport_releases_timed_frames_by_pts_then_arrival():
    """PlayoutClock.tla, `Pop`: BaseOutputTransport puts every frame with a pts on a
    priority queue keyed (pts, arrival) and releases each when its clock reaches it --
    not in arrival order. That is why an End stamped earlier than words queued before
    it is released first (pc_release_words) and why ReplyHoldGate moves every pts of a
    held context by the same shift.

    If it goes: released in arrival order, the gate's restamping would be moot and its
    word timing (WordsOnTime) would be measured against a clock that does not exist."""
    out, probe = StubOutput(), Probe()
    async with Run(out, probe) as run:
        now = out.get_clock().get_time()
        late = [word(w, "c", now + int(d * 1e9)) for w, d in (("one", 0.3), ("two", 0.3))]
        end = LLMFullResponseEndFrame()
        end.pts = now + int(0.1e9)
        await run.send(*late, end)
        await _until(lambda: len(_of(probe, (TTSTextFrame, LLMFullResponseEndFrame))) >= 3)
        released = [(type(f).__name__, getattr(f, "text", None), ts)
                    for ts, f in probe.seen if isinstance(f, (TTSTextFrame,
                                                              LLMFullResponseEndFrame))]
    assert [(k, t) for k, t, _ in released] == [
        ("LLMFullResponseEndFrame", None), ("TTSTextFrame", "one"), ("TTSTextFrame", "two")
    ], released


def _played(probe):
    """What the transport pushed on after writing it."""
    from pipecat.frames.frames import OutputAudioRawFrame
    return [f for _, f in probe.seen if isinstance(f, OutputAudioRawFrame)]


async def check_the_transport_repushes_played_audio_untagged():
    """Ledger.tla / LedgerPlayout.tla environment ("the transport's untagged rebuild of
    each chunk it played is recognised and ignored"): what the output transport pushes on
    after writing a chunk carries no context_id, so the ledger, which opens turns only on
    the TTS's tagged frames, ignores it.

    If it goes: tagged copies below the transport look like the TTS's own chunks -- a
    reply's audio counted twice into its layout, or a filler's copy opening a turn
    (the `ledger_phantom` shape)."""
    out, probe = StubOutput(), Probe()
    async with Run(out, probe) as run:
        await run.send(TTSStartedFrame(context_id="c"),
                       TTSAudioRawFrame(b"\x01\x00" * 4800, 24000, 1, context_id="c"))
        await _until(lambda: sum(len(f.audio) for f in _played(probe)) >= 9600)
    played = _played(probe)
    assert sum(len(f.audio) for f in played) >= 9600, (
        "the transport did not push on all it wrote")
    tagged = [getattr(f, "context_id", None) for f in played
              if getattr(f, "context_id", None) is not None]
    assert not tagged, f"played chunks re-pushed with a context: {tagged}"


# ---- UserTurn.tla, PlayoutClock.tla: the assistant aggregator ------------------------


async def check_the_assistant_aggregator_commits_at_the_interruption_and_the_end():
    """UserTurn.tla, `CutLands` (the reply a barge-in cut is committed by the assistant
    aggregator when the InterruptionFrame reaches it -- CutReplyFence waits for that
    frame past the aggregator); PlayoutClock.tla, `Pop` (the aggregator commits the words
    it was handed at the End, and words handed after the End belong to the next
    message). Words are what played: the transport hands them on at their pts.

    If it goes: if the cut reply were not committed at the interruption, the fence's
    wait would order nothing; if the End no longer closed the message, the release
    probe's truncated commit (pc_release_words) would change shape."""
    context = LLMContext([{"role": "user", "content": "q"}])
    assistant = LLMContextAggregatorPair(context).assistant()

    def said():
        return [m.get("content") for m in context.get_messages() if m["role"] == "assistant"]
    below = Tap()
    async with Run(assistant, below) as run:
        await run.send(LLMFullResponseStartFrame(), word("Sure,", "c1", 1), word("here", "c1", 2))
        await _marked(run, below)
        assert said() == [], "committed before the cut"
        await run.send(InterruptionFrame())
        await _until(lambda: said())
        assert said() == ["Sure, here"], said()
        await run.send(LLMFullResponseStartFrame(), word("One", "c2", 1),
                       LLMFullResponseEndFrame(), word("two", "c2", 2), word("three", "c2", 3))
        await _marked(run, below)
        assert said() == ["Sure, here", "One"], said()
        await run.send(LLMFullResponseStartFrame(), word("Next.", "c3", 1),
                       LLMFullResponseEndFrame())
        await _marked(run, below)
    assert said() == ["Sure, here", "One", "two three Next."], (
        f"the words after the End did not leak into the next message: {said()}")


# ---- 1.12 only: speculative inference (UserTurn.tla's fence, NoLostGoodbye) ----------


class Speculating(ScriptedStop):
    async def process_frame(self, frame):
        if isinstance(frame, SpeculateNow):
            await self.trigger_user_turn_inference_triggered(
                speculation=UserTurnSpeculation(text=frame.text))
        elif isinstance(frame, ConfirmNow):
            await self.trigger_user_turn_finalized(confirms_speculation=True)
        return await super().process_frame(frame)


class SpeculateNow(DataFrame):
    def __init__(self, text):
        super().__init__()
        self.text = text


class ConfirmNow(DataFrame):
    pass


async def check_a_speculative_inference_commits_nothing():
    """1.12 only. UserTurn.tla's fence (`CommitStep`) and the reply hold's arming (the
    gate is armed by on_user_turn_message_added): a speculative inference runs on a
    provisional copy of the context (llm_response_universal._run_speculative_inference)
    and commits nothing -- no user message, no on_user_turn_message_added, the
    aggregation kept for the turn's real end. Latent: no brain stop strategy speculates
    (check_the_brain_never_speculates).

    If it goes: a speculation would write the caller's words twice and arm the gate for
    a reply nobody confirmed."""
    if UserTurnSpeculation is None:
        raise NotHere("pipecat has no speculative inference before 1.9")
    context, pair, _ = _user_pair(stop=Speculating())
    user, below = pair.user(), Tap()
    added = []

    @user.event_handler("on_user_turn_message_added")
    async def _added(_a, message):
        added.append(message.content)
    starts = _turn_starts(user)
    async with Run(user, below) as run:
        await _open_turn(run, starts, "what time is it")
        await run.send(SpeculateNow("what time is it"))
        await _marked(run, below)
        frames = [f for _, f in below.down if isinstance(f, LLMContextFrame)]
        assert len(frames) == 1 and getattr(frames[0], "speculation", False), frames
        assert frames[0].context is not context, "the speculation ran on the real context"
        assert _user_msgs(context) == [] and added == [], (_user_msgs(context), added)
        assert user.aggregation_string() == "what time is it", "the aggregation was spent"


async def check_a_confirming_stop_commits_without_inference():
    """1.12 only. UserTurn.tla, `Hangup`/`CommitStep` as 0aa019b's fence reads them: a
    stop that confirms a speculation writes the turn to the context with run_llm=False
    (_maybe_emit_user_turn_stopped(strategy, run_llm=not params.confirms_speculation) ->
    _push_aggregation(run_llm=False)): the reply it confirms is already generated, so no
    LLMContextFrame goes down. CutReplyFence.hold_commits does not continue (merge) such
    a commit. Latent, as above.

    If it goes: a confirmed speculation would be answered twice, or the fence would
    drop a reply that answers the turn."""
    if UserTurnSpeculation is None:
        raise NotHere("pipecat has no speculative inference before 1.9")
    context, pair, _ = _user_pair(stop=Speculating())
    user, below = pair.user(), Tap()
    starts = _turn_starts(user)
    async with Run(user, below) as run:
        await _open_turn(run, starts, "what time is it")
        await run.send(SpeculateNow("what time is it"))
        await _marked(run, below)
        before = [f for _, f in below.down if isinstance(f, LLMContextFrame)]
        await run.send(ConfirmNow())
        await _marked(run, below)
        after = [f for _, f in below.down if isinstance(f, LLMContextFrame)]
    assert _user_msgs(context) == ["what time is it"], context.get_messages()
    assert len(after) == len(before), "a confirming stop asked the LLM again"


async def check_the_brain_never_speculates():
    """1.12 only, and structural: the two speculation premises above are latent because
    no stop strategy the brain builds speculates -- pipecat's only one is
    EagerUserTurnStopStrategy, and the brain's LateStartTurnStopStrategy is a
    TurnAnalyzerUserTurnStopStrategy -- and the brain constructs no UserTurnSpeculation.

    If it goes: the speculation rows the models do not have (no `SpecStart` of pipecat's
    own) become live; model them before shipping it."""
    if UserTurnSpeculation is None:
        raise NotHere("pipecat has no speculative inference before 1.9")
    from pipecat.turns.user_stop.eager_user_turn_stop_strategy import (
        EagerUserTurnStopStrategy,
    )

    from teaport_brain.endpointing import LateStartTurnStopStrategy
    assert not issubclass(LateStartTurnStopStrategy, EagerUserTurnStopStrategy)
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "teaport_brain")
    offenders = []
    for here, _dirs, names in os.walk(root):
        for name in sorted(names):
            if name.endswith(".py"):
                with open(os.path.join(here, name), encoding="utf-8") as f:
                    src = f.read()
                if "EagerUserTurnStopStrategy" in src or "UserTurnSpeculation(" in src:
                    offenders.append(os.path.relpath(os.path.join(here, name), root))
    assert not offenders, f"the brain speculates now: {offenders}"


CHECKS = [v for k, v in list(globals().items()) if k.startswith("check_")]


def main():
    failed, skipped = [], []

    async def run_all():
        for check in CHECKS:
            try:
                await check()
            except NotHere as e:
                skipped.append(check.__name__)
                print(f"  skip {check.__name__}: {e}")
            except Exception as e:  # noqa: BLE001 -- report every premise, then fail
                failed.append(check.__name__)
                print(f"  FAIL {check.__name__}: {type(e).__name__}: {e}")
            else:
                print(f"  ok {check.__name__}")
    asyncio.run(run_all())
    if failed:
        print(f"{len(failed)} premise(s) failed: {', '.join(failed)}")
        sys.exit(1)
    print("ALL PASS" + (f" ({len(skipped)} newer-pipecat premise(s) skipped)" if skipped else ""))


if __name__ == "__main__":
    main()
