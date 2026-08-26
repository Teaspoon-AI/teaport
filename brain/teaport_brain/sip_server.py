# SPDX-License-Identifier: MIT
#
# teaport — SIP brain client (M2): the real agent behind the teaport-sip gateway.
#
# The SIP-over-UNIX-socket sibling of gateway_server.py (the OpenClaw WebSocket
# relay). Same brain — both front-ends now build it through the shared factory
# agent_session.build_agent_session(): persona + VAD + smart-turn endpointing +
# heard-grounding ledger + memory recall/reclaim + captions + turn-timing taps +
# the degenerate-text guards + tools. The only difference is the transport:
# SipGatewayTransport over the teaport-sip AF_UNIX SOCK_SEQPACKET socket
# (sip_transport.py) instead of a FastAPI WebSocket. A phone call at the gateway
# reaches Voxtral STT -> LLM -> Kokoro TTS instead of a stub echo.
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
# untouched. The SIP path drives the SAME shared pipeline the OpenClaw path does
# (via agent_session), so it now gets memory recall/reclaim, captions/transcript
# emitters, and the turn-timing taps it previously lacked (captions/transcript
# frames are harmlessly dropped by the SIP serializer — no SIP control type carries
# them). Tools run exactly like the OpenClaw path: the async ask_openclaw follow-up
# rides a FollowupGate + _make_consult_followup with a ThinkingSound bed over the
# consult wait, so a caller gets REAL tool results instead of hallucinated ones. On
# SIP there is no OpenClaw plugin to service the native openclaw_agent_consult
# round-trip (the sip_serializer drops it, being a non-protocol control), so
# ask_openclaw degrades to the CLI agent_consult path; the fast tools (host status,
# time, web search/fetch, memory) run unchanged.

import argparse
import asyncio
import os

# Cache-only HF hub, read at huggingface_hub import time — set before any imports.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from loguru import logger  # noqa: E402

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402

from teaport_brain.agent_session import build_agent_session  # noqa: E402
from teaport_brain.services import make_tts  # noqa: E402
from teaport_brain.sip_serializer import SipProtocolSerializer  # noqa: E402
from teaport_brain.sip_transport import (  # noqa: E402
    DEFAULT_UDS_PATH,
    SipGatewayTransport,
    connect_seqpacket,
    make_sip_params,
)


# --- DIAGNOSTIC: half-duplex input gate (echo-hypothesis test) ------------------
# Telephony has no client-side echo cancellation (unlike the OpenClaw Talk client
# the WS path relies on), so the bot's own audio echoes back down the line and
# retriggers the VAD/STT (the bot fights itself → rough conversation). This gate
# DROPS caller audio while the bot is speaking, plus a tail covering the gateway's
# bounded (~1 s) playout backlog. It is NOT the real fix — that's an echo canceller
# in the teaport-sip bridge (pjmedia AEC), keeping the brain transport-agnostic —
# but it isolates whether echo is the cause. Kill switch: SIP_HALF_DUPLEX=0.
HALF_DUPLEX = os.getenv("SIP_HALF_DUPLEX", "1").strip().lower() not in ("0", "false", "no")
_HD_TAIL_S = float(os.getenv("SIP_HALF_DUPLEX_TAIL_S", "0.8"))


class HalfDuplexInputGate(FrameProcessor):
    """Swallow caller InputAudioRawFrames while the bot is speaking (+ a tail), so
    un-cancelled line echo can't retrigger the VAD. Half-duplex: no barge-in.

    The one SIP-specific processor — passed to build_agent_session as an
    input-side processor (inserted right after transport.input())."""

    def __init__(self, tail_s: float = _HD_TAIL_S):
        super().__init__()
        self._muted = False
        self._tail_s = tail_s
        self._unmute_task = None

    async def _unmute_after_tail(self):
        await asyncio.sleep(self._tail_s)
        self._muted = False
        logger.debug("half-duplex: unmuted (tail elapsed)")

    async def _cancel_pending(self):
        if self._unmute_task is not None:
            t, self._unmute_task = self._unmute_task, None
            await self.cancel_task(t)

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            await self._cancel_pending()
            if not self._muted:
                logger.debug("half-duplex: muted (bot speaking)")
            self._muted = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self._cancel_pending()
            self._unmute_task = self.create_task(self._unmute_after_tail())
        # Drop the caller's mic while muted; pass everything else through.
        if (self._muted and direction == FrameDirection.DOWNSTREAM
                and isinstance(frame, InputAudioRawFrame)):
            return
        await self.push_frame(frame, direction)


async def run(sock_path: str):
    logger.info(f"connecting to teaport-sip gateway at {sock_path}")
    sock = connect_seqpacket(sock_path)
    logger.info("connected — the brain is the socket client (gateway is the server)")

    transport = SipGatewayTransport(sock, make_sip_params(SipProtocolSerializer()))

    logger.info(f"half-duplex input gate: {'ON' if HALF_DUPLEX else 'off'} "
                f"(tail {_HD_TAIL_S}s) — diagnostic; real fix is AEC in the bridge")
    # Same shared brain as the OpenClaw path, minus barge-in when HALF_DUPLEX is on
    # (the input gate is the ONE SIP-specific processor). SIP runs ONE persistent
    # pipeline across calls, so pipecat's idle-timeout (default ~5 min) must NOT
    # cancel it between calls — cancel_on_idle_timeout=False keeps the brain alive
    # so the next caller isn't met with silence.
    session = build_agent_session(
        transport,
        input_processors=[HalfDuplexInputGate()] if HALF_DUPLEX else None,
        cancel_on_idle_timeout=False,
    )

    greeted = {"active": False}

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
            await session.greet()
        elif state == "disconnected":
            # Reset for the next caller WITHOUT tearing down the pipeline (M2).
            greeted["active"] = False
            session.reset_context()
            logger.info("call disconnected — LLM context reset for the next caller")

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _client):
        logger.info("SIP gateway hung up (socket EOF) — stopping the pipeline")
        await session.task.cancel()

    logger.info("SIP brain pipeline starting (STT -> LLM -> TTS over teaport-sip)")
    await PipelineRunner(handle_sigint=False).run(session.task)
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
