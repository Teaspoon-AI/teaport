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
                                    "tools": []}) == host_context.HostContext(voice_rules=True)
    # Fields of the wrong type count as absent; anything else is not a frame at all.
    assert host_context.from_frame({"type": "session_config", "instructions": 7,
                                    "tools": "x"}) == host_context.HostContext(voice_rules=True)
    assert host_context.from_frame({"type": "context", "text": "hi"}) is None
    assert host_context.from_frame(["session_config"]) is None


# ------------------------------------------------------------------ the tools

def test_the_host_schema_replaces_the_brains_copy_and_control_is_not_offered():
    schemas = tools.host_tool_schemas([HOST_CONSULT, HOST_CONTROL,
                                       {"type": "function", "name": "some_future_tool",
                                        "parameters": {"type": "object"}},
                                       {"type": "function", "name": "web_search",
                                        "parameters": {"type": "object", "properties": {
                                            "q": {"type": "string"}}}}])
    # Only the consult's, and never openclaw_agent_control (the relay shows a call to it
    # as a card and runs nothing). Not a host tool that shares a brain tool's name either:
    # the brain's handler reads the brain's schema.
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
    """The copy for a session the host gave no tools: OpenClaw 2026.9.7's
    REALTIME_VOICE_AGENT_CONSULT_TOOL, by name and parameters (confirmationId included)."""
    s = tools.OPENCLAW_AGENT_CONSULT
    assert s.name == "openclaw_agent_consult"
    assert list(s.properties) == ["question", "context", "responseStyle", "confirmationId"]
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
    # A risky action's second call, after the user said yes aloud: the relay's id goes
    # back to it, or the relay only asks again. Not when empty or not a string.
    assert tools._consult_args({"question": "q", "confirmationId": " c-1 "}) == {
        "question": "q", "confirmationId": "c-1"}
    assert tools._consult_args({"question": "q", "confirmationId": ""}) == {"question": "q"}
    assert tools._consult_args({"question": "q", "confirmationId": 7}) == {"question": "q"}
    # The CLI lane's message is OpenClaw's chat rendering: no id in it.
    assert tools._consult_message({"question": "q", "confirmationId": "c-1"}) == "q"
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


# The consult paragraph under OpenClaw's Talk text: the #230 eval's arm B2, word for word
# (34 synthetic cases x 10, gpt-oss-120b: no phantom control calls, grounding equal to the
# persona-file prompt). Pinned whole: a rewording here is a new arm, to be run first.
B2_CONSULT = (
    "Where the instructions above differ, these win. You have no openclaw_agent_control "
    "tool here; never call it. When the user asks how a request that is in progress is "
    "going, or wants it cancelled or changed, just answer in words; that is taken care of "
    "for you. Before you call openclaw_agent_consult, say one short natural sentence about "
    "what you're doing — in your own words, specific to this request, never a stock phrase "
    "like 'Let me check that for you' — then call it in the same response. While an "
    "openclaw_agent_consult request is still in progress you do not yet know its answer: "
    "never guess or make one up, even if the user says 'go ahead' or 'start' — say it is "
    "still in progress; you will be told the moment it completes. Answer from the "
    "conversation when the answer is already in it, such as what you just said or what a "
    "tool already returned; call openclaw_agent_consult only for something you do not have "
    "yet. You cannot see the agent's other sessions from this call, so never describe them: "
    "if the user asks what else you are doing or what is running, call "
    "openclaw_agent_consult to find out. ")


def _default_tools():
    return tools.active_tools(tools.ToolContext(has_tts=True))


@pytest.mark.skipif(not HAS_AGENT, reason="the consult needs a gateway")
def test_under_talks_text_the_overlay_is_the_evals_b2():
    prompt = persona.build_system_prompt(HOST_TEXT, _default_tools(), host=persona.HOST_RULES)
    assert prompt == (HOST_TEXT + "\n\n" + persona._TOOLS_HOSTED + B2_CONSULT
                      + persona._DELIVERY)
    # The tools paragraph is the tuned one with the consult taken out, nothing else.
    assert persona._TOOLS_HOSTED == persona._TOOLS_WITH_AGENT.replace(
        persona._CONSULT_HINT + ", ", "").replace(
        "A web search, page fetch, or openclaw_agent_consult", "A web search or page fetch"
    ).replace(persona._CONSULT_IN_PROGRESS + " ", "")


