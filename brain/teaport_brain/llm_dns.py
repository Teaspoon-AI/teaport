#
# llm_dns.py — the language model's address, remembered, so a stalled DNS server does
# not cost a turn.
#
# Every new connection to the model starts with a DNS lookup, and the lookup is the
# network's. Live 2026-10-15 22:19 the home router's DNS stopped answering for ~20 s:
# systemd-resolved first gave it 5 s per attempt, then fell back to TCP and gave it 10 s,
# each request's connect timeout ran out on the lookup, and three turns went unanswered.
# The resolver's own serve-stale (install.sh) answers from its cache only after that
# first 5-10 s attempt -- too late for a turn.
#
# So the brain keeps the last answer for each host it reaches the model on, and does not
# wait long for a new one. StaleDNSBackend, the model client's httpcore network backend:
#
#   * looks the host up; an answer within FAST_S (the usual case: milliseconds from the
#     resolver's cache) is recorded and used;
#   * a lookup still pending after FAST_S, or one that failed, uses the last addresses
#     that host had (within MAX_AGE_S) instead, and carries on in the background to
#     refresh them. A stall already known (the host's lookup pending longer than FAST_S)
#     is not waited on again. With nothing remembered, the lookup is waited for inside
#     the connect budget, as before;
#   * connects to the addresses itself (no second lookup), one after another, the
#     families alternating from IPv4, each attempt given a share of the connect budget
#     left. The box has IPv6: a black-holed IPv6 route must not spend the budget before
#     IPv4 is tried, nor a dead IPv4 one before IPv6 -- the stock backend's connect by
#     name raced the families; alternating them, a share each, gets a home network the
#     same with no concurrent attempts to clean up. (It ignores gai.conf's preference;
#     a home router answers on both or on IPv4.) Remembered addresses that all fail --
#     the host moved -- give way to the lookup's own answer, inside the same budget.
#
# The model's host is first looked up when the brain starts and again when each
# session's client is built (prime), so the first turn after a restart already has
# something to fall back on -- if DNS answers then. The record lives in memory: a
# restart during an outage starts with nothing, which the resolver's own serve-stale
# (install.sh's drop-in, packaging/resolved) is there to cover, after its first attempt.
#
# TLS is unaffected: httpcore sets the server name it verifies from the request's own
# host, not from the address the socket was opened to (AsyncHTTPConnection._connect,
# "server_hostname"), so a connection to an address still checks the certificate of the
# name asked for. test_llm_dns pins that.
#
# Memory: a few addresses per host, for the one or two hosts the model is reached on. A
# stalled lookup holds one of asyncio's executor threads until the resolver gives up;
# connections to the same host on the same loop share it rather than starting another.
#
import asyncio
import ipaddress
import socket
import time
import weakref

import httpcore
import httpx
from loguru import logger

# How long a lookup may take before a remembered address is used instead. The
# resolver's cache answers in milliseconds; a lookup this slow is the network's DNS
# server not answering.
FAST_S = 1.5
# How old a remembered address may be. Cloudflare-fronted endpoints keep theirs for
# months; a day matches the resolver's serve-stale drop-in (packaging/resolved).
MAX_AGE_S = 24 * 3600.0
# How many addresses are tried, and the least time each is given: 2 s outlasts one lost
# SYN (Linux retransmits the first after 1 s).
MAX_ADDRESSES = 4
MIN_ATTEMPT_S = 2.0
# A lookup for the host that failed this recently means the DNS server is down: the
# next connection does not wait FAST_S for another before using the remembered address.
FAILED_FOR_S = 30.0
# A lookup in flight longer than this is not shared with a new connection: it is stuck
# on a resolver that has since recovered, or never will; a fresh one is asked instead.
SHARE_FOR_S = 10.0

# host -> (addresses, when the lookup that found them started)
_KNOWN: dict = {}
# loop -> {host: (the lookup in flight, when it started)}; a task belongs to its loop
_PENDING: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
# host -> when its last lookup failed (or came back empty)
_FAILED: dict = {}


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        return False


async def _getaddrinfo(host: str) -> list:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    seen, out = set(), []
    for family, *_, sockaddr in infos:
        address = sockaddr[0]
        if family == socket.AF_INET6 and len(sockaddr) > 3 and sockaddr[3]:
            address = f"{address}%{sockaddr[3]}"      # keep a link-local address usable
        if address not in seen:
            seen.add(address)
            out.append(address)
    return out


