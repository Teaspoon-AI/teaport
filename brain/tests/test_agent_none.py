#
# Unit test: TEAPORT_AGENT=none is a box with no gateway, and the brain acts like it.
#
# The installer has always had a voice-only path (no OpenClaw, no front door), but the
# brain never learned which kind of box it was on. Without a gateway it still advertised
# web_search / web_fetch / search_memory / remember / ask_openclaw, told the model in the
# persona to use them, and ran MemoryRecall on every turn — up to TEAPORT_RECALL_TIMEOUT
# spent on a search that returns None. Nothing crashed (the gateway client is
# best-effort), so nothing said so; the caller just heard "I'll look into that" go
# nowhere. Issue #38.
#
# The mode is a module-level constant read at import (like TEAPORT_AGENT_FIRST), so this
# script sets the variable BEFORE importing anything and covers the whole of `none` in
# one process: the tools schema, the registered handlers, the persona overlay, the
# persona source, and a built session's pipeline. The rest of the suite runs in the
# default (`openclaw`) mode and pins that nothing there moved.
#
# Run: python test_agent_none.py   (or via the suite)
#
import asyncio
import os
import re
import sys
import tempfile

os.environ["TEAPORT_AGENT"] = "none"
# Agent-first is meaningless without a gateway; set it to prove it is ignored.
os.environ["TEAPORT_AGENT_FIRST"] = "1"
# Before teaport_brain.services is imported anywhere (see test_service_unusable).
os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.processors.frame_processor import FrameProcessor  # noqa: E402

from teaport_brain import agent_backend, persona, tools  # noqa: E402
from teaport_brain.agent_session import build_agent_session  # noqa: E402
from teaport_brain.memory_recall import MemoryRecall  # noqa: E402

GATEWAY_TOOLS = {"web_search", "web_fetch", "search_memory", "remember", "ask_openclaw"}
LOCAL_TOOLS = {"get_host_status", "get_current_time", "list_voices", "switch_voice"}


def test_the_mode_is_read_once_and_agent_first_is_ignored_without_a_gateway():
    assert agent_backend.AGENT == "none"
    assert agent_backend.HAS_AGENT is False
    assert tools.AGENT_FIRST is False, (
        "TEAPORT_AGENT_FIRST=1 with no gateway would send every turn to ask_openclaw, "
        "which is not registered")
    assert "none" in agent_backend.startup_line()
    assert "ask_openclaw" in agent_backend.startup_line(), (
        "the journal line must name what is off")


def test_only_the_local_tools_are_advertised():
    names = {t.name for t in tools.build_tools_schema().standard_tools}
    assert names == LOCAL_TOOLS, f"advertised {sorted(names)}"


def test_the_overlay_does_not_name_the_gateway_tools():
    prompt = persona.build_system_prompt("You are a test persona.")
    # As identifiers: "remember" is also an English word the delivery text uses.
    mentioned = set(re.findall(r"\b[a-z]+_[a-z_]+\b", prompt))
    assert not mentioned & GATEWAY_TOOLS, (
        f"the persona still tells the model to use {sorted(mentioned & GATEWAY_TOOLS)}")
    # ...but still names the local ones, and says what it cannot do instead of
    # leaving the model to promise a lookup it cannot make.
    assert mentioned >= LOCAL_TOOLS, f"local tools missing: {sorted(LOCAL_TOOLS - mentioned)}"
    assert "cannot search the web" in prompt
    assert "say so plainly" in prompt
    # The delivery paragraphs are the tuned text and are shared verbatim.
    assert "Never read out a web address or URL" in prompt
    assert "talked over and lost" in prompt
    assert prompt.endswith(persona.VOICE_OVERLAY_LOCAL)


def test_the_gateway_overlay_is_untouched():
    # test_persona_rules pins the wording; this pins that the gateway overlay is the
    # gateway tools paragraph plus the same delivery text the local one uses.
    assert persona.VOICE_OVERLAY.startswith("You have tools")
    assert "ask_openclaw" in persona.VOICE_OVERLAY
    shared = persona.VOICE_OVERLAY[persona.VOICE_OVERLAY.index("Your words are spoken aloud"):]
    assert persona.VOICE_OVERLAY_LOCAL.endswith(shared)


def test_the_persona_comes_from_the_persona_file_not_the_workspace():
    with tempfile.TemporaryDirectory() as d:
        ws = os.path.join(d, "workspace")
        os.makedirs(ws)
        with open(os.path.join(ws, "SOUL.md"), "w") as f:
            f.write("# WORKSPACE SOUL\n")
        pf = os.path.join(d, "persona.md")
        with open(pf, "w") as f:
            f.write("PERSONA FILE\n")
        persona.WORKSPACE_DIR, persona.PERSONA_FILE = ws, pf
        assert persona.load_persona() == "PERSONA FILE", (
            "without a gateway the workspace files are not the persona source")
        os.remove(pf)
        assert persona.load_persona() == persona.FALLBACK_PERSONA


class _Stub(FrameProcessor):
    """A processor standing in for a transport side. Never linked into anything."""


class _Transport:
    def __init__(self):
        self._input = _Stub(name="stub-in")
        self._output = _Stub(name="stub-out")

    def input(self):
        return self._input

    def output(self):
        return self._output


async def test_a_built_session_has_no_memory_recall():
    session = build_agent_session(_Transport())
    kinds = [type(p) for p in session.task.pipeline.processors]
    assert MemoryRecall not in kinds, (
        "MemoryRecall is in the pipeline: every turn would spend up to "
        "TEAPORT_RECALL_TIMEOUT on a memory_search with no gateway to answer it")
    # The session's context advertises the same schema the tools module built, and the
    # LLM has handlers for exactly those: a gateway tool a model hallucinates by name
    # would otherwise fail slowly against a gateway that is not there.
    names = {t.name for t in session.context.tools.standard_tools}
    assert names == LOCAL_TOOLS
    registered = {n for n in session.llm._functions if n is not None}
    assert registered == LOCAL_TOOLS, f"registered {sorted(registered)}"


def main():
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
        print(f"  ok {name}")


if __name__ == "__main__":
    main()
