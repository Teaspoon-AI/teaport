"""local_audio: the format conversions, the real-time playback feed, the face tap and the
reconnect policy (fake aplay/arecord processes and a fake /talk; no sound card)."""
import asyncio
import json
import os
import sys
import tempfile

import numpy as np
import pytest
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
    assert la._url() == "ws://h/talk?features=volume,restart,local&token=a%2Bb%26c%3Dd%20%25"


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
    monkeypatch.setattr(la, "VOLUME_FILE", os.path.join(tempfile.mkdtemp(), "volume.json"))

    async def unknown():
        return None                              # a brain that cannot say (no /talk/status here)
    monkeypatch.setattr(la, "talk_active", unknown)


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


# ------------------------------------- back-off when another Talk client takes the box

def test_backoff_holds_while_a_session_is_live_and_for_the_grace_after():
    clock = _Clock()
    b = la.Backoff(60, clock=clock)
    assert not b.over(True)
    clock.t += 59
    assert not b.over(False)                    # none live, but the taker was live 59 s ago
    clock.t += 30
    assert not b.over(True)                     # live again: the grace starts over
    clock.t += 59
    assert not b.over(None)
    clock.t += 1
    assert b.over(None)                         # a brain that cannot say counts as none


def test_backoff_counts_from_the_eviction_itself():
    clock = _Clock()
    b = la.Backoff(60, clock=clock)
    clock.t += 10
    assert not b.over(False)                    # gone 10 s after it took the box: still theirs
    clock.t += 50
    assert b.over(False)


def test_only_the_taken_close_code_is_taken():
    from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
    from websockets.frames import Close

    def closed(code, cls=ConnectionClosedError):
        return cls(Close(code, ""), Close(code, ""), True)
    assert la.taken(closed(la.TAKEN_CLOSE_CODE))
    assert not la.taken(closed(1000, ConnectionClosedOK))   # ended: idle timeout, STT busy
    assert not la.taken(closed(1012))                       # brain restarting
    assert not la.taken(ConnectionClosedError(None, None))  # dropped (network, crash)
    assert not la.taken(OSError("boom"))
    assert not la.taken(None)


def test_status_is_asked_next_to_talk_with_the_token_in_a_header(monkeypatch):
    """Never ?token=: uvicorn's access log would record it on every poll."""
    import io
    import urllib.request
    sent = []

    def urlopen(req, timeout):
        sent.append(req)
        return io.BytesIO(b'{"active": true}')
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(la, "URL", "ws://127.0.0.1:7861/talk")
    monkeypatch.setattr(la, "GATEWAY_TOKEN", "")
    assert la._talk_active_sync() is True
    assert sent[-1].full_url == "http://127.0.0.1:7861/talk/status"
    assert not sent[-1].has_header("Authorization")
    monkeypatch.setattr(la, "URL", "wss://box:443/talk/?x=1")
    monkeypatch.setattr(la, "GATEWAY_TOKEN", "a+b")
    assert la._talk_active_sync() is True
    assert sent[-1].full_url == "https://box:443/talk/status"
    assert sent[-1].get_header("Authorization") == "Bearer a+b"

    def refused(req, timeout):
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert la._talk_active_sync() is None


def _redials(monkeypatch, close_code, active):
    """A first session the brain closes with `close_code`, then someone talking near the
    box (the flag) while the brain reports `active()` for /talk/status. Returns the
    connection count after 1 s of voice."""
    flag = tempfile.mktemp()
    _fake_card(monkeypatch, ARECORD, flag)
    monkeypatch.setattr(la, "BACKOFF_POLL_SECS", 0.05)
    polls, conns = [0], [0]

    async def talk_active():
        if not conns[0]:
            return None                          # the start-up check: no session yet
        polls[0] += 1
        return active()
    monkeypatch.setattr(la, "talk_active", talk_active)

    async def run():
        async def on_connect(ws, n):
            conns[0] = n
            if n == 1:
                await ws.close(code=close_code)
                return
            await ws.close()

        server, url, count = await _talk(on_connect)
        monkeypatch.setattr(la, "URL", url)
        bridge = asyncio.create_task(la.run_bridge())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if count[0]:
                break
        await asyncio.sleep(0.3)
        open(flag, "w").close()                  # someone speaks, and keeps speaking
        await asyncio.sleep(1.0)
        os.unlink(flag)
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        server.close()
        return count[0]

    return asyncio.run(run()), polls[0]


