"""local_audio: the format conversions and the real-time playback feed (no sound card)."""
import numpy as np
import soxr

from teaport_brain import local_audio as la


def _tone(rate, secs, hz=440, amp=8000):
    t = np.arange(int(rate * secs)) / rate
    return (amp * np.sin(2 * np.pi * hz * t)).astype("<i2")


def test_capture_picks_the_configured_channel_and_upsamples():
    left, right = _tone(16000, 1.0, 440), _tone(16000, 1.0, 880)
    raw = np.stack([left, right], axis=1).tobytes()
    out = {}
    for ch in (0, 1):
        rs = soxr.ResampleStream(16000, 24000, 1, dtype="int16")
        out[ch] = np.frombuffer(la.capture_to_relay(raw, rs, ch), dtype="<i2")
        assert abs(len(out[ch]) - 24000) < 1000  # 1 s at 24 kHz, minus resampler latency
    # Different channels in -> different audio out (zero crossings ~ 2 * frequency).
    zc = lambda x: int(np.sum(np.diff(np.sign(x)) != 0))
    assert 1.8 < zc(out[1]) / zc(out[0]) < 2.2


def test_relay_to_device_is_16k_stereo_with_identical_channels():
    rs = soxr.ResampleStream(24000, 16000, 1, dtype="int16")
    dev = np.frombuffer(la.relay_to_device(_tone(24000, 1.0).tobytes(), rs), dtype="<i2")
    assert abs(len(dev) - 2 * 16000) < 1500  # minus resampler latency
    frames = dev.reshape(-1, 2)
    assert (frames[:, 0] == frames[:, 1]).all()


class _Clock:
    t = 100.0
    def __call__(self):
        return self.t


def _playback():
    clock, sunk = _Clock(), []
    return la.Playback(sunk.append, clock), clock, sunk


def test_idle_playback_feeds_silence_and_stays_ahead_of_the_clock():
    play, clock, sunk = _playback()
    play.pump()
    assert sunk and all(c == bytes(len(c)) for c in sunk)
    ahead = len(sunk) * la.CHUNK_MS / 1000
    assert la.LEAD_SECS <= ahead < la.LEAD_SECS + 0.03
    n = len(sunk)
    play.pump()  # no time passed -> nothing more to write
    assert len(sunk) == n
    clock.t += 0.5
    play.pump()
    assert len(sunk) > n


def test_speech_is_played_and_clear_drops_what_was_not_yet_handed_over():
    play, clock, sunk = _playback()
    play.pump()
    quiet = len(sunk)
    play.push(_tone(24000, 2.0).tobytes())
    assert play.speaking
    clock.t += 0.5
    play.pump()
    spoken = b"".join(sunk[quiet:])
    assert any(spoken)  # real audio reached the card
    play.clear()
    assert not play.speaking
    clock.t += 0.5
    before = len(sunk)
    play.pump()
    assert all(c == bytes(len(c)) for c in sunk[before:])  # silence after the flush
