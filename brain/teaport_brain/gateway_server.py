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
# The brain itself — pipeline, tools, greeting — lives in agent_session.py, shared with
# the SIP front-end (sip_server.py); who may hold the engine's one STT slot is the
# session arbiter's (session_arbiter.py). This module is just the OpenClaw WebSocket
# transport + FastAPI plumbing around them.
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
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from loguru import logger
from starlette.websockets import WebSocketState

from pipecat.frames.frames import (
    OutputTransportMessageUrgentFrame,
)
from pipecat.pipeline.runner import PipelineRunner
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from teaport_brain.agent_session import build_agent_session
from teaport_brain import agent_backend, audio_dump, config_ui, display, privacy, reply_hold, sdnotify, wake_gate, xvf_led
from teaport_brain import sip_server
from teaport_brain import session_arbiter as arb
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
# pipeline — and before the session arbiter — runs. When unset, anyone who can reach
# this port gets a full agent session (memory read/write tools included) and can
# replace a client's session by claiming its ?client= id, so we log a loud warning at
# startup.
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")
# The close codes a /talk session ends with when the session arbiter (session_arbiter.py)
# decides; 4000-4999 is the private range. Every other end — the session's own end, the
# idle timeout, a brain shutdown — closes with 1000 or drops the socket. Clients that
# do not know a code see a plain close. The local audio bridge (local_audio.py) acts on
# each; it keeps its own copies, since importing these would pull Pipecat into it.
#
# The slot went to another session by the policy: the same client reconnected (it
# replaces its own session), or this was a sleeping room mic and a Talk session took it.
TAKEN_CLOSE_CODE = 4001
TAKEN_CLOSE_REASON = "taken by another Talk session"
REPLACED_CLOSE_REASON = "replaced by the same client's new session"
REAPED_CLOSE_REASON = "the client stopped sending"
# The mic path's asleep session (wake words, wake_gate.py) closed, or refused, for a phone
# call: the call outranks the room, and the engine has one STT slot. The bridge stays off
# until the brain reports no call (/talk/status "call").
YIELD_CLOSE_CODE = 4002
YIELD_CLOSE_REASON = "a phone call needs the speech engine"
# A wake session whose STT could not connect (the reason: "busy" or "unavailable"). It
# ends quietly -- a box asleep in a room says nothing -- and the bridge stays deaf and
# retries, never falling back to listening without its wake words.
STT_CLOSE_CODE = 4003
# Refused: another conversation is live (the reason says which kind:
# session_arbiter.Refusal.reason). A client that asked to talk heard the busy line first.
BUSY_CLOSE_CODE = 4004
# A live Talk session ended because a phone call came in; its user heard why first.
CALL_CLOSE_CODE = 4005
CALL_CLOSE_REASON = "a phone call came in"
# A room mic without wake words counts as asleep again once nobody has spoken for this
# long (the bridge sends its own LOCAL_AUDIO_KEEPALIVE_SECS as ?keepalive=).
ROOM_KEEPALIVE_SECS = 45.0
# How long a Talk session a call ends may take to say so before it is closed anyway.
CALL_LINE_MAX_SECS = 8.0
# Set whenever the session arbiter's holder changes, so the busy lamp follows a call's
# start and end at once (_busy_lamp). Made by the lamp's own task, on the loop that
# waits on it.
_lamp_wake: asyncio.Event | None = None
LAMP_POLL_SECS = 1.0    # during a call: how soon a re-enumerated ring is lit again
LAMP_RETRY_SECS = 15.0  # after a failed write (logged once in xvf_led)


def call_live() -> bool:
    return arb.ARBITER.call_live()


