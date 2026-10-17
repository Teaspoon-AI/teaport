#
# The model's connection, shared by the sessions on a loop and opened ahead of a turn
# when it is not open (services: _model_http_client, warm_connection; #201).
#
# A local HTTP server counts the connections and requests the client makes. It speaks
# HTTP/1.1 (the client does over cleartext); what is checked here -- when a warm-up runs,
# and that the turn's request finds its connection -- does not depend on the protocol.
# test_llm_http2 covers the HTTP/2 connection itself.
#
# Run: python test_llm_warm.py   (or via pytest test_suite.py)
#
import asyncio
import inspect
import os
import sys

# Before teaport_brain.services is imported anywhere (see test_service_unusable).
os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.processors.frame_processor import FrameProcessor  # noqa: E402

from teaport_brain import services  # noqa: E402
from teaport_brain.services import BoundedOpenAILLMService, OpenAILLMService  # noqa: E402


class Server:
    """Counts connections and requests; answers every request with a short 200."""

    def __init__(self):
        self.connections, self.requests = 0, []

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.base_url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/v1"
        return self

    async def __aexit__(self, *exc):
        self._server.close()

    async def _handle(self, reader, writer):
        self.connections += 1
        try:
            while head := await reader.readuntil(b"\r\n\r\n"):
                method = head.split(b" ", 1)[0].decode()
                self.requests.append(method)
                body = b"" if method == "HEAD" else b"ok"     # a HEAD answer has no body
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n" + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


def _service(base_url):
    svc = BoundedOpenAILLMService(api_key="t", base_url=base_url,
                                  settings=OpenAILLMService.Settings(model="m"))
    # Outside a pipeline: the service's own task manager is not set up.
    svc.create_task = lambda coro, name=None: asyncio.ensure_future(coro)
    return svc


async def _settle(svc):
    if svc._warm_task is not None:
        await svc._warm_task


async def test_sessions_on_one_loop_share_the_client():
    a, b = _service("http://127.0.0.1:9/v1"), _service("http://127.0.0.1:9/v1")
    assert a._http is b._http and a._http is not None


def test_without_a_loop_each_service_gets_its_own_client():
    a, b = _service("http://127.0.0.1:9/v1"), _service("http://127.0.0.1:9/v1")
    assert a._http is not b._http


async def test_each_session_keeps_its_own_request_timeout():
    # The shared client's timeout is not what bounds a request: the session's is, set by
    # the SDK on each one.
    saved = os.environ.get("LLM_TIMEOUT_SECS")
    try:
        os.environ["LLM_TIMEOUT_SECS"] = "7"
        a = _service("http://127.0.0.1:9/v1")
        os.environ["LLM_TIMEOUT_SECS"] = "11"
        b = _service("http://127.0.0.1:9/v1")
    finally:
        os.environ.pop("LLM_TIMEOUT_SECS", None)
        if saved is not None:
            os.environ["LLM_TIMEOUT_SECS"] = saved
    assert a._http is b._http
    for svc, read in ((a, 7.0), (b, 11.0)):
        assert svc._client.timeout.read == read, svc._client.timeout
        request = svc._client._build_request(
            __import__("openai")._models.FinalRequestOptions(method="post", url="/x"))
        assert request.extensions["timeout"]["read"] == read, request.extensions


async def test_a_session_with_no_connection_open_opens_one_ahead_of_its_request():
    async with Server() as server:
        svc = _service(server.base_url)
        svc.warm_connection()
        await _settle(svc)
        assert server.requests == ["HEAD"] and server.connections == 1
        # The turn's request rides the connection the warm-up opened.
        await svc._http.get(server.base_url + "/models")
        assert server.connections == 1, server.connections


async def test_nothing_runs_while_a_connection_is_open():
    async with Server() as server:
        svc = _service(server.base_url)
        await svc._http.get(server.base_url + "/models")
        svc.warm_connection()
        await _settle(svc)
        assert server.requests == ["GET"], server.requests


async def test_an_idle_connection_is_retired_before_the_server_closes_it():
    # OpenRouter closes an idle connection after 5-10 minutes; the model's client retires
    # it first, and a connection past that counts as not open: the warm-up replaces it.
    assert services._KEEPALIVE_SECS < 300, services._KEEPALIVE_SECS
    async with Server() as server:
        svc = _service(server.base_url)
        assert svc._http._transport._pool._keepalive_expiry == services._KEEPALIVE_SECS
        await svc._http.get(server.base_url + "/models")
        port = int(server.base_url.rsplit(":", 1)[1].split("/")[0])
        (conn,) = [c for c in svc._http._transport._pool.connections
                   if c._origin.port == port]
        conn._connection._expire_at = 0.0           # idle past the keepalive
        svc.warm_connection()
        await _settle(svc)
        assert server.requests == ["GET", "HEAD"] and server.connections == 2, (
            server.requests, server.connections)


