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
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger  # noqa: E402

from teaport_brain import consult_bridge, tools  # noqa: E402
from teaport_brain import openclaw_client as oc  # noqa: E402
from tempdirs import tempdir  # noqa: E402

_CONSULT_ERR = os.path.join(tempdir("teaport-consult-"), "last.err")


def _http(code, reason):
    return urllib.error.HTTPError("http://gw/v1/chat/completions", code, reason, {}, None)


class _Warm:
    """Stands in for _completions_sync: each call pops the next scripted reply (a
    string, None for an empty turn, or an exception to raise)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, message, token, timeout, hangup=None):
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
        self._saved = (oc._completions_sync, oc._run_openclaw, oc._token, asyncio.sleep, oc.CONSULT_ERR_FILE)
        oc.CONSULT_ERR_FILE = _CONSULT_ERR   # not the box's file in /tmp
        oc._completions_sync = self.warm
        oc._run_openclaw = self._run
        oc._token = lambda: "tok"
        real_sleep = asyncio.sleep
        asyncio.sleep = lambda s, *a, **k: real_sleep(0)  # the 3 s retry pause
        return self

    def __exit__(self, *exc):
        oc._completions_sync, oc._run_openclaw, oc._token, asyncio.sleep, oc.CONSULT_ERR_FILE = self._saved


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


async def test_a_run_openclaw_cancelled_is_cancelled_not_empty():
    """#230: the relay submits {"status": "cancelled", "message": ...} for a consult its
    run control stopped (buildRealtimeVoiceAgentCancelProviderResult). It has no text,
    and read as "empty" the caller was told it "came back empty"."""
    result = {"status": "cancelled", "message": "Cancelled the active OpenClaw run."}
    assert consult_bridge.classify(result) == (
        None, "cancelled", "Cancelled the active OpenClaw run.")
    assert consult_bridge.classify({"status": "cancelled"})[1] == "cancelled"


async def test_the_runner_timeout_is_a_timeout():
    assert consult_bridge.classify({"error": "OpenClaw tool call timed out"})[1] == "timeout"
    # A run that times out inside the agent reports the agent's own message, and the
    # iOS client words the runner's wait differently; time still ran out.
    for err in ("preflight setup timed out", "agent runtime timeout",
                "OpenClaw realtime tool call timed out"):
        assert consult_bridge.classify({"error": err})[1] == "timeout", err
    assert consult_bridge.classify({"error": "provider authentication failed"})[1] == "error"
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

    class Owed:
        def land(self, request, text, key, *, failure=None, detail=""):
            delivered.append((text, failure))

    saved = (tools._NATIVE_CONSULT_ACK_TIMEOUT, tools._ASYNC_CONSULT_TIMEOUT,
             oc.agent_consult)
    tools._NATIVE_CONSULT_ACK_TIMEOUT, tools._ASYNC_CONSULT_TIMEOUT = ack, ceiling
    if cli is not None:
        oc.agent_consult = cli
    try:
        await tools._consult_and_land("call-1", fut, "news?", Owed(), "tc-1")
    finally:
        (tools._NATIVE_CONSULT_ACK_TIMEOUT, tools._ASYNC_CONSULT_TIMEOUT,
         oc.agent_consult) = saved
    return delivered


async def test_the_cli_fallback_gets_the_whole_async_budget():
    """SIP (no relay: fut None), or a relay that never acks: the CLI lane. It used to run
    on CONSULT_TIMEOUT's 45 s while Talk waited 180 s for the same requests."""
    for fut in (None, asyncio.get_running_loop().create_future()):
        seen = {}

        async def cli(request, *, timeout=None, session_key=None):
            seen["timeout"] = timeout
            return oc.ConsultOutcome("cli answer")

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


async def test_a_cancelled_run_is_delivered_as_cancelled():
    """#230: owed.land gets "cancelled" (and closes it silently), not "empty"."""
    fut = asyncio.get_running_loop().create_future()
    fut.set_result({"status": "cancelled", "message": "Cancelled the active OpenClaw run."})
    delivered = await _wait(fut)
    assert delivered == [(None, "cancelled")], delivered


async def test_the_budget_sits_just_past_the_control_ui():
    """The Control UI gives up at 120 s; waiting longer waits on nothing."""
    if "TEAPORT_ASYNC_CONSULT_TIMEOUT" in os.environ:
        return  # an override is the operator's call
    assert 120 < tools._ASYNC_CONSULT_TIMEOUT <= 150, tools._ASYNC_CONSULT_TIMEOUT


# --- the placeholder the model reads while a consult runs ---------------------------

