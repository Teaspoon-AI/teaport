#
# teaport — what the OpenClaw host tells a voice session (issue #230)
#
# OpenClaw composes what a realtime voice surface is told and hands it to the provider
# with each session (RealtimeVoiceBridgeCreateRequest, 2026.9.7 and newer):
#
#   instructions  its realtime voice instructions (DEFAULT_REALTIME_INSTRUCTIONS: when to
#                 consult the agent), the operator's talk.realtime.instructions, the agent
#                 paragraph and the IDENTITY / USER / SOUL profile, and any earlier
#                 conversation when the session replaces one;
#   tools         openclaw_agent_consult and openclaw_agent_control.
#
# The brain follows that contract instead of composing its own: the session's system
# prompt starts with the instructions as the host sent them (persona.build_system_prompt,
# then the voice overlay), and the model's consult tool takes the host's schema
# (tools.host_tool_schemas).
#
# Where it comes from, per front end:
#   Talk   the plugin's session_config frame, the first frame on /talk when the URL
#          carries ?session_config=1 (plugin/provider.js, sessionConfigFrame;
#          gateway_server reads it before it builds the session).
#   room   not yet: the persona path (persona.load_persona) until the plugin serves the
#   phone  same SDK-built text to the brain (issue #230, Phase 3). A HostContext from that
#          fetch goes to build_agent_session(host=...) exactly as Talk's does.
#
from dataclasses import dataclass

from loguru import logger

# The frame's type, and the ?session_config= value that promises it.
FRAME_TYPE = "session_config"
PROTOCOL = "1"


@dataclass(frozen=True)
class HostContext:
    """What the host composed for one voice session. `instructions` may be empty (a host
    that composed nothing; the brain then uses its own persona); `tools` are the host's
    tool definitions as it sent them (RealtimeVoiceTool: type, name, description,
    parameters), only some of which the model is offered (tools.host_tool_schemas)."""
    instructions: str = ""
    tools: tuple = ()


def from_frame(msg) -> HostContext | None:
    """The HostContext a session_config frame carries, or None for anything else. A field
    of the wrong type counts as absent: the frame is the plugin's, which sends
    req.instructions ?? "" and req.tools ?? [] as they came."""
    if not isinstance(msg, dict) or msg.get("type") != FRAME_TYPE:
        return None
    instructions = msg.get("instructions")
    tools = msg.get("tools")
    if not isinstance(instructions, str):
        logger.warning("session_config: instructions is not a string — ignored")
        instructions = ""
    if not isinstance(tools, list):
        logger.warning("session_config: tools is not a list — ignored")
        tools = []
    return HostContext(instructions=instructions,
                       tools=tuple(t for t in tools if isinstance(t, dict)))
