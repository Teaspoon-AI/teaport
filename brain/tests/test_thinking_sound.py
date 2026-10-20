#
# thinking_sound synthesizes the typing bed a long consult plays (a seamless loop,
# modelled on a recorded mechanical keyboard) when the brain starts, holds it in memory
# and streams it in 40 ms chunks; TEAPORT_THINKING_WAV swaps in the user's own wav.
# These hold the module to what it promises: the same bed every run, as loud as the
# bed it replaced and never louder than -6 dBFS, and every chunk of it played, so the
# wrap-around the synthesis builds is not cut at the seam. An override plays as given,
# and a bad one plays the bed instead: it neither raises (warm() runs at the brain's
# start, where an exception would crash-loop the service) nor is written over.
#
# Run: python test_thinking_sound.py   (or via pytest test_suite.py)
#
import os
import struct
import sys
import wave

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tempdirs import tempdir  # noqa: E402

os.environ.pop("TEAPORT_THINKING_WAV", None)   # the synthesized bed, whatever the shell set

import numpy as np  # noqa: E402

from teaport_brain import thinking_sound as ts  # noqa: E402

BED = ts._synthesize()


def _write_wav(path, pcm, rate=ts._SR, channels=1):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype(np.int16).tobytes())


def _load_with(path):
    saved, ts._WAV = ts._WAV, path
    try:
        return ts._load_pcm()
    finally:
        ts._WAV = saved


def _contents(path):
    """The file's bytes; None for a directory or a missing file."""
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def test_the_bed_is_the_same_every_run():
    assert BED.dtype == np.int16 and len(BED) == 15 * ts._SR
    assert np.array_equal(BED, ts._synthesize())


def test_the_bed_is_as_loud_as_before_and_peaks_under_minus_6_dbfs():
    pcm = BED.astype(np.float64) / 32767
    rms_db = 20 * np.log10(np.sqrt(np.mean(pcm ** 2)))
    assert -36.5 < rms_db < -34.0, rms_db               # the old bed: -36.4 RMS, a sparser loop
    assert np.max(np.abs(pcm)) <= 0.5, np.max(np.abs(pcm))


def test_every_chunk_of_the_loop_plays():
    chunks = ts._get_chunks()
    assert sum(len(c) for c in chunks) == 2 * len(BED)
    assert {len(c) for c in chunks} == {2 * ts._SR * ts._CHUNK_MS // 1000}


def test_an_override_wav_plays_as_given():
    path = os.path.join(tempdir(), "mine.wav")
    mine = (np.sin(np.arange(ts._SR) / 7) * 8000).astype(np.int16)
    _write_wav(path, mine)
    assert np.array_equal(_load_with(path), mine)
    assert _contents(path) is not None


def test_a_bad_override_plays_the_bed_and_is_left_alone():
    d = tempdir()
    stereo = os.path.join(d, "stereo-48k.wav")
    _write_wav(stereo, np.zeros(9600), rate=48000, channels=2)
    text = os.path.join(d, "notes.wav")
    with open(text, "w") as f:
        f.write("not a wav")
    torn = os.path.join(d, "torn.wav")
    _write_wav(torn, np.arange(4800))
    with open(torn, "r+b") as f:
        f.truncate(os.path.getsize(torn) - 1)        # an odd byte count of samples
    # A LIST chunk that claims 2 bytes past the RIFF end: wave raises RuntimeError.
    past_end = os.path.join(d, "past-riff-end.wav")
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, ts._SR, 2 * ts._SR, 2, 16)
    info = b"INFOISFT" + struct.pack("<I", 6) + b"Lavf\0\0"
    with open(past_end, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 4 + len(fmt) + 8 + len(info)) + b"WAVE" + fmt
                + b"LIST" + struct.pack("<I", len(info) + 2) + info
                + b"data" + struct.pack("<I", 9600) + bytes(9600))
    short = os.path.join(d, "short.wav")
    _write_wav(short, np.ones(ts._SR * ts._CHUNK_MS // 1000 - 1))   # not one whole chunk
    for path in (d, stereo, text, torn, past_end, short, os.path.join(d, "missing.wav")):
        before = _contents(path)
        assert np.array_equal(_load_with(path), BED), path
        assert _contents(path) == before, f"{path} was written"


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
