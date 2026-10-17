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
# the server is never told. Reproduced on a local h2 server, and live (#201):
#   * no RST_STREAM: the server goes on generating and sending a reply nobody reads --
#     on OpenRouter each cut reply ran on until the connection closed (13-265 tokens);
#   * the data it sends is never acknowledged, and drains the connection's flow-control
#     window: after ~16 MiB every stream on the connection crawls or stalls;
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
# generating when the caller's barge-in stopped the reply, not at their next turn. Live
# on OpenRouter, with it: cut replies stopped at 7-15 tokens, and 5 cuts and a request
# went over one connection.
#
# The wrap reads httpcore's private HTTP/2 internals; httpcore is pinned exactly
# (pyproject) and enable() checks the parts it uses before patching. If they have moved,
# it logs an ERROR and the model client stays on HTTP/1.1 -- slower to reconnect, never
# broken. test_llm_http2 fails once httpcore resets streams itself: then delete this.
#
import asyncio
import inspect

import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import httpcore
from loguru import logger

try:
    from httpcore._async.http2 import AsyncHTTP2Connection
except ImportError:  # pragma: no cover -- enable() reports it
    AsyncHTTP2Connection = None

# The frames' write bound: the request that closed is gone, so this stands in for its
# timeouts (httpcore reads the write timeout from a request's extensions).
_FLUSH_REQUEST = httpcore.Request("POST", "https://flush.invalid/",
                                  extensions={"timeout": {"write": 5.0}})

_original = None        # httpcore's own _response_closed, once wrapped
_enabled = None         # enable()'s answer, once asked
_flushes: set = set()   # the flush tasks running (a reference, so none is collected)


def _problem() -> str | None:
    """What of httpcore's HTTP/2 internals the wrap needs is missing, if anything."""
    if AsyncHTTP2Connection is None:
        return "httpcore._async.http2.AsyncHTTP2Connection is gone"
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
    for name in ("streams", "reset_stream", "acknowledge_received_data"):
        if not hasattr(conn._h2_state, name):
            return f"h2's H2Connection has no {name}"
    return None


def enable() -> bool:
    """Wrap httpcore's HTTP/2 stream close (once). True when the model client may use
    HTTP/2; False -- logged -- when httpcore has moved, and HTTP/1.1 is used instead."""
    global _original, _enabled
    if _enabled is None:
        problem = _problem()
        if problem is not None:
            logger.error(f"llm_http2: {problem}; the model's connection stays on HTTP/1.1 "
                         f"(a cancelled reply closes it, the next request reconnects)")
            _enabled = False
        else:
            _original = AsyncHTTP2Connection._response_closed
            AsyncHTTP2Connection._response_closed = _response_closed
            _enabled = True
    return _enabled


async def _response_closed(self, stream_id: int) -> None:
    try:
        unread = [e for e in self._events.get(stream_id, ())
                  if isinstance(e, h2.events.DataReceived)]
        stream = self._h2_state.streams.get(stream_id)
        reset = stream is not None and not stream.closed
        if unread or reset:
            for event in unread:
                self._h2_state.acknowledge_received_data(event.flow_controlled_length,
                                                         stream_id)
            if reset:
                self._h2_state.reset_stream(stream_id, h2.errors.ErrorCodes.CANCEL)
            task = asyncio.get_running_loop().create_task(_flush(self))
            _flushes.add(task)
            task.add_done_callback(_flushes.discard)
    except h2.exceptions.ProtocolError:
        pass            # the connection itself is closing: nothing left to tell it
    finally:
        # httpcore's own bookkeeping -- the stream's slot, its events -- whatever the above
        # did.
        await _original(self, stream_id)


async def _flush(conn) -> None:
    try:
        await conn._write_outgoing_data(_FLUSH_REQUEST)
    except Exception as e:  # noqa: BLE001 -- the connection's own error marks it unusable
        logger.debug(f"llm_http2: stream reset not sent ({type(e).__name__}: {e})")
