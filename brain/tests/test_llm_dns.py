#
# llm_dns.StaleDNSBackend: the model's last known address, used when a DNS lookup stalls
# or fails (live 2026-10-15: the router's DNS stopped answering for ~20 s and three turns
# were lost to connect timeouts spent on the lookup).
#
# The resolver and the inner backend are fakes here, so each case says exactly what the
# network did; one end-to-end case runs real sockets through httpx.
#
# Run: python test_llm_dns.py   (or via pytest test_suite.py)
#
import asyncio
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

import httpcore  # noqa: E402
import httpx  # noqa: E402

from teaport_brain import llm_dns  # noqa: E402
from teaport_brain.llm_dns import StaleDNSBackend, use_stale_dns  # noqa: E402

HOST = "model.test"
FAST = 0.1


class Resolver:
    """A DNS server that answers, stalls, or fails, as the case needs."""

    def __init__(self, answer=("10.0.0.1",), delay=0.0, error=None):
        self.answer, self.delay, self.error = list(answer), delay, error
        self.calls = 0

    async def __call__(self, host, port):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return list(self.answer)


class Stream(httpcore.AsyncNetworkStream):
    def __init__(self, host, log):
        self.host, self.log = host, log

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.log.append(("tls", server_hostname))
        raise httpcore.ConnectError("test: stop at the TLS handshake")

    async def aclose(self):
        pass


class Inner(httpcore.AsyncNetworkBackend):
    """The stock backend's stand-in: records where it was asked to connect."""

    def __init__(self, refuse=(), hang=()):
        self.log = []
        self.refuse, self.hang = set(refuse), set(hang)

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        self.log.append(("connect", host, timeout))
        if timeout is not None and timeout <= 0:     # as anyio.fail_after(0) would
            raise httpcore.ConnectTimeout(f"test: no time left for {host}")
        if host in self.refuse:
            raise httpcore.ConnectError(f"test: {host} refused")
        if host in self.hang:              # a black-holed route: no answer at all
            await asyncio.sleep(timeout if timeout is not None else 3600)
            raise httpcore.ConnectTimeout(f"test: {host} never answered")
        return Stream(host, self.log)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise NotImplementedError

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def _pending_tasks():
    return [task for hosts in list(llm_dns._PENDING.values()) for task, _ in hosts.values()]


def _fresh():
    # A lookup still stalled from an earlier case must not land in this one's record.
    for task in _pending_tasks():
        task.cancel()
    llm_dns._KNOWN.clear()
    llm_dns._PENDING.clear()
    llm_dns._FAILED.clear()
    llm_dns.FAST_S = FAST


def _connects(inner):
    return [entry[1] for entry in inner.log if entry[0] == "connect"]


async def test_a_quick_lookup_is_used_and_remembered():
    _fresh()
    inner = Inner()
    await StaleDNSBackend(inner, Resolver()).connect_tcp(HOST, 443, timeout=5)
    assert _connects(inner) == ["10.0.0.1"], inner.log  # its own answer: no second lookup
    assert llm_dns._KNOWN[HOST][0] == ["10.0.0.1"]


async def test_a_stalled_lookup_uses_the_last_known_address():
    _fresh()
    inner = Inner()
    await StaleDNSBackend(inner, Resolver(["10.0.0.1"])).connect_tcp(HOST, 443, timeout=5)
    stalled = Resolver(["10.0.0.2"], delay=0.4)
    t = time.monotonic()
    await StaleDNSBackend(inner, stalled).connect_tcp(HOST, 443, timeout=5)
    took = time.monotonic() - t
    assert _connects(inner)[-1] == "10.0.0.1" and took < FAST + 0.2, (inner.log, took)
    # The lookup goes on, and its answer is the next connection's.
    await asyncio.sleep(0.5)
    assert llm_dns._KNOWN[HOST][0] == ["10.0.0.2"], llm_dns._KNOWN


async def test_a_failed_lookup_uses_the_last_known_address_at_once():
    _fresh()
    inner = Inner()
    await StaleDNSBackend(inner, Resolver(["10.0.0.1"])).connect_tcp(HOST, 443, timeout=5)
    failing = Resolver(error=socket.gaierror(-3, "Temporary failure in name resolution"))
    t = time.monotonic()
    await StaleDNSBackend(inner, failing).connect_tcp(HOST, 443, timeout=5)
    assert _connects(inner)[-1] == "10.0.0.1" and time.monotonic() - t < FAST
    assert llm_dns._KNOWN[HOST][0] == ["10.0.0.1"], "a failed lookup must not forget the address"


async def test_with_nothing_remembered_a_stalled_lookup_is_the_connect_timeout():
    _fresh()
    try:
        await StaleDNSBackend(Inner(), Resolver(delay=5)).connect_tcp(HOST, 443, timeout=0.2)
    except httpcore.ConnectTimeout:
        pass
    else:
        raise AssertionError("a lookup longer than the connect budget connected")


