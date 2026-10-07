"""The tool contract (tools.py, THE TOOL CONTRACT): one record per tool, and ONE decision
— active_tools() — behind the schema the model is offered, the handlers registered on
the LLM and the tools the system prompt names. A tool that is switched off, or missing
what it needs, must be absent from all three; client tools reach only a client that
announced them, and round-trip through a client_tool / tool_result exchange."""
import asyncio
import dataclasses
import json
import os
import re

import pytest

from teaport_brain import config_schema, consult_bridge, persona, tools
from teaport_brain.agent_backend import HAS_AGENT

ALL_FEATURES = frozenset({"volume", "restart", "local"})


def _names(ts):
    return [t.name for t in ts]


def _with(monkeypatch, **enabled):
    """TOOLS with some switches flipped, as TEAPORT_TOOL_<NAME>=... would at import."""
    patched = tuple(dataclasses.replace(t, enabled=enabled.get(t.name, t.enabled))
                    for t in tools.TOOLS)
    monkeypatch.setattr(tools, "TOOLS", patched)


class _LLM:
    def __init__(self):
        self.registered = {}
        self.pushed = []

    def register_function(self, name, handler, **kw):
        self.registered[name] = (handler, kw)

    def event_handler(self, name):           # _install_spoke_tracker
        return lambda fn: fn

    async def push_frame(self, frame, *a, **kw):
        self.pushed.append(frame)


def test_every_tool_has_a_switch_row_and_a_hint():
    rows = {r["name"]: r for r in config_schema.load()["settings"]}
    for t in tools.TOOLS:
        flag = f"TEAPORT_TOOL_{t.name.upper()}"
        assert flag in rows, f"{t.name}: no config_schema row for {flag}"
        assert rows[flag]["type"] == "flag" and rows[flag]["group"] == "tools", flag
        assert rows[flag].get("default", True) is t.enabled or os.getenv(flag), (
            f"{flag}: schema default and code default disagree")
        assert t.hint.startswith(t.name), f"{t.name}: the prompt hint must name the tool"
        for need in t.needs:
            assert need in ("agent", "tts") or re.fullmatch(r"client:[a-z]+", need) or (
                need.startswith("host:") and need[5:] in tools.HOST_CHECKS), need


def test_defaults_keep_todays_tools_and_offer_client_tools_only_when_announced(monkeypatch):
    monkeypatch.setitem(tools.HOST_CHECKS, "wifi_setup", lambda: False)
    base = _names(tools.active_tools(tools.ToolContext(has_tts=True)))
    assert "set_volume" not in base and "restart_session" not in base
    with_volume = _names(tools.active_tools(tools.ToolContext(
        has_tts=True, client_features=frozenset({"volume"}))))
    assert with_volume == base + ["set_volume"]
    # restart_session is off by default even for a client that can do it.
    everything = _names(tools.active_tools(tools.ToolContext(
        has_tts=True, client_features=ALL_FEATURES)))
    assert everything == base + ["set_volume"]
    gateway = {"web_search", "web_fetch", "search_memory", "remember", "ask_openclaw"}
    if HAS_AGENT:
        assert gateway <= set(base)
    else:
        assert not gateway & set(base)


def test_a_switch_turns_a_tool_on_or_off_everywhere(monkeypatch):
    _with(monkeypatch, get_current_time=False, restart_session=True)
    ctx = tools.ToolContext(has_tts=True, client_features=ALL_FEATURES)
    active = tools.active_tools(ctx)
    names = _names(active)
    assert "get_current_time" not in names and "restart_session" in names
    # The schema offers exactly these ...
    assert [t.name for t in tools.build_tools_schema(ctx).standard_tools] == names
    # ... the LLM gets handlers for exactly these (same tts + features) ...
    llm = _LLM()
    tools.register_tools(llm, tts=object(), client_features=ALL_FEATURES)
    assert list(llm.registered) == names
    # ... and the prompt names exactly these: the off tool is never mentioned.
    prompt = persona.build_system_prompt("Persona.", tools=active)
    mentioned = set(re.findall(r"\b[a-z]+_[a-z_]+\b", prompt))
    assert "get_current_time" not in mentioned
    assert {"set_volume", "restart_session"} <= mentioned
    assert {n for n in names if "_" in n} <= mentioned


