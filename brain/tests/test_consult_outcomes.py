#
# Unit test: a consult that produced no answer says why, and each lane gets the budget
# it needs (#80).
#
# Journals 2026-09-30..10-03: of 10 Talk consults 3 answered; of 17 SIP consults 4. The
# rest were, roughly:
#
#   * SIP: the warm lane's 45 s cap (11 of 13), then "no budget left after warm lane"
#     -- on the same kinds of request (local search, news) that took 45-100 s on Talk.
#   * SIP: HTTP 402 Payment Required twice, then a cold `--local` CLI that could not
#     even start, "A Gateway is running for this state directory".
#   * Talk: "OpenClaw finished with no text." -- the Control UI's placeholder for an
#     empty run -- delivered to the caller as the answer.
#   * Talk: the brain's 180 s ceiling, a minute past the Control UI's hard 120 s.
#
# Run: python test_consult_outcomes.py   (or via the suite)
#
import asyncio
import io
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger  # noqa: E402

from teaport_brain import consult_bridge, tools  # noqa: E402
from teaport_brain import openclaw_client as oc  # noqa: E402


def _http(code, reason):
    return urllib.error.HTTPError("http://gw/v1/chat/completions", code, reason, {}, None)


class _Warm:
    """Stands in for _completions_sync: each call pops the next scripted reply (a
    string, None for an empty turn, or an exception to raise)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, message, token, timeout):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


class _Patched:
    """Swap the warm lane, the CLI runner and the token for one test."""

    def __init__(self, warm, cli_rc=0, cli_out=b'{\n"payloads": [{"text": "cli answer"}]\n}'):
        self.warm = warm
        self.cli_args = None
        self.cli_rc, self.cli_out = cli_rc, cli_out

    async def _run(self, args, *, timeout, capture):
        self.cli_args = args
        return self.cli_rc, self.cli_out, b""

    def __enter__(self):
        self._saved = (oc._completions_sync, oc._run_openclaw, oc._token, asyncio.sleep)
        oc._completions_sync = self.warm
        oc._run_openclaw = self._run
        oc._token = lambda: "tok"
        real_sleep = asyncio.sleep
        asyncio.sleep = lambda s, *a, **k: real_sleep(0)  # the 3 s retry pause
        return self

    def __exit__(self, *exc):
        oc._completions_sync, oc._run_openclaw, oc._token, asyncio.sleep = self._saved


# --- the gateway / CLI lane ---------------------------------------------------------

async def test_an_answer_comes_back_as_text():
    with _Patched(_Warm("forecast: sunny")) as p:
        out = await oc.agent_consult("weather?", timeout=60)
    assert out == oc.ConsultOutcome("forecast: sunny"), out
    assert p.cli_args is None


async def test_a_402_is_not_retried_and_does_not_reach_the_cli():
    """Payment Required refuses the second attempt exactly as the first, and the CLI
    runs on the same gateway and provider."""
    warm = _Warm(_http(402, "Payment Required"), "never reached")
    with _Patched(warm) as p:
        out = await oc.agent_consult("news?", timeout=60)
    assert warm.calls == 1, f"retried a 402 ({warm.calls} calls)"
    assert p.cli_args is None, f"ran the CLI after a 402: {p.cli_args}"
    assert out.failure == "error" and "402" in out.detail, out


async def test_a_5xx_is_retried_once():
    warm = _Warm(_http(503, "Service Unavailable"), "second try")
    with _Patched(warm):
        out = await oc.agent_consult("news?", timeout=60)
    assert warm.calls == 2 and out.text == "second try", (warm.calls, out)


async def test_a_timeout_is_reported_as_one_and_not_handed_to_the_cli():
    warm = _Warm(TimeoutError("timed out"))
    with _Patched(warm) as p:
        out = await oc.agent_consult("restaurants near me?", timeout=60)
    assert out.failure == "timeout", out
    assert warm.calls == 1, "a timeout means the budget is gone; no second attempt"
    assert p.cli_args is None, "the CLI would rerun the same doomed turn"


async def test_an_empty_turn_is_empty_not_an_answer():
    warm = _Warm(None)
    with _Patched(warm) as p:
        out = await oc.agent_consult("anything", timeout=60)
    assert out.text is None and out.failure == "empty", out
    assert warm.calls == 1 and p.cli_args is None


async def test_a_disabled_endpoint_goes_through_the_gateway_not_local():
    """The gateway answered (404: chat completions off), so it owns the state
    directory and `--local` would exit "A Gateway is running for this state
    directory". The plain CLI dispatches through it instead."""
    with _Patched(_Warm(_http(404, "Not Found"), _http(404, "Not Found"))) as p:
        out = await oc.agent_consult("post hello", timeout=60)
    assert p.cli_args is not None, "the CLI is the only lane left when the endpoint is off"
    assert "--local" not in p.cli_args, p.cli_args
    assert out.text == "cli answer", out


