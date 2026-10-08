# SPDX-License-Identifier: MIT
#
# teaport — the SIP front-end: the real agent behind the teaport-sip gateway.
#
# Runs inside teaport-brain, in the same event loop as the /talk WebSocket
# (gateway_server.py starts serve()), and asks the same session arbiter
# (session_arbiter.py) for the engine before each call (issue #58: one brain process,
# several front-ends). The SIP-over-UNIX-socket sibling of the /talk front-end. Same brain — both front-ends now build it through the shared factory
# agent_session.build_agent_session(): persona + VAD + smart-turn endpointing +
# heard-grounding ledger + memory recall/reclaim + captions + turn-timing taps +
# the degenerate-text guards + tools. The only difference is the transport:
# SipGatewayTransport over the teaport-sip AF_UNIX SOCK_SEQPACKET socket
# (sip_transport.py) instead of a FastAPI WebSocket. A phone call at the gateway
# reaches Voxtral STT -> LLM -> Kokoro TTS instead of a stub echo.
#
# Lifecycle (M2, per-call): the UDS connection is PERSISTENT (a SipConnection owns
# the socket + receive loop + call-control dispatch for as long as the gateway is up;
# serve() connects again when it comes back), but each
# CALL gets a FRESH pipeline: a new SipGatewayTransport + AgentSession (fresh
# STT/LLM/TTS/context), run as a background task; on call.state=disconnected we cancel
# that task, tearing the per-call transport down — which frees the engine's single STT
# slot and recycles all per-call state (the next caller builds fresh, so no context
# reset is needed). Single active call in protocol v0: a new call cancels any running
# call first. If the gateway socket closes (EOF), we cancel any running call and look
# for it again.
#
# Ringing with a head start (issue #111). The brain answers the call, not the gateway
# (teaport-sip.conf auto_answer=false, which `teaport sip configure` writes): on
# call.incoming the caller hears it ring while the brain asks the session arbiter for
# the engine -- which may mean asking a live conversation first (call_prompt.py) --
# builds the pipeline and has the model word its greeting (AgentSession.
# prepare_greeting). SIP_ANSWER_AFTER_SECS after the call came in (a couple of rings),
# and once the greeting is ready (or _ANSWER_GRACE_SECS later at most), it sends
# call.answer, and on `confirmed` the greeting plays at once. A call the conversation
# said not to pick up is never answered: the caller hears it ring until they give up,
# and no busy line, no hangup. A refused call (an awake room that cannot be asked) is
# answered to hear the busy line, then hung up.
#
# A gateway that answers by itself (auto_answer=true: an older conf) sends `confirmed`
# without our call.answer, and gets the behaviour from before #111: no head start, the
# greeting asked for at once, and a call that was to keep ringing gets the busy line
# instead (it can no longer ring). A `confirmed` with no call.incoming before it is
# built the same way.
#
# Mid-call (re)connect: a gateway that resynchronizes on connect (teaport-sip's
# reconnect replay) keeps the call up while this process is gone, and when the next
# brain connects it replays the call in progress — call.incoming + call.state with
# "replay": true — before any audio. A replayed `confirmed` needs nothing special to
# build: it is a confirmed like any other, so the caller gets a fresh pipeline. It only
# changes the FIRST thing said: the caller has been in silence for a few seconds and the
# old process took the conversation with it, so the session opens with a short "sorry, I
# lost you for a moment, where were we?" (AgentSession.greet(resumed=True)) instead of a
# hello from scratch. The caller-audio dump, if on, starts a new caller-<id>.r1 file
# rather than truncating the first process's (audio_dump.py). A replayed call still
# ringing (`incoming`, or `early` once the gateway sends 180 Ringing) waits for our
# call.answer, so it rings on from here like a live one. A replayed `connecting` is
# ignored, and the live `confirmed` that follows greets normally, since that caller has
# heard nothing yet. An older gateway never sends the flag, and nothing changes.
#
# This mirrors the OpenClaw path (gateway_server.py), which already builds a fresh
# pipeline per WebSocket connection. The earlier one-persistent-pipeline design (reset
# context between calls, cancel_on_idle_timeout=False to keep it alive) is gone.
#
# Standalone, for the brain/test rigs only (its arbiter is its own, so it does not see
# the box's Talk sessions):
#   python -m teaport_brain.sip_server [--socket /run/teaport/teaport-sip.sock]
#   Requires the engine at TEAPORT_URL (STT/TTS) and an LLM at LLM_BASE_URL.
#
# The SIP path drives the SAME shared pipeline the OpenClaw path does
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
import re
import socket
import time
import urllib.parse

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.pipeline.runner import PipelineRunner

from teaport_brain import reply_hold
from teaport_brain import session_arbiter as arb
from teaport_brain.agent_session import build_agent_session
from teaport_brain.env import env_flag, env_num
from teaport_brain import agent_backend, audio_dump, display, sdnotify
from teaport_brain.memory_hygiene import turn_reclaim
from teaport_brain.services import make_tts
from teaport_brain.sip_serializer import (
    BYTES_PER_FRAME,
    PIPELINE_SAMPLE_RATE,
    SipProtocolSerializer,
    encode_audio,
)
from teaport_brain.sip_transport import (
    DEFAULT_UDS_PATH,
    SipConnection,
    SipGatewayTransport,
    connect_seqpacket,
    make_sip_params,
)