@pytest.mark.skipif(not HAS_AGENT, reason="the consult needs a gateway")
def test_under_the_rooms_text_the_overlay_says_when_to_consult_and_no_more():
    """The room's and the phone's host text is the agent paragraph and profile: no rules
    to override, no control tool, no relay run control. So none of the sentences that
    are only true under Talk's text, and a sentence saying when to consult instead."""
    prompt = persona.build_system_prompt(HOST_TEXT, _default_tools(), host=persona.HOST_CONTEXT)
    overlay = prompt[len(HOST_TEXT) + 2:]
    for talk_only in ("instructions above", "openclaw_agent_control", "taken care of for you"):
        assert talk_only not in overlay, talk_only
    for kept in (persona._CONSULT_WHEN, persona._CONSULT_PREAMBLE, persona._CONSULT_IN_PROGRESS,
                 persona._FROM_THE_CONVERSATION, persona._OTHER_SESSIONS):
        assert kept in overlay, kept
    assert overlay.startswith(persona._TOOLS_HOSTED) and overlay.endswith(persona._DELIVERY)


def test_the_persona_file_prompt_is_unchanged():
    """No host text: the tuned overlay as before (#230 changes nothing there), and none of
    the host paragraph, which speaks of instructions above that are not there."""
    prompt = persona.build_system_prompt("BRAIN PERSONA.", _default_tools())
    assert prompt == "BRAIN PERSONA.\n\n" + persona.voice_overlay()
    assert "instructions above" not in prompt and persona._OTHER_SESSIONS not in prompt
    # An empty host text is no host text.
    assert persona.build_system_prompt("  ", _default_tools(), host=persona.HOST_RULES) == (
        persona.build_system_prompt(None, _default_tools()))


def test_a_consult_that_is_off_is_named_as_missing_under_talks_text(monkeypatch):
    """Talk's text tells the model to call openclaw_agent_consult; with the consult off
    (or no gateway) the override names it too, so the model never calls a tool it lacks."""
    off = [t for t in _default_tools() if t.name != "openclaw_agent_consult"]
    prompt = persona.build_system_prompt(HOST_TEXT, off, host=persona.HOST_RULES)
    assert ("You have no openclaw_agent_control or openclaw_agent_consult tool here; never "
            "call them.") in prompt
    for consult_only in (persona._RUN_CONTROL_IN_WORDS, persona._CONSULT_PREAMBLE,
                         persona._OTHER_SESSIONS):
        assert consult_only not in prompt
    # The room's text names no tool, so nothing is said there.
    room = persona.build_system_prompt(HOST_TEXT, off, host=persona.HOST_CONTEXT)
    assert "openclaw_agent" not in room[len(HOST_TEXT):]


@pytest.mark.skipif(not HAS_AGENT, reason="the consult needs a gateway")
def test_a_composed_tools_paragraph_under_host_text_keeps_the_lookup(monkeypatch):
    """With the web tools off the consult is the only way to look things up: the tools
    paragraph must not say the assistant cannot look anything up."""
    few = [t for t in _default_tools() if t.name not in ("web_search", "web_fetch",
                                                         "search_memory")]
    for host in (persona.HOST_RULES, persona.HOST_CONTEXT):
        prompt = persona.build_system_prompt(HOST_TEXT, few, host=host)
        assert persona._CANNOT_LOOK_UP not in prompt, host
        assert persona._CONSULT_PREAMBLE in prompt, host


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
    if HAS_AGENT:
        assert B2_CONSULT in system    # Talk's text: the overlay for HOST_RULES
    offered = {s.name: s for s in session.context.tools.standard_tools}
    assert "openclaw_agent_control" not in offered
    if HAS_AGENT:
        assert offered["openclaw_agent_consult"].description == HOST_CONSULT["description"]
    # The room's: the overlay for HOST_CONTEXT.
    session = asyncio.run(build(ROOM_HOST))
    system = session.context.get_messages()[0]["content"]
    assert system.startswith(ROOM_HOST.instructions) and "openclaw_agent_control" not in system
    if HAS_AGENT:
        assert persona._CONSULT_WHEN in system

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

    async def fetch(front_end, *, patience=0.0, timeout=None):
        order.append(f"fetch:{front_end}")
        built["patience"] = patience
        return ROOM_HOST

    saved_fetch = gs.host_context.fetch
    gs.host_context.fetch = fetch
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
        gs.host_context.fetch = saved_fetch
    return order, (built if "host" in built else None)


