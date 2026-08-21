#
# Unit test: the turn ALWAYS ends.
#
# The failure this guards against is a user turn that starts and never stops. pipecat
# finishes a turn in two places and the second can revoke the first: BaseSmartTurn's
# own silence counter reaches stop_secs, returns COMPLETE and clears its audio buffer;
# the strategy then re-runs analyze_end_of_turn() on VADUserStoppedSpeakingFrame, gets
# (INCOMPLETE, None) back from the now-empty buffer without the model ever running, and
# writes _turn_complete back to False. The final transcript then can't end the turn, and
# only the aggregator's 5s force-stop does — six seconds of dead air, or forever if room
# noise keeps re-arming that timeout. See endpointing.LatchedTurnStopStrategy.
#
# The window is ONE audio chunk wide, so the test walks the boundary rather than
# guessing at it: with stop_secs=0.3 and 20ms chunks the wedge is at exactly 300ms of
# post-start speech, and 280ms or 320ms both pass either way. A fix that only moved the
# race somewhere else would still fail here, because every offset has to end its turn.
#
# The model is stubbed to vote COMPLETE unconditionally, so any INCOMPLETE the strategy
# acts on can only have come from the empty-buffer path — never from a real verdict.
#
# Run: python test_endpointing.py   (or via pytest test_suite.py)
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
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    STTMetadataFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (  # noqa: E402
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_controller import UserTurnController  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams  # noqa: E402

from teaport_brain.endpointing import (  # noqa: E402
    INTERRUPT_MIN_WORDS,
    LatchedTurnStopStrategy,
)

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)
STOP_SECS = 0.3          # jetson01's ENDPOINT_STOP_SECS, and the boundary under test


class AlwaysCompleteSmartTurn(BaseSmartTurn):
    """Votes COMPLETE whenever it is actually asked."""

    def _predict_endpoint(self, audio_array):
        return {"prediction": 1, "probability": 0.99}


class AlwaysIncompleteSmartTurn(BaseSmartTurn):
    """Votes INCOMPLETE whenever it is actually asked."""

    def _predict_endpoint(self, audio_array):
        return {"prediction": 0, "probability": 0.01}


async def run_turn(strategy_cls, speech_ms_after_start,
                   analyzer_cls=AlwaysCompleteSmartTurn):
    """One barge-in-shaped turn. Returns True if it ended on its own.

    The user is already speaking when the first interim transcript lands, which is
    what starts the user turn; the strategy's reset() then clears _vad_user_speaking
    while the mic is still live, so every later chunk counts as silence. The VAD frame
    arrives with no audio chunk after it, which is the phase that wedges.
    """
    task_manager = TaskManager()
    task_manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    analyzer = analyzer_cls(
        sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=STOP_SECS)
    )
    stop = strategy_cls(turn_analyzer=analyzer)
    controller = UserTurnController(
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
            stop=[stop],
        ),
        # The 5s force-stop is the symptom, not the cure: disable it so the test sees
        # whether the STRATEGY ended the turn.
        user_turn_stop_timeout=3600,
    )
    stopped = []

    async def ignore(*args, **kwargs):
        pass

    for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                  "on_user_turn_started", "on_user_turn_stop_timeout"):
        controller.add_event_handler(event, ignore)

    async def on_stopped(controller, strategy, params):
        stopped.append(True)

    controller.add_event_handler("on_user_turn_stopped", on_stopped)
    await controller.setup(task_manager)
    analyzer.set_sample_rate(SAMPLE_RATE)

    async def speak(ms):
        for _ in range(ms // CHUNK_MS):
            await controller.process_frame(
                InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1)
            )

    await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
    await speak(400)
    await controller.process_frame(InterimTranscriptionFrame("hello there", "u", "t", None))
    await speak(speech_ms_after_start)
    vad_stop = VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS)
    vad_stop.timestamp = 0.0
    await controller.process_frame(vad_stop)
    final = TranscriptionFrame("hello there", "u", "t", None)
    final.finalized = True
    await controller.process_frame(final)
    await asyncio.sleep(0.2)
    await controller.cleanup()
    return bool(stopped)


