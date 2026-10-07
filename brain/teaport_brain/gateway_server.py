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
import asyncio
import json
import logging
import os
import re
import sys
import time

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from loguru import logger

from pipecat.frames.frames import OutputTransportMessageUrgentFrame
from pipecat.pipeline.runner import PipelineRunner
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from teaport_brain.agent_session import (
    acquire_slot,
    build_agent_session,
    slot_active,
)
from teaport_brain import agent_backend, audio_dump, config_ui, reply_hold, sdnotify, wake_gate
from teaport_brain.gateway_serializer import (
    PIPELINE_SAMPLE_RATE,
    RELAY_SAMPLE_RATE,
    TeaportGatewaySerializer,
)
from teaport_brain.captions import sends_every_final
from teaport_brain.tools import parse_client_features
from teaport_brain.memory_hygiene import turn_reclaim
from teaport_brain.services import make_tts

LISTEN_PORT = int(os.getenv("GATEWAY_PORT", "7861"))
# Shared secret for /talk. When set, a client must present it as ?token=<value> on
# the WebSocket URL (the teaport-realtime plugin sends its TEAPORT_GATEWAY_TOKEN
# env / provider-config token); a missing or wrong token is rejected BEFORE the
# pipeline — and before the single-slot eviction — runs. When unset, anyone who can
# reach this port gets a full agent session (memory read/write tools included) and
# can evict the live call, so we log a loud warning at startup.
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")
# The close code a /talk session gets when a newer one evicts it (4000-4999 is the
# private range). Every other end — the session's own end, the idle timeout, a brain
# shutdown — closes with 1000 or drops the socket, so a client can tell "another client
# took the slot" apart and back off (the local audio bridge does: local_audio.py).
# Clients that predate it see a plain close, as before.
TAKEN_CLOSE_CODE = 4001
TAKEN_CLOSE_REASON = "taken by another Talk session"
# The mic path's asleep session (wake words, wake_gate.py) closed for a phone call: the
# call outranks the room, and the engine has one STT slot. The bridge stays off until the
# brain reports no call (/talk/status "call").
YIELD_CLOSE_CODE = 4002
YIELD_CLOSE_REASON = "a phone call needs the speech engine"
# A wake session whose STT could not connect (the reason: "busy" or "unavailable"). It
# ends quietly -- a box asleep in a room says nothing -- and the bridge stays deaf and
# retries, never falling back to listening without its wake words.
STT_CLOSE_CODE = 4003
# A phone call is live while the SIP brain has said so within this (POST /talk/call,
# which it repeats every CALL_REFRESH_SECS for the length of the call). A lease, not a
# flag: a SIP brain that dies mid-call leaves no call behind for longer than this.
CALL_LEASE_SECS = 30.0
_call_until = 0.0
# The wake sessions running now: websocket -> (WakeGate, AgentSession). A phone call
# closes the asleep ones (/talk/call).
_wake_sessions: dict = {}


def call_live() -> bool:
    return time.monotonic() < _call_until


def _close_with(websocket, code: int, reason: str) -> None:
    """Make the transport's own close of `websocket` (pipecat closes it as the ending
    pipeline is cancelled, with no code) send `code`. Closing it here instead would race
    that close: the second one raises in starlette."""
    close = websocket.close

    async def close_coded(*_args, **_kwargs):
        await close(code=code, reason=reason)

    websocket.close = close_coded


def _close_as_taken(websocket) -> None:
    _close_with(websocket, TAKEN_CLOSE_CODE, TAKEN_CLOSE_REASON)


