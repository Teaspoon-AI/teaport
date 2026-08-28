# SPDX-License-Identifier: MIT
#
# teaport — Pipecat transport over the teaport-sip AF_UNIX SOCK_SEQPACKET socket.
#
# The SIP-over-UDS equivalent of pipecat's FastAPIWebsocketTransport, but split so a
# FRESH per-call pipeline can be built and torn down WITHOUT disturbing the socket or
# the gateway's call-control stream:
#
#   * SipConnection  — PERSISTENT. Owns the connected SEQPACKET socket, runs the one
#     receive loop for the whole process, dispatches call-control events (hello /
#     call.incoming / call.state / dtmf) to the server's handlers, routes inbound
#     caller audio (audio.in) to the CURRENTLY-ACTIVE call's input transport (a
#     settable sink; None between calls -> audio is dropped), and exposes send() for
#     audio.out. It lives until the gateway hangs up (socket EOF) or the process ends.
#
#   * SipGatewayTransport — PER CALL. Lightweight: built when a call is confirmed and
#     torn down when it disconnects. Its INPUT registers itself as the connection's
#     current audio sink on start and deregisters on stop/cancel; its OUTPUT writes
#     playout via connection.send(). NEITHER side closes the socket nor touches the
#     receive loop, so a pipeline ending between calls leaves the connection (and thus
#     the next call) alive. This mirrors the OpenClaw path's fresh-pipeline-per-socket.
#
# The brain is the socket CLIENT: the server (sip_server.py) connect()s to the
# gateway's socket, then hands the connected socket to a SipConnection. SEQPACKET
# preserves message boundaries, so each recv() yields exactly one protocol datagram.
#
# Two things differ from a media WebSocket and shape this transport:
#   1. Call control. hello / call.incoming / call.state / dtmf arrive as 0x01
#      control datagrams; SipConnection peels them (via the serializer) and dispatches
#      to call-control EVENT HANDLERS (on_call_state, ...) that the server registers —
#      mirroring how gateway_server registers on_client_*.
#   2. Playout pacing. The gateway owns the RTP clock and drops frames oldest-first if
#      its ~1 s queue overruns, so the output side paces at ~REAL TIME (one 20 ms frame
#      per 20 ms). pipecat's BaseOutputTransport does NO pacing of its own — MediaSender
#      hands write_audio_frame whatever the audio queue yields, as fast as it yields it
#      (base_output.py:887-910) — so every 1.7.0 transport that writes to a socket
#      itself hand-rolls a send clock (the WebRTC ones let their SDK pace instead), and
#      so do we (see _write_audio_sleep). Without it the whole utterance goes out in a
#      burst and the gateway drops the tail.

import asyncio
import os
import socket
import struct
import time

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    ClientConnectedFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.utils.base_object import BaseObject

from teaport_brain.sip_serializer import (
    BYTES_PER_FRAME,
    PIPELINE_SAMPLE_RATE,
    SipControlFrame,
    SipProtocolSerializer,
    encode_control,
)


class SipGatewayParams(TransportParams):
    """TransportParams plus the SIP wire serializer. The base TransportParams has
    no serializer field (that lives on the WebSocket params), so we add it here."""

    serializer: FrameSerializer | None = None

# Largest datagram we will read (1 tag + 640 audio, or small JSON). Matches
# teaport-sip/src/ipc/protocol.h MAX_FRAME_BYTES; recv() truncates beyond this.
MAX_FRAME_BYTES = 2048

# Default gateway socket path (the LIVE appliance gateway). It lives in the systemd
# units' RuntimeDirectory (/run/teaport, mode 0750) — NOT world-writable /tmp — so no
# other local user can pre-bind it to hijack the session. The offline test points at a
# separate fake-gateway socket instead.
DEFAULT_UDS_PATH = "/run/teaport/teaport-sip.sock"