# What the plugin's voice-context route gives the room (synthetic).
ROOM_HOST = host_context.HostContext(instructions="Agent context: synthetic room text.")


def _text(msg):
    return {"type": "websocket.receive", "text": json.dumps(msg)}


def test_the_brain_reads_the_frame_before_it_builds_and_says_hello_after():
    order, built = _run_talk({"session_config": "1", "captions": "2"},
                             [_text(FRAME), {"type": "websocket.receive", "bytes": b"\0\0"}])
    assert order == ["receive", "build", "hello", "run"], order
    assert built["host"] == host_context.from_frame(FRAME)


def test_without_the_flag_the_brain_never_waits_and_asks_the_plugin():
    """A /talk client with no session_config -- the Discord bridge, a plugin before #230
    -- is not left with the persona file: it gets what the room gets, from the plugin's
    route, under its own name. Nothing is waited for on the socket."""
    order, built = _run_talk({"client": "discord", "voice": "af_nova"}, [_text(FRAME)])
    assert "receive" not in order, order
    assert order[:3] == ["fetch:discord", "build", "hello"], order
    assert built["host"] is ROOM_HOST and built["patience"] == host_context.PATIENCE_S
    order, built = _run_talk({"captions": "2"}, [_text(FRAME)])
    assert order[:2] == ["fetch:talk", "build"], order
    # Nor for the room mic, whatever it says: its bridge is not OpenClaw. The room asks
    # the gateway's teaport plugin instead (host_context.fetch), before the build.
    order, built = _run_talk({"features": "local", "session_config": "1"}, [_text(FRAME)])
    assert "receive" not in order, order
    assert order[:3] == ["fetch:room", "build", "hello"], order
    assert built["host"] is ROOM_HOST and built["patience"] == host_context.PATIENCE_S
    # A wake session opening asleep can wait longer for a gateway still starting.
    order, built = _run_talk({"features": "local", "wake": "hey teaport"}, [])
    assert order[:2] == ["fetch:room", "build"], order
    assert built["patience"] == host_context.PATIENCE_ASLEEP_S


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


# ------------------------------------------------------------------ the room and phone fetch

class _Gateway:
    """A loopback HTTP server playing the gateway's /teaport/voice-context route."""

    def __init__(self, status, body, *, before=()):
        """Answers `before` first -- (status, body, headers) each, in order -- then
        (status, body) for good."""
        import http.server
        import threading

        gw = self
        self.requests = []
        queue = list(before)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                gw.requests.append((self.path, self.headers.get("Authorization")))
                code, answer, headers = queue.pop(0) if queue else (status, body, {})
                data = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _fetch(monkeypatch, status, body, front_end="sip", token="tok-1", *, before=(),
           patience=0.0):
    from teaport_brain import openclaw_client as oc

    gw = _Gateway(status, body, before=before)
    try:
        monkeypatch.setattr(oc, "GATEWAY_URL", gw.url)
        monkeypatch.setattr(oc, "_token", lambda: token)
        monkeypatch.setattr(host_context, "HAS_AGENT", True)
        host = asyncio.run(host_context.fetch(front_end, patience=patience))
    finally:
        gw.close()
    return host, gw.requests


def _warnings():
    from loguru import logger

    seen = []
    return seen, logger.add(lambda m: seen.append(str(m)), level="INFO")


ROUTE_BODY = {"version": 1, "agentId": "main", "openclawVersion": "2026.9.7",
              "instructions": "Agent context: synthetic.\n\n### IDENTITY.md\nName: Test",
              "tools": [HOST_CONSULT]}


