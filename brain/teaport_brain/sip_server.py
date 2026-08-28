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
# Usage:  python -m teaport_brain.sip_server [--socket /run/teaport/teaport-sip.sock]
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
from teaport_brain.env import env_flag, env_num  # noqa: E402
from teaport_brain.memory_hygiene import turn_reclaim  # noqa: E402
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
#
# Both knobs go through env.py rather than a hand-rolled parse and a bare cast, because
# both are read at IMPORT time out of /etc/teaport/brain.env, which installer repairs
# preserve verbatim:
#
#   The kill switch had grown its OWN truth table ("0"/"false"/"no"), which accepted
#   neither `off` nor `n`. So `SIP_HALF_DUPLEX=off` — a spelling docs/CONFIG.md
#   documents, and one brain.env can decide, since an EnvironmentFile overrides the
#   unit's own `Environment=SIP_HALF_DUPLEX=0` (systemd.exec) — left the gate ON:
#   barge-in silently dead on the phone line, with not one journal line about it,
#   since a private table also skips env_flag's "disabled" log. tools.py records the
#   identical bug for `TEAPORT_AGENT_FIRST=on`.
#
#   A bare float() on the tail turns one operator typo (`SIP_HALF_DUPLEX_TAIL_S=`, or
#   `=0,8` from a comma-decimal locale) into an import-time ValueError: sip_server never
#   starts, and systemd/teaport-sip-brain.service.in's Restart=always + RestartSec=5
#   crash-loop it forever, with no way to clear it short of hand-editing the file —
#   re-running the installer will not. env_num warns and falls back instead.
HALF_DUPLEX = env_flag("SIP_HALF_DUPLEX", True)
_HD_TAIL_S = env_num("SIP_HALF_DUPLEX_TAIL_S", "0.8", float)


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
    # SERIALIZE everything going the other way — control, audio, and interruptions.
    # (Audio used to bypass it and call encode_audio() directly, which left the wire
    # format defined in two places and the serializer's audio and InterruptionFrame
    # branches unreachable; the transport now routes all three through it.)
    # SipProtocolSerializer is stateless, so one instance is safe for both directions.
    serializer = SipProtocolSerializer()
    params = make_sip_params(serializer)
    connection = SipConnection(sock, serializer)

    logger.info(f"half-duplex input gate: {'ON' if HALF_DUPLEX else 'off'} "
                f"(tail {_HD_TAIL_S}s) — diagnostic; real fix is AEC in the bridge")

    # Single active call in protocol v0. Holds (call_id, session, runner_task,
    # transport) for the currently-running per-call pipeline, or None between calls.
    # The call_id is load-bearing: without it a `disconnected` cannot tell whether it
    # is about the pipeline we are actually running (see on_call_state).
    active = {"call": None}
    # The in-flight bring-up, if any: {"task", "call_id"}. Building the pipeline and
    # greeting is SLOW (model construction plus greet()'s STT poll), so it runs as a
    # task rather than inline in the receive loop — see on_call_state.
    setup = {"task": None, "call_id": None}

    async def cancel_active_call(reason: str, call_id: str | None = None,
                                 reclaim: bool = True):
        """Tear down the running per-call pipeline: cancel its task and wait out its
        teardown so the STT _disconnect closes the engine socket and frees the single
        STT slot before the next call's STT connects.

        `reclaim` runs the session-end memory reclaim once the pipeline is gone; pass
        False when a replacement call is already being brought up behind this teardown
        (see the `confirmed` branch of on_call_state, the only such caller).

        `call_id`, when given, is a GUARD: tear down only if the running pipeline
        belongs to that call. Without it this cancelled whatever happened to be
        running, so a stale `disconnected` for a finished call killed the pipeline of
        the call that had replaced it — leaving that caller connected to a gateway with
        no brain: no audio, no hangup, and no further `confirmed` ever coming. TLC finds
        it in 9 steps (brain/formal/SipCall.tla, MODE = "asWritten", NoWrongTeardown)."""
        call = active["call"]
        if call is None:
            return
        if call_id is not None and call[0] != call_id:
            logger.info(f"ignoring {reason} for call {call_id} — the active pipeline "
                        f"belongs to call {call[0]}")
            return
        active["call"] = None
        _call_id, session, runner_task, _transport = call
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
        if reclaim:
            # A CALL is a session, so this is the session end — the SIP twin of the
            # turn_reclaim in gateway_server's talk() finally, and the only place the
            # per-call pipeline's memory ever comes back: MemoryReclaim deliberately
            # omits empty_cache per turn (it can lock the CUDA allocator against an
            # in-flight synth and deadlock a barge-in), and glibc keeps the freed
            # smart-turn/VAD/resampler arena pages (~35 MB a session) until something
            # calls malloc_trim. Without this, RSS and VRAM ratchet call over call on
            # the 8 GB unified pool — and on the phone-dedicated box docs/CONFIG.md
            # recommends (`systemctl disable --now teaport-brain`) NO other process is
            # running sessions to reclaim on our behalf, so it ends at an OOM.
            #
            # Off the loop, like MemoryReclaim's per-turn trim, but AWAITED: this
            # usually runs inline in the connection's single receive loop, so awaiting
            # it is also what guarantees no bring-up starts mid-reclaim — nothing is
            # dispatched while the loop is not reading.
            await asyncio.get_running_loop().run_in_executor(None, turn_reclaim)

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

    async def cancel_setup(reason: str, call_id: str | None = None):
        """Abandon an in-flight bring-up. `call_id` guards it the same way
        cancel_active_call's does."""
        task = setup["task"]
        if task is None:
            return
        if call_id is not None and setup["call_id"] != call_id:
            return
        logger.info(f"abandoning the in-flight bring-up for call {setup['call_id']} "
                    f"({reason})")
        setup["task"] = None
        setup["call_id"] = None
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning(f"bring-up task ended with: {e!r}")

    async def bring_up_call(call_id):
        """Build the per-call pipeline and greet. Runs as a TASK, never inline in the
        receive loop — see on_call_state."""
        logger.info(f"building a FRESH per-call pipeline for call {call_id} "
                    "(new STT/LLM/TTS/context)")
        transport = SipGatewayTransport(connection, params)
        # Same shared brain as the OpenClaw path, minus barge-in when HALF_DUPLEX is on
        # (the input gate is the ONE SIP-specific processor). No cancel_on_idle_timeout
        # override: a per-call pipeline uses PipelineTask's default, exactly like the
        # OpenClaw per-connection pipeline.
        session = build_agent_session(
            transport,
            input_processors=[HalfDuplexInputGate()] if HALF_DUPLEX else None,
        )
        runner_task = asyncio.create_task(
            PipelineRunner(handle_sigint=False).run(session.task)
        )
        # Publish BEFORE greeting: from here a `disconnected` for this call can find
        # and tear down the pipeline even though the bring-up is still running.
        active["call"] = (call_id, session, runner_task, transport)
        await session.greet()
        if session.should_end:
            # STT is unavailable — either the engine's single slot is held by another
            # session (e.g. the local OpenClaw brain) or the engine is unreachable.
            # greet() spoke the matching line instead of greeting; let it play out, then
            # hang the caller up and tear the pipeline down so the line drops cleanly.
            # wait_until_delivered returns once the line has been spoken (or after a
            # short timeout if the engine can't even synthesize it — hang up either way).
            logger.info(f"STT unavailable for call {call_id} — playing the warning, "
                        "then hanging up")
            await session.followup_gate.wait_until_delivered()
            await transport.send_control({"type": "call.hangup"})
            await cancel_active_call("STT unavailable", call_id=call_id)

    @connection.event_handler("on_call_state")
    async def on_call_state(_connection, call_id, state):
        logger.info(f"call.state={state} (call {call_id})")
        # This handler is dispatched sync=True, INLINE in the connection's single
        # receive loop, so whatever it awaits is time the socket is not being read:
        # no control, and no caller audio either. The bring-up is by far the longest
        # thing here — model construction plus greet()'s 12s STT poll, plus the
        # can't-hear branch's wait_until_delivered — so it runs as a task and this
        # handler returns promptly. Teardown stays inline because ordering is
        # load-bearing (the STT slot must be freed before the next call claims it) and
        # it is bounded by cancel_active_call's 5s wait.
        #
        # See brain/formal/SipCall.tla: a blocked reader lets control events queue up,
        # and a stale `disconnected` dispatched after the backlog clears is exactly what
        # tore down the wrong call.
        if state == "confirmed":
            # Single active call in v0: evict any running pipeline (and any bring-up
            # still in flight) first, so the STT slot is free before ours connects.
            await cancel_setup("superseded by a new call")
            # No reclaim on THIS teardown: the bring-up below starts on the same event
            # loop immediately behind it, and gc + malloc_trim + empty_cache would stall
            # that caller's setup — with empty_cache free to contend the CUDA allocator
            # lock against the greeting's synth, the hazard MemoryReclaim documents. The
            # superseding call reclaims when IT ends, so nothing is lost.
            #
            # gateway_server writes this same rule as `if not slot_active()`. That guard
            # would be inert here: slot_active() reads a module global in agent_session
            # that only acquire_slot sets, and the SIP path never calls acquire_slot —
            # different process, different globals (see agent_session's SCOPE note), so
            # it is always False in this process and would skip nothing. The condition is
            # therefore put where this process actually knows it: at the one call site
            # that has a replacement call in hand.
            await cancel_active_call("superseded by a new call", reclaim=False)
            setup["call_id"] = call_id
            setup["task"] = asyncio.create_task(bring_up_call(call_id))
        elif state == "disconnected":
            # Both guarded by call_id: this may be a stale disconnected for a call that
            # has already been superseded, in which case it must touch nothing.
            await cancel_setup("call disconnected during bring-up", call_id=call_id)
            await cancel_active_call("call disconnected", call_id=call_id)

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
        # EOF (or any exit): abandon any bring-up, tear down any running call, and
        # close the socket. The bring-up first — it is the thing that could otherwise
        # publish a new active call after we tore the old one down.
        await cancel_setup("connection closed")
        await cancel_active_call("connection closed")
        connection.close()
    logger.info("SIP brain stopped")


def main():
    parser = argparse.ArgumentParser(description="teaport SIP brain client (M2)")
    parser.add_argument("--socket", default=os.getenv("TEAPORT_SIP_SOCKET", DEFAULT_UDS_PATH),
                        help="gateway UDS path (default: the live /run/teaport/teaport-sip.sock)")
    args = parser.parse_args()
    logger.info("Priming TTS service...")
    make_tts()  # warm the engine TTS client once at startup
    asyncio.run(run(args.socket))


if __name__ == "__main__":
    main()