async def _busy_lamp() -> None:
    """The XVF3800's LED ring as the phone's busy lamp (xvf_led.py): breathing red while
    a call rings (the SIP front-end's face, sip_server.CALL_FACE: until it is answered),
    solid red while a call holds the engine and is not ringing (call_live, the session
    arbiter's call claim), the
    room's own effect otherwise -- however the call ended: hung up, torn down with the SIP
    front-end, this brain stopping (the finally, after the SIP teardown:
    _ReadyServer.shutdown) or dying (the first pass of the next one restores the ring from
    xvf_led's marker). It follows those two and nothing else, woken by their changes, so
    this is the one place the lamp is driven from. During a call it asks every pass, not
    only on a change: busy() is idempotent, and a ring that re-enumerated mid-call (a
    replug, a reset) comes back unlit."""
    global _lamp_wake
    _lamp_wake = wake = asyncio.Event()
    face = sip_server.CALL_FACE
    arb.ARBITER.listeners.append(wake.set)  # sync, on this loop (SessionArbiter._set_holder)
    face.listeners.append(wake.set)         # likewise (sip_server's handlers, on this loop)
    # "unknown": the first pass sets the ring whatever it is (a dead brain's red goes).
    shown, retry_at = "unknown", 0.0
    try:
        while True:
            # Ringing first: a call the brain answers rings for a couple of seconds
            # after it has the engine (sip_server's ring head start).
            want = ("ringing" if face.state == display.RINGING else
                    "busy" if call_live() else None)
            if (want or want != shown) and time.monotonic() >= retry_at:
                # Off the loop: a few ms of USB, up to xvf_led.TIMEOUT_MS on a wedged device.
                if await asyncio.to_thread(xvf_led.busy, want is not None,
                                           ringing=want == "ringing"):
                    shown = want
                else:
                    retry_at = time.monotonic() + LAMP_RETRY_SECS
            try:
                await asyncio.wait_for(wake.wait(), LAMP_POLL_SECS)
            except asyncio.TimeoutError:
                pass
            wake.clear()
    finally:
        arb.ARBITER.listeners.remove(wake.set)
        face.listeners.remove(wake.set)
        _lamp_wake = None
        await asyncio.to_thread(xvf_led.busy, False)  # a no-op unless it is lit


def _close_with(websocket, code: int, reason: str) -> None:
    """Make the transport's own close of `websocket` (pipecat closes it as the ending
    pipeline is cancelled, with no code) send `code`. Closing it here instead would race
    that close: the second one raises in starlette."""
    close = websocket.close

    async def close_coded(*_args, **_kwargs):
        await close(code=code, reason=reason)

    websocket.close = close_coded


async def _refuse(websocket, refusal: arb.Refusal, *, speak: bool, room: bool,
                  voice: str | None = None, language: str | None = None) -> None:
    """End a /talk connection the arbiter refused. Nothing was built for it: no STT, no
    pipeline. A client that asked to talk sees the busy line's caption at once and hears
    it (synthesized once per voice, session_arbiter.busy_line_audio) before the close; a
    sleeping room mic, which asked nothing, is closed at once. The code: YIELD for the
    room during a call (the bridge waits the call out), BUSY otherwise (it backs off)."""
    code = YIELD_CLOSE_CODE if room and refusal.holder == arb.CALL else BUSY_CLOSE_CODE
    reason = YIELD_CLOSE_REASON if code == YIELD_CLOSE_CODE else refusal.reason
    if speak:
        try:
            line = arb.busy_line(refusal.holder, getattr(arb.tts_for(voice, language),
                                                         "espeak_language", None))
            # Framed as a response of its own: it comes before any mic audio, so the
            # client's side may have no turn to put it in yet, and OpenClaw's relay fails
            # a session on output nobody owns (plugin/provider.js, _onResponseMarker).
            await websocket.send_text(json.dumps({"type": "response", "state": "start"}))
            await websocket.send_text(json.dumps(
                {"type": "transcript", "role": "assistant", "text": line, "final": True,
                 "utterance": "busy"}))
            _line, pcm = await arb.busy_line_audio(refusal.holder, voice, language)
            if pcm:
                await websocket.send_bytes(pcm)
                # Played out before the close: a client may drop what it has not played.
                await asyncio.sleep(len(pcm) / (2 * RELAY_SAMPLE_RATE) + 0.5)
            await websocket.send_text(json.dumps({"type": "response", "state": "done"}))
        except Exception as e:  # noqa: BLE001 — the client left first
            logger.debug(f"busy line not delivered: {e!r}")
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 — already closed
        pass


