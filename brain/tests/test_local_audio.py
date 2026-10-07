"""local_audio: the format conversions, the real-time playback feed, the face tap and the
reconnect policy (fake aplay/arecord processes and a fake /talk; no sound card)."""
import asyncio
import json
import os
import sys
import tempfile

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
    play.push(_tone(24000, 2.0).tobytes(), voiced=True)
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


def test_mouth_level_tracks_loudness():
    assert la.mouth_level(bytes(1280)) == 0.0
    quiet = la.mouth_level(np.full(640, 50, "<i2").tobytes())      # ~ -56 dBFS
    mid = la.mouth_level(np.full(640, 2000, "<i2").tobytes())      # ~ -24 dBFS
    loud = la.mouth_level(np.full(640, 20000, "<i2").tobytes())    # ~ -4 dBFS
    assert quiet == 0.0 and 0.6 < mid < 1.0 and loud == 1.0


def test_mouth_levels_come_due_when_their_audio_is_heard():
    play, clock, sunk = _playback()
    play.push(np.tile(_tone(24000, 0.2), 1).tobytes(), voiced=True)   # 200 ms of speech
    play.pump()                                         # card primed LEAD_SECS ahead
    assert play.mouth_due(clock.t - 0.001) is None      # nothing heard yet
    first = play.mouth_due(clock.t)                     # the first chunk plays now
    assert first is not None and first > 0.5
    assert play.mouth_due(clock.t) is None              # each level is delivered once
    clock.t += 0.3
    play.pump()
    levels = [play.mouth_due(clock.t + i * 0.02) for i in range(20)]
    assert 0.0 in levels                                # the tail of silence closes it


def test_uncaptioned_audio_never_moves_the_mouth_until_a_caption_vouches_for_it():
    play, clock, sunk = _playback()
    play.push(_tone(24000, 0.5).tobytes())              # the thinking sound: no caption
    assert not play.speaking
    play.pump()
    assert play.mouth_due(clock.t + 0.05) == 0.0        # loud, but not speech
    play.mark_voiced()                                  # a caption: it was the reply after all
    assert play.speaking
    assert play.mouth_due(clock.t + 0.1) > 0.5          # chunks already handed to the card...
    clock.t += 0.2
    play.pump()
    assert play.mouth_due(clock.t + 0.1) > 0.5          # ...and the ones still queued then


def test_resync_follows_the_card_not_the_first_write():
    """aplay took 150 ms to start: the card has played less than the wall clock says.
    Re-anchored, the feed stops until it is LEAD_SECS ahead of the CARD again, and a
    chunk's mouth level comes due when the card plays it."""
    play, clock, sunk = _playback()
    play.push(_tone(24000, 2.0).tobytes(), voiced=True)
    play.pump()
    clock.t += 0.3
    play.pump()
    written = len(sunk)
    assert written * la.CHUNK_MS / 1000 >= 0.3 + la.LEAD_SECS
    played = int(0.15 * la.DEVICE_RATE)                 # really: started at +150 ms
    play.resync(played, clock.t)
    clock.t += 0.1
    play.pump()
    assert len(sunk) == written                         # backlog was 270 ms: nothing new
    # The chunk at 0.4 s of audio is heard 0.25 s after the resync (0.4 - 0.15), not at +0.1.
    pos = 0.4
    play.mouth_due(clock.t - 0.1 + (pos - 0.15) - 0.001)
    assert play._levels[0][0] / la.DEVICE_RATE == pos
    clock.t += 0.5
    play.pump()
    ahead = len(sunk) * la.CHUNK_MS / 1000 - (0.15 + 0.6)   # card has played 0.75 s now
    assert la.LEAD_SECS <= ahead < la.LEAD_SECS + 0.03


def test_echo_free_waits_out_the_last_sound():
    play, clock, sunk = _playback()
    assert play.echo_free(clock.t)
    play.push(_tone(24000, 0.1).tobytes())
    assert not play.echo_free(clock.t)                  # queued
    play.pump()
    assert not play.echo_free(clock.t + 0.1)            # still playing
    assert play.echo_free(clock.t + 0.12 + la.ECHO_TAIL_SECS)


RUNNING = """state: RUNNING
owner_pid   : 15188
trigger_time: 6167.636218837
tstamp      : 0.000000000
delay       : 4148
avail       : 680
avail_max   : 3840
-----
hw_ptr      : 28560
appl_ptr    : 32680
"""


