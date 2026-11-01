#
# The peek's speculative reply over a real pipeline (stt.py THE PEEK): the real STT in
# slice mode with the preview at the stop, the real user aggregator with
# LateStartTurnStopStrategy on a scripted Smart Turn, and the real Speculator, to the
# commit's LLMContextFrame, where the LLM service would take the stream.
#
#   1. A one-word answer ("Yes.") whose words all come after the stop: no interim, so no
#      user turn is open when the peek's preview lands. The speculation is asked on it
#      anyway (the empty aggregation plus the provisional), the COMPLETE verdict closes
#      the turn on the peek -- no second commit -- and the commit adopts that
#      speculation through the whole-context check: a hit, one request. (The aggregator
#      handles the preview only once Smart Turn has answered the stop: its inference
#      holds the aggregator's input task, so on a COMPLETE turn the speculation and the
#      commit come together, and the head start is on an INCOMPLETE one.)
#   2. The same, the verdict INCOMPLETE and the caller resuming: the speculation is
#      cancelled, the peek dropped, and the next stop's turn is asked on every word once.
#
# Run: python test_turn_text_peek.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.audio.turn.smart_turn.base_smart_turn import (  # noqa: E402
    BaseSmartTurn,
    SmartTurnParams,
)
from pipecat.frames.frames import (  # noqa: E402
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineParams, PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from stt_harness import WireRecorder  # noqa: E402
from test_speculative_reply import CHUNK, CHUNK_MS, SAMPLE_RATE, TakeAtCommit, _llm  # noqa: E402

import teaport_brain.stt as stt_mod  # noqa: E402
from teaport_brain.agent_session import user_aggregator_params  # noqa: E402
from teaport_brain.barge_pause import PauseAwareMinWordsStrategy  # noqa: E402
from teaport_brain.endpointing import LateStartTurnStopStrategy  # noqa: E402
from teaport_brain.speculate import Speculator  # noqa: E402

STEP_S = 5.0


class Gated(BaseSmartTurn):
    """Smart Turn answering when the test lets it: the verdict lands after the preview."""

    def __init__(self, *args, complete=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate = threading.Event()
        self.complete = complete

    def _predict_endpoint(self, audio_array):
        self.gate.wait(STEP_S)
        return ({"prediction": 1, "probability": 0.98} if self.complete
                else {"prediction": 0, "probability": 0.02})


class Call:
    def __init__(self, complete=True):
        was = stt_mod.TURN_TEXT, stt_mod.PREVIEW_AT_STOP
        stt_mod.TURN_TEXT, stt_mod.PREVIEW_AT_STOP = "slice", True
        try:
            self.stt = stt_mod.TeaportSTTService(url="ws://127.0.0.1:1/none")
        finally:
            stt_mod.TURN_TEXT, stt_mod.PREVIEW_AT_STOP = was
        self.analyzer = Gated(sample_rate=SAMPLE_RATE, complete=complete,
                              params=SmartTurnParams(stop_secs=0.3))
        self.strategy = LateStartTurnStopStrategy(turn_analyzer=self.analyzer)
        self.context = LLMContext([{"role": "system", "content": "be brief"}])
        self.pair = LLMContextAggregatorPair(self.context, user_params=user_aggregator_params(
            user_turn_strategies=UserTurnStrategies(
                start=[PauseAwareMinWordsStrategy(min_words=2)], stop=[self.strategy]),
            user_turn_stop_timeout=3600))
        self.opened = []
        self.speculator = Speculator(llm=_llm(self.opened), aggregator=self.pair.user())
        self.strategy.speculator = self.speculator
        self.commit = TakeAtCommit(self.speculator)
        self.task = PipelineTask(Pipeline([self.stt, self.pair.user(), self.commit]),
                                 params=PipelineParams(audio_in_sample_rate=SAMPLE_RATE),
                                 cancel_on_idle_timeout=False)

    async def __aenter__(self):
        started = asyncio.Event()

        @self.task.event_handler("on_pipeline_started")
        async def _started(_task, _frame):
            started.set()

        self.running = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.wait_for(started.wait(), STEP_S)
        self.stt._websocket = WireRecorder()
        self.stt._session_origin = self.stt._sent_samples
        await self.stt._handle_message({"type": "session.created", "id": "s", "created": 1,
                                        "capabilities": ["transcription.progress",
                                                         "transcription.preview"]})
        return self

    async def __aexit__(self, *exc):
        self.analyzer.gate.set()
        await self.task.cancel()
        await self.running

    async def audio(self, ms):
        for _ in range(ms // CHUNK_MS):
            await self.task.queue_frame(InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE,
                                                           num_channels=1))

    async def until(self, pred, what):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STEP_S
        while not pred():
            assert loop.time() < deadline, f"never: {what}"
            await asyncio.sleep(0.01)

    async def preview(self, through, text):
        await self.stt._handle_message({"type": "transcription.preview", "through_ms": through,
                                        "delta": text, "main_through_ms": 0,
                                        "credited_ms": 0})

    def commits(self):
        return self.stt._websocket.commits()


async def test_a_one_word_answer_is_speculated_on_its_peek_and_the_verdict_adopts_it():
    async with Call() as call:
        await call.task.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await call.audio(400)
        await call.task.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await call.until(lambda: call.stt._peek is not None, "the peek at the stop")
        assert len(call.commits()) == 1
        await call.preview(call.stt._peek.at_ms, " Yes.")
        await asyncio.sleep(0.1)
        assert not call.pair.user()._user_turn_controller._user_turn, "no turn open yet"
        call.analyzer.gate.set()                   # the verdict: COMPLETE
        await call.until(lambda: call.commit.taken, "the commit")
        assert [m[-1]["content"] for m in (o.messages for o in call.opened)] == ["Yes."]
        assert call.commit.taken == [({"role": "user", "content": "Yes."}, True)], (
            call.commit.taken)
        assert call.speculator.hits == 1 and len(call.opened) == 1
        assert len(call.commits()) == 1, "the turn closed on the peek: no second commit"


async def test_a_peek_the_caller_talks_past_is_dropped_and_the_turn_is_whole():
    async with Call(complete=False) as call:
        await call.task.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await call.audio(400)
        await call.task.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await call.until(lambda: call.stt._peek is not None, "the peek at the stop")
        await call.preview(call.stt._peek.at_ms, " So I")
        call.analyzer.gate.set()                   # INCOMPLETE
        await call.until(lambda: call.opened, "the speculation on the preview")
        assert call.speculator.text == "So I" and call.strategy._incomplete_n == 1
        await call.task.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await call.until(lambda: call.speculator.text is None, "the resumed cancel")
        assert call.speculator.misses == 1 and call.stt._peek is None
        await call.audio(400)
        await call.stt._handle_message({"type": "transcription.delta", "delta": " So",
                                        "end_ms": 300})
        await call.stt._handle_message({"type": "transcription.delta", "delta": " I",
                                        "end_ms": 450})
        await call.stt._handle_message({"type": "transcription.delta", "delta": " was",
                                        "end_ms": 700})
        call.analyzer.complete = True
        await call.task.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await call.until(lambda: len(call.commits()) == 2, "the second stop's peek")
        # COMPLETE at once (the gate is open): adopted before its preview lands
        await call.until(lambda: call.stt._turns, "the adoption")
        await call.preview(call.stt._turns[-1].at_ms, " thinking")
        await call.until(lambda: call.commit.taken, "the commit")
        assert [m for m, _ in call.commit.taken] == [
            {"role": "user", "content": "So I was thinking"}], call.commit.taken
        assert len(call.commits()) == 2


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}", flush=True)
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