def _qp_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def run_relay_bot(websocket: WebSocket):
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
    awake = qp.get("awake") == "1"
    if qp.get("wake") is not None:
        gate = wake_gate.WakeGate(
            wake_gate.parse_phrases(qp.get("wake") or ""), asleep=not awake,
            conversation_secs=_qp_float(qp.get("conversation_secs"), 7200.0),
            store=wake_gate.MIC if "local" in features else wake_gate.MicConversation())
    # Who is asking for the engine (session_arbiter.py): the box's own mic (?features=
    # local, the local audio bridge) is the room, any other client a Talk session, and
    # ?client= is the id it keeps across its own reconnects. A room session is asleep
    # while its wake gate is; without wake words, while nobody in the room has spoken yet
    # and again once nobody (user or bot) has for the bridge's keep-alive (?keepalive=,
    # its LOCAL_AUDIO_KEEPALIVE_SECS) -- the bridge greets an empty room at start, and
    # that must not hold the box against a Talk client for the idle timeout.
    room = "local" in features
    client = (qp.get("client") or "").strip()[:128] or None
    keepalive = _qp_float(qp.get("keepalive"), ROOM_KEEPALIVE_SECS)
    serializer = TeaportGatewaySerializer()
    session_holder: dict = {}

    def asleep() -> bool:
        if gate is not None:
            return gate.asleep
        if not room:
            return False
        s = session_holder.get("s")
        return s is None or room_idle(s.followup_gate, keepalive)

    # A call during this conversation asks it first (session_arbiter PROMPT, issue #111):
    # the session's CallPrompt puts the question and, on a yes, the hold. A session
    # without one (answer_phone_call switched off) cannot be asked.
    def prompt():
        return getattr(session_holder.get("s"), "call_prompt", None)

    async def ask(call):
        return await prompt().ask(call.caller)

    async def hold():
        await prompt().hold()

    async def resume():
        await prompt().resume()

    claim = arb.Claim(
        arb.ROOM if room else arb.TALK, client=client,
        label=("room mic" if room else "Talk session") + (f" ({client})" if client else ""),
        asleep=asleep,
        gone=lambda: _client_gone(websocket, serializer),
        end=lambda why: _end_for(websocket, session_holder.get("s"), why, room=room,
                                 asleep=asleep()),
        ask=ask, can_ask=lambda: prompt() is not None, hold=hold, resume=resume)
    # A sleeping wake-word room asked nothing; a dial without wake words is a voice in the
    # room, and is told.
    speak_refusal = not (gate is not None and gate.asleep)
    refusal = arb.ARBITER.would_refuse(claim)
    if refusal is not None:
        await _refuse(websocket, refusal, speak=speak_refusal, room=room,
                      voice=qp.get("voice"), language=qp.get("language"))
        return
    if gate is not None and awake:
        gate.store.clear()  # a fresh conversation, by request
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
            audio_out_sample_rate=RELAY_SAMPLE_RATE,
            add_wav_header=False,
            serializer=serializer,
        ),
    )
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
        # Talk's own turn-taking (TALK_* over the shared knobs; endpointing.turn_settings).
        front_end="talk",
    )
    session_holder["s"] = session

    # What this brain accepts beyond audio, so the plugin can tell a Talk client
    # (teaport.talk.capabilities) before it sends anything. It goes out at once, on the
    # raw socket: before the arbiter's grant (up to ~5 s waiting out a session it ends)
    # and the pipeline start, which could otherwise hold it past the plugin's
    # wait for a hello and have a working brain taken for one without context notes.
    # It does not mean notes can be sent yet; "ready" says that (below). Nothing else
    # writes to the socket until the pipeline runs. A plugin that predates context
    # notes ignores both types. The brain and the plugin ship together: a plugin from
    # before "ready" took the hello for it and sent notes into a session not yet up.
    # "wake": a wake session says it is gating (asleep or awake). The local audio bridge
    # sends no mic audio until it reads this, so a brain that predates wake words --
    # which would greet the room and hear everything -- is never fed the room.
    hello = {"type": "hello", "features": {"context": session.client_notes.limits()}}
    if gate is not None:
        hello["wake"] = "asleep" if gate.asleep else "awake"
    await websocket.send_text(json.dumps(hello))

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
            # that answered once and then never again, while holding the slot.
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

    # --- The session arbiter -------------------------------------------------
    # One conversation at a time (session_arbiter.py). The grant waits out a session it
    # ends (a sleeping room mic, or this client's own previous connection -- a frozen
    # one leaves its pipeline running and on_client_disconnected never fires), so our
    # STT finds the engine free. A refusal here means the box got busy while this
    # session was being built: nothing has run yet, so it is refused like any other.
    refusal = await arb.ARBITER.acquire(claim)
    if refusal is not None:
        await _refuse(websocket, refusal, speak=speak_refusal, room=room,
                      voice=qp.get("voice"), language=qp.get("language"))
        return
    try:
        await PipelineRunner(handle_sigint=False).run(session.task)
    finally:
        arb.ARBITER.release(claim)
        if gate is not None:
            gate.ended()  # the mic conversation, kept for the next wake (wake_gate.py)


