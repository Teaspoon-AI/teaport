#
# Unit test: VAD_SAMPLE_RATE=8000 runs Silero on the true narrowband signal.
#
# The SIP media is G.711 mu-law at 8 kHz — the carrier's own SDP offer is
# "m=audio 31720 RTP/AVP 0 18 101" (PCMU, G729, telephone-event) with no wideband codec
# in it, so an inbound call cannot be anything else. pjmedia decodes and resamples to
# 16 kHz for our port, and Silero — a waveform model — is handed "16 kHz" audio with no
# energy above 4 kHz.
#
# That is the leading explanation for the one thing no threshold could fix: measured
# 2026-09-09, speech sits at p50 0.88 while ~8% of genuine speech frames collapse into
# the same 0.00-0.05 band as silence, so no gate above zero separates them
# (VAD_CONFIDENCE 0.70 -> 0.35 -> 0.15 moved the flicker rate by nothing).
#
# Decimating back to 8 kHz discards nothing when the source was 8 kHz to begin with; it
# undoes the gateway's upsampling. 512 samples at 16 kHz become exactly the 256 Silero
# wants at 8 kHz, so pipecat's buffering is untouched.
#
# Run: python test_vad_sample_rate.py   (or via the suite)
#

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import teaport_brain  # noqa: E402, F401
from teaport_brain.endpointing import NarrowbandSileroMixin  # noqa: E402


class _FakeSilero:
    """Stands in for SileroVADAnalyzer: records what the model was actually given."""

    def __init__(self):
        self.sample_rate = 16000
        self.seen = []

    def voice_confidence(self, buffer):
        self.seen.append((len(buffer) // 2, self.sample_rate))
        self.last = np.frombuffer(buffer, dtype=np.int16).astype(np.float32)
        return 0.5


class Narrowband(NarrowbandSileroMixin, _FakeSilero):
    pass


def _frame(n, sr=16000, hz=220, amp=8000):
    t = np.arange(n) / sr
    return (np.sin(2 * np.pi * hz * t) * amp).astype(np.int16).tobytes()


def test_a_512_sample_frame_reaches_silero_as_256_at_8k():
    v = Narrowband()
    v.voice_confidence(_frame(512))
    assert v.seen == [(256, 8000)], v.seen


def test_the_analyzer_keeps_its_16k_identity_afterwards():
    # Only the model's input is converted; pipecat's frame budget and every other rate
    # in the pipeline must be untouched, or the buffering changes underneath us.
    v = Narrowband()
    v.voice_confidence(_frame(512))
    assert v.sample_rate == 16000, v.sample_rate


def test_the_rate_is_restored_even_if_the_model_raises():
    class Boom(NarrowbandSileroMixin, _FakeSilero):
        def voice_confidence(self, buffer):
            if len(buffer) == 512:      # the decimated call
                raise RuntimeError("model failed")
            return NarrowbandSileroMixin.voice_confidence(self, buffer)
    v = Boom()
    try:
        v.voice_confidence(_frame(512))
    except RuntimeError:
        pass
    assert v.sample_rate == 16000, "a raising model must not strand the analyzer at 8k"


def test_decimation_lowpasses_rather_than_dropping_samples():
    # Naive decimation aliases; pair-averaging is a crude half-band filter in front of
    # it. A 6 kHz tone sits above the 4 kHz Nyquist of the decimated rate, so it must be
    # attenuated rather than folded down at full amplitude.
    def rms_after(hz):
        v = Narrowband()
        v.voice_confidence(_frame(512, hz=hz))
        return float(np.sqrt((v.last ** 2).mean()))

    out_of_band, in_band = rms_after(6000), rms_after(500)
    assert out_of_band < in_band * 0.7, (
        f"6 kHz survived decimation at {out_of_band:.0f} against {in_band:.0f} for "
        "500 Hz — the lowpass is doing nothing and out-of-band energy is aliasing down")


def test_an_odd_or_tiny_frame_does_not_crash():
    v = Narrowband()
    v.voice_confidence(_frame(511))     # odd
    v.voice_confidence(b"\x00\x00")     # one sample: too small to decimate
    assert v.sample_rate == 16000


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("ALL PASS")
