#
# Unit test: the STT commit follows Smart Turn's verdict, not the raw VAD stop (#43).
#
# A commit is irreversible on the engine side -- finish() right-pads and redecodes the
# buffer, then resets the stream -- so a commit sent at a mid-sentence breath does not
# just split the turn's text, it hands the rest of the sentence to a decoder with no
# context. Measured 2026-09-18 with real caller audio and a 350 ms pause inserted at a
# natural dip: at ENDPOINT_STOP_SECS=0.2, 4 of 4 runs split ("Hey, what's" +
# "Time is here."), one fragment came back EMPTY, and Smart Turn had called every first
# fragment INCOMPLETE -- it held the pipecat turn open, but the segment was already
# closed underneath it.
#
# So TeaportSTTService no longer commits on VADUserStoppedSpeakingFrame. The stop
# strategy answers every VAD stop with a TurnVerdictFrame pushed upstream (its model's
# answer, and again when an INCOMPLETE answer falls through to the silence ceiling), and
# the STT commits on a complete one. What these pin, in order:
#
#   * a COMPLETE verdict commits; the VAD stop alone does not;
#   * an INCOMPLETE verdict holds the segment open, and the caller's resumed words land
#     in the SAME segment -- one commit for the whole utterance;
#   * the ceiling's verdict commits a held segment;
#   * a VAD stop over the bot's own voice commits at once (that flush is the barge-in),
#     and the verdict that follows does not commit again;
#   * a held segment whose verdict never arrives expires into a commit with a warning,
#     a verdict that then arrives is logged as late rather than lost, and the
#     session-wide fallback to vad-stop is undone by the first verdict that arrives;
#   * a done the engine sends on its own releases a held segment, and a stop after
#     such a close commits the tail rather than hold it;
#   * the stranded backstop stands down while a segment is held, and stands back up
#     when the caller resumes;
#   * the commit's cancel of a live hold is awaited, and comes before the bookkeeping;
#   * vad-stop mode is the old behaviour, verdict frames and all;
#   * and, over the REAL controller and the REAL LateStartTurnStopStrategy, the frames
#     the STT relies on are actually produced: the model's at the VAD stop, the
#     ceiling's when an INCOMPLETE one times out, and nothing on a COMPLETE one --
#     including when the stop, or the ceiling, ends the turn INLINE because the final
#     was already in hand (the flag the push used to read is reset by then), and when
#     the turn is closed under an INCOMPLETE verdict by something other than the
#     ceiling (a re-applied stop, the watchdog), which reports the close so the held
#     segment goes with it.
#
# Run: python test_stt_commit_on_verdict.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from loguru import logger  # noqa: E402
from pipecat.audio.turn.smart_turn.base_smart_turn import (  # noqa: E402
    BaseSmartTurn,
    SmartTurnParams,
)
from pipecat.clocks.system_clock import SystemClock  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
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
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup  # noqa: E402
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_turn_controller import UserTurnController  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402
from pipecat.utils.asyncio.task_manager import TaskManager  # noqa: E402

from stt_harness import WireRecorder  # noqa: E402
from turn_harness import running_controller  # noqa: E402

import teaport_brain.stt as stt_mod  # noqa: E402
from teaport_brain.captions import UserTranscriptEmitter  # noqa: E402
from teaport_brain.endpointing import (  # noqa: E402
    INTERRUPT_MIN_WORDS,
    LateStartTurnStopStrategy,
    TurnVerdictFrame,
)
from teaport_brain.stt import TeaportSTTService  # noqa: E402
from teaport_brain.turn_timing import TurnTimer  # noqa: E402

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)
STOP_SECS = 0.2

# The ceiling and the backstop, shortened so the suite stays quick. The STT reads both
# from its module namespace, so patching there is what production would see with the
# env set that way.
CEILING = 0.1
QUIET = 0.05

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


