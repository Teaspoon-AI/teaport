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
#   other  surface (voice-call) uses: the agent paragraph and a 6,000-char profile, and
#          the consult tool. No voice rules: those the brain's overlay gives
#          (persona.voice_overlay). "other" is any /talk client that sends no
#          session_config frame: the Discord bridge, a plugin before #230.
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
from teaport_brain.settings import setting

# The frame's type, and the ?session_config= value that promises it.
FRAME_TYPE = "session_config"
PROTOCOL = "1"


@dataclass(frozen=True)
class HostContext:
    """What the host composed for one voice session. `instructions` may be empty (a host
    that composed nothing; the brain then uses its own persona); `tools` are the host's
    tool definitions as it sent them (RealtimeVoiceTool: type, name, description,
    parameters), only some of which the model is offered (tools.host_tool_schemas).
    `voice_rules`: the instructions carry OpenClaw's own realtime voice rules (when to
    consult, the control tool, the acknowledgement), as a Talk session's do; the room's
    and the phone's are the agent paragraph and profile only. The overlay that follows
    them differs (persona.HOST_RULES / HOST_CONTEXT)."""
    instructions: str = ""
    tools: tuple = ()
    voice_rules: bool = False


def _host(instructions, tools, *, voice_rules: bool, source: str) -> HostContext:
    """A HostContext from what the plugin sent. A field of the wrong type counts as
    absent: the plugin sends OpenClaw's values as they came."""
    if not isinstance(instructions, str):
        logger.warning(f"{source}: instructions is not a string — ignored")
        instructions = ""
    if not isinstance(tools, list):
        logger.warning(f"{source}: tools is not a list — ignored")
        tools = []
    return HostContext(instructions=instructions, voice_rules=voice_rules,
                       tools=tuple(t for t in tools if isinstance(t, dict)))


def from_frame(msg) -> HostContext | None:
    """The HostContext a Talk session's session_config frame carries (req.instructions ??
    "" and req.tools ?? []: OpenClaw's Talk instructions, its voice rules included), or
    None for anything else."""
    if not isinstance(msg, dict) or msg.get("type") != FRAME_TYPE:
        return None
    return _host(msg.get("instructions"), msg.get("tools"), voice_rules=True,
                 source="session_config")


# The plugin's route (plugin/voice-context.js, VOICE_CONTEXT_PATH) and the response
# version this brain reads.
VOICE_CONTEXT_PATH = "/teaport/voice-context"
VOICE_CONTEXT_VERSION = 1
# How long one ask may take. Loopback, and the plugin reads three small files, so it
# answers in milliseconds; this only bounds a wedged gateway. A gateway that is down
# refuses at once, and one starting or restarting answers 503 at once.
FETCH_TIMEOUT_S = 2.0
# How long a session start keeps asking a gateway that is not up yet. teaport-brain is
# not ordered after the gateway, and the gateway answers 503 with Retry-After: 1 until its
# startup plugins are ready, and 503 while it restarts (OpenClaw's
# docs/plugins/architecture-internals/gateway-routes.md); a session opened then would
# keep the persona file for its whole conversation. So a 503 that is the gateway's (a
# Retry-After, or no reason of the plugin's: its own 503s name one and stay until the
# host changes) and a refused connection are asked again, after the Retry-After
# (RETRY_AFTER_MAX_S at most) or RETRY_REFUSED_S, for as long as the caller can wait:
#   PATIENCE_ASLEEP_S  a wake session that opens asleep: nobody is waiting on it, and its
#                      bridge waits 15 s for the hello (local_audio.HELLO_SECS).
#   PATIENCE_S         anyone else: someone is there -- a call being answered, a room or
#                      Discord user who just spoke -- and hears nothing until the build.
# A session that still got nothing asks once more when it wakes (agent_session's
# refresh_host, with WAKE_TIMEOUT_S): the room's gateway is usually up by then.
PATIENCE_ASLEEP_S = 5.0
PATIENCE_S = 1.0
RETRY_REFUSED_S = 0.5
RETRY_AFTER_MAX_S = 2.0
WAKE_TIMEOUT_S = 1.0
# The agent whose context is asked for: OPENCLAW_AGENT_ID, the one the CLI lane consults,
# when it is set; unset, the agent the /v1/chat/completions lane's model "openclaw"
# reaches (the route resolves it with resolveSessionAgentIdStrict and no owner, which is
# that agent whenever the lane can answer; plugin/voice-context.js, resolveAgentId). Its
# own default ("main") is not sent: on a gateway whose default agent is another, that
# would ask for an agent the operator never named.
AGENT_ID = setting("OPENCLAW_AGENT_ID", default=None)