# --- FALLBACK: half-duplex input gate (pre-AEC echo containment) ----------------
# Telephony has no client-side echo cancellation (unlike the OpenClaw Talk client
# the WS path relies on), so the bot's own audio echoes back down the line and
# retriggers the VAD/STT (the bot fights itself → rough conversation). This gate
# DROPS caller audio while the bot is speaking, plus a tail covering the gateway's
# bounded (~1 s) playout backlog. It was NEVER the real fix — that's an echo canceller
# in the teaport-sip bridge (pjmedia AEC), keeping the brain transport-agnostic — and
# as of 2026-09-09 that canceller exists and runs WebRTC on both boxes, so the gate is
# now the fallback for a bridge whose AEC would not initialise, not the default.
#
# DEFAULT OFF, changed 2026-09-09, because the safe direction inverted. While no bridge
# cancelled echo, ON was the conservative choice: better a bot that cannot be interrupted
# than one that fights itself. Now the risk is the other one, and it is silent — the gate
# drops the caller's mic for the whole of every reply plus the tail, so there is NO
# barge-in, and nothing in the journal says the call went that way.
#
# The 18-minute call of 2026-09-09 is the case for off. It recorded 37 barge-in cuts,
# twelve of them before a single word was heard — which the gate would have reduced to
# zero, and that would have been the wrong outcome twice over. Those cuts ARE the user
# interrupting: with the gate on, a reply that ran thirty-seven seconds runs all
# thirty-seven, and "Okay, stop." is dropped by the gate rather than heard. And the same
# cuts are the evidence for the endpointing and consult-delivery defects (#22, #26);
# suppressed, they leave a clean-looking ledger over a phone bot nobody can interrupt.
#
# Turn it back ON (SIP_HALF_DUPLEX=1) only when the gateway logs an AEC fallback —
# "webrtc EC create failed ... falling back to default sw EC", or aec=false. The brain
# is deliberately transport-agnostic and cannot see the gateway's AEC state, so this
# stays a manual knob. If AEC is merely weak rather than absent, reach for a longer
# aec_tail_ms in teaport-sip.conf before reaching for this.
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
#   starts, and the brain unit's Restart= (the old teaport-sip-brain unit's, back then)
#   crash-loop it forever, with no way to clear it short of hand-editing the file —
#   re-running the installer will not. env_num warns and falls back instead.
HALF_DUPLEX = env_flag("SIP_HALF_DUPLEX", False)
_HD_TAIL_S = env_num("SIP_HALF_DUPLEX_TAIL_S", "0.8", float)

# Caller-path makeup gain (dB) applied to the audio the transcriber sees, to recover the
# quiet speech a caller produces over the bot (~4 dB down within a call, and the segments
# the engine drops are the quietest). The bridge's echo canceller is NOT the cause; the
# rationale, the measurements and that correction live in one place, above _apply_makeup
# in stt.py. SIP-only, and read HERE rather than in make_stt so the Talk front-end --
# which shares this brain.env but does its own client-side AEC -- never picks it up.
# 0 = off. Measured 2026-09-11: +6 dB recovered the quiet barge-in "stop"s the engine was
# dropping with no regressions on 205 clips. Default off pending a live call to confirm.
STT_MAKEUP_DB = env_num("SIP_STT_MAKEUP_DB", "0", float)

# Ringing with a head start (see the header): how long after the call comes in the brain
# answers it, at the earliest -- a couple of rings, time the pipeline is built in and the
# greeting worded. 0: as soon as the pipeline is up. Only with the gateway's auto_answer
# off: a gateway that answers by itself has answered already.
ANSWER_AFTER_SECS = env_num("SIP_ANSWER_AFTER_SECS", "5", float)
# Past ANSWER_AFTER_SECS, how much longer the call may ring for the greeting to be ready;
# then it is answered anyway and the greeting asked for the usual way.
_ANSWER_GRACE_SECS = 3.0
# From call.answer to `confirmed`: a gateway that has not confirmed by then never will.
_CONFIRM_SECS = 10.0


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


# The gateway's socket. teaport-brain runs this front-end in its own event loop (serve,
# started by gateway_server): it looks for the socket every _SERVE_POLL_S while there is
# none -- telephony off, or the gateway (re)starting -- and reconnects whenever the gateway
# goes away, which a gateway with teaport-sip#3's reconnect replay survives mid-call.
# Empty: no SIP front-end at all.
SIP_SOCKET = os.getenv("TEAPORT_SIP_SOCKET", DEFAULT_UDS_PATH)
_SERVE_POLL_S = 2.0
# What serve() is doing, for /health (and so `teaport sip status` / doctor): "off" (not
# running, or TEAPORT_SIP_SOCKET empty), "waiting" (no gateway to talk to) or "connected".
_state = "off"


