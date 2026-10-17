#
# llm_http2.py — HTTP/2 to the language model, with a cancelled stream reset.
#
# Our requests to the model are cancelled all the time: a speculative reply the caller's
# next words make wrong (speculate.py), a reply cut by a barge-in. Over HTTP/1.1 a
# response closed before its end closes its connection, so the next request pays a TCP
# and TLS handshake (65-100 ms from the box to openrouter.ai) and a DNS lookup (llm_dns),
# and a speculation and the real request need a connection each. Over HTTP/2 they all
# share one connection, and a cancelled request ends only its own stream (#201).
#
# httpcore's HTTP/2 (1.0.9, the latest release) does not end it, though: a response
# closed early is dropped on our side only (AsyncHTTP2Connection._response_closed), and
# the server is never told. Reproduced on a local h2 server (#201):
#   * no RST_STREAM: the server goes on generating and sending a reply nobody reads (the
#     model's tokens, billed);
#   * the data it sends is never acknowledged, and drains the connection's flow-control
#     window: after ~16 MiB every stream on the connection stalls;
#   * h2 still counts the stream open: at the 101st cancel, "Max outbound streams is 100,
#     100 open" fails every request on the connection.
# Upstream: encode/httpcore#943 (closed unfixed), #1022 (open), PR #1098 (the reset,
# unmerged since 2026-07).
#
# So _response_closed is wrapped here. For a stream closed before its end it first
#   * acknowledges the data that arrived for it and was never read -- even when h2 has
#     already seen the stream end (a short reply sent in full, cut before it was read),
#     whose data would otherwise keep the connection's window short for good;
#   * resets it (RST_STREAM, CANCEL) while h2 still counts it open: h2 then counts it
#     closed, and itself acknowledges whatever the server sent before the reset reached
#     it (h2's closed-stream handling);
# and the frames go out from a task of their own -- not from inside the shielded
# teardown httpcore runs _response_closed in, where taking the write lock can deadlock
# against a request holding it (upstream #1098's reason for not flushing). The next
# request or read would send them anyway; the task makes it now, so the server stops
# generating when the caller's barge-in stopped the reply, not at their next turn.
#
# One more gap against HTTP/1.1: httpcore drops an idle HTTP/1.1 connection whose socket
# has turned readable -- the server closed it -- before reusing it, but an HTTP/2 one only
# on its keepalive clock (has_expired). An idle connection the server closed without a
# GOAWAY would take the next request and fail it with "Server disconnected", and the
# SDK's retry would cost the caller its backoff (~0.5-1 s). (One closed with a GOAWAY is
# fine: httpcore reads it on the next request and moves that request to a new
# connection.) HTTP/1.1's rule -- idle and readable -- is wrong here, though: over HTTP/2
# an idle connection is readable whenever frames are waiting, and after every cut reply
# they are -- the stream's data already on its way when the reset went out. Live on the
# box (2026-10-16) that rule closed the connection at the next request after each cut,
# sometimes before the reset was flushed, and one cut reply ran to its end. So
# has_expired is wrapped to look closer, only when an idle connection is readable: a
# one-byte peek at the socket (a duplicate of its descriptor, so the event loop's own is
# untouched) that sees the end of the stream -- the server closed it -- and nothing
# else. Waiting bytes, TLS records of frames or of a GOAWAY, keep the connection.
#
# The wraps read httpcore's private HTTP/2 internals; httpcore is pinned exactly
# (pyproject) and enable() checks the parts it uses before patching. If they have moved,
# it logs an ERROR and the model client stays on HTTP/1.1 -- slower to reconnect, never
# broken. test_llm_http2 fails once httpcore resets streams itself: then delete this.
#
import asyncio
import inspect
import socket

import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import httpcore
from loguru import logger

try:
    from httpcore._async.http2 import AsyncHTTP2Connection, HTTPConnectionState
except ImportError:  # pragma: no cover -- enable() reports it
    AsyncHTTP2Connection = HTTPConnectionState = None

# The frames' write bound: the request that closed is gone, so this stands in for its
# timeouts (httpcore reads the write timeout from a request's extensions).
_FLUSH_REQUEST = httpcore.Request("POST", "https://flush.invalid/",
                                  extensions={"timeout": {"write": 5.0}})

_original = None        # httpcore's own _response_closed, once wrapped
_original_expired = None  # and its has_expired
_enabled = None         # enable()'s answer, once asked
_flushes: set = set()   # the flush tasks running (a reference, so none is collected)


