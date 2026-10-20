#
# thinking_sound types the bed a long consult plays, live: a keyboard of modal keys
# (modelled on a recorded mechanical keyboard) built when the brain starts, and a
# typist per bed that schedules and renders each keystroke as it plays, in 40 ms
# chunks; TEAPORT_THINKING_WAV loops the user's own wav instead. These hold the module
# to what it promises: typing that a seed reproduces and that never repeats, as loud
# as the bed it replaced and never louder than -6 dBFS, every key press reported
# before its chunk and where its click is heard, and no numpy.random (its import
# alone is 2-3 MB resident, and nothing else in the brain loads it). An override
# plays as given, and a bad one plays live typing instead: it neither raises (warm()
# runs at the brain's start, where an exception would crash-loop the service) nor is
# written over.
#
# Run: python test_thinking_sound.py   (or via pytest test_suite.py)
#
import os
import struct
import subprocess
import sys
import wave

BRAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BRAIN)

from tempdirs import tempdir  # noqa: E402

os.environ.pop("TEAPORT_THINKING_WAV", None)   # live typing, whatever the shell set

import numpy as np  # noqa: E402

from teaport_brain import thinking_sound as ts  # noqa: E402

KEYBOARD = ts._Keyboard()


def _typed(secs, seed):
    """`secs` of a seeded typist: the PCM (as floats, before TEAPORT_THINKING_GAIN) and
    every key press as (absolute sample, letter, the chunk it was reported with)."""
    t = ts._Typist(KEYBOARD, seed=seed)
    pcm, presses = [], []
    for i in range(int(secs * 1000) // ts._CHUNK_MS):
        chunk, pressed = t.chunk()
        pcm.append(chunk)
        presses += [(i * ts._CHUNK + at, letter, i) for at, letter in pressed]
    return np.frombuffer(b"".join(pcm), dtype=np.int16) / 32767 / ts._GAIN, presses


def _write_wav(path, pcm, rate=ts._SR, channels=1):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype(np.int16).tobytes())


def _load_with(path):
    saved, ts._WAV = ts._WAV, path
    try:
        return ts._load_override()
    finally:
        ts._WAV = saved


def _contents(path):
    """The file's bytes; None for a directory or a missing file."""
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def test_the_typing_is_reproducible_and_never_repeats():
    a, pa = _typed(10, seed=1)
    b, pb = _typed(10, seed=1)
    c, _ = _typed(10, seed=2)
    assert np.array_equal(a, b) and pa == pb
    assert not np.array_equal(a, c)
    half = len(a) // 2                           # not a loop: its second half is new typing
    assert np.corrcoef(a[:half], a[half:])[0, 1] < 0.1


def test_the_typing_is_as_loud_as_before_and_peaks_under_minus_6_dbfs():
    x, presses = _typed(60, seed=1)
    rms_db = 20 * np.log10(np.sqrt(np.mean(x ** 2)))
    assert -36.5 < rms_db < -34.0, rms_db           # the loop it replaced: -35.4 RMS
    assert np.max(np.abs(x)) <= 0.5 + 1e-4, np.max(np.abs(x))
    assert 6.5 < len(presses) / 60 < 9.5, len(presses)   # steady touch typing, ~8 keys/s


def test_every_key_press_is_reported_before_its_chunk_and_heard_on_its_click():
    x, presses = _typed(30, seed=3)
    # The click is the brightest thing in a keystroke: above 5 kHz, its onset is the
    # loudest sample near the press (the switch's second strike comes 0.6-1.5 ms later
    # and ~4 dB softer).
    f = np.fft.rfftfreq(len(x), 1 / ts._SR)
    hp = np.abs(np.fft.irfft(np.fft.rfft(x) * (f > 5000), len(x)))
    w, near = int(ts._SR * 0.003), int(ts._SR * 0.002)
    on_click = 0
    for at, letter, reported in presses:
        assert letter in ts._LETTERS or letter == "space", letter
        assert at >= (reported + 1) * ts._CHUNK, (at, reported)   # announced ahead of its audio
        if w <= at < len(x) - w:
            on_click += abs(int(np.argmax(hp[at - w:at + w])) - w) <= near
    assert on_click >= 0.95 * len(presses), (on_click, len(presses))


def test_every_chunk_is_40_ms():
    t = ts._Typist(KEYBOARD, seed=4)
    assert {len(t.chunk()[0]) for _ in range(200)} == {2 * ts._CHUNK}


def test_it_never_imports_numpy_random():
    code = ("import sys\n"
            "from teaport_brain import thinking_sound as ts\n"
            "ts.warm()\n"
            "t = ts._Typist(ts._KEYBOARD)\n"
            "[t.chunk() for _ in range(100)]\n"
            "print('numpy.random' in sys.modules)\n")
    env = dict(os.environ, PYTHONPATH=BRAIN, TEAPORT_THINKING_WAV="")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip().splitlines()[-1] == "False", out.stdout


def test_an_override_wav_plays_as_given_and_loops_every_chunk():
    path = os.path.join(tempdir(), "mine.wav")
    mine = (np.sin(np.arange(ts._SR) / 7) * 8000).astype(np.int16)
    _write_wav(path, mine)
    assert np.array_equal(_load_with(path), mine)
    assert _contents(path) is not None
    saved = ts._WAV, ts._LOOP, ts._KEYBOARD
    ts._WAV, ts._LOOP, ts._KEYBOARD = path, None, None
    try:
        ts._prepare()
        assert ts._KEYBOARD is None
        assert len(ts._LOOP) == len(mine) // ts._CHUNK      # every whole chunk, the last included
        assert {len(c) for c in ts._LOOP} == {2 * ts._CHUNK}
    finally:
        ts._WAV, ts._LOOP, ts._KEYBOARD = saved


def test_a_bad_override_plays_live_typing_and_is_left_alone():
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
    _write_wav(short, np.ones(ts._CHUNK - 1))       # not one whole chunk
    for path in (d, stereo, text, torn, past_end, short, os.path.join(d, "missing.wav")):
        before = _contents(path)
        assert _load_with(path) is None, path
        assert _contents(path) == before, f"{path} was written"


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
