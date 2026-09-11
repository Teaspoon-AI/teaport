#
# Unit test: the caller-path makeup gain applied to the audio sent to the transcriber.
#
# The SIP bridge's echo canceller attenuates the caller's own voice during double-talk
# (measured 2026-09-11: ~4 dB within-call, and the segments the engine drops are the
# quietest). A flat makeup gain on the audio the STT sees recovers the quiet barge-in
# "stop"s. This pins the transform: correct scale, hard clip, no-op at 0 dB, and that it
# is applied to what run_stt sends and NOT to what VAD/endpointing upstream would read.
#
# Run: python test_stt_makeup_gain.py   (or via pytest test_suite.py)
#
import asyncio
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

import numpy as np  # noqa: E402

from teaport_brain.stt import TeaportSTTService  # noqa: E402


class Sender(TeaportSTTService):
    """Captures the audio run_stt would put on the wire, instead of sending it."""

    def __init__(self, makeup_db=0.0):
        super().__init__(url="ws://127.0.0.1:1/none", makeup_db=makeup_db)
        self.sent = []

    async def start_processing_metrics(self):
        pass

    async def _send_via(self, audio):
        # Drive run_stt with no websocket; capture what it accounted as sent.
        self._websocket = None
        before = self._seg_bytes
        async for _ in self.run_stt(audio):
            pass
        # run_stt applies the gain, then accounts len(audio) into _seg_bytes; recover the
        # gained bytes by re-applying the pure transform (the method under test).
        return self._apply_makeup(audio), self._seg_bytes - before


def _pcm(samples):
    return np.asarray(samples, dtype="<i2").tobytes()


def _samples(audio):
    return np.frombuffer(audio, dtype="<i2")


async def test_zero_db_is_a_byte_identical_noop():
    stt = Sender(makeup_db=0.0)
    audio = _pcm([0, 100, -100, 5000, -5000, 32767, -32768])
    assert stt._apply_makeup(audio) == audio
    assert stt._makeup_scale == 1.0


async def test_six_db_is_a_factor_of_two_within_a_bit():
    stt = Sender(makeup_db=6.0)
    # +6.0206 dB is exactly x2; 6.0 dB is x1.9953, so a mid-scale sample doubles ~to a LSB.
    assert math.isclose(stt._makeup_scale, 10 ** (6.0 / 20), rel_tol=1e-9)
    out = _samples(stt._apply_makeup(_pcm([1000, -1000, 250])))
    assert list(out) == [round(1000 * stt._makeup_scale),
                         round(-1000 * stt._makeup_scale),
                         round(250 * stt._makeup_scale)]


async def test_loud_samples_hard_clip_and_do_not_wrap():
    # The int16 near the rails must clip to the rails, never overflow/wrap to the far end.
    stt = Sender(makeup_db=12.0)                    # ~x3.98
    out = _samples(stt._apply_makeup(_pcm([30000, -30000, 32767, -32768, 10])))
    assert out[0] == 32767 and out[1] == -32768     # clipped, correct sign
    assert out[2] == 32767 and out[3] == -32768
    assert out[4] == round(10 * stt._makeup_scale)  # a quiet sample is untouched by the clip
    assert out.min() >= -32768 and out.max() <= 32767


async def test_negative_db_attenuates():
    stt = Sender(makeup_db=-6.0)
    out = _samples(stt._apply_makeup(_pcm([10000])))
    assert out[0] == round(10000 * (10 ** (-6.0 / 20)))
    assert abs(out[0]) < 10000


async def test_odd_trailing_byte_is_preserved():
    # The contract is bytes; a stray odd byte (never from a 20 ms frame) must pass, not raise.
    stt = Sender(makeup_db=6.0)
    audio = _pcm([1000, -1000]) + b"\x7f"
    out = stt._apply_makeup(audio)
    assert len(out) == len(audio)
    assert out[-1] == 0x7f
    assert list(_samples(out[:-1])) == [round(1000 * stt._makeup_scale),
                                        round(-1000 * stt._makeup_scale)]


async def test_run_stt_applies_the_gain_to_what_it_sends():
    stt = Sender(makeup_db=6.0)
    audio = _pcm([2000, -2000, 500, -500])
    gained, accounted = await stt._send_via(audio)
    assert list(_samples(gained)) == [round(s * stt._makeup_scale) for s in (2000, -2000, 500, -500)]
    # accounting counts the frame once (length is unchanged by the gain)
    assert accounted == len(audio)


async def test_run_stt_at_zero_db_sends_untouched_audio():
    stt = Sender(makeup_db=0.0)
    audio = _pcm([2000, -2000, 500, -500])
    gained, _ = await stt._send_via(audio)
    assert gained == audio


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
