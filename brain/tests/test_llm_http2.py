#
# llm_http2: the model's HTTP/2 connection survives cancelled replies (#201).
#
# A local HTTP/2 server (h2, cleartext with prior knowledge: the client's http1=False)
# streams each response in chunks the way the model streams tokens, and records what the
# client did: the connections it opened, the streams it reset, the bytes each stream was
# sent. httpcore 1.0.9 alone fails three of these cases (no reset, the window drained, the
# 101st request refused); each case below says which.
#
# Run: python test_llm_http2.py   (or via pytest test_suite.py)
#
import asyncio
import inspect
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

import h2.config  # noqa: E402
import h2.connection  # noqa: E402
import h2.events  # noqa: E402
import h2.exceptions  # noqa: E402
import httpx  # noqa: E402

from teaport_brain import llm_http2  # noqa: E402

llm_http2.enable()


class Server:
    """An HTTP/2 server streaming `size` bytes per request, `chunk` at a time, `delay`
    seconds apart."""

    def __init__(self, size=200_000, chunk=400, delay=0.01):
        self.size, self.chunk, self.delay = size, chunk, delay
        self.connections, self.resets, self.sent = 0, set(), {}

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        for task in getattr(self, "_tasks", ()):
            task.cancel()

    async def close_idle(self, goaway):
        conn, writer = self._last
        if goaway:
            conn.close_connection()
            writer.write(conn.data_to_send())
            await writer.drain()
        writer.close()

    async def ping(self):
        conn, writer = self._last
        conn.ping(b"12345678")
        writer.write(conn.data_to_send())
        await writer.drain()

    async def _handle(self, reader, writer):
        self.connections += 1
        conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        self._last = (conn, writer)
        window = asyncio.Event()
        self._tasks = getattr(self, "_tasks", [])

        async def respond(sid):
            conn.send_headers(sid, [(":status", "200"), ("content-type", "text/event-stream")])
            writer.write(conn.data_to_send())
            while self.sent[sid] < self.size and sid not in self.resets:
                n = min(self.chunk, conn.local_flow_control_window(sid),
                        self.size - self.sent[sid])
                if n <= 0:
                    window.clear()
                    await window.wait()
                    continue
                conn.send_data(sid, b"x" * n)
                self.sent[sid] += n
                writer.write(conn.data_to_send())
                await writer.drain()
                if self.delay:
                    await asyncio.sleep(self.delay)
            if sid not in self.resets:
                conn.end_stream(sid)
                writer.write(conn.data_to_send())

        try:
            while data := await reader.read(65536):
                if writer.is_closing():
                    return
                for ev in conn.receive_data(data):
                    if isinstance(ev, h2.events.RequestReceived):
                        self.sent[ev.stream_id] = 0
                        self._tasks.append(asyncio.ensure_future(respond(ev.stream_id)))
                    elif isinstance(ev, h2.events.StreamReset):
                        self.resets.add(ev.stream_id)
                    elif isinstance(ev, h2.events.WindowUpdated):
                        window.set()
                writer.write(conn.data_to_send())
        except (ConnectionError, h2.exceptions.ProtocolError):
            pass


def _client():
    return httpx.AsyncClient(http1=False, http2=True, timeout=httpx.Timeout(5.0))


async def _cut(client, url, wait=0.0):
    """A barge-in: the reply's first chunk, then the stream is closed."""
    async with client.stream("GET", url) as r:
        if wait:
            await asyncio.sleep(wait)
        async for _ in r.aiter_raw():
            break


def _h2(client):
    (conn,) = client._transport._pool.connections
    return conn._connection


async def _until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return pred()


async def test_a_cut_reply_is_reset_and_the_server_stops_sending():
    # Stock httpcore: no RST_STREAM at all, and each cut stream is sent in full.
    async with Server(size=200_000) as server, _client() as client:
        for _ in range(5):
            await _cut(client, server.url)
        assert await _until(lambda: len(server.resets) == 5), server.resets
        assert all(n < 20_000 for n in server.sent.values()), server.sent
        assert server.connections == 1


async def test_more_cuts_than_the_stream_limit_leave_the_connection_usable():
    # Stock httpcore: request 101 fails, "Max outbound streams is 100, 100 open".
    async with Server(size=200_000) as server, _client() as client:
        for _ in range(120):
            await _cut(client, server.url)
        assert _h2(client)._h2_state.open_outbound_streams == 0
        server.size, server.delay = 4_000, 0
        r = await client.get(server.url)
        assert r.status_code == 200 and len(r.content) == 4_000
        assert server.connections == 1, server.connections


async def test_short_replies_cut_unread_do_not_drain_the_window():
    # A reply sent in full before it was read (h2 has seen it end), cut before its data
    # was read: that data was never acknowledged. Stock httpcore leaks it from the
    # connection's 16 MiB window -- 60 KB x 450 cuts leave the server ~16 KB to send in,
    # and every stream after crawls 16 KB per round trip, or stalls. Acknowledged, the
    # server's window stays above half (h2 returns credit in half-window steps).
    async with Server(size=60_000, chunk=16_384, delay=0) as server, _client() as client:
        for _ in range(450):
            await _cut(client, server.url, wait=0.002)
        await asyncio.sleep(0.1)
        window = server._last[0].outbound_flow_control_window
        assert window > 4 * 1024 * 1024, window
        server.size = 200_000
        r = await client.get(server.url)
        assert r.status_code == 200 and len(r.content) == 200_000
        assert server.connections == 1, server.connections


