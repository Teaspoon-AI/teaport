# SPDX-License-Identifier: MIT
#
# teaport — SIP brain client (M2): the real agent behind the teaport-sip gateway.
#
# The SIP-over-UNIX-socket sibling of gateway_server.py (the OpenClaw WebSocket
# relay). Same brain, same factories (services.make_stt/make_llm/make_tts), same
# persona + VAD + smart-turn endpointing + heard-grounding ledger — but the
# transport is SipGatewayTransport over the teaport-sip AF_UNIX SOCK_SEQPACKET
# socket (sip_transport.py) instead of a FastAPI WebSocket. A phone call at the
# gateway now reaches Voxtral STT -> LLM -> Kokoro TTS instead of a stub echo.
#
# Lifecycle (M2): the UDS connection = the pipeline's lifetime (one persistent
# pipeline, single active call in protocol v0). We GREET on call.state=confirmed
# and RESET the LLM context on call.state=disconnected so the next caller starts
# fresh WITHOUT tearing the pipeline down. If the gateway socket closes, we stop.
#
# Usage:  python -m teaport_brain.sip_server [--socket /tmp/teaport-sip.sock]
#   Requires the engine at TEAPORT_URL (STT/TTS) and an LLM at LLM_BASE_URL.
#
# NOTE: parallel to gateway_server.py, not a replacement — the OpenClaw path is
# untouched. Deferred vs. the OpenClaw pipeline (documented in RUNLOG): tools /
# ask_openclaw, memory recall + reclaim, captions/transcript emitters (no SIP
# control type carries them), thinking-sound, follow-up gate, turn-timing taps.

import argparse
import asyncio
import os