def _verify_peer_uid(s: socket.socket, path: str) -> None:
    """Refuse a gateway socket whose PEER isn't us or root.

    Defense-in-depth against a squatted rendezvous socket. The primary control is that
    the gateway binds inside a non-world-writable RuntimeDirectory (see DEFAULT_UDS_PATH),
    so no other user can create the path at all. But if an operator points
    TEAPORT_SIP_SOCKET/@SIP_UDS@ back into a shared dir, a stranger's socket there could
    be handed a live brain session (inject audio.in / read every audio.out frame). We
    check the CONNECTED peer's uid via SO_PEERCRED — race-free, unlike stat'ing the path
    (TOCTOU) — and accept only our own euid or root. Best-effort: if the platform has no
    SO_PEERCRED (non-Linux dev), skip; the RuntimeDirectory is the real guard."""
    peercred = getattr(socket, "SO_PEERCRED", None)
    if peercred is None:
        return
    try:
        raw = s.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("iII"))
        _pid, uid, _gid = struct.unpack("iII", raw)
    except OSError:
        return  # couldn't read creds — fall back to the directory guard
    if uid not in (os.geteuid(), 0):
        raise PermissionError(
            f"refusing gateway socket {path}: peer uid {uid} is not us "
            f"({os.geteuid()}) or root — possible squatted socket")