# A 200 whose body is not JSON (_get_sync).
_NOT_JSON = object()


def _retry_after(headers) -> float | None:
    """The Retry-After seconds a response carries, None without one (an HTTP date reads
    as 1 s: the gateway sends seconds)."""
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    try:
        return min(max(float(value), 0.1), RETRY_AFTER_MAX_S)
    except ValueError:
        return 1.0


def _get_sync(url: str, token: str, timeout: float):
    """(status, body, Retry-After): body the parsed JSON, None for an error page that is
    not JSON, _NOT_JSON for a 200 that is not."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            try:
                return r.status, json.load(r), None
            except ValueError:
                return r.status, _NOT_JSON, None
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:  # noqa: BLE001 — a body that is not JSON (a 404 page)
            body = None
        return e.code, body, _retry_after(e.headers)


def _refused(e: BaseException) -> bool:
    return isinstance(e, ConnectionRefusedError) or (
        isinstance(e, urllib.error.URLError) and isinstance(e.reason, ConnectionRefusedError))


async def fetch(front_end: str, *, patience: float = 0.0,
                timeout: float = FETCH_TIMEOUT_S) -> HostContext | None:
    """The voice context the gateway's teaport plugin builds for a session that gets none
    with the session itself -- the room mic ("room"), the phone ("sip"), a /talk client
    with no session_config frame (its ?client= name) -- or None, and then a warning says
    why and what the session uses instead. A gateway not up yet is asked again for up to
    `patience` seconds (PATIENCE_S above). Never raises: a session must start whatever
    the gateway does. No gateway configured (TEAPORT_AGENT=none) asks nothing and logs
    nothing new (the startup line says it)."""
    if not HAS_AGENT:
        return None
    from teaport_brain import openclaw_client as oc

    token = oc._token()
    fallback = "the persona file and the brain's consult schema"
    if not token:
        logger.warning(f"voice context ({front_end}): no gateway token — using {fallback}")
        return None
    query = {"front_end": front_end}
    if AGENT_ID:
        query["agent"] = AGENT_ID
    url = f"{oc.GATEWAY_URL}{VOICE_CONTEXT_PATH}?" + urllib.parse.urlencode(query)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + patience
    while True:
        again = None  # seconds to wait before asking again, when it is worth it
        try:
            status, body, retry_after = await asyncio.wait_for(
                loop.run_in_executor(None, _get_sync, url, token, timeout), timeout + 0.5)
        except Exception as e:  # noqa: BLE001 — refused, timed out, unreachable
            if _refused(e):
                why, again = "the gateway refused the connection (not up)", RETRY_REFUSED_S
            else:
                why = f"no answer from the gateway ({e!r})"
        else:
            if status == 200 and isinstance(body, dict):
                break
            if status == 200:
                why = ("the gateway answered 200 but not with JSON (something other than "
                       "the teaport plugin answers that path)" if body is _NOT_JSON
                       else "the gateway answered 200 with JSON that is not an object")
            else:
                # 503 while the gateway starts or restarts, or with the plugin's own
                # reason (an OpenClaw before 2026.9.7: host_unsupported; no default
                # agent: agent_unresolved); 400 agent_unknown (OPENCLAW_AGENT_ID names
                # no agent); 404 from a plugin that predates the route.
                reason = body.get("error") if isinstance(body, dict) else None
                why = f"gateway answered {status}" + (f" ({reason})" if reason else "")
                if status == 503 and (retry_after is not None or not reason):
                    again = retry_after or 1.0
        if again is not None and loop.time() + again <= deadline:
            logger.info(f"voice context ({front_end}): {why} — asking again in {again:g}s")
            await asyncio.sleep(again)
            continue
        logger.warning(f"voice context ({front_end}): {why} — using {fallback}")
        return None
    if body.get("version") != VOICE_CONTEXT_VERSION:
        logger.warning(f"voice context ({front_end}): unknown version "
                       f"{body.get('version')!r} — using {fallback}")
        return None
    host = _host(body.get("instructions"), body.get("tools"), voice_rules=False,
                 source=f"voice context ({front_end})")
    logger.info(f"voice context ({front_end}): {len(host.instructions)} chars for agent "
                f"{body.get('agentId')!r} (OpenClaw {body.get('openclawVersion') or '?'})")
    return host