def test_pcm_status_parsing():
    # RUNNING is verbatim from a 6.8 kernel (hda, aplay --buffer-time=100000).
    st = la.parse_pcm_status(RUNNING)
    assert st == {"state": "RUNNING", "owner_pid": 15188, "hw_ptr": 28560,
                  "appl_ptr": 32680, "delay": 4148}
    assert la.played_frames(st) == 28560 - 28          # delay beyond the ring: in flight
    assert la.parse_pcm_status("closed\n") is None
    assert la.parse_pcm_status("error -19\n") is None
    assert la.played_frames(dict(st, state="XRUN")) is None
    assert la.played_frames(dict(st, delay=10**9)) == 28560   # nonsense in-flight: ignored


def test_pcm_status_dir_from_the_device_string():
    assert la.pcm_status_dir("hw:CARD=Array,DEV=0") == "/proc/asound/Array/pcm0p"
    assert la.pcm_status_dir("hw:2,1") == "/proc/asound/card2/pcm1p"
    assert la.pcm_status_dir("hw:Array") == "/proc/asound/Array/pcm0p"
    assert la.pcm_status_dir("hw:CARD=Array") == "/proc/asound/Array/pcm0p"
    for other in ("plughw:CARD=Array,DEV=0", "default", "sysdefault:CARD=Array", "hw:../x,0"):
        assert la.pcm_status_dir(other) is None


def test_read_pcm_status_picks_our_substream():
    d = tempfile.mkdtemp()
    for sub, text in (("sub0", RUNNING.replace("15188", "999")), ("sub1", RUNNING), ("sub2", "closed\n")):
        os.makedirs(os.path.join(d, sub))
        with open(os.path.join(d, sub, "status"), "w") as f:
            f.write(text)
    assert la.read_pcm_status(d, 15188)["owner_pid"] == 15188
    assert la.read_pcm_status(d, 4242) is None
    assert la.read_pcm_status("/nonexistent", 15188) is None


def test_voice_gate_wakes_on_speech_not_on_the_room():
    gate = la.VoiceGate(-40)
    assert not any(gate.feed(-50) for _ in range(200))          # the room, idle
    assert not any(gate.feed(-20 if i % 3 == 0 else -55) for i in range(60))  # door knocks
    gate.reset()
    hits = [gate.feed(-30 if i % 5 else -48) for i in range(20)]  # speech with dips
    assert hits[-1] and not hits[10]


def _face(clock=None):
    sent = []
    face = la.FaceTap(send=lambda b: sent.append(json.loads(b)), clock=clock or _Clock())
    return face, sent


def test_face_follows_a_turn():
    clock = _Clock()
    face, sent = _face(clock)
    face.user("what's the", False)
    face.user("what's the weather", False)  # still listening: no repeat
    face.user("what's the weather", True)
    face.audio()                             # first TTS audio: not speaking yet
    assert face.assistant("It is", False)
    assert face.voiced
    face.assistant("It is", False)           # a repeat without new words keeps it voiced
    assert face.voiced
    face.assistant("It is sunny today", False)
    face.assistant("It is sunny today.", True)
    assert face.voiced                       # the last word's audio is still to come
    face.audio()                             # ... and arrives after the final: speech
    clock.t += 1                             # heard out (no audio since)
    assert not face.voiced
    face.quiet()
    assert sent == [
        {"state": "listening"}, {"state": "thinking"}, {"state": "speaking"},
        {"text": "It is sunny today."}, {"state": "idle"},
    ]


def test_face_mouth_sends_only_real_moves_and_keeps_an_open_mouth_alive():
    clock = _Clock()
    face, sent = _face(clock)
    face.mouth(0.8)                          # not speaking: nothing
    assert sent == []
    face.state("speaking")
    sent.clear()
    for lv in (0.0, 0.02, 0.5, 0.52, 0.9, 0.3, 0.0, 0.0):
        face.mouth(lv)
    assert sent == [{"mouth": 0.5}, {"mouth": 0.9}, {"mouth": 0.3}, {"mouth": 0.0}]
    sent.clear()
    face.mouth(1.0)
    for _ in range(10):                      # a steady, clamped vowel for 0.5 s
        clock.t += 0.05
        face.mouth(1.0)
    assert len(sent) >= 3 and all(e == {"mouth": 1.0} for e in sent)
    face.interrupted()                       # barge-in shuts the mouth
    assert sent[-2:] == [{"mouth": 0.0}, {"state": "listening"}]


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


