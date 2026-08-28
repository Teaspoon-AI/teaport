# SPDX-License-Identifier: MIT
#
# teaport — teaport-sip socket wire serializer (protocol v0)
#
# Wire format between the teaport-sip GATEWAY (C++/GPL, PJSIP-linked) and this
# Pipecat brain (MIT). The gateway is the AF_UNIX SOCK_SEQPACKET *server*; the
# brain is the *client* (see sip_transport.py). SEQPACKET preserves message
# boundaries, so exactly one protocol frame rides per datagram:
#
#   +--------+----------------------------+
#   | type   | payload                    |
#   | 1 byte | 0..N bytes                 |
#   +--------+----------------------------+
#
#   0x01 MSG_CONTROL   both      UTF-8 JSON object, flat, has "type"
#   0x10 MSG_AUDIO_IN  gw->brain caller audio   (S16LE PCM 16k mono, 640B/20ms)
#   0x11 MSG_AUDIO_OUT brain->gw playout audio  (S16LE PCM 16k mono, 640B/20ms)
#
# The wire rate EQUALS the pipeline rate (16 kHz), so — unlike the OpenClaw
# gateway_serializer's 24 kHz relay — there is NO resampling here. Outbound TTS
# is 24 kHz, but the SipGatewayOutputTransport is configured with
# audio_out_sample_rate=16000, so BaseOutputTransport resamples 24k->16k and
# hands us exactly 640-byte OutputAudioRawFrames; we only tag them.
#
# Direction of conversion (mirrors gateway_serializer.py):
#   brain -> gateway (serialize):
#       OutputAudioRawFrame                 -> 0x11 + raw PCM16 16k
#       OutputTransportMessage[Urgent]Frame -> 0x01 + JSON (brain->gw control only)
#       InterruptionFrame                   -> None (protocol v0 has no playout flush)
#   gateway -> brain (deserialize):
#       0x10 + PCM  -> InputAudioRawFrame(16000, 1ch)
#       0x01 + JSON -> SipControlFrame(control=<dict>)   (dispatched by the transport)
#
# This file is the SINGLE definition of that wire format, and the transport keeps it
# that way: SipGatewayOutputTransport routes audio, control and interruptions alike
# through _write_frame -> serialize(). It briefly did not — audio called encode_audio()
# directly — which made the two branches above dead code that still read as normative,
# exactly the trap a protocol v1 would fall into. Note the transport cuts playout into
# 640-byte frames BEFORE calling serialize(), because the type tag is per datagram: the
# chunk-after-serialize approach pipecat offers (params.fixed_audio_packet_size) would
# leave the 0x11 on the first slice only.
#
# Robustness contract (mirrors gateway_serializer.py): one malformed datagram
# from the socket must NEVER kill the session — anything we can't parse is logged
# and dropped, never raised.

import json
from dataclasses import dataclass

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    SystemFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

# One-byte frame type tags (must match teaport-sip/src/ipc/protocol.h).
MSG_CONTROL = 0x01
MSG_AUDIO_IN = 0x10
MSG_AUDIO_OUT = 0x11

# Media contract: S16LE PCM, 16 kHz mono, 20 ms == 320 samples == 640 bytes.
PIPELINE_SAMPLE_RATE = 16000
BYTES_PER_FRAME = 640

# brain -> gateway control types the protocol defines. Anything else a processor
# might emit (e.g. an OpenClaw-style {"type":"transcript"} / {"type":"clear"})
# is NOT part of protocol v0 and is dropped rather than put on the wire.
_BRAIN_CONTROL_TYPES = frozenset({"hello", "call.answer", "call.hangup"})


@dataclass
class SipControlFrame(SystemFrame):
    """Carries a decoded gateway->brain control message (0x01 payload).

    Returned by ``deserialize`` and consumed by the input transport's receive
    loop, which dispatches it to the transport's call-control event handlers. It
    is never pushed through the pipeline, so it needs no special handling
    downstream — but it subclasses SystemFrame so an accidental push is benign.
    """

    control: dict | None = None


def encode_control(msg: dict) -> bytes:
    """0x01 + UTF-8 JSON. Raises only on a non-serializable dict (caller guards)."""
    return bytes([MSG_CONTROL]) + json.dumps(msg, ensure_ascii=False).encode("utf-8")


def encode_audio(pcm: bytes) -> bytes:
    """0x11 + raw PCM16. `pcm` MUST be one 640-byte / 20 ms frame; the caller
    (SipGatewayOutputTransport.write_audio_frame) re-packetizes to guarantee it.

    Oversize is not a caught error anywhere: a 64 KB SEQPACKET send succeeds locally
    and the gateway's MAX_FRAME_BYTES recv silently truncates it, so the audio just
    disappears. Undersize is worse than useless too — the gateway treats one datagram
    as one 20 ms RTP frame."""
    return bytes([MSG_AUDIO_OUT]) + bytes(pcm)


class SipProtocolSerializer(FrameSerializer):
    """teaport-sip protocol v0 <-> Pipecat frames, at 16 kHz, no resampling."""

    def __init__(self, sample_rate: int = PIPELINE_SAMPLE_RATE):
        super().__init__()
        self._sample_rate = sample_rate

    # ---- brain -> gateway --------------------------------------------------

    async def serialize(self, frame: Frame):
        if isinstance(frame, OutputAudioRawFrame):
            # Already 16 kHz by now (audio_out_sample_rate=16000) and already cut to
            # exactly BYTES_PER_FRAME by the transport. Tag as playout.
            return encode_audio(bytes(frame.audio))
        if isinstance(frame, InterruptionFrame):
            # Reached on every barge-in (the output transport's process_frame routes
            # it here), but protocol v0 has no "flush the caller's playout" control
            # message, so we can't tell the gateway to drop already-queued audio; we
            # simply stop sending, and the gateway's ~1 s bounded queue drains.
            # (Documented deferral — see RUNLOG. Adding the v1 flush is a one-line
            # change right here, with nothing to touch in the transport.)
            return None
        if isinstance(frame, (OutputTransportMessageUrgentFrame, OutputTransportMessageFrame)):
            if self.should_ignore_frame(frame):  # drop RTVI protocol messages
                return None
            msg = frame.message
            if not isinstance(msg, dict) or msg.get("type") not in _BRAIN_CONTROL_TYPES:
                # Not a protocol-v0 brain->gateway control message — don't put it
                # on the wire (the gateway would ignore it anyway).
                return None
            try:
                return encode_control(msg)
            except (TypeError, ValueError):
                return None
        return None

    # ---- gateway -> brain --------------------------------------------------

    async def deserialize(self, data):
        # SEQPACKET always delivers bytes; never let one bad datagram raise.
        if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
            return None
        tag = data[0]
        payload = bytes(data[1:])

        if tag == MSG_AUDIO_IN:
            if len(payload) % 2 != 0:
                # Odd byte count can't be S16LE frames — drop rather than corrupt.
                logger.warning(
                    f"sip serializer: dropping odd-length audio datagram ({len(payload)} bytes)"
                )
                return None
            return InputAudioRawFrame(
                audio=payload, sample_rate=self._sample_rate, num_channels=1
            )

        if tag == MSG_CONTROL:
            try:
                msg = json.loads(payload)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                logger.warning("sip serializer: dropping unparseable control datagram")
                return None
            if not isinstance(msg, dict):
                logger.warning(f"sip serializer: ignoring non-object control: {msg!r}")
                return None
            return SipControlFrame(control=msg)

        logger.warning(f"sip serializer: dropping datagram with unknown tag 0x{tag:02x}")
        return None
