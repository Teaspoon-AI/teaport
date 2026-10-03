#
# teaport — pending-consult registry (the brain half of in-process consults).
#
# ask_openclaw (tools.py) emits an openclaw_agent_consult tool_call over /talk
# with a call_id and parks a future here; the teaport plugin's submitToolResult
# forwards the relay's result back as a {"type":"tool_result"} message, which
# the serializer resolves through this module. Single event loop, no locking.
#
# Results follow the relay's speakable-result convention: a string, or a dict
# carrying one of text / result / output / error. Working notices arrive with
# will_continue=True and must NOT resolve the future.
#
import asyncio

from loguru import logger

_pending: dict = {}

# Every consult call_id starts with this (tools._ask_openclaw mints them). The relay
# also echoes tool_results for the brain's OWN tool calls -- the display-only
# tool_call events tools._wrap sends with pipecat's "fc_..." ids -- dozens per Talk
# consult. Those are not consults and never were, so they are dropped without a log
# line; only a consult id that matches nothing (a result landing after its waiter
# gave up) is worth one.
CALL_ID_PREFIX = "teaport-consult-"

# What the Control UI's consult runner submits when the run ended with no text
# (ui/src/pages/chat/talk/shared.ts, OpenClaw 2026.9.7) and when its own 120 s wait
# ran out. The first is an empty result dressed as an answer -- spoken as one, the
# caller heard "OpenClaw finished with no text" as the reply (#80).
_NO_TEXT_RESULTS = frozenset({"OpenClaw finished with no text."})
_TIMEOUT_ERRORS = frozenset({"OpenClaw tool call timed out"})


def create(call_id: str) -> asyncio.Future:
    fut = asyncio.get_running_loop().create_future()
    _pending[call_id] = fut
    return fut


def cancel(call_id: str) -> None:
    fut = _pending.pop(call_id, None)
    if fut is not None and not fut.done():
        fut.cancel()


def resolve(call_id: str, result, will_continue: bool = False) -> bool:
    """Route a tool_result message. Returns True if it matched a pending consult."""
    fut = _pending.get(call_id)
    if fut is None:
        if call_id.startswith(CALL_ID_PREFIX):
            logger.debug(f"consult_bridge: tool_result for unknown call_id {call_id!r}")
        return False
    if will_continue:
        # Interim "working" notice — the relay's agent run is underway. Mark the
        # future so the tool's ack-phase timeout knows the native path is alive.
        fut.working = True
        logger.debug(f"consult_bridge: working notice for {call_id}")
        return True
    _pending.pop(call_id, None)
    if not fut.done():
        fut.set_result(result)
    return True


def extract_text(result) -> str:
    """Speakable text from a relay consult result (string or keyed dict)."""
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        for key in ("text", "result", "output", "error"):
            v = result.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def classify(result) -> tuple[str | None, str | None, str]:
    """(text, failure, detail) for a relay consult result: the speakable answer, or
    None with why -- "error", "timeout" (the runner's own wait ran out), or "empty"
    (no answer, including the runner's no-text placeholder). Same vocabulary as
    openclaw_client.ConsultOutcome."""
    if isinstance(result, dict) and result.get("error") \
            and not any(result.get(k) for k in ("text", "result", "output")):
        err = str(result["error"]).strip()
        return None, ("timeout" if err in _TIMEOUT_ERRORS else "error"), err
    text = extract_text(result)
    if not text or text in _NO_TEXT_RESULTS:
        return None, "empty", text or "no text in the result"
    return text, None, ""