class Recorder(TeaportSTTService):
    """The real service with the wire recorded instead of sent; frames captured."""

    def __init__(self, **kwargs):
        # Explicit, not the module default: that is read from TEAPORT_STT_COMMIT_ON at
        # import, and a box set to vad-stop is one of the places this suite runs.
        # test_vad_stop_mode_is_the_old_behaviour passes its own.
        kwargs.setdefault("commit_on", "verdict")
        super().__init__(url="ws://127.0.0.1:1/none", **kwargs)
        self._websocket = WireRecorder()
        self.pushed = []
        self._whys = []

    async def push_frame(self, frame, direction=DOWN):
        self.pushed.append(frame)

    async def stop_processing_metrics(self):
        pass

    def create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro)

    async def cancel_task(self, task, timeout=None):
        task.cancel()

    def commit_whys(self):
        """The commits on the wire, by the reason the segment line would record. The
        wire carries only {"type", "final"}, so the reason is read off the service at
        each commit -- _send_commit records it before sending."""
        return self._whys

    async def _send_commit(self, final=True, why="other"):
        self._whys.append(why)
        await super()._send_commit(final=final, why=why)


async def vad_stop(s):
    await s.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS), DOWN)


async def vad_start(s):
    await s.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2), DOWN)


async def verdict(s, complete, source="verdict"):
    await s.process_frame(TurnVerdictFrame(complete=complete, source=source), UP)


async def delta(s, text):
    await s._handle_message({"type": "transcription.delta", "delta": text})


async def done(s, text):
    await s._handle_message({"type": "transcription.done", "text": text})


# ---- the STT alone ----------------------------------------------------------------

async def test_the_vad_stop_alone_commits_nothing_and_a_complete_verdict_commits():
    s = Recorder()
    await delta(s, "what time is it")
    await vad_stop(s)
    assert s._websocket.commits() == [], "committed on the raw VAD stop"
    await verdict(s, True)
    assert len(s._websocket.commits()) == 1
    assert s.commit_whys() == ["verdict"]
    assert s._hold_task is None, "the hold's expiry must be cancelled by the commit"


async def test_an_incomplete_verdict_holds_and_the_resumed_words_join_the_segment():
    s = Recorder()
    await delta(s, "hey, what's")
    await vad_stop(s)
    await verdict(s, False)
    assert s._websocket.commits() == [], "committed on an INCOMPLETE verdict"
    # The caller resumes: the stop that was pending is answered by their own voice.
    await vad_start(s)
    assert s._commit_pending is False
    await delta(s, " the time?")
    await vad_stop(s)
    await verdict(s, True)
    assert len(s._websocket.commits()) == 1, "one utterance, one commit"
    await done(s, "hey, what's the time?")
    finals = [f.text for f in s.pushed if getattr(f, "finalized", False)]
    assert finals == ["hey, what's the time?"]


async def test_the_ceilings_verdict_commits_a_held_segment():
    s = Recorder()
    await delta(s, "so I was thinking")
    await vad_stop(s)
    await verdict(s, False)
    await verdict(s, True, source="ceiling")
    assert s.commit_whys() == ["ceiling"]


async def test_a_stale_complete_verdict_after_the_caller_resumed_is_ignored():
    """The model's answer can land after the caller has already started again (the
    frame crosses four queues; the VAD start crosses none). It answers a stop that is
    no longer pending, so it must not close the segment under the new words."""
    s = Recorder()
    await delta(s, "hey")
    await vad_stop(s)
    await vad_start(s)
    await verdict(s, True)
    assert s._websocket.commits() == [], "a verdict for an answered stop closed the segment"


async def test_a_vad_stop_over_the_bot_commits_at_once_and_the_verdict_does_not_repeat_it():
    s = Recorder()
    await s.process_frame(BotStartedSpeakingFrame(), UP)
    await vad_stop(s)
    assert s.commit_whys() == ["bot-speaking"], "the flush is the barge-in; it must not wait"
    await verdict(s, True)
    assert s.commit_whys() == ["bot-speaking"], "the verdict re-committed a flushed segment"
    await s.process_frame(BotStoppedSpeakingFrame(), UP)
    await vad_stop(s)
    assert s.commit_whys() == ["bot-speaking"], "with the bot quiet the stop must hold again"
    await s._cancel_hold_expiry()          # leave no timer behind for the next test


