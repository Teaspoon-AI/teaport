#
# teaport — OpenClaw gateway-relay voice server
#
# The Pipecat voice brain (engine STT -> LLM -> engine TTS, with the heard-grounding
# TranscriptLedger + HeardContextCorrector) behind a plain WebSocket that the
# OpenClaw `teaport` realtime-voice provider plugin connects to.
#
#   OpenClaw Talk client --talk.session.appendAudio--> gateway --RealtimeVoiceBridge-->
#       teaport plugin --WS /talk--> THIS server (STT/LLM/TTS + barge-in grounding)
#       --WS /talk--> plugin.onAudio --talk.event--> Talk client
#
# OpenClaw drives the plugin as a bridge-only provider over transport
# "gateway-relay": it pumps the user's PCM16/24k mic audio in via bridge.sendAudio()
# and relays our audio/clear/transcript back out. Pipecat owns the whole brain
# (STT+LLM+TTS+tools) and, crucially, the heard-grounded barge-in this project is
# built around; OpenClaw is just the multi-surface front-end.
#
# Audio is PCM16 mono 24 kHz both ways (the relay fixes this format); the STT
# service resamples to the STT's 16 kHz. the engine TTS provides per-word
# playout timestamps, the sharpest heard-grounding.
#
# The brain itself — pipeline, tools, greeting, single-slot eviction — lives in
# agent_session.py, shared with the SIP front-end (sip_server.py). This module is
# just the OpenClaw WebSocket transport + FastAPI plumbing around it.
#
# Usage:  teaport-brain [--host 0.0.0.0] [--port 7861]
#   Requires the engine reachable at TEAPORT_URL (default ws://127.0.0.1:8000).
#
import argparse
import os

# Cache-only HF hub, read at huggingface_hub import time — set before any imports.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket  # noqa: E402
from loguru import logger  # noqa: E402

from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.transports.websocket.fastapi import (  # noqa: E402
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from teaport_brain.agent_session import (  # noqa: E402
    acquire_slot,
    build_agent_session,
    slot_active,
)
from teaport_brain.gateway_serializer import (  # noqa: E402
    PIPELINE_SAMPLE_RATE,
    RELAY_SAMPLE_RATE,
    TeaportGatewaySerializer,
)
from teaport_brain.memory_hygiene import turn_reclaim  # noqa: E402
from teaport_brain.services import make_tts  # noqa: E402

LISTEN_PORT = int(os.getenv("GATEWAY_PORT", "7861"))
# Shared secret for /talk. When set, a client must present it as ?token=<value> on
# the WebSocket URL (the teaport-realtime plugin sends its TEAPORT_GATEWAY_TOKEN
# env / provider-config token); a missing or wrong token is rejected BEFORE the
# pipeline — and before the single-slot eviction — runs. When unset, anyone who can
# reach this port gets a full agent session (memory read/write tools included) and
# can evict the live call, so we log a loud warning at startup.
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")


async def run_relay_bot(websocket: WebSocket):
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
            audio_out_sample_rate=RELAY_SAMPLE_RATE,
            add_wav_header=False,
            serializer=TeaportGatewaySerializer(),
        ),
    )
    # OpenClaw selects the TTS voice/language per session: the teaport provider forwards
    # talk.realtime.providers.teaport.{voice,language} as WS URL query params. A
    # voice's prefix implies its language (ef_*→Spanish, …), so `voice` alone is enough;
    # `language` can override the phonemizer. Missing/unknown → defaults (af_heart/en-us).
    qp = websocket.query_params
    session = build_agent_session(
        transport, voice=qp.get("voice"), language=qp.get("language")
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        logger.info("OpenClaw relay client connected — greeting")
        await session.greet()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        logger.info("OpenClaw relay client disconnected — stopping")
        await session.task.cancel()

    # --- Single-slot eviction -----------------------------------------------
    # A frozen/abandoned client leaves its pipeline running (on_client_disconnected
    # never fires), holding the single STT slot until a ~5-min idle timeout. So
    # before starting ours, evict the previous pipeline (acquire_slot cancels it and
    # waits for its teardown so our STT can claim the slot). Pairs with the
    # STT-unavailable greeting warning as a backstop if a race slips.
    _my_done, release = await acquire_slot(session.task)
    try:
        await PipelineRunner(handle_sigint=False).run(session.task)
    finally:
        await release()


app = FastAPI()


@app.get("/health")
async def health():
    return {"ok": True, "tts": "engine"}


@app.websocket("/talk")
async def talk(websocket: WebSocket):
    # Auth BEFORE anything else: run_relay_bot evicts the live pipeline for every new
    # connection, so an unauthenticated socket must never get that far — otherwise any
    # host that can reach this port can kill the owner's call and use the agent (and
    # its memory read/write tools).
    if GATEWAY_TOKEN and websocket.query_params.get("token") != GATEWAY_TOKEN:
        logger.warning("rejected /talk client: missing or bad token")
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        await run_relay_bot(websocket)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"relay session error: {e}")
    finally:
        # Skip the reclaim when a replacement session evicted us: it is already
        # mid-greeting on this same event loop, and gc+malloc_trim+empty_cache here
        # would stall its audio — and empty_cache can contend the CUDA allocator
        # lock against its in-flight synth (the hazard MemoryReclaim's docstring
        # documents). The replacement's own session-end reclaim covers the memory.
        if not slot_active():
            turn_reclaim()
        else:
            logger.info("session-end reclaim skipped — a replacement session is active")


def main():
    parser = argparse.ArgumentParser(
        description="teaport OpenClaw gateway-relay voice server"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    args = parser.parse_args()
    if not GATEWAY_TOKEN:
        logger.warning(
            "GATEWAY_TOKEN is not set — /talk is UNAUTHENTICATED: anyone who can "
            "reach this port can use the agent (and its memory tools) and evict "
            "the live call. Set GATEWAY_TOKEN (server) + TEAPORT_GATEWAY_TOKEN "
            "(plugin) except on a trusted network."
        )
    logger.info("Priming TTS service...")
    make_tts()  # warm the engine TTS client once at startup (G2P/synthesis are engine-side)
    logger.info(f"teaport OpenClaw relay server on ws://{args.host}:{args.port}/talk")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
