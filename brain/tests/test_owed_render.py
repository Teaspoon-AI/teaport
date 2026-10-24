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
    def __init__(self):
        self.asked, self.rendered = [], 0
        self.caller_request_pending = False

    def ask(self, keys, purpose, context):
        self.asked.append((list(keys), purpose))

    def request_rendered(self):
        self.rendered += 1
        self.caller_request_pending = False

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


async def test_the_judges_ask_the_sessions_model_and_read_one_word():
    """BoundedOpenAILLMService.judge_cut and judge_gist (owed.Owed's defaults): the
    session's own client, model and routing; the measured prompts
    (owed.cut_judge_messages, owed.gist_judge_messages); a small completion budget;
    HEARD / OWED / anything else (no verdict)."""
    calls = []

    class Completions:
        def __init__(self, content):
            self.content = content

        async def create(self, **params):
            calls.append(params)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.content))])

    llm = _llm(_Provider())
    verdicts = []
    for content in ("HEARD", " owed.", "Maybe?", None):
        llm._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions(content)))
        verdicts.append(await llm.judge_cut("What's the price?", "It's two hundred",
                                            "forty-nine dollars."))
        verdicts.append(await llm.judge_gist("What year was the lighthouse first lit?",
                                             "The lighthouse was first lit in 1871.",
                                             "1871 is the year the lighthouse was first lit."))
    assert verdicts == [True, True, False, False, None, None, None, None], verdicts
    assert calls[0]["messages"] == owed.cut_judge_messages(
        "What's the price?", "It's two hundred", "forty-nine dollars.")
    assert calls[1]["messages"] == owed.gist_judge_messages(
        "What year was the lighthouse first lit?", "The lighthouse was first lit in 1871.",
        "1871 is the year the lighthouse was first lit.")
    for params in calls[:2]:
        assert params["max_tokens"] == services._JUDGE_MAX_TOKENS and "stream" not in params
        assert params["model"] == services.setting("LLM_MODEL")


async def test_whether_a_reply_tells_a_sync_answer_is_read_from_its_context():
    """#204 review finding 4: the LLM service registers what each request was rendered
    with -- a new answer result at its context's tail is fresh in the book (Owed.noted)
    -- whatever the frame's direction: a caller's commit that reads a fresh web_search
    result carries it, as the post-tool request does; once a reply heard in full has
    told it, a request carries none."""
    round_ = [{"role": "user", "content": "Any toy stores on Burnet Road?"},
              {"role": "assistant", "content": None, "tool_calls": [
                  {"id": "call-ws", "type": "function",
                   "function": {"name": "web_search", "arguments": "{}"}}]},
              {"role": "tool", "tool_call_id": "call-ws", "content": json.dumps(
                  {"results": [{"snippet": "Toy Joy, 5501 Burnet Rd, Austin."}]})}]
    llm = _llm(_Provider())
    llm.owed = owed.Owed(LLMContext([]), gate=_Gate(), answer_tools={"web_search"})

    async def render(kind, msgs):
        reply = owed.Reply(kind)
        token = services._REPLY.set(reply)
        try:
            await llm.get_chat_completions(LLMContext(msgs))
        finally:
            services._REPLY.reset(token)
        return reply
    commit = await render(owed.COMMIT, round_ + [{"role": "user", "content": "Hold on."}])
    assert commit.fresh == ("call-ws",), commit.fresh
    post = await render(owed.POST, round_)
    assert post.fresh == ("call-ws",)
    text = "There's a Toy Joy at 5501 Burnet Road."
    llm.owed.settle(post, text, cut=False, audio=True, text=text)
    told = await render(owed.COMMIT, round_ + [{"role": "assistant", "content": text},
                                               {"role": "user", "content": "Thanks."}])
    assert told.fresh == ()


async def test_a_caller_request_carries_the_note_before_their_words():
    provider = _Provider()
    llm = _llm(provider)
    ctx = _context({"role": "assistant", "content": "The top story on Hacker News right now"},
                   {"role": "user", "content": "I'm ready."})
    llm.owed, gate = _book(ctx)
    before = json.dumps(ctx.get_messages())
    reply = owed.Reply(owed.COMMIT)
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
    """ow_renderInGet: the speculation opens through _open_stream in its own task, so its
    request carries the note; ow_adoptDropsKeys: the commit that adopts it -- through the
    real Speculator.take and its _AdoptedStream -- registers the keys that note carried,
    and its telling closes the record (Fable's final review: it read them off the wrong
    object, and a full telling on an adopted turn closed nothing)."""
    from teaport_brain.speculate import Speculator, _AdoptedStream
    provider = _Provider()
    llm = _llm(provider)
    ctx = _context()
    llm.owed, _ = _book(ctx)
    aggregator = SimpleNamespace(context=ctx, _aggregation=[],
                                 _user_turn_controller=SimpleNamespace(_user_turn=True))
    llm.speculator = Speculator(llm=llm, aggregator=aggregator)
    assert await llm.speculator.start("I'm ready.", "test")
    for _ in range(200):
        if llm.speculator._current is not None and llm.speculator._current.done:
            break
        await asyncio.sleep(0.01)
    assert _notes(provider.requests[0]), "the speculation went out bare"
    ctx.add_message({"role": "user", "content": "I'm ready."})     # the commit
    reply = owed.Reply(owed.COMMIT)
    token = services._REPLY.set(reply)
    try:
        adopted = await llm.get_chat_completions(ctx)
    finally:
        services._REPLY.reset(token)
    assert isinstance(adopted, _AdoptedStream) and llm.speculator.hits == 1
    assert len(provider.requests) == 1, "the commit opened a request of its own"
    assert reply.noted == ("call-hn",), f"the adopted note's keys were lost: {reply.noted}"
    llm.owed.settle(reply, "Tesla Roadster is the top story on Hacker News right now.",
                    cut=False, audio=True)
    while llm.owed._judging:                                        # the gist verdict
        await asyncio.gather(*list(llm.owed._judging), return_exceptions=True)
    assert llm.owed.record("call-hn").state == owed.CLOSED
    await adopted.close()