def test_the_tuned_paragraphs_stay_verbatim_for_the_default_sets():
    default = tools.active_tools(tools.ToolContext(has_tts=True))
    assert persona.build_system_prompt("P", tools=default) == persona.build_system_prompt("P")
    # A client tool adds its own sentence and leaves the tuned paragraph alone.
    with_volume = tools.active_tools(tools.ToolContext(
        has_tts=True, client_features=frozenset({"volume"})))
    overlay = persona.tools_paragraph(with_volume)
    tuned = persona._TOOLS_WITH_AGENT if HAS_AGENT else persona._TOOLS_LOCAL
    assert overlay.startswith(tuned) and "set_volume" in overlay[len(tuned):]


def test_a_composed_paragraph_keeps_the_rules_its_tools_need(monkeypatch):
    _with(monkeypatch, get_host_status=False)
    text = persona.tools_paragraph(tools.active_tools(tools.ToolContext(has_tts=True)))
    assert "get_host_status" not in text and "get_current_time" in text
    assert persona._NO_TOOL_NAME_INSTRUCTION in text
    if HAS_AGENT:
        assert persona._PREAMBLE_INSTRUCTION in text and persona._CONSULT_IN_PROGRESS in text
        assert persona._CANNOT_LOOK_UP not in text
    else:
        assert persona._CANNOT_LOOK_UP in text
    _with(monkeypatch, **{t.name: False for t in tools.TOOLS})
    none = persona.tools_paragraph(tools.active_tools(tools.ToolContext(has_tts=True)))
    assert none.startswith("You have no tools") and persona._CANNOT_LOOK_UP in none


def test_client_features_are_parsed_strictly():
    assert tools.parse_client_features("volume, RESTART,local,teleport") == ALL_FEATURES
    assert tools.parse_client_features(None) == frozenset()
    assert tools.CLIENT_FEATURES == ALL_FEATURES


class _Params:
    def __init__(self, llm, args):
        self.llm, self.arguments, self.results = llm, args, []
        self.tool_call_id = "fc_1"

    async def result_callback(self, result, **kw):
        self.results.append(result)


def test_a_client_tool_asks_the_client_and_returns_its_answer():
    async def run():
        llm = _LLM()
        params = _Params(llm, {"level": 30})
        call = asyncio.create_task(tools._client_tool("set_volume")(params))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if llm.pushed:
                break
        msg = llm.pushed[0].message
        assert msg["type"] == "client_tool" and msg["name"] == "set_volume"
        assert msg["args"] == {"level": 30} and msg["call_id"].startswith(tools.CLIENT_CALL_PREFIX)
        # The client answers with a tool_result; the serializer routes it here.
        assert consult_bridge.resolve(msg["call_id"], {"ok": True, "volume": 30})
        await call
        return params.results

    assert asyncio.run(run()) == [{"ok": True, "volume": 30}]


def test_a_client_that_does_not_answer_is_reported_not_waited_on(monkeypatch):
    monkeypatch.setattr(tools, "CLIENT_TOOL_TIMEOUT_S", 0.05)

    async def run():
        params = _Params(_LLM(), {})
        await tools._client_tool("restart_session")(params)
        return params.results, dict(consult_bridge._pending)

    results, pending = asyncio.run(run())
    assert results == [{"ok": False, "error": "the device did not respond"}]
    assert not pending, "the timed-out call_id must not stay registered"


def test_the_serializer_routes_a_client_tool_result():
    from teaport_brain.gateway_serializer import TeaportGatewaySerializer

    async def run():
        fut = consult_bridge.create("teaport-client-abc")
        frame = await TeaportGatewaySerializer().deserialize(json.dumps(
            {"type": "tool_result", "call_id": "teaport-client-abc", "result": {"ok": True}}))
        return frame, await asyncio.wait_for(fut, 1)

    frame, result = asyncio.run(run())
    assert frame is None and result == {"ok": True}


def test_a_client_tool_that_cannot_ask_still_answers_once_and_leaves_nothing_behind():
    class _Broken(_LLM):
        async def push_frame(self, frame, *a, **kw):
            raise RuntimeError("pipeline is gone")

    async def run():
        params = _Params(_Broken(), {"level": 10})
        await tools._client_tool("set_volume")(params)
        return params.results, dict(consult_bridge._pending)

    results, pending = asyncio.run(run())
    assert results == [{"ok": False, "error": "the device could not be reached"}]
    assert not pending


def test_a_cancelled_client_tool_gives_no_result_and_leaves_nothing_behind():
    async def run():
        params = _Params(_LLM(), {})
        call = asyncio.create_task(tools._client_tool("set_volume")(params))
        await asyncio.sleep(0.02)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        return params.results, dict(consult_bridge._pending)

    results, pending = asyncio.run(run())
    assert results == [] and not pending


