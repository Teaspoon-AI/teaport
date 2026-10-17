#
# owed.py's render hook in the real LLM service (#203): what each request carries, as the
# provider receives it.
#
# BoundedOpenAILLMService._open_stream renders the note into every request while a record
# is owed (formal/Owed.tla NoteWhileOwed; ow_renderInGet is the hook one method up, where
# an adopted speculation goes out bare), a run of ours goes up from the gate as a
# RunFrame and is rendered at the tail with the run line first, a run whose records
# were heard meanwhile sends no request (ow_askOnly's second half), and the re-offer's
# stay_silent is consumed where the calls are dispatched: neither it nor a result
# reaches the context, and the gate is told the turn is over (RunSilentFrame).
#
# The provider is a fake openai client: the request it records is the one pipecat built
# from the view, through the real adapter.
#
# Run: python test_owed_render.py   (or via pytest test_suite.py)
#
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from types import SimpleNamespace  # noqa: E402

from openai.types.chat import ChatCompletionChunk  # noqa: E402
from pipecat.adapters.schemas.function_schema import FunctionSchema  # noqa: E402
from pipecat.adapters.schemas.tools_schema import ToolsSchema  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    FunctionCallInProgressFrame,
    FunctionCallsStartedFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.services.openai.llm import OpenAILLMService  # noqa: E402

from teaport_brain import owed  # noqa: E402
from teaport_brain import services  # noqa: E402
from teaport_brain.followup_gate import SYSTEM_NOTICE_TAG, RunFrame, RunSilentFrame  # noqa: E402
from teaport_brain.services import BoundedOpenAILLMService  # noqa: E402

REQ = "Fetch the current top story from Hacker News and give its title and URL."
ANSWER = "Top story right now:\n\n- **Title:** Tesla Roadster\n\n(Posted by `shekhar101`.)"


def _chunk(text=None, tool=None):
    delta = {"role": "assistant"}
    if text is not None:
        delta["content"] = text
    if tool is not None:
        delta["tool_calls"] = [{"index": 0, "id": "call-s", "type": "function",
                                "function": {"name": tool, "arguments": "{}"}}]
    return ChatCompletionChunk.model_validate({
        "id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c

    async def close(self):
        pass


class _Provider:
    """The openai client's chat.completions, recording each request."""

    def __init__(self, reply=lambda params: [_chunk("Tesla Roadster is the top story.")]):
        self.requests = []
        self._reply = reply

    async def create(self, **params):
        self.requests.append(params)
        return _Stream(self._reply(params))


def _llm(provider):
    llm = BoundedOpenAILLMService(api_key="test", base_url="http://127.0.0.1:9/v1",
                                  settings=OpenAILLMService.Settings(model="m"))
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=provider))
    llm._warm_closed = True      # no connection to warm: there is no server
    return llm


class _Gate:
    caller_request_pending = False

    def __init__(self):
        self.asked, self.rendered, self.released = [], 0, 0

    def ask(self, keys, purpose):
        self.asked.append((list(keys), purpose))

    def request_rendered(self):
        self.rendered += 1

    def release_claim(self):
        self.released += 1

    def quiet_for(self):
        return 0.0


def _context(*tail):
    return LLMContext([
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "What's the top story on Hacker News?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-hn", "type": "function",
             "function": {"name": "ask_openclaw", "arguments": json.dumps({"request": REQ})}}]},
        {"role": "tool", "tool_call_id": "call-hn",
         "content": json.dumps({"status": "working_in_background"})},
        *tail], tools=ToolsSchema(standard_tools=[FunctionSchema(
            name="get_current_time", description="the time", properties={}, required=[])]))


def _book(ctx, land=True):
    gate = _Gate()
    book = owed.Owed(ctx, gate=gate)
    if land:
        book.land(REQ, ANSWER, "call-hn")
        gate.asked.clear()
    return book, gate


def _notes(params):
    return [m for m in params["messages"] if m["role"] == "user"
            and isinstance(m.get("content"), str) and m["content"].startswith(SYSTEM_NOTICE_TAG)]


async def test_a_caller_request_carries_the_note_before_their_words():
    provider = _Provider()
    llm = _llm(provider)
    ctx = _context({"role": "assistant", "content": "The top story on Hacker News right now"},
                   {"role": "user", "content": "I'm ready."})
    llm.owed, gate = _book(ctx)
    before = json.dumps(ctx.get_messages())
    reply = owed.Reply("caller")
    token = services._REPLY.set(reply)
    try:
        await llm.get_chat_completions(ctx)
    finally:
        services._REPLY.reset(token)
    msgs = provider.requests[0]["messages"]
    assert msgs[-1]["content"] == "I'm ready." and msgs[-2] is not None
    assert msgs[-2]["content"].startswith(SYSTEM_NOTICE_TAG) and "Tesla Roadster" in msgs[-2]["content"]
    assert "They have NOT heard this yet." in msgs[-2]["content"]
    assert json.dumps(ctx.get_messages()) == before, "the note was stored in the context"
    assert reply.noted == ("call-hn",) and gate.rendered == 1


