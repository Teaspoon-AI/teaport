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
import os
import time

import httpx
from loguru import logger
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from pipecat.services.openai.llm import OpenAILLMService

from teaport_brain.env import env_num
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
    raw = os.getenv("LLM_EXTRA_BODY")
    if raw:
        extra["extra_body"] = json.loads(raw)
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
_MAX_TOKENS_BY_EFFORT = {"": 1024, "low": 1024, "medium": 3072, "high": 8192}
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
    settings = dict(model=os.getenv("LLM_MODEL", "gpt-oss-120b"), extra=_llm_extra())
    cap = _max_tokens()
    if cap is not None:
        settings["max_tokens"] = cap
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
