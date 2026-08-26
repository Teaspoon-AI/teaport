#
# A user turn started from an interim must not flush its own final transcript.
#
# Live failure, 2026-08-25 19:05 on jetson01. The engine closed a transcription
# segment on its own: cumulative interim at 19:05:51.068, final ~2 ms behind it
# (the ledger charted "Can you recite Hamlet's soliloquy for me?" at 51.070).
# The interim reached the LLM user aggregator first; MinWordsUserTurnStartStrategy
# started a user turn from it and the aggregator broadcast an interruption
# (51.083). FrameProcessor.broadcast_interruption flushes the aggregator's own
# frame queue, and the InterruptionFrame makes every processor between the STT
# and the aggregator flush theirs (FrameQueue.reset drops all non-Uninterruptible
# frames). The final — a plain data frame — was in one of those queues. Flushed.
#
# The turn it had just started then owned no text: TurnAnalyzerUserTurnStopStrategy
# cannot fire without transcript text, no VAD stop was coming (the VAD went quiet
# before the turn began), so the 5 s user_turn_stop_timeout force-stopped the turn
# (19:05:56.086 "User stopped speaking (strategy: None)") and push_aggregation()
# had nothing to push. SILENT TURN reached=['nothing'] — the model was never asked.
#
# The fix: stt.py pushes finals as FinalTranscriptionFrame, a TranscriptionFrame
# with pipecat's UninterruptibleFrame mixin — the designed escape hatch that
# FrameQueue.reset() preserves. Interims stay interruptible on purpose.
#
# The second test is the one that matters: real Pipeline, real PipelineTask, real
# LLMUserAggregator with the gateway's strategies, and the real TeaportSTTService
# pushing the exact interim+final tail of the incident. It fails on the pre-fix
# code (the final is flushed, the model is never asked) and passes with the fix.
#
# Run: python test_final_survives_turn_start.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.audio.turn.smart_turn.base_smart_turn import (  # noqa: E402
    BaseSmartTurn,
    SmartTurnParams,
)
from pipecat.frames.frames import (  # noqa: E402
    InterimTranscriptionFrame,
    LLMContextFrame,
    TranscriptionFrame,
    UninterruptibleFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessor  # noqa: E402
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (  # noqa: E402
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from teaport_brain.endpointing import INTERRUPT_MIN_WORDS  # noqa: E402
from teaport_brain.stt import TeaportSTTService  # noqa: E402

HAMLET = "Can you recite Hamlet's soliloquy for me?"

# The aggregator's force-stop watchdog, shortened from the live 5 s so the broken
# path fails the test quickly. Both paths must beat the assert deadline below:
# fixed code commits via the TurnAnalyzer transcript fallback (~stt p99, 0.8 s),
# and even a force-stop WITH text at 2 s would still ask the model.
STOP_TIMEOUT_SECS = 2.0
ASSERT_DEADLINE_SECS = 5.0


class Recorder(TeaportSTTService):
    """Real _handle_message, frames captured instead of pushed."""

    def __init__(self):
        super().__init__(url="ws://127.0.0.1:1/none")
        self.pushed = []

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)

    async def stop_processing_metrics(self):
        pass


async def test_the_finals_the_stt_pushes_are_uninterruptible():
    """Unit half: what stt.py pushes for a final must carry the mixin."""
    stt = Recorder()
    await stt._handle_message({"type": "transcription.delta", "delta": HAMLET})
    await stt._handle_message({"type": "transcription.done", "text": HAMLET})

    finals = [f for f in stt.pushed if isinstance(f, TranscriptionFrame)]
    assert len(finals) == 1, f"expected one final, got {len(finals)}"
    assert isinstance(finals[0], UninterruptibleFrame), (
        "the final is a plain TranscriptionFrame: any InterruptionFrame flushes "
        "it from the queues between the STT and the aggregator, and a turn "
        "started from the matching interim broadcasts exactly that interruption"
    )
    # Still a TranscriptionFrame with finalized=True for every consumer downstream
    # (aggregator, MinWords, TurnAnalyzer, ledger) — the mixin must change nothing.
    assert finals[0].finalized is True
    assert finals[0].text == HAMLET

    # Interims stay interruptible: they are disposable hypotheses by design.
    interims = [f for f in stt.pushed if isinstance(f, InterimTranscriptionFrame)]
    assert interims, "expected the delta to be pushed as an interim"
    assert not any(isinstance(f, UninterruptibleFrame) for f in interims), (
        "interims must stay flushable — only the final carries the words the turn commits"
    )


class NeverCompletes(BaseSmartTurn):
    """Turn analyzer that never declares end-of-turn on audio.

    Faithful to the incident: turn B saw no audio and no VAD events, so the
    analyzer contributed nothing between the turn start and the force-stop.
    """

    def _predict_endpoint(self, audio_array):
        return {"prediction": 0, "probability": 0.0}


class ContextTap(FrameProcessor):
    """Sits where the LLM sits; records every context the aggregator asks it to run."""

    def __init__(self):
        super().__init__()
        self.contexts = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            self.contexts.append(list(frame.context.get_messages()))
        await self.push_frame(frame, direction)


async def test_a_turn_started_from_an_interim_keeps_its_in_flight_final():
    """Integration half: the exact tail of the live incident over a real pipeline."""
    stt = TeaportSTTService(url="ws://127.0.0.1:1/none")  # connect fails fast; driven by hand
    context = LLMContext([{"role": "system", "content": "s"}])
    pair = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # Mirrors gateway_server.py minus the VAD analyzer: turn B in the
            # incident had no VAD events — the VAD went quiet before it started.
            user_mute_strategies=[],
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(
                        turn_analyzer=NeverCompletes(
                            sample_rate=16000, params=SmartTurnParams(stop_secs=0.5)
                        )
                    )
                ],
            ),
            user_turn_stop_timeout=STOP_TIMEOUT_SECS,
        ),
    )
    tap = ContextTap()
    pipeline = Pipeline([stt, pair.user(), tap, pair.assistant()])
    task = PipelineTask(pipeline, observers=[])
    runner = PipelineRunner(handle_sigint=False)
    running = asyncio.create_task(runner.run(task))
    await asyncio.sleep(0.7)  # StartFrame propagation; the failed engine connect settles

    # The incident's tail, verbatim: the engine closes the segment on its own —
    # cumulative interim, then the final right behind it. Both are queued at the
    # aggregator before its frame task runs; processing the interim starts the
    # turn and broadcasts the interruption while the final is still queued.
    await stt._handle_message({"type": "transcription.delta", "delta": HAMLET})
    await stt._handle_message({"type": "transcription.done", "text": HAMLET})

    deadline = time.monotonic() + ASSERT_DEADLINE_SECS
    while time.monotonic() < deadline and not tap.contexts:
        await asyncio.sleep(0.1)

    await task.cancel()
    await running

    assert tap.contexts, (
        "the model was never asked: the turn-start interruption flushed the "
        "in-flight final, the turn owned no text, and the force-stop had "
        "nothing to push (live 2026-08-25: SILENT TURN reached=['nothing'])"
    )
    users = [m for m in tap.contexts[0] if m.get("role") == "user"]
    assert users and users[-1]["content"] == HAMLET, (
        f"the committed user turn must carry the final's words, got: {users!r}"
    )


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
