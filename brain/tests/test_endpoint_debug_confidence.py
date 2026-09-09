#
# Unit test: the VAD probe reports the confidence the VAD actually used.
#
# InstrumentedSileroVAD exists to answer one question — is real speech sitting on top
# of the confidence/min_volume gates? — and for that it has to report the number Silero
# returned. It did not.
#
# SileroVADAnalyzer.voice_confidence returns `self._model(...)[0]`, a shape-(1,) array.
# numpy 1.25 deprecated and 2.x REMOVED float() on any array with ndim > 0 ("only
# 0-dimensional arrays can be converted to Python scalars"), so on numpy 2.5 — the box
# and the lockfile both — the probe's float(c) raised TypeError on every single frame
# and its except wrote 0.0.
#
# Live 2026-09-09 17:33, the call the probe was turned on for: 104 state transitions,
# every one logged conf=0.00/0.7, while the VAD was plainly reaching SPEAKING — which
# vad_analyzer.py:211 gates on `confidence >= 0.7 and volume >= 0.6`. The one field the
# instrument exists to show was the one field it never showed, and 0.0 is a
# VALID-LOOKING confidence, so it read as "Silero hears no voice" rather than as a
# broken probe. The vol= column was real throughout, which is what made the reading
# plausible enough to act on.
#
# So this pins both halves: the value is the model's, and a conversion that still fails
# surfaces as NaN — unmistakably an instrument fault rather than a measurement.
#
# Run: python test_endpoint_debug_confidence.py   (or via the suite)
#

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import teaport_brain  # noqa: E402, F401
from teaport_brain.endpoint_debug import InstrumentedSileroVAD  # noqa: E402


class _FakeParent(InstrumentedSileroVAD):
    """Skips the ONNX model; returns whatever shape the parent might hand back."""

    def __init__(self, value):
        self._value = value
        self._dbg_conf = 0.0

    def voice_confidence(self, buffer):
        # Re-enter the real override with our stand-in as "super()".
        return InstrumentedSileroVAD.voice_confidence(self, buffer)

    def _super_value(self):
        return self._value


def _probe(value):
    """Run the real override with `value` as the model's return."""
    vad = _FakeParent.__new__(_FakeParent)
    vad._dbg_conf = 0.0
    # Patch the bound super() call by shadowing SileroVADAnalyzer.voice_confidence.
    import teaport_brain.endpoint_debug as ed
    real = ed.SileroVADAnalyzer.voice_confidence
    ed.SileroVADAnalyzer.voice_confidence = lambda self, buf: value
    try:
        InstrumentedSileroVAD.voice_confidence(vad, b"")
    finally:
        ed.SileroVADAnalyzer.voice_confidence = real
    return vad._dbg_conf


def test_a_shape_1_array_is_the_shape_silero_actually_returns():
    # The exact shape that broke it: model(...)[0] on the real analyzer.
    got = _probe(np.array([0.83], dtype=np.float32))
    assert abs(got - 0.83) < 1e-6, (
        f"the probe reported {got!r} for a shape-(1,) confidence of 0.83 — this is the "
        "numpy-2 float() removal, and 0.0 reads as 'Silero hears no voice'")


def test_a_zero_d_array_and_a_plain_float_still_work():
    assert abs(_probe(np.float32(0.42)) - 0.42) < 1e-6
    assert abs(_probe(0.42) - 0.42) < 1e-6


def test_a_value_that_cannot_convert_reports_nan_not_a_plausible_number():
    got = _probe(np.array([0.1, 0.2], dtype=np.float32))  # size > 1: genuinely ambiguous
    assert math.isnan(got), (
        f"a failed conversion reported {got!r}; it must be NaN, because any real-looking "
        "number lets a broken instrument pass for a measurement")


def test_the_probe_never_raises_into_the_audio_path():
    # voice_confidence runs inside the VAD executor; raising there kills the transport
    # audio task and with it all transcription.
    class Hostile:
        def __array__(self, *a, **k):
            raise RuntimeError("nope")
    try:
        got = _probe(Hostile())
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"the probe raised into the audio path: {e!r}")
    assert math.isnan(got) or isinstance(got, float)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("ALL PASS")