# The offsets that bracket the one-chunk window, plus the window itself.
OFFSETS = [0, 100, 200, 260, 280, 300, 320, 340, 400, 600, 1000]


async def test_every_offset_ends_its_turn():
    for ms in OFFSETS:
        assert await run_turn(LatchedTurnStopStrategy, ms), (
            f"turn never ended with {ms}ms of speech after the turn started"
        )


async def test_stock_strategy_still_wedges():
    """Upstream is still broken, so the shim is still load-bearing.

    If this starts failing, pipecat fixed it and LatchedTurnStopStrategy can go.
    """
    wedged = [ms for ms in OFFSETS
              if not await run_turn(TurnAnalyzerUserTurnStopStrategy, ms)]
    assert wedged == [300], f"expected the stock strategy to wedge at 300ms, got {wedged}"


async def test_latch_does_not_override_a_real_incomplete_verdict():
    """Smart Turn's "they are mid-thought" veto has to still work.

    The latch may only preserve a completion the silence backstop already reached;
    it must never manufacture one. At 100ms of post-start speech the backstop has
    not fired, so the model's INCOMPLETE is the only verdict and the turn must stay
    open -- otherwise this change would cut people off mid-sentence.
    """
    assert not await run_turn(LatchedTurnStopStrategy, 100,
                              analyzer_cls=AlwaysIncompleteSmartTurn), (
        "the latch ended a turn Smart Turn had judged incomplete"
    )


async def test_a_turn_that_starts_from_a_final_transcript_commits_at_once():
    """The user answers while the bot is still talking, so the turn starts FROM the
    final transcript. That start resets the stop strategy, and super() then arms a
    (stt_p99 - stop_secs) timer instead of committing. Live, the interruption that the
    same transcript triggers cancels that timer, nothing commits the turn, and the
    aggregator force-stops it 5s later WITHOUT running inference -- so the user gets no
    reply at all. The transcript is already final: commit on it, not on a timer.

    STTMetadataFrame is what makes this bite. With no p99 the timer is 0s, fires
    immediately, and the bug is invisible; 0.8s is what the appliance reports.
    """
    task_manager = TaskManager()
    task_manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    stopped = []
    analyzer = AlwaysCompleteSmartTurn(
        sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=STOP_SECS)
    )
    analyzer.set_sample_rate(SAMPLE_RATE)
    controller = UserTurnController(
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
            stop=[LatchedTurnStopStrategy(turn_analyzer=analyzer)],
        ),
        user_turn_stop_timeout=3600,
    )

    async def ignore(*args, **kwargs):
        pass

    for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                  "on_user_turn_started", "on_user_turn_stop_timeout"):
        controller.add_event_handler(event, ignore)

    async def on_stopped(controller, strategy, params):
        stopped.append(True)

    controller.add_event_handler("on_user_turn_stopped", on_stopped)
    await controller.setup(task_manager)

    await controller.process_frame(
        STTMetadataFrame(service_name="teaport", ttfs_p99_latency=0.8)
    )
    final = TranscriptionFrame("say exactly what is it saying", "u", "t", None)
    final.finalized = True
    await controller.process_frame(final)
    # Deliberately shorter than the 0.5s timer super() arms: the commit must not
    # depend on a timer a barge-in can cancel.
    await asyncio.sleep(0.05)
    committed = bool(stopped)
    await controller.cleanup()
    assert committed, "turn did not commit on its own finalized transcript"