def connect_seqpacket(path: str, timeout: float = 5.0) -> socket.socket:
    """Connect to the gateway's SOCK_SEQPACKET socket as the client.

    Returns a NON-BLOCKING socket ready for asyncio loop.sock_recv/sock_sendall.
    Raises (ConnectionRefusedError / FileNotFoundError / socket.timeout) if the
    gateway isn't listening, or PermissionError if the peer isn't us/root (see
    _verify_peer_uid) — the caller decides how to surface that.
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.settimeout(timeout)
    try:
        s.connect(path)
        _verify_peer_uid(s, path)
    except Exception:
        s.close()
        raise
    s.settimeout(None)
    s.setblocking(False)
    return s


class SipConnection(BaseObject):
    """Persistent gateway connection: the socket, the receive loop, and control
    dispatch — all decoupled from any one call's pipeline.

    A BaseObject so it carries pipecat's event-handler machinery. The server
    registers call-control handlers on it (register with @connection.event_handler):
      - on_client_connected(connection)              socket is up (once, at start)
      - on_client_disconnected(connection)           gateway hung up / socket EOF
      - on_hello(connection, msg)                    gateway hello {proto,rate,...}
      - on_call_incoming(connection, msg)            {call_id, from, to}
      - on_call_state(connection, call_id, state)    state transition
      - on_dtmf(connection, call_id, digit)          DTMF digit

    The control handlers are dispatched SYNCHRONOUSLY (is_sync=True) so they run
    inline in the receive loop, IN WIRE ORDER. That ordering is load-bearing: a
    call.state=confirmed builds the per-call pipeline and a following
    call.state=disconnected tears it down, and if the two ran as concurrent tasks
    the teardown could race ahead of the build and orphan the pipeline.

    Inbound caller audio (audio.in) is routed to the current call's input transport
    via a settable sink; between calls the sink is None and the audio is dropped.
    """

    def __init__(self, sock: socket.socket, serializer: FrameSerializer):
        super().__init__()
        self._sock = sock
        self._serializer = serializer
        self._connected = True
        self._closing = False
        self._hello_acked = False
        # One writer at a time on this fd — see send(). Concurrent sock_sendall calls
        # strand each other; audio and control both go out through here.
        self._send_lock = asyncio.Lock()
        # The CURRENTLY-ACTIVE call's input transport (its push_audio_frame sink), or
        # None between calls. Set by SipGatewayInputTransport.start, cleared on its
        # stop/cancel. Inbound audio for a None sink is dropped.
        self._audio_sink = None
        for ev in ("on_client_connected", "on_client_disconnected", "on_hello",
                   "on_call_incoming", "on_call_state", "on_dtmf"):
            # sync=True: dispatch inline in the receive loop, preserving wire order.
            self._register_event_handler(ev, sync=True)

    # ---- audio sink registration (the per-call input transport wires itself here) --

    def set_audio_sink(self, sink):
        """Point inbound caller audio at `sink` (an input transport) or None."""
        self._audio_sink = sink

    @property
    def current_sink(self):
        return self._audio_sink

    # ---- socket status + send ----------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_closing(self) -> bool:
        return self._closing

    async def send(self, data: bytes):
        """Send one datagram (audio.out or a brain->gateway control).
        Guarded so a dead peer can't raise into a pipeline.

        SERIALIZED, and the lock is not optional. SEQPACKET makes each datagram atomic
        ON THE WIRE, but that says nothing about two coroutines calling sock_sendall on
        one fd: asyncio registers at most ONE writer per fd, so the second call's
        _add_writer cancels the first's handle and the first future is never resolved.
        Its awaiting coroutine then blocks forever.

        That is not theoretical here. The output transport's MediaSender calls this via
        write_audio_frame continuously, while the receive loop calls send_control() for
        the hello ack and sip_server for call.hangup. Whenever the UDS send buffer is
        full (gateway stalled), the audio sender is the one left hanging — the call goes
        silent with nothing logged, because no exception is ever raised."""
        if not (self._connected and not self._closing):
            return
        async with self._send_lock:
            # Re-check: close() can land while we waited for the lock.
            if not (self._connected and not self._closing):
                return
            try:
                loop = asyncio.get_event_loop()
                await loop.sock_sendall(self._sock, data)
            except Exception as e:  # noqa: BLE001 — dead peer must not kill the pipeline
                logger.warning(f"{self}: send failed: {e!r}")

    async def send_control(self, msg: dict):
        """Send a brain->gateway control message (e.g. call.hangup). send() serializes
        it against the output side's audio."""
        try:
            await self.send(encode_control(msg))
        except (TypeError, ValueError) as e:
            logger.warning(f"send_control: undeliverable message {msg!r}: {e}")

    def close(self):
        """Close the socket for good (process shutdown / EOF cleanup). Idempotent."""
        if self._connected and not self._closing:
            self._closing = True
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._connected = False

    # ---- the one persistent receive loop -----------------------------------

    async def run(self):
        """Own the connection for the whole process: announce it, pump datagrams
        until EOF, then announce the disconnect. Returns when the gateway hangs up.

        The server awaits this as its top-level coroutine; per-call pipelines are
        launched from the on_call_state handler as background tasks, so this loop
        keeps dispatching control + routing audio across the lifetime of every call.
        """
        await self._call_event_handler("on_client_connected")
        try:
            await self._receive_messages()
        finally:
            if not self._closing:
                await self._call_event_handler("on_client_disconnected")

    async def _receive_messages(self):
        loop = asyncio.get_event_loop()
        try:
            while True:
                data = await loop.sock_recv(self._sock, MAX_FRAME_BYTES)
                if not data:  # EOF: gateway closed the socket
                    break
                frame = await self._serializer.deserialize(data)
                if frame is None:
                    continue
                if isinstance(frame, SipControlFrame):
                    await self._handle_control(frame.control or {})
                elif isinstance(frame, InputAudioRawFrame):
                    sink = self._audio_sink
                    if sink is not None:
                        await sink.push_audio_frame(frame)
                    # else: no active call -> drop the caller's audio.
                else:
                    logger.debug(f"{self}: dropping non-audio/control frame {frame}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"{self}: receive loop error: {e.__class__.__name__} ({e})")

    async def _handle_control(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "hello":
            # Optional brain->gateway ack (harmless; the live gateway ignores it).
            if not self._hello_acked:
                self._hello_acked = True
                await self.send(
                    encode_control({"type": "hello", "proto": 0, "role": "brain"})
                )
            await self._call_event_handler("on_hello", msg)
        elif mtype == "call.incoming":
            await self._call_event_handler("on_call_incoming", msg)
        elif mtype == "call.state":
            await self._call_event_handler("on_call_state", msg.get("call_id"), msg.get("state"))
        elif mtype == "dtmf":
            await self._call_event_handler("on_dtmf", msg.get("call_id"), msg.get("digit"))
        else:
            logger.debug(f"{self}: ignoring control type {mtype!r}")


class SipGatewayInputTransport(BaseInputTransport):
    """Per-call input side. Does NOT own the socket or a receive loop: it just wires
    itself in as the persistent connection's current audio sink while the pipeline
    runs, and out again on teardown. The connection's receive loop pushes caller
    audio into this transport's queue (via push_audio_frame) from there."""

    def __init__(self, transport: BaseTransport, connection: SipConnection,
                 params: TransportParams, **kwargs):
        super().__init__(params, **kwargs)
        self._transport = transport
        self._connection = connection
        self._params = params
        self._started = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._started:
            return
        self._started = True
        # set_transport_ready creates the audio-in queue + consumer task; do it
        # BEFORE registering as the sink so the queue exists for the first datagram.
        await self.set_transport_ready(frame)
        self._connection.set_audio_sink(self)
        # Pipeline-internal parity with the WebSocket transport (nothing consumes it
        # today, but pipecat convention emits it when a client's media is available).
        await self.push_frame(ClientConnectedFrame())

    def _deregister(self):
        # Only clear the sink if it is still us — a replacement call may already have
        # claimed it (the server cancels-then-builds, so this is belt-and-braces).
        if self._connection.current_sink is self:
            self._connection.set_audio_sink(None)

    async def stop(self, frame: EndFrame):
        self._deregister()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        self._deregister()
        await super().cancel(frame)

    async def cleanup(self):
        self._deregister()
        await super().cleanup()
        await self._transport.cleanup()


class SipGatewayOutputTransport(BaseOutputTransport):
    """Per-call output side. Cuts playout into 640-byte protocol frames, hands each to
    the serializer, and paces the result at real time over the persistent connection.
    Structured like FastAPIWebsocketOutputTransport minus the WAV-header path. Its
    teardown does NOT close the socket.

    Every brain->gateway frame — audio, control, interruptions — leaves through
    _write_frame, so SipProtocolSerializer is the ONE definition of the wire format."""

    def __init__(self, transport: BaseTransport, connection: SipConnection,
                 params: TransportParams, **kwargs):
        super().__init__(params, **kwargs)
        self._transport = transport
        self._connection = connection
        self._params = params
        # Real-time media clock, computed on StartFrame: one 640-byte frame per 20 ms.
        #
        # This used to say "FastAPI uses interval/2 (2x realtime) because a WS client
        # buffers". That is FALSE, and the /2 is what made it tempting: pipecat writes
        # `(self.audio_chunk_size / self.sample_rate) / 2` (fastapi.py:463) where
        # audio_chunk_size is in BYTES (base_output.py:135), so the /2 is the S16LE
        # bytes->samples correction, NOT a burst. At 640 B / 16 kHz that is
        # (640/16000)/2 = 0.02 s — the same 20 ms our spelling below produces.
        #
        # The clock itself is still ours to keep: BaseOutputTransport paces nothing
        # (base_output.py:887-910), so fastapi/websocket-server/websocket-client/tavus
        # each hand-roll exactly this. And we keep OUR spelling because the two agree
        # only while audio_out_10ms_chunks == 2: pipecat's interval is per INCOMING
        # chunk, ours is per OUTGOING 640-byte datagram, and write_audio_frame sleeps
        # once per datagram. At audio_out_10ms_chunks=4 pipecat's formula would sleep
        # 40 ms per 20 ms datagram — half real time — while ours stays right.
        self._send_interval = 0.0
        self._next_send_time = 0.0
        # Re-packetization remainder. NOT a duplicate of the base class's chunking:
        # MediaSender.handle_audio_frame cuts the TTS path to audio_chunk_size = 640 B
        # (base_output.py:598-606), but two base-class paths call write_audio_frame
        # DIRECTLY with a whole unchunked buffer:
        #   * _send_silence on EndFrame (base_output.py:870-885) — one frame of
        #     sample_rate * 2 * audio_out_end_silence_secs bytes, i.e. 64000 B here with
        #     the default 2 s (base_transport.py:72). Every graceful teardown hits this.
        #   * _write_dtmf_audio (base_output.py:291-304) — one whole 16000 B tone.
        # Without the loop below either becomes a single 64001-byte datagram, and
        # nothing raises to tell us: measured on this box, SO_SNDBUF is 212992 and
        # SEQPACKET send() happily returns 64001 — the gateway's MAX_FRAME_BYTES-sized
        # recv then TRUNCATES it to 2048 and 62 KB of playout vanishes silently.
        self._audio_send_buffer = bytearray()
        self._started = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._started:
            return
        self._started = True
        if self._params.serializer:
            await self._params.serializer.setup(frame)
        # One 20 ms frame every 20 ms (BYTES_PER_FRAME/2 samples / sample_rate).
        self._send_interval = (BYTES_PER_FRAME / 2) / self.sample_rate
        await self.set_transport_ready(frame)

    # stop/cancel/cleanup must NOT close the socket or touch the receive loop — the
    # connection outlives this per-call transport. We only run the base teardown.
    async def stop(self, frame: EndFrame):
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)

    async def cleanup(self):
        await super().cleanup()
        await self._transport.cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            # Barge-in. Drop any partially buffered playout so stale PCM isn't replayed
            # (the media sender already dropped its queued chunks), reset the clock, and
            # OFFER the interruption to the serializer — the hook pipecat's Twilio
            # serializer uses for {"event":"clear"} and fastapi.py:501-506 wires up.
            # Protocol v0 has no playout flush, so it serializes to None today; wiring
            # it now means a v1 flush control needs no change on this side.
            self._audio_send_buffer.clear()
            await self._write_frame(frame)
            self._next_send_time = 0.0

    async def send_message(
        self, frame: "OutputTransportMessageFrame | OutputTransportMessageUrgentFrame"
    ):
        await self._write_frame(frame)

    async def _write_frame(self, frame: Frame):
        """The ONE brain->gateway path: serialize the frame, put the datagram on the
        socket. Audio, control and interruptions all come through here.

        Audio used to call encode_audio() directly and skip the serializer entirely,
        which left SipProtocolSerializer.serialize's audio and InterruptionFrame
        branches unreachable while still LOOKING authoritative — so a protocol v1 that
        retagged audio or added a real flush would have changed the serializer, watched
        control adopt v1, and silently kept emitting v0 audio. There is no performance
        case for the shortcut either: one extra frame object and coroutine per 20 ms is
        nothing beside the sleep and the sendall it sits next to.

        The serializer must return bytes here (protocol v0 is binary-only); a str would
        only surface as a per-datagram send warning."""
        if self._connection.is_closing or not self._connection.is_connected:
            return
        if not self._params.serializer:
            return
        try:
            datagram = await self._params.serializer.serialize(frame)
        except Exception as e:  # noqa: BLE001 — one bad frame must not kill the call
            logger.error(f"{self}: serializing {frame}: {e.__class__.__name__} ({e})")
            return
        if datagram:
            await self._connection.send(datagram)

    async def write_audio_frame(self, frame) -> bool:
        if self._connection.is_closing or not self._connection.is_connected:
            return False
        self._audio_send_buffer.extend(bytes(frame.audio))
        while len(self._audio_send_buffer) >= BYTES_PER_FRAME:
            chunk = bytes(self._audio_send_buffer[:BYTES_PER_FRAME])
            del self._audio_send_buffer[:BYTES_PER_FRAME]
            # Cut BEFORE tagging: the 0x11 tag is per datagram, so pipecat's own
            # fixed_audio_packet_size trick (chunk the SERIALIZED payload,
            # fastapi.py:569-580) would slice our tag off every frame but the first.
            # Re-wrap in the class we were handed, as base_output.py:599 does, so the
            # serializer sees the same frame type the pipeline produced.
            await self._write_frame(type(frame)(
                chunk, sample_rate=frame.sample_rate, num_channels=frame.num_channels))
            await self._write_audio_sleep()
        return True

    async def _write_audio_sleep(self):
        """Pace the playout to the real-time media clock (drift-corrected)."""
        now = time.monotonic()
        sleep_duration = max(0.0, self._next_send_time - now)
        await asyncio.sleep(sleep_duration)
        if sleep_duration == 0.0:
            self._next_send_time = time.monotonic() + self._send_interval
        else:
            self._next_send_time += self._send_interval


class SipGatewayTransport(BaseTransport):
    """teaport-sip PER-CALL transport: one SipGatewayInputTransport + one
    SipGatewayOutputTransport bound to a persistent SipConnection. Built when a call
    is confirmed, torn down when it disconnects — the connection is untouched.

    Call-control event handlers live on the SipConnection (persistent), NOT here, so
    they survive across the per-call transports coming and going."""

    def __init__(self, connection: SipConnection, params: TransportParams,
                 input_name: str | None = None, output_name: str | None = None):
        super().__init__(input_name=input_name, output_name=output_name)
        self._connection = connection
        self._params = params
        self._input = SipGatewayInputTransport(
            self, self._connection, self._params, name=self._input_name
        )
        self._output = SipGatewayOutputTransport(
            self, self._connection, self._params, name=self._output_name
        )

    def input(self) -> SipGatewayInputTransport:
        return self._input

    def output(self) -> SipGatewayOutputTransport:
        return self._output

    async def send_control(self, msg: dict):
        """Delegate a brain->gateway control message to the connection's socket."""
        await self._connection.send_control(msg)


def make_sip_params(serializer: SipProtocolSerializer | None = None) -> SipGatewayParams:
    """SipGatewayParams for the SIP media contract: 16 kHz mono both ways, 20 ms
    output frames (audio_out_10ms_chunks=2 -> 640-byte writes), no WAV header.

    `serializer` covers the WHOLE brain->gateway wire format — audio, control and
    interruptions all reach it via SipGatewayOutputTransport._write_frame — so
    substituting one here really does swap the protocol."""
    return SipGatewayParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
        audio_out_sample_rate=PIPELINE_SAMPLE_RATE,
        audio_out_channels=1,
        audio_out_10ms_chunks=2,  # 2 * 320 B = 640 B = one 20 ms protocol frame
        serializer=serializer or SipProtocolSerializer(),
    )
