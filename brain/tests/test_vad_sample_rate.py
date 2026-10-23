#
# Unit test: a VAD sample rate of 8000 runs Silero on the true narrowband signal.
# It is per front-end (issue #215; test_turn_settings.py): 8000 for the phone by
# default, the shared 16000 for /talk and the room mic.
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

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

for k, v in (("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime"),
             ("LLM_BASE_URL", "http://127.0.0.1:9/v1"), ("LLM_API_KEY", "not-a-real-key")):
    os.environ.setdefault(k, v)

from loguru import logger  # noqa: E402

import teaport_brain  # noqa: E402, F401
from teaport_brain import agent_session, endpoint_debug  # noqa: E402
from teaport_brain.endpointing import NarrowbandSileroMixin  # noqa: E402
from teaport_brain.speech_onset import SpeechOnsetMixin  # noqa: E402


class _FakeSilero:
    """Stands in for SileroVADAnalyzer: records what the model was actually given.

    `sample_rate` is a READ-ONLY property here because it is one on pipecat's
    VADAnalyzer. The first version of this stub made it a plain attribute, so the mixin
    could assign to it and every test passed -- while the real class raised once per
    audio frame in production and took the whole VAD down with it. A stub that is easier
    to satisfy than the class it stands for tests nothing.
    """

    def __init__(self):
        self._sample_rate = 16000
        self.seen = []

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

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


def test_the_mixin_matches_the_REAL_analyzer_contract():
    """Pin the assumption against pipecat itself, not against the stub.

    The stub can drift from the class it stands for -- that is exactly how the read-only
    `sample_rate` property got past five green tests and then raised once per audio
    frame in production. This asserts the two facts the mixin depends on directly on
    pipecat's own class: that `sample_rate` is a property with no setter (so assigning
    it is a bug), and that `_sample_rate` is the backing field the mixin may swap.
    """
    from pipecat.audio.vad.vad_analyzer import VADAnalyzer

    prop = getattr(VADAnalyzer, "sample_rate", None)
    assert isinstance(prop, property), "sample_rate stopped being a property"
    assert prop.fset is None, (
        "sample_rate GAINED a setter — the mixin may now assign it directly, and the "
        "_sample_rate workaround (with this test) can go")
    assert "_sample_rate" in VADAnalyzer.__init__.__code__.co_names, (
        "VADAnalyzer no longer backs sample_rate with _sample_rate; the mixin's swap "
        "has nothing to write to and will silently stop converting the rate")


def test_an_odd_or_tiny_frame_does_not_crash():
    v = Narrowband()
    v.voice_confidence(_frame(511))     # odd
    v.voice_confidence(b"\x00\x00")     # one sample: too small to decimate
    assert v.sample_rate == 16000


def test_the_session_analyzer_is_narrowband_at_8000_alone():
    # agent_session._vad_cls takes the session's rate (TurnSettings.vad_sample_rate), not
    # a process-wide one: one process builds a phone call's analyzer and a Talk one's.
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    narrow, wide = agent_session._vad_cls(8000), agent_session._vad_cls(16000)
    assert issubclass(narrow, NarrowbandSileroMixin) and not issubclass(wide, NarrowbandSileroMixin)
    for cls in (narrow, wide):
        # The onset mixin is outermost, so it reads whichever confidence the rest made.
        assert cls.__mro__[1] is SpeechOnsetMixin, cls.__mro__
        assert issubclass(cls, SileroVADAnalyzer)
    mro = narrow.__mro__
    assert mro.index(NarrowbandSileroMixin) < mro.index(SileroVADAnalyzer), mro


def test_the_narrowband_mixin_stacks_with_the_debug_analyzer():
    saved, endpoint_debug.ENABLED = endpoint_debug.ENABLED, True
    try:
        cls = agent_session._vad_cls(8000)
    finally:
        endpoint_debug.ENABLED = saved
    # Onset outermost, then the narrowband conversion, then the instrumented model: the
    # debug census logs the confidence the 8 kHz model gave.
    mro = cls.__mro__
    order = [mro.index(c) for c in (SpeechOnsetMixin, NarrowbandSileroMixin,
                                    endpoint_debug.InstrumentedSileroVAD)]
    assert order == sorted(order), mro


def test_a_rate_that_is_neither_warns_and_runs_the_stock_analyzer():
    lines = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        cls = agent_session._vad_cls(800)
    finally:
        logger.remove(sink)
    assert not issubclass(cls, NarrowbandSileroMixin)
    assert any("800 is neither 16000" in line for line in lines), lines
    lines.clear()
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        agent_session._vad_cls(8000), agent_session._vad_cls(16000)
    finally:
        logger.remove(sink)
    assert not lines, lines


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("ALL PASS")