def test_taken_by_another_client_no_voice_redial_while_it_is_live(monkeypatch):
    """The dashboard took the box and is in use: a voice in the room (its own user,
    say) must not take it back, and the bridge only polls the brain meanwhile."""
    monkeypatch.setattr(la, "BACKOFF_SECS", 0.2)
    count, polls = _redials(monkeypatch, la.TAKEN_CLOSE_CODE, lambda: True)
    assert count == 1 and polls >= 5


def test_taken_then_the_other_session_ends_voice_redials_after_the_grace(monkeypatch):
    monkeypatch.setattr(la, "BACKOFF_SECS", 0.2)
    count, _ = _redials(monkeypatch, la.TAKEN_CLOSE_CODE, lambda: False)
    assert count >= 2


def test_a_brain_restart_or_a_plain_end_redials_on_voice_as_before(monkeypatch):
    """Not taken: no back-off, the brain is not even asked."""
    monkeypatch.setattr(la, "BACKOFF_SECS", 3600)
    for code in (1000, 1012):
        count, polls = _redials(monkeypatch, code, lambda: True)
        assert count >= 2 and polls == 0, code


def test_backoff_zero_treats_taken_as_any_end(monkeypatch):
    """LOCAL_AUDIO_BACKOFF_SECS=0: no back-off at all, not one of 0 s — no poll, no lines."""
    monkeypatch.setattr(la, "BACKOFF_SECS", 0)
    count, polls = _redials(monkeypatch, la.TAKEN_CLOSE_CODE, lambda: True)
    assert count >= 2 and polls == 0


def _first_dials(monkeypatch, active, secs=1.5):
    """A bridge process starting while the brain reports active() (called per poll).
    Returns (connections, voice-free) after `secs`, nobody speaking."""
    flag = tempfile.mktemp()                     # never created: nobody speaks
    _fake_card(monkeypatch, ARECORD, flag)
    monkeypatch.setattr(la, "START_SETTLE_SECS", 0.5)
    monkeypatch.setattr(la, "START_POLL_SECS", 0.05)
    monkeypatch.setattr(la, "BACKOFF_POLL_SECS", 0.05)
    monkeypatch.setattr(la, "BACKOFF_SECS", 60)
    t0 = []

    async def talk_active():
        t0 or t0.append(asyncio.get_running_loop().time())
        return active(asyncio.get_running_loop().time() - t0[0])
    monkeypatch.setattr(la, "talk_active", talk_active)

    async def run():
        async def on_connect(ws, n):
            try:
                await ws.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        server, url, count = await _talk(on_connect)
        monkeypatch.setattr(la, "URL", url)
        bridge = asyncio.create_task(la.run_bridge())
        await asyncio.sleep(secs)
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        server.close()
        return count[0]

    return asyncio.run(run())


def test_a_bridge_restart_does_not_evict_a_session_in_use(monkeypatch):
    """Started (a crash, a unit restart) while another client's session is live: no
    first dial — it backs off like an evicted bridge."""
    assert _first_dials(monkeypatch, lambda t: True) == 0


def test_our_own_dying_session_does_not_hold_a_restart_off(monkeypatch):
    """The previous process's session, still live for a moment after it died, is
    waited out (START_SETTLE_SECS): the new process dials at once, as at any start."""
    assert _first_dials(monkeypatch, lambda t: t < 0.2) == 1


def test_a_start_with_no_brain_dials_at_once(monkeypatch):
    assert _first_dials(monkeypatch, lambda t: None) == 1


# ------------------------------------------------------ the brain's half of it

def _gateway():
    for k, v in (("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime"),
                 ("LLM_BASE_URL", "http://127.0.0.1:9/v1"), ("LLM_API_KEY", "not-a-real-key")):
        os.environ.setdefault(k, v)
    from teaport_brain import gateway_server
    return gateway_server


def test_an_evicted_talk_socket_is_closed_with_the_taken_code():
    """acquire_slot tells the evicted session before cancelling it, and the gateway
    turns the transport's own close of that socket into the taken code."""
    gs = _gateway()
    from teaport_brain import agent_session
    assert gs.TAKEN_CLOSE_CODE == la.TAKEN_CLOSE_CODE
    closes, order = [], []

    class _Socket:
        async def close(self, code=1000, reason=None):
            closes.append((code, reason))

    class _Task:
        def __init__(self, name):
            self.name = name

        async def cancel(self):
            order.append(f"cancel {self.name}")
            await sock.close()                   # what pipecat does as the pipeline goes

    sock = _Socket()

    async def run():
        _, release_a = await agent_session.acquire_slot(
            _Task("a"), on_evicted=lambda: (order.append("evicted a"), gs._close_as_taken(sock)))
        a_done = agent_session._active_session[1]
        a_done.set()                             # a's teardown finishes at once
        _, release_b = await agent_session.acquire_slot(_Task("b"))
        await release_a()
        assert agent_session.slot_active()       # a's late release leaves b's slot alone
        await release_b()
        assert not agent_session.slot_active()

    asyncio.run(run())
    assert order == ["evicted a", "cancel a"]
    assert closes == [(gs.TAKEN_CLOSE_CODE, gs.TAKEN_CLOSE_REASON)]


