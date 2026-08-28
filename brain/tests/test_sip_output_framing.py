#
# Unit test: the SIP output transport's brain->gateway path — one wire format, one
# definition of it, and a re-packetizer that is not the decoration it looks like.
#
# Two findings from the SIP review meet here, and both are about code that LOOKS
# redundant from the outside:
#
#   1. write_audio_frame used to call encode_audio() directly instead of going through
#      params.serializer, which made SipProtocolSerializer.serialize's OutputAudioRawFrame
#      and InterruptionFrame branches unreachable — while still reading as the normative
#      description of the wire. A protocol v1 that retagged audio or added a real playout
#      flush would have been written in the serializer, watched control adopt v1, and
#      silently kept emitting v0 audio from the transport. So: every brain->gateway frame
#      now leaves through _write_frame, and test 1 asserts the serializer SAW each one.
#
#   2. `_audio_send_buffer` was called a second copy of pipecat's framing that "can only
#      ever pass data straight through", on the grounds that BaseOutputTransport always
#      chunks output to audio_chunk_size (640 B here). That is false in pipecat 1.7.0:
#      MediaSender._send_silence (base_output.py:870-885) builds ONE OutputAudioRawFrame
#      of sample_rate*2*audio_out_end_silence_secs bytes — 64000 B at our 16 kHz with the
#      default 2 s — and calls write_audio_frame with it directly, and _write_dtmf_audio
#      (base_output.py:290-304) does the same with a whole 16000 B tone. Nothing raises if
#      we forward that as one datagram: SEQPACKET accepts 64001 bytes and the gateway's
#      2048-byte recv truncates it, losing 62 KB of playout in silence. Test 1 pins the
#      unchunked frame arriving, so if a future pipecat DOES pre-chunk it, this fails and
#      tells us the buffer may finally be deletable.
#
# Both tests drive the real pipecat pipeline (pipecat.tests.utils.run_test) rather than
# calling write_audio_frame by hand: the claims above are claims about what the BASE class
# does, so a hand-fed frame would only be testing the test.
#
# Run: python test_sip_output_framing.py
#
import asyncio
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()  # these assertions are about 1.7.0 internals; a pass elsewhere is noise

from pipecat.frames.frames import (  # noqa: E402
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSAudioRawFrame,
)
from pipecat.tests.utils import run_test  # noqa: E402

from teaport_brain.sip_serializer import (  # noqa: E402
    BYTES_PER_FRAME,
    MSG_AUDIO_OUT,
    PIPELINE_SAMPLE_RATE,
    SipProtocolSerializer,
)
from teaport_brain.sip_transport import (  # noqa: E402
    SipConnection,
    SipGatewayTransport,
    make_sip_params,
)


class _RecordingSerializer(SipProtocolSerializer):
    """The real serializer plus a log of every frame the transport handed it. The log
    is the point: it is what distinguishes "the transport serialized this" from "the
    transport encoded this itself and the serializer was never consulted"."""

    def __init__(self):
        super().__init__()
        self.seen = []

    async def serialize(self, frame):
        self.seen.append(frame)
        return await super().serialize(frame)

    def audio_frames(self):
        return [f for f in self.seen if isinstance(f, OutputAudioRawFrame)]


class _Wire:
    """A SipConnection over a socketpair, with the GATEWAY end drained as it arrives.

    Drained concurrently rather than after the fact: the send buffer would hold this
    test's ~65 KB, but a test that only works while the peer never reads is a test of
    the socket buffer, not of the transport."""

    def __init__(self):
        self.brain_sock, self.gw_sock = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.brain_sock.setblocking(False)
        self.gw_sock.setblocking(False)
        self.serializer = _RecordingSerializer()
        self.connection = SipConnection(self.brain_sock, self.serializer)
        self.datagrams = []
        self._drain = None

    def start(self):
        self._drain = asyncio.create_task(self._drain_loop())

    async def _drain_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            data = await loop.sock_recv(self.gw_sock, 4096)
            if not data:
                return
            self.datagrams.append(data)

    async def stop(self):
        await asyncio.sleep(0.05)          # let the last datagrams land
        self._drain.cancel()
        try:
            await self._drain
        except asyncio.CancelledError:
            pass
        self.connection.close()
        self.gw_sock.close()

    def build_output(self, end_silence_secs=None):
        params = make_sip_params(self.serializer)
        if end_silence_secs is not None:
            params.audio_out_end_silence_secs = end_silence_secs
        return SipGatewayTransport(self.connection, params).output()