async def test_with_nothing_remembered_a_failed_lookup_is_a_connect_error():
    _fresh()
    failing = Resolver(error=socket.gaierror(-2, "Name or service not known"))
    try:
        await StaleDNSBackend(Inner(), failing).connect_tcp(HOST, 443, timeout=1)
    except httpcore.ConnectError:
        pass
    else:
        raise AssertionError("a failed lookup connected")


async def test_an_address_older_than_a_day_is_not_used():
    _fresh()
    llm_dns._KNOWN[HOST] = (["10.0.0.1"], time.monotonic() - llm_dns.MAX_AGE_S - 1)
    inner = Inner()
    await StaleDNSBackend(inner, Resolver(["10.0.0.3"], delay=0.3)).connect_tcp(HOST, 443, timeout=5)
    assert _connects(inner) == ["10.0.0.3"], inner.log  # waited for the lookup instead


async def test_the_next_remembered_address_is_tried_when_one_refuses():
    _fresh()
    llm_dns._KNOWN[HOST] = (["10.0.0.1", "10.0.0.2"], time.monotonic())
    inner = Inner(refuse={"10.0.0.1"})
    await StaleDNSBackend(inner, Resolver(delay=5)).connect_tcp(HOST, 443, timeout=5)
    assert _connects(inner) == ["10.0.0.1", "10.0.0.2"], inner.log


async def test_an_address_is_connected_to_directly():
    _fresh()
    resolver, inner = Resolver(), Inner()
    await StaleDNSBackend(inner, resolver).connect_tcp("127.0.0.1", 80, timeout=1)
    assert resolver.calls == 0 and _connects(inner) == ["127.0.0.1"]


async def test_connections_waiting_on_one_host_share_one_lookup():
    # A stalled lookup holds an executor thread until the resolver gives up; a second
    # connection to the same host must not start another.
    _fresh()
    llm_dns._KNOWN[HOST] = (["10.0.0.1"], time.monotonic())
    resolver = Resolver(delay=0.4)
    backend = StaleDNSBackend(Inner(), resolver)
    await asyncio.gather(backend.connect_tcp(HOST, 443, timeout=5),
                         backend.connect_tcp(HOST, 443, timeout=5))
    assert resolver.calls == 1, resolver.calls
    await asyncio.sleep(0.5)


async def test_tls_still_verifies_the_name_asked_for():
    # Connected to a remembered ADDRESS, the TLS handshake must still be for the host:
    # httpcore takes server_hostname from the request, not from the socket.
    _fresh()
    llm_dns._KNOWN[HOST] = (["10.0.0.1"], time.monotonic())
    inner = Inner()
    pool = httpcore.AsyncConnectionPool(network_backend=StaleDNSBackend(inner, Resolver(delay=5)))
    try:
        await pool.request("GET", f"https://{HOST}/v1")
    except httpcore.ConnectError:
        pass
    assert ("connect", "10.0.0.1") in [e[:2] for e in inner.log], inner.log
    assert ("tls", HOST) in inner.log, inner.log
    await pool.aclose()


async def test_end_to_end_a_request_rides_out_a_dns_stall():
    _fresh()

    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    resolver = Resolver(["127.0.0.1"])
    client = use_stale_dns(httpx.AsyncClient(timeout=httpx.Timeout(5, connect=5)))
    client._transport._pool._network_backend = StaleDNSBackend(resolve=resolver)
    try:
        async with client as c:
            assert (await c.get(f"http://{HOST}:{port}/")).text == "ok"
            resolver.delay = 10           # the router's DNS stops answering
            t = time.monotonic()
            assert (await c.get(f"http://{HOST}:{port}/")).text == "ok"
            assert time.monotonic() - t < FAST + 0.5
    finally:
        server.close()
        for task in _pending_tasks():
            task.cancel()


def test_the_model_client_uses_the_remembered_addresses():
    from teaport_brain.services import BoundedOpenAILLMService, OpenAILLMService
    svc = BoundedOpenAILLMService(api_key="t", base_url="https://openrouter.ai/api/v1",
                                  settings=OpenAILLMService.Settings(model="m"))
    pool = svc._client._client._transport._pool
    assert isinstance(pool._network_backend, StaleDNSBackend), pool._network_backend
    assert pool._keepalive_expiry is None and pool._max_keepalive_connections == 100


