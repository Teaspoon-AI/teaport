#
# teaport — shared service factories (LLM / TTS / STT).
#
# The appliance brain (gateway_server.py) builds its LLM/TTS/STT pipeline from these
# factories. Deliberately free of transport/demo imports — it must stay cheap to
# import for the appliance.
#
# The LLM endpoint + key are read from the environment AT CALL TIME (or passed
# explicitly), never at import time — the import-order trap this module replaced
# must not grow back.
#

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass
from typing import Sequence

import httpcore
import httpx
from loguru import logger
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from pipecat.frames.frames import (
    FunctionCallFromLLM,
    LLMContextFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    SystemFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.openai.llm import OpenAILLMService

from teaport_brain import barge_pause, llm_http2
from teaport_brain import owed as owed_mod
from teaport_brain.followup_gate import RunFrame, RunSilentFrame
from teaport_brain.llm_dns import prime, use_stale_dns
from teaport_brain.settings import secret_file, setting
from teaport_brain.stt import TeaportSTTService
from teaport_brain.tts_text import has_speech

TEAPORT_URL = setting("TEAPORT_URL")


# gpt-oss reasoning effort. gpt-oss emits hundreds of hidden chain-of-thought
# chars before the first SPOKEN token; "low" cuts that ~10x (first-spoken-token
# 0.18-0.34s vs 0.5s+) with no quality loss on short replies. Sent as a top-level
# `extra` kwarg; set LLM_REASONING_EFFORT="" for models that don't support it.
# LLM_EXTRA_BODY (JSON) rides `extra_body` for provider-specific routing — e.g.
# OpenRouter's {"provider": {"order": ["Groq"], "allow_fallbacks": true}}.
def _reasoning_effort() -> str:
    return setting("LLM_REASONING_EFFORT")


def _llm_extra() -> dict:
    extra: dict = {}
    effort = _reasoning_effort()
    if effort:
        extra["reasoning_effort"] = effort
    # setting(), not a bare json.loads: this runs inside build_agent_session(), so a
    # malformed hand-edit (or a wrapper that `source`d brain.env and stripped the
    # quotes) took down EVERY session with a raw JSONDecodeError while the service
    # itself looked healthy. See settings.py.
    body = setting("LLM_EXTRA_BODY")
    if body is not None:
        extra["extra_body"] = body
    return extra


def get_llm_api_key() -> str:
    # LLM_API_KEY wins; else the installer-written key file. A local
    # OpenAI-compatible server that ignores auth still needs a placeholder —
    # set LLM_API_KEY=sk-local (or write it to the file).
    key = setting("LLM_API_KEY") or secret_file("LLM_API_KEY")
    if key:
        return key
    raise RuntimeError(
        "no LLM API key — set LLM_API_KEY or write ~/.config/teaport/llm_key "
        "(see docs/CONFIG.md)"
    )


# Completion cap. Left unset, a credit-metered gateway reserves against the MODEL'S
# ceiling rather than the reply's actual size — 65536 tokens for gpt-oss-120b — and
# refuses the whole request once the key's remaining balance cannot cover that
# reservation, however short the answer would have been:
#
#   Error code: 402 - This request requires more credits, or fewer max_tokens.
#   You requested up to 65536 tokens, but can only afford 55187.
#
# Live on 2026-08-18: nine turns failed that way with $7.75 still on the key, and the
# only symptom in the room was the assistant going quiet — the ErrorFrame reaches the
# journal, not the caller. A voice reply is two sentences; reserving 64K for it is what
# turned a funded key into a dead assistant.
#
# The cap is sized for the reasoning effort ACTUALLY IN FORCE. gpt-oss bills hidden
# chain-of-thought as completion tokens, and LLM_REASONING_EFFORT is an independent
# knob an operator is invited to turn (docs/CONFIG.md) — so a cap sized for "low"
# starves "high": the reasoning alone exhausts the budget, the completion comes back
# finish_reason=length with zero visible content, no LLMTextFrame is ever pushed, and
# the room hears dead air. That is the SAME silent assistant this cap exists to
# prevent, arrived at from the other side. Hence a table, not a constant.
#
# The cap also bounds a degenerate collapse (2340 characters in one live completion),
# but LLMTextGuard does the fine-grained containment; this only bounds the worst case.
#
# It applies to tool-call arguments too, which is the other reason for the headroom: a
# truncated arguments stream is malformed JSON, and pipecat raises on it inside
# _process_context, so the whole turn errors out and the function never runs. A long
# openclaw_agent_consult request is the realistic case.
#
# "" is deliberately NOT in this table. LLM_REASONING_EFFORT="" is a configuration
# docs/CONFIG.md invites ("for models that don't support it"), and _llm_extra() then omits
# reasoning_effort from the request entirely — so the model runs at ITS OWN default, which
# is typically medium. Mapping "" to the 1024-token low budget billed a medium-effort
# completion against the cheapest cap and produced exactly the failure described above:
# finish_reason=length, zero visible content, dead air. Falling through to the
# unknown-effort headroom is correct, because the effort in force is unknown by
# construction.
_MAX_TOKENS_BY_EFFORT = {"low": 1024, "medium": 3072, "high": 8192}
_MAX_TOKENS_UNKNOWN_EFFORT = 4096


def _default_max_tokens() -> int:
    return _MAX_TOKENS_BY_EFFORT.get(_reasoning_effort(), _MAX_TOKENS_UNKNOWN_EFFORT)


# Parsed defensively: this value lives in brain.env, which installer repairs preserve
# verbatim, so a bare int() would turn one operator typo into an import-time crash-loop
# that re-running the installer cannot clear (setting() is that defensive parse).
def _max_tokens() -> int | None:
    """Completion cap, or None for no cap at all.

    LLM_MAX_TOKENS=0 means uncapped — the pre-2026-08-18 behaviour. The cap exists to
    stop a credit-metered gateway reserving against the model's 65536-token ceiling;
    an operator on a local llama.cpp server or an un-metered endpoint has no such
    gateway, and 0 is the value that most obviously reads as "no cap", so it must not
    quietly mean 1024."""
    default = _default_max_tokens()
    value = setting("LLM_MAX_TOKENS", default=default)
    if value == 0:
        logger.info("LLM_MAX_TOKENS=0 — completion cap disabled (no max_tokens sent)")
        return None
    if value < 0:
        logger.warning(f"LLM_MAX_TOKENS={value} is negative; using {default}")
        return default
    return value


# Read timeout. The OpenAI SDK defaults to Timeout(connect=5, read=600, ...) — TEN
# MINUTES of a voice turn hanging on a stalled connection, with no error, no log line
# and no frame. Nothing else in the pipeline reports a slow completion either
# ("Generating chat from context" prints BEFORE the HTTP call), so a stall is
# indistinguishable from silence and the session simply dies.
#
# Measured live 2026-08-19: a turn was triggered at 12:06:25, nothing came back, and the
# operator tore the session down 16 seconds later because the assistant had stopped
# answering. The same request replayed in 2.8s — a transient, not a bad request, and it
# would have hung for ten more minutes. Three "the bot stopped responding" reports across
# three models are all consistent with this one mechanism.
#
# A voice turn is already lost by ~20s, so waiting longer buys nothing and costs the
# session. Raise LLM_TIMEOUT_SECS for a slow local llama.cpp box.
#
# PER ATTEMPT, not per turn. retry_on_timeout below buys one retry, and base_llm re-issues
# it "without a timeout so we get a response" — but the httpx client timeout set in
# create_client still applies to that second attempt, so the worst case before an
# ErrorFrame appears is ~2x this value plus connect, not 1x. That is a deliberate trade:
# the 2026-08-19 stall replayed successfully in 2.8s, so the retry most likely rescues the
# turn outright rather than merely failing faster. Size the knob for one attempt and
# expect up to two.
# pipecat HAS a request timeout — and ships it disabled. base_llm only wraps the call in
# asyncio.wait_for `if self._retry_on_timeout`, which defaults to False, so by default
# nothing bounds the request and it falls through to the SDK's 600s read timeout. Turning
# it on also buys the retry: the stalled turn above replayed successfully in 2.8s, so one
# retry would most likely have rescued it outright rather than merely failing faster.
#
# That wait_for bounds only the OPENING of the stream, so it does not cover a server
# that accepts the request and then stalls mid-response. Bounding the transport itself
# is what covers that, and it has to be set on the httpx client — see
# BoundedOpenAILLMService for why an AsyncOpenAI-level timeout is dropped on the floor.
# This value feeds both.
def _llm_timeout_secs() -> float:
    secs = setting("LLM_TIMEOUT_SECS")
    if secs <= 0:
        logger.warning(f"LLM_TIMEOUT_SECS={secs} is not positive; using 20")
        return 20.0
    return secs


class _InterceptHandler(logging.Handler):
    """Forward a stdlib LogRecord to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())


def _intercept_stdlib_logging() -> None:
    """Make httpx and the OpenAI SDK visible in the journal.

    Everything this brain logs goes through loguru, and loguru does not see stdlib
    logging unless it is explicitly routed. httpx and the OpenAI SDK both use stdlib
    logging, so the entire transport layer was silent: not one "HTTP Request: POST" line
    in a whole session.

    That silence hid the two ways a turn can issue more than one request. The SDK retries
    on its own — create_client never sets max_retries, so the default of 2 applies — and
    pipecat re-issues the call itself when retry_on_timeout fires. Neither writes a line
    this process was capturing, so "one Generating chat" was being read as "one request"
    when it only ever meant "one call into pipecat".

    httpx logs each request at INFO, and the SDK logs "Retrying request..." at INFO, so
    INFO is enough to count the requests a turn actually made.
    """
    for name in ("httpx", "openai"):
        stdlib = logging.getLogger(name)
        stdlib.handlers = [_InterceptHandler()]
        stdlib.setLevel(logging.INFO)
        stdlib.propagate = False


async def _watch_completion_identity(stream):
    """Log the completion id carried on every chunk, and WARN if it changes mid-stream.

    A router (OpenRouter here) can fail over mid-response and regenerate on a second
    backend, streaming the whole answer again down the SAME SSE connection. From inside
    the process that is indistinguishable from one generation: one `create()` call, one
    stream object, one LLMFullResponseStart/End pair. The only thing that separates the
    two cases is the per-chunk completion id, which is why it is worth a log line.

    Live 2026-08-26 08:32 and 08:48: the assistant spoke its reply twice. The second copy
    at 08:48 had INDEPENDENTLY RE-SAMPLED wording — "four thousand five hundred eight"
    became "four five zero eight" — so it was generated, not replayed by anything of ours.
    Whether the provider generated it twice within one completion, or a router concatenated
    two completions, was not decidable from the logs we had. Same id across both copies
    means the model repeated itself; a changed id means the request was effectively served
    twice. Nothing below our service layer was observable at all: httpx and the OpenAI SDK
    log through stdlib logging, which this process never routed into loguru (see
    _intercept_stdlib_logging).
    """
    first = None
    changes = 0
    async for chunk in stream:
        cid = getattr(chunk, "id", None)
        if cid and first is None:
            first = cid
            logger.debug(f"completion id={cid} "
                         f"provider={getattr(chunk, 'provider', None)!r}")
        elif cid and cid != first:
            changes += 1
            if changes == 1:
                logger.warning(
                    f"completion id CHANGED mid-stream: {first} -> {cid} — the response "
                    f"was served by more than one generation, so any duplicated text is a "
                    f"router retry, not the model repeating itself")
            first = cid
        yield chunk


async def _sequential_tool_call_indices(stream):
    """Renumber tool_call.index to the order calls first appear in the response.

    base_llm coalesces streamed tool-call deltas on the assumption that
    `tool_call.index` counts calls WITHIN the response — 0 for the first, 1 for the
    second. It starts at func_idx=0 and treats any other index as "a new call started",
    flushing the accumulator and resetting function_name. At the end it gates the ENTIRE
    dispatch on that last function_name being non-empty.

    This endpoint numbers them by position in the TOOLS ARRAY instead. `remember` is the
    sixth of nine tools, so its deltas arrive as index=5, every one of them trips the
    "new call" branch, function_name ends empty and run_function_calls() is never
    reached — the parsed call is left sitting in functions_list and silently discarded.
    No error, no warning, no log line: the model asked for a tool and the pipeline just
    did not run it. Reproduced 3/3 on 2026-08-20; a tool at array index 0 would work and
    every other tool would not, which is the shape of the openclaw_agent_consult drops that
    went unexplained for hours.

    Mapping by order of first appearance is identity for a provider that already numbers
    calls 0,1,2, so this costs nothing when it is not needed.

    Keyed on tool_call.id, NOT on index alone. Two calls to the SAME tool in one response
    share an index under this numbering — "remember X and Y" sends both as index=5 — and
    mapping index->ordinal collapsed them into one call: base_llm's "new call" branch
    never fired, so it concatenated them into function_name="rememberremember" with
    arguments='{"fact":"X"}{"fact":"Y"}', whose json.loads raises inside _process_context
    and errors out the whole turn. Ids are unique per call, so they separate the two;
    continuation deltas carry no id and inherit whichever call is currently open at that
    index. A provider that sends no ids at all falls back to the old index ordering.
    """
    by_id: dict = {}      # tool_call.id -> ordinal
    open_at: dict = {}    # raw index -> ordinal currently accumulating there
    issued = 0            # ordinals handed out so far
    async for chunk in stream:
        for choice in (chunk.choices or []):
            for tool_call in (getattr(getattr(choice, "delta", None), "tool_calls", None) or []):
                tid = getattr(tool_call, "id", None)
                if tid:
                    if tid not in by_id:
                        by_id[tid] = issued
                        issued += 1
                    open_at[tool_call.index] = by_id[tid]
                elif tool_call.index not in open_at:
                    open_at[tool_call.index] = issued
                    issued += 1
                tool_call.index = open_at[tool_call.index]
        yield chunk


class _RenumberedStream:
    """The renumbering iterator, wrapped so base_llm can still CLOSE the real stream.

    base_llm tears the completion stream down with:

        chunk_iter = stream.__aiter__()
        ...
        if hasattr(chunk_iter, "aclose"): await chunk_iter.aclose()
        if hasattr(stream, "close"):      await stream.close()
        elif hasattr(stream, "aclose"):   await stream.aclose()

    under a comment saying it exists "to prevent socket leaks and uvloop crashes ...
    preventing uvloop's broken asyncgen finalizer from firing on Python 3.12+
    (MagicStack/uvloop#699)".

    openai.AsyncStream has close() — which does `await self.response.aclose()`, the call
    that actually releases the HTTP connection — and NO aclose(). A bare async generator
    is the exact reverse. So returning the generator from get_chat_completions sent
    base_llm down the `elif aclose` branch, where it closed the WRAPPER and left the real
    AsyncStream to the garbage collector: one leaked httpx response per interrupted
    completion, and in a voice pipeline every barge-in interrupts one. A connection with a
    response still open is never idle, so the keepalive timer below never reclaims it.

    A small class rather than a generator keeps base_llm on the branch it intends: the
    iterator still acloses first (cascading cleanup through httpx's nested generators),
    then close() reaches the SDK stream.
    """

    __slots__ = ("_stream", "_iter")

    def __init__(self, stream):
        self._stream = stream
        self._iter = _sequential_tool_call_indices(_watch_completion_identity(stream))

    def __aiter__(self):
        return self._iter

    async def close(self):
        await self._stream.close()


class _NothingToSay:
    """The stream of a run whose records were all heard by the time it rendered: no
    request is sent, and the completion ends empty (owed.Owed.view; Owed.tla RunGo)."""

    def __aiter__(self):
        return self._none()

    @staticmethod
    async def _none():
        return
        yield  # noqa: unreachable -- makes this an async generator

    async def close(self):
        pass


# The request the LLM is answering now, in the task answering it (process_frame): an
# owed.Reply. A speculation's request runs in a task of its own and reads None. Set
# around the WHOLE process_frame: a task spawned inside it (a function call's) inherits
# this Reply, stale once the completion is over -- read it only on the completion's path.
_REPLY: contextvars.ContextVar = contextvars.ContextVar("teaport_owed_reply", default=None)
# In a speculation's task (speculate._Speculation.run): the list its render writes the
# record keys its note carried into, for the commit that adopts its stream
# (speculate._AdoptedStream.owed_noted; formal/Owed.tla, adoptDropsKeys).
SPECULATION_NOTED: contextvars.ContextVar = contextvars.ContextVar(
    "teaport_speculation_noted", default=None)


# The judges' completion budget: one word of answer, after the reasoning a reasoning
# model spends first (gpt-oss-120b: the 241-cut and the gist measurements ran at this
# budget, 0 unparsed).
_JUDGE_MAX_TOKENS = 512


# Connect timeout. Separate from the read timeout below because they fail differently:
# an unreachable endpoint should fail fast, a slow-but-alive one should not.
_CONNECT_SECS = 5.0

# How long an idle connection to the model is kept. OpenRouter closes an idle HTTP/2
# connection somewhere between 5 and 10 minutes (open at 300 s, closed by 600 s, measured
# 2026-10-16); retired before that, the connection is never found closed by a request,
# and the next turn's warm-up (warm_connection) opens its successor while the caller
# speaks. (A close found by a request costs nothing more than a reconnect -- httpcore
# reads the server's GOAWAY and moves the request -- but on the turn's own time.)
_KEEPALIVE_SECS = 240.0


# One HTTP client for the model per event loop, shared by every session on it (#201). Over
# HTTP/2 one connection carries them all -- a phone call's greeting, a room session's
# turns, a speculation beside the real request -- so a session starts on a connection
# the last one left open instead of a lookup and a handshake of its own (65-100 ms from
# the box), and each session no longer leaves a client behind that nothing closes
# (pipecat never closes its own). Each session's requests carry its own timeout
# (AsyncOpenAI's, which the SDK sets on every request: create_client) -- with one
# coupling: streams sharing a connection share its reads, and httpcore fails them all
# when one's read times out. A silent connection is a silent connection for every stream
# on it, so that only moves the failure to the shortest bound among them; the warm-up's
# is the turn's own for that reason. Per loop because a connection belongs to the loop
# that opened it; a closed loop's client is dropped (its sockets with it) the next time
# one is asked for. Without a running loop (a test building a service synchronously)
# each call gets a client of its own.
_HTTP_CLIENTS: dict = {}    # loop -> client


def _model_http_client(base_url) -> httpx.AsyncClient:
    for closed in [loop for loop in _HTTP_CLIENTS if loop.is_closed()]:
        del _HTTP_CLIENTS[closed]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    client = _HTTP_CLIENTS.get(loop) if loop is not None else None
    if client is None:
        # The model's last known address, used when a lookup stalls (llm_dns). HTTP/2
        # where the server offers it: a cancelled reply ends its stream, not the
        # connection, and a speculation shares it with the real request (llm_http2).
        client = use_stale_dns(DefaultAsyncHttpxClient(
            http2=llm_http2.enable(),
            timeout=httpx.Timeout(_llm_timeout_secs(), connect=_CONNECT_SECS),
            limits=httpx.Limits(
                max_keepalive_connections=100, max_connections=1000,
                keepalive_expiry=_KEEPALIVE_SECS,
            ),
        ), base_url)
        if loop is not None:
            _HTTP_CLIENTS[loop] = client
    return client


def _has_live_connection(client: httpx.AsyncClient, url) -> bool:
    """Is a connection to url's server open in client's pool, and able to take a request
    now? (One with a stream running counts, over HTTP/2.) Read from the pool httpx uses
    for url (a proxy's, where one is configured); a pool that cannot be read counts as
    yes: no warm-up is better than a guess."""
    url = httpx.URL(str(url))
    transport_for = getattr(client, "_transport_for_url", None)
    pool = getattr(transport_for(url) if transport_for else None, "_pool", None)
    if not isinstance(pool, httpcore.AsyncConnectionPool):
        return True
    origin = httpcore.Origin(url.raw_scheme, url.raw_host,
                             url.port or (443 if url.scheme == "https" else 80))
    return any(c.can_handle_request(origin) and c.is_available() and not c.has_expired()
               for c in pool.connections)


@dataclass
class FunctionCallsDispatchedFrame(SystemFrame):
    """Broadcast for EVERY function call a completion makes, before any of them runs.

    pipecat's own FunctionCallsStartedFrame — which llm_text_guard listens for to know
    a tool call is the reply — deliberately excludes the built-in cancel_<tool> call
    from its broadcast (run_function_calls filters it out of user_visible_calls, since
    it is an internal mechanism, not a user-facing one). A completion whose ONLY call
    is a silent cancel therefore never sets that flag, and if it also streamed some
    leading punctuation, the guard mistakes the cancel for a vanished reply and speaks
    an apology over it. See BoundedOpenAILLMService.run_function_calls.

    Not a fix applied at the guard: FunctionCallInProgressFrame — broadcast for every
    call including the cancel tool — arrives too late to help, since base_llm schedules
    each call with create_task() and never awaits it before its own `finally` pushes
    LLMFullResponseEndFrame; verified that such a task gets no turn to run until the
    synchronous push_frame chain from that `finally` already suspends, which it doesn't
    by the time it reaches the guard. This frame closes the gap instead, by broadcasting
    synchronously — awaited in full — before any call, visible or not, is scheduled.
    """

    function_calls: Sequence[FunctionCallFromLLM]


class BoundedOpenAILLMService(OpenAILLMService):
    """OpenAILLMService whose timeout actually reaches the socket.

    retry_on_timeout only wraps `chat.completions.create()`, and every request here is
    a STREAM, so that call returns the moment the response headers land. It bounds
    opening the stream and nothing else. A server that accepts the request and then
    stalls mid-stream has already passed that gate, so the wait_for never fires, the
    retry never happens, and consuming the stream falls through to the OpenAI SDK's
    default read timeout of 600 seconds. Measured live 2026-08-20: a turn committed at
    19:12:23.6, the request was sent, and 150+ seconds later there was still no
    response, no error and no log line -- the assistant simply stopped answering.

    Passing `timeout=` to OpenAILLMService does not fix it. create_client() accepts
    **kwargs and then drops them, and it supplies its own DefaultAsyncHttpxClient, so
    an AsyncOpenAI-level timeout leaves the transport at read=600 (verified on the
    box: sdk=20.0 while httpx still reported read=600). Overriding create_client is
    the only seam that sets the value httpx actually enforces.

    With the transport bounded, a mid-stream stall raises httpx.ReadTimeout, which
    base_llm already catches as httpx.TimeoutException -- it fires on_completion_timeout,
    pushes an error and closes the turn, so LLMErrorSpeaker can say something out loud.
    That path was always wired; nothing ever armed it. It also makes the untimed retry
    inside get_chat_completions safe, since httpx now bounds that attempt too.

    The limits are upstream's, repeated here because create_client builds the client in
    one expression and there is no seam to inject only a timeout.
    """

    # A cancelled completion is the one failure that leaves NO trace at all. base_llm
    # wraps _process_context in `except Exception`, and CancelledError is a
    # BaseException, so it is not caught, not logged, and not turned into an error
    # frame -- the `finally` still pushes LLMFullResponseEndFrame, so the turn closes
    # with no text and the room just goes quiet. Live on 2026-08-20: a turn committed
    # at 19:25:55.55, "Generating chat from context" was logged at 19:25:55.56, and
    # then the journal had nothing further at all. The event loop was idle and the
    # process held no connection to the endpoint, so the request was not hung -- it
    # was gone. Working that out took a stack dump and a context replay; it should
    # take one log line.
    async def _process_context(self, context):
        started = time.monotonic()
        try:
            await super()._process_context(context)
            await self._run_said_nothing()
        except asyncio.CancelledError:
            logger.warning(
                f"{self}: completion CANCELLED after {time.monotonic() - started:.1f}s "
                "— this turn produces no reply and raises no error"
            )
            reply = _REPLY.get()
            if reply is not None and not reply.claimed and self.owed is not None:
                # Cancelled before the ledger saw it start (its Start frame flushed with
                # the interruption): nothing of it was heard. One the ledger has is
                # settled there, by its own audio.
                self.owed.settle(reply, None, cut=True, audio=False)
            raise
        except Exception:
            await self._run_said_nothing()
            raise
        else:
            # The success case is logged too, because "the bot said nothing" has three
            # very different causes and the journal could not tell them apart: the
            # completion never returned (no line), it returned and the reply went
            # missing downstream (this line, then silence), or it returned a tool call
            # that never dispatched (this line, then no "Calling function"). One line
            # splits all three.
            logger.info(f"{self}: completion finished in {time.monotonic() - started:.1f}s")

    async def _run_said_nothing(self):
        """A run of ours that said nothing and called nothing (stay_silent alone, empty,
        failed before its first text): its End will be held at the TTS
        (test_pipecat_premises.py), so the gate is told the turn is over
        (RunSilentFrame). Not with a call out: that is a tool round the gate keeps."""
        reply = _REPLY.get()
        if (reply is not None and reply.kind in owed_mod.RUNS and not reply.texts
                and not reply.calls):
            await self.push_frame(RunSilentFrame())

    async def run_function_calls(self, function_calls):
        """Broadcast FunctionCallsDispatchedFrame for every call, then dispatch as usual.

        See the frame's docstring for why: base_llm's own FunctionCallsStartedFrame
        excludes the cancel_<tool> call, so a cancel-only completion never tells
        llm_text_guard a call happened. This broadcasts BEFORE calling super(), so it
        reaches the guard well ahead of the LLMFullResponseEndFrame _process_context's
        `finally` pushes right after this method returns — unlike FunctionCallInProgressFrame,
        which base_llm broadcasts from inside a task it schedules and never awaits.
        """
        reply = _REPLY.get()
        if reply is not None and reply.kind == owed_mod.REOFFER:
            # The re-offer's stay_silent (owed.STAY_SILENT): consumed HERE, so neither the
            # call nor a result enters the context -- the run said nothing, which is all
            # it means. The broadcast still tells the guard a call was the answer, so a
            # stray "..." beside it is not spoken as a vanished reply.
            silent = [c for c in function_calls if c.function_name == owed_mod.STAY_SILENT]
            if silent:
                logger.info(f"{self}: the re-offer called stay_silent -- saying nothing")
                function_calls = [c for c in function_calls if c not in silent]
                if not function_calls:
                    await self.broadcast_frame(FunctionCallsDispatchedFrame,
                                               function_calls=silent)
                    return
        if function_calls:
            if reply is not None:
                reply.calls += len(function_calls)
                if self.owed is not None:
                    self.owed.called(reply, [c.tool_call_id for c in function_calls])
            await self.broadcast_frame(
                FunctionCallsDispatchedFrame, function_calls=function_calls
            )
        await super().run_function_calls(function_calls)

    # A speculate.Speculator when TEAPORT_SPECULATIVE_REPLY is on (agent_session sets it);
    # None otherwise, and get_chat_completions is then exactly _open_stream.
    speculator = None
    # The session's owed records (owed.Owed; agent_session sets it): the render hook in
    # _open_stream asks it for each request's view. None: requests go as they are.
    owed = None

    async def process_frame(self, frame, direction):
        """Every request the LLM answers gets an owed.Reply: what it is for (a caller's
        commit, a post-tool answer pushed up, a run of ours), carried on its
        LLMFullResponseStartFrame to the ledger, which settles it at the reply's end.
        Whether it carries a sync tool's answer is read from its context at the render
        (owed.Owed.noted: the book's fresh answers), not from the frame's direction (#204)."""
        if not isinstance(frame, LLMContextFrame) or self.owed is None:
            await super().process_frame(frame, direction)
            return
        if isinstance(frame, RunFrame):
            reply = owed_mod.Reply(frame.purpose, frame.keys)
        else:
            reply = owed_mod.Reply(owed_mod.COMMIT if direction == FrameDirection.DOWNSTREAM
                                   else owed_mod.POST)
        token = _REPLY.set(reply)
        try:
            await super().process_frame(frame, direction)
        finally:
            _REPLY.reset(token)

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        reply = _REPLY.get()
        if reply is not None and direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                # The ledger reads it at the TTS's sighting, and settles the reply by it.
                frame.owed_reply = reply
            elif isinstance(frame, LLMTextFrame) and has_speech(frame.text):
                # The TTS's own test: text it would speak. A reply of zero-width spaces
                # (#70) is none, and its run is silent (RunSilentFrame).
                reply.texts += 1
        await super().push_frame(frame, direction)

    def _rendered(self, reply, noted, context) -> None:
        """`reply`'s request is built from `context`: what its note carried is evidence
        when it ends, and so is a fresh answer in it; and a commit is no longer one still
        to be rendered -- told only now, after the view (which waits for the settles), so
        a consult landing meanwhile still finds it pending and leaves the note to it
        (Owed.tla Land)."""
        self.owed.noted(reply, noted, context.get_messages())
        if reply is not None and reply.kind == owed_mod.COMMIT and self.owed.gate is not None:
            self.owed.gate.request_rendered()

    async def get_chat_completions(self, context):
        reply = _REPLY.get()
        # A speculation opened for this very context, if there is one, is the stream --
        # its buffered chunks replay first, then it continues live. The request-building
        # path below is what the speculation itself went through, so the two are the
        # same request either way; only WHEN it was opened differs. A run of ours is
        # never one: no speculation lives outside a caller's turn.
        if self.speculator is not None and (reply is None or reply.kind not in owed_mod.RUNS):
            adopted = await self.speculator.take(context)
            if adopted is not None:
                if self.owed is not None:
                    self._rendered(reply, adopted.owed_noted, context)
                return adopted
        return await self._open_stream(context)

    # Paired with _process_context's CANCELLED line: this marks the boundary between
    # "the request never got a response" and "the response arrived but consuming it
    # stalled". Without it both look identical from the journal — a "Generating chat
    # from context" line and nothing after it. Both the ordinary request and the
    # speculation open their stream here.
    #
    # The stream is also renumbered on the way past — see _sequential_tool_call_indices.
    #
    # THE RENDER HOOK (#203, owed.py). The request goes out as the view owed.Owed.view
    # builds: the context with the note for every owed record -- before the caller's last
    # words, or at the tail with the run line for a run of ours. HERE, not in
    # get_chat_completions: the speculative reply opens its stream through this method
    # directly (speculate.py), and an adopted speculation rendered anywhere else would go
    # out bare (formal/Owed.tla, ow_renderInGet). After HeardContextCorrector's reconcile
    # (a commit's frame passes it; a speculation runs it before its snapshot), so the
    # note is rendered on the context as heard. The view goes to super(), so pipecat's
    # "Generating chat from context" line logs exactly what the model saw.
    async def _open_stream(self, context):
        reply = _REPLY.get()
        noted = ()
        if self.owed is not None:
            kind = reply.kind if reply is not None else "caller"
            view, noted = await self.owed.view(context, kind,
                                               reply.keys if reply is not None else ())
            if view is None:
                logger.info(f"{self}: the {kind} has nothing owed to tell -- no request")
                return _NothingToSay()
            self._rendered(reply, noted, context)
            context = view
            if reply is None and (into := SPECULATION_NOTED.get()) is not None:
                into[:] = noted
        stream = await super().get_chat_completions(context)
        logger.debug(f"{self}: response stream open")
        # _RenumberedStream, not the bare generator — see the class docstring: a
        # generator has no close(), so wrapping in one silently disabled base_llm's
        # socket release.
        return _RenumberedStream(stream)

    async def judge_cut(self, question: str, heard: str, rest: str):
        """The cut judge owed.Owed asks (Owed._judge): whether a reply cut while it played
        left anything the caller still needs. True for HEARD, False for OWED, None for
        anything else. The caller bounds the time."""
        return await self._judge(owed_mod.cut_judge_messages(question, heard, rest))

    async def judge_gist(self, question: str, outcome: str, heard: str):
        """The gist judge owed.Owed asks (Owed._gist, #210): whether what the caller heard
        of a record gave them the answer to their question. As judge_cut."""
        return await self._judge(owed_mod.gist_judge_messages(question, outcome, heard))

    async def judge_decline(self, question: str, heard: str, rest: str, words: str):
        """The decline check owed.Owed asks (Owed._ask_decline, #231): whether the caller,
        cutting a telling of an owed answer, still wants the rest. True for DECLINED, False
        for WANTED, None for anything else. As judge_cut."""
        return await self._judge(owed_mod.decline_judge_messages(question, heard, rest, words),
                                 owed_mod.decline_verdict)

    async def judge_barge(self, asked: str, reply: str, heard: str, said: str):
        """The barge-in judge barge_pause.BargeInJudgeGate asks (#224): whether the
        caller's words over a paused reply let it carry on. barge_pause.CONTINUE, STOP,
        ANSWER, or None for anything else. The caller bounds the time."""
        return await self._judge(barge_pause.judge_messages(asked, reply, heard, said),
                                 barge_pause.judge_verdict)

    async def _judge(self, messages: list, parse=owed_mod.judge_verdict):
        """A judge's one-word verdict, on this session's own client -- the same model,
        provider routing, key and HTTP/2 connection as the turn's requests."""
        extra = _llm_extra()
        response = await self._client.chat.completions.create(
            model=setting("LLM_MODEL"),
            messages=messages,
            max_tokens=_JUDGE_MAX_TOKENS,
            **{k: v for k, v in extra.items() if k != "extra_body"},
            extra_body=extra.get("extra_body"))
        choices = getattr(response, "choices", None) or ()
        content = choices[0].message.content if choices else ""
        return parse(content)

    def create_client(self, api_key=None, base_url=None, organization=None,
                      project=None, default_headers=None, **kwargs):
        self._http = _model_http_client(base_url)
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            project=project,
            default_headers=default_headers,
            # This session's bound on each request: the SDK passes it with every one,
            # so the shared client's own does not apply (but see _HTTP_CLIENTS).
            timeout=httpx.Timeout(_llm_timeout_secs(), connect=_CONNECT_SECS),
            http_client=self._http,
        )

    # --- the connection, open before the turn's request needs it (#201) ------------------
    # Over HTTP/2 the connection outlives cancelled replies and is kept _KEEPALIVE_SECS
    # idle, and the client is shared across sessions, so it is usually open already. When
    # it is not -- the brain's first session, or after a silence longer than that -- it
    # is opened at the session's start and when the caller starts speaking: a HEAD to the model's base URL (any answer will do; it is the connection
    # that counts), in the background. Over HTTP/2 it shares that connection with whatever
    # request comes next -- the greeting asked for at the same moment joins it while it is
    # still connecting -- rather than competing with it, which is what made this unsafe
    # over HTTP/1.1. Nothing runs while a connection is open.
    _http = None
    _warm_task = None
    _warm_closed = False

    def warm_connection(self):
        """Open the connection to the model's server now, in the background, unless one is
        open. Called at the session's start and when a user turn starts (agent_session)."""
        if self._warm_closed or self._http is None:
            return
        if self._warm_task is not None and not self._warm_task.done():
            return
        url = self._client.base_url
        if _has_live_connection(self._http, url):
            return
        self._warm_task = self.create_task(self._warm(str(url)), "llm-warm")

    async def _warm(self, url: str):
        started = time.monotonic()
        try:
            # The turn's own bound: on a shared HTTP/2 connection a shorter one would fail
            # the turn's request with it if the server went silent (_HTTP_CLIENTS).
            await self._http.head(url, timeout=httpx.Timeout(_llm_timeout_secs(),
                                                             connect=_CONNECT_SECS))
        except Exception as e:  # noqa: BLE001 -- a warm-up must never break the session
            logger.warning(f"{self}: could not open the connection to the language model's "
                           f"server ahead of the turn ({type(e).__name__} after "
                           f"{time.monotonic() - started:.1f}s); the request opens its own")
            return
        logger.debug(f"{self}: connection to the language model opened ahead of the turn "
                     f"in {time.monotonic() - started:.2f}s")

    async def start(self, frame):
        await super().start(frame)
        self.warm_connection()

    async def cleanup(self):
        # Closed for good: a turn-start event already scheduled must not start one after.
        self._warm_closed = True
        if self._warm_task is not None:
            await self.cancel_task(self._warm_task)
            self._warm_task = None
        await super().cleanup()


def prime_model_dns() -> None:
    """Look the model's host up at the brain's start, so a DNS stall at the first turn
    -- hours later, say -- already has its address to fall back on (llm_dns)."""
    try:
        host = httpx.URL(setting("LLM_BASE_URL") or "").raw_host.decode("ascii")
    except (httpx.InvalidURL, UnicodeError):
        return      # make_llm reports a bad URL when a session needs it
    prime(host)


def warm_first_use_imports() -> None:
    """Load now, at startup, what the first session and the first reply would otherwise
    load at their first use.

    - openai builds `client.chat` lazily: a cached_property that runs
      `from .resources.chat import AsyncChat` on first use (openai/_client.py, 2.54).
      That one import loads ~500 modules, most of them openai.types pydantic models,
      and it ran inside the process's first chat request. On the box that froze the
      loop 1297 ms right after a wake word (2026-10-15 14:06, after 10 h idle), ~700 ms
      with the files in the page cache. anyio's asyncio backend loads at the first
      connection.
    - httpcore loads with this module (llm_dns's backend); h11 loads when make_llm builds
      the first session's client, and certifi's CA bundle path is looked up (through
      importlib.resources) then.
    - Not pipecat's sentence split, any more. Up to pipecat 1.11 it was NLTK's Punkt,
      loaded lazily and warmed here from the punkt_tab data on disk. Since 1.12 it is
      sentencex: it loads with pipecat.utils.string, carries its rules in the wheel
      (no data, no download), and its first split imports nothing (~1 ms).
      test_llm_warmup still runs a split in the first reply it pins, so a pipecat
      that brings a lazy load back fails there. Nothing left here can reach the
      network, which the NLTK block could (its punkt_tab download), so the brain
      still starts at a first boot with none, for the spoken Wi-Fi setup.

    Each is paid once per process, so it fell on the first session after every restart.
    The server calls this before freeze_startup_objects, so the modules are loaded once
    and frozen with the rest, and selfcheck calls it, so a broken extension among them
    (jiter, under openai's chat API) fails the check, not the brain's start."""
    import anyio._backends._asyncio  # noqa: F401
    import certifi
    import httpcore  # noqa: F401
    import openai.resources.chat  # noqa: F401

    certifi.where()


# Any OpenAI-compatible chat endpoint — Groq, Cerebras, OpenRouter, a local
# llama.cpp server, etc. Point LLM_BASE_URL at the endpoint, LLM_API_KEY at its
# key, LLM_MODEL at the served model. gpt-oss-120b is the reference model (clean
# tool calls, no self-narration; llama-3.3-70b leaks "<function=...>" text —
# avoid). Provider-specific routing rides LLM_EXTRA_BODY (see _llm_extra).
def make_llm():
    base_url = setting("LLM_BASE_URL")
    if not base_url:
        raise RuntimeError(
            "LLM_BASE_URL is not set, or is not an http(s):// URL (see the warning above) — "
            "point it at your OpenAI-compatible endpoint "
            "(e.g. https://api.groq.com/openai/v1, or http://127.0.0.1:8182/v1 for a "
            "local server); see docs/CONFIG.md"
        )
    # When uncapped, max_tokens is left OUT of Settings rather than set to None: the
    # field then keeps its NOT_GIVEN default, the OpenAI SDK sentinel that drops it from
    # the request body entirely. Passing None would serialize an explicit null.
    _intercept_stdlib_logging()
    settings = dict(model=setting("LLM_MODEL"), extra=_llm_extra())
    cap = _max_tokens()
    if cap is not None:
        # BOTH fields. pipecat sends max_tokens and max_completion_tokens side by side and
        # drops whichever is left at the NOT_GIVEN sentinel, and the two halves of the
        # ecosystem no longer agree on which one to read: max_tokens is what llama.cpp,
        # Groq, Cerebras and OpenRouter accept, while pipecat's own InputParams marks it
        # "deprecated, use max_completion_tokens" and newer endpoints honour only the
        # latter. Setting one alone means a silently uncapped completion on half of them —
        # and an uncapped completion on a credit-metered gateway is the 2026-08-18 402.
        settings["max_tokens"] = cap
        settings["max_completion_tokens"] = cap
    return BoundedOpenAILLMService(
        api_key=get_llm_api_key(),
        base_url=base_url,
        settings=OpenAILLMService.Settings(**settings),
        # pipecat's own bound on the request, plus one retry (see _llm_timeout_secs).
        retry_on_timeout=True,
        retry_timeout_secs=_llm_timeout_secs(),
    )


# One TTS: the engine's, with word-level timestamps (they drive heard-grounding and
# playout-paced captions — the product's signature). Supertonic (no timestamps)
# and the brain-local torch/onnx backends were retired 2026-07; the engine's
# embedded TTS is the single voice path, mirroring STT.
def make_tts(voice: str | None = None, language: str | None = None, phonemes: bool = False):
    # voice/language are per-session (OpenClaw-selectable); they fall back
    # to the TTS_VOICE/TTS_LANGUAGE env, then the defaults. phonemes: the client
    # announced ?features=phonemes (engine_tts sends the words' timed phonemes).
    from teaport_brain.engine_tts import EngineTTSService

    return EngineTTSService(
        voice=voice or setting("TTS_VOICE"),
        language=language or setting("TTS_LANGUAGE"),
        phonemes=phonemes,
    )


def make_stt(makeup_db: float = 0.0) -> TeaportSTTService:
    """The incumbent engine by default; the streaming backend when asked for.

    TEAPORT_STT_BACKEND=streaming points the same service at vLLM serving
    Voxtral-Mini-4B-Realtime (see stt.py for why `final` cannot mean the same thing on
    both). Kept as one env switch so a call can be made on either backend without a
    deploy, and so reverting is deleting a line.

    makeup_db is the caller-path makeup gain (SIP only; see stt.py). Threaded from the
    front-end rather than read from env here, because both brains share brain.env and
    only the SIP one should apply it.
    """
    streaming = setting("TEAPORT_STT_BACKEND") == "streaming"
    if not streaming:
        return TeaportSTTService(url=TEAPORT_URL, makeup_db=makeup_db)
    url = setting("TEAPORT_STT_URL")
    model = setting("TEAPORT_STT_MODEL")
    logger.info(f"STT backend: streaming (vLLM) at {url}, model {model}")
    return TeaportSTTService(url=url, model=model, streaming_backend=True, makeup_db=makeup_db)
