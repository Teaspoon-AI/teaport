#
# pipecat premise (#185): a barge-in that overtakes a FunctionCallInProgressFrame still
# in transit below the LLM leaves the tool call committed as IN_PROGRESS for good.
#
# How it happens, all in pipecat's own code:
#   * the LLM broadcasts FunctionCallInProgressFrame from the call's task. It is a
#     ControlFrame, so every processor below queues it behind the non-system frames
#     ahead of it. The output transport goes further: it parks it in the media sender's
#     audio queue (handle_sync_frame) behind any reply audio still waiting to be played.
#   * the interruption, and the FunctionCallCancelFrame the LLM broadcasts on it, are
#     SystemFrames: every processor handles them on its input task, ahead of its
#     queue, and the output transport pushes the interruption on BEFORE it handles it.
#     The interruption reset keeps the in-progress frame (it is uninterruptible), so it
#     is delivered afterwards.
#   * so the assistant aggregator sees the cancel for a call it only knows from
#     FunctionCallsStartedFrame (entry None) -- _handle_function_call_cancel returns
#     without settling anything -- and then the in-progress frame, which writes the
#     tool_calls message and an IN_PROGRESS tool message. The call's task is already
#     cancelled, so no result ever settles it.
#
# The rig is the real thing end to end: the brain's BoundedOpenAILLMService and pipecat's
# function-call machinery (run_function_calls, the call task, the interruption cancel),
# pipecat's real BaseOutputTransport (only the socket write stubbed), and the real
# LLMAssistantAggregator over an LLMContext. The one scripted element is the audio
# device: its write of the reply's first chunk blocks on an event, i.e. the bot is
# still playing its "let me look that up" when the tool call is dispatched. Every step
# waits on an event the code under test sets; nothing waits on time.
#
# The control run dispatches the call with nothing queued ahead of it: the aggregator
# sees the in-progress frame first, and pipecat settles the call CANCELLED. That pins
# the rig can see a settled call, so the race run's verdict means something.
#
# Run: python test_pipecat_function_call_race.py   (or via the suite)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    FunctionCallCancelFrame,
    FunctionCallFromLLM,
    FunctionCallInProgressFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
)
from pipecat.transports.base_output import BaseOutputTransport  # noqa: E402
from pipecat.transports.base_transport import TransportParams  # noqa: E402

from teaport_brain.services import BoundedOpenAILLMService  # noqa: E402

SR = 16000
CALL_ID = "call_185"
STEP_TIMEOUT_S = 5.0   # a failsafe on each event wait, never the synchronisation itself


class DeviceOutput(BaseOutputTransport):
    """pipecat's real output transport; the socket write is the audio device. While
    `playing` is clear, the first write blocks: the bot is mid-preamble."""

    def __init__(self):
        super().__init__(TransportParams(audio_out_enabled=True, audio_out_sample_rate=SR))
        self.ready = asyncio.Event()
        self.writing = asyncio.Event()
        self.playing = asyncio.Event()

    async def start(self, frame):
        await super().start(frame)
        await self.set_transport_ready(frame)
        self.ready.set()

    async def write_audio_frame(self, frame):
        self.writing.set()
        await self.playing.wait()
        return True