async def test_a_black_holed_ipv6_route_does_not_spend_the_budget():
    # The box has IPv6. With v6 first in the resolver's order and its route dropping
    # packets, IPv4 must still connect at once: the families alternate from IPv4.
    _fresh()
    llm_dns._KNOWN[HOST] = (["2001:db8::1", "10.0.0.1"], time.monotonic())
    inner = Inner(hang={"2001:db8::1"})
    t = time.monotonic()
    await StaleDNSBackend(inner, Resolver(delay=5)).connect_tcp(HOST, 443, timeout=5)
    took = time.monotonic() - t
    assert _connects(inner) == ["10.0.0.1"] and took < FAST + 0.3, (inner.log, took)


async def test_with_nothing_remembered_a_cancelled_lookup_is_a_connect_error():
    _fresh()
    backend = StaleDNSBackend(Inner(), Resolver(delay=5))
    connecting = asyncio.ensure_future(backend.connect_tcp(HOST, 443, timeout=5))
    await asyncio.sleep(0.05)
    _pending_tasks()[0].cancel()
    try:
        await connecting
    except httpcore.ConnectError:
        pass
    else:
        raise AssertionError("a cancelled lookup connected")


def test_an_international_name_is_primed_under_the_name_it_connects_with():
    host = httpx.URL("https://münchen.example/v1").raw_host.decode("ascii")
    assert host.startswith("xn--"), host


async def test_a_dead_ipv4_address_leaves_time_for_ipv6():
    # IPv4 goes first but only takes a share of the budget, and IPv6 comes second even
    # when the host has more IPv4 addresses: an IPv6-only path still works.
    _fresh()
    llm_dns._KNOWN[HOST] = (["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "2001:db8::1"],
                            time.monotonic())
    inner = Inner(hang={"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"})
    t = time.monotonic()
    await StaleDNSBackend(inner, Resolver(delay=5)).connect_tcp(HOST, 443, timeout=5)
    took = time.monotonic() - t
    # IPv4's share outlasts one lost SYN (2 s); IPv6 is next, not behind four IPv4.
    assert _connects(inner) == ["10.0.0.1", "2001:db8::1"] and took < 2.6, (inner.log, took)
    assert inner.log[0][2] >= llm_dns.MIN_ATTEMPT_S - 0.01, inner.log


async def test_a_cancelled_lookup_falls_back_rather_than_cancelling_the_request():
    # The lookup a connection is waiting on gets cancelled under it (a loop shutting
    # down): the connection falls back, it does not raise CancelledError.
    _fresh()
    saved, llm_dns.FAST_S = llm_dns.FAST_S, 2.0
    try:
        llm_dns._KNOWN[HOST] = (["10.0.0.1"], time.monotonic())
        backend = StaleDNSBackend(Inner(), Resolver(delay=5))
        connecting = asyncio.ensure_future(backend.connect_tcp(HOST, 443, timeout=5))
        await asyncio.sleep(0.05)
        task = _pending_tasks()[0]
        task.cancel()
        t = time.monotonic()
        await connecting
        assert time.monotonic() - t < 0.5 and _connects(backend._inner) == ["10.0.0.1"]
    finally:
        llm_dns.FAST_S = saved


async def test_a_lookup_that_failed_moments_ago_is_not_waited_on_again():
    # A long outage: the resolver gives up on each lookup after 5-15 s. The next
    # connection's fresh lookup is not waited on: the last one just failed.
    _fresh()
    llm_dns._KNOWN[HOST] = (["10.0.0.1"], time.monotonic())
    llm_dns._FAILED[HOST] = time.monotonic()
    inner = Inner()
    t = time.monotonic()
    await StaleDNSBackend(inner, Resolver(delay=5)).connect_tcp(HOST, 443, timeout=5)
    assert time.monotonic() - t < FAST / 2 and _connects(inner) == ["10.0.0.1"]


async def test_a_stall_already_known_is_not_waited_on_again():
    _fresh()
    llm_dns._KNOWN[HOST] = (["10.0.0.1"], time.monotonic())
    backend = StaleDNSBackend(Inner(), Resolver(delay=5))
    await backend.connect_tcp(HOST, 443, timeout=5)        # pays FAST_S once
    t = time.monotonic()
    await backend.connect_tcp(HOST, 443, timeout=5)        # the same lookup, still stalled
    assert time.monotonic() - t < FAST / 2, time.monotonic() - t
    for task in _pending_tasks():
        task.cancel()


async def test_the_wait_never_outlasts_the_connect_budget():
    _fresh()
    saved, llm_dns.FAST_S = llm_dns.FAST_S, 1.5
    try:
        llm_dns._KNOWN[HOST] = (["10.0.0.1"], time.monotonic())
        inner = Inner()
        t = time.monotonic()
        await StaleDNSBackend(inner, Resolver(delay=5)).connect_tcp(HOST, 443, timeout=0.3)
        # Half the budget waited at most, and the connect given what is left (> 0).
        assert time.monotonic() - t < 0.5 and _connects(inner) == ["10.0.0.1"], inner.log
        assert inner.log[0][2] > 0.1, inner.log
    finally:
        llm_dns.FAST_S = saved
        for task in _pending_tasks():
            task.cancel()


async def test_the_client_looks_the_host_up_when_it_is_built():
    # After a brain restart the first turn must already have an address to fall back on
    # -- and a remembered one too old to use counts as none.
    _fresh()
    llm_dns._KNOWN["localhost"] = (["127.0.0.9"], time.monotonic() - llm_dns.MAX_AGE_S - 1)
    client = use_stale_dns(httpx.AsyncClient(), "http://localhost:9/v1")
    assert await _until(lambda: llm_dns._KNOWN["localhost"][0] != ["127.0.0.9"]), llm_dns._KNOWN
    await client.aclose()


def test_proxies_from_the_environment_are_kept_and_covered():
    # Replacing the client's transport would drop its env-proxy mounts; the backend goes
    # under the client's own transports instead -- the proxy's too, so the proxy host's
    # address is remembered as well.
    names = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")
    saved = {n: os.environ.pop(n, None) for n in names}
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:3128"
    os.environ["NO_PROXY"] = "localhost"          # its mount is None: skipped, not a crash
    try:
        client = use_stale_dns(httpx.AsyncClient(trust_env=True))
        proxies = [t for t in client._mounts.values() if t is not None]
        assert proxies, "the proxy mount is gone"
        for transport in proxies:
            assert isinstance(transport._pool._network_backend, StaleDNSBackend), transport
    finally:
        for n in names:
            os.environ.pop(n, None)
            if saved[n] is not None:
                os.environ[n] = saved[n]


async def test_behind_a_proxy_the_proxys_host_is_looked_up_at_build():
    # The model host is never connected to directly there: the proxy's address is the
    # one to have ready.
    _fresh()
    resolved = []

    async def resolve(host, port):
        resolved.append(host)
        return ["127.0.0.1"]
    names = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")
    saved = {n: os.environ.pop(n, None) for n in names}
    os.environ["HTTPS_PROXY"] = "http://proxy.test:3128"
    try:
        client = httpx.AsyncClient(trust_env=True)
        orig = llm_dns.StaleDNSBackend.__init__
        llm_dns.StaleDNSBackend.__init__ = lambda self, inner=None, resolve_=None: orig(
            self, inner, resolve)
        try:
            use_stale_dns(client, "https://openrouter.ai/api/v1")
        finally:
            llm_dns.StaleDNSBackend.__init__ = orig
        assert await _until(lambda: resolved), resolved
        assert resolved == ["proxy.test"], resolved
    finally:
        for n in names:
            os.environ.pop(n, None)
            if saved[n] is not None:
                os.environ[n] = saved[n]


async def test_a_link_local_ipv6_address_keeps_its_scope():
    loop = asyncio.get_running_loop()

    async def getaddrinfo(host, port, type=0):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1", port, 0, 3)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", port))]
    loop.getaddrinfo = getaddrinfo
    try:
        assert await llm_dns._getaddrinfo("box.local", 80) == ["fe80::1%3", "192.168.1.9"]
    finally:
        del loop.getaddrinfo