def status() -> str:
    return _state

# Standalone (`python -m teaport_brain.sip_server`, the brain/test rigs): how long to wait
# for the gateway to bind its socket before giving up. The gateway is Type=simple: it
# counts as started the moment it is forked, a second or more before PJSUA2 is up and the
# UDS is listening, so a connect on the first tick would fail for a socket about to
# appear. 30 s covers a cold gateway on a loaded box with room to spare.
_GATEWAY_WAIT_S = 30.0
_GATEWAY_POLL_S = 0.5


# --- The phone on the OLED face (display.CallFace) ---------------------------------------
# This front-end is the one owner of the face's call state, since it is where the call's
# lifecycle is known: ringing from call.incoming until the call is answered (the caller
# hears it ring until then: the session arbiter's grant, and with the brain answering,
# the ring head start), active from then until the call is over,
# and none at every end -- hung up, refused (busy), a bring-up that failed, a teardown,
# the gateway going away, the brain stopping (serve() cancelled). The busy lamp
# (gateway_server._busy_lamp) breathes red from here while it rings.
CALL_FACE = display.CallFace()

# Display names that stand in for a name the carrier does not have, or that say the
# caller withheld one: shown as the number instead, whenever the From URI carries one
# (the box works for its owner), or the handset alone when there is none.
_NO_NAME = frozenset({
    "", "WIRELESS CALLER", "UNKNOWN", "UNKNOWN CALLER", "UNKNOWN NAME", "UNAVAILABLE",
    "OUT OF AREA", "ANONYMOUS", "PRIVATE", "PRIVATE CALLER", "PRIVATE NUMBER",
    "RESTRICTED", "WITHHELD", "BLOCKED", "CALLER ID BLOCKED", "NO CALLER ID",
})
_NAME_ADDR = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|([^<]*?))\s*<([^>]*)>')


def caller_id(from_header) -> str | None:
    """Who a call says it is from, for the face and the question: the display name in
    its SIP From header unless that is a placeholder ("WIRELESS CALLER", "Anonymous",
    "Restricted", ...), else the number in the URI, readable (+1 346 234 8500 for an
    11-digit NANP number, as sent otherwise). None when there is no number to show
    (anonymous@anonymous.invalid, a URI user with no digits)."""
    if not isinstance(from_header, str):
        return None
    m = _NAME_ADDR.match(from_header)
    if m:
        name = re.sub(r"\\(.)", r"\1", m.group(1)) if m.group(1) is not None else m.group(2)
        uri = m.group(3)
    else:
        name, uri = "", from_header.split(";", 1)[0]
    name = " ".join(name.split())
    if name.upper() not in _NO_NAME and not re.fullmatch(r"\+?[\d\s().-]+", name):
        return name
    user, _, host = re.sub(r"^\s*(?:sips?|tel):", "", uri, flags=re.I).partition("@")
    user = urllib.parse.unquote(user.split(";", 1)[0]).strip()
    if host.split(";", 1)[0].lower().startswith("anonymous.invalid") or not re.search(r"\d", user):
        return None
    digits = re.sub(r"[\s().-]", "", user)
    if re.fullmatch(r"\+?1\d{10}", digits):
        d = digits.lstrip("+")
        return f"+1 {d[1:4]} {d[4:7]} {d[7:]}"
    return user or None


async def answer_busy(connection, holder_kind: str, call_id) -> None:
    """A call the session arbiter refused: the busy line (TTS only, the cached one
    session_arbiter.busy_line_audio shares with /talk), paced out to the gateway in its
    20 ms frames, then hang up. No pipeline, no STT. A hangup meanwhile cancels it."""
    import numpy as np
    import soxr

    line, pcm = await arb.busy_line_audio(holder_kind)
    logger.info(f"call {call_id} refused ({holder_kind} conversation has the box) — "
                "saying it is busy, then hanging up")
    if pcm:
        wire = soxr.resample(np.frombuffer(pcm, dtype=np.int16), 24000, PIPELINE_SAMPLE_RATE)
        data = wire.astype("<i2").tobytes()
        loop = asyncio.get_running_loop()
        start = loop.time()
        for n, i in enumerate(range(0, len(data) - BYTES_PER_FRAME + 1, BYTES_PER_FRAME)):
            await connection.send(encode_audio(data[i:i + BYTES_PER_FRAME]))
            # Real time: the gateway's playout queue is about a second deep.
            await asyncio.sleep(max(0.0, start + (n + 1) * 0.02 - loop.time()))
        await asyncio.sleep(0.3)  # the gateway's own backlog plays out
    await connection.send_control({"type": "call.hangup", "call_id": call_id})


