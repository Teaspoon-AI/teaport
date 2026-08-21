#
# Unit test: an utterance the engine cannot finalize must still end the user turn.
#
# The engine answers input_audio_buffer.commit with
# {"type": "transcription.done", "text": "", ...} when it has nothing to transcribe --
# key PRESENT, value empty (verified against the live engine 2026-08-20: 1.5s of silence
# returns exactly that). So msg.get("text", self._interim_buffer) never reaches its
# default, and `if text:` then drops the frame entirely.
#
# Dropping it wedges the turn. MinWordsUserTurnStartStrategy starts a user turn from an
# INTERIM, but TurnAnalyzerUserTurnStopStrategy only sets its _text from a final
# TranscriptionFrame and bails out of _maybe_trigger_user_turn_stopped() while _text is
# empty. A turn that started on interims and got an empty final therefore never stops on
# its own -- it waits out the aggregator's 5s user_turn_stop_timeout, and because any VAD
# or transcription frame re-arms that timeout, a live mic in a noisy room starves it and
# the turn never ends at all.
#
# The second test is the one that matters: it drives the real UserTurnController with the
# real strategies, so it fails if the fallback is removed OR if the turn can still wedge
# for some other reason.
#
# Run: python test_stt_final.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipecat.frames.frames import (  # noqa: E402
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_turn_controller import UserTurnController  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams  # noqa: E402

from teaport_brain.endpointing import (  # noqa: E402
    INTERRUPT_MIN_WORDS,
    LatchedTurnStopStrategy,
)
from teaport_brain.stt import TeaportSTTService  # noqa: E402

STOP_SECS = 0.3
SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)


class Recorder(TeaportSTTService):
    """Real _handle_message, frames captured instead of pushed."""

    def __init__(self):
        super().__init__(url="ws://127.0.0.1:1/none")
        self.pushed = []

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)

    async def stop_processing_metrics(self):
        pass


async def finalize(deltas, final_text):
    """Stream `deltas`, then finalize with `final_text`. Returns the pushed finals."""
    stt = Recorder()
    for d in deltas:
        await stt._handle_message({"type": "transcription.delta", "delta": d})
    await stt._handle_message({"type": "transcription.done", "text": final_text})
    return [f for f in stt.pushed if isinstance(f, TranscriptionFrame)]


async def test_empty_final_falls_back_to_the_interim_hypothesis():
    finals = await finalize(["change ", "your ", "voice"], "")
    assert len(finals) == 1, f"expected one final frame, got {len(finals)}"
    assert finals[0].text == "change your voice"
    assert finals[0].finalized is True


async def test_engine_text_still_wins_when_it_has_some():
    finals = await finalize(["chang", "e your voic"], "change your voice.")
    assert [f.text for f in finals] == ["change your voice."]


async def test_nothing_heard_at_all_pushes_nothing():
    """No deltas and no final text means no utterance -- and no turn was started
    either, since MinWords only fires on transcribed words. Pushing an empty frame
    here would put an empty user turn into the context."""
    assert await finalize([], "") == []


async def test_the_turn_still_ends_after_an_unfinalizable_utterance():
    """End to end over the real controller: the wedge this fallback exists to prevent."""
    task_manager = TaskManager()
    task_manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    stopped = []
    controller = UserTurnController(
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
            stop=[LatchedTurnStopStrategy(turn_analyzer=_always_complete())],
        ),
        # The 5s force-stop is the symptom, not the cure: disable it so the test sees
        # whether the STRATEGY ended the turn.
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

    async def speak(ms):
        for _ in range(ms // CHUNK_MS):
            await controller.process_frame(
                InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1)
            )

    await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
    await speak(400)
    await controller.process_frame(InterimTranscriptionFrame("change your voice", "u", "t", None))
    await speak(200)
    vad_stop = VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS)
    vad_stop.timestamp = 0.0
    await controller.process_frame(vad_stop)
    # The engine could not finalize; stt.py falls back to the interim hypothesis.
    for frame in await finalize(["change your voice"], ""):
        await controller.process_frame(frame)
    await asyncio.sleep(0.2)
    await controller.cleanup()
    assert stopped, "turn never ended after an unfinalizable utterance"


def _always_complete():
    from pipecat.audio.turn.smart_turn.base_smart_turn import BaseSmartTurn, SmartTurnParams

    class AlwaysComplete(BaseSmartTurn):
        def _predict_endpoint(self, audio_array):
            return {"prediction": 1, "probability": 0.99}

    analyzer = AlwaysComplete(sample_rate=16000, params=SmartTurnParams(stop_secs=STOP_SECS))
    analyzer.set_sample_rate(16000)
    return analyzer


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run_aio():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
