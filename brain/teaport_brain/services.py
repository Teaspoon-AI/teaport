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
import json
import logging
import os
import time

import httpx
from loguru import logger
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from pipecat.services.openai.llm import OpenAILLMService

from teaport_brain.env import env_json, env_num
from teaport_brain.stt import TeaportSTTService

TEAPORT_URL = os.getenv("TEAPORT_URL", "ws://127.0.0.1:8000/v1/realtime")


# gpt-oss reasoning effort. gpt-oss emits hundreds of hidden chain-of-thought
# chars before the first SPOKEN token; "low" cuts that ~10x (first-spoken-token
# 0.18-0.34s vs 0.5s+) with no quality loss on short replies. Sent as a top-level
# `extra` kwarg; set LLM_REASONING_EFFORT="" for models that don't support it.
# LLM_EXTRA_BODY (JSON) rides `extra_body` for provider-specific routing — e.g.
# OpenRouter's {"provider": {"order": ["Groq"], "allow_fallbacks": true}}.
_DEFAULT_EFFORT = "low"  # shared with _default_max_tokens — the two must not drift


def _reasoning_effort() -> str:
    return (os.getenv("LLM_REASONING_EFFORT", _DEFAULT_EFFORT) or "").strip().lower()


def _llm_extra() -> dict:
    extra: dict = {}
    effort = _reasoning_effort()
    if effort:
        extra["reasoning_effort"] = effort
    # env_json, not a bare json.loads: this runs inside build_agent_session(), so a
    # malformed hand-edit (or a wrapper that `source`d brain.env and stripped the
    # quotes) took down EVERY session with a raw JSONDecodeError while the service
    # itself looked healthy. See env.py.
    body = env_json("LLM_EXTRA_BODY")
    if body is not None:
        extra["extra_body"] = body
    return extra


def get_llm_api_key() -> str:
    # LLM_API_KEY wins; else the installer-written key file. A local
    # OpenAI-compatible server that ignores auth still needs a placeholder —
    # set LLM_API_KEY=sk-local (or write it to the file).
    key = os.getenv("LLM_API_KEY")
    if key:
        return key
    path = os.path.expanduser("~/.config/teaport/llm_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
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
# ask_openclaw request is the realistic case.
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
# that re-running the installer cannot clear (see env.env_num, which this shares).
def _max_tokens() -> int | None:
    """Completion cap, or None for no cap at all.

    LLM_MAX_TOKENS=0 means uncapped — the pre-2026-08-18 behaviour. The cap exists to
    stop a credit-metered gateway reserving against the model's 65536-token ceiling;
    an operator on a local llama.cpp server or an un-metered endpoint has no such
    gateway, and 0 is the value that most obviously reads as "no cap", so it must not
    quietly mean 1024."""
    default = _default_max_tokens()
    value = env_num("LLM_MAX_TOKENS", str(default), int)
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
    secs = env_num("LLM_TIMEOUT_SECS", "20", float)
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
    every other tool would not, which is the shape of the ask_openclaw drops that went
    unexplained for hours.

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
    completion, and in a voice pipeline every barge-in interrupts one. With
    keepalive_expiry=None below, nothing reclaims them on a timer either.

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


# Connect timeout. Separate from the read timeout below because they fail differently:
# an unreachable endpoint should fail fast, a slow-but-alive one should not.
_CONNECT_SECS = 5.0


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
        except asyncio.CancelledError:
            logger.warning(
                f"{self}: completion CANCELLED after {time.monotonic() - started:.1f}s "
                "— this turn produces no reply and raises no error"
            )
            raise
        else:
            # The success case is logged too, because "the bot said nothing" has three
            # very different causes and the journal could not tell them apart: the
            # completion never returned (no line), it returned and the reply went
            # missing downstream (this line, then silence), or it returned a tool call
            # that never dispatched (this line, then no "Calling function"). One line
            # splits all three.
            logger.info(f"{self}: completion finished in {time.monotonic() - started:.1f}s")

    # Paired with the above: this marks the boundary between "the request never got a
    # response" and "the response arrived but consuming it stalled". Without it both
    # look identical from the journal — a "Generating chat from context" line and
    # nothing after it.
    #
    # The stream is also renumbered on the way past — see _sequential_tool_call_indices.
    async def get_chat_completions(self, context):
        stream = await super().get_chat_completions(context)
        logger.debug(f"{self}: response stream open")
        # _RenumberedStream, not the bare generator — see the class docstring: a
        # generator has no close(), so wrapping in one silently disabled base_llm's
        # socket release.
        return _RenumberedStream(stream)

    def create_client(self, api_key=None, base_url=None, organization=None,
                      project=None, default_headers=None, **kwargs):
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            project=project,
            default_headers=default_headers,
            http_client=DefaultAsyncHttpxClient(
                timeout=httpx.Timeout(_llm_timeout_secs(), connect=_CONNECT_SECS),
                limits=httpx.Limits(
                    max_keepalive_connections=100, max_connections=1000, keepalive_expiry=None
                ),
            ),
        )


# Any OpenAI-compatible chat endpoint — Groq, Cerebras, OpenRouter, a local
# llama.cpp server, etc. Point LLM_BASE_URL at the endpoint, LLM_API_KEY at its
# key, LLM_MODEL at the served model. gpt-oss-120b is the reference model (clean
# tool calls, no self-narration; llama-3.3-70b leaks "<function=...>" text —
# avoid). Provider-specific routing rides LLM_EXTRA_BODY (see _llm_extra).
def make_llm():
    base_url = os.getenv("LLM_BASE_URL")
    if not base_url:
        raise RuntimeError(
            "LLM_BASE_URL is not set — point it at your OpenAI-compatible endpoint "
            "(e.g. https://api.groq.com/openai/v1, or http://127.0.0.1:8182/v1 for a "
            "local server); see docs/CONFIG.md"
        )
    # When uncapped, max_tokens is left OUT of Settings rather than set to None: the
    # field then keeps its NOT_GIVEN default, the OpenAI SDK sentinel that drops it from
    # the request body entirely. Passing None would serialize an explicit null.
    _intercept_stdlib_logging()
    settings = dict(model=os.getenv("LLM_MODEL", "gpt-oss-120b"), extra=_llm_extra())
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
def make_tts(voice: str | None = None, language: str | None = None):
    # voice/language are per-session (OpenClaw-selectable); they fall back
    # to the TTS_VOICE/TTS_LANGUAGE env, then the defaults.
    from teaport_brain.engine_tts import EngineTTSService

    return EngineTTSService(
        voice=voice or os.getenv("TTS_VOICE", "af_heart"),
        language=language or os.getenv("TTS_LANGUAGE") or None,
    )


def make_stt() -> TeaportSTTService:
    return TeaportSTTService(url=TEAPORT_URL)
