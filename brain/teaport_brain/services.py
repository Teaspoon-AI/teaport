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

import json
import os

from loguru import logger

from pipecat.services.openai.llm import OpenAILLMService

from teaport_brain.stt import TeaportSTTService

TEAPORT_URL = os.getenv("TEAPORT_URL", "ws://127.0.0.1:8000/v1/realtime")


# gpt-oss reasoning effort. gpt-oss emits hundreds of hidden chain-of-thought
# chars before the first SPOKEN token; "low" cuts that ~10x (first-spoken-token
# 0.18-0.34s vs 0.5s+) with no quality loss on short replies. Sent as a top-level
# `extra` kwarg; set LLM_REASONING_EFFORT="" for models that don't support it.
# LLM_EXTRA_BODY (JSON) rides `extra_body` for provider-specific routing — e.g.
# OpenRouter's {"provider": {"order": ["Groq"], "allow_fallbacks": true}}.
def _llm_extra() -> dict:
    extra: dict = {}
    effort = os.getenv("LLM_REASONING_EFFORT", "low")
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
# 1024 is sized for gpt-oss's reasoning tokens (billed as completion tokens, a few
# hundred at reasoning_effort=low) plus a spoken reply, with headroom — while cutting
# the reservation ~64x. It also puts a hard ceiling on a degenerate collapse, which has
# run to 2340 characters in a single completion; LLMTextGuard still does the fine-grained
# containment, this just bounds the worst case.
#
# Parsed defensively: this value lives in brain.env, which installer repairs preserve
# verbatim, so a bare int() would turn one operator typo into an import-time crash-loop
# that re-running the installer cannot clear (see engine_tts._env_num, same reasoning).
def _max_tokens() -> int:
    raw = (os.getenv("LLM_MAX_TOKENS") or "").strip()
    try:
        value = int(raw or "1024")
    except ValueError:
        logger.warning(f"LLM_MAX_TOKENS={raw!r} is not a number; using 1024")
        return 1024
    if value < 1:
        logger.warning(f"LLM_MAX_TOKENS={value} is not positive; using 1024")
        return 1024
    return value


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
    return OpenAILLMService(
        api_key=get_llm_api_key(),
        base_url=base_url,
        settings=OpenAILLMService.Settings(
            model=os.getenv("LLM_MODEL", "gpt-oss-120b"),
            max_tokens=_max_tokens(),
            extra=_llm_extra(),
        ),
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