# Cache-only HF hub, read at huggingface_hub import time — set before any imports.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from loguru import logger  # noqa: E402

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams  # noqa: E402
from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADParams  # noqa: E402
from pipecat.frames.frames import LLMRunFrame, TTSSpeakFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (  # noqa: E402
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from teaport_brain import endpoint_debug  # noqa: E402
from teaport_brain.endpointing import (  # noqa: E402
    ENDPOINT_STOP_SECS,
    INTERRUPT_MIN_WORDS,
    SMARTTURN_COMPLETE_THRESHOLD,
    VAD_CONFIDENCE,
    VAD_MIN_VOLUME,
    EagerSmartTurnAnalyzer,
)
from teaport_brain.engine_tts import LANG_NAMES  # noqa: E402
from teaport_brain.heard_context import HeardContextCorrector  # noqa: E402
from teaport_brain.persona import build_system_prompt, load_persona  # noqa: E402
from teaport_brain.services import make_llm, make_stt, make_tts  # noqa: E402
from teaport_brain.sip_serializer import SipProtocolSerializer  # noqa: E402
from teaport_brain.sip_transport import (  # noqa: E402
    DEFAULT_UDS_PATH,
    SipGatewayTransport,
    connect_seqpacket,
    make_sip_params,
)
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

# Non-English TTS languages → the name to steer the LLM to reply in (mirrors
# gateway_server); English needs no directive (the persona is already English).
_TTS_LANG_NAMES = {k: v for k, v in LANG_NAMES.items() if not k.startswith("en-")}


def _vad_cls():
    return (endpoint_debug.InstrumentedSileroVAD if endpoint_debug.ENABLED
            else SileroVADAnalyzer)


def _initial_messages(system_prompt: str, lang_name: str | None) -> list:
    msgs = [{"role": "system", "content": system_prompt}]
    if lang_name:
        msgs.append({"role": "system", "content": f"Always reply to the user in {lang_name}."})
    return msgs


async def run(sock_path: str):
    logger.info(f"connecting to teaport-sip gateway at {sock_path}")
    sock = connect_seqpacket(sock_path)
    logger.info("connected — the brain is the socket client (gateway is the server)")

    transport = SipGatewayTransport(sock, make_sip_params(SipProtocolSerializer()))

    # pipecat 1.x drives VAD from the user aggregator (not the transport); it owns
    # endpointing + barge-in. Same policy as the OpenClaw path (endpointing.py).
    vad_analyzer = _vad_cls()(
        params=VADParams(confidence=VAD_CONFIDENCE, min_volume=VAD_MIN_VOLUME,
                         stop_secs=ENDPOINT_STOP_SECS)
    )

    stt = make_stt()
    llm = make_llm()
    tts = make_tts()

    persona = load_persona()
    logger.info(f"loaded shared persona ({len(persona)} chars)")
    system_prompt = build_system_prompt(persona)
    lang_name = _TTS_LANG_NAMES.get(getattr(tts, "espeak_language", "en-us"))
    # M2: no tools schema — plain persona chat (tools/ask_openclaw deferred).
    context = LLMContext(_initial_messages(system_prompt, lang_name))
    if lang_name:
        logger.info(f"TTS language {lang_name} → instructing the LLM to reply in it")

    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad_analyzer,
            user_mute_strategies=[],
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[TurnAnalyzerUserTurnStopStrategy(
                    turn_analyzer=EagerSmartTurnAnalyzer(
                        complete_threshold=SMARTTURN_COMPLETE_THRESHOLD,
                        params=SmartTurnParams(stop_secs=ENDPOINT_STOP_SECS),
                    )
                )],
            ),
        ),
    )

    ledger = TranscriptLedger()  # observer: logs the merged user/assistant transcript
    heard_corrector = HeardContextCorrector(ledger, context)

    pipeline = Pipeline([
        transport.input(),
        stt,
        context_aggregator.user(),
        heard_corrector,
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])

    task = PipelineTask(pipeline, observers=[ledger])

    greeted = {"active": False}

    async def greet():
        # Don't greet through a deaf STT: if the single-session engine is busy the
        # STT connect returns 503 and stt_available resolves False — warn instead.
        for _ in range(120):
            if stt.stt_available is not None:
                break
            await asyncio.sleep(0.1)
        if stt.stt_available is not True:
            logger.warning("STT unavailable at confirmed — warning the caller instead of greeting")
            await task.queue_frames([TTSSpeakFrame(
                "Sorry, I can't hear you right now — my speech recognition isn't "
                "available. Please hang up and call back in a moment."
            )])
            return
        # Greet with an LLM turn. Seed it as a USER-role stage direction, not a
        # system note: at confirm the context is otherwise system-only, and strict
        # OpenAI-compatible providers (observed: OpenRouter->qwen3) reject a
        # completion with "No user query found in messages". The parenthetical
        # reads as a call-connected cue, so the persona still generates the wording.
        context.add_message({
            "role": "user",
            "content": "(The phone call just connected. Greet me warmly in one short sentence.)",
        })
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _client):
        logger.info("SIP gateway socket connected")

    @transport.event_handler("on_hello")
    async def on_hello(_transport, msg):
        logger.info(f"gateway hello: proto={msg.get('proto')} rate={msg.get('rate')} "
                    f"ch={msg.get('channels')} ptime={msg.get('ptime_ms')}ms")

    @transport.event_handler("on_call_incoming")
    async def on_call_incoming(_transport, msg):
        logger.info(f"call.incoming id={msg.get('call_id')} from={msg.get('from')} to={msg.get('to')}")

    @transport.event_handler("on_dtmf")
    async def on_dtmf(_transport, call_id, digit):
        logger.info(f"dtmf {digit!r} (call {call_id})")

    @transport.event_handler("on_call_state")
    async def on_call_state(_transport, call_id, state):
        logger.info(f"call.state={state} (call {call_id})")
        if state == "confirmed" and not greeted["active"]:
            greeted["active"] = True
            await greet()
        elif state == "disconnected":
            # Reset for the next caller WITHOUT tearing down the pipeline (M2).
            greeted["active"] = False
            context.set_messages(_initial_messages(system_prompt, lang_name))
            heard_corrector._done = len(ledger.events)
            logger.info("call disconnected — LLM context reset for the next caller")

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _client):
        logger.info("SIP gateway hung up (socket EOF) — stopping the pipeline")
        await task.cancel()

    logger.info("SIP brain pipeline starting (STT -> LLM -> TTS over teaport-sip)")
    await PipelineRunner(handle_sigint=False).run(task)
    logger.info("SIP brain pipeline stopped")


def main():
    parser = argparse.ArgumentParser(description="teaport SIP brain client (M2)")
    parser.add_argument("--socket", default=os.getenv("TEAPORT_SIP_SOCKET", DEFAULT_UDS_PATH),
                        help="gateway UDS path (default: the live /tmp/teaport-sip.sock)")
    args = parser.parse_args()
    logger.info("Priming TTS service...")
    make_tts()  # warm the engine TTS client once at startup
    asyncio.run(run(args.socket))


if __name__ == "__main__":
    main()