async def test_a_second_turn_from_a_later_interim_still_commits():
    """One utterance, two turns -- the shape that produced no reply at all.

    MinWordsUserTurnStartStrategy fires on INTERIM transcripts, so a later interim of a
    sentence whose turn already committed starts a SECOND turn. That start resets the
    stop strategy, and the second turn can never recover: the stop strategy ignores
    interims, and the interruption the turn start raises flushes the queued final that
    would have set _text. Live, the aggregator force-stopped it 5s later on a path that
    runs no inference.

    The final is deliberately never delivered here, because live it was flushed.
    """
    task_manager = TaskManager()
    task_manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    stops = []
    analyzer = AlwaysCompleteSmartTurn(
        sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=STOP_SECS)
    )
    analyzer.set_sample_rate(SAMPLE_RATE)
    controller = UserTurnController(
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
            stop=[LatchedTurnStopStrategy(turn_analyzer=analyzer)],
        ),
        user_turn_stop_timeout=3600,
    )

    async def ignore(*args, **kwargs):
        pass

    for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                  "on_user_turn_started", "on_user_turn_stop_timeout"):
        controller.add_event_handler(event, ignore)

    async def on_stopped(controller, strategy, params):
        stops.append(True)

    controller.add_event_handler("on_user_turn_stopped", on_stopped)
    await controller.setup(task_manager)

    async def speak(ms):
        for _ in range(ms // CHUNK_MS):
            await controller.process_frame(
                InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1)
            )

    await controller.process_frame(STTMetadataFrame(service_name="teaport", ttfs_p99_latency=0.8))
    await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
    await speak(400)
    await controller.process_frame(
        InterimTranscriptionFrame("they are pushing the code", "u", "t", None))
    vad_stop = VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS)
    vad_stop.timestamp = 0.0
    await controller.process_frame(vad_stop)
    final = TranscriptionFrame("they are pushing the code", "u", "t", None)
    final.finalized = True
    await controller.process_frame(final)
    await asyncio.sleep(0.05)
    assert stops, "first turn never committed"

    # The same sentence, one word longer, arriving after the first turn committed.
    await controller.process_frame(
        InterimTranscriptionFrame("they are pushing the code somewhere", "u", "t", None))
    await asyncio.sleep(0.05)
    committed = len(stops) >= 2
    await controller.cleanup()
    assert committed, "second turn started from a later interim and never committed"


async def test_no_commit_before_this_utterance_has_a_transcript():
    """A turn must not be committed on the previous utterance's words.

    _text is the base strategy's "did the user say anything" gate. Carrying it across a
    turn start before the CURRENT utterance has been transcribed satisfies that gate
    with the last utterance's words, so the silence backstop commits the turn before
    STT has delivered anything for it. The aggregator's own buffer is still empty, so
    push_aggregation() returns without asking the model and the user gets nothing.

    Live 2026-08-20: committed at 22:15:28.157, and the transcript it was supposedly
    for arrived at 22:15:28.568.
    """
    task_manager = TaskManager()
    task_manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    stops = []
    analyzer = AlwaysCompleteSmartTurn(
        sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=STOP_SECS)
    )
    analyzer.set_sample_rate(SAMPLE_RATE)
    controller = UserTurnController(
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
            stop=[LatchedTurnStopStrategy(turn_analyzer=analyzer)],
        ),
        user_turn_stop_timeout=3600,
    )

    async def ignore(*args, **kwargs):
        pass

    for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                  "on_user_turn_started", "on_user_turn_stop_timeout"):
        controller.add_event_handler(event, ignore)

    async def on_stopped(controller, strategy, params):
        stops.append(True)

    controller.add_event_handler("on_user_turn_stopped", on_stopped)
    await controller.setup(task_manager)

    async def speak(ms):
        for _ in range(ms // CHUNK_MS):
            await controller.process_frame(
                InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1)
            )

    # An utterance that completes normally, leaving its words in _text. VAD stop first,
    # then the final — the order the live pipeline produces.
    await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
    await speak(400)
    stop_a = VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS)
    stop_a.timestamp = 0.0
    await controller.process_frame(stop_a)
    first = TranscriptionFrame("four", "u", "t", None)
    first.finalized = True
    await controller.process_frame(first)
    await asyncio.sleep(0.05)
    assert len(stops) == 1, "the first utterance should have committed once"

    # A NEW utterance. Its turn starts from an interim, and STT has not finalized it
    # yet — exactly the live shape.
    await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
    await speak(400)
    await controller.process_frame(
        InterimTranscriptionFrame("the one with", "u", "t", None))
    await speak(300)          # trips the silence backstop
    await asyncio.sleep(0.05)
    committed_early = len(stops) > 1
    await controller.cleanup()
    assert not committed_early, (
        "turn committed on the previous utterance's text, before its own transcript"
    )


def main():
    sync = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    for fn in sync:
        fn()
        print(f"  ok {fn.__name__}")

    async def run_aio():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