async def test_a_warm_up_waits_as_long_as_the_turn_would():
    # On a shared HTTP/2 connection a read timeout fails every stream on it: the warm-up's
    # must not be shorter than the turn's own.
    seen = []

    async def head(url, timeout):
        seen.append(timeout)
    svc = _service("http://127.0.0.1:9/v1")
    svc._http.head = head           # the loop's shared client: put back below
    try:
        svc.warm_connection()
        await _settle(svc)
    finally:
        del svc._http.head
    assert seen and seen[0].read == svc._client.timeout.read, (seen, svc._client.timeout)


def test_a_closed_loops_client_is_let_go():
    loops = []

    def build():
        async def go():
            loops.append(asyncio.get_running_loop())
            return _service("http://127.0.0.1:9/v1")._http
        return asyncio.new_event_loop().run_until_complete(go())
    first = build()
    loops[0].close()
    build()
    loops[1].close()
    assert loops[0] not in services._HTTP_CLIENTS and first is not None
    _service("http://127.0.0.1:9/v1")
    assert not any(loop.is_closed() for loop in services._HTTP_CLIENTS)


async def test_a_request_cut_without_an_answer_leaves_its_connection():
    # services: a request cancelled after _NO_ANSWER_SECS with no response headers takes
    # the connection out of reuse; one cancelled sooner (the usual barge-in or discarded
    # speculation, or a provider still starting) does not.
    from teaport_brain import llm_http2
    calls, saved = [], (llm_http2.stop_reusing, services._NO_ANSWER_SECS,
                        OpenAILLMService.get_chat_completions)

    async def never(self, context):
        await asyncio.sleep(3600)
    llm_http2.stop_reusing = lambda conns: calls.append(list(conns)) or 1
    services._NO_ANSWER_SECS = 0.2
    OpenAILLMService.get_chat_completions = never
    try:
        svc = _service("http://127.0.0.1:9/v1")
        for wait, expected in ((0.05, 0), (0.3, 1)):
            try:
                await asyncio.wait_for(svc._open_stream(None), wait)
            except asyncio.TimeoutError:
                pass
            assert len(calls) == expected, (wait, calls)
    finally:
        (llm_http2.stop_reusing, services._NO_ANSWER_SECS,
         OpenAILLMService.get_chat_completions) = saved


async def test_one_warm_up_at_a_time():
    async with Server() as server:
        svc = _service(server.base_url)
        svc.warm_connection()
        first = svc._warm_task
        svc.warm_connection()
        assert svc._warm_task is first
        await _settle(svc)
        assert server.requests == ["HEAD"], server.requests


async def test_a_server_that_does_not_answer_is_only_logged():
    from loguru import logger
    lines = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        svc = _service("http://127.0.0.1:9/v1")      # nothing listens there
        svc.warm_connection()
        await _settle(svc)
    finally:
        logger.remove(sink)
    assert any("ahead of the turn" in m for m in lines), lines


async def test_a_cleaned_up_service_starts_no_warm_up():
    async with Server() as server:
        svc = _service(server.base_url)
        svc.cancel_task = lambda task, timeout=None: _cancel(task)
        await svc.cleanup()
        svc.warm_connection()
        assert svc._warm_task is None and server.requests == []


async def _cancel(task):
    task.cancel()


async def test_a_user_turn_start_warms_the_connection():
    # The session wires the caller's turn start to the warm-up.
    from teaport_brain.agent_session import build_agent_session

    class _Stub(FrameProcessor):
        pass

    class _Transport:
        def __init__(self):
            self._in, self._out = _Stub(name="stub-in"), _Stub(name="stub-out")

        def input(self):
            return self._in

        def output(self):
            return self._out

    session = build_agent_session(_Transport())
    calls = []
    session.llm.warm_connection = lambda: calls.append(1)
    def walk(processors):
        for p in processors:
            yield p
            yield from walk(getattr(p, "processors", ()))
    (user,) = [p for p in walk(session.task.pipeline.processors)
               if "on_user_turn_started" in getattr(p, "_event_handlers", {})]
    handlers = user._event_handlers["on_user_turn_started"]
    for handler in getattr(handlers, "handlers", handlers):
        await handler(user, None)
    assert calls == [1], calls


def main():
    sync = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and not inspect.iscoroutinefunction(v)]
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and inspect.iscoroutinefunction(v)]
    for fn in sync:
        fn()
        print(f"  ok {fn.__name__}")

    async def run():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