class CallClaim:
    """A call's claim on the session arbiter (session_arbiter.py), the same one every
    front-end of this process goes through: start() before the call's STT connects, end()
    when the call is over (twice is harmless). A sleeping room mic yields to the call; a
    live conversation is asked first (and put on hold if it says yes) or, one that cannot
    be asked, told and ended (Talk) or left the box (an awake room) -- all before start()
    returns, so the call's STT finds the engine free. One call at a time, so one claim at
    a time."""

    def __init__(self):
        self._claim = None

    async def start(self, call_id, caller: str | None = None,
                    answered: asyncio.Event | None = None) -> "arb.Refusal | None":
        """None: the call has the engine. Otherwise the arbiter's Refusal: `declined`
        when the conversation said not to pick up (the call is to ring on), else the box
        is busy (the caller gets the busy line). `caller` is for the question; a set
        `answered` (the caller is connected) means no question: the call is taken."""
        claim = arb.Claim(arb.CALL, client="sip", label=f"phone call {call_id}",
                          end=lambda _why: self._release(claim), caller=caller,
                          answered=answered)
        # Held from before the ask: an end() while it waits (a Talk user is being told
        # about the call) releases it there, and the arbiter then installs nothing.
        self._claim = claim
        try:
            refusal = await arb.ARBITER.acquire(claim)
        except BaseException:
            # Abandoned (a hangup, a newer call): let it go here, since nothing else
            # will -- a conversation put on hold for it would otherwise stay held.
            if self._claim is claim:
                self._claim = None
            arb.ARBITER.release(claim)
            raise
        if refusal is not None:
            if self._claim is claim:
                self._claim = None
            logger.info(f"the session arbiter refused call {call_id}: a {refusal.holder} "
                        "conversation has the box"
                        + (" and said not to pick up" if refusal.declined else ""))
            return refusal
        return None

    async def _release(self, claim) -> None:
        # The arbiter gives the engine to the same client's next call: the SIP front-end
        # tears the old call down itself before it asks (on_call_state), so let go.
        arb.ARBITER.release(claim)
        if self._claim is claim:
            self._claim = None

    def end(self) -> None:
        if self._claim is not None:
            claim, self._claim = self._claim, None
            arb.ARBITER.release(claim)


class _Ring:
    """One call from the moment it is known (call.incoming, or a `confirmed` with none
    before it) until it is torn down: what its bring-up needs to answer it."""

    def __init__(self, call_id, caller: str | None, answered: bool):
        self.call_id = call_id
        self.caller = caller
        self.t0 = asyncio.get_running_loop().time()
        self.answered = asyncio.Event()  # `confirmed` seen
        if answered:
            self.answered.set()
        self.answer_sent = False         # our call.answer went out
        self.declined = False            # left ringing: the conversation said no


async def _connect_when_listening(sock_path: str):
    """connect_seqpacket, retried until the gateway is listening or _GATEWAY_WAIT_S is up.

    Only the 'not there yet' errors are retried: no socket file, a file nobody is
    accepting on, or a connect that timed out because the accept backlog was briefly
    saturated. A peer-uid PermissionError is a different gateway, not a slow one, and
    is raised at once.
    """
    deadline = time.monotonic() + _GATEWAY_WAIT_S
    waited = False
    while True:
        try:
            return await asyncio.to_thread(connect_seqpacket, sock_path)
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as e:
            if time.monotonic() >= deadline:
                logger.error(f"gateway not listening at {sock_path} after {_GATEWAY_WAIT_S:g}s ({e!r})")
                raise
            if not waited:
                logger.info(f"gateway not listening yet ({e.__class__.__name__}) — waiting up to "
                            f"{_GATEWAY_WAIT_S:g}s for it to bind {sock_path}")
                waited = True
            await asyncio.sleep(_GATEWAY_POLL_S)


async def serve(sock_path: str = SIP_SOCKET) -> None:
    """The SIP front-end inside teaport-brain, for the life of the process: connect to the
    gateway whenever its socket is there, run the connection until the gateway goes away,
    and look again. Telephony off is a socket that never appears: one stat every
    _SERVE_POLL_S. Never raises (but for cancellation): a SIP failure is logged and the
    front-end starts over, so it cannot take the Talk front-end down with it."""
    global _state
    if not sock_path:
        logger.info("SIP front-end off (TEAPORT_SIP_SOCKET is empty)")
        return
    said_absent = False
    try:
        await _serve(sock_path, said_absent)
    finally:
        _state = "off"


async def _serve(sock_path: str, said_absent: bool) -> None:
    global _state
    while True:
        _state = "waiting"
        if not os.path.exists(sock_path):
            if not said_absent:
                logger.info(f"SIP front-end: no gateway socket at {sock_path} (telephony off, "
                            f"or the gateway is starting) — looking every {_SERVE_POLL_S:g} s")
                said_absent = True
            await asyncio.sleep(_SERVE_POLL_S)
            continue
        try:
            sock = await asyncio.to_thread(connect_seqpacket, sock_path)
        except PermissionError as e:
            logger.error(f"SIP front-end: {e} — not connecting to it")
            await asyncio.sleep(60.0)
            continue
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as e:
            logger.debug(f"SIP front-end: gateway not accepting yet ({e!r})")
            await asyncio.sleep(_SERVE_POLL_S)
            continue
        said_absent = False
        _state = "connected"
        logger.info(f"SIP front-end: connected to the gateway at {sock_path}")
        try:
            await run_connection(sock)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — contained: Talk goes on, SIP starts over
            logger.exception("SIP front-end failed — reconnecting; the Talk front-end is unaffected")
        await asyncio.sleep(_SERVE_POLL_S)