async def test_an_empty_lookup_is_a_lookup_failure():
    _fresh()
    try:
        await StaleDNSBackend(Inner(), Resolver(answer=[])).connect_tcp(HOST, 443, timeout=1)
    except httpcore.ConnectError:
        pass
    else:
        raise AssertionError("an empty answer connected")
    llm_dns._KNOWN[HOST] = (["10.0.0.1"], time.monotonic())
    inner = Inner()
    await StaleDNSBackend(inner, Resolver(answer=[])).connect_tcp(HOST, 443, timeout=1)
    assert _connects(inner) == ["10.0.0.1"], inner.log
    assert llm_dns._KNOWN[HOST][0] == ["10.0.0.1"], "an empty answer must not be remembered"


def test_a_client_whose_pool_moved_keeps_the_stock_dns():
    # The hook is optional: a pool without it is logged, and the brain still answers.
    from loguru import logger
    client = httpx.AsyncClient()
    client._transport = object()
    lines = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="ERROR")
    try:
        assert use_stale_dns(client, "https://openrouter.ai/api/v1") is client
    finally:
        logger.remove(sink)
    assert any("keeps the stock DNS" in m for m in lines), lines


async def _until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return pred()


def main():
    saved = llm_dns.FAST_S
    sync = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    try:
        for fn in sync:
            fn()
            print(f"  ok {fn.__name__}")

        async def run():
            for fn in aio:
                await fn()
                print(f"  ok {fn.__name__}")
        asyncio.run(run())
    finally:
        llm_dns.FAST_S = saved
        _fresh()
        llm_dns.FAST_S = saved


if __name__ == "__main__":
    main()
    print("ALL PASS")
