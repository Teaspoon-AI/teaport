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
#          gateway_server reads it before it builds the session). OpenClaw's whole Talk
#          text: its realtime voice rules (when to consult, the control tool), the
#          operator's talk.realtime.instructions, the agent paragraph and a 12,000-char
#          profile.
#   room   fetch(): the plugin's GET /teaport/voice-context on the gateway
#   phone  (plugin/voice-context.js), built with the same SDK OpenClaw's own phone
#          surface (voice-call) uses: the agent paragraph and a 6,000-char profile, and
#          the consult tool. No voice rules: those the brain's overlay gives
#          (persona.voice_overlay).
# Either way the result goes to build_agent_session(host=...). None -- no gateway
# (TEAPORT_AGENT=none), or one that cannot answer (restarting, a plugin before #230, an
# OpenClaw before 2026.9.7) -- builds from the persona file (persona.load_persona) and the
# brain's copy of the consult schema (tools.OPENCLAW_AGENT_CONSULT).
#
import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from loguru import logger

from teaport_brain.agent_backend import HAS_AGENT

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


# The plugin's route (plugin/voice-context.js, VOICE_CONTEXT_PATH) and the response
# version this brain reads.
VOICE_CONTEXT_PATH = "/teaport/voice-context"
VOICE_CONTEXT_VERSION = 1
# How long a session start waits for it. Loopback, and the plugin reads three small
# files, so it answers in milliseconds; this only bounds a wedged gateway. A gateway that
# is down refuses at once, and one starting or restarting answers 503 at once.
FETCH_TIMEOUT_S = 2.0


def _get_sync(url: str, token: str, timeout: float) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:  # noqa: BLE001 — a body that is not JSON (a 404 page)
            return e.code, None


async def fetch(front_end: str) -> HostContext | None:
    """The voice context the gateway's teaport plugin builds for a room-mic ("room") or
    phone ("sip") session, or None -- and then a warning says why and what the session
    uses instead. Never raises: a session must start whatever the gateway does. No
    gateway configured (TEAPORT_AGENT=none) asks nothing and logs nothing new (the
    startup line says it)."""
    if not HAS_AGENT:
        return None
    from teaport_brain import openclaw_client as oc

    token = oc._token()
    fallback = "the persona file and the brain's consult schema"
    if not token:
        logger.warning(f"voice context ({front_end}): no gateway token — using {fallback}")
        return None
    url = (f"{oc.GATEWAY_URL}{VOICE_CONTEXT_PATH}?"
           + urllib.parse.urlencode({"front_end": front_end}))
    loop = asyncio.get_running_loop()
    try:
        status, body = await asyncio.wait_for(
            loop.run_in_executor(None, _get_sync, url, token, FETCH_TIMEOUT_S),
            FETCH_TIMEOUT_S + 0.5)
    except Exception as e:  # noqa: BLE001 — refused, timed out, not JSON
        logger.warning(f"voice context ({front_end}): no answer from the gateway ({e!r}) — "
                       f"using {fallback}")
        return None
    if status != 200 or not isinstance(body, dict):
        # 503 while the gateway starts or restarts, or with its own reason (an OpenClaw
        # before 2026.9.7: host_unsupported; no default agent: agent_unresolved); 404 from
        # a plugin that predates the route.
        why = (body or {}).get("error") if isinstance(body, dict) else None
        logger.warning(f"voice context ({front_end}): gateway answered {status}"
                       + (f" ({why})" if why else "") + f" — using {fallback}")
        return None
    if body.get("version") != VOICE_CONTEXT_VERSION:
        logger.warning(f"voice context ({front_end}): unknown version "
                       f"{body.get('version')!r} — using {fallback}")
        return None
    host = from_frame({"type": FRAME_TYPE, "instructions": body.get("instructions"),
                       "tools": body.get("tools")})
    logger.info(f"voice context ({front_end}): {len(host.instructions)} chars for agent "
                f"{body.get('agentId')!r} (OpenClaw {body.get('openclawVersion') or '?'})")
    return host