async def run(sock_path: str):
    """Standalone: one connection, for the brain/test rigs (`python -m
    teaport_brain.sip_server`). teaport-brain runs serve() instead."""
    logger.info(f"connecting to teaport-sip gateway at {sock_path}")
    sock = await _connect_when_listening(sock_path)
    logger.info("connected — the brain is the socket client (gateway is the server)")
    await run_connection(sock, on_ready=sdnotify.ready)


async def run_connection(sock, on_ready=None):
    """Serve one gateway connection until the gateway goes away: control, audio, and a
    fresh pipeline per call. Every call ends with it."""

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

    # ON is the line worth noticing in a journal: it means the caller cannot barge in
    # for the whole of every reply, which is otherwise invisible. Say what that costs.
    logger.info(
        f"half-duplex input gate: ON (tail {_HD_TAIL_S}s) — NO barge-in; the caller's "
        f"mic is dropped while the bot speaks. Fallback for a bridge without AEC; "
        f"unset SIP_HALF_DUPLEX to restore barge-in"
        if HALF_DUPLEX else
        "half-duplex input gate: off — barge-in live; the bridge cancels echo (AEC)"
    )
    if STT_MAKEUP_DB:
        logger.info(f"STT makeup gain: +{STT_MAKEUP_DB:g} dB on the caller signal into the "
                    "transcriber, recovering quiet speech over the bot -- see stt.py")

    # Single active call in protocol v0. Holds (call_id, session, runner_task,
    # transport) for the currently-running per-call pipeline, or None between calls.
    # The call_id is load-bearing: without it a `disconnected` cannot tell whether it
    # is about the pipeline we are actually running (see on_call_state).
    active = {"call": None}
    # Teardowns started from inside the pipeline's own finish (see _bring_up's
    # on_pipeline_finished). Held only so the event loop keeps a strong reference until
    # they run — a bare create_task() result may be collected mid-flight.
    teardowns: set = set()
    # The in-flight bring-up, if any: {"task", "call_id"}. Building the pipeline and
    # greeting is SLOW (model construction plus greet()'s STT poll), so it runs as a
    # task rather than inline in the receive loop — see on_call_state.
    setup = {"task": None, "call_id": None}
    presence = CallClaim()
    # The current call (a _Ring), by call id: one at a time. And the callers of calls the
    # gateway replays, until the state that says what to do with them arrives.
    rings: dict = {}
    replayed: dict = {}
    # Said once per connection: the gateway answers calls itself (auto_answer=true).
    told = {"auto_answer": False}

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
        # next call's STT connects (mirrors session_arbiter.SETTLE_SECS).
        await asyncio.sleep(0.3)
        presence.end()
        CALL_FACE.end(_call_id)
        if reclaim and not arb.ARBITER.held():
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
        # "replay": true in the hello is the gateway saying it replays a call in
        # progress to a brain that (re)connects; one that predates that leaves a brain
        # restarted mid-call with no session for the call. Logged so whoever decides
        # whether this unit may restart alone can see which kind is running.
        logger.info(f"gateway hello: proto={msg.get('proto')} rate={msg.get('rate')} "
                    f"ch={msg.get('channels')} ptime={msg.get('ptime_ms')}ms "
                    + ("replay=yes (a call in progress survives a brain restart)"
                       if msg.get("replay") is True else
                       "replay=no (older gateway: a brain restart strands a call in progress)"))

    @connection.event_handler("on_call_incoming")
    async def on_call_incoming(_connection, msg):
        logger.info(f"call.incoming id={msg.get('call_id')} from={msg.get('from')} "
                    f"to={msg.get('to')}"
                    + (" (replayed: the call was already up when this brain connected)"
                       if msg.get("replay") is True else ""))
        caller = caller_id(msg.get("from"))
        call_id = msg.get("call_id")
        if msg.get("replay") is True:
            # What it is now comes with the replayed state (on_call_state): answered long
            # ago (the face goes straight to active once it has the engine again), or
            # still ringing.
            replayed[call_id] = caller
            return
        CALL_FACE.ringing(call_id, caller)
        current = active["call"]
        if (current is not None and current[0] != call_id
                and rings.get(current[0]) is not None and rings[current[0]].answered.is_set()):
            # A second INVITE over a call that is up (protocol v0 carries one). A gateway
            # since teaport-sip 0.6.0 turns it away itself (486) and never says so; an
            # older one passes it on, and then: the call in progress goes on, and this
            # one rings unanswered until its caller gives up -- its `disconnected` puts
            # the face back to the call still on the line. Answered by the gateway
            # itself (auto_answer=true), it replaces the first, as it always has
            # (on_call_state).
            logger.info(f"call {call_id} rings over call {current[0]}, which is up: "
                        "left ringing (one call at a time)")
            return
        # It rings from here: the bring-up asks for the engine, builds, and answers.
        await start_call(call_id, caller, answered=False)

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

    async def bring_up_call(call_id, resumed: bool = False):
        """Build the per-call pipeline and greet. Runs as a TASK, never inline in the
        receive loop — see on_call_state. `resumed`: the call was already up when this
        brain connected (a replayed confirmed), so greet() apologises for the gap
        instead of greeting from scratch."""
        logger.info(f"building a FRESH per-call pipeline for call {call_id} "
                    "(new STT/LLM/TTS/context)"
                    + (" — RESUMING a call already in progress" if resumed else ""))
        try:
            await _bring_up(call_id, resumed)
        except BaseException:
            # Failed, or abandoned (a hangup, a newer call, the line going) before it had
            # a pipeline to tear down, which would end it on the face: no call here any
            # more. A newer call's ringing is its own (end() is for this call only).
            if active["call"] is None or active["call"][0] != call_id:
                CALL_FACE.end(call_id)
            raise
        finally:
            # Retire our own entry once the bring-up is over, so `setup` means what its
            # name says: a bring-up STILL IN FLIGHT. Without this it stayed set for the
            # life of the call, and every ordinary hangup logged "abandoning the
            # in-flight bring-up" for one that had finished long before — observed on
            # the appliance 2026-08-28, 30s after the bring-up completed. Harmless in
            # effect (cancel_setup only cancels a task that is not done) but it puts a
            # lie in the journal at exactly the moment someone reads it to find out why
            # a call ended.
            #
            # Identity-checked on the TASK, not the call_id: a bring-up that has already
            # been superseded must not clear the replacement's entry.
            if setup["task"] is asyncio.current_task():
                setup["task"] = None
                setup["call_id"] = None

    async def answer(ring) -> bool:
        """Answer `ring` (unless the gateway has), and wait for `confirmed`. False when
        it never comes."""
        if not ring.answered.is_set():
            if not ring.answer_sent:
                ring.answer_sent = True
                logger.info(f"answering call {ring.call_id}")
                await connection.send_control({"type": "call.answer", "call_id": ring.call_id})
            try:
                await asyncio.wait_for(ring.answered.wait(), _CONFIRM_SECS)
            except asyncio.TimeoutError:
                logger.warning(f"call {ring.call_id}: no `confirmed` within "
                               f"{_CONFIRM_SECS:g} s of call.answer")
                return False
        return True

    async def ring_head_start(session, ring) -> str | None:
        """While the caller hears it ring: let it ring until SIP_ANSWER_AFTER_SECS after
        the call came in, and have the greeting worded meanwhile (up to
        _ANSWER_GRACE_SECS longer for it). The greeting's words, or None (it is asked for
        the usual way). Cut short by the gateway answering by itself."""
        loop = asyncio.get_running_loop()
        prep = asyncio.ensure_future(session.prepare_greeting())
        answered = asyncio.ensure_future(ring.answered.wait())
        try:
            left = ring.t0 + ANSWER_AFTER_SECS - loop.time()
            if left > 0:
                await asyncio.wait({answered}, timeout=left)
            if not prep.done() and not answered.done():
                await asyncio.wait({prep, answered}, timeout=_ANSWER_GRACE_SECS,
                                   return_when=asyncio.FIRST_COMPLETED)
        finally:
            answered.cancel()
            if not prep.done():
                prep.cancel()
                await asyncio.gather(prep, return_exceptions=True)
                if not ring.answered.is_set():
                    logger.info(f"call {ring.call_id}: the greeting was not ready in time "
                                "— answering, and asking for it after")
        if prep.cancelled():
            return None
        if prep.exception() is not None:
            logger.warning(f"call {ring.call_id}: the greeting could not be prepared "
                           f"({prep.exception()!r}) — asking for it after the answer")
            return None
        return prep.result()

    async def _bring_up(call_id, resumed: bool = False):
        ring = rings.get(call_id)
        if ring is None:  # not through start_call (a test driving this directly)
            ring = rings[call_id] = _Ring(call_id, None, answered=True)
        # Before anything takes the engine: the session arbiter makes way for the call
        # (asking a live conversation first), or refuses it.
        refusal = await presence.start(call_id, caller=ring.caller, answered=ring.answered)
        if refusal is not None:
            # No session is built for it. Building one (models, the STT connect) on this
            # event loop would stall the conversation that has the box, for a caller who
            # is only to hear one line, or nothing at all. Nor is it the box's call: the
            # phone comes down off the face.
            CALL_FACE.end(call_id)
            if refusal.declined and not ring.answered.is_set():
                # Don't pick up: never answered, never told busy -- it rings on until the
                # caller gives up (their `disconnected` clears it).
                ring.declined = True
                logger.info(f"call {call_id} left ringing: the {refusal.holder} "
                            "conversation said not to pick up")
                return
            if refusal.declined:
                logger.info(f"call {call_id}: the {refusal.holder} conversation said not to "
                            "pick up, but the gateway has answered it already "
                            "(auto_answer=true) — it cannot ring on, so it hears busy")
            if await answer(ring):
                await answer_busy(connection, refusal.holder, call_id)
            return
        if ring.answered.is_set():
            # Answered already (by the gateway): on the face, the box is on the phone.
            CALL_FACE.active(call_id)
        transport = SipGatewayTransport(connection, params)
        # Same shared brain as the OpenClaw path, minus barge-in when HALF_DUPLEX is on
        # (the input gate is the ONE SIP-specific processor). No cancel_on_idle_timeout
        # override: a per-call pipeline uses PipelineTask's default, exactly like the
        # OpenClaw per-connection pipeline.
        # The audio tap goes FIRST, so it records what the transport delivered rather
        # than what survived the gate — the question it exists to answer is about the
        # bytes arriving, and a capture taken downstream of a processor that can drop
        # frames would beg it. Off unless TEAPORT_AUDIO_DUMP names a directory.
        input_procs = []
        if audio_dump.ENABLED:
            input_procs.append(audio_dump.CallerAudioTap(call_id))
        if HALF_DUPLEX:
            input_procs.append(HalfDuplexInputGate())
        session = build_agent_session(
            transport,
            input_processors=input_procs or None,
            stt_makeup_db=STT_MAKEUP_DB,
            reply_hold_enabled=reply_hold.SIP_ENABLED,
            # The phone's own turn-taking (SIP_* over the shared knobs).
            front_end="sip",
        )

        @session.task.event_handler("on_pipeline_finished")
        async def on_pipeline_finished(_task, frame):
            """The pipeline ended and the CALLER did not: hang them up.

            A per-call pipeline can end without any call-control event behind it —
            agent_session ends it when the STT, the TTS or the output transport can no
            longer do its job (_end_session_when_unusable), pipecat ends it when the
            start/setup timeout fires, and the idle timeout can cancel it.
            None of those reach the gateway; the socket is persistent and the call stays
            up, so without this the caller is left on a line with no brain behind it —
            no audio, no hangup, and no further `confirmed` or `disconnected` ever
            coming, which is the same dead end brain/formal/SipCall.tla's NoWrongTeardown
            was written about.

            The guard is the ordinary case: our own teardown clears active["call"]
            BEFORE it cancels, so a pipeline finishing under cancel_active_call (or one
            belonging to a superseded call) finds nothing here and returns.

            `frame` is the terminal frame (End, Stop or Cancel) and is taken because
            pipecat PASSES it — `_call_event_handler("on_pipeline_finished", frame)`
            calls every handler as `handler(worker, frame)`, and a handler of the wrong
            arity does not raise where anyone would see it: BaseObject._run_handler
            catches the TypeError and logs one line, so the handler simply never runs
            and the caller is left on the dead line this exists to prevent."""
            call = active["call"]
            if call is None or call[0] != call_id:
                return
            logger.error(f"the pipeline for call {call_id} ended on its own on {frame} "
                         "(a service was written off, or a start/idle timeout fired) — "
                         "hanging the caller up rather than leaving them on a dead line")
            await transport.send_control({"type": "call.hangup", "call_id": call_id})
            # As a TASK, not inline: this handler is awaited BY the runner task, and
            # cancel_active_call waits on that same task — inline it would wait on
            # itself and only get past on the 5s timeout.
            t = asyncio.create_task(
                cancel_active_call("pipeline ended under the call", call_id=call_id))
            teardowns.add(t)
            t.add_done_callback(teardowns.discard)

        runner_task = asyncio.create_task(
            PipelineRunner(handle_sigint=False).run(session.task)
        )
        # Publish BEFORE greeting: from here a `disconnected` for this call can find
        # and tear down the pipeline even though the bring-up is still running.
        active["call"] = (call_id, session, runner_task, transport)
        prepared = None
        if not ring.answered.is_set():
            # Still ringing: the head start, then answer it.
            prepared = await ring_head_start(session, ring)
            if not await answer(ring):
                await transport.send_control({"type": "call.hangup", "call_id": call_id})
                await cancel_active_call("never confirmed", call_id=call_id)
                return
            # Answered: on the face, the box is on the phone (it rang until now).
            CALL_FACE.active(call_id)
        if prepared:
            await session.greet(resumed=resumed, prepared=prepared)
        else:
            await session.greet(resumed=resumed)
        if session.should_end:
            # STT is unavailable — the engine's single slot is held by another process
            # (a standalone rig) or the engine is unreachable. greet() spoke the
            # matching line instead of greeting; let it play out, then
            # hang the caller up and tear the pipeline down so the line drops cleanly.
            # wait_until_delivered returns once the line has been spoken (or after a
            # short timeout if the engine can't even synthesize it — hang up either way).
            logger.info(f"STT unavailable for call {call_id} — playing the warning, "
                        "then hanging up")
            await session.followup_gate.wait_until_delivered()
            await transport.send_control({"type": "call.hangup", "call_id": call_id})
            await cancel_active_call("STT unavailable", call_id=call_id)

    @connection.event_handler("on_call_state")
    async def on_call_state(_connection, call_id, state, replay=False):
        logger.info(f"call.state={state} (call {call_id})"
                    + (" (replayed on connect)" if replay else ""))
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
            ring = rings.get(call_id)
            if ring is not None and ring.answered.is_set():
                return  # said twice: nothing new
            if ring is not None:
                if not ring.answer_sent and not told["auto_answer"]:
                    told["auto_answer"] = True
                    logger.warning(
                        "the gateway answered a call by itself (teaport-sip.conf "
                        "auto_answer=true): no ring head start, and a call a conversation "
                        "says not to pick up hears busy instead of ringing on. Set "
                        "auto_answer=false and `teaport sip restart` (docs/CONFIG.md)")
                ring.answered.set()
                if ring.declined:
                    # Left ringing, and answered anyway: it cannot ring on any more.
                    t = asyncio.create_task(answer_busy(connection, arb.ROOM, call_id))
                    teardowns.add(t)
                    t.add_done_callback(teardowns.discard)
                return
            # A call with no ring before it: answered by the gateway (a replayed call,
            # or one whose call.incoming this brain missed). Built and greeted now.
            await start_call(call_id, replayed.pop(call_id, None), answered=True,
                             resumed=replay)
        elif state in ("incoming", "early") and replay and call_id not in rings:
            # Replayed still ringing: it waits for our call.answer, so it rings on here.
            caller = replayed.pop(call_id, None)
            CALL_FACE.ringing(call_id, caller)
            await start_call(call_id, caller, answered=False)
        elif state == "disconnected":
            # Both guarded by call_id: this may be a stale disconnected for a call that
            # has already been superseded, in which case it must touch nothing.
            await cancel_setup("call disconnected during bring-up", call_id=call_id)
            await cancel_active_call("call disconnected", call_id=call_id)
            if active["call"] is None and setup["task"] is None:
                presence.end()  # a call that never got its pipeline
            CALL_FACE.end(call_id)  # hung up while ringing, or refused and gone
            ring = rings.get(call_id)
            if ring is not None and ring.declined:
                logger.info(f"call {call_id}: the caller gave up (it was left ringing)")
            rings.pop(call_id, None)
            replayed.pop(call_id, None)

    async def start_call(call_id, caller, *, answered: bool, resumed: bool = False):
        """A call to bring up: ringing (answered=False, it waits for our call.answer) or
        already answered by the gateway."""
        # Single active call in v0: evict any running pipeline (and any bring-up still in
        # flight) first, so the STT slot is free before ours connects.
        await cancel_setup("superseded by a new call")
        # No reclaim on THIS teardown: the bring-up below starts on the same event loop
        # immediately behind it, and gc + malloc_trim + empty_cache would stall that
        # caller's setup — with empty_cache free to contend the CUDA allocator lock
        # against the greeting's synth, the hazard MemoryReclaim documents. The
        # superseding call reclaims when IT ends, so nothing is lost.
        #
        # gateway_server writes this same rule as `if not ARBITER.held()`. That guard
        # alone would not do here: the old call has let the arbiter go and the new one
        # has not taken it yet, so it reads False and would skip nothing. The condition
        # is therefore put where this process actually knows it: at the one call site
        # that has a replacement call in hand.
        await cancel_active_call("superseded by a new call", reclaim=False)
        rings.clear()
        rings[call_id] = _Ring(call_id, caller, answered)
        setup["call_id"] = call_id
        setup["task"] = asyncio.create_task(bring_up_call(call_id, resumed=resumed))

    @connection.event_handler("on_client_disconnected")
    async def on_disconnected(_connection):
        logger.info("SIP gateway hung up (socket EOF)")

    logger.info("SIP front-end ready — persistent connection up; a fresh pipeline is "
                "built per call (STT -> LLM -> TTS over teaport-sip)")
    # Standalone, ready = connected AND able to take a call: the connection, its
    # serializer and every handler exist, and the receive loop is the next thing to run
    # (datagrams the gateway sends before it starts wait in the socket). Inside
    # teaport-brain the brain's readiness is its HTTP port's, not this.
    if on_ready is not None:
        on_ready()
    try:
        # Blocks for the whole connection: dispatches control + routes audio. Per-call
        # pipelines run as background tasks launched from on_call_state above.
        await connection.run()
    finally:
        # EOF (or any exit): abandon any bring-up, tear down any running call, and
        # close the socket. The bring-up first — it is the thing that could otherwise
        # publish a new active call after we tore the old one down.
        await cancel_setup("connection closed")
        await cancel_active_call("connection closed")
        presence.end()
        CALL_FACE.end()  # the line itself is gone (or this brain is stopping)
        connection.close()
    logger.info("SIP connection closed")


def main():
    parser = argparse.ArgumentParser(
        description="teaport SIP front-end, standalone (test rigs; teaport-brain runs it itself)")
    parser.add_argument("--socket", default=os.getenv("TEAPORT_SIP_SOCKET", DEFAULT_UDS_PATH),
                        help="gateway UDS path (default: the live /run/teaport/teaport-sip.sock)")
    args = parser.parse_args()
    logger.info(agent_backend.startup_line())
    logger.info("Priming TTS service...")
    make_tts()  # warm the engine TTS client once at startup
    asyncio.run(run(args.socket))


if __name__ == "__main__":
    main()
