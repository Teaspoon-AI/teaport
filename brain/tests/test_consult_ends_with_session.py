#
# #122: a consult still running when its session ends is ended with it -- the brain's part
# of it. The agent's run it started finishes.
#
# Live 2026-10-10 03:59 (the room): a consult started at :58:55, restart_session asked at
# :59:01. The client left at :06.946 and the pipeline began cancelling; at :07.293 the
# consult's narrator pushed "Still working on that -- ..." into it, pipecat reported the
# consult as a dangling task, and the agent's turn answered at :07.717 -- an answer
# dropped at delivery, the session being gone.
#
# Now the session's end, where it reaches the LLM (its CancelFrame, or the EndFrame of a
# session that ends once drained), cancels the consult
# (services.BoundedOpenAILLMService.end_with_session), and with it its narrator: nothing
# of either is pushed after that, nothing is left dangling, and nothing lands once the
# owed book has closed (Owed.close_session, which records the consult as it always did).
#
# The agent's run is NOT stopped (the owner's call on #122): an action the caller asked
# for -- "text my wife I'm late", then they hang up -- is done, as it was before, and only
# its answer is dropped (tools._run_to_its_end). The warm lane's request stays open to its
# end: OpenClaw aborts the run when its HTTP client disconnects.
#
# The session is the one build_agent_session makes for Talk, run from the user aggregator
# down (test_owed_session.py's rig). Scripted: the agent, the narrator's schedule, the
# engine's synthesis. The warm lane's gateway is a real socket on 127.0.0.1 that holds the
# request open until the test lets the agent's turn finish. Every wait is on a condition,
# bounded.
#
# Run: python test_consult_ends_with_session.py   (or via the suite)
#
import asyncio
import json
import os
import re
import socket
import sys
import threading
import time
from types import SimpleNamespace as NS

os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from loguru import logger  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    CancelFrame,
    EndFrame,
    OutputTransportMessageUrgentFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain import openclaw_client as oc  # noqa: E402
from teaport_brain import tools  # noqa: E402
from teaport_brain.agent_session import build_agent_session  # noqa: E402
from teaport_brain.followup_gate import FollowupGate  # noqa: E402
from test_post_tool_under_speech import TalkTransport, _segments  # noqa: E402

STARTUP_TIMEOUT_S = 10.0
STEP_TIMEOUT_S = 10.0
CALL_ID = "call-news"
REQ = "Summarize today's top news in Lyon"


# ---------------------------------------------------------------- the session's end

class _Agent:
    """The agent's run (oc.agent_consult): it answers once `release` is set."""

    def __init__(self):
        self.state = None
        self.release = asyncio.Event()

    async def __call__(self, message, *, timeout=None, session_key=None):
        self.state = "running"
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.state = "cancelled"
            raise
        self.state = "answered"
        return oc.ConsultOutcome("Two strikes and a festival.")


class _Call:
    """The model's openclaw_agent_consult call, as pipecat hands it to the handler."""

    def __init__(self, llm):
        self.llm = llm
        self.tool_call_id = CALL_ID
        self.arguments = {"question": REQ}
        self.results = []

    async def result_callback(self, result, properties=None):
        self.results.append(result)


class _NoProvider:
    """The model: nothing here asks it anything."""

    async def create(self, **params):
        raise AssertionError(f"a completion was asked for: {params.get('messages')}")


def _who() -> str:
    """Which of the consult's tasks is running: the consult, its narrator, or neither."""
    task = asyncio.current_task()
    if task is None:
        return ""
    if task.get_name().endswith("::_consult_and_land"):    # pipecat's name for it
        return "consult"
    if getattr(task.get_coro(), "__name__", "") == "_consult_progress":
        return "narrator"
    return ""


def _task(name: str) -> asyncio.Task:
    found = [t for t in asyncio.all_tasks()
             if t.get_name().endswith("::" + name)
             or getattr(t.get_coro(), "__name__", "") == name]
    assert len(found) == 1, f"{name}: {found}"
    return found[0]


