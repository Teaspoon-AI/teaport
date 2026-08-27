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
# Lifecycle (M2, per-call): the UDS connection is PERSISTENT (a SipConnection owns
# the socket + receive loop + call-control dispatch for the whole process), but each
# CALL gets a FRESH pipeline. On call.state=confirmed we build a new
# SipGatewayTransport + AgentSession (fresh STT/LLM/TTS/context), run it as a
# background task, and greet; on call.state=disconnected we cancel that task, tearing
# the per-call transport down — which frees the engine's single STT slot and recycles
# all per-call state (the next caller builds fresh, so no context reset is needed).
# Single active call in protocol v0: a new confirmed cancels any running call first.
# If the gateway socket closes (EOF), we cancel any running call and stop.
#
# This mirrors the OpenClaw path (gateway_server.py), which already builds a fresh
# pipeline per WebSocket connection. The earlier one-persistent-pipeline design (reset
# context between calls, cancel_on_idle_timeout=False to keep it alive) is gone.
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
    SipConnection,
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

    # The serializer is shared: the persistent connection uses it to DESERIALIZE
    # inbound datagrams; the per-call output transport uses it (via params) to
    # SERIALIZE brain->gateway control. SipProtocolSerializer is stateless, so one
    # instance is safe for both directions.
    serializer = SipProtocolSerializer()
    params = make_sip_params(serializer)
    connection = SipConnection(sock, serializer)

    logger.info(f"half-duplex input gate: {'ON' if HALF_DUPLEX else 'off'} "
                f"(tail {_HD_TAIL_S}s) — diagnostic; real fix is AEC in the bridge")

    # Single active call in protocol v0. Holds (session, runner_task, transport) for
    # the currently-running per-call pipeline, or None between calls.
    active = {"call": None}

    async def cancel_active_call(reason: str):
        """Tear down the running per-call pipeline (if any): cancel its task and wait
        out its teardown so the STT _disconnect closes the engine socket and frees the
        single STT slot before the next call's STT connects."""
        call = active["call"]
        if call is None:
            return
        active["call"] = None
        session, runner_task, _transport = call
        logger.info(f"tearing down the per-call pipeline ({reason})")
        try:
            await session.task.cancel()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"error cancelling per-call pipeline task: {e!r}")
        try:
            await asyncio.wait_for(runner_task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("per-call pipeline runner did not finish within 5s of cancel")
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning(f"per-call pipeline runner ended with: {e!r}")
        # Let the engine process the STT close and free the single slot before the
        # next call's STT connects (mirrors the OpenClaw path's acquire_slot settle).
        await asyncio.sleep(0.3)

    @connection.event_handler("on_client_connected")
    async def on_connected(_connection):
        logger.info("SIP gateway socket connected")

    @connection.event_handler("on_hello")
    async def on_hello(_connection, msg):
        logger.info(f"gateway hello: proto={msg.get('proto')} rate={msg.get('rate')} "
                    f"ch={msg.get('channels')} ptime={msg.get('ptime_ms')}ms")

    @connection.event_handler("on_call_incoming")
    async def on_call_incoming(_connection, msg):
        logger.info(f"call.incoming id={msg.get('call_id')} from={msg.get('from')} to={msg.get('to')}")

    @connection.event_handler("on_dtmf")
    async def on_dtmf(_connection, call_id, digit):
        logger.info(f"dtmf {digit!r} (call {call_id})")

    @connection.event_handler("on_call_state")
    async def on_call_state(_connection, call_id, state):
        logger.info(f"call.state={state} (call {call_id})")
        if state == "confirmed":
            # Single active call in v0: evict any running pipeline first so its STT
            # slot is freed before ours connects.
            await cancel_active_call("superseded by a new call")
            logger.info(f"building a FRESH per-call pipeline for call {call_id} "
                        "(new STT/LLM/TTS/context)")
            transport = SipGatewayTransport(connection, params)
            # Same shared brain as the OpenClaw path, minus barge-in when HALF_DUPLEX
            # is on (the input gate is the ONE SIP-specific processor). No
            # cancel_on_idle_timeout override: a per-call pipeline uses PipelineTask's
            # default, exactly like the OpenClaw per-connection pipeline.
            session = build_agent_session(
                transport,
                input_processors=[HalfDuplexInputGate()] if HALF_DUPLEX else None,
            )
            runner_task = asyncio.create_task(
                PipelineRunner(handle_sigint=False).run(session.task)
            )
            active["call"] = (session, runner_task, transport)
            await session.greet()
            if session.should_end:
                # The engine's single STT slot is held by another session (e.g. the
                # local OpenClaw brain). greet() spoke the busy line instead of
                # greeting; let it play out, then hang the caller up and tear the
                # per-call pipeline down so the line drops cleanly. wait_until_delivered
                # returns once the line has been spoken (or after a short timeout if the
                # engine can't even synthesize it — hang up either way).
                logger.info(f"STT slot busy for call {call_id} — playing the busy "
                            "message, then hanging up")
                await session.followup_gate.wait_until_delivered()
                await transport.send_control({"type": "call.hangup"})
                await cancel_active_call("busy — STT slot held by another session")
        elif state == "disconnected":
            await cancel_active_call("call disconnected")
            logger.info("call disconnected — per-call pipeline torn down, STT slot freed")

    @connection.event_handler("on_client_disconnected")
    async def on_disconnected(_connection):
        logger.info("SIP gateway hung up (socket EOF) — stopping")

    logger.info("SIP brain ready — persistent connection up; a fresh pipeline is "
                "built per call (STT -> LLM -> TTS over teaport-sip)")
    try:
        # Blocks for the whole process: dispatches control + routes audio. Per-call
        # pipelines run as background tasks launched from on_call_state above.
        await connection.run()
    finally:
        # EOF (or any exit): tear down any running call and close the socket.
        await cancel_active_call("connection closed")
        connection.close()
    logger.info("SIP brain stopped")


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