def test_face_thinking_sound_is_thinking_not_speaking():
    clock = _Clock()
    face, sent = _face(clock)
    face.user("look that up", True)
    face.assistant("I'll check.", True)      # the filler, one final
    face.quiet()                             # filler heard out, no bed yet: idle
    for _ in range(int(la.FACE_BED_SECS / 0.04) + 2):   # then the typing bed, 40 ms frames
        clock.t += 0.04
        face.audio()
        face.tick()
        face.mouth(0.0)
    assert sent[-1] == {"state": "thinking"}
    for _ in range(int(40 / 0.04)):          # a 40 s consult: still thinking
        clock.t += 0.04
        face.audio()
        face.tick()
    assert sent[-1] == {"state": "thinking"}
    assert not any(e.get("state") == "speaking" for e in sent[2:])
    clock.t += la.FACE_STALE_SECS            # it ended without a reply
    face.tick()
    assert sent[-1] == {"state": "idle"}


def test_face_stuck_states_time_out_from_the_last_activity():
    clock = _Clock()
    face, sent = _face(clock)
    face.interrupted()                       # a barge-in that led nowhere
    clock.t += la.FACE_STALE_SECS - 1
    face.tick()
    assert sent[-1] == {"state": "listening"}
    face.user("hm", False)                   # activity restarts the clock
    clock.t += la.FACE_STALE_SECS - 1
    face.tick()
    assert sent[-1] == {"state": "listening"}
    clock.t += 1
    face.tick()
    assert sent[-1] == {"state": "idle"}


def test_face_ended_resets_the_avatar():
    face, sent = _face()
    face.assistant("Hello there", False)
    face.mouth(0.7)
    face.ended()
    assert sent[-2:] == [{"mouth": 0.0}, {"state": "idle"}]
    assert not face.voiced


def test_face_without_a_daemon_is_silent():
    face = la.FaceTap(path="/nonexistent/face.sock")
    face.state("speaking")                   # must not raise
    face.mouth(0.8)


def test_token_is_url_encoded(monkeypatch):
    monkeypatch.setattr(la, "GATEWAY_TOKEN", "a+b&c=d %")
    monkeypatch.setattr(la, "URL", "ws://h/talk")
    assert la._url() == "ws://h/talk?token=a%2Bb%26c%3Dd%20%25"


# ------------------------------------------------- the reconnect policy, end to end

APLAY = "import sys\nwhile sys.stdin.buffer.read(4096): pass\n"
# Paced 20 ms capture chunks: silence, or a loud tone while the flag file exists.
ARECORD = """import os, sys, time, math
flag = sys.argv[1]
loud = b"".join(int(8000 * math.sin(i / 3)).to_bytes(2, "little", signed=True) * 2 for i in range(320))
t = time.monotonic()
while True:
    sys.stdout.buffer.write(loud if os.path.exists(flag) else bytes(1280))
    sys.stdout.buffer.flush()
    t += 0.02
    time.sleep(max(0, t - time.monotonic()))
"""
DEAD = "import sys\nsys.stderr.write('arecord: pcm_read: Input/output error\\n')\nsys.exit(1)\n"


def _fake_card(monkeypatch, capture_script, flag):
    async def spawn(*argv, **kw):
        script = APLAY if argv[0] == "aplay" else capture_script
        return await asyncio.create_subprocess_exec(sys.executable, "-c", script, flag, **kw)
    monkeypatch.setattr(la, "_spawn", spawn)
    monkeypatch.setattr(la, "DEVICE", "fake")
    monkeypatch.setattr(la, "RETRY_SECS", 0.3)
    monkeypatch.setattr(la, "FACE_SOCK", os.path.join(tempfile.mkdtemp(), "face.sock"))


async def _talk(on_connect):
    """A fake /talk; on_connect(ws, n) runs per connection. Returns (server, url)."""
    import websockets
    count = [0]

    async def handler(ws):
        count[0] += 1
        await on_connect(ws, count[0])

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}/talk", count


def test_dead_card_never_dials(monkeypatch):
    flag = tempfile.mktemp()
    _fake_card(monkeypatch, DEAD, flag)

    async def run():
        async def on_connect(ws, n):
            await ws.close()
        server, url, count = await _talk(on_connect)
        monkeypatch.setattr(la, "URL", url)
        bridge = asyncio.create_task(la.run_bridge())
        await asyncio.sleep(3.0)                 # three capture attempts, a retry, more
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        server.close()
        return count[0]

    assert asyncio.run(run()) == 0


