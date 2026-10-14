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
# `quiet` is the control. On the room mic a silent room reads over the volume gate
# (issue #159), so the dump states shares of all frames rather than "rejects N% of loud
# frames", and a wake session's asleep and awake hours are counted apart.
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
    v._dbg_phase = None
    v._dbg_reset()
    v._params = _Params()
    return v


def _dumped(fn):
    lines = []
    real = ed.logger.info
    ed.logger.info = lambda m, *a, **k: lines.append(str(m))
    try:
        fn()
    finally:
        ed.logger.info = real
    return lines


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


def test_the_dump_states_shares_of_all_frames_not_rejections():
    # 30 frames over the volume gate under the conf gate, 70 over both, 100 quiet: the
    # speech share is of ALL frames (35%), and nothing reads as "rejected" -- on the room
    # mic the frames over the volume gate are mostly a silent room (issue #159).
    v = _vad()
    for _ in range(30):
        v._dbg_sample(0.10, 0.8)
    for _ in range(70):
        v._dbg_sample(0.80, 0.8)
    for _ in range(100):
        v._dbg_sample(0.02, 0.2)
    joined = " ".join(_dumped(v._dbg_dump))
    assert "200 frames" in joined and "vol>=0.6: 50% of frames" in joined, joined
    assert "speech (conf>=0.35 and vol>=0.6): 35% of frames" in joined, joined
    assert "reject" not in joined and "loud" not in joined, joined


def test_a_phase_change_dumps_the_old_phase_and_starts_again():
    # A wake session: hours asleep, minutes awake. The census of each is its own.
    v = _vad()
    phase = ["asleep"]
    v.census_phase = lambda: phase[0]
    for _ in range(40):
        v._dbg_sample(0.02, 0.55)     # the silent room
    phase[0] = "awake"
    lines = _dumped(lambda: v._dbg_sample(0.9, 0.9))
    assert lines and lines[0].startswith("[VAD-DIST asleep] 40 frames"), lines
    assert "at phase end" in lines[0], lines
    assert v._dbg_frames == 1 and sum(v._dbg_quiet) == 0, "the awake census starts empty"
    assert _dumped(v._dbg_dump)[0].startswith("[VAD-DIST awake] 1 frames"), "and is labelled"


def test_a_session_without_phases_is_one_census():
    v = _vad()
    for _ in range(5):
        v._dbg_sample(0.5, 0.9)
    assert v._dbg_frames == 5
    assert _dumped(v._dbg_dump)[0].startswith("[VAD-DIST session] 5 frames")


def test_a_failing_phase_never_breaks_the_audio_path():
    v = _vad()
    v.census_phase = lambda: 1 / 0
    v._dbg_sample(0.5, 0.9)           # must not raise
    assert v._dbg_frames == 1 and v._dbg_phase == "?"


def test_percentiles_track_the_distribution():
    v = _vad()
    for _ in range(90):
        v._dbg_sample(0.05, 0.8)
    for _ in range(10):
        v._dbg_sample(0.95, 0.8)
    head = _dumped(v._dbg_dump)[0]
    assert "p50=0.07" in head or "p50=0.05" in head, head   # bucket centre of [0.05,0.10)
    assert "p90=0.07" in head or "p90=0.05" in head, head


def test_a_bad_value_neither_records_nor_raises():
    v = _vad()
    before = sum(v._dbg_loud) + sum(v._dbg_quiet)
    v._dbg_sample(float("nan"), 0.8)   # NaN is what the probe reports when it fails
    v._dbg_sample("not a number", 0.8)
    assert sum(v._dbg_loud) + sum(v._dbg_quiet) == before, "unplaceable values must not bucket"
    assert v._dbg_dropped == 2, "...and must be COUNTED, not silently dropped"
    lines = _dumped(v._dbg_dump)
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