def _problem() -> str | None:
    """What of httpcore's HTTP/2 internals the wrap needs is missing, if anything."""
    if AsyncHTTP2Connection is None:
        return "httpcore._async.http2.AsyncHTTP2Connection is gone"
    has_expired = getattr(AsyncHTTP2Connection, "has_expired", None)
    if not callable(has_expired) or inspect.iscoroutinefunction(has_expired) or \
            list(inspect.signature(has_expired).parameters) != ["self"]:
        return "AsyncHTTP2Connection.has_expired is not has_expired(self)"
    for name, params in (("_response_closed", ["self", "stream_id"]),
                         ("_write_outgoing_data", ["self", "request"])):
        fn = getattr(AsyncHTTP2Connection, name, None)
        if not inspect.iscoroutinefunction(fn):
            return f"AsyncHTTP2Connection.{name} is not a coroutine function"
        if list(inspect.signature(fn).parameters) != params:
            return f"AsyncHTTP2Connection.{name} takes {list(inspect.signature(fn).parameters)}"
    conn = AsyncHTTP2Connection(origin=httpcore.Origin(b"https", b"check.invalid", 443),
                                stream=None)
    if not isinstance(getattr(conn, "_h2_state", None), h2.connection.H2Connection):
        return "AsyncHTTP2Connection._h2_state is not an h2 H2Connection"
    if not isinstance(getattr(conn, "_events", None), dict):
        return "AsyncHTTP2Connection._events is not a dict"
    if getattr(conn, "_state", None) is not HTTPConnectionState.IDLE or \
            not hasattr(conn, "_network_stream"):
        return "AsyncHTTP2Connection has no _state / _network_stream"
    for name in ("streams", "reset_stream", "acknowledge_received_data"):
        if not hasattr(conn._h2_state, name):
            return f"h2's H2Connection has no {name}"
    return None


def enable() -> bool:
    """Wrap httpcore's HTTP/2 stream close and expiry (once). True when the model client may use
    HTTP/2; False -- logged -- when httpcore has moved, and HTTP/1.1 is used instead."""
    global _original, _original_expired, _enabled
    if _enabled is None:
        problem = _problem()
        if problem is not None:
            logger.error(f"llm_http2: {problem}; the model's connection stays on HTTP/1.1 "
                         f"(a cancelled reply closes it, the next request reconnects)")
            _enabled = False
        else:
            _original = AsyncHTTP2Connection._response_closed
            _original_expired = AsyncHTTP2Connection.has_expired
            AsyncHTTP2Connection._response_closed = _response_closed
            AsyncHTTP2Connection.has_expired = _has_expired
            _enabled = True
    return _enabled


async def _response_closed(self, stream_id: int) -> None:
    unread = [e for e in self._events.get(stream_id, ())
              if isinstance(e, h2.events.DataReceived)]
    stream = self._h2_state.streams.get(stream_id)
    reset = stream is not None and not stream.closed
    if unread or reset:
        try:
            for event in unread:
                self._h2_state.acknowledge_received_data(event.flow_controlled_length,
                                                         stream_id)
            if reset:
                self._h2_state.reset_stream(stream_id, h2.errors.ErrorCodes.CANCEL)
        except h2.exceptions.ProtocolError:
            pass        # the connection itself is closing: nothing left to tell it
        else:
            task = asyncio.get_running_loop().create_task(_flush(self))
            _flushes.add(task)
            task.add_done_callback(_flushes.discard)
    await _original(self, stream_id)


def _has_expired(self) -> bool:
    if _original_expired(self):
        return True
    if self._state != HTTPConnectionState.IDLE or \
            not self._network_stream.get_extra_info("is_readable"):
        return False
    return _closed_by_peer(self._network_stream.get_extra_info("socket"))


def _closed_by_peer(sock) -> bool:
    """Has the server closed this readable socket -- the end of the stream, and no bytes
    before it? A socket that cannot be looked at counts as closed (HTTP/1.1's rule)."""
    try:
        fd = sock.fileno()
        if fd < 0:
            return True
        with socket.fromfd(fd, sock.family, sock.type) as peek:
            return peek.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except BlockingIOError:
        return False        # readable a moment ago, drained since: open
    except (OSError, AttributeError):
        return True


async def _flush(conn) -> None:
    try:
        await conn._write_outgoing_data(_FLUSH_REQUEST)
    except Exception as e:  # noqa: BLE001 -- the connection's own error marks it unusable
        logger.debug(f"llm_http2: stream reset not sent ({type(e).__name__}: {e})")