async def test_a_held_segment_whose_verdict_never_comes_expires_into_a_commit():
    s = Recorder()
    warnings = []
    sink = logger.add(lambda m: warnings.append(m), level="WARNING")
    try:
        await delta(s, "hello")
        await vad_stop(s)
        await verdict(s, False)
        await asyncio.sleep(CEILING + stt_mod._HOLD_SLACK_SECS + 0.05)
        assert s.commit_whys() == ["hold-expired"], "a held segment must not stay open forever"
        assert any("hold expired" in w for w in warnings), "an expiry must be loud"
    finally:
        logger.remove(sink)


async def test_a_session_that_never_sees_a_verdict_stops_holding():
    """A stop strategy that never answers (not the brain's, or a model that raises)
    would otherwise cost every turn the full hold. The first expiry with no verdict
    ever seen falls the service back to vad-stop; one that follows earlier verdicts
    is a transient and does not."""
    s = Recorder()
    await delta(s, "hello")
    await vad_stop(s)
    await asyncio.sleep(CEILING + stt_mod._HOLD_SLACK_SECS + 0.05)
    assert s.commit_whys() == ["hold-expired"]
    assert s.commit_on == "vad-stop", "a session nothing answers must stop holding"
    await vad_stop(s)
    assert s.commit_whys() == ["hold-expired", "vad-stop"]
    # The wiring answers after all -- a first inference slower than the wait, say --
    # and the first verdict to land, whatever it says, puts the commit back on the
    # verdict: the next stop holds.
    await verdict(s, False)
    assert s.commit_on == "verdict", "a verdict arriving must undo the fallback"
    await delta(s, "and hello again")
    await vad_stop(s)
    assert s.commit_whys() == ["hold-expired", "vad-stop"] and s._commit_pending is True
    await s._cancel_hold_expiry()

    s = Recorder()
    await delta(s, "hello")
    await vad_stop(s)
    await verdict(s, True)                # the wiring works: one verdict has arrived
    await delta(s, "hello again")
    await vad_stop(s)
    await asyncio.sleep(CEILING + stt_mod._HOLD_SLACK_SECS + 0.05)
    assert s.commit_whys() == ["verdict", "hold-expired"]
    assert s.commit_on == "verdict", "one lost verdict after a good one is a transient"


async def test_a_vad_start_cancels_the_holds_expiry():
    """A caller who resumes and talks for longer than the ceiling must not have their
    sentence cut by the expiry timer of the stop they superseded."""
    s = Recorder()
    await delta(s, "hello")
    await vad_stop(s)
    await verdict(s, False)
    await vad_start(s)
    assert s._hold_task is None
    await asyncio.sleep(CEILING + stt_mod._HOLD_SLACK_SECS + 0.05)
    # (The backstop, back on duty for the held words, may fire in this quiet -- that is
    # a missed-stop commit and a different test. The HOLD's timer must not.)
    assert "hold-expired" not in s.commit_whys(), "the expiry fired into a resumed utterance"


async def test_the_stranded_backstop_stands_down_while_a_segment_is_held():
    """The backstop guards a MISSED VAD stop. Once the stop has come and the segment is
    held for its verdict, the engine's lagging deltas must not re-arm it: a backstop
    commit landing inside the ceiling's wait is the very fragment the hold prevents."""
    s = Recorder()
    await delta(s, "so I was")
    await vad_stop(s)
    await verdict(s, False)
    await delta(s, " thinking")           # a delta that lagged the stop
    assert s._stranded_task is None, "a delta re-armed the backstop under a hold"
    await asyncio.sleep(QUIET * 3)
    assert s._websocket.commits() == [], "the backstop committed a held segment"
    # The caller resumes: the hold is over and a missed stop is once again possible,
    # so the backstop is back on duty.
    await vad_start(s)
    await delta(s, " about")
    assert s._stranded_task is not None, "the backstop must re-arm once the hold is released"
    await asyncio.sleep(QUIET * 3)
    assert s.commit_whys() == ["backstop"]