def test_the_bare_defaults_offer_exactly_what_they_register():
    """build_tools_schema() and register_tools(llm), both given nothing, agree (no voice,
    no client); a session gives both its own tts and features."""
    llm = _LLM()
    tools.register_tools(llm)
    assert _names(tools.build_tools_schema().standard_tools) == list(llm.registered)
    assert "list_voices" not in llm.registered


def test_device_tools_alone_are_not_announced_as_no_tools(monkeypatch):
    _with(monkeypatch, **{t.name: False for t in tools.TOOLS if not t.client})
    only = tools.active_tools(tools.ToolContext(has_tts=True, client_features=ALL_FEATURES))
    assert _names(only) == ["set_volume"]
    text = persona.tools_paragraph(only)
    assert "no tools" not in text and "you can use set_volume" in text
    assert persona._CANNOT_LOOK_UP in text


def test_restart_session_tells_the_model_one_thing(monkeypatch):
    """Schema, prompt and the bridge's result (test_local_audio) agree: call it with no
    preamble, then one goodbye — the bridge waits for that reply to play."""
    desc = tools.RESTART_SESSION.description
    assert "no line before it" in desc and "goodbye" in desc and "first" not in desc
    _with(monkeypatch, restart_session=True)
    active = tools.active_tools(tools.ToolContext(has_tts=True, client_features=ALL_FEATURES))
    paragraph = persona.tools_paragraph(active)
    device = paragraph[paragraph.index("On the device"):]
    assert "restart_session" in device and "goodbye" in device and "no preamble" in device


def test_the_agent_first_directive_names_the_sessions_direct_tools():
    from teaport_brain.agent_session import AGENT_FIRST_DIRECTIVE, agent_first_directive
    tuned = "Only list_voices and switch_voice may be called directly."
    default = tools.active_tools(tools.ToolContext(has_tts=True))
    assert agent_first_directive(default) == AGENT_FIRST_DIRECTIVE + " " + tuned
    with_device = tools.active_tools(tools.ToolContext(has_tts=True,
                                                       client_features=ALL_FEATURES))
    assert agent_first_directive(with_device).endswith(
        " Only list_voices, switch_voice and set_volume may be called directly.")
    assert agent_first_directive([]) == AGENT_FIRST_DIRECTIVE


def test_agent_first_is_ignored_while_ask_openclaw_is_switched_off():
    import subprocess
    import sys
    env = dict(os.environ, TEAPORT_AGENT="openclaw", TEAPORT_AGENT_FIRST="1")
    probe = "from teaport_brain import tools; print(tools.AGENT_FIRST)"
    for switch, expected in (("0", "False"), ("1", "True")):
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                             env=dict(env, TEAPORT_TOOL_ASK_OPENCLAW=switch), timeout=120)
        assert out.stdout.strip().splitlines()[-1] == expected, (switch, out.stderr[-500:])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_wifi_setup_needs_someone_at_the_box_and_the_unit(monkeypatch):
    local = tools.ToolContext(has_tts=True, client_features=frozenset({"local"}))
    remote = tools.ToolContext(has_tts=True, client_features=frozenset({"volume"}))
    monkeypatch.setitem(tools.HOST_CHECKS, "wifi_setup", lambda: True)
    assert "wifi_setup" in _names(tools.active_tools(local))
    assert "wifi_setup" not in _names(tools.active_tools(remote))   # a remote Talk user
    monkeypatch.setitem(tools.HOST_CHECKS, "wifi_setup", lambda: False)
    assert "wifi_setup" not in _names(tools.active_tools(local))    # not installed here
    monkeypatch.setitem(tools.HOST_CHECKS, "wifi_setup", lambda: True)
    _with(monkeypatch, wifi_setup=False)
    assert "wifi_setup" not in _names(tools.active_tools(local))    # switched off


def test_the_wifi_setup_tool_hands_over_and_keeps_the_model_quiet():
    class Voice:
        begun = 0

        async def begin(self):
            Voice.begun += 1

    async def run():
        params = _Params(_LLM(), {})
        got = {}

        async def cb(result, **kw):
            got.update(result=result, **kw)
        params.result_callback = cb
        await tools._wifi_setup(params, voice=Voice())
        return got

    got = asyncio.run(run())
    assert Voice.begun == 1 and got["result"]["ok"]
    assert got["properties"].run_llm is False