def test_the_room_and_phone_ask_the_plugin_with_the_gateway_token(monkeypatch):
    host, requests = _fetch(monkeypatch, 200, ROUTE_BODY, front_end="sip")
    assert requests == [("/teaport/voice-context?front_end=sip", "Bearer tok-1")]
    assert host == host_context.HostContext(instructions=ROUTE_BODY["instructions"],
                                            tools=(HOST_CONSULT,))


def test_a_gateway_that_cannot_answer_falls_back_with_a_warning(monkeypatch):
    from loguru import logger

    warnings = []
    sink = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        # Starting or restarting (503), an OpenClaw without the SDK, a plugin before the
        # route (404 with no JSON), an unknown response version.
        for status, body in [(503, {"error": "host_unsupported"}), (404, b"not found"),
                             (200, dict(ROUTE_BODY, version=2))]:
            host, _ = _fetch(monkeypatch, status, body)
            assert host is None, (status, body)
        # Nothing listening at all.
        from teaport_brain import openclaw_client as oc
        gw = _Gateway(200, ROUTE_BODY)
        url = gw.url
        gw.close()
        monkeypatch.setattr(oc, "GATEWAY_URL", url)
        assert asyncio.run(host_context.fetch("room")) is None
        # No token: nothing is sent.
        assert _fetch(monkeypatch, 200, ROUTE_BODY, token=None) == (None, [])
    finally:
        logger.remove(sink)
    assert len(warnings) == 5, warnings
    assert all("persona file" in w for w in warnings), warnings
    assert any("503 (host_unsupported)" in w for w in warnings), warnings


def test_a_gateway_still_starting_is_asked_again_within_the_patience(monkeypatch):
    """The gateway answers 503 with Retry-After until its startup plugins are ready, and
    teaport-brain is not ordered after it: a session opening then asks again rather than
    keep the persona file for its whole conversation."""
    from loguru import logger

    starting = (503, {"error": {"message": "starting"}}, {"Retry-After": "0.1"})
    seen, sink = _warnings()
    try:
        host, requests = _fetch(monkeypatch, 200, ROUTE_BODY, before=[starting, starting],
                                patience=1.0)
        assert host is not None and len(requests) == 3, requests
        # Without patience (or past it), one ask and the persona file.
        host, requests = _fetch(monkeypatch, 200, ROUTE_BODY, before=[starting])
        assert host is None and len(requests) == 1
        # The plugin's own 503 names a reason that stays until the host changes, and
        # carries no Retry-After: not asked again.
        unsupported = (503, {"error": "host_unsupported"}, {})
        host, requests = _fetch(monkeypatch, 200, ROUTE_BODY, before=[unsupported],
                                patience=1.0)
        assert host is None and len(requests) == 1
    finally:
        logger.remove(sink)
    assert any("asking again in 0.1s" in w for w in seen), seen


def test_a_gateway_not_listening_yet_is_asked_again(monkeypatch):
    from loguru import logger

    from teaport_brain import openclaw_client as oc

    gw = _Gateway(200, ROUTE_BODY)
    url = gw.url
    gw.close()
    monkeypatch.setattr(oc, "GATEWAY_URL", url)
    monkeypatch.setattr(oc, "_token", lambda: "tok-1")
    monkeypatch.setattr(host_context, "HAS_AGENT", True)
    monkeypatch.setattr(host_context, "RETRY_REFUSED_S", 0.05)
    seen, sink = _warnings()
    try:
        assert asyncio.run(host_context.fetch("room", patience=0.3)) is None
    finally:
        logger.remove(sink)
    again = [w for w in seen if "refused" in w and "asking again" in w]
    final = [w for w in seen if "refused" in w and "persona file" in w]
    assert len(again) >= 3 and len(final) == 1, seen


def test_a_200_that_is_not_json_says_so(monkeypatch):
    from loguru import logger

    seen, sink = _warnings()
    try:
        host, _ = _fetch(monkeypatch, 200, b"<html>a login page</html>")
    finally:
        logger.remove(sink)
    assert host is None
    assert any("answered 200 but not with JSON" in w for w in seen), seen
    assert not any("JSONDecodeError" in w for w in seen), seen