async def test_every_audio_datagram_is_built_by_the_serializer():
    """Speech plus the EndFrame silence: the serializer must have seen one
    OutputAudioRawFrame per datagram, each exactly one 640-byte protocol frame."""
    wire = _Wire()
    wire.start()
    out = wire.build_output()

    # Spy on what the BASE class hands us, before our re-packetization.
    handed = []
    write_audio_frame = out.write_audio_frame

    async def spy(frame):
        handed.append(len(frame.audio))
        return await write_audio_frame(frame)

    out.write_audio_frame = spy

    # 1280 B = two 20 ms protocol frames, so the TTS path lands on the chunk size
    # exactly and any framing bug has to come from somewhere else.
    speech = bytes([0x11, 0x22]) * BYTES_PER_FRAME
    started = time.monotonic()
    await run_test(
        out,
        frames_to_send=[TTSAudioRawFrame(
            speech, sample_rate=PIPELINE_SAMPLE_RATE, num_channels=1)],
    )
    elapsed = time.monotonic() - started
    await wire.stop()

    assert max(handed) > BYTES_PER_FRAME, (
        f"pipecat handed write_audio_frame only {sorted(set(handed))} bytes. In 1.7.0 "
        f"MediaSender._send_silence (base_output.py:870-885) passes the whole "
        f"audio_out_end_silence_secs buffer unchunked, which is why "
        f"_audio_send_buffer exists. If pipecat now pre-chunks everything, that buffer "
        f"may finally be redundant — verify _write_dtmf_audio too, then delete it.")

    expected = sum(handed) // BYTES_PER_FRAME
    assert len(wire.datagrams) == expected, (
        f"{len(wire.datagrams)} datagrams for {sum(handed)} bytes, expected {expected}")
    for i, d in enumerate(wire.datagrams):
        assert d[0] == MSG_AUDIO_OUT, f"datagram {i} tagged 0x{d[0]:02x}, not audio.out"
        assert len(d) == 1 + BYTES_PER_FRAME, (
            f"datagram {i} is {len(d)} bytes; the gateway reads one datagram as one "
            f"20 ms RTP frame, and its recv truncates anything oversized")
    assert wire.datagrams[0][1:] == speech[:BYTES_PER_FRAME]

    audio = wire.serializer.audio_frames()
    assert len(audio) == len(wire.datagrams), (
        f"the serializer saw {len(audio)} audio frames but {len(wire.datagrams)} "
        f"datagrams reached the wire — audio is bypassing params.serializer, which is "
        f"what left its OutputAudioRawFrame branch dead")
    assert all(len(f.audio) == BYTES_PER_FRAME for f in audio)

    # Pacing: the gateway owns the RTP clock and drops oldest-first on a ~1 s queue, so
    # this must be ~1x realtime. Both bounds matter — the lower one catches the clock
    # being "simplified" away, the upper one catches an interval that is off by the 2x
    # the old comment wrongly attributed to pipecat's (chunk_bytes/rate)/2.
    realtime = expected * (BYTES_PER_FRAME / 2) / PIPELINE_SAMPLE_RATE
    assert elapsed >= realtime * 0.8, (
        f"{expected} frames ({realtime:.2f}s of audio) went out in {elapsed:.2f}s — "
        f"too fast; the gateway's bounded queue would drop the tail")
    assert elapsed < realtime + 1.5, (
        f"{expected} frames ({realtime:.2f}s of audio) took {elapsed:.2f}s — the send "
        f"clock is slower than realtime")


async def test_barge_in_reaches_the_serializer_and_drops_the_partial_frame():
    """The InterruptionFrame branch is live, not decorative. Protocol v0 serializes it
    to None so nothing goes on the wire today — the assertion that matters is that the
    serializer was ASKED, because that is the whole hook a v1 flush hangs on."""
    wire = _Wire()
    wire.start()
    out = wire.build_output(end_silence_secs=0)   # the silence is test 1's business

    # A stranded part-frame, as a mid-utterance barge-in leaves behind.
    out._audio_send_buffer.extend(b"\xff" * (BYTES_PER_FRAME // 2))

    await run_test(out, frames_to_send=[InterruptionFrame()])
    await wire.stop()

    assert any(isinstance(f, InterruptionFrame) for f in wire.serializer.seen), (
        "the interruption never reached the serializer, so a protocol-v1 playout flush "
        "would have to be wired into the transport as well as the serializer — see "
        "fastapi.py:501-506 for the shape this mirrors")
    assert bytes(out._audio_send_buffer) == b"", (
        "stale PCM survived the barge-in; the next utterance would be prefixed with it "
        "and every frame after that misaligned by half a frame")
    assert wire.datagrams == [], (
        f"protocol v0 has no playout-flush control, so a barge-in must put nothing on "
        f"the wire, but {len(wire.datagrams)} datagram(s) went out")


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