async def test_a_resume_puts_the_backstop_back_on_a_held_segments_words():
    """The hold stands the backstop down. If the caller resumes and their new speech
    decodes to nothing (spoken over the bot) and its VAD stop is then missed, the held
    words must still be committed -- the old code had banked them at the first stop."""
    s = Recorder()
    await delta(s, "so I was")
    await vad_stop(s)
    await verdict(s, False)
    assert s._stranded_task is None
    await vad_start(s)                     # resumed; no deltas follow, no stop follows
    assert s._stranded_task is not None, "the held words are nobody's once the hold lifts"
    await asyncio.sleep(QUIET * 3)
    assert s.commit_whys() == ["backstop"]
    # ...and a resume into an EMPTY held segment arms nothing: no words, nothing to lose.
    s = Recorder()
    await vad_stop(s)
    await vad_start(s)
    assert s._stranded_task is None


async def test_vad_stop_mode_is_the_old_behaviour():
    s = Recorder(commit_on="vad-stop")
    await delta(s, "hello")
    await vad_stop(s)
    assert s.commit_whys() == ["vad-stop"]
    assert s._commit_pending is False and s._hold_task is None
    await verdict(s, True)
    await verdict(s, False)
    assert s.commit_whys() == ["vad-stop"], "verdict frames must be inert in vad-stop mode"


async def test_a_disconnect_clears_the_hold():
    s = Recorder()
    await delta(s, "hello")
    await vad_stop(s)
    await verdict(s, False)
    s._websocket = None                   # the recorder has no close handshake to run
    await s._disconnect_websocket()
    assert s._commit_pending is False and s._hold_task is None


def test_an_unknown_mode_is_refused():
    try:
        Recorder(commit_on="sometimes")
    except ValueError:
        return
    raise AssertionError("an unknown commit_on must not construct a service that never commits")


async def test_a_verdict_that_arrives_after_the_expiry_is_late_not_lost():
    """The expiry is wall-clock from the VAD stop; the ceiling's verdict is a frame
    queued behind the audio the aggregator is processing. When the expiry wins, the
    commit is right and only the blame is wrong: the verdict that then lands must be
    logged as late against that expiry, not treated as a frame the wiring dropped."""
    s = Recorder()
    lines = []
    sink = logger.add(lambda m: lines.append(m), level="WARNING")
    try:
        await delta(s, "hello")
        await vad_stop(s)
        await verdict(s, False)
        await asyncio.sleep(CEILING + stt_mod._HOLD_SLACK_SECS + 0.05)
        assert s.commit_whys() == ["hold-expired"]
        await verdict(s, True, source="ceiling")
        assert s.commit_whys() == ["hold-expired"], "the late verdict must not commit again"
        assert any("late, not lost" in w for w in lines), "a late verdict must be logged as late"
        assert s.commit_on == "verdict", "a late verdict is not a broken wiring"
    finally:
        logger.remove(sink)


async def test_a_done_the_engine_sent_on_its_own_releases_the_hold():
    """The engine's endpointer closes segments on its own. One that closes the HELD
    segment has put its words in the final already: the hold has nothing left to keep
    whole, and the verdict it was waiting for would only close a segment of silence
    (a finish() decode under the reply, logged as a wordless final)."""
    s = Recorder()
    await delta(s, "what time is it")
    await vad_stop(s)
    await verdict(s, False)
    assert s._commit_pending is True and s._hold_task is not None
    await done(s, "what time is it")          # unasked: no commit of ours in flight
    assert s._commit_pending is False, "a segment the engine closed itself is not held"
    assert s._hold_task is None, "nor is its expiry armed"
    await verdict(s, True, source="ceiling")
    assert s._websocket.commits() == [], "the ceiling's verdict committed an empty segment"
    finals = [f.text for f in s.pushed if getattr(f, "finalized", False)]
    assert finals == ["what time is it"]