async def test_a_run_of_zero_width_spaces_is_a_silent_run():
    """#70: a run whose text is only U+200B gives the TTS nothing to speak, so no context
    opens and its End is held: it must count as a run that said nothing (the TTS's own
    has_speech), or the gate's turn stays set for good (Owed.tla SILENT)."""
    provider = _Provider(reply=lambda p: [_chunk("\u200b\u200b\u200b")])
    llm = _llm(provider)
    ctx = _context()
    llm.owed, _ = _book(ctx)
    frames = await _run(llm, RunFrame(context=ctx, keys=("call-hn",), purpose="run"))
    assert any(isinstance(f, RunSilentFrame) for f in frames), (
        "a run of zero-width spaces kept the gate's turn")


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
    assert start.owed_reply.kind == "run" and start.owed_reply.noted == ("call-hn",)
    assert any(isinstance(f, LLMTextFrame) for f in frames)


async def test_a_run_whose_answer_was_heard_sends_no_request():
    """Checked where it renders (Owed.tla RunGo, ow_askOnly): a run whose records were
    heard since it was asked goes as an empty completion, and the gate is told the turn
    is over."""
    provider = _Provider()
    llm = _llm(provider)
    ctx = _context()
    llm.owed, gate = _book(ctx)
    llm.owed.record("call-hn").state = owed.CLOSED
    frames = await _run(llm, RunFrame(context=ctx, keys=("call-hn",), purpose="run"),
                        LLMContextFrame(context=LLMContext([{"role": "user", "content": "x"}])))
    assert len(provider.requests) == 1, "the run sent a request"
    assert not _notes(provider.requests[0])
    assert any(isinstance(f, RunSilentFrame) for f in frames), "the gate kept the turn"


async def test_a_commit_is_rendered_only_once_its_view_is_built():
    """Owed.tla Land: a consult landing while the commit's view waits for a settle finds
    the commit still to be rendered, leaves the note to it, and asks no run ("tell them
    now" right after "hold on"). A post-tool request never clears a pending commit."""
    provider = _Provider()
    llm = _llm(provider)
    ctx = _context({"role": "user", "content": "Hold on."})
    llm.owed, gate = _book(ctx, land=False)
    gate.caller_request_pending = True               # the commit passed TurnCommitMark
    earlier = owed.Reply("caller")
    llm.owed.noted(earlier, ("x",))                  # a reply still to settle
    reply = owed.Reply(owed.COMMIT)

    async def render():
        token = services._REPLY.set(reply)
        try:
            await llm.get_chat_completions(ctx)
        finally:
            services._REPLY.reset(token)
    task = asyncio.ensure_future(render())
    await asyncio.sleep(0.05)
    assert gate.rendered == 0, "rendered before its view"
    llm.owed.land(REQ, ANSWER, "call-hn")
    assert gate.asked == [], "a run was asked while the commit was still to be rendered"
    llm.owed.settle(earlier, None, cut=True, audio=False)
    await task
    notes = _notes(provider.requests[0])
    assert gate.rendered == 1 and notes and "Tesla Roadster" in notes[0]["content"]
    post = owed.Reply(owed.POST)                        # a context frame on its way up
    token = services._REPLY.set(post)
    try:
        await llm.get_chat_completions(ctx)
    finally:
        services._REPLY.reset(token)
    assert gate.rendered == 1, "a post-tool request told the gate a commit was rendered"


async def test_a_run_that_calls_a_tool_keeps_its_turn():
    """RunSilentFrame only for a run that said nothing AND called nothing: a call out is a
    tool round the gate must keep (NoRunMidTurn)."""
    provider = _Provider(reply=lambda p: [_chunk(tool="get_current_time")])
    llm = _llm(provider)
    called = asyncio.Event()

    async def handler(params):
        called.set()
        await params.result_callback({"time": "noon"})
    llm.register_function("get_current_time", handler)
    ctx = _context()
    llm.owed, _ = _book(ctx)
    frames = await _run(llm, RunFrame(context=ctx, keys=("call-hn",), purpose="run"))
    assert called.is_set(), "the run's call was not dispatched"
    assert not any(isinstance(f, RunSilentFrame) for f in frames), (
        "a run with a call out told the gate its turn was over")


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
