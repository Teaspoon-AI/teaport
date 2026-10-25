"""What the OpenClaw host tells a voice session (issue #230, host_context.py).

A Talk session's plugin sends OpenClaw's req.instructions and req.tools as the first
frame on /talk ({"type": "session_config", ...}, announced with ?session_config=1). The
brain reads it before it builds the session: the system prompt starts with the host's
instructions, the model's consult is OpenClaw's openclaw_agent_consult with the host's
schema, and openclaw_agent_control (which the relay does not run) is not offered. Without
the flag nothing is waited for. And the consult is called openclaw_agent_consult
everywhere: the old name is gone from the repo.

All host text here is synthetic."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

# Before teaport_brain.services is imported anywhere (see test_service_unusable): a built
# session constructs its clients; nothing is sent to them.
os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.processors.frame_processor import FrameProcessor  # noqa: E402

from teaport_brain import host_context, persona, tools  # noqa: E402
from teaport_brain.agent_backend import HAS_AGENT  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOST_TEXT = ("You are a test voice interface. Call openclaw_agent_consult for anything "
             "that needs the agent.\n\nAdditional realtime instructions:\nThe user's name "
             "is Sam Example.")

# OpenClaw's two tools for a transcript-driven provider, shaped as the relay sends them
# (RealtimeVoiceTool), with descriptions of our own so a test can tell whose schema won.
HOST_CONSULT = {
    "type": "function", "name": "openclaw_agent_consult",
    "description": "HOST DESCRIPTION of the consult.",
    "parameters": {"type": "object",
                   "properties": {"question": {"type": "string", "description": "Q."},
                                  "context": {"type": "string", "description": "C."}},
                   "required": ["question"]},
}
HOST_CONTROL = {
    "type": "function", "name": "openclaw_agent_control",
    "description": "Steer a running consult.",
    "parameters": {"type": "object", "properties": {"action": {"type": "string"}},
                   "required": ["action"]},
}
FRAME = {"type": "session_config", "instructions": HOST_TEXT,
         "tools": [HOST_CONSULT, HOST_CONTROL]}


# ------------------------------------------------------------------ the frame

def test_a_session_config_frame_becomes_a_host_context():
    host = host_context.from_frame(FRAME)
    assert host.instructions == HOST_TEXT
    assert host.tools == (HOST_CONSULT, HOST_CONTROL)
    # An empty frame is a host that composed nothing, not a missing frame.
    assert host_context.from_frame({"type": "session_config", "instructions": "",
                                    "tools": []}) == host_context.HostContext()
    # Fields of the wrong type count as absent; anything else is not a frame at all.
    assert host_context.from_frame({"type": "session_config", "instructions": 7,
                                    "tools": "x"}) == host_context.HostContext()
    assert host_context.from_frame({"type": "context", "text": "hi"}) is None
    assert host_context.from_frame(["session_config"]) is None


# ------------------------------------------------------------------ the tools

def test_the_host_schema_replaces_the_brains_copy_and_control_is_not_offered():
    schemas = tools.host_tool_schemas([HOST_CONSULT, HOST_CONTROL,
                                       {"type": "function", "name": "some_future_tool",
                                        "parameters": {"type": "object"}}])
    # Only a tool the brain can run, and never openclaw_agent_control: the relay shows
    # a call to it as a card and runs nothing.
    assert [s.name for s in schemas] == ["openclaw_agent_consult"]
    assert schemas[0].description == HOST_CONSULT["description"]
    assert schemas[0].properties == HOST_CONSULT["parameters"]["properties"]
    assert schemas[0].required == ["question"]
    # A malformed definition is dropped, and the brain's copy stands.
    assert tools.host_tool_schemas([{"name": "openclaw_agent_consult",
                                     "parameters": "nope"}]) == ()

    ctx = tools.ToolContext(has_tts=True, host_schemas=schemas)
    offered = {s.name: s for s in tools.build_tools_schema(ctx).standard_tools}
    assert "openclaw_agent_control" not in offered
    if HAS_AGENT:
        assert offered["openclaw_agent_consult"].description == HOST_CONSULT["description"]
    # Every other tool keeps its own schema, in the same order as without a host.
    plain = tools.build_tools_schema(tools.ToolContext(has_tts=True)).standard_tools
    assert [s.name for s in plain] == list(offered)
    assert all(a is b for a, b in zip(plain, offered.values())
               if a.name != "openclaw_agent_consult")


def test_the_fallback_consult_is_openclaws_tool():
    """The copy for a session the host gave no tools (room, phone): OpenClaw's
    REALTIME_VOICE_AGENT_CONSULT_TOOL, by name and parameters."""
    s = tools.OPENCLAW_AGENT_CONSULT
    assert s.name == "openclaw_agent_consult"
    assert list(s.properties) == ["question", "context", "responseStyle"]
    assert s.required == ["question"]
    assert all(p["type"] == "string" for p in s.properties.values())
    assert tools.TOOLS_BY_NAME["openclaw_agent_consult"].schema is s


def test_the_consult_reads_openclaws_arguments():
    assert tools._consult_args({"question": "  is the shop open?  "}) == {
        "question": "is the shop open?"}
    # OpenClaw's aliases, in its order, and its two optional fields.
    assert tools._consult_args({"query": "q", "task": "t"}) == {"question": "q"}
    assert tools._consult_args({"question": "q", "context": " c ", "responseStyle": ""}) == {
        "question": "q", "context": "c"}
    assert tools._consult_args({"request": "q"}) is None
    assert tools._consult_args(None) is None
    assert tools._consult_message({"question": "q", "context": "c", "responseStyle": "s"}) == (
        "q\n\nContext:\nc\n\nSpoken style:\ns")


class _LLM:
    def __init__(self):
        self.pushed = []
        self.tasks = []

    async def push_frame(self, frame, *a, **kw):
        self.pushed.append(frame)

    def create_task(self, coro):
        coro.close()        # the background waiter is not under test here
        return None


def test_one_consult_call_reaches_the_relay_with_openclaws_name_and_arguments():
    """The relay RUNS every openclaw_agent_consult it is handed, so the display card
    tools._wrap sends for other tools must not go out for this one: one call, the
    handler's, with OpenClaw's arguments."""
    from pipecat.processors.aggregators.llm_context import LLMContext

    from teaport_brain.owed import Owed

    llm = _LLM()
    results = []

    async def cb(result, **kw):
        results.append(result)

    params = SimpleNamespace(llm=llm, tool_call_id="tc-1", result_callback=cb,
                             arguments={"question": "what's on today?", "context": "at home"})
    handler = tools._wrap("openclaw_agent_consult",
                          tools._bind_openclaw_agent_consult(tools.ToolContext(owed=Owed(LLMContext([])))),
                          lambda: "en")
    llm._teaport_spoke = True   # the model spoke first: no fallback line
    asyncio.run(handler(params))
    calls = [f.message for f in llm.pushed
             if getattr(f, "message", {}).get("type") == "tool_call"]
    assert len(calls) == 1, calls
    assert calls[0]["name"] == "openclaw_agent_consult"
    assert calls[0]["args"] == {"question": "what's on today?", "context": "at home"}
    assert results and results[0]["status"] == "working_in_background"
    from teaport_brain import consult_bridge
    consult_bridge.cancel(calls[0]["call_id"])


# ------------------------------------------------------------------ the prompt

def test_the_prompt_starts_with_exactly_the_host_text():
    prompt = persona.build_system_prompt(HOST_TEXT)
    assert prompt.startswith(HOST_TEXT + "\n\n")
    # Then the brain's voice rules, as before.
    assert prompt.endswith(persona.voice_overlay())


class _Stub(FrameProcessor):
    pass


class _Transport:
    def __init__(self):
        self._input, self._output = _Stub(name="stub-in"), _Stub(name="stub-out")

    def input(self):
        return self._input

    def output(self):
        return self._output


def test_a_session_built_with_a_host_context_uses_it(monkeypatch):
    from teaport_brain import agent_session
    from teaport_brain.agent_session import build_agent_session

    # The brain's own persona, synthetic: never this machine's workspace files.
    monkeypatch.setattr(agent_session, "load_persona", lambda: "BRAIN PERSONA.")

    async def build(host):
        return build_agent_session(_Transport(), context_notes=True, front_end="talk",
                                   host=host)

    session = asyncio.run(build(host_context.from_frame(FRAME)))
    system = session.context.get_messages()[0]["content"]
    assert system.startswith(HOST_TEXT), system[:200]
    offered = {s.name: s for s in session.context.tools.standard_tools}
    assert "openclaw_agent_control" not in offered
    if HAS_AGENT:
        assert offered["openclaw_agent_consult"].description == HOST_CONSULT["description"]

    # A host that composed nothing: the brain's own persona, and its own schema.
    session = asyncio.run(build(host_context.HostContext()))
    system = session.context.get_messages()[0]["content"]
    assert system.startswith("BRAIN PERSONA.\n\n"), system[:80]


# ------------------------------------------------------------------ the wait

def _run_talk(query, frames, *, wait=None):
    """gateway_server.run_relay_bot with fakes around it. `frames` are what the client
    sends (ASGI receive messages, in order; when they run out, receive() blocks).
    Returns (what happened in order, the build kwargs or None)."""
    from teaport_brain import gateway_server as gs

    order, built = [], {}

    class _Session:
        task = SimpleNamespace(queue_frames=None, cancel=None)
        client_notes = SimpleNamespace(limits=lambda: {"max_chars": 7})

    class _FakeTransport:
        def __init__(self, **kw):
            pass

        def event_handler(self, name):
            return lambda fn: fn

    def build(transport, **kw):
        order.append("build")
        built.update(kw)
        return _Session()

    class _Runner:
        def __init__(self, **kw):
            pass

        async def run(self, task):
            order.append("run")

    pending = list(frames)

    async def receive():
        order.append("receive")
        if pending:
            return pending.pop(0)
        await asyncio.sleep(3600)

    async def send_text(text):
        order.append(json.loads(text)["type"])

    saved = {k: getattr(gs, k) for k in
             ("FastAPIWebsocketTransport", "build_agent_session", "PipelineRunner",
              "SESSION_CONFIG_WAIT_SECS")}
    saved_arbiter = gs.arb.ARBITER
    gs.FastAPIWebsocketTransport = _FakeTransport
    gs.build_agent_session = build
    gs.PipelineRunner = _Runner
    gs.arb.ARBITER = gs.arb.SessionArbiter()
    if wait is not None:
        gs.SESSION_CONFIG_WAIT_SECS = wait
    try:
        asyncio.run(gs.run_relay_bot(SimpleNamespace(
            query_params=query, send_text=send_text, receive=receive)))
    finally:
        for k, v in saved.items():
            setattr(gs, k, v)
        gs.arb.ARBITER = saved_arbiter
    return order, (built or None)


def _text(msg):
    return {"type": "websocket.receive", "text": json.dumps(msg)}


def test_the_brain_reads_the_frame_before_it_builds_and_says_hello_after():
    order, built = _run_talk({"session_config": "1", "captions": "2"},
                             [_text(FRAME), {"type": "websocket.receive", "bytes": b"\0\0"}])
    assert order == ["receive", "build", "hello", "run"], order
    assert built["host"] == host_context.from_frame(FRAME)


def test_without_the_flag_the_brain_never_waits():
    order, built = _run_talk({"captions": "2"}, [_text(FRAME)])
    assert "receive" not in order, order
    assert built["host"] is None
    # Nor for the room mic, whatever it says: its bridge is not OpenClaw.
    order, built = _run_talk({"features": "local", "session_config": "1"}, [_text(FRAME)])
    assert "receive" not in order and built["host"] is None, order


def test_a_frame_that_does_not_come_as_promised_builds_without_it():
    order, built = _run_talk({"session_config": "1"}, [], wait=0.05)
    assert order == ["receive", "build", "hello", "run"], order
    assert built["host"] is None
    # Audio where the frame should be: built without it, never stuck.
    order, built = _run_talk({"session_config": "1"},
                             [{"type": "websocket.receive", "bytes": b"\0\0"}])
    assert built["host"] is None and order[:2] == ["receive", "build"], order


def test_a_client_that_leaves_while_the_brain_waits_gets_no_session():
    order, built = _run_talk({"session_config": "1"}, [{"type": "websocket.disconnect"}])
    assert order == ["receive"] and built is None, order


# ------------------------------------------------------------------ one name

def test_the_old_consult_name_is_gone():
    """One consult tool, OpenClaw's, on every front end: the brain's own name for it is
    not kept anywhere, not even as an alias. The historical run logs (brain/test/RUNLOG-*)
    record what ran then and keep it."""
    old = "ask_" + "openclaw"
    if not os.path.isdir(os.path.join(REPO, "plugin")):
        pytest.skip("not a repository checkout")
    hits = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "__pycache__",
                                                ".claude")]
        for f in files:
            if f.startswith("RUNLOG-") or not f.endswith(
                    (".py", ".js", ".mjs", ".md", ".toml", ".sh", ".tla", ".json", ".html",
                     ".txt", ".cfg", ".service", ".conf")):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, encoding="utf-8") as fh:
                    if old in fh.read().lower():
                        hits.append(os.path.relpath(path, REPO))
            except (OSError, UnicodeDecodeError):
                continue
    assert not hits, hits


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