def test_talk_status_says_whether_the_slot_is_held(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    gs = _gateway()
    from teaport_brain import agent_session

    def req(token=None, bearer=None):
        return SimpleNamespace(query_params={"token": token} if token else {},
                               headers={"authorization": f"Bearer {bearer}"} if bearer else {})
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(agent_session, "_active_session", None)
    assert asyncio.run(gs.talk_status(req())) == {"active": False}
    monkeypatch.setattr(agent_session, "_active_session", (object(), None, None))
    assert asyncio.run(gs.talk_status(req())) == {"active": True}
    monkeypatch.setenv("GATEWAY_TOKEN", "secret")
    for bad in (req(), req(token="nope"), req(bearer="nope")):
        with pytest.raises(HTTPException) as e:
            asyncio.run(gs.talk_status(bad))
        assert e.value.status_code == 401
    assert asyncio.run(gs.talk_status(req(bearer="secret"))) == {"active": True}
    assert asyncio.run(gs.talk_status(req(token="secret"))) == {"active": True}


def test_uvicorn_request_lines_never_carry_the_token():
    """The access line of a ?token= request and the WebSocket [accepted] line of every
    Talk connect, formatted the way uvicorn formats them."""
    import logging
    from uvicorn.logging import AccessFormatter, DefaultFormatter
    gs = _gateway()
    gs.redact_tokens_in_uvicorn_logs()
    gs.redact_tokens_in_uvicorn_logs()           # idempotent
    lines = []

    class _Keep(logging.Handler):
        def __init__(self, fmt):
            super().__init__()
            self.setFormatter(fmt)

        def emit(self, record):
            lines.append(self.format(record))
    access, error = logging.getLogger("uvicorn.access"), logging.getLogger("uvicorn.error")
    assert sum(type(f).__name__ == "_RedactTokens" for f in access.filters) == 1
    handlers = {access: _Keep(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s',
                                              use_colors=False)),
                error: _Keep(DefaultFormatter("%(message)s", use_colors=False))}
    saved = {log: (log.level, log.propagate) for log in handlers}
    try:
        for log, h in handlers.items():
            log.addHandler(h)
            log.setLevel(logging.INFO)
            log.propagate = False
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET",
                    "/talk/status?token=s3cr%2Bet&x=1", "1.1", 200)
        error.info('%s - "WebSocket %s" [accepted]', "127.0.0.1:6",
                   "/talk?features=volume,restart,local&token=s3cr%2Bet")
    finally:
        for log, h in handlers.items():
            log.removeHandler(h)
            log.setLevel(saved[log][0])
            log.propagate = saved[log][1]
    assert lines == ['127.0.0.1:5 - "GET /talk/status?token=<redacted>&x=1 HTTP/1.1" 200 OK',
                     '127.0.0.1:6 - "WebSocket /talk?features=volume,restart,local&token=<redacted>" [accepted]'], lines


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


# ------------------------------------------------- client tools (tools.py's contract)

def test_volume_steps_levels_and_persists(tmp_path):
    path = str(tmp_path / "state" / "volume.json")
    v = la.Volume(path)
    assert v.percent == 100 and v.gain == 1.0                    # first run: full volume
    assert v.apply({"direction": "louder"}) == {"ok": True, "volume": 100,
                                                 "note": "already at the top"}
    assert v.apply({"direction": "quieter"})["volume"] == 90
    assert v.apply({"level": 35})["volume"] == 35
    assert v.apply({"level": 250})["volume"] == 100              # clamped
    assert v.apply({"level": 0})["volume"] == 0 and v.gain == 0.0
    assert v.apply({"level": "loud"})["ok"] is False
    assert v.apply({})["ok"] is False
    v.apply({"level": 50})
    again = la.Volume(path)                                      # a new session / reboot
    assert again.percent == 50
    assert abs(20 * np.log10(again.gain) + la.VOLUME_RANGE_DB / 2) < 1e-6


