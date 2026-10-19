#
# thinking_sound synthesizes the typing bed a long consult plays (a seamless loop,
# modelled on a recorded mechanical keyboard), caches it as a wav, and streams it in
# 40 ms chunks. These hold the loop to what the module promises: the same bed every
# run, as loud as the bed it replaced and never louder than -6 dBFS, and every chunk
# of it played, so the wrap-around the synthesis builds is not cut at the seam.
#
# Run: python test_thinking_sound.py   (or via pytest test_suite.py)
#
import os
import sys
import wave

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tempdirs import tempdir  # noqa: E402

# Cache into a scratch dir, not next to the code (read at import).
os.environ["TEAPORT_THINKING_WAV"] = os.path.join(tempdir(), "typing.wav")

import numpy as np  # noqa: E402

from teaport_brain import thinking_sound as ts  # noqa: E402


def test_the_bed_is_the_same_every_run():
    a, b = ts._synthesize(), ts._synthesize()
    assert a.dtype == np.int16 and len(a) == 15 * ts._SR
    assert np.array_equal(a, b)


def test_the_bed_is_as_loud_as_before_and_peaks_under_minus_6_dbfs():
    pcm = ts._synthesize().astype(np.float64) / 32767
    rms_db = 20 * np.log10(np.sqrt(np.mean(pcm ** 2)))
    assert -36.5 < rms_db < -34.0, rms_db               # the old bed: -36.4 RMS, a sparser loop
    assert np.max(np.abs(pcm)) <= 0.5, np.max(np.abs(pcm))


def test_every_chunk_of_the_loop_plays():
    chunks = ts._get_chunks()
    with wave.open(os.environ["TEAPORT_THINKING_WAV"], "rb") as w:   # cached on the miss
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (ts._SR, 1, 2)
        frames = w.getnframes()
    assert sum(len(c) for c in chunks) == 2 * frames
    assert {len(c) for c in chunks} == {2 * ts._SR * ts._CHUNK_MS // 1000}


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