def _alternate(addresses: list) -> list:
    """The families alternating, IPv4 first, each in the resolver's order: a host with
    many IPv4 addresses still gets an IPv6 one tried second."""
    v4 = [a for a in addresses if ":" not in a]
    v6 = [a for a in addresses if ":" in a]
    out = []
    for i in range(max(len(v4), len(v6))):
        out += v4[i:i + 1] + v6[i:i + 1]
    return out[:MAX_ADDRESSES]


def _left(timeout, started):
    return None if timeout is None else max(0.0, timeout - (time.monotonic() - started))


class StaleDNSBackend(httpcore.AsyncNetworkBackend):
    """The stock backend's connections, to addresses this backend looked up -- or, when
    the lookup is slow or fails, to the host's last known ones. `resolve` and `inner`
    are for tests."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None, resolve=None):
        self._inner = inner or httpcore.AnyIOBackend()
        self._resolve = resolve or _getaddrinfo

    def lookup(self, host: str) -> "tuple[asyncio.Task, float]":
        """The host's lookup on this loop: the one in flight (unless it has been for
        SHARE_FOR_S), or a new one."""
        loop = asyncio.get_running_loop()
        pending = _PENDING.setdefault(loop, {})
        entry = pending.get(host)
        if entry is not None and not entry[0].done() and \
                time.monotonic() - entry[1] < SHARE_FOR_S:
            return entry
        task = loop.create_task(self._resolve(host))
        entry = (task, time.monotonic())
        pending[host] = entry

        def _record(t, pending=pending, host=host, began=entry[1]):
            if pending.get(host, (None,))[0] is t:
                del pending[host]
            if t.cancelled():
                return
            error = t.exception()   # retrieved even when superseded: no "never retrieved"
            known = _KNOWN.get(host)
            if known is not None and known[1] > began:
                return      # a lookup started later (this one stuck, replaced) answered
            if error is None and t.result():
                _KNOWN[host] = (t.result(), began)
                _FAILED.pop(host, None)
            else:
                _FAILED[host] = time.monotonic()
        task.add_done_callback(_record)
        return entry

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        if _is_ip(host):
            return await self._inner.connect_tcp(host, port, timeout, local_address,
                                                 socket_options)
        started = time.monotonic()
        addresses, lookup = await self._addresses(host, timeout, started)
        try:
            return await self._connect(addresses, port, timeout, started, local_address,
                                       socket_options)
        except (httpcore.ConnectError, httpcore.ConnectTimeout):
            # Remembered addresses that no longer answer (the host moved while DNS was
            # slow): the lookup's own answer, waited for and tried with what is left of
            # the budget, if it differs.
            left = _left(timeout, started)
            if not lookup.done() and (left is None or left > 0):
                await asyncio.wait({lookup}, timeout=left)
            left = _left(timeout, started)
            if not lookup.done() or lookup.cancelled() or lookup.exception() is not None \
                    or not lookup.result() or lookup.result() == addresses \
                    or (left is not None and left <= 0):
                raise
            return await self._connect(lookup.result(), port, timeout, started,
                                       local_address, socket_options)

    async def _addresses(self, host, timeout, started) -> "tuple[list, asyncio.Task]":
        """The addresses to connect to, and the host's lookup they came from or stood
        in for."""
        lookup, since = self.lookup(host)
        known = _KNOWN.get(host)
        if known is not None and started - known[1] > MAX_AGE_S:
            known = None
        if known is None:
            # Nothing to fall back on: wait for the lookup inside the connect budget, as
            # the stock backend would have.
            # (A lookup cancelled under it -- only at loop teardown -- is CancelledError
            # here, as it is from the stock backend.)
            try:
                found = await asyncio.wait_for(asyncio.shield(lookup), _left(timeout, started))
            except asyncio.TimeoutError:
                raise httpcore.ConnectTimeout(f"DNS lookup for {host} timed out") from None
            except OSError as e:
                raise httpcore.ConnectError(str(e)) from e
            if not found:
                raise httpcore.ConnectError(f"DNS lookup for {host} returned no addresses")
            return found, lookup
        # A stall already known -- this lookup pending past FAST_S, or the host's last
        # one failing moments ago -- is not waited on again; nor is any for more than
        # half the connect budget left, so the remembered address has time to connect.
        wait = max(0.0, FAST_S - (started - since))
        failed_ago = started - _FAILED.get(host, float("-inf"))
        if failed_ago < FAILED_FOR_S:
            wait = 0.0
        left = _left(timeout, started)
        if left is not None:
            wait = min(wait, left / 2)
        if not lookup.done():
            await asyncio.wait({lookup}, timeout=wait)
        if lookup.done() and not lookup.cancelled() and lookup.exception() is None \
                and lookup.result():
            return lookup.result(), lookup
        if not lookup.done() and failed_ago < FAILED_FOR_S:
            why = f"failed {failed_ago:.0f}s ago and is not answering yet"
        elif not lookup.done():
            why = f"still unanswered after {time.monotonic() - since:.1f}s"
        elif lookup.cancelled():
            why = "cancelled"
        elif lookup.exception() is not None:
            why = f"failed ({type(lookup.exception()).__name__})"
        else:
            why = "returned no addresses"
        logger.info(f"DNS for {host} {why}: connecting to its last known address")
        return known[0], lookup

    async def _connect(self, addresses, port, timeout, started, local_address,
                       socket_options):
        order = _alternate(list(addresses))
        last = None
        for n, address in enumerate(order):
            left = _left(timeout, started)
            if left is not None and left <= 0:
                break
            # A share of what is left, so the next address still has time; the last one
            # gets all of it.
            share = left if left is None or n == len(order) - 1 else \
                min(left, max(MIN_ATTEMPT_S, left / (len(order) - n)))
            try:
                return await self._inner.connect_tcp(address, port, share, local_address,
                                                     socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        raise last or httpcore.ConnectTimeout("no address answered in the connect budget")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return await self._inner.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds):
        await self._inner.sleep(seconds)


def use_stale_dns(client: httpx.AsyncClient, base_url: str | None = None) -> httpx.AsyncClient:
    """Put StaleDNSBackend under a client's transports, and look base_url's host up now
    so a stall at the first request already has an answer to fall back on.

    The client's own transports are kept -- the default one and any env-proxy mounts, so
    a proxy's address is remembered too. httpx takes no network backend, so each pool's
    is set after the pool is built. A pool without that attribute (an httpx or httpcore
    that moved it) is logged and left on the stock backend: the brain must still answer,
    without the remembered addresses, rather than fail every session over an optional
    shortcut. test_llm_dns pins the attribute, so CI catches the move first."""
    backend = StaleDNSBackend()
    transports = [getattr(client, "_transport", None)] + list(getattr(client, "_mounts", {}).values())
    installed = 0
    for transport in transports:
        pool = getattr(transport, "_pool", None)
        if isinstance(pool, httpcore.AsyncConnectionPool) and hasattr(pool, "_network_backend"):
            pool._network_backend = backend
            installed += 1
        elif transport is not None:
            logger.error(f"llm_dns: {type(transport).__name__} has no connection pool with "
                         f"_network_backend; it keeps the stock DNS (no remembered addresses)")
    if installed and base_url:
        _prime(client, backend, httpx.URL(base_url))
    return client


def _prime(client, backend, url):
    """Look up now the host base_url's requests will connect to -- the model's, or the
    proxy's where an env proxy carries them -- unless a usable answer is remembered."""
    # raw_host: the ASCII (punycode) form httpcore connects with, the key the connect
    # path reads.
    host = url.raw_host.decode("ascii")
    transport_for = getattr(client, "_transport_for_url", None)
    pool = getattr(transport_for(url) if transport_for else None, "_pool", None)
    proxy_url = getattr(pool, "_proxy_url", None)
    if proxy_url is not None:
        host = proxy_url.host.decode("ascii")
    prime(host, backend)


def prime(host: str, backend: "StaleDNSBackend | None" = None) -> None:
    """Look host up now, in the background, unless a usable answer is remembered or no
    loop is running (the first connection looks it up then). The brain calls it at
    startup with the model's host (services.prime_model_dns)."""
    known = _KNOWN.get(host)
    if not host or _is_ip(host) or (known and time.monotonic() - known[1] <= MAX_AGE_S):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    (backend or StaleDNSBackend()).lookup(host)