def room_idle(followup_gate, keepalive: float) -> bool:
    """A room session without wake words counts as asleep: nobody has taken a turn yet,
    or nobody (user or bot) has spoken for `keepalive` seconds and nothing is under way
    -- no turn, and no consult answer still owed (as the bridge's own keep-alive)."""
    if not getattr(followup_gate, "user_heard", False):
        return True
    return (followup_gate.is_clear() and not getattr(followup_gate, "owed", 0)
            and time.monotonic() - followup_gate.last_active >= keepalive)


def _client_gone(websocket, serializer) -> bool:
    """Has this session's client already left? Its socket is closed, or it has sent
    nothing (not even its mic's silence) for session_arbiter.STALE_SECS."""
    if WebSocketState.DISCONNECTED in (getattr(websocket, "client_state", None),
                                       getattr(websocket, "application_state", None)):
        return True
    return time.monotonic() - serializer.last_rx >= arb.STALE_SECS


async def _end_for(websocket, session, why: str, *, room: bool, asleep: bool) -> None:
    """A /talk session's end when the arbiter gives its slot to another (Claim.end).
    Silent only where nothing is lost: a client replacing its own session, one already
    gone, or a room mic that was asleep. A live Talk session a phone call ends is told
    first."""
    if why == arb.REPLACED:
        _close_with(websocket, TAKEN_CLOSE_CODE, REPLACED_CLOSE_REASON)
    elif why == arb.REAPED:
        logger.info("session arbiter: the client of the session holding the engine is gone "
                    "— ending it")
        _close_with(websocket, TAKEN_CLOSE_CODE, REAPED_CLOSE_REASON)
    elif why == arb.CALL_IN and (room or asleep):
        logger.info("phone call — closing the asleep mic session to free the speech engine")
        _close_with(websocket, YIELD_CLOSE_CODE, YIELD_CLOSE_REASON)
    elif why == arb.CALL_IN:
        logger.info("phone call — telling the Talk session, then ending it")
        if session is not None:
            await session.say_last_line(arb.call_line(
                getattr(session.tts, "espeak_language", None)), CALL_LINE_MAX_SECS)
        _close_with(websocket, CALL_CLOSE_CODE, CALL_CLOSE_REASON)
    else:  # TAKEN: a sleeping room, and a Talk session wants to talk
        _close_with(websocket, TAKEN_CLOSE_CODE, TAKEN_CLOSE_REASON)
    if session is not None:
        await session.task.cancel()
    else:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@asynccontextmanager
async def _lifespan(_app):
    lamp = asyncio.create_task(_busy_lamp())
    try:
        yield
    finally:
        lamp.cancel()
        try:
            await lamp
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=_lifespan)
# The config page + its JSON routes ride this app rather than a process of their
# own: on the 8 GB box a second Python service is memory the engine reserve
# wants. They share /talk's GATEWAY_TOKEN. See config_ui.py.
app.include_router(config_ui.router)