def test_volume_scales_the_card_not_the_mouth():
    clock, sunk = _Clock(), []
    play = la.Playback(sunk.append, clock, gain=lambda: 0.1)
    play.push(_tone(24000, 0.2).tobytes(), voiced=True)
    play.pump()
    out = np.frombuffer(b"".join(sunk), dtype="<i2")
    assert 0 < np.abs(out).max() <= 0.1 * 8000 * 1.05           # 20 dB down at the card
    assert play.mouth_due(clock.t) > 0.5                         # the mouth still opens fully


def test_volume_survives_bad_values(tmp_path):
    """A hand-edited state file or an odd tool argument is never an exception (one in
    Volume() would crash-loop the unit; one in apply would end the session)."""
    path = str(tmp_path / "volume.json")
    for saved, percent in (('{"volume": Infinity}', 100), ('{"volume": NaN}', 100),
                           ('[1]', 100), ('"x"', 100), ('{"volume": true}', 100),
                           ('{"volume": 1e3}', 100), ('{"volume": "40"}', 40), ('', 100)):
        with open(path, "w") as f:
            f.write(saved)
        assert la.Volume(path).percent == percent, saved
    v = la.Volume(str(tmp_path / "fresh.json"))
    for bad in ({"level": float("inf")}, {"level": float("nan")}, {"level": True},
                {"level": [50]}, {"level": "50%"}, {"direction": "up"}, ["louder"], "louder"):
        assert v.apply(bad)["ok"] is False, bad
    assert v.percent == 100                                      # none of them changed it
    assert v.apply({"level": 55.6})["volume"] == 56              # rounded, not truncated


def test_volume_level_wins_over_direction_and_the_note_says_so(tmp_path):
    v = la.Volume(str(tmp_path / "v.json"))
    v.apply({"level": 50})
    r = v.apply({"level": 50, "direction": "louder"})
    assert r["volume"] == 50 and "direction (louder) was ignored" in r["note"]
    assert "note" not in v.apply({"level": 50})                  # nothing to say
    assert v.apply({"direction": "quieter"}) == {"ok": True, "volume": 40}
    v.apply({"level": 0})
    assert v.apply({"direction": "quieter"})["note"] == "already at the bottom"