def _qp_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
    # `captions` is the caption protocol the plugin speaks (captions.sends_every_final).
    qp = websocket.query_params
    features = parse_client_features(qp.get("features"))
    # The mic path's wake words (?wake=, the local audio bridge; wake_gate.py): the
    # session opens asleep, and nothing it hears goes anywhere before one is said.
    # ?awake=1 is restart_session's fresh conversation, awake from the start. Only a
    # client at the box (?features=local) keeps its conversation between sessions; any
    # other gets a store of its own, so no client is ever handed another's context.
    gate = None
    if qp.get("wake") is not None:
        awake = qp.get("awake") == "1"
        gate = wake_gate.WakeGate(
            wake_gate.parse_phrases(qp.get("wake") or ""), asleep=not awake,
            conversation_secs=_qp_float(qp.get("conversation_secs"), 7200.0),
            store=wake_gate.MIC if "local" in features else wake_gate.MicConversation())
        if awake:
            gate.store.clear()  # a fresh conversation, by request
        if call_live() and gate.asleep:
            # A phone call has the engine: a room listener now would take its STT slot.
            await websocket.close(code=YIELD_CLOSE_CODE, reason=YIELD_CLOSE_REASON)
            return
    # The caller-audio tap, as on the phone path (sip_server): first in the pipeline, so
    # it records the PCM the VAD and STT go on to see. Off unless TEAPORT_AUDIO_DUMP
    # names a directory. Talk sessions have no call id, so the UTC start time stands in,
    # prefixed so they are told apart from phone calls (caller-talk-<time>.wav). Never on
    # a wake session: the room before a wake word is not to be kept, in any form.
    input_procs = None
    if audio_dump.ENABLED and gate is None:
        input_procs = [audio_dump.CallerAudioTap(
            "talk-" + time.strftime("%Y%m%d%H%M%S", time.gmtime()) + f"{time.time() % 1:.6f}"[2:],
            sample_rate=PIPELINE_SAMPLE_RATE)]
    session = build_agent_session(
        transport, voice=qp.get("voice"), language=qp.get("language"),
        caption_every_final=sends_every_final(qp.get("captions")),
        context_notes=True,
        # What the client can do itself (the local audio bridge: volume, restart);
        # the client tools that need it are offered only then (tools.py, THE TOOL CONTRACT).
        client_features=features,
        input_processors=input_procs,
        # Off by default on Talk until measured there (reply_hold.TALK_ENABLED).
        reply_hold_enabled=reply_hold.TALK_ENABLED,
        wake_gate=gate,
        # A wake session asleep for hours is not idle: the bridge ends it (keep-alive).
        cancel_on_idle_timeout=False if gate is not None else None,
    )

    # What this brain accepts beyond audio, so the plugin can tell a Talk client
    # (teaport.talk.capabilities) before it sends anything. It goes out at once, on the
    # raw socket: before acquire_slot (up to ~5 s waiting out the previous session's
    # teardown) and the pipeline start, which could otherwise hold it past the plugin's
    # wait for a hello and have a working brain taken for one without context notes.
    # It does not mean notes can be sent yet; "ready" says that (below). Nothing else
    # writes to the socket until the pipeline runs. A plugin that predates context
    # notes ignores both types. The brain and the plugin ship together: a plugin from
    # before "ready" took the hello for it and sent notes into a session not yet up.
    await websocket.send_text(json.dumps({
        "type": "hello",
        "features": {"context": session.client_notes.limits()},
    }))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        if gate is not None:
            logger.info("local audio bridge connected — " + (
                f"asleep, listening for {len(gate.phrases)} wake word(s)" if gate.asleep
                else "awake (a fresh conversation), greeting"))
            await session.greet(speak=False, hello=not gate.asleep)
            if session.should_end:
                # Quietly (see STT_CLOSE_CODE): the bridge stays deaf and retries.
                _close_with(websocket, STT_CLOSE_CODE, session.end_reason or "unavailable")
                await session.task.cancel()
                return
            await session.task.queue_frames(
                [OutputTransportMessageUrgentFrame(message={"type": "ready"})])
            return
        logger.info("OpenClaw relay client connected — greeting")
        await session.greet()
        if session.should_end:
            # greet() spoke the can't-hear line instead of greeting. Nothing read this
            # before, so the session stayed up: the client saw a connected assistant
            # that answered once and then never again, while holding _active_session.
            # docs/CONFIG.md and docs/FAQ.md both promise the session ends here, and
            # the SIP path already does it (hangs the caller up). Let the line play,
            # then end the pipeline — the client sees a clean disconnect. It never says
            # "ready", so the plugin keeps notes out of a session being torn down.
            logger.info("STT unavailable — ending the session after the warning plays")
            await session.followup_gate.wait_until_delivered()
            await session.task.cancel()
            return
        # The plugin sends notes only after this: the pipeline is running and the STT
        # works, so a note is answered at once and lands in a session that will last.
        await session.task.queue_frames([OutputTransportMessageUrgentFrame(message={"type": "ready"})])

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
    _my_done, release = await acquire_slot(session.task, on_evicted=lambda: _close_as_taken(websocket))
    if gate is not None:
        _wake_sessions[websocket] = (gate, session)
    try:
        await PipelineRunner(handle_sigint=False).run(session.task)
    finally:
        await release()
        if gate is not None:
            _wake_sessions.pop(websocket, None)
            gate.ended()  # the mic conversation, kept for the next wake (wake_gate.py)


