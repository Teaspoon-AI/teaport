#
# Unit test: one ONNX session per model, shared by every session's analyzers
# (teaport_brain.onnx_sessions).
#
# What sharing must not change is what each analyzer hears. Silero is recurrent: its
# state and context ride on the per-analyzer SileroOnnxModel and pass THROUGH the
# InferenceSession as inputs and outputs, so two analyzers on one session must score a
# stream exactly as each scores it alone -- interleaved frame by frame (a room session
# and a phone call on one box), and from two threads at once (each analyzer runs its
# model on its own executor thread). The test proves it can see a leak: the same
# interleaving through ONE model, whose state really is shared, does not match.
#
# And the seam itself: install() takes on both pipecat sites, every analyzer of a model
# gets the same session, the brain's options are on it beside pipecat's own, a model
# at any other path (or with other options) still builds -- its own session, cached --
# and nothing outside those two pipecat modules sees the stand-in.
#
# Run: python test_onnx_sessions.py
#
import os
import shutil
import sys
import tempfile
import threading
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

import numpy as np  # noqa: E402
import onnxruntime  # noqa: E402

from teaport_brain import onnx_sessions  # noqa: E402

onnx_sessions.install()

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3  # noqa: E402
from pipecat.audio.vad.silero import SileroOnnxModel, SileroVADAnalyzer  # noqa: E402

from teaport_brain.endpointing import EagerSmartTurnAnalyzer  # noqa: E402

RATE = 16000
FRAME = 512  # Silero's 16 kHz frame
_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "test")


def _speech(name: str, seed: int) -> np.ndarray:
    """A real utterance (brain/test/*.wav) as float32, or a voiced-ish synthetic stand-in
    where the fixtures were not copied along with the tests."""
    path = os.path.join(_FIXTURES, name)
    if os.path.exists(path):
        with wave.open(path) as w:
            assert w.getframerate() == RATE and w.getnchannels() == 1
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    else:
        rng = np.random.default_rng(seed)
        t = np.arange(RATE * 3) / RATE
        gate = np.sin(2 * np.pi * (0.7 + 0.2 * seed) * t) > 0
        a = (4000 * np.sin(2 * np.pi * (180 + 40 * seed) * t) * gate
             + rng.normal(0, 400, t.size)).astype(np.int16)
    return a.astype(np.float32) / 32768.0


A = _speech("question.wav", 1)
B = _speech("question_host_status.wav", 2)


def _frames(x):
    return [x[i:i + FRAME] for i in range(0, x.size - FRAME + 1, FRAME)]


def _vad():
    v = SileroVADAnalyzer(sample_rate=RATE)
    v.set_sample_rate(RATE)
    return v


def _score(model, frames):
    # Straight to the model (as selfcheck.py does): voice_confidence() also resets the
    # state every 5 s of WALL time, which would make the comparison timing-dependent.
    return [float(np.asarray(model(f, RATE)).ravel()[0]) for f in frames]


def test_install_takes_on_both_pipecat_sites():
    got = onnx_sessions.install()
    assert got == {"pipecat.audio.vad.silero": True,
                   "pipecat.audio.turn.smart_turn.local_smart_turn_v3": True}, got
    # Idempotent, and scoped: the real module is untouched.
    assert onnx_sessions.install() == got
    assert onnxruntime.InferenceSession is not onnx_sessions.shared_session
    assert isinstance(onnxruntime.InferenceSession, type)


def test_every_analyzer_shares_one_session_per_model():
    v1, v2 = _vad(), _vad()
    assert v1._model is not v2._model, "each analyzer must keep its own SileroOnnxModel"
    assert v1._model.session is v2._model.session
    s1 = EagerSmartTurnAnalyzer(complete_threshold=0.5)
    s2 = LocalSmartTurnAnalyzerV3()  # the stock class shares it too (selfcheck builds one)
    assert s1._session is s2._session
    assert s1._session is not v1._model.session


def test_the_brain_options_are_on_beside_pipecats():
    vo = _vad()._model.session.get_session_options()
    assert vo.enable_cpu_mem_arena is False and vo.enable_mem_pattern is False
    assert (vo.intra_op_num_threads, vo.inter_op_num_threads) == (1, 1)
    so = EagerSmartTurnAnalyzer()._session.get_session_options()
    assert so.enable_cpu_mem_arena is False and so.enable_mem_pattern is False
    assert (so.intra_op_num_threads, so.inter_op_num_threads) == (1, 1)
    assert so.execution_mode == onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    assert so.graph_optimization_level == onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL


