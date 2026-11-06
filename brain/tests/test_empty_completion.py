#
# A caller's turn whose completion comes back with nothing in it (#15): asked once more,
# then answered with the recovery line -- never dead air, never two lines, and never
# when the silence was meant.
#
# Live 2026-10-31, a phone call: two completions in a row (gpt-oss-120b on Groq, a ~4.1k
# token prompt) ended finish_reason=stop after ~80 completion tokens, ~70 of them
# reasoning: LLMFullResponseStartFrame, then LLMFullResponseEndFrame, nothing between --
# no text and no tool call. The caller asked twice more; the third ask was answered, 18 s
# after the first. The same shape on 2026-09-04 and 2026-10-24.
#
# The real BoundedOpenAILLMService (its render hook and the owed book), with the real
# LLMTextGuard below it where the recovery line has to pass; the provider is a fake
# openai client, so every request is the one pipecat built, through the real adapter.
#
# Run: python test_empty_completion.py   (or via pytest test_suite.py)
#
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from types import SimpleNamespace  # noqa: E402

from loguru import logger  # noqa: E402
from openai.types.chat import ChatCompletionChunk  # noqa: E402
from pipecat.adapters.schemas.function_schema import FunctionSchema  # noqa: E402
from pipecat.adapters.schemas.tools_schema import ToolsSchema  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    InterruptionFrame,
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

from teaport_brain import llm_text_guard, owed, services  # noqa: E402
from teaport_brain.followup_gate import RunFrame, RunSilentFrame  # noqa: E402
from teaport_brain.llm_text_guard import LLMTextGuard  # noqa: E402
from teaport_brain.mood_tag import MoodTag  # noqa: E402
from teaport_brain.services import BoundedOpenAILLMService  # noqa: E402

ASK = "Edit the note to say Thursday instead of Wednesday."
ANSWER = "Done, the note says Thursday now."
LINE = llm_text_guard.RECOVERY_TEXT.strip()


def _chunk(text=None, tool=None, finish=None):
    delta = {"role": "assistant"}
    if text is not None:
        delta["content"] = text
    if tool is not None:
        delta["tool_calls"] = [{"index": 0, "id": "call-t", "type": "function",
                                "function": {"name": tool, "arguments": "{}"}}]
    return ChatCompletionChunk.model_validate({
        "id": "gen-1", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


def _usage(completion, reasoning):
    return ChatCompletionChunk.model_validate({
        "id": "gen-1", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [], "usage": {
            "prompt_tokens": 4100, "completion_tokens": completion,
            "total_tokens": 4100 + completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning}}})


# The 2026-10-31 completion: a role delta with no content, the stop, the usage.
EMPTY = [_chunk(""), _chunk(finish="stop"), _usage(80, 71)]
SAID = [_chunk(ANSWER), _chunk(finish="stop"), _usage(30, 12)]


class _Stream:
    def __init__(self, chunks, hang=None):
        self._chunks = chunks
        self._hang = hang

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c
        if self._hang is not None:
            await self._hang.wait()

    async def close(self):
        pass


class _Provider:
    """The openai client's chat.completions: replies[i] answers request i (the last one
    answers every request after it); each request is recorded, and `on_request`, if set,
    runs as it arrives."""

    def __init__(self, *replies, on_request=None, hang=None):
        self.requests = []
        self._replies = replies
        self.on_request = on_request
        self._hang = hang

    async def create(self, **params):
        self.requests.append(params)
        if self.on_request is not None:
            self.on_request(len(self.requests))
        chunks = self._replies[min(len(self.requests), len(self._replies)) - 1]
        return _Stream(chunks, self._hang)


class _Gate:
    def __init__(self):
        self.caller_request_pending = False

    def request_rendered(self):
        self.caller_request_pending = False

    def ask(self, keys, purpose, context):
        pass

    def quiet_for(self):
        return 0.0


def _llm(provider, ctx):
    llm = BoundedOpenAILLMService(api_key="test", base_url="http://127.0.0.1:9/v1",
                                  settings=OpenAILLMService.Settings(model="m"))
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=provider))
    llm._warm_closed = True      # no connection to warm: there is no server
    gate = _Gate()
    llm.owed = owed.Owed(ctx, gate=gate)
    return llm, gate


