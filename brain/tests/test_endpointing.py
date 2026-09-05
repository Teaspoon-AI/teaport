#
# Unit test: the turn ALWAYS ends, and never before the user has finished.
#
# These pin the pipecat behaviour teaport's endpointing depends on. They exist because
# every one of these properties was broken at some point in pipecat 1.5.0, each failure
# costing a live session: a turn that could not end at all, a turn committed on a timer a
# barge-in cancelled, a turn committed on the previous utterance's words. All were fixed
# upstream in 1.6.0/1.7.0, and the workarounds teaport carried for them were deleted when
# it moved to 1.7.0 — these tests are what makes that deletion safe to keep.
#
# The window that used to matter is ONE audio chunk wide, so the first test walks the
# boundary rather than guessing at it: with stop_secs=0.3 and 20ms chunks, 300ms of
# post-start speech was the exact offset that wedged.
#
# The model is stubbed to vote COMPLETE unconditionally, so any INCOMPLETE the strategy
# acts on can only have come from the empty-buffer path, never from a real verdict.
#
# Run: on the appliance only — see pinned_pipecat.py.
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
    ENDPOINT_STOP_SECS,
    INTERRUPT_MIN_WORDS,
    SMARTTURN_STOP_SECS,
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


async def run_turn(speech_ms_after_start, analyzer_cls=AlwaysCompleteSmartTurn,
                   stop_secs=STOP_SECS, silence_ms_after_stop=0):
    """One barge-in-shaped turn. Returns True if it ended on its own.

    The user is already speaking when the first interim transcript lands, which is
    what starts the user turn; the strategy's reset() then clears _vad_user_speaking
    while the mic is still live, so every later chunk counts as silence. The VAD frame
    arrives with no audio chunk after it, which is the phase that wedges.
    """
    task_manager = TaskManager()
    task_manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    analyzer = analyzer_cls(
        sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=stop_secs)
    )
    stop = TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)
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
    await speak(silence_ms_after_stop)  # the mic stays live: silence keeps arriving
    await asyncio.sleep(0.2)
    await controller.cleanup()
    return bool(stopped)


# The offsets that bracket the one-chunk window, plus the window itself.
OFFSETS = [0, 100, 200, 260, 280, 300, 320, 340, 400, 600, 1000]


async def test_every_offset_ends_its_turn():
    for ms in OFFSETS:
        assert await run_turn(ms), (
            f"turn never ended with {ms}ms of speech after the turn started"
        )


async def test_an_incomplete_verdict_keeps_the_turn_open():
    """Smart Turn's "they are mid-thought" veto must keep the turn open.

    The counterpart to the test above: a turn has to end on its own, but never before
    the user has finished. At 100ms of post-start speech the silence backstop has not
    fired, so the model's INCOMPLETE is the only verdict there is — anything that
    commits here cuts people off mid-sentence."""
    assert not await run_turn(100, analyzer_cls=AlwaysIncompleteSmartTurn), (
        "the latch ended a turn Smart Turn had judged incomplete"
    )


async def test_smart_turns_silence_limit_is_a_ceiling_on_an_incomplete_verdict():
    """SmartTurnParams.stop_secs is not a silence floor: BaseSmartTurn.append_audio
    force-completes the turn once that much silence has accumulated, INCOMPLETE
    verdict or not, and empties its buffer so the model is never asked again.
    Feeding it the VAD's 0.2 floor (commit 7136c88) left a mid-sentence pause one
    audio chunk before it was committed anyway -- the "sole guard" was no guard.
    The brain keeps the two apart: the VAD's ENDPOINT_STOP_SECS asks the question,
    SMARTTURN_STOP_SECS is how long a "not done" is honoured."""
    # The premise: at the old coupling, 500ms of silence commits over the veto.
    assert await run_turn(100, analyzer_cls=AlwaysIncompleteSmartTurn,
                          stop_secs=ENDPOINT_STOP_SECS, silence_ms_after_stop=400), (
        "stop_secs did not force-complete over an INCOMPLETE verdict; the ceiling "
        "semantics this split rests on have changed")
    # The fix: at the brain's ceiling the veto survives the same silence.
    assert not await run_turn(100, analyzer_cls=AlwaysIncompleteSmartTurn,
                              stop_secs=SMARTTURN_STOP_SECS, silence_ms_after_stop=400), (
        f"an INCOMPLETE verdict was overridden within 500ms of silence at "
        f"SMARTTURN_STOP_SECS={SMARTTURN_STOP_SECS}")
    assert SMARTTURN_STOP_SECS >= ENDPOINT_STOP_SECS + 0.5, (
        "SMARTTURN_STOP_SECS must leave an INCOMPLETE verdict real room past the VAD "
        "floor, or the model's veto is decorative")


async def test_no_commit_before_this_utterance_has_a_transcript():
    """A turn must not be committed on the previous utterance's words.

    _text is the strategy's "did the user say anything" gate. Anything that satisfies it
    before the CURRENT utterance has been transcribed lets the silence backstop commit
    the turn early — and the aggregator's own buffer is still empty at that point, so
    push_aggregation() returns without asking the model and the user gets nothing back.

    Live on pipecat 1.5.0, 2026-08-20: committed at 22:15:28.157, and the transcript it
    was supposedly for did not arrive until 22:15:28.568."""
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
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)],
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