app = FastAPI()
# The config page + its JSON routes ride this app rather than a process of their
# own: on the 8 GB box a second Python service is memory the engine reserve
# wants. They share /talk's GATEWAY_TOKEN. See config_ui.py.
app.include_router(config_ui.router)


@app.get("/health")
async def health():
    return {"ok": True, "tts": "engine"}


@app.get("/talk/status")
async def talk_status(request: Request):
    """Whether a /talk session holds the slot now. The local audio bridge, evicted by
    another client, polls it to stay off the box while that session is live. Same
    token as /talk (and the config page, whose check this is): who is talking is
    nobody else's business. The bridge sends it as a bearer header, out of the access log."""
    config_ui._authorize(request)
    return {"active": slot_active(), "call": call_live()}


# How long /talk/call waits for a yielded room listener to let the engine's slot go.
YIELD_WAIT_SECS = 4.0


@app.post("/talk/call")
async def talk_call(request: Request):
    """The SIP brain's word that a phone call is up ({"state": "start"}, repeated as
    "refresh" while it lasts) or over ("end"). One conversation at a time, and a call
    outranks the room (issue #58): the mic path's asleep session is closed and its STT
    slot freed before this answers, so the call's STT connects; while a call is live no
    new one opens, and /talk/status says "call" so the bridge stays off. A mic
    conversation that is AWAKE is not cut off: the call gets the busy line, as before
    -- the hold/decline prompt for that is #58's."""
    global _call_until
    config_ui._authorize(request)
    try:
        state = (await request.json()).get("state")
    except Exception:  # noqa: BLE001 — a body that is not a JSON object
        state = None
    if state == "end":
        _call_until = 0.0
        return {"call": False, "yielded": 0}
    _call_until = time.monotonic() + CALL_LEASE_SECS
    asleep = [(ws, s) for ws, (g, s) in list(_wake_sessions.items()) if g.asleep]
    for ws, s in asleep:
        logger.info("phone call — closing the asleep mic session to free the speech engine")
        _close_with(ws, YIELD_CLOSE_CODE, YIELD_CLOSE_REASON)
        await s.task.cancel()
    if asleep:
        deadline = time.monotonic() + YIELD_WAIT_SECS
        while slot_active() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
    return {"call": True, "yielded": len(asleep)}


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


class _ReadyServer(uvicorn.Server):
    """uvicorn.Server that tells systemd it is ready once the port is bound.

    Not an app startup hook: uvicorn runs the ASGI lifespan startup BEFORE it creates
    the listening socket (Server.startup), so a hook would report ready while /talk and
    /health still refuse connections, and before a port already in use fails the start.
    `started` is set only once every listener is up; a bind failure exits instead."""

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            sdnotify.ready()


# A ?token= in a request line uvicorn logs: the access line of an HTTP request, the
# "WebSocket /talk?...&token=..." [accepted] line of every Talk connect.
_TOKEN_IN_URL = re.compile(r"(token=)[^&\s\"']+")


class _RedactTokens(logging.Filter):
    """Blank the token in uvicorn's request lines, so the journal never holds it. Only
    the arguments are rewritten: uvicorn's access formatter unpacks them by position."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_TOKEN_IN_URL.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                                for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_URL.sub(r"\1<redacted>", record.msg)
        return True


def redact_tokens_in_uvicorn_logs() -> None:
    """After uvicorn.Config, which (re)configures these loggers."""
    for name in ("uvicorn.access", "uvicorn.error"):
        log = logging.getLogger(name)
        if not any(isinstance(f, _RedactTokens) for f in log.filters):
            log.addFilter(_RedactTokens())


def serve(host: str, port: int) -> None:
    """uvicorn.run(app, host=, port=) with a readiness notification after the bind."""
    config = uvicorn.Config(app, host=host, port=port)
    redact_tokens_in_uvicorn_logs()
    server = _ReadyServer(config)
    try:
        server.run()
    except KeyboardInterrupt:  # as uvicorn.run: it re-raises a Ctrl-C after shutting down
        pass
    if not server.started:
        sys.exit(3)  # uvicorn.run's own exit status for a server that never started


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
    logger.info(agent_backend.startup_line())
    logger.info("Priming TTS service...")
    make_tts()  # warm the engine TTS client once at startup (G2P/synthesis are engine-side)
    logger.info(f"teaport OpenClaw relay server on ws://{args.host}:{args.port}/talk")
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