def test_volume_is_saved_by_rename(tmp_path, monkeypatch):
    """A failed save leaves the old level on disk (and no temp file), never a truncated
    file that would read back as full volume."""
    path = str(tmp_path / "v.json")
    v = la.Volume(path)
    v.apply({"level": 30})
    assert la.Volume(path).percent == 30 and os.listdir(tmp_path) == ["v.json"]

    def broken(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(la.os, "replace", broken)
    assert v.apply({"level": 70}) == {"ok": True, "volume": 70}  # applied, not saved
    assert la.Volume(path).percent == 30 and os.listdir(tmp_path) == ["v.json"]


def test_client_tools_dispatch():
    class Card:
        volume = la.Volume(os.path.join(tempfile.mkdtemp(), "v.json"))
    quiet = _Quiet()
    restart = la.Restart(quiet, quiet)
    r = la.client_tool(Card, {"name": "set_volume", "args": {"level": 40}}, restart)
    assert r == {"ok": True, "volume": 40} and not restart.armed.is_set()
    r = la.client_tool(Card, {"name": "restart_session", "args": {}}, restart)
    assert r["ok"] and restart.armed.is_set() and "goodbye" in r["note"]
    r = la.client_tool(Card, {"name": "unlock_door"}, restart)
    assert r == {"ok": False, "error": "this device cannot do unlock_door"}


def test_a_bad_client_tool_request_is_an_error_result_never_an_exception():
    class Card:
        class volume:
            @staticmethod
            def apply(args):
                raise RuntimeError("boom")
    quiet = _Quiet()
    restart = la.Restart(quiet, quiet)
    for m in ({"name": "set_volume", "args": "louder"}, {"name": "set_volume", "args": [1]},
              {"name": "set_volume", "args": {"level": 10}}, {"name": None}):
        assert la.client_tool(Card, m, restart)["ok"] is False, m
    assert not restart.armed.is_set()


class _Quiet:
    """Stands in for both the Playback and the FaceTap a Restart watches."""
    speaking = voiced = False
    sounding = False                                             # audio queued or echoing

    def echo_free(self, now):
        return not self.sounding


def _restart_fired(restart, steps):
    """Run restart.wait() against `steps`: (seconds to wait, action) pairs, then report
    after which step (by index) the wait returned, or None."""
    async def run():
        waiter = asyncio.create_task(restart.wait())
        for i, (secs, action) in enumerate(steps):
            action()
            await asyncio.sleep(secs)
            if waiter.done():
                return i
        waiter.cancel()
        return None
    return asyncio.run(run())


def test_restart_waits_for_the_goodbye_not_a_line_spoken_before_the_call(monkeypatch):
    """The model said a line, then called restart_session; its goodbye (the reply to the
    tool result) comes after a gap. Ending in that gap would cut the goodbye off."""
    monkeypatch.setattr(la, "RESTART_POLL_SECS", 0.01)
    play = _Quiet()
    restart = la.Restart(play, play)
    restart.caption("u1")                                        # the line before the call

    def line_playing():
        play.speaking = play.sounding = True
        restart.arm()                                            # the tool runs meanwhile

    def line_done():
        play.speaking = play.sounding = False                    # the gap

    def goodbye():
        restart.caption("u2")
        play.speaking = play.sounding = True

    def goodbye_done():
        play.speaking = play.sounding = False

    fired = _restart_fired(restart, [(0.1, line_playing), (0.3, line_done),
                                     (0.1, goodbye), (0.1, goodbye_done)])
    assert fired == 3, f"restart fired after step {fired}, not after the goodbye"


def test_restart_without_a_goodbye_waits_then_goes(monkeypatch):
    monkeypatch.setattr(la, "RESTART_POLL_SECS", 0.01)
    monkeypatch.setattr(la, "RESTART_SPEECH_WAIT_SECS", 0.3)
    play = _Quiet()
    restart = la.Restart(play, play)
    fired = _restart_fired(restart, [(0.1, restart.arm), (0.1, lambda: None),
                                     (0.3, lambda: None)])
    assert fired == 2                                            # after the wait, not before


def test_restart_session_redials_at_once_without_a_voice(monkeypatch):
    """restart_session: the bridge answers the tool, lets the goodbye play (none here,
    so it waits RESTART_SPEECH_WAIT_SECS), ends the session and dials a fresh one
    straight away — no voice needed, unlike a session the brain ended."""
    flag = tempfile.mktemp()                       # never created: nobody speaks
    _fake_card(monkeypatch, ARECORD, flag)
    monkeypatch.setattr(la, "RESTART_SPEECH_WAIT_SECS", 0.3)
    got = {}

    async def run():
        async def on_connect(ws, n):
            got.setdefault("paths", []).append(ws.request.path)
            if n == 1:
                await ws.send(json.dumps({"type": "client_tool", "call_id": "teaport-client-x",
                                          "name": "restart_session", "args": {}}))
                while True:                        # skip mic audio to the tool_result
                    msg = await asyncio.wait_for(ws.recv(), 2)
                    if isinstance(msg, str):
                        got["result"] = json.loads(msg)
                        break
            try:
                await ws.wait_closed()             # the bridge hangs up, not us
            except Exception:  # noqa: BLE001
                pass

        server, url, count = await _talk(on_connect)
        monkeypatch.setattr(la, "URL", url)
        bridge = asyncio.create_task(la.run_bridge())
        for _ in range(80):
            await asyncio.sleep(0.05)
            if count[0] == 2:
                break
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        server.close()
        return count[0]

    assert asyncio.run(run()) == 2
    assert got["result"]["type"] == "tool_result" and got["result"]["call_id"] == "teaport-client-x"
    assert got["result"]["result"]["ok"] is True
    assert all("features=volume,restart,local" in p for p in got["paths"]), got["paths"]


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
    monkeypatch.setattr(la, "CARD_ABSENT_MIN_SECS", 0.1)

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


def test_a_flapping_card_keeps_its_backoff(monkeypatch, tmp_path):
    """A card that drops off and comes straight back (brownout, bad cable) is not a
    replug: it waits out the backoff, which keeps growing, instead of being retried --
    and the brain redialled between failures -- every few seconds."""
    card_dir = tmp_path / "Array"
    monkeypatch.setattr(la, "pcm_status_dir", lambda dev: str(card_dir / "pcm0p"))
    monkeypatch.setattr(la, "CARD_POLL_SECS", 0.02)
    monkeypatch.setattr(la, "CARD_ABSENT_MIN_SECS", 0.3)

    async def run():
        async def blink():
            await asyncio.sleep(0.05)
            card_dir.mkdir()                     # back after 0.05 s: a flap
        asyncio.get_running_loop().create_task(blink())
        t0 = asyncio.get_running_loop().time()
        early = await la.wait_for_card("hw:CARD=Array,DEV=0", 0.6)
        return early, asyncio.get_running_loop().time() - t0

    early, took = asyncio.run(run())
    assert not early and took >= 0.59


def test_card_present_from_real_device_strings(tmp_path, monkeypatch):
    """The /proc path from device strings as written, not a stub."""
    (tmp_path / "card0").mkdir()
    (tmp_path / "Array").mkdir()
    real = la.pcm_status_dir
    monkeypatch.setattr(la, "pcm_status_dir",
                        lambda dev: (real(dev) or "").replace("/proc/asound", str(tmp_path)) or None)
    assert la.card_present("hw:0,0") is True
    assert la.card_present("hw:CARD=Array,DEV=0") is True
    assert la.card_present("hw:CARD=Gone,DEV=0") is False
    assert la.card_present("plughw:Array") is None


# ------------------------------------------------------------------- wake words

def test_wake_phrases_are_parsed_as_the_spotter_spells_them():
    assert la.wake_phrases("") == []
    assert la.wake_phrases(" , ;\n") == []
    assert la.wake_phrases("hey Teaport, teaport;computer\nHey  TEAPORT!, o'brien") == \
        ["HEY TEAPORT", "TEAPORT", "COMPUTER", "O'BRIEN"]
    assert la.wake_phrases("R2-D2") == ["R D"]   # digits are not letters; the variants drop it


def test_keyword_variants_add_the_split_compound_and_drop_unknown_tokens():
    # A stand-in for the model's sentencepiece: these pieces, as gigaspeech's BPE cuts them.
    pieces = {"HEY": ["▁HE", "Y"], "TEAPORT": ["▁TEA", "PORT"], "TEA": ["▁TEA"],
              "PORT": ["▁", "PORT"], "COMPUTER": ["▁COMP", "U", "TER"], "COMP": ["▁COMP"],
              "UTER": ["▁", "U", "TER"], "COMPU": ["▁COMP", "U"], "TER": ["▁", "TER"],
              "X": ["▁X"]}

    def encode(text):
        return [p for w in text.split() for p in pieces[w]]
    vocab = {"▁HE", "Y", "▁TEA", "▁", "PORT", "▁COMP", "U", "TER"}
    assert la.keyword_variants("HEY TEAPORT", encode, vocab) == [
        ["▁HE", "Y", "▁TEA", "PORT"],            # as spelled
        ["▁HE", "Y", "▁TEA", "▁", "PORT"]]       # "TEA PORT"; HEY's halves are too short
    assert la.keyword_variants("COMPUTER", encode, vocab) == [
        ["▁COMP", "U", "TER"], ["▁COMP", "▁", "U", "TER"], ["▁COMP", "U", "▁", "TER"]]
    assert la.keyword_variants("X", encode, vocab) == []   # a token the model lacks


def test_a_spotter_without_its_model_says_why():
    """No model (or no sherpa-onnx, on a box without the `wake` extra): a WakeError
    naming the fix, never a crash (sherpa-onnx exits the process on some bad input)."""
    with pytest.raises(la.WakeError, match="install.sh"):
        la.WakeSpotter(["HEY TEAPORT"], model_dir=tempfile.mkdtemp())


def test_keep_alive_counts_from_the_last_speech_and_while_the_bot_is_heard():
    clock = _Clock()
    alive = la.KeepAlive(30, clock=clock)
    clock.t += 29
    assert not alive.expired()
    alive.activity()                            # a transcript, a caption, bot audio
    clock.t += 29
    assert not alive.expired()
    clock.t += 1
    assert alive.expired()
    assert not alive.expired(busy=True)         # still playing (a long reply, the thinking sound)
    clock.t += 29
    assert not alive.expired()
    clock.t += 1
    assert alive.expired()


class _FakeSpotter:
    """Hears its phrase once each time the test creates the `wake` file, and counts
    what it was fed (16 kHz mono, as from the card)."""

    def __init__(self, path):
        self.path, self.phrases, self.fed = path, ["HEY TEAPORT"], 0

    def feed(self, pcm):
        assert pcm.dtype == np.int16 and pcm.ndim == 1
        self.fed += len(pcm)
        if os.path.exists(self.path):
            os.unlink(self.path)
            return "HEY TEAPORT"
        return None


def _wake_mode(monkeypatch, capture_script=ARECORD):
    """A fake card with wake words on and a _FakeSpotter. Returns (loud flag, wake file,
    spotters loaded)."""
    flag, wake = tempfile.mktemp(), tempfile.mktemp()
    _fake_card(monkeypatch, capture_script, flag)
    monkeypatch.setattr(la, "WAKE_WORDS", "hey teaport")
    monkeypatch.setattr(la, "KEEPALIVE_POLL_SECS", 0.05)
    loaded = []

    def load(phrases):
        assert phrases == ["HEY TEAPORT"]
        loaded.append(_FakeSpotter(wake))
        return loaded[-1]
    monkeypatch.setattr(la, "load_spotter", load)
    return flag, wake, loaded


async def _until(cond, secs=2.0):
    for _ in range(int(secs / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


def test_wake_words_dial_on_a_wake_word_never_on_a_voice_or_at_start(monkeypatch):
    flag, wake, loaded = _wake_mode(monkeypatch)
    got, face = {}, []
    monkeypatch.setattr(la.FaceTap, "_sendto", lambda self, data: face.append(json.loads(data)))

    async def run():
        async def on_connect(ws, n):
            first = await asyncio.wait_for(ws.recv(), 2)
            total, t0 = len(first), asyncio.get_running_loop().time()
            while asyncio.get_running_loop().time() - t0 < 0.05:   # the pre-roll, at once
                total += len(await asyncio.wait_for(ws.recv(), 1))
            got["preroll"] = total
            await ws.close()                     # the brain hung up

        server, url, count = await _talk(on_connect)
        monkeypatch.setattr(la, "URL", url)
        bridge = asyncio.create_task(la.run_bridge())
        open(flag, "w").close()                  # the room talks, from the start...
        await asyncio.sleep(2.5)
        assert count[0] == 0                     # ...no dial at start, none on a voice
        assert loaded and loaded[0].fed > 16000  # the spotter heard it all
        assert {"state": "listening"} not in face
        open(wake, "w").close()                  # "hey teaport"
        assert await _until(lambda: count[0] == 1)
        assert {"state": "listening"} in face    # the face shows it heard
        await asyncio.sleep(1.0)
        assert count[0] == 1                     # hung up: no redial on the voice still there
        open(wake, "w").close()
        assert await _until(lambda: count[0] == 2)
        os.unlink(flag)
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        server.close()

    asyncio.run(run())
    # The wake word and what followed it: the last WAKE_PREROLL_SECS (24 kHz mono = 48 bytes/ms).
    assert got["preroll"] >= 48 * la.WAKE_PREROLL_SECS * 1000 * 0.9, got


def test_keep_alive_goes_deaf_then_a_wake_word_or_the_bot_turns_the_mic_back_on(monkeypatch):
    """After a wake the mic stays on until nobody has spoken for KEEPALIVE_SECS; then the
    session is KEPT (no redial, no greeting) but hears silence, until a wake word -- or
    the bot speaking into it (a late follow-up) -- turns the mic back on."""
    flag, wake, loaded = _wake_mode(monkeypatch)
    monkeypatch.setattr(la, "KEEPALIVE_SECS", 0.6)
    chunks, bot = [], asyncio.Queue()

    async def run():
        async def on_connect(ws, n):
            async def say():
                while True:
                    await ws.send(await bot.get())
            sayer = asyncio.create_task(say())
            try:
                async for msg in ws:
                    if isinstance(msg, bytes):
                        chunks.append((asyncio.get_running_loop().time(), any(msg)))
            finally:
                sayer.cancel()

        def heard(since):                        # (any audio, any silence) since `since`
            got = [loud for t, loud in chunks if t >= since]
            return any(got), not all(got)

        loop = asyncio.get_running_loop()
        server, url, count = await _talk(on_connect)
        monkeypatch.setattr(la, "URL", url)
        bridge = asyncio.create_task(la.run_bridge())
        open(flag, "w").close()                  # someone keeps talking near the box
        await asyncio.sleep(0.5)
        open(wake, "w").close()
        assert await _until(lambda: count[0] == 1)
        t = loop.time()
        await asyncio.sleep(0.4)
        assert heard(t) == (True, False)         # live: the mic goes through
        await asyncio.sleep(0.8)                 # no transcript, no bot: keep-alive runs out
        t = loop.time()
        await asyncio.sleep(0.4)
        assert heard(t) == (False, True)         # deaf: silence only, the session kept

        open(wake, "w").close()                  # a wake word in the deaf session
        assert await _until(lambda: heard(loop.time() - 0.1)[0])
        t = loop.time()
        await asyncio.sleep(0.3)
        assert heard(t) == (True, False)
        assert await _until(lambda: heard(loop.time() - 0.1) == (False, True), 3)  # deaf again

        await bot.put(_tone(24000, 0.2).tobytes())   # the bot speaks
        assert await _until(lambda: heard(loop.time() - 0.1)[0])
        assert count[0] == 1                     # all of it one session
        os.unlink(flag)
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        server.close()

    asyncio.run(run())


def test_speech_in_the_conversation_keeps_the_mic_on(monkeypatch):
    flag, wake, _ = _wake_mode(monkeypatch)
    monkeypatch.setattr(la, "KEEPALIVE_SECS", 0.5)
    chunks = []

    async def run():
        async def on_connect(ws, n):
            async def talk():                    # the user, transcribed, every 0.2 s
                while True:
                    await asyncio.sleep(0.2)
                    await ws.send(json.dumps({"type": "transcript", "role": "user",
                                              "text": "and then", "final": False}))
            talker = asyncio.create_task(talk())
            try:
                async for msg in ws:
                    if isinstance(msg, bytes):
                        chunks.append(any(msg))
            finally:
                talker.cancel()

        server, url, count = await _talk(on_connect)
        monkeypatch.setattr(la, "URL", url)
        bridge = asyncio.create_task(la.run_bridge())
        open(flag, "w").close()
        await asyncio.sleep(0.3)
        open(wake, "w").close()
        assert await _until(lambda: count[0] == 1)
        await asyncio.sleep(1.5)                 # 3x the keep-alive, talking all along
        os.unlink(flag)
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        server.close()

    asyncio.run(run())
    assert chunks and all(chunks[: len(chunks) - 5])    # never muted (the tail: the flag went)


def test_no_wake_words_never_loads_the_spotter(monkeypatch):
    """LOCAL_AUDIO_WAKE_WORDS empty: today's bridge, and sherpa-onnx is never imported."""
    flag = tempfile.mktemp()
    _fake_card(monkeypatch, ARECORD, flag)
    monkeypatch.setattr(la, "WAKE_WORDS", " , ")
    loads = []
    monkeypatch.setattr(la, "load_spotter", lambda phrases: loads.append(phrases))

    async def run():
        async def on_connect(ws, n):
            await ws.close()
        server, url, count = await _talk(on_connect)
        monkeypatch.setattr(la, "URL", url)
        bridge = asyncio.create_task(la.run_bridge())
        assert await _until(lambda: count[0] == 1)   # dialled at start, as before
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        server.close()

    asyncio.run(run())
    assert loads == []
    assert "sherpa_onnx" not in sys.modules and "sentencepiece" not in sys.modules


def test_wake_words_that_cannot_be_spotted_never_fall_back_to_listening(monkeypatch):
    """Wake words were asked for so the room is NOT heard: a spotter that cannot load
    leaves the bridge quiet (no dial, no voice wake, no crash loop), saying why."""
    flag = tempfile.mktemp()
    _fake_card(monkeypatch, ARECORD, flag)
    monkeypatch.setattr(la, "WAKE_WORDS", "hey teaport")

    def broken(phrases):
        raise la.WakeError("no keyword model at /nowhere")
    monkeypatch.setattr(la, "load_spotter", broken)

    async def run():
        async def on_connect(ws, n):
            await ws.close()
        server, url, count = await _talk(on_connect)
        monkeypatch.setattr(la, "URL", url)
        bridge = asyncio.create_task(la.run_bridge())
        open(flag, "w").close()
        await asyncio.sleep(1.5)
        os.unlink(flag)
        alive = not bridge.done()
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        server.close()
        return count[0], alive

    assert asyncio.run(run()) == (0, True)


def test_wake_words_while_taken_wait_out_the_backoff(monkeypatch):
    """Another client took the box: its user saying the wake word to their own session
    must not take it back while that session is live."""
    flag, wake, _ = _wake_mode(monkeypatch)
    monkeypatch.setattr(la, "BACKOFF_SECS", 60)
    monkeypatch.setattr(la, "BACKOFF_POLL_SECS", 0.05)
    monkeypatch.setattr(la, "START_SETTLE_SECS", 0.1)
    monkeypatch.setattr(la, "START_POLL_SECS", 0.05)

    async def live():
        return True
    monkeypatch.setattr(la, "talk_active", live)

    async def run():
        async def on_connect(ws, n):
            await ws.close()
        server, url, count = await _talk(on_connect)
        monkeypatch.setattr(la, "URL", url)
        bridge = asyncio.create_task(la.run_bridge())
        await asyncio.sleep(0.5)
        open(wake, "w").close()
        await asyncio.sleep(1.0)
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        server.close()
        return count[0]

    assert asyncio.run(run()) == 0


if __name__ == "__main__":
    # test_suite.py runs this file as a script: without this it would pass having run nothing.
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