async def test_nothing_owed_nothing_rendered():
    provider = _Provider()
    llm = _llm(provider)
    ctx = _context({"role": "user", "content": "What time is it?"})
    llm.owed, _ = _book(ctx, land=False)
    await llm._open_stream(ctx)
    assert not _notes(provider.requests[0])
    assert len(provider.requests[0]["messages"]) == len(ctx.get_messages())


async def test_a_speculation_renders_in_its_own_task_and_the_commit_adopts_its_note():
    """ow_renderInGet: the speculation opens through _open_stream, so its request carries
    the note; the commit that adopts it registers what it carried."""
    provider = _Provider()
    llm = _llm(provider)
    ctx = _context({"role": "user", "content": "I'm ready."})
    llm.owed, _ = _book(ctx)
    spec_stream = await asyncio.ensure_future(llm._open_stream(ctx))   # no Reply: its own task
    assert _notes(provider.requests[0]), "the speculation went out bare"
    llm.speculator = SimpleNamespace(take=lambda c: _adopt(spec_stream))
    reply = owed.Reply("caller")
    token = services._REPLY.set(reply)
    try:
        assert await llm.get_chat_completions(ctx) is spec_stream
    finally:
        services._REPLY.reset(token)
    assert reply.noted == ("call-hn",) and len(provider.requests) == 1


async def _adopt(stream):
    return stream


class _Sink(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.frames = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        self.frames.append(frame)
        await self.push_frame(frame, direction)


async def _run(llm, *frames, settle=0.3):
    """[llm, sink], running: `frames` queued at the top; what reached the sink."""
    sink = _Sink()
    task = PipelineTask(Pipeline([llm, sink]), cancel_on_idle_timeout=False)
    started = asyncio.Event()

    @task.event_handler("on_pipeline_started")
    async def _started(_t, _f):
        started.set()
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.wait_for(started.wait(), 10)
    await task.queue_frames(list(frames))
    await asyncio.sleep(settle)
    await task.cancel()
    await runner
    return sink.frames


async def test_a_run_goes_with_the_note_at_the_tail_and_the_run_line_first():
    provider = _Provider()
    llm = _llm(provider)
    ctx = _context()
    llm.owed, gate = _book(ctx)
    frames = await _run(llm, RunFrame(context=ctx, keys=("call-hn",), purpose="run"))
    msgs = provider.requests[0]["messages"]
    assert msgs[-1]["content"].startswith(SYSTEM_NOTICE_TAG + "\n" + owed.RUN_LINE), msgs[-1]
    assert gate.rendered == 0, "a run is not a caller request"
    start = next(f for f in frames if isinstance(f, LLMFullResponseStartFrame))
    assert start.owed_reply.purpose == "run" and start.owed_reply.noted == ("call-hn",)
    assert any(isinstance(f, LLMTextFrame) for f in frames)


async def test_a_run_whose_answer_was_heard_sends_no_request():
    provider = _Provider()
    llm = _llm(provider)
    ctx = _context()
    llm.owed, gate = _book(ctx)
    llm.owed.record("call-hn").state = owed.CLOSED
    frames = await _run(llm, RunFrame(context=ctx, keys=("call-hn",), purpose="run"),
                        LLMContextFrame(context=LLMContext([{"role": "user", "content": "x"}])))
    assert len(provider.requests) == 1, "the run sent a request"
    assert not _notes(provider.requests[0])
    assert sum(isinstance(f, LLMFullResponseStartFrame) for f in frames) == 1
    assert gate.released == 1, "the window the run claimed was kept"


async def test_the_reoffers_stay_silent_never_reaches_the_context():
    provider = _Provider(reply=lambda p: [_chunk(tool=owed.STAY_SILENT)])
    llm = _llm(provider)
    ctx = _context()
    llm.owed, _ = _book(ctx)
    n = len(ctx.get_messages())
    frames = await _run(llm, RunFrame(context=ctx, keys=("call-hn",), purpose="reoffer"))
    params = provider.requests[0]
    names = [t["function"]["name"] for t in params["tools"]]
    assert names == ["get_current_time", owed.STAY_SILENT], names
    assert params["messages"][-1]["content"].endswith(owed.REOFFER_LINE)
    assert not any(isinstance(f, (FunctionCallsStartedFrame, FunctionCallInProgressFrame))
                   for f in frames), "stay_silent was dispatched as a call"
    assert any(isinstance(f, services.FunctionCallsDispatchedFrame) for f in frames), (
        "the guard is not told a call was the answer")
    assert any(isinstance(f, RunSilentFrame) for f in frames), "the gate is not told"
    assert len(ctx.get_messages()) == n, "stay_silent entered the context"


async def test_stay_silent_is_only_the_reoffers():
    """On any other request the call is the model's to make like any unknown call; the
    run loop consumes it only for the re-offer, which alone offers it."""
    provider = _Provider()
    llm = _llm(provider)
    ctx = _context({"role": "user", "content": "hi"})
    llm.owed, _ = _book(ctx)
    await _run(llm, LLMContextFrame(context=ctx))
    names = [t["function"]["name"] for t in provider.requests[0]["tools"]]
    assert owed.STAY_SILENT not in names


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