class Rig:
    """The session build_agent_session makes for Talk, from the user aggregator down. Every
    frame the LLM pushes is recorded with the task that pushed it, and whether the
    session's end had begun."""

    def __init__(self):
        session = build_agent_session(TalkTransport(), front_end="talk")
        self.session, self.owed, self.llm = session, session.owed, session.llm
        inner = next(p for p in session.task.pipeline.processors if isinstance(p, Pipeline))
        procs = inner.processors
        first = next(i for i, p in enumerate(procs) if isinstance(p, LLMUserAggregator))
        procs = [p for p in procs[first:] if not type(p).__name__.startswith("Pipeline")]
        self.gate = next(p for p in procs if isinstance(p, FollowupGate))
        session.tts._synth_text = _segments
        self.llm._client = NS(chat=NS(completions=_NoProvider()))
        self.llm._warm_closed = True
        self.pushed = []        # (frame, _who(), the session's end had begun)
        self.ending = False
        push = self.llm.push_frame

        async def recording(frame, direction=FrameDirection.DOWNSTREAM):
            self.pushed.append((frame, _who(), self.ending))
            await push(frame, direction)
        self.llm.push_frame = recording
        self.lands = []         # (key, text, failure, the book closed already)
        land = self.owed.land

        def recording_land(request, text, key, **kw):
            self.lands.append((key, text, kw.get("failure"), self.owed._closed))
            return land(request, text, key, **kw)
        self.owed.land = recording_land
        self.task = PipelineTask(Pipeline(procs), observers=[session.ledger],
                                 cancel_on_idle_timeout=False)
        self.started = asyncio.Event()

        @self.task.event_handler("on_pipeline_started")
        async def _started(_task, _frame):
            self.started.set()

    async def start(self):
        self._run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.wait_for(self.started.wait(), STARTUP_TIMEOUT_S)

    async def end(self, how: str):
        """The session ends: "cancel" as when the client leaves, a call hangs up or the
        session restarts; "drained" as stop_when_done ends it (an EndFrame)."""
        self.ending = True
        if how == "cancel":
            await self.task.cancel()
        else:
            await self.task.stop_when_done()
        await asyncio.wait_for(self._run, STEP_TIMEOUT_S)

    async def close(self):
        if not self._run.done():
            await self.task.cancel()
            await self._run

    def seed(self):
        """The turn that asked: the caller's words, the model's call, its placeholder."""
        self.session.context.add_messages([
            {"role": "user", "content": "What's in the news in Lyon today?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": CALL_ID, "type": "function",
                 "function": {"name": "openclaw_agent_consult",
                              "arguments": json.dumps({"question": REQ})}}]},
            {"role": "tool", "tool_call_id": CALL_ID,
             "content": json.dumps({"status": "working_in_background"})}])

    def placeholder(self) -> dict:
        return next(json.loads(m["content"]) for m in self.session.context.get_messages()
                    if m.get("role") == "tool" and m.get("tool_call_id") == CALL_ID)


async def until(predicate, timeout=STEP_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for a condition"
        await asyncio.sleep(0.01)


async def _end_mid_consult(how: str):
    agent = _Agent()
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    saved = oc.agent_consult, tools._PROGRESS_SCHEDULE
    oc.agent_consult = agent
    # Its first line at once, its next long after the test: the narrator is speaking
    # while the session runs, and would still be waiting to speak when it ends.
    tools._PROGRESS_SCHEDULE = (0.0, 600.0)
    try:
        rig = Rig()
        await rig.start()
        try:
            rig.seed()
            call = _Call(rig.llm)
            await tools._openclaw_agent_consult(call, owed=rig.owed, gate=rig.gate,
                                                relay=False, tts=rig.session.tts)
            assert call.results[0]["status"] == "working_in_background", call.results
            await until(lambda: agent.state == "running")
            consult, narrator = _task("_consult_and_land"), _task("_consult_progress")
            # Whether the consult had ended when the session's end reached the gate, far
            # below the LLM, whose cleanup closes the owed book.
            ended_first = []
            process = rig.gate.process_frame

            async def gate_sees(frame, direction):
                if isinstance(frame, (CancelFrame, EndFrame)):
                    ended_first.append(consult.done())
                await process(frame, direction)
            rig.gate.process_frame = gate_sees
            await until(lambda: any(isinstance(f, TTSSpeakFrame) and who == "narrator"
                                    for f, who, _ in rig.pushed))
            assert any(isinstance(f, OutputTransportMessageUrgentFrame) and who == "consult"
                       for f, who, _ in rig.pushed), "the consult told its client nothing"
            assert rig.gate.consults_running == 1
            await rig.end(how)
        finally:
            await rig.close()
        # The agent's run goes on past the session; once it answers, the answer is dropped.
        assert agent.state == "running", (
            f"the agent's run is {agent.state!r} after the session's end: an action the "
            "caller asked for would not be done")
        agent.release.set()
        await until(lambda: any("ended after its session did" in line for line in lines))
    finally:
        oc.agent_consult, tools._PROGRESS_SCHEDULE = saved
        logger.remove(sink)

    assert agent.state == "answered", agent.state
    assert any("answered (27 chars); dropped" in line for line in lines), "the drop unlogged"
    assert consult.cancelled(), consult
    assert ended_first == [True], (
        f"the consult was still running when the session's end reached the gate: {ended_first}")
    assert narrator.done(), "the narrator outlived the session"
    after = [(type(f).__name__, who) for f, who, ending in rig.pushed if ending and who]
    assert not after, f"pushed into the ending session: {after}"
    dangling = [line for line in lines if "dangling tasks" in line]
    assert not dangling, dangling
    assert rig.gate.consults_running == 0, rig.gate.consults_running
    assert not getattr(rig.llm, "_teaport_progress_active", False)
    # The owed book closes it with the session as it did a consult that ran on: its
    # placeholder says it did not report back. Nothing else lands, then or after -- not
    # the agent's answer, which came after the session's end.
    assert rig.lands == [(CALL_ID, None, "unknown", True)], rig.lands
    assert rig.placeholder()["status"] == "unknown", rig.placeholder()
    # The cancellation itself is no failure: nothing logs or tells one.
    told = [line for line in lines if "openclaw_agent_consult(async)" in line
            and any(w in line for w in ("failed", "direct consult", "unfinished"))]
    assert not told, told


async def test_a_session_that_ends_mid_consult_ends_the_consult_and_its_narrator():
    """The client leaves (or the call hangs up, or the session restarts): a CancelFrame."""
    await _end_mid_consult("cancel")


async def test_a_session_ended_once_drained_ends_its_consult_too():
    """stop_when_done (the no-model line said, a service written off): an EndFrame."""
    await _end_mid_consult("drained")


# ---------------------------------------------------------------- the warm lane's request

class _Gateway:
    """OpenClaw's /v1/chat/completions on a real socket on 127.0.0.1. It takes one request
    and holds it open -- the agent's turn running -- until `finish` is set, then answers
    it. `result`: "answered" when the answer went out to a client still connected,
    "aborted" when the client closed the connection first (which aborts OpenClaw's run)."""

    def __init__(self, answer: str):
        self.answer = answer
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}"
        self.request = b""
        self.result = None
        self.requested = threading.Event()
        self.finish = threading.Event()
        self.done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    @staticmethod
    def _whole(data: bytes) -> bool:
        head, sep, body = data.partition(b"\r\n\r\n")
        length = re.search(rb"(?im)^content-length:\s*(\d+)", head)
        return bool(sep) and len(body) >= (int(length.group(1)) if length else 0)

    @staticmethod
    def _hung_up(conn) -> bool:
        conn.setblocking(False)
        try:
            return conn.recv(1) == b""
        except BlockingIOError:
            return False
        finally:
            conn.settimeout(STEP_TIMEOUT_S)

    def _serve(self):
        conn, _ = self._listener.accept()
        self._listener.close()
        try:
            with conn:
                conn.settimeout(STEP_TIMEOUT_S)
                while not self._whole(self.request):
                    data = conn.recv(65536)
                    if not data:
                        self.result = "closed before the whole request"
                        return
                    self.request += data
                self.requested.set()
                while not self.finish.wait(0.02):
                    if self._hung_up(conn):
                        self.result = "aborted"
                        return
                if self._hung_up(conn):
                    self.result = "aborted"
                    return
                body = json.dumps({"choices": [{"message": {"content": self.answer}}]}).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self.result = "answered"
        except OSError as e:
            self.result = self.result or f"aborted ({e!r})"
        finally:
            self.done.set()