def _context(*tail):
    return LLMContext([
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "Note that the dentist is on Wednesday."},
        {"role": "assistant", "content": "Noted: the dentist is on Wednesday."},
        *tail], tools=ToolsSchema(standard_tools=[FunctionSchema(
            name="get_current_time", description="the time", properties={}, required=[])]))


CONSULT = "Look up when the dentist opens."


def _consulted(*tail):
    """A context with a consult out: a record of ours to tell once it lands."""
    return _context(
        {"role": "user", "content": "When does the dentist open?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-c", "type": "function", "function": {
                "name": "openclaw_agent_consult",
                "arguments": json.dumps({"question": CONSULT})}}]},
        {"role": "tool", "tool_call_id": "call-c",
         "content": json.dumps({"status": "working_in_background"})},
        *tail)


async def _until(done, what, timeout=10.0):
    for _ in range(int(timeout / 0.01)):
        if done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class _Sink(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.frames = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        self.frames.append(frame)
        await self.push_frame(frame, direction)

    def ends(self) -> int:
        return sum(isinstance(f, LLMFullResponseEndFrame) for f in self.frames)


async def _run(processors, *steps):
    """`processors` and a sink, running; what reached the sink. A step is a context frame
    queued at the top, waited for until its completion's End has reached the sink (a
    retry and a recovery line both come before it), or an async callable given the sink,
    which waits for what it needs itself."""
    sink = _Sink()
    task = PipelineTask(Pipeline([*processors, sink]), cancel_on_idle_timeout=False)
    started = asyncio.Event()

    @task.event_handler("on_pipeline_started")
    async def _started(_t, _f):
        started.set()
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.wait_for(started.wait(), 10)
    for step in steps:
        if callable(step):
            await step(sink)
        else:
            n = sink.ends() + 1
            await task.queue_frames([step])
            await _until(lambda: sink.ends() >= n, "the completion's End")
    await asyncio.sleep(0.05)       # anything after the End would arrive here
    await task.cancel()
    await runner
    return sink.frames


def _answer(llm, ctx, result=None):
    """A step: the assistant aggregator's ask for the answer to the call's result, pushed
    up, once the ledger has settled the reply before it; waited for until its End."""
    async def step(sink):
        for f in sink.frames:
            reply = getattr(f, "owed_reply", None)
            if reply is not None and not reply.settled:
                llm.owed.settle(reply, None, cut=False, audio=False)
        ctx.add_message({"role": "tool", "tool_call_id": "call-t",
                         "content": json.dumps(result or {"time": "noon"})})
        n = sink.ends() + 1
        await llm.queue_frame(LLMContextFrame(context=ctx), FrameDirection.UPSTREAM)
        await _until(lambda: sink.ends() >= n, "the answer's End")
    return step


async def _result(params):
    await params.result_callback({"ok": True})


def _texts(frames):
    return [f.text for f in frames if isinstance(f, LLMTextFrame)]


def _lines(frames):
    return [t for t in _texts(frames) if t.strip() == LINE]


class _Log:
    def __enter__(self):
        self.lines = []
        self._id = logger.add(lambda m: self.lines.append(m.record["message"]), level="INFO")
        return self

    def __exit__(self, *exc):
        logger.remove(self._id)

    def having(self, text):
        return [line for line in self.lines if text in line]


async def test_an_empty_reply_to_the_caller_is_asked_again_and_answered():
    """The issue's scenario: the retry is the caller's second ask without the wait. One
    reply downstream -- one Start, the answer, one End -- and the retry is the request
    the first attempt went out as."""
    ctx = _context({"role": "user", "content": ASK})
    provider = _Provider(EMPTY, SAID)
    llm, _ = _llm(provider, ctx)
    with _Log() as log:
        frames = await _run([llm, LLMTextGuard()], LLMContextFrame(context=ctx))
    assert len(provider.requests) == 2, f"asked {len(provider.requests)} times"
    assert provider.requests[1]["messages"] == provider.requests[0]["messages"]
    assert _texts(frames) == [ANSWER], _texts(frames)
    assert [type(f).__name__ for f in frames
            if isinstance(f, (LLMFullResponseStartFrame, LLMFullResponseEndFrame))] == [
        "LLMFullResponseStartFrame", "LLMFullResponseEndFrame"]
    finished = log.having("completion finished")
    assert len(finished) == 2, finished
    assert ("finish_reason=stop, completion_tokens=80, reasoning_tokens=71, "
            "no words and no tool call: content ''") in finished[0], finished[0]
    assert "no words" not in finished[1], finished[1]
    assert log.having("asking once more")


async def test_nothing_twice_speaks_the_recovery_line_once():
    """Retried once, not again: then the guard's line, through the guard, once -- its own
    End check (a reply it emptied) sees text forwarded and stays quiet."""
    ctx = _context({"role": "user", "content": ASK})
    provider = _Provider(EMPTY)
    llm, _ = _llm(provider, ctx)
    frames = await _run([llm, LLMTextGuard()], LLMContextFrame(context=ctx))
    assert len(provider.requests) == 2, f"asked {len(provider.requests)} times"
    assert _texts(frames) == [LINE], _texts(frames)


async def test_the_recovery_line_is_in_the_sessions_language():
    """A German room hears it in German, from the service and from the guard alike
    (#15 review: an empty completion speaks it on ordinary turns, not only on the rare
    collapse it was written for)."""
    german = llm_text_guard.recovery_text("de").strip()
    assert german != LINE and "Faden" in german, german
    ctx = _context({"role": "user", "content": ASK})
    provider = _Provider(EMPTY)
    llm, _ = _llm(provider, ctx)
    llm.lang_fn = lambda: "de"
    frames = await _run([llm, LLMTextGuard(lang_fn=lambda: "de")], LLMContextFrame(context=ctx))
    assert [t.strip() for t in _texts(frames)] == [german], _texts(frames)


async def test_a_later_request_no_call_came_before_is_not_the_callers_round():
    """The caller's round is spent by the request that answers it: a further request with
    no dispatch before it (a cancelled consult's run_llm, a minute on) that comes back
    empty is not the caller's turn, and gets no "could you say that again?" (#15 review)."""
    async def handler(params):
        await params.result_callback({"time": "noon"})

    done = "Done, it is noon."
    ctx = _context({"role": "user", "content": "What time is it?"})
    provider = _Provider([_chunk(tool="get_current_time"), _chunk(finish="tool_calls")],
                         [_chunk(done), _chunk(finish="stop")], EMPTY)
    llm, _ = _llm(provider, ctx)
    llm.register_function("get_current_time", handler)

    async def later(sink):
        ctx.add_message({"role": "assistant", "content": done})
        n = sink.ends() + 1
        await llm.queue_frame(LLMContextFrame(context=ctx), FrameDirection.UPSTREAM)
        await _until(lambda: sink.ends() >= n, "the later request's End")

    frames = await _run([llm], LLMContextFrame(context=ctx), _answer(llm, ctx), later)
    assert len(provider.requests) == 3, len(provider.requests)
    assert _texts(frames) == [done], _texts(frames)


async def test_a_reply_of_only_punctuation_takes_the_same_path():
    """#16's "..." as the whole answer, and a whitespace-only one: the same check as a
    silent run's (has_speech), so the retry comes first, and the guard's End line never
    adds a second. What the attempt streamed is in the journal as it was: wordless, it
    is logged whether TEAPORT_LOG_CONVERSATIONS logs words or not."""
    for junk in ("...", "\n\n"):
        ctx = _context({"role": "user", "content": "What's the capital of Peru?"})
        provider = _Provider([_chunk(junk), _chunk(finish="stop")])
        llm, _ = _llm(provider, ctx)
        with _Log() as log:
            frames = await _run([llm, LLMTextGuard()], LLMContextFrame(context=ctx))
        assert len(provider.requests) == 2, (junk, len(provider.requests))
        assert _lines(frames) == [LINE], (junk, _texts(frames))
        assert log.having(f"content {junk!r}"), (junk, log.having("completion finished"))
        assert not log.having("entirely unspeakable"), "the guard spoke a second line"


async def test_a_collapse_the_guard_cut_is_not_retried():
    """A degenerate punctuation collapse: the guard has cut it and spoken its line over
    it, so a retry would only be swallowed (and hold the End back) -- one line, one ask."""
    ctx = _context({"role": "user", "content": ASK})
    provider = _Provider([_chunk("." * 60), _chunk(finish="stop")])
    llm, _ = _llm(provider, ctx)
    frames = await _run([llm, LLMTextGuard()], LLMContextFrame(context=ctx))
    assert len(provider.requests) == 1, f"asked {len(provider.requests)} times"
    assert _lines(frames) == [LINE], _texts(frames)


async def test_a_run_of_ours_that_says_nothing_is_left_silent():
    """A run of ours may mean to say nothing (stay_silent; a re-offer after "hold on"):
    no retry, no line -- the gate is told the turn is over, as before."""
    ctx = _consulted({"role": "user", "content": "Hold on."})
    provider = _Provider(EMPTY)
    llm, _ = _llm(provider, ctx)
    llm.owed.land(CONSULT, "It opens at nine.", "call-c")
    frames = await _run([llm], RunFrame(context=ctx, keys=("call-c",), purpose="reoffer"))
    assert len(provider.requests) == 1, f"asked {len(provider.requests)} times"
    assert not _texts(frames), _texts(frames)
    assert any(isinstance(f, RunSilentFrame) for f in frames)


async def test_a_request_that_is_not_the_callers_words_is_not_retried():
    """A greeting's cue and a client note's reaction order are commits too, but not the
    caller's words: the recovery line ("Could you say that again?") answers nobody."""
    for tail in ({"role": "user", "content": "(The session just connected. Greet the user.)"},
                 {"role": "user", "content": "[app event] The user opened the settings."}):
        ctx = _context(tail)
        provider = _Provider(EMPTY)
        llm, _ = _llm(provider, ctx)
        frames = await _run([llm], LLMContextFrame(context=ctx))
        assert len(provider.requests) == 1, (tail, len(provider.requests))
        assert not _texts(frames), (tail, _texts(frames))


async def test_a_caller_who_moved_on_is_not_answered_twice():
    """A turn superseded by the caller's next one: their next turn opened while the empty
    completion streamed (the gate's caller_request_pending), or their words reached the
    context -- that turn's own completion answers, and a retry here would answer the
    same words twice."""
    def turn_opened(llm, gate, ctx):
        gate.caller_request_pending = True

    def words_came(llm, gate, ctx):
        ctx.add_message({"role": "user", "content": "Hello?"})

    for moved in (turn_opened, words_came):
        ctx = _context({"role": "user", "content": ASK})
        provider = _Provider(EMPTY, SAID)
        llm, gate = _llm(provider, ctx)
        provider.on_request = lambda n: moved(llm, gate, ctx) if n == 1 else None
        with _Log() as log:
            frames = await _run([llm], LLMContextFrame(context=ctx))
        assert len(provider.requests) == 1, (moved.__name__, len(provider.requests))
        assert not _texts(frames), (moved.__name__, _texts(frames))
        assert log.having("no retry, no recovery line"), moved.__name__


async def test_a_barge_in_cancels_without_a_retry():
    """A completion the caller barged in on is cancelled: no retry, and no line spoken
    into their turn."""
    ctx = _context({"role": "user", "content": ASK})
    hang = asyncio.Event()
    provider = _Provider([_chunk("")], hang=hang)
    llm, _ = _llm(provider, ctx)

    async def ask(_sink):
        await llm.queue_frame(LLMContextFrame(context=ctx))
        await _until(lambda: provider.requests, "the request")

    async def barge_in(_sink):
        await llm.queue_frame(InterruptionFrame())
        await _until(lambda: log.having("completion CANCELLED"), "the cancel")
    with _Log() as log:
        frames = await _run([llm, LLMTextGuard()], ask, barge_in)
    hang.set()
    assert len(provider.requests) == 1, f"asked {len(provider.requests)} times"
    assert not _texts(frames), _texts(frames)
    assert log.having("completion CANCELLED")


async def test_a_tool_round_answers_whoever_began_it():
    """A call is an answer (no retry); the answer to its result is the caller's turn
    still when the caller's words began the round -- empty, it is retried and then
    recovered -- and a run's own when a run of ours called the tool: left alone."""
    async def handler(params):
        await params.result_callback({"time": "noon"})

    for first, expect in ((None, [LINE]), ("run", [])):
        ctx = _consulted({"role": "user", "content": "What time is it?"})
        provider = _Provider([_chunk(tool="get_current_time"), _chunk(finish="tool_calls")],
                             EMPTY)
        llm, _ = _llm(provider, ctx)
        llm.register_function("get_current_time", handler)
        if first == "run":
            llm.owed.land(CONSULT, "It opens at nine.", "call-c")
            asked = RunFrame(context=ctx, keys=("call-c",), purpose="run")
        else:
            asked = LLMContextFrame(context=ctx)

        frames = await _run([llm], asked, _answer(llm, ctx))
        n = len(provider.requests)
        assert n == (3 if expect else 2), (first, n)
        assert _texts(frames) == expect, (first, _texts(frames))


async def test_an_answer_after_the_model_spoke_beside_its_call_is_left_alone():
    """Review round 1's finding: "That's all, goodnight." -- "Goodnight!" and
    end_conversation in one completion, then an empty answer to the result, which is
    right: the goodbye has been said. A round that has spoken owes nothing more, so no
    retry and no "Could you say that again?" as the box goes to sleep. The same for
    "Got it" with remember: the line would have the caller say it again, and the fact
    saved twice."""
    for said, tool in (("Goodnight!", "end_conversation"), ("Got it.", "remember")):
        ctx = _context({"role": "user", "content": "That's all, goodnight."})
        provider = _Provider([_chunk(said), _chunk(tool=tool), _chunk(finish="tool_calls")],
                             EMPTY)
        llm, _ = _llm(provider, ctx)
        llm.register_function(tool, _result)
        frames = await _run([llm, LLMTextGuard()], LLMContextFrame(context=ctx),
                            _answer(llm, ctx, {"ok": True}))
        assert len(provider.requests) == 2, (tool, len(provider.requests))
        assert _texts(frames) == [said], (tool, _texts(frames))


async def test_no_recovery_after_the_tools_that_end_the_session_or_settle_a_call():
    """end_conversation and restart_session are called with no line before them, and the
    session ends once their answer has played; answer_phone_call's results include ones
    that ask for nothing to be said (the caller hung up, the call left ringing). An empty
    answer after any of them is not recovered."""
    from teaport_brain import tools
    assert services._NO_RECOVERY_AFTER <= set(tools.TOOLS_BY_NAME), (
        sorted(services._NO_RECOVERY_AFTER - set(tools.TOOLS_BY_NAME)))
    for tool in sorted(services._NO_RECOVERY_AFTER):
        ctx = _context({"role": "user", "content": "That's all, goodnight."})
        provider = _Provider([_chunk(tool=tool), _chunk(finish="tool_calls")], EMPTY)
        llm, _ = _llm(provider, ctx)
        llm.register_function(tool, _result)
        frames = await _run([llm, LLMTextGuard()], LLMContextFrame(context=ctx),
                            _answer(llm, ctx, {"ok": True}))
        assert len(provider.requests) == 2, (tool, len(provider.requests))
        assert not _texts(frames), (tool, _texts(frames))


async def test_a_consult_landing_during_the_first_attempt_is_not_in_its_retry():
    """The retry is the request as it went out: a consult that lands while the first
    attempt streams rewrites its placeholder in the live context (Owed._rewrite), and
    the retry must not carry that answer without its note -- the book counts the reply
    as carrying the first render's records (none), so the record stays owed and its
    landing run tells it, once."""
    ctx = _consulted({"role": "assistant", "content": "I'm looking into it."},
                     {"role": "user", "content": ASK})
    provider = _Provider(EMPTY, SAID)
    llm, gate = _llm(provider, ctx)
    asked = []
    gate.ask = lambda keys, purpose, context: asked.append((tuple(keys), purpose))
    sent = []       # each request's tool results as it went out (its dicts are live)

    def request(n):
        sent.append([m["content"] for m in provider.requests[-1]["messages"]
                     if m.get("role") == "tool"])
        if n == 1:
            llm.owed.land(CONSULT, "It opens at nine.", "call-c")
    provider.on_request = request
    frames = await _run([llm], LLMContextFrame(context=ctx))
    assert len(provider.requests) == 2, f"asked {len(provider.requests)} times"
    assert sent == [[json.dumps({"status": "working_in_background"})]] * 2, sent
    reply = next(f.owed_reply for f in frames if isinstance(f, LLMFullResponseStartFrame))
    assert reply.noted == () and [r.key for r in llm.owed.owed()] == ["call-c"]
    assert asked == [(("call-c",), "run")], asked
    assert _texts(frames) == [ANSWER], _texts(frames)


async def test_a_slow_or_capped_empty_reply_gets_the_line_at_once():
    """No retry when it would cost too much or change nothing: a first attempt that took
    longer than _RETRY_WITHIN_S (the caller has waited that long already), or one that
    ended on the token cap (finish_reason=length: the same request hits it again)."""
    class _Slow(_Stream):
        async def _gen(self):
            await asyncio.sleep(0.3)
            for c in self._chunks:
                yield c

    def slow_provider():
        provider = _Provider(EMPTY, SAID)
        create = provider.create

        async def slow(**params):
            stream = await create(**params)
            return _Slow(stream._chunks)
        provider.create = slow
        return provider

    within = services._RETRY_WITHIN_S
    services._RETRY_WITHIN_S = 0.2
    try:
        for name, provider in (("slow", slow_provider()),
                               ("length", _Provider([_chunk(""), _chunk(finish="length")],
                                                    SAID))):
            ctx = _context({"role": "user", "content": ASK})
            llm, _ = _llm(provider, ctx)
            with _Log() as log:
                frames = await _run([llm, LLMTextGuard()], LLMContextFrame(context=ctx))
            assert len(provider.requests) == 1, (name, len(provider.requests))
            assert _texts(frames) == [LINE], (name, _texts(frames))
            assert not log.having("asking once more"), name
    finally:
        services._RETRY_WITHIN_S = within


async def test_a_reply_that_is_only_a_face_is_not_retried():
    """A reply of only a face -- an emoticon or an emoji -- has no speech, but it said
    that: the guard forwards it whole and history keeps it. Not asked again, and no
    line glued onto it (review round 1b: ":):)Sorry, I lost my train of thought..."). A
    mood tag alone, the emoji or the bracketed one, is MoodTag's: it strips the tag and
    speaks the line itself, once. A face after an empty first attempt ends it there."""
    def reply(text):
        return [_chunk(text), _chunk(finish="stop")]

    guard = (lambda: [LLMTextGuard()])
    moods = (lambda: [MoodTag(), LLMTextGuard()])
    for name, procs, replies, asks, expect in (
            ("an emoticon", guard, (reply(":)"),), 1, ":)"),
            ("a thumbs-up", guard, (reply("\U0001F44D"),), 1, "\U0001F44D"),
            ("a mood emoji, moods off", guard, (reply("\U0001F622"),), 1, "\U0001F622"),
            ("an emoticon, moods on", moods, (reply(":)"),), 1, ":)"),
            ("a mood emoji, moods on", moods, (reply("\U0001F622"),), 1, LINE),
            ("a bracket tag, moods on", moods, (reply("[mood: sad]"),), 1, LINE),
            ("nothing, then a face", guard, (EMPTY, reply(":)")), 2, ":)"),
            ("'...', then a face", guard, (reply("..."), reply(":)")), 2, ":)")):
        ctx = _context({"role": "user", "content": ASK})
        provider = _Provider(*replies)
        llm, _ = _llm(provider, ctx)
        frames = await _run([llm, *procs()], LLMContextFrame(context=ctx))
        assert len(provider.requests) == asks, (name, len(provider.requests))
        assert "".join(_texts(frames)).strip() == expect, (name, _texts(frames))


def test_a_face_here_is_a_face_the_guard_keeps_whole():
    """The service's emoticon is the guard's (its leading-punct strip keeps one whole):
    one that the guard kept but this did not count as a face would be retried and have
    the line glued onto it again."""
    assert services._EMOTICON.pattern in llm_text_guard._LEADING_PUNCT.pattern
    for face in (":)", ";-)", ":(", ":/", ":*", "=)", "\U0001F44D", "\u2764\ufe0f"):
        assert services._shows_a_face(face), face
        assert llm_text_guard._LEADING_PUNCT.sub("", face) == face, face
    for none in ("", "...", "\n", "\u200b", "?!"):
        assert not services._shows_a_face(none), repr(none)


async def test_with_the_guard_off_the_service_still_recovers():
    """TEAPORT_LLM_TEXT_GUARD=0: no guard in the pipeline, so nothing cuts a collapse --
    it is retried like any reply with no words -- and the line, pushed by the service
    itself, goes with no leading space when nothing came before it, and with one after
    the "..." that, with no guard, reached the TTS and history ahead of it."""
    enabled = llm_text_guard.ENABLED
    llm_text_guard.ENABLED = False
    try:
        for name, replies, expect in (("a collapse, then the answer",
                                       ([_chunk("." * 60), _chunk(finish="stop")], SAID),
                                       ["." * 60, ANSWER]),
                                      ("nothing twice", (EMPTY,), [LINE]),
                                      ("'...' twice", ([_chunk("..."), _chunk(finish="stop")],),
                                       ["...", "...", " " + LINE])):
            ctx = _context({"role": "user", "content": ASK})
            provider = _Provider(*replies)
            llm, _ = _llm(provider, ctx)
            frames = await _run([llm], LLMContextFrame(context=ctx))
            assert len(provider.requests) == 2, (name, len(provider.requests))
            assert _texts(frames) == expect, (name, _texts(frames))
    finally:
        llm_text_guard.ENABLED = enabled


async def test_an_adopted_speculation_is_retried_on_its_context():
    """An adopted speculation's request rendered in its own task; with no note it was the
    context the commit matched, so that is what the retry re-sends."""
    from teaport_brain.speculate import Speculator
    ctx = _context()
    provider = _Provider(EMPTY, SAID)
    llm, _ = _llm(provider, ctx)
    aggregator = SimpleNamespace(context=ctx, _aggregation=[],
                                 _user_turn_controller=SimpleNamespace(_user_turn=True))
    llm.speculator = Speculator(llm=llm, aggregator=aggregator)

    async def speculate(_sink):
        assert await llm.speculator.start(ASK, "test")
        await _until(lambda: llm.speculator._current is not None
                     and llm.speculator._current.done, "the speculation")
        ctx.add_message({"role": "user", "content": ASK})     # the commit
    frames = await _run([llm], speculate, LLMContextFrame(context=ctx))
    assert llm.speculator.hits == 1, "the speculation was not adopted"
    assert len(provider.requests) == 2, f"asked {len(provider.requests)} times"
    assert provider.requests[1]["messages"] == provider.requests[0]["messages"]
    assert _texts(frames) == [ANSWER], _texts(frames)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]

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