async def test_a_done_that_answers_our_commit_leaves_a_later_hold_alone():
    """The counter-case: the done for a commit of OURS can land while a later stop is
    rightly held (the caller resumed and paused again inside the engine's ~0.8 s
    finish). That hold stays."""
    s = Recorder()
    await delta(s, "hello")
    await vad_stop(s)
    await verdict(s, True)                    # commit 1, in flight
    await vad_start(s)
    await delta(s, " and")
    await vad_stop(s)
    await verdict(s, False)                   # stop 2, held
    await done(s, "hello")                    # commit 1's answer
    assert s._commit_pending is True and s._hold_task is not None, (
        "the done for an earlier commit released a later stop's hold")
    await verdict(s, True, source="ceiling")
    assert s.commit_whys() == ["verdict", "ceiling"]


async def test_a_stop_after_the_engines_own_close_commits_the_tail_instead_of_holding():
    """The common order on the appliance: the engine finalizes on a shorter silence
    than the VAD's floor, so its done lands BEFORE the VAD stop. What the stop finds
    open is the tail after the engine's cut -- silence, on the ordinary turn -- and a
    hold over it keeps nothing whole. It commits at once, as the old design did, and
    the verdicts that follow answer nothing."""
    s = Recorder()
    await delta(s, "what time is it")
    await done(s, "what time is it")
    await vad_stop(s)
    assert s.commit_whys() == ["tail"]
    assert s._commit_pending is False and s._hold_task is None
    await verdict(s, False)
    await verdict(s, True, source="ceiling")
    assert s.commit_whys() == ["tail"], "verdicts for a tail commit must not commit again"
    # ...but words streaming AFTER the engine's cut are a sentence the engine has split
    # once already, and the stop for them holds like any other.
    s = Recorder()
    await delta(s, "hey, what's")
    await done(s, "hey, what's")
    await delta(s, " the time")
    await vad_stop(s)
    assert s.commit_whys() == [] and s._commit_pending is True
    await s._cancel_hold_expiry()
    # And a close before the caller's NEXT utterance is the previous utterance's.
    s = Recorder()
    await delta(s, "hello")
    await done(s, "hello")
    await vad_start(s)
    await vad_stop(s)
    assert s.commit_whys() == [] and s._commit_pending is True, (
        "a done before the VAD start belongs to the previous utterance")
    await s._cancel_hold_expiry()


class LiveRecorder(Recorder):
    """Recorder with pipecat's REAL task helpers: create_task through a TaskManager, and
    BaseObject's cancel_task, which AWAITS the task it cancels. That await is the one
    place _send_commit can suspend and the reason its bookkeeping comes after it; the
    plain Recorder's bare task.cancel() never suspends, so nothing there could catch a
    reorder. `in_the_gap`, if set, runs once as that await returns: what another task
    did while the commit was suspended, seen the instant before the commit goes on.
    (Driven from inside rather than from a second task because a frame handed to
    process_frame from outside yields in pipecat's own handling before it reaches this
    service's branch, and the commit resumes first.)"""
    create_task = TeaportSTTService.create_task

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.in_the_gap = None

    async def cancel_task(self, task, timeout=1.0):
        await TeaportSTTService.cancel_task(self, task, timeout)
        hook, self.in_the_gap = self.in_the_gap, None
        if hook is not None:
            await hook()


async def live_recorder():
    s = LiveRecorder()
    clock = SystemClock()
    clock.start()
    await s.setup(FrameProcessorSetup(clock=clock, task_manager=TaskManager(),
                                      pipeline_worker=None, audio_in_sample_rate=SAMPLE_RATE))
    return s