async def _wait(event: threading.Event, timeout: float) -> bool:
    return await asyncio.get_running_loop().run_in_executor(None, event.wait, timeout)


async def test_the_agents_request_runs_to_its_end_after_the_session():
    """SIP: "text my wife I'm late", then the caller hangs up. The consult ends with the
    session, but the warm lane's request to OpenClaw stays open until the agent's turn is
    done -- its client disconnecting would abort the run, the text unsent -- and its
    answer, once it comes, is dropped."""
    gateway = _Gateway("Sent.")
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    saved = oc.GATEWAY_URL, oc._token
    oc.GATEWAY_URL, oc._token = gateway.url, (lambda: "tok")
    try:
        rig = Rig()
        await rig.start()
        try:
            rig.seed()
            await tools._openclaw_agent_consult(_Call(rig.llm), owed=rig.owed, gate=rig.gate,
                                                relay=False, tts=rig.session.tts)
            assert await _wait(gateway.requested, STEP_TIMEOUT_S), "no request reached it"
            consult = _task("_consult_and_land")
            await rig.end("cancel")
        finally:
            await rig.close()
        assert consult.cancelled(), consult
        assert not gateway.done.is_set(), f"the request ended with the session: {gateway.result}"
        gateway.finish.set()
        assert await _wait(gateway.done, STEP_TIMEOUT_S), "the gateway never finished"
        assert gateway.result == "answered", f"the agent's run was {gateway.result}"
        await until(lambda: any("ended after its session did" in line for line in lines))
    finally:
        oc.GATEWAY_URL, oc._token = saved
        logger.remove(sink)
    assert b"POST /v1/chat/completions" in gateway.request, gateway.request[:80]
    assert any("answered (5 chars); dropped" in line for line in lines)
    assert rig.lands == [(CALL_ID, None, "unknown", True)], rig.lands


# ---------------------------------------------------------------- the teardown's gap

async def test_a_task_registered_after_the_session_ended_is_cancelled_at_once():
    """A consult registered once the session's end has taken its list of tasks (a tool call
    still running as the session ended) is cancelled there and then, not missed."""
    rig = Rig()
    await rig.start()
    try:
        await rig.end("cancel")
    finally:
        await rig.close()
    late = asyncio.get_running_loop().create_task(asyncio.Event().wait())
    assert rig.llm.end_with_session(late) is late
    done, _ = await asyncio.wait({late}, timeout=STEP_TIMEOUT_S)
    assert late in done and late.cancelled(), "registered after the end, it ran on"


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