def test_first_session_dials_at_once_later_ones_wait_for_a_voice(monkeypatch):
    flag = tempfile.mktemp()
    _fake_card(monkeypatch, ARECORD, flag)
    got = {}

    async def run():
        async def on_connect(ws, n):
            if n == 1:
                await ws.close()                 # the brain hung up (idle timeout, eviction)
                return
            first = await asyncio.wait_for(ws.recv(), 2)
            got["burst"] = len(first)
            total, t0 = len(first), asyncio.get_running_loop().time()
            while asyncio.get_running_loop().time() - t0 < 0.05:   # the pre-roll, at once
                total += len(await asyncio.wait_for(ws.recv(), 1))
            got["preroll"] = total
            await ws.close()

        server, url, count = await _talk(on_connect)
        monkeypatch.setattr(la, "URL", url)
        bridge = asyncio.create_task(la.run_bridge())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if count[0]:
                break
        assert count[0] == 1                     # dialled at start, no voice needed
        await asyncio.sleep(2.0)
        assert count[0] == 1                     # ended: no redial on a timer
        open(flag, "w").close()                  # someone speaks
        for _ in range(60):
            await asyncio.sleep(0.05)
            if count[0] == 2:
                break
        os.unlink(flag)
        await asyncio.sleep(0.5)
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        server.close()
        return count[0]

    assert asyncio.run(run()) == 2
    # >= the WAKE_SECS of voice that woke it (24 kHz mono = 48 bytes/ms), sent as a burst.
    assert got["preroll"] >= 48 * la.WAKE_SECS * 1000 * 0.9, got


def test_face_reply_tail_audio_is_not_the_thinking_sound():
    """The final caption lands at the last word; that word's audio follows it. Those
    trailing frames must not read as the thinking sound (the face then sat in
    "thinking" for FACE_STALE_SECS after every reply)."""
    clock = _Clock()
    face, sent = _face(clock)
    face.assistant("Hey there, welcome back", False)
    face.assistant("Hey there, welcome back!", True)
    for _ in range(10):                      # 0.3 s of tail audio after the final
        face.audio()
        clock.t += 0.03
    clock.t += 0.1
    face.quiet()                             # heard out with audio only 0.1 s ago
    assert sent[-1] == {"state": "idle"}


def test_face_thinking_sound_after_a_reply_still_shows_thinking():
    clock = _Clock()
    face, sent = _face(clock)
    face.assistant("Let me check", False)
    face.assistant("Let me check.", True)
    clock.t += 1.0                           # past the tail grace: the bed starts
    for _ in range(50):                      # 1.5 s of typing
        face.audio()
        clock.t += 0.03
    face.quiet()
    assert sent[-1] == {"state": "thinking"}


def test_card_present_follows_the_proc_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(la, "pcm_status_dir", lambda dev: str(tmp_path / "Array" / "pcm0p")
                        if dev.startswith("hw:") else None)
    assert la.card_present("hw:CARD=Array,DEV=0") is False
    (tmp_path / "Array").mkdir()
    assert la.card_present("hw:CARD=Array,DEV=0") is True
    assert la.card_present("plughw:Array") is None


def test_a_replugged_card_ends_the_backoff_early(monkeypatch, tmp_path):
    card_dir = tmp_path / "Array"
    monkeypatch.setattr(la, "pcm_status_dir", lambda dev: str(card_dir / "pcm0p"))
    monkeypatch.setattr(la, "CARD_POLL_SECS", 0.05)

    async def run():
        async def plug_in_later():
            await asyncio.sleep(0.2)
            card_dir.mkdir()
        asyncio.get_running_loop().create_task(plug_in_later())
        t0 = asyncio.get_running_loop().time()
        early = await la.wait_for_card("hw:CARD=Array,DEV=0", 30)
        return early, asyncio.get_running_loop().time() - t0

    early, took = asyncio.run(run())
    assert early and took < 1.0                  # not the 30 s backoff

    async def present_but_failing():
        t0 = asyncio.get_running_loop().time()
        early = await la.wait_for_card("hw:CARD=Array,DEV=0", 0.3)   # card_dir exists now
        return early, asyncio.get_running_loop().time() - t0

    early, took = asyncio.run(present_but_failing())
    assert not early and took >= 0.29            # a card that is there waits it out