@app.get("/health")
async def health():
    # "sip": the SIP front-end's state (sip_server.status): off, waiting for a gateway,
    # or connected to one. `teaport sip status` and `teaport doctor` read it.
    return {"ok": True, "tts": "engine", "sip": sip_server.status()}


@app.get("/talk/status")
async def talk_status(request: Request):
    """Who holds the speech engine now (session_arbiter.ARBITER.status): "active" while
    any session does, "call" while a phone call does, "live" while any conversation is
    (anything but a sleeping room mic), "holder" its kind (talk, room or call),
    "asleep" for a sleeping room mic and "held" the kind of a conversation on hold for a
    call (or null). "call" is true from the moment a call asks for the engine, not only
    once it has it. The local audio bridge, refused or
    ended, polls it to stay off the box while that lasts. Same token as /talk (and the
    config page, whose check this is): who is talking is nobody else's business. The
    bridge sends it as a bearer header, out of the access log."""
    config_ui._authorize(request)
    return arb.ARBITER.status()


@app.websocket("/talk")
async def talk(websocket: WebSocket):
    # Auth BEFORE anything else: an unauthenticated socket must never reach the session
    # arbiter — otherwise any host that can reach this port can use the agent (and its
    # memory read/write tools), or claim a client's id and replace its session.
    if GATEWAY_TOKEN and not config_ui.token_matches(
            websocket.query_params.get("token"), GATEWAY_TOKEN):
        logger.warning("rejected /talk client: missing or bad token")
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        await run_relay_bot(websocket)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"relay session error: {e}")
    finally:
        # Skip the reclaim when another session already holds the engine: it may be
        # mid-greeting on this same event loop, and gc+malloc_trim+empty_cache here
        # would stall its audio — and empty_cache can contend the CUDA allocator
        # lock against its in-flight synth (the hazard MemoryReclaim's docstring
        # documents). The replacement's own session-end reclaim covers the memory.
        if not arb.ARBITER.held():
            turn_reclaim()
        else:
            logger.info("session-end reclaim skipped — another session holds the engine")


class _ReadyServer(uvicorn.Server):
    """uvicorn.Server that tells systemd it is ready once the port is bound.

    Not an app startup hook: uvicorn runs the ASGI lifespan startup BEFORE it creates
    the listening socket (Server.startup), so a hook would report ready while /talk and
    /health still refuse connections, and before a port already in use fails the start.
    `started` is set only once every listener is up; a bind failure exits instead."""

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            # The phone line's front-end, in this same event loop (sip_server.serve): it
            # waits for the gateway's socket, and its calls go through the same session
            # arbiter as /talk. A failure in it is contained there; Talk goes on.
            self.sip = asyncio.create_task(sip_server.serve())
            sdnotify.ready()

    async def shutdown(self, sockets=None):
        sip = getattr(self, "sip", None)
        if sip is not None:
            # A live call is torn down (the gateway keeps the caller and replays the call
            # to the next brain that connects).
            sip.cancel()
            await asyncio.gather(sip, return_exceptions=True)
        await super().shutdown(sockets=sockets)


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
    # Before anything logs: no caller id reaches the journal (privacy.py).
    privacy.install()
    parser = argparse.ArgumentParser(
        description="teaport OpenClaw gateway-relay voice server"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    args = parser.parse_args()
    if not GATEWAY_TOKEN:
        logger.warning(
            "GATEWAY_TOKEN is not set — /talk is UNAUTHENTICATED: anyone who can "
            "reach this port can use the agent (and its memory tools) and replace "
            "a client's session. Set GATEWAY_TOKEN (server) + TEAPORT_GATEWAY_TOKEN "
            "(plugin) except on a trusted network."
        )
    logger.info(agent_backend.startup_line())
    logger.info("Priming TTS service...")
    make_tts()  # warm the engine TTS client once at startup (G2P/synthesis are engine-side)
    logger.info(f"teaport OpenClaw relay server on ws://{args.host}:{args.port}/talk")
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
