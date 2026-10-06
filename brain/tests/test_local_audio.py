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
        {"text": "It is sunny today."}, {"state": "idle"},
    ]


def test_face_mouth_sends_only_real_moves():
    face, sent = _face()
    for lv in (0.0, 0.02, 0.5, 0.52, 0.9, 0.3, 0.0, 0.0):
        face.mouth(lv)
    assert sent == [{"mouth": 0.5}, {"mouth": 0.9}, {"mouth": 0.3}, {"mouth": 0.0}]
    face.mouth(0.7)
    face.interrupted()                       # barge-in shuts the mouth
    assert sent[-2:] == [{"mouth": 0.0}, {"state": "listening"}]


def test_mouth_level_tracks_loudness():
    assert la.mouth_level(bytes(1280)) == 0.0
    quiet = la.mouth_level(np.full(640, 50, "<i2").tobytes())      # ~ -56 dBFS
    mid = la.mouth_level(np.full(640, 2000, "<i2").tobytes())      # ~ -24 dBFS
    loud = la.mouth_level(np.full(640, 20000, "<i2").tobytes())    # ~ -4 dBFS
    assert quiet == 0.0 and 0.6 < mid < 1.0 and loud == 1.0


def test_mouth_levels_come_due_when_their_audio_is_heard():
    play, clock, sunk = _playback()
    play.push(np.tile(_tone(24000, 0.2), 1).tobytes())   # 200 ms of speech
    play.pump()                                         # card primed LEAD_SECS ahead
    assert play.mouth_due(clock.t - 0.001) is None      # nothing heard yet
    first = play.mouth_due(clock.t)                     # the first chunk plays now
    assert first is not None and first > 0.5
    assert play.mouth_due(clock.t) is None              # each level is delivered once
    clock.t += 0.3
    play.pump()
    levels = [play.mouth_due(clock.t + i * 0.02) for i in range(20)]
    assert 0.0 in levels                                # the tail of silence closes it


def test_face_barge_in_and_new_caption():
    face, sent = _face()
    face.assistant("Let me tell you", False)
    face.interrupted()
    face.assistant("Sure", False)            # a new utterance: speaking again
    assert sent == [{"state": "speaking"}, {"state": "listening"}, {"state": "speaking"}]
    face.quiet()
    assert sent[-1] == {"state": "idle"}
    n = len(sent)
    face.quiet()                             # already idle: nothing
    assert len(sent) == n


def test_face_without_a_daemon_is_silent():
    face = la.FaceTap(path="/nonexistent/face.sock")
    face.state("listening")                  # must not raise
    face.mouth(0.8)
