#
# pipecat premise (#185): a barge-in that overtakes a FunctionCallInProgressFrame still
# in transit below the LLM leaves the tool call committed as IN_PROGRESS for good -- and
# the brain's fix, heard_context.drop_overtaken_tool_calls, which drops that frame.
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
# The rig wires the fix exactly as agent_session does (on the assistant aggregator, before
# anything else wraps it). One run leaves it off: pipecat alone still has the race, so the
# fix is still needed -- when that run fails, pipecat has fixed #185 and the fix can go.
# The others pin what the fix must not drop: a call that was not cancelled, and a result
# already on its way behind the overtaken frame (cancel -> in-progress -> result).
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
    FunctionCallResultFrame,
    FunctionCallResultProperties,
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

from teaport_brain.heard_context import drop_overtaken_tool_calls  # noqa: E402
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
    """LLM -> output transport -> assistant aggregator, in a running PipelineTask.

    fixed: the aggregator wired as agent_session wires it (drop_overtaken_tool_calls).
    report: when the tool reports its result -- None: never (it is still running when
    the caller barges in); "at_once": at once, the handler then still unwinding, so the
    barge-in cancels it after its result is on its way; "on_finish": once `finish` is
    set, and the handler returns."""

    def __init__(self, fixed=True, report=None):
        self.llm = BoundedOpenAILLMService(
            api_key="test", base_url="http://127.0.0.1:1",
            settings=BoundedOpenAILLMService.Settings(model="x"))
        self.handler_entered = asyncio.Event()
        self.finish = asyncio.Event()
        self.report = report
        self.llm.register_function("lookup", self._lookup)
        self.out = DeviceOutput()
        self.context = LLMContext()
        self.assistant = LLMContextAggregatorPair(self.context).assistant()
        if fixed:
            drop_overtaken_tool_calls(self.assistant)
        # What the aggregator handled, in order, recorded at its own process_frame.
        self.seen: list[str] = []
        self.handled = {t: asyncio.Event() for t in
                        (InterruptionFrame, FunctionCallCancelFrame,
                         FunctionCallInProgressFrame, FunctionCallResultFrame,
                         FunctionCallsStartedFrame)}
        process = self.assistant.process_frame

        async def recording(frame, direction):
            await process(frame, direction)
            for t, ev in self.handled.items():
                if isinstance(frame, t):
                    self.seen.append(t.__name__)
                    ev.set()
        self.assistant.process_frame = recording
        self.task = PipelineTask(Pipeline([self.llm, self.out, self.assistant]),
                                 cancel_on_idle_timeout=False)

    async def _lookup(self, params):
        # cancel_on_interruption (the default): a barge-in cancels it while it runs.
        # Its result asks for no inference: the rig has no LLM endpoint to answer it.
        no_llm = FunctionCallResultProperties(run_llm=False)
        if self.report == "at_once":
            await params.result_callback({"answer": 42}, properties=no_llm)
        self.handler_entered.set()
        if self.report == "on_finish":
            await self.finish.wait()
            await params.result_callback({"answer": 42}, properties=no_llm)
            return
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

    async def dispatch(self, call_id=CALL_ID):
        self.handler_entered.clear()
        await self.llm.run_function_calls([FunctionCallFromLLM(
            function_name="lookup", tool_call_id=call_id, arguments={},
            context=self.context)])
        await self.wait(self.handler_entered)

    async def send(self, frame):
        """Queue a function-call frame at the top and wait until the aggregator has
        handled it (the LLM and the transport pass it on as it is)."""
        event = self.handled[type(frame)]
        event.clear()
        await self.task.queue_frame(frame)
        await self.wait(event)

    def tool_result(self, call_id=CALL_ID):
        """What the context says of call_id: ABSENT (no tool_calls message for it at all),
        the content of its tool message, or None (a call with no tool message)."""
        messages = self.context.get_messages()
        if not any(m.get("role") == "assistant" and any(
                c.get("id") == call_id for c in m.get("tool_calls") or [])
                for m in messages):
            return ABSENT
        for m in messages:
            if m.get("role") == "tool" and m.get("tool_call_id") == call_id:
                return m.get("content")
        return None

    async def race(self):
        """The preamble is playing when the tool call is dispatched, and the caller barges
        in before its in-progress frame (and result, if any) is past the audio."""
        # The preamble is playing: its first chunk is being written to the device.
        await self.task.queue_frame(OutputAudioRawFrame(b"\x01\x00" * (SR // 5), SR, 1))
        await self.wait(self.out.writing)
        # The tool call is dispatched; its in-progress frame queues behind that audio.
        await self.dispatch()
        # The caller barges in. Wait until the aggregator has handled the interruption,
        # then let the device finish the chunk it is writing.
        await self.task.queue_frame(InterruptionFrame())
        await self.wait(self.handled[InterruptionFrame])
        self.out.playing.set()
        await self.wait(self.handled[FunctionCallInProgressFrame])


ABSENT = "<no tool call in the context>"


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


async def test_a_barge_in_that_overtakes_the_in_progress_frame_leaves_no_call_unsettled():
    """The race, wired as agent_session wires it: the in-progress frame waits behind the
    preamble's audio in the output transport when the caller barges in. The call is
    cancelled before the aggregator has heard of it, so it never enters the context."""
    async with Rig() as rig:
        await rig.race()
        result = rig.tool_result()
        running = rig.assistant.has_function_calls_in_progress
    order = " -> ".join(rig.seen)
    assert result in (ABSENT, "CANCELLED"), (
        f"#185: the barge-in overtook FunctionCallInProgressFrame on its way to the "
        f"assistant aggregator, which committed tool call {CALL_ID} as {result!r} with "
        f"nothing left to settle it. Aggregator saw: {order}")
    assert not running, (
        f"the overtaken call {CALL_ID} is still on the aggregator's in-progress map: "
        f"has_function_calls_in_progress stays True for the rest of the session")
    print(f"  PASS race: aggregator saw {order}; tool call: {result}")


async def test_pipecat_alone_still_leaves_the_overtaken_call_in_progress():
    """The same race without the fix: pipecat's aggregator writes the call IN_PROGRESS
    for good. When this fails, pipecat has fixed #185 itself, and
    drop_overtaken_tool_calls can go."""
    async with Rig(fixed=False) as rig:
        await rig.race()
        result = rig.tool_result()
        llm_tasks = len(rig.llm._function_call_tasks)
    assert result == "IN_PROGRESS" and llm_tasks == 0, (
        f"pipecat alone no longer reproduces #185 (tool call: {result!r}, {llm_tasks} "
        f"call task(s) left): drop_overtaken_tool_calls may be dead weight now")
    print(f"  PASS premise: without the fix the call stays {result!r} with no task left")


async def test_a_call_that_was_not_cancelled_still_lands_and_settles():
    """The fix drops nothing of a call no cancel overtook: its in-progress frame, parked
    behind the same audio, lands IN_PROGRESS once it plays, and its result settles it."""
    async with Rig(report="on_finish") as rig:
        await rig.task.queue_frame(OutputAudioRawFrame(b"\x01\x00" * (SR // 5), SR, 1))
        await rig.wait(rig.out.writing)
        await rig.dispatch()
        rig.out.playing.set()
        await rig.wait(rig.handled[FunctionCallInProgressFrame])
        running = rig.tool_result()
        rig.finish.set()
        await rig.wait(rig.handled[FunctionCallResultFrame])
        result = rig.tool_result()
    assert running == "IN_PROGRESS" and result == '{"answer": 42}', (
        f"a call no cancel overtook: {running!r} while running, {result!r} after its "
        f"result; aggregator saw {rig.seen}")
    print(f"  PASS live call: {running!r} while running, then {result!r}")


async def test_a_result_behind_the_overtaken_frame_does_not_bring_the_call_back():
    """cancel -> in-progress -> result: the handler reported, then was cancelled while
    still unwinding, so its result rides behind the in-progress frame. The fix drops the
    frame; pipecat then drops the result (the call is not in progress), as it drops a
    result behind a cancel in the ordinary order -- the call does not come back."""
    async with Rig(report="at_once") as rig:
        await rig.race()
        await rig.wait(rig.handled[FunctionCallResultFrame])
        result = rig.tool_result()
        running = rig.assistant.has_function_calls_in_progress
    order = " -> ".join(rig.seen)
    assert result == ABSENT and not running, (
        f"cancel -> in-progress -> result left tool call {CALL_ID} as {result!r} "
        f"(still on the in-progress map: {running}); aggregator saw {order}")
    print(f"  PASS late result: aggregator saw {order}; tool call: {result}")


async def test_a_new_call_after_the_race_lands_and_settles():
    """Only the overtaken call's frame goes: the next call, with an id of its own, is
    dispatched after the barge-in and lands IN_PROGRESS, then settles with its result."""
    async with Rig(report="on_finish") as rig:
        await rig.race()
        overtaken = rig.tool_result()
        rig.handled[FunctionCallInProgressFrame].clear()
        await rig.dispatch("call_185_next")
        await rig.wait(rig.handled[FunctionCallInProgressFrame])
        running = rig.tool_result("call_185_next")
        rig.finish.set()
        await rig.wait(rig.handled[FunctionCallResultFrame])
        result = rig.tool_result("call_185_next")
    assert overtaken == ABSENT, (
        f"#185 itself, before the next call: the overtaken call reads {overtaken!r}")
    assert running == "IN_PROGRESS" and result == '{"answer": 42}', (
        f"the call after the race: {running!r} while running, {result!r} after its "
        f"result; aggregator saw {rig.seen}")
    print(f"  PASS next call: {running!r}, then {result!r}")


async def test_an_overtaken_calls_id_reused_by_a_new_call_lands_and_settles():
    """Ids can repeat (some servers number calls per response). A cancel that found no
    in-progress frame marks its call, and the frame may never come (the reply gate
    dropped it): a new call reusing the id announces itself with its own
    FunctionCallsStartedFrame, which clears the mark. And once a late frame has been
    dropped, its mark is gone: a later in-progress frame under the same id with no
    FunctionCallsStartedFrame of its own (pipecat leaves its internal cancel_<tool>
    calls out of that frame) is not taken for it."""
    def call(cls, call_id, **kw):
        return cls(function_name="lookup", tool_call_id=call_id, arguments={}, **kw)

    def started(call_id):
        return FunctionCallsStartedFrame(function_calls=[FunctionCallFromLLM(
            function_name="lookup", tool_call_id=call_id, arguments={}, context=None)])

    done = FunctionCallResultProperties(run_llm=False)
    async with Rig() as rig:
        rig.out.playing.set()
        # A call overtaken by its cancel whose in-progress frame never comes, then a new
        # call under the same id.
        await rig.send(started("r1"))
        await rig.send(FunctionCallCancelFrame(function_name="lookup", tool_call_id="r1"))
        await rig.send(started("r1"))
        await rig.send(call(FunctionCallInProgressFrame, "r1", cancel_on_interruption=True))
        reused = rig.tool_result("r1")
        await rig.send(call(FunctionCallResultFrame, "r1", result={"n": 1}, properties=done))
        reused_done = rig.tool_result("r1")
        # A late frame dropped, then the same id again without a started frame.
        await rig.send(started("r2"))
        await rig.send(FunctionCallCancelFrame(function_name="lookup", tool_call_id="r2"))
        await rig.send(call(FunctionCallInProgressFrame, "r2", cancel_on_interruption=True))
        late = rig.tool_result("r2")
        await rig.send(call(FunctionCallInProgressFrame, "r2", cancel_on_interruption=True))
        again = rig.tool_result("r2")
        await rig.send(call(FunctionCallResultFrame, "r2", result={"n": 2}, properties=done))
        again_done = rig.tool_result("r2")
    assert (reused, reused_done) == ("IN_PROGRESS", '{"n": 1}'), (
        f"a new call reusing an overtaken id: {reused!r}, then {reused_done!r}")
    assert late == ABSENT, f"the late in-progress frame landed: {late!r}"
    assert (again, again_done) == ("IN_PROGRESS", '{"n": 2}'), (
        f"the id after its late frame was dropped: {again!r}, then {again_done!r}")
    print(f"  PASS id reuse: {reused!r} -> {reused_done!r}; after a drop {again!r} -> "
          f"{again_done!r}")


async def main():
    failed = 0
    for name, fn in [(n, f) for n, f in list(globals().items())
                     if n.startswith("test_") and asyncio.iscoroutinefunction(f)]:
        try:
            await fn()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