def test_interleaved_streams_keep_their_own_state():
    fa, fb = _frames(A), _frames(B)
    ref_a, ref_b = _score(_vad()._model, fa), _score(_vad()._model, fb)
    # Not vacuous: speech and pauses both, and the two streams differ.
    assert max(ref_a) > 0.5 and min(ref_a) < 0.5, (min(ref_a), max(ref_a))
    assert ref_a[:20] != ref_b[:20]
    va, vb = _vad(), _vad()
    assert va._model.session is vb._model.session
    got_a, got_b = [], []
    for i in range(max(len(fa), len(fb))):
        if i < len(fa):
            got_a += _score(va._model, [fa[i]])
        if i < len(fb):
            got_b += _score(vb._model, [fb[i]])
    assert got_a == ref_a and got_b == ref_b
    # The control: the same interleaving through ONE model -- state genuinely shared --
    # does not reproduce the reference, so the check above would have caught a leak.
    one = _vad()._model
    leak_a = []
    for i in range(len(fa)):
        leak_a += _score(one, [fa[i]])
        if i < len(fb):
            _score(one, [fb[i]])
    assert leak_a != ref_a, "a shared state went unnoticed: the test cannot see a leak"


def test_concurrent_runs_on_one_session_match_running_alone():
    fa, fb = _frames(A), _frames(B)
    ref_a, ref_b = _score(_vad()._model, fa), _score(_vad()._model, fb)
    st = EagerSmartTurnAnalyzer()
    st.set_sample_rate(RATE)
    windows = [A[: n] for n in range(RATE, A.size, RATE // 2)] + [B[: n] for n in
                                                                  range(RATE, B.size, RATE // 2)]
    ref_st = [LocalSmartTurnAnalyzerV3._predict_endpoint(st, w)["probability"] for w in windows]
    for _ in range(3):
        va, vb = _vad(), _vad()
        s1, s2 = EagerSmartTurnAnalyzer(), EagerSmartTurnAnalyzer()
        for s in (s1, s2):
            s.set_sample_rate(RATE)
        assert s1._session is s2._session is st._session
        out: dict = {}
        go = threading.Barrier(4)

        def vad(key, model, frames):
            go.wait()
            out[key] = _score(model, frames)

        def turn(key, s):
            go.wait()
            out[key] = [LocalSmartTurnAnalyzerV3._predict_endpoint(s, w)["probability"]
                        for w in windows]

        threads = [threading.Thread(target=vad, args=("a", va._model, fa)),
                   threading.Thread(target=vad, args=("b", vb._model, fb)),
                   threading.Thread(target=turn, args=("s1", s1)),
                   threading.Thread(target=turn, args=("s2", s2))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert out["a"] == ref_a and out["b"] == ref_b
        assert out["s1"] == ref_st and out["s2"] == ref_st


def test_another_model_path_or_options_still_builds_its_own_session():
    bundled = _vad()._model.session
    with tempfile.TemporaryDirectory() as d:
        silero = os.path.join(d, "renamed_vad.onnx")
        shutil.copy(_silero_path(), silero)
        m1, m2 = SileroOnnxModel(silero), SileroOnnxModel(silero)
        assert m1.session is m2.session and m1.session is not bundled
        assert _score(m1, _frames(A)[:40]) == _score(_vad()._model, _frames(A)[:40])
        # A custom Smart Turn path (pipecat's smart_turn_model_path) works and is cached.
        turn = os.path.join(d, "turn.onnx")
        shutil.copy(_smart_turn_path(), turn)
        c1 = LocalSmartTurnAnalyzerV3(smart_turn_model_path=turn)
        c2 = LocalSmartTurnAnalyzerV3(smart_turn_model_path=turn)
        assert c1._session is c2._session is not EagerSmartTurnAnalyzer()._session
        c1.set_sample_rate(RATE)
        p = c1._predict_endpoint(A)["probability"]
        assert 0.0 <= p <= 1.0
        # Other options: another session, never a shared one built for different ones.
        so = onnxruntime.SessionOptions()
        so.intra_op_num_threads = 2
        other = onnx_sessions.shared_session(silero, sess_options=so)
        assert other is not m1.session
        assert other.get_session_options().enable_cpu_mem_arena is False
        assert onnx_sessions.shared_session(silero, sess_options=so) is other
    # A serialized model (bytes): built with the brain's options, never cached.
    with open(_silero_path(), "rb") as f:
        blob = f.read()
    b1, b2 = onnx_sessions.shared_session(blob), onnx_sessions.shared_session(blob)
    assert b1 is not b2 and b1.get_session_options().enable_mem_pattern is False


def _silero_path():
    from importlib import resources
    return str(resources.files("pipecat.audio.vad.data").joinpath("silero_vad.onnx"))


def _smart_turn_path():
    from importlib import resources
    return str(resources.files("pipecat.audio.turn.smart_turn.data")
               .joinpath("smart-turn-v3.2-cpu.onnx"))


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