async def test_a_speculation_and_the_real_request_share_the_connection():
    # The speculation is cut while the real request streams beside it; the real one is
    # untouched and nothing opens a second connection.
    async with Server(size=8_000, chunk=400, delay=0.005) as server, _client() as client:
        real = asyncio.ensure_future(client.get(server.url))
        await _cut(client, server.url)
        r = await real
        assert r.status_code == 200 and len(r.content) == 8_000
        assert server.connections == 1, server.connections


async def test_a_reply_read_to_its_end_is_not_reset():
    async with Server(size=4_000, chunk=400, delay=0) as server, _client() as client:
        for _ in range(3):
            assert len((await client.get(server.url)).content) == 4_000
        await asyncio.sleep(0.1)
        assert not server.resets, server.resets


async def test_a_cut_on_a_connection_that_died_is_contained():
    # The reset's frames cannot go out: the connection is gone. The cut itself raises
    # nothing into the caller's turn. The next request may find the dead connection --
    # a transport error, which the SDK retries -- and the retry gets a new one.
    async with Server(size=200_000) as server, _client() as client:
        async with client.stream("GET", server.url) as r:
            async for _ in r.aiter_raw():
                break
            _h2(client)._network_stream._stream._transport.abort()
        await asyncio.sleep(0.1)
        server.size, server.delay = 4_000, 0
        try:
            await client.get(server.url)
        except httpx.TransportError:
            pass
        assert len((await client.get(server.url)).content) == 4_000
        assert server.connections == 2, server.connections


async def test_a_connection_the_server_closed_with_a_goaway_moves_the_request():
    # What a server closing an idle connection does (OpenRouter, 2026-10-16: a GOAWAY's
    # record waiting on the socket): httpcore reads it on the next request and sends that
    # request on a new connection -- no error, no SDK retry. services keeps connections
    # idle for less than OpenRouter does, so this is the rare case, not the turn's.
    async with Server(size=4_000, delay=0) as server, _client() as client:
        assert len((await client.get(server.url)).content) == 4_000
        await server.close_idle(goaway=True)
        await asyncio.sleep(0.1)
        assert len((await client.get(server.url)).content) == 4_000
        assert server.connections == 2, server.connections


async def test_a_frame_to_an_idle_connection_keeps_it():
    # A PING to an idle connection is not a close: the connection stays.
    async with Server(size=4_000, delay=0) as server, _client() as client:
        assert len((await client.get(server.url)).content) == 4_000
        await server.ping()
        await asyncio.sleep(0.1)
        assert len((await client.get(server.url)).content) == 4_000
        assert server.connections == 1, server.connections


async def test_the_request_after_a_cut_rides_the_same_connection():
    # The cut reply's data already on its way when the reset went out waits on the idle
    # connection's socket; it is not a close. (Live 2026-10-16, a version that took an
    # idle readable socket for one cost each cut a new connection, and sometimes the
    # reset.)
    async with Server(size=400_000, chunk=400, delay=0) as server, _client() as client:
        for _ in range(5):
            await _cut(client, server.url)
            r = await client.get(server.url)
            assert r.status_code == 200
            server.size = 400_000
        assert server.connections == 1, server.connections


async def test_an_idle_connection_left_alone_is_reused():
    async with Server(size=4_000, delay=0) as server, _client() as client:
        for _ in range(3):
            assert len((await client.get(server.url)).content) == 4_000
            await asyncio.sleep(0.1)
        assert server.connections == 1, server.connections


def test_the_model_client_uses_http2():
    from teaport_brain.services import BoundedOpenAILLMService, OpenAILLMService
    svc = BoundedOpenAILLMService(api_key="t", base_url="https://openrouter.ai/api/v1",
                                  settings=OpenAILLMService.Settings(model="m"))
    pool = svc._client._client._transport._pool
    assert pool._http2 and pool._http1, (pool._http1, pool._http2)


def test_moved_internals_keep_http1_and_say_so():
    from loguru import logger
    saved = (llm_http2._enabled, llm_http2._original,
             llm_http2.AsyncHTTP2Connection._write_outgoing_data)
    lines = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="ERROR")
    try:
        llm_http2._enabled = llm_http2._original = None
        del llm_http2.AsyncHTTP2Connection._write_outgoing_data
        assert llm_http2.enable() is False
    finally:
        logger.remove(sink)
        (llm_http2._enabled, llm_http2._original,
         llm_http2.AsyncHTTP2Connection._write_outgoing_data) = saved
    assert any("stays on HTTP/1.1" in m for m in lines), lines


def test_httpcore_still_lacks_the_reset():
    # When this fails, httpcore resets a closed stream itself (encode/httpcore#1098):
    # delete llm_http2's wrap, keep the tests above.
    source = inspect.getsource(llm_http2._original)
    assert "reset_stream" not in source and "acknowledge_received_data" not in source


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
