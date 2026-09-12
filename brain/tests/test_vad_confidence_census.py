#
# Unit test: the per-frame confidence census measures the whole distribution.
#
# The transition log can only ever show dips that CROSS the gate, so every reading is
# truncated by the threshold being measured. On 2026-09-09 that produced three rounds of
# tuning on self-confirming evidence: dips read as median 0.59 through a 0.70 gate, 0.26
# through 0.35, and still-below-0.15 through 0.15 — each number an artifact of where the
# gate sat, and lowering the gate never reduced the flicker (0.91 -> 0.94 per turn) because
# the dips simply went lower too.
#
# The census records EVERY frame instead, split on the volume gate: `loud` is the
# population the confidence threshold actually decides on (100% of the flicker was
# confidence dropping while volume stayed above its gate — 216/216 then 138/138), and
# `quiet` is the control showing what real silence looks like.
#
# Run: python test_vad_confidence_census.py   (or via the suite)
#

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import teaport_brain  # noqa: E402, F401
import teaport_brain.endpoint_debug as ed  # noqa: E402


class _Params:
    min_volume = 0.6
    confidence = 0.35


def _vad():
    """An InstrumentedSileroVAD with the census state but no ONNX model."""
    v = ed.InstrumentedSileroVAD.__new__(ed.InstrumentedSileroVAD)
    v._dbg_conf = 0.0
    v._dbg_loud = [0] * ed._DIST_BUCKETS
    v._dbg_quiet = [0] * ed._DIST_BUCKETS
    v._dbg_frames = 0
    v._dbg_dropped = 0
    v._params = _Params()
    return v


def test_frames_are_split_on_the_volume_gate():
    v = _vad()
    v._dbg_sample(0.9, 0.8)    # loud
    v._dbg_sample(0.1, 0.8)    # loud
    v._dbg_sample(0.02, 0.2)   # quiet
    assert sum(v._dbg_loud) == 2, "loud frames must be the ones above min_volume"
    assert sum(v._dbg_quiet) == 1
    # ...and land in the right buckets (0.05 wide).
    assert v._dbg_loud[int(0.9 * ed._DIST_BUCKETS)] == 1
    assert v._dbg_quiet[int(0.02 * ed._DIST_BUCKETS)] == 1


def test_the_dump_reports_what_the_gate_rejects():
    # The number the whole exercise needs: how much of the audio the VOLUME gate already
    # calls speech is being thrown away by the CONFIDENCE gate.
    v = _vad()
    for _ in range(30):
        v._dbg_sample(0.10, 0.8)   # loud but below a 0.35 conf gate
    for _ in range(70):
        v._dbg_sample(0.80, 0.8)   # loud and above it
    lines = []
    real = ed.logger.info
    ed.logger.info = lambda m, *a, **k: lines.append(str(m))
    try:
        v._dbg_dump()
    finally:
        ed.logger.info = real
    joined = " ".join(lines)
    assert "rejects 30% of loud frames" in joined, joined
    assert "loud n=100" in joined, joined


def test_percentiles_track_the_distribution():
    v = _vad()
    for _ in range(90):
        v._dbg_sample(0.05, 0.8)
    for _ in range(10):
        v._dbg_sample(0.95, 0.8)
    lines = []
    real = ed.logger.info
    ed.logger.info = lambda m, *a, **k: lines.append(str(m))
    try:
        v._dbg_dump()
    finally:
        ed.logger.info = real
    head = lines[0]
    assert "p50=0.07" in head or "p50=0.05" in head, head   # bucket centre of [0.05,0.10)
    assert "p90=0.07" in head or "p90=0.05" in head, head


def test_a_bad_value_neither_records_nor_raises():
    v = _vad()
    before = sum(v._dbg_loud) + sum(v._dbg_quiet)
    v._dbg_sample(float("nan"), 0.8)   # NaN is what the probe reports when it fails
    v._dbg_sample("not a number", 0.8)
    assert sum(v._dbg_loud) + sum(v._dbg_quiet) == before, "unplaceable values must not bucket"
    assert v._dbg_dropped == 2, "...and must be COUNTED, not silently dropped"
    lines = []
    real = ed.logger.info
    ed.logger.info = lambda m, *a, **k: lines.append(str(m))
    try:
        v._dbg_dump()
    finally:
        ed.logger.info = real
    assert "DROPPED 2 unplaceable frames" in " ".join(lines), (
        "a census with a hole in it must say so; that is how the last broken probe hid")


def test_the_dump_never_raises_into_the_audio_path():
    v = _vad()
    v._dbg_loud = None            # force the dump to fail internally
    v._dbg_dump()                 # must not raise: this runs in the VAD executor


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("ALL PASS")