async def test_the_commit_awaits_the_holds_cancel_and_a_resume_in_that_gap_is_kept():
    """The ceiling's verdict is handled in the aggregator's task and the caller's resume
    arrives from the transport's, so the two can cross inside the commit. Its one
    suspension is the cancel of the live hold, and it comes FIRST: a VAD start landing
    there still finds the stop pending, releases it, and puts the backstop back on the
    held words. With the bookkeeping ahead of the cancel, or the cancel after the send,
    the resume finds nothing pending and arms nothing."""
    s = await live_recorder()
    await delta(s, "so I was")
    await vad_stop(s)
    await verdict(s, False)
    hold = s._hold_task
    assert hold is not None and not hold.done()
    seen = {}

    async def resume_in_the_gap():
        seen["pending"] = s._commit_pending
        await vad_start(s)

    s.in_the_gap = resume_in_the_gap
    await verdict(s, True, source="ceiling")
    assert hold.cancelled(), "the cancel must be awaited, not fired and forgotten"
    assert "pending" in seen, "the commit never suspended in the cancel of a live hold"
    assert seen["pending"] is True, (
        "the resume must find the stop still pending: the bookkeeping follows the cancel")
    assert s.commit_whys() == ["ceiling"]
    assert s._commit_pending is False and s._hold_task is None
    assert s._stranded_task is not None, (
        "the resume must have put the backstop back on the held words")
    await s._cancel_stranded_commit()


# ---- the strategy really produces the frames -----------------------------------------

class Incomplete(BaseSmartTurn):
    def _predict_endpoint(self, audio_array):
        return {"prediction": 0, "probability": 0.02}


class Complete(BaseSmartTurn):
    def _predict_endpoint(self, audio_array):
        return {"prediction": 1, "probability": 0.98}


class Rig:
    """The real controller with the brain's strategies, its upstream pushes routed to
    a real STT service -- the wiring the pipeline does, minus the pipeline."""

    def __init__(self, analyzer_cls):
        analyzer = analyzer_cls(sample_rate=SAMPLE_RATE,
                                params=SmartTurnParams(stop_secs=CEILING))
        analyzer.set_sample_rate(SAMPLE_RATE)
        self.stt = Recorder()
        self.verdicts = []
        self.controller = UserTurnController(
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[LateStartTurnStopStrategy(turn_analyzer=analyzer)],
            ),
            user_turn_stop_timeout=3600,
        )

        async def ignore(*args, **kwargs):
            pass

        for event in ("on_broadcast_frame", "on_reset_aggregation", "on_user_turn_started",
                      "on_user_turn_inference_triggered", "on_user_turn_stopped",
                      "on_user_turn_stop_timeout"):
            self.controller.add_event_handler(event, ignore)

        async def on_push(_c, frame, direction=DOWN):
            if isinstance(frame, TurnVerdictFrame):
                assert direction == UP, "the verdict must travel upstream to the STT"
                self.verdicts.append((frame.complete, frame.source))
                await self.stt.process_frame(frame, direction)

        self.controller.add_event_handler("on_push_frame", on_push)

    async def audio(self, ms):
        for _ in range(ms // CHUNK_MS):
            await self.controller.process_frame(
                InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1))

    async def utterance(self, text="what time is it"):
        """Speech, an interim that opens the turn, the VAD stop -- seen by the STT
        first, as in the pipeline."""
        c = self.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await self.audio(300)
        await c.process_frame(InterimTranscriptionFrame(text, "u", "t", None))
        await delta(self.stt, text)
        await vad_stop(self.stt)
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS))


async def test_the_strategy_reports_a_complete_verdict_at_the_vad_stop():
    rig = Rig(Complete)
    async with running_controller(rig.controller):
        await rig.utterance()
        assert rig.verdicts == [(True, "verdict")]
        assert rig.stt.commit_whys() == ["verdict"]
        await rig.audio(int((CEILING + 0.1) * 1000))
        assert rig.verdicts == [(True, "verdict")], "no ceiling after a COMPLETE verdict"


