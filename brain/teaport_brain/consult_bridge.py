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