async def test_an_unreachable_gateway_runs_the_cli_embedded():
    down = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    with _Patched(_Warm(down, down)) as p:
        out = await oc.agent_consult("post hello", timeout=60)
    assert p.cli_args is not None and "--local" in p.cli_args, p.cli_args
    assert out.text == "cli answer", out


async def test_a_cli_that_exits_nonzero_with_no_payload_is_an_error():
    down = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    with _Patched(_Warm(down, down), cli_rc=1, cli_out=b""):
        out = await oc.agent_consult("post hello", timeout=60)
    assert out.failure == "error" and "rc=1" in out.detail, out


# --- the native (relay) lane ---------------------------------------------------------

async def test_the_no_text_placeholder_is_an_empty_result():
    for result in ("OpenClaw finished with no text.",
                   {"text": "OpenClaw finished with no text."}, {}, ""):
        text, failure, _ = consult_bridge.classify(result)
        assert text is None and failure == "empty", (result, text, failure)


async def test_the_runner_timeout_is_a_timeout():
    assert consult_bridge.classify({"error": "OpenClaw tool call timed out"})[1] == "timeout"
    assert consult_bridge.classify(
        {"error": "Persisted user turn changed before replay admission"})[1] == "error"
    assert consult_bridge.classify({"text": "Sunny."}) == ("Sunny.", None, "")


async def test_echoes_of_the_brains_own_tool_calls_are_not_logged():
    buf = io.StringIO()
    sink = logger.add(buf, level="DEBUG", format="{message}")
    try:
        for i in range(30):
            assert consult_bridge.resolve(f"fc_{i:04x}", {"ok": True}) is False
        consult_bridge.resolve(f"{consult_bridge.CALL_ID_PREFIX}gone", {"text": "late"})
    finally:
        logger.remove(sink)
    lines = [ln for ln in buf.getvalue().splitlines() if "unknown call_id" in ln]
    assert len(lines) == 1 and "gone" in lines[0], lines


# --- the async waiter ---------------------------------------------------------------

async def _wait(fut, *, ack=0.05, ceiling=0.3, cli=None):
    delivered = []

    async def followup(request, text, tool_call_id=None, failure=None, detail=""):
        delivered.append((text, failure))

    saved = (tools._NATIVE_CONSULT_ACK_TIMEOUT, tools._ASYNC_CONSULT_TIMEOUT,
             oc.agent_consult)
    tools._NATIVE_CONSULT_ACK_TIMEOUT, tools._ASYNC_CONSULT_TIMEOUT = ack, ceiling
    if cli is not None:
        oc.agent_consult = cli
    try:
        await tools._consult_and_followup("call-1", fut, "news?", followup, "tc-1")
    finally:
        (tools._NATIVE_CONSULT_ACK_TIMEOUT, tools._ASYNC_CONSULT_TIMEOUT,
         oc.agent_consult) = saved
    return delivered


async def test_the_cli_fallback_gets_the_whole_async_budget():
    """SIP: no relay, so no ack, so the CLI lane. It used to run on CONSULT_TIMEOUT's
    45 s while Talk waited 180 s for the same requests."""
    seen = {}

    async def cli(request, *, timeout=None, session_key=None):
        seen["timeout"] = timeout
        return oc.ConsultOutcome("cli answer")

    fut = asyncio.get_running_loop().create_future()
    delivered = await _wait(fut, ack=0.05, ceiling=100.0, cli=cli)
    assert delivered == [("cli answer", None)], delivered
    assert seen["timeout"] is not None and 99 < seen["timeout"] <= 100, seen


async def test_an_acked_consult_that_never_returns_is_a_timeout():
    fut = asyncio.get_running_loop().create_future()
    fut.working = True
    delivered = await _wait(fut, ack=0.05, ceiling=0.2)
    assert delivered == [(None, "timeout")], delivered


async def test_the_no_text_placeholder_is_not_delivered_as_an_answer():
    fut = asyncio.get_running_loop().create_future()
    fut.set_result("OpenClaw finished with no text.")
    delivered = await _wait(fut)
    assert delivered == [(None, "empty")], delivered


async def test_the_budget_sits_just_past_the_control_ui():
    """The Control UI gives up at 120 s; waiting longer waits on nothing."""
    if "TEAPORT_ASYNC_CONSULT_TIMEOUT" in os.environ:
        return  # an override is the operator's call
    assert 120 < tools._ASYNC_CONSULT_TIMEOUT <= 150, tools._ASYNC_CONSULT_TIMEOUT


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run_aio():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
