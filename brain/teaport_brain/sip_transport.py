# SPDX-License-Identifier: MIT
#
# teaport — Pipecat transport over the teaport-sip AF_UNIX SOCK_SEQPACKET socket.
#
# The SIP-over-UDS equivalent of pipecat's FastAPIWebsocketTransport, structured
# the same way (a shared client wrapper + a BaseInputTransport + a
# BaseOutputTransport) but speaking teaport-sip protocol v0 (sip_serializer.py)
# to the gateway instead of a WebSocket to OpenClaw.
#
# The brain is the socket CLIENT: the server (sip_server.py) connect()s to the
# gateway's socket, then hands the connected socket here. SEQPACKET preserves
# message boundaries, so each recv() yields exactly one protocol datagram.
#
# Two things differ from a media WebSocket and shape this transport:
#   1. Call control. hello / call.incoming / call.state / dtmf arrive as 0x01
#      control datagrams; the input transport peels them (via the serializer) and
#      dispatches to call-control EVENT HANDLERS (on_call_state, ...) that the
#      server registers — mirroring how gateway_server registers on_client_*.
#   2. Playout pacing. The gateway owns the RTP clock and drops frames oldest-
#      first if its ~1 s queue overruns, so the output side paces at ~REAL TIME
#      (one 20 ms frame per 20 ms), NOT the FastAPI transport's 2x-realtime burst
#      (which would overrun and truncate the caller's audio).

import asyncio
import socket
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

from teaport_brain.sip_serializer import (
    BYTES_PER_FRAME,
    PIPELINE_SAMPLE_RATE,
    SipControlFrame,
    SipProtocolSerializer,
    encode_audio,
    encode_control,
)


class SipGatewayParams(TransportParams):
    """TransportParams plus the SIP wire serializer. The base TransportParams has
    no serializer field (that lives on the WebSocket params), so we add it here."""

    serializer: FrameSerializer | None = None

# Largest datagram we will read (1 tag + 640 audio, or small JSON). Matches
# teaport-sip/src/ipc/protocol.h MAX_FRAME_BYTES; recv() truncates beyond this.
MAX_FRAME_BYTES = 2048

# Dev-default gateway socket path (the LIVE appliance gateway). The offline test
# points at a separate fake-gateway socket instead.
DEFAULT_UDS_PATH = "/tmp/teaport-sip.sock"