class Rig:
    """LLM -> output transport -> assistant aggregator, in a running PipelineTask."""

    def __init__(self):
        self.llm = BoundedOpenAILLMService(
            api_key="test", base_url="http://127.0.0.1:1",
            settings=BoundedOpenAILLMService.Settings(model="x"))
        self.handler_entered = asyncio.Event()
        self.llm.register_function("lookup", self._lookup)
        self.out = DeviceOutput()
        self.context = LLMContext()
        self.assistant = LLMContextAggregatorPair(self.context).assistant()
        # What the aggregator handled, in order, recorded at its own process_frame.
        self.seen: list[str] = []
        self.handled = {t: asyncio.Event() for t in
                        (InterruptionFrame, FunctionCallCancelFrame,
                         FunctionCallInProgressFrame)}
        process = self.assistant.process_frame

        async def recording(frame, direction):
            await process(frame, direction)
            for t, ev in self.handled.items():
                if isinstance(frame, t):
                    self.seen.append(t.__name__)
                    ev.set()
            if isinstance(frame, FunctionCallsStartedFrame):
                self.seen.append("FunctionCallsStartedFrame")
        self.assistant.process_frame = recording
        self.task = PipelineTask(Pipeline([self.llm, self.out, self.assistant]),
                                 cancel_on_idle_timeout=False)

    async def _lookup(self, params):
        # A tool still running when the caller barges in (cancel_on_interruption).
        self.handler_entered.set()
        await asyncio.Event().wait()

    async def __aenter__(self):
        self._run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await self.wait(self.out.ready)
        return self

    async def __aexit__(self, *exc):
        self.out.playing.set()
        await self.task.cancel()
        await self._run

    @staticmethod
    async def wait(event):
        await asyncio.wait_for(event.wait(), STEP_TIMEOUT_S)

    async def dispatch(self):
        await self.llm.run_function_calls([FunctionCallFromLLM(
            function_name="lookup", tool_call_id=CALL_ID, arguments={},
            context=self.context)])
        await self.wait(self.handler_entered)

    def tool_result(self):
        """The content of CALL_ID's tool message, or None if there is none."""
        messages = self.context.get_messages()
        assert any(m.get("role") == "assistant" and any(
            c.get("id") == CALL_ID for c in m.get("tool_calls") or []) for m in messages), (
            f"no tool_calls message for {CALL_ID}: {messages}")
        for m in messages:
            if m.get("role") == "tool" and m.get("tool_call_id") == CALL_ID:
                return m.get("content")
        return None


async def test_a_call_the_aggregator_has_seen_is_cancelled_by_the_barge_in():
    """Control: in-progress frame first, then the barge-in -> CANCELLED."""
    async with Rig() as rig:
        rig.out.playing.set()
        await rig.dispatch()
        await rig.wait(rig.handled[FunctionCallInProgressFrame])
        await rig.task.queue_frame(InterruptionFrame())
        await rig.wait(rig.handled[FunctionCallCancelFrame])
        result = rig.tool_result()
    assert result == "CANCELLED", (rig.seen, result)
    print(f"  PASS control: aggregator saw {rig.seen}; tool result {result!r}")


async def test_a_barge_in_that_overtakes_the_in_progress_frame_still_settles_the_call():
    """The race: the in-progress frame waits behind the preamble's audio in the output
    transport when the caller barges in."""
    async with Rig() as rig:
        # The preamble is playing: its first chunk is being written to the device.
        await rig.task.queue_frame(OutputAudioRawFrame(b"\x01\x00" * (SR // 5), SR, 1))
        await rig.wait(rig.out.writing)
        # The tool call is dispatched; its in-progress frame queues behind that audio.
        await rig.dispatch()
        # The caller barges in. Wait until the aggregator has handled the interruption,
        # then let the device finish the chunk it is writing.
        await rig.task.queue_frame(InterruptionFrame())
        await rig.wait(rig.handled[InterruptionFrame])
        rig.out.playing.set()
        await rig.wait(rig.handled[FunctionCallInProgressFrame])
        result = rig.tool_result()
        still_tracked = rig.assistant.has_function_calls_in_progress
        llm_tasks = len(rig.llm._function_call_tasks)
    order = " -> ".join(rig.seen)
    assert result == "CANCELLED", (
        f"#185 REPRODUCED: the barge-in overtook FunctionCallInProgressFrame on its way "
        f"to the assistant aggregator, which committed tool call {CALL_ID} as {result!r} "
        f"with nothing left to settle it (the LLM tracks {llm_tasks} call task(s); the "
        f"aggregator still reports a call in progress: {still_tracked}). Aggregator "
        f"saw: {order}")
    print(f"  PASS race: aggregator saw {order}; tool result {result!r}")


async def main():
    failed = 0
    for name, fn in [(n, f) for n, f in globals().items()
                     if n.startswith("test_") and asyncio.iscoroutinefunction(f)]:
        try:
            await fn()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
