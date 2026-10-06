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


def _face():
    sent = []
    return la.FaceTap(send=lambda b: sent.append(__import__("json").loads(b))), sent


def test_face_follows_a_turn():
    face, sent = _face()
    face.user("what's the", False)
    face.user("what's the weather", False)  # still listening: no repeat
    face.user("what's the weather", True)
    face.speaking()                          # first TTS audio
    face.assistant("It is", False)
    face.assistant("It is sunny today", False)
    face.assistant("It is sunny today.", True)
    face.quiet()
    assert sent == [
        {"state": "listening"}, {"state": "thinking"}, {"state": "speaking"},
        {"word": "It"}, {"word": "is"}, {"word": "sunny"}, {"word": "today"},
        {"text": "It is sunny today."}, {"state": "idle"},
    ]


def test_face_barge_in_and_new_caption():
    face, sent = _face()
    face.assistant("Let me tell you", False)
    face.interrupted()
    face.assistant("Sure", False)            # a new utterance: not a prefix match
    assert sent[-2:] == [{"state": "speaking"}, {"word": "Sure"}]
    assert {"state": "listening"} in sent
    face.quiet()
    assert sent[-1] == {"state": "idle"}
    n = len(sent)
    face.quiet()                             # already idle: nothing
    assert len(sent) == n


def test_face_without_a_daemon_is_silent():
    face = la.FaceTap(path="/nonexistent/face.sock")
    face.state("listening")                  # must not raise
    face.assistant("hello there", False)