async def test_the_strategy_reports_incomplete_then_the_ceiling():
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.utterance()
        assert rig.verdicts == [(False, "verdict")]
        assert rig.stt._websocket.commits() == [], "the STT committed on INCOMPLETE"
        # Silence keeps arriving; the analyzer's ceiling overrides the verdict.
        await rig.audio(int((CEILING + 0.1) * 1000))
        assert rig.verdicts == [(False, "verdict"), (True, "ceiling")]
        assert rig.stt.commit_whys() == ["ceiling"]


async def test_a_resumption_gets_a_fresh_verdict_and_no_ceiling_from_the_old_stop():
    rig = Rig(Incomplete)
    c = rig.controller
    async with running_controller(c):
        await rig.utterance("hey, what's")
        await vad_start(rig.stt)
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(int((CEILING + 0.1) * 1000))
        assert rig.verdicts == [(False, "verdict")], "speech must not let the ceiling fire"
        assert rig.stt._websocket.commits() == []
        await delta(rig.stt, " the time?")
        await vad_stop(rig.stt)
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS))
        assert rig.verdicts == [(False, "verdict"), (False, "verdict")]
        await rig.audio(int((CEILING + 0.1) * 1000))
        assert rig.verdicts[-1] == (True, "ceiling")
        assert rig.stt.commit_whys() == ["ceiling"], "one commit for the whole utterance"
        assert rig.stt._interim_buffer == "hey, what's the time?"


async def test_a_complete_verdict_with_the_final_already_in_hand_is_reported_complete():
    """The common order on the appliance: the engine's segmenter closes the final BEFORE
    the VAD stop. A COMPLETE verdict then stops the turn inside the stock handler and
    the controller resets the strategy on the way, so the flag the push used to read is
    False by the time it is read -- and the STT was told INCOMPLETE for a turn the model
    had just called done. The verdict is what the turn did."""
    rig = Rig(Complete)
    c = rig.controller
    async with running_controller(c):
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(300)
        await c.process_frame(InterimTranscriptionFrame("what time is it", "u", "t", None))
        await c.process_frame(
            TranscriptionFrame("what time is it", "u", "t", None, finalized=True))
        await vad_stop(rig.stt)
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS))
        assert not c._user_turn, "the stop should have ended the turn inline"
        assert rig.verdicts == [(True, "verdict")]
        assert rig.stt.commit_whys() == ["verdict"]
        await rig.audio(int((CEILING + 0.1) * 1000))
        assert rig.verdicts == [(True, "verdict")], "no ceiling after a COMPLETE verdict"


async def test_the_ceiling_that_stops_the_turn_inline_still_reports_itself():
    """INCOMPLETE at the stop, the final lands under the ceiling, then the ceiling: with
    text and a final in hand it stops the turn inside the stock handler, the reset lands
    before the push's comparison, and the ceiling's verdict was never pushed."""
    rig = Rig(Incomplete)
    c = rig.controller
    async with running_controller(c):
        await rig.utterance("so I was thinking")
        assert rig.verdicts == [(False, "verdict")]
        await rig.audio(40)
        await c.process_frame(
            TranscriptionFrame("so I was thinking", "u", "t", None, finalized=True))
        await rig.audio(int((CEILING + 0.1) * 1000))
        assert not c._user_turn, "the ceiling should have ended the turn inline"
        assert rig.verdicts == [(False, "verdict"), (True, "ceiling")]
        assert rig.stt.commit_whys() == ["ceiling"]