class _PlaceholderLLM:
    def __init__(self):
        self.tasks = []

    async def push_frame(self, frame, *a, **k):
        pass

    def create_task(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(t)
        return t


class _Params:
    def __init__(self, llm, request):
        self.llm, self.arguments, self.tool_call_id = llm, {"question": request}, "tc-1"
        self.results = []

    async def result_callback(self, result, properties=None):
        self.results.append((result, properties))


async def _placeholders():
    """(first, duplicate) placeholder results for the same request."""
    from pipecat.processors.aggregators.llm_context import LLMContext
    from teaport_brain.owed import Owed

    owed = Owed(LLMContext([]))
    llm = _PlaceholderLLM()
    first, dup = _Params(llm, "French rioting news"), _Params(llm, "French rioting news")
    dup.tool_call_id = "tc-2"
    await tools._openclaw_agent_consult(first, owed=owed)
    await tools._openclaw_agent_consult(dup, owed=owed)
    for t in llm.tasks:
        t.cancel()
    await asyncio.gather(*llm.tasks, return_exceptions=True)
    return first.results[0], dup.results[0]


async def test_the_placeholder_says_there_are_no_results_yet():
    """It stays in the context for every turn the caller takes while the consult runs,
    so it must cover those turns, not only the first. Asked "did you get any news on
    that?" mid-consult, the old "Do not respond now" let the model invent the news
    (2026-10-03 19:08)."""
    for (result, props), status in zip(await _placeholders(),
                                       ("working_in_background", "duplicate")):
        assert result["status"] == status, result
        text = result["instruction"]
        assert "Do not respond now" in text, text
        assert "NO results" in text and "still waiting" in text, text
        assert "never present your own knowledge as something you found" in text, text
        assert props is not None and props.run_llm is False, (
            "a placeholder must not trigger inference -- that is the fabrication path "
            "no_inference() exists to close")


# --- a session without a relay asks the agent directly (#78) ------------------------
#
# Only a client that announced ?features=relay (the plugin on OpenClaw's Talk relay)
# has anything to run the consult's tool_call and ack it. The phone, the room's bridge
# and the Discord bridge announce no such thing: their consults used to send the call
# anyway and wait the whole ack timeout for an ack that could never come.

class _WireLLM(_PlaceholderLLM):
    """Records what the handler sends the client."""

    def __init__(self):
        super().__init__()
        self.sent = []

    async def push_frame(self, frame, *a, **k):
        msg = getattr(frame, "message", None)
        if isinstance(msg, dict):
            self.sent.append(msg)

    def calls(self):
        return [m for m in self.sent if m.get("type") == "tool_call"]


class _CallbackParams:
    def __init__(self, llm, request):
        self.llm, self.arguments, self.tool_call_id = llm, {"question": request}, "tc-1"
        self.results = []

    async def result_callback(self, result, properties=None):
        self.results.append(result)


async def _consult(ctx, *, ack, request="what is on at the cinema tonight?"):
    """Run one consult through the session's bound handler with a scripted agent lane:
    (llm, params, agent calls, seconds the handler and its waiter took, log text)."""
    asked = []

    async def agent(message, *, timeout=None, session_key=None):
        asked.append(message)
        return oc.ConsultOutcome("two films and a quiz night")

    log = io.StringIO()
    sink = logger.add(log, format="{message}", level="INFO")
    saved = (tools._NATIVE_CONSULT_ACK_TIMEOUT, oc.agent_consult)
    tools._NATIVE_CONSULT_ACK_TIMEOUT, oc.agent_consult = ack, agent
    llm = _WireLLM()
    params = _CallbackParams(llm, request)
    t0 = time.monotonic()
    try:
        await tools._bind_openclaw_agent_consult(ctx)(params)
        await asyncio.wait_for(asyncio.gather(*llm.tasks), 5)
    finally:
        tools._NATIVE_CONSULT_ACK_TIMEOUT, oc.agent_consult = saved
        logger.remove(sink)
    return llm, params, asked, time.monotonic() - t0, log.getvalue()


def _owed_ctx(features):
    from pipecat.processors.aggregators.llm_context import LLMContext
    from teaport_brain.owed import Owed
    return tools.ToolContext(owed=Owed(LLMContext([])), client_features=frozenset(features))


async def test_only_a_client_that_announces_relay_has_a_relay():
    assert tools.ToolContext(client_features=tools.parse_client_features("relay")).relay
    # The room's bridge (as local_audio announces itself), the phone and Discord (none).
    for raw in ("volume,restart,local,phonemes,mood,typing,sleep", None, ""):
        assert not tools.ToolContext(client_features=tools.parse_client_features(raw)).relay


# The plugin's query as withVoiceParams builds it for a session with onToolCall -- a
# literal copy of what plugin/test/transcripts.test.mjs asserts it is ("the plugin says it
# has a relay only when the host gave it onToolCall"); and as a plugin from before #78
# builds it (no features, run_control since #236).
_PLUGIN_QUERY = "captions=2&session_config=1&run_control=1&features=relay"
_OLD_PLUGIN_QUERY = "captions=2&session_config=1&run_control=1"


async def _session_relay(**kw) -> bool:
    """Build a real session (agent_session.build_agent_session) and read `relay` off the
    ToolContext its register_tools built: the one the consult handler is bound with."""
    from test_client_notes import _Transport
    from teaport_brain.agent_session import build_agent_session

    built = []

    class Recorded(tools.ToolContext):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            built.append(self)

    saved = tools.ToolContext
    tools.ToolContext = Recorded
    try:
        build_agent_session(_Transport(), **kw)
    finally:
        tools.ToolContext = saved
    assert len(built) == 1, built
    return built[0].relay


async def _talk_relay(query: str) -> bool:
    """A /talk session for `query`, its features and run control read as gateway_server's
    run_relay_bot reads them."""
    from urllib.parse import parse_qs
    from teaport_brain import run_control
    qp = {k: v[-1] for k, v in parse_qs(query).items()}
    return await _session_relay(
        client_features=tools.parse_client_features(qp.get("features")),
        run_control=qp.get("run_control") == run_control.PROTOCOL,
        context_notes=True, front_end="talk")


async def test_the_plugins_query_gives_its_session_a_relay_and_the_phone_none():
    assert await _talk_relay(_PLUGIN_QUERY)
    # Skew: install.sh --only brain updates the brain and leaves an older plugin, which
    # still runs consults through its relay and says run_control=1.
    assert await _talk_relay(_OLD_PLUGIN_QUERY)
    # The phone (sip_server's build) and the room's bridge.
    assert not await _session_relay(front_end="sip")
    assert not await _talk_relay("features=volume,restart,local,phonemes,mood,typing")
    # Discord: a plain /talk client.
    assert not await _talk_relay("client=discord")


async def test_a_session_without_a_relay_never_waits_on_an_ack():
    """Async (the room, the phone): no tool_call goes out, and the agent is asked at
    once -- with an ack timeout far longer than the test, any wait on it would show."""
    for features in (frozenset({"local", "volume", "typing"}), frozenset()):
        llm, params, asked, took, log = await _consult(_owed_ctx(features), ack=30.0)
        assert llm.calls() == [], llm.calls()
        assert asked == ["what is on at the cinema tonight?"], asked
        assert took < 2.0, took
        assert params.results[0]["status"] == "working_in_background", params.results
        assert "relay never acked" not in log and "no relay on this session" in log, log


async def test_a_sync_consult_without_a_relay_never_waits_on_an_ack():
    """Agent-first (no owed records): the same, on the turn."""
    llm, params, asked, took, log = await _consult(tools.ToolContext(), ack=30.0)
    assert llm.calls() == [] and asked and took < 2.0, (llm.calls(), asked, took)
    assert params.results == [{"answer": "two films and a quiz night"}], params.results
    assert "relay never acked" not in log, log


async def test_a_talk_session_still_goes_through_the_relay_first():
    """The plugin's session sends the call and waits for the relay's ack; only a relay
    that never acks falls back, and the log says how long it waited, to a tenth of a
    second (1.5 s used to print as "2s")."""
    for ctx in (_owed_ctx({"relay"}), tools.ToolContext(client_features=frozenset({"relay"}))):
        llm, params, asked, took, log = await _consult(ctx, ack=0.12)
        [call] = llm.calls()
        assert call["name"] == "openclaw_agent_consult", call
        assert call["args"] == {"question": "what is on at the cinema tonight?"}, call
        assert took >= 0.12, took
        assert asked == ["what is on at the cinema tonight?"], asked
        assert "relay never acked in 0.1s" in log, log


async def test_an_acked_talk_consult_is_answered_by_the_relay():
    """The relay's answer arrives: the agent lane is never asked."""
    asked = []

    async def agent(message, *, timeout=None, session_key=None):
        asked.append(message)
        return oc.ConsultOutcome("not this one")

    class Relay(_WireLLM):
        async def push_frame(self, frame, *a, **k):
            await super().push_frame(frame, *a, **k)
            msg = getattr(frame, "message", None)
            if isinstance(msg, dict) and msg.get("type") == "tool_call":
                asyncio.get_running_loop().call_soon(
                    consult_bridge.resolve, msg["call_id"], {"text": "from the relay"})

    saved = oc.agent_consult
    oc.agent_consult = agent
    llm = Relay()
    params = _CallbackParams(llm, "is it raining?")
    try:
        await tools._bind_openclaw_agent_consult(
            tools.ToolContext(client_features=frozenset({"relay"})))(params)
    finally:
        oc.agent_consult = saved
    assert params.results == [{"answer": "from the relay"}], params.results
    assert asked == [] and len(llm.calls()) == 1


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