def connect_seqpacket(path: str, timeout: float = 5.0) -> socket.socket:
    """Connect to the gateway's SOCK_SEQPACKET socket as the client.

    Returns a NON-BLOCKING socket ready for asyncio loop.sock_recv/sock_sendall.
    Raises (ConnectionRefusedError / FileNotFoundError / socket.timeout) if the
    gateway isn't listening — the caller decides how to surface that.
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except Exception:
        s.close()
        raise
    s.settimeout(None)
    s.setblocking(False)
    return s


class SipGatewayCallbacks:
    """Async callbacks the client/input transport fire; wired by the parent
    transport to its event handlers. Plain class (not a pydantic model) so the
    async callables pass through untouched."""

    def __init__(self, *, on_connected, on_disconnected, on_hello,
                 on_call_incoming, on_call_state, on_dtmf):
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.on_hello = on_hello
        self.on_call_incoming = on_call_incoming
        self.on_call_state = on_call_state
        self.on_dtmf = on_dtmf


class _UDSClient:
    """Shared wrapper around the connected SEQPACKET socket.

    Mirrors FastAPIWebsocketClient: a leave-counter (setup++/disconnect--) closes
    the socket only after BOTH input and output transports have finished, and
    every send is guarded so a dead peer can't raise into the pipeline. SEQPACKET
    sends are atomic per datagram, so the input side (hello ack) and the output
    side (audio) can share this one socket without interleaving.
    """

    def __init__(self, sock: socket.socket, callbacks: SipGatewayCallbacks):
        self._sock = sock
        self._callbacks = callbacks
        self._closing = False
        self._connected = True
        self._leave_counter = 0

    async def setup(self, _frame: StartFrame):
        self._leave_counter += 1

    async def recv_datagram(self) -> bytes:
        """One protocol datagram, or b'' on EOF (peer closed). Every protocol
        frame is >= 1 byte (the tag), so b'' unambiguously means disconnect."""
        loop = asyncio.get_event_loop()
        return await loop.sock_recv(self._sock, MAX_FRAME_BYTES)

    async def send(self, data: bytes):
        if not self._can_send():
            return
        try:
            loop = asyncio.get_event_loop()
            await loop.sock_sendall(self._sock, data)
        except Exception as e:  # noqa: BLE001 — dead peer must not kill the pipeline
            logger.warning(f"{self.__class__.__name__}: send failed: {e!r}")

    async def disconnect(self):
        self._leave_counter -= 1
        if self._leave_counter > 0:
            return
        if self._connected and not self._closing:
            self._closing = True
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._connected = False

    def _can_send(self) -> bool:
        return self._connected and not self._closing

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_closing(self) -> bool:
        return self._closing

    async def trigger_connected(self):
        await self._callbacks.on_connected(self)

    async def trigger_disconnected(self):
        await self._callbacks.on_disconnected(self)


class SipGatewayInputTransport(BaseInputTransport):
    """Reads gateway datagrams: audio -> the VAD/STT path, control -> call
    events. Structured like FastAPIWebsocketInputTransport."""

    def __init__(self, transport: BaseTransport, client: _UDSClient,
                 callbacks: SipGatewayCallbacks, params: TransportParams, **kwargs):
        super().__init__(params, **kwargs)
        self._transport = transport
        self._client = client
        self._callbacks = callbacks
        self._params = params
        self._receive_task = None
        self._hello_acked = False
        self._initialized = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._initialized:
            return
        self._initialized = True
        await self._client.setup(frame)
        if self._params.serializer:
            await self._params.serializer.setup(frame)
        await self._client.trigger_connected()
        await self.push_frame(ClientConnectedFrame())
        if not self._receive_task:
            self._receive_task = self.create_task(self._receive_messages())
        await self.set_transport_ready(frame)

    async def _teardown(self):
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        await self._client.disconnect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._teardown()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._teardown()

    async def cleanup(self):
        await super().cleanup()
        await self._teardown()
        await self._transport.cleanup()

    async def _receive_messages(self):
        try:
            while True:
                data = await self._client.recv_datagram()
                if not data:  # EOF: gateway closed the socket
                    break
                if not self._params.serializer:
                    continue
                frame = await self._params.serializer.deserialize(data)
                if frame is None:
                    continue
                if isinstance(frame, SipControlFrame):
                    await self._handle_control(frame.control or {})
                elif isinstance(frame, InputAudioRawFrame):
                    await self.push_audio_frame(frame)
                else:
                    await self.push_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"{self}: receive loop error: {e.__class__.__name__} ({e})")
        # If the gateway hung up (we're not the ones closing), signal disconnect.
        if not self._client.is_closing:
            await self._client.trigger_disconnected()

    async def _handle_control(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "hello":
            # Optional brain->gateway ack (harmless; the live gateway ignores it).
            if not self._hello_acked:
                self._hello_acked = True
                await self._client.send(
                    encode_control({"type": "hello", "proto": 0, "role": "brain"})
                )
            await self._callbacks.on_hello(msg)
        elif mtype == "call.incoming":
            await self._callbacks.on_call_incoming(msg)
        elif mtype == "call.state":
            await self._callbacks.on_call_state(msg.get("call_id"), msg.get("state"))
        elif mtype == "dtmf":
            await self._callbacks.on_dtmf(msg.get("call_id"), msg.get("digit"))
        else:
            logger.debug(f"{self}: ignoring control type {mtype!r}")


class SipGatewayOutputTransport(BaseOutputTransport):
    """Tags 16 kHz playout audio as 0x11 and paces it at real time. Structured
    like FastAPIWebsocketOutputTransport but without the WAV-header path and with
    a real-time (not 2x) send clock."""

    def __init__(self, transport: BaseTransport, client: _UDSClient,
                 params: TransportParams, **kwargs):
        super().__init__(params, **kwargs)
        self._transport = transport
        self._client = client
        self._params = params
        # Real-time media clock, computed on StartFrame. FastAPI uses interval/2
        # (2x realtime) because a WS client buffers; the SIP gateway instead
        # drops on a bounded queue, so we pace at 1x to avoid overrun.
        self._send_interval = 0.0
        self._next_send_time = 0.0
        # Re-packetization remainder: OutputAudioRawFrames are already 640 B (20 ms
        # @ 16 kHz, audio_out_10ms_chunks=2), but buffer defensively so any size in
        # still goes out as exact 640-byte protocol frames.
        self._audio_send_buffer = bytearray()
        self._initialized = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._initialized:
            return
        self._initialized = True
        await self._client.setup(frame)
        if self._params.serializer:
            await self._params.serializer.setup(frame)
        # One 20 ms frame every 20 ms (BYTES_PER_FRAME/2 samples / sample_rate).
        self._send_interval = (BYTES_PER_FRAME / 2) / self.sample_rate
        await self.set_transport_ready(frame)

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._client.disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._client.disconnect()

    async def cleanup(self):
        await super().cleanup()
        await self._client.disconnect()
        await self._transport.cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            # Drop any partially buffered playout so stale PCM isn't replayed; the
            # media sender already dropped its queued chunks on the interruption.
            self._audio_send_buffer.clear()
            self._next_send_time = 0.0

    async def send_message(
        self, frame: "OutputTransportMessageFrame | OutputTransportMessageUrgentFrame"
    ):
        if self._client.is_closing or not self._client.is_connected:
            return
        if not self._params.serializer:
            return
        datagram = await self._params.serializer.serialize(frame)
        if datagram:
            await self._client.send(datagram)

    async def write_audio_frame(self, frame) -> bool:
        if self._client.is_closing or not self._client.is_connected:
            return False
        self._audio_send_buffer.extend(bytes(frame.audio))
        while len(self._audio_send_buffer) >= BYTES_PER_FRAME:
            chunk = bytes(self._audio_send_buffer[:BYTES_PER_FRAME])
            del self._audio_send_buffer[:BYTES_PER_FRAME]
            await self._client.send(encode_audio(chunk))
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
    """teaport-sip transport: one SipGatewayInputTransport + one
    SipGatewayOutputTransport over a shared connected SEQPACKET socket.

    Event handlers (register with @transport.event_handler(...)):
      - on_client_connected(transport, client)       socket is up
      - on_client_disconnected(transport, client)    gateway hung up / socket EOF
      - on_hello(transport, msg)                      gateway hello {proto,rate,...}
      - on_call_incoming(transport, msg)              {call_id, from, to}
      - on_call_state(transport, call_id, state)      state transition
      - on_dtmf(transport, call_id, digit)            DTMF digit
    """

    def __init__(self, sock: socket.socket, params: TransportParams,
                 input_name: str | None = None, output_name: str | None = None):
        super().__init__(input_name=input_name, output_name=output_name)
        self._params = params
        self._callbacks = SipGatewayCallbacks(
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            on_hello=self._on_hello,
            on_call_incoming=self._on_call_incoming,
            on_call_state=self._on_call_state,
            on_dtmf=self._on_dtmf,
        )
        self._client = _UDSClient(sock, self._callbacks)
        self._input = SipGatewayInputTransport(
            self, self._client, self._callbacks, self._params, name=self._input_name
        )
        self._output = SipGatewayOutputTransport(
            self, self._client, self._params, name=self._output_name
        )
        for ev in ("on_client_connected", "on_client_disconnected", "on_hello",
                   "on_call_incoming", "on_call_state", "on_dtmf"):
            self._register_event_handler(ev)

    def input(self) -> SipGatewayInputTransport:
        return self._input

    def output(self) -> SipGatewayOutputTransport:
        return self._output

    async def send_control(self, msg: dict):
        """Send a brain->gateway control message (e.g. call.hangup) directly on
        the shared socket. SEQPACKET sends are atomic, so this is safe alongside
        the output side's audio."""
        try:
            await self._client.send(encode_control(msg))
        except (TypeError, ValueError) as e:
            logger.warning(f"send_control: undeliverable message {msg!r}: {e}")

    async def _on_connected(self, client):
        await self._call_event_handler("on_client_connected", client)

    async def _on_disconnected(self, client):
        await self._call_event_handler("on_client_disconnected", client)

    async def _on_hello(self, msg):
        await self._call_event_handler("on_hello", msg)

    async def _on_call_incoming(self, msg):
        await self._call_event_handler("on_call_incoming", msg)

    async def _on_call_state(self, call_id, state):
        await self._call_event_handler("on_call_state", call_id, state)

    async def _on_dtmf(self, call_id, digit):
        await self._call_event_handler("on_dtmf", call_id, digit)


def make_sip_params(serializer: SipProtocolSerializer | None = None) -> SipGatewayParams:
    """SipGatewayParams for the SIP media contract: 16 kHz mono both ways, 20 ms
    output frames (audio_out_10ms_chunks=2 -> 640-byte writes), no WAV header."""
    return SipGatewayParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
        audio_out_sample_rate=PIPELINE_SAMPLE_RATE,
        audio_out_channels=1,
        audio_out_10ms_chunks=2,  # 2 * 320 B = 640 B = one 20 ms protocol frame
        serializer=serializer or SipProtocolSerializer(),
    )