def test_the_agent_the_operator_named_is_asked_for(monkeypatch):
    """OPENCLAW_AGENT_ID, when set, is the agent the CLI lane consults: the room and the
    phone ask for its context too. Unset, the route's default agent (nothing sent)."""
    monkeypatch.setattr(host_context, "AGENT_ID", "kitchen")
    _, requests = _fetch(monkeypatch, 200, ROUTE_BODY, front_end="room")
    assert requests[0][0] == "/teaport/voice-context?front_end=room&agent=kitchen", requests
    monkeypatch.setattr(host_context, "AGENT_ID", None)
    _, requests = _fetch(monkeypatch, 200, ROUTE_BODY, front_end="room")
    assert requests[0][0] == "/teaport/voice-context?front_end=room", requests
    from teaport_brain.settings import setting
    assert setting("OPENCLAW_AGENT_ID", default=None, env={}) is None


def test_a_room_built_without_the_context_takes_it_at_the_wake(monkeypatch):
    """A room session that opened while the gateway was down was built from the persona
    file; its wake asks again and puts OpenClaw's text in place before anything is
    answered -- the same system message, so nothing else in the context moves."""
    from teaport_brain import agent_backend, agent_session, wake_gate
    from teaport_brain.agent_session import build_agent_session

    monkeypatch.setattr(agent_session, "load_persona", lambda: "BRAIN PERSONA.")
    monkeypatch.setattr(agent_backend, "HAS_AGENT", True)
    asks = []

    async def fetch(front_end, *, patience=0.0, timeout=None):
        asks.append((front_end, timeout))
        return ROOM_HOST

    monkeypatch.setattr(host_context, "fetch", fetch)

    async def run():
        gate = wake_gate.WakeGate(wake_gate.parse_phrases("hey teaport"),
                                  store=wake_gate.MicConversation())
        session = build_agent_session(_Transport(), context_notes=True, front_end="room",
                                      wake_gate=gate, client_features=frozenset({"local"}),
                                      host=None)
        system = session.context.get_messages()[0]
        assert system["content"].startswith("BRAIN PERSONA."), system["content"][:80]
        pushed = []

        async def push(frame):
            pushed.append(frame)

        assert await gate.final("the radio is on", push) is None
        assert asks == [], "asked while asleep"
        await gate.final("hey teaport", push)
        assert asks == [("room", host_context.WAKE_TIMEOUT_S)], asks
        assert session.context.get_messages()[0] is system
        assert system["content"].startswith(ROOM_HOST.instructions), system["content"][:80]
        # Once: a second wake phrase in the conversation asks nothing.
        await gate.final("hey teaport, again", push)
        assert len(asks) == 1
        # Built WITH the context: nothing to ask at the wake.
        gate = wake_gate.WakeGate(wake_gate.parse_phrases("hey teaport"),
                                  store=wake_gate.MicConversation())
        build_agent_session(_Transport(), context_notes=True, front_end="room",
                            wake_gate=gate, client_features=frozenset({"local"}),
                            host=ROOM_HOST)
        await gate.final("hey teaport", push)
        assert len(asks) == 1

    asyncio.run(run())


def test_without_a_gateway_nothing_is_asked(monkeypatch):
    monkeypatch.setattr(host_context, "HAS_AGENT", False)
    assert asyncio.run(host_context.fetch("room")) is None


# ------------------------------------------------------------------ one name

def test_the_old_consult_name_is_gone():
    """One consult tool, OpenClaw's, on every front end: the brain's own name for it is
    not kept anywhere, not even as an alias. The historical run logs (brain/test/RUNLOG-*)
    record what ran then and keep it, and the two settings named after it keep rows
    under their old names (settings.py: a renamed setting is still read, with a warning)."""
    old = "ask_" + "openclaw"
    renamed = ("teaport_tool_" + old, "teaport_" + old + "_timeout")
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
                    text = fh.read().lower()
                for name in renamed:
                    text = text.replace(name, "")
            except (OSError, UnicodeDecodeError):
                continue
            if old in text:
                hits.append(os.path.relpath(path, REPO))
    assert not hits, hits


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