async def test_a_turn_closed_under_an_incomplete_verdict_closes_the_held_segment():
    """A turn can end under a running ceiling on something other than the ceiling:
    keep_barge_in_reachable re-applying a stop the controller refused earlier, or the
    aggregator's stop watchdog. Both go through the controller's turn stop, which resets
    the strategy and clears the analyzer -- the ceiling stops counting and its verdict
    never comes. The strategy reports the close instead, so the STT's held segment
    closes with the turn rather than waiting out its expiry under a lost-frame warning."""
    from pipecat.turns.user_stop import UserTurnStoppedParams

    rig = Rig(Incomplete)
    c = rig.controller
    async with running_controller(c):
        await rig.utterance("so I was thinking")
        assert rig.verdicts == [(False, "verdict")] and rig.stt._commit_pending
        await c._trigger_user_turn_stop(
            None, UserTurnStoppedParams(enable_user_speaking_frames=True))
        assert not c._user_turn
        assert rig.verdicts == [(False, "verdict"), (True, "turn-stopped")]
        assert rig.stt.commit_whys() == ["turn-stopped"]
        await rig.audio(int((CEILING + 0.1) * 1000))
        assert rig.verdicts[-1] == (True, "turn-stopped"), "no ceiling after the turn closed"


# ---- the frame really crosses the pipeline ---------------------------------------------

class WiredSTT(TeaportSTTService):
    """The real service in a real pipeline: real push_frame, real queues. Only the
    wire is recorded (installed after the doomed connect settles) and the commit
    reasons noted."""

    def __init__(self):
        super().__init__(url="ws://127.0.0.1:1/none", commit_on="verdict")
        self.whys = []

    async def _send_commit(self, final=True, why="other"):
        self.whys.append(why)
        await super()._send_commit(final=final, why=why)


async def test_over_a_real_pipeline_the_verdict_reaches_the_stt_and_commits():
    """The strategy's push is an upstream frame that has to cross the aggregator's own
    queue and every processor between it and the STT -- in production TurnTimer,
    UserTranscriptEmitter and MemoryRecall. The two pure ones are in the path here.
    Both verdicts are exercised: INCOMPLETE holds, the ceiling's commits."""
    stt = WiredSTT()
    analyzer = Incomplete(sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=CEILING))
    pair = LLMContextAggregatorPair(
        LLMContext([{"role": "system", "content": "s"}]),
        user_params=LLMUserAggregatorParams(
            user_mute_strategies=[],
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[LateStartTurnStopStrategy(turn_analyzer=analyzer)],
            ),
            user_turn_stop_timeout=3600,
        ),
    )
    pipeline = Pipeline([stt, TurnTimer({}), UserTranscriptEmitter(), pair.user(),
                         pair.assistant()])
    task = PipelineTask(pipeline, observers=[])
    runner = PipelineRunner(handle_sigint=False)
    running = asyncio.create_task(runner.run(task))
    await asyncio.sleep(0.7)  # StartFrame propagation; the refused engine connect settles
    stt._websocket = WireRecorder()

    async def settle(secs):
        deadline = asyncio.get_running_loop().time() + secs
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)

    try:
        await stt.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        for _ in range(300 // CHUNK_MS):
            await stt.queue_frame(InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE,
                                                     num_channels=1))
        await delta(stt, "so I was thinking")
        await stt.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS))
        await settle(0.3)
        assert stt._commit_pending is True, "the VAD stop did not put the segment on hold"
        assert stt._websocket.commits() == [], (
            f"committed {stt.whys} on an INCOMPLETE verdict")
        # Silence flows on; the ceiling's verdict crosses the pipeline and commits.
        deadline = asyncio.get_running_loop().time() + CEILING + 2.0
        while asyncio.get_running_loop().time() < deadline and not stt.whys:
            await stt.queue_frame(InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE,
                                                     num_channels=1))
            await asyncio.sleep(CHUNK_MS / 1000)
        assert stt.whys == ["ceiling"], (
            f"expected the ceiling's commit to reach the STT through the pipeline, "
            f"got {stt.whys}")
    finally:
        stt._websocket = None              # the recorder has no close handshake to run
        await task.cancel()
        await running


def main():
    stt_mod.SMARTTURN_STOP_SECS = CEILING
    stt_mod._STRANDED_INTERIM_SECS = QUIET
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
