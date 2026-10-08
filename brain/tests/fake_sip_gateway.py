# SPDX-License-Identifier: MIT
#
# teaport — a fake teaport-sip gateway: the phone path without a phone line.
#
# Stands in for the C++ gateway (Teaspoon-AI/teaport-sip) on its brain-facing side
# only. It binds the AF_UNIX SOCK_SEQPACKET socket the SIP brain connects to and
# speaks protocol v0 as teaport-sip/docs/PROTOCOL.md and src/gateway.cpp define it;
# it never touches SIP, RTP or a registrar, so a call can be placed at the brain
# while the real line stays registered somewhere else.
#
# What it does the way the gateway does it:
#   * hello first on every connection, with "replay": true (an older gateway's hello,
#     without it, is --no-replay);
#   * an inbound call as call.incoming {call_id, from, to}, then call.state incoming
#     (ringing), connecting (200 OK sent) and confirmed (ACK, media up). With
#     auto_answer off it waits in `incoming` for the brain's call.answer;
#   * media at 50 frames/s on its own clock, both ways: one 640-byte audio_in per
#     20 ms (the caller's WAV, then silence, as RTP keeps coming on a quiet line), and
#     one frame popped per 20 ms from a bounded ~1 s playout queue that drops the
#     oldest, which is what the caller hears and what gets recorded;
#   * call.hangup from the brain hangs up the active call, reported back as a live
#     call.state disconnected; a caller hangup is the same disconnected;
#   * a brain that goes away leaves the call up (the caller hears silence, their
#     audio is lost), and the next brain to connect is replayed it: hello, then
#     call.incoming and call.state with "replay": true, and only then live events
#     and audio. One brain at a time; a second waits in the listen backlog.
#
# Where it is not the gateway: media starts at `confirmed` rather than at the SDP
# answer a few ms earlier (the brain drops audio before `confirmed` anyway); there is
# no echo on the line; and nothing is resampled or jitter-buffered beyond the queue.
#
# It is stdlib-only and independent of teaport_brain on purpose: it is a second,
# separate reading of the protocol, so a wire bug in the brain's serializer cannot be
# mirrored here and pass. It also runs under the system python3 on the box.
#
# Driven two ways:
#
#   from tests (see test_sip_fake_gateway.py):
#       gw = FakeSipGateway(path); await gw.start(); await gw.wait_brain()
#       call = await gw.place_call("+15551234567")
#       await call.play_wav("question.wav"); await call.wait_bot_speech(after=...)
#       await call.hangup()
#   or the whole script at once: await run_call(gw, caller=..., wav=..., hangup=...)
#
#   by hand (cwd brain/):
#       python -m tests.fake_sip_gateway --socket ~/fake-sip/gw.sock \
#           --from +15551234567 --wav test/question.wav --record /tmp/bot.wav
#   then start the SIP brain with --socket pointing at the same path. See
#   docs/CONFIG.md, "Testing the phone path without a line", for the box recipe.
#
from __future__ import annotations

import argparse
import array
import asyncio
import json
import logging
import math
import os
import socket
import sys
import time
import uuid
import wave
from collections import deque

# Wire contract (teaport-sip/src/ipc/protocol.h). Restated, not imported: see above.
MSG_CONTROL = 0x01
MSG_AUDIO_IN = 0x10   # gateway -> brain, caller audio
MSG_AUDIO_OUT = 0x11  # brain -> gateway, playout
PROTO_VERSION = 0
SAMPLE_RATE = 16000
CHANNELS = 1
PTIME_MS = 20
BYTES_PER_FRAME = 640  # 20 ms of S16LE mono at 16 kHz
FRAME_S = PTIME_MS / 1000.0
SILENCE = bytes(BYTES_PER_FRAME)

# The gateway reads with a MAX_FRAME_BYTES (2048) buffer and silently truncates
# anything longer; we read more so an oversize datagram is SEEN and reported.
_RECV_BYTES = 65536
# Bounded playout queue, oldest dropped (PROTOCOL.md: "bounded ~1 s queues").
PLAYOUT_QUEUE_FRAMES = 50
# A played frame louder than this (int16 RMS, about -50 dBFS) is the bot talking.
SPEECH_RMS = 100.0
# Bot speech separated by less than this is one stretch of talk (word gaps, commas).
SPURT_GAP_S = 0.4

# The live gateway's socket (the units' RuntimeDirectory). Binding it needs
# allow_live_path=True, and is refused while anything listens there.
LIVE_SOCKET = "/run/teaport/teaport-sip.sock"

DEFAULT_CALLER = "+15551234567"
DEFAULT_HOST = "fake-sip.invalid"

log = logging.getLogger("fake-sip-gw")


# --- audio helpers ----------------------------------------------------------------

def frame_rms(pcm: bytes) -> float:
    """RMS of S16LE PCM, in int16 units."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    a = array.array("h", pcm[:2 * n])
    if sys.byteorder == "big":
        a.byteswap()
    return math.sqrt(sum(s * s for s in a) / n)


def load_wav(path: str) -> bytes:
    """A WAV as 16 kHz mono S16LE, the gateway's wire format whatever the codec on the
    line: channels are averaged, other rates (8 kHz telephony, 24 kHz TTS, ...) are
    resampled by linear interpolation, which is plenty for speech into an STT."""
    with wave.open(path, "rb") as w:
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path}: {8 * width}-bit samples; only 16-bit PCM WAVs are read")
    a = array.array("h", raw[:len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        a.byteswap()
    if ch > 1:
        a = array.array("h", (int(sum(a[i:i + ch]) / ch) for i in range(0, len(a) - ch + 1, ch)))
    if rate != SAMPLE_RATE and len(a) > 1:
        n_out = int(len(a) * SAMPLE_RATE / rate)
        step = rate / SAMPLE_RATE
        out = array.array("h", bytes(2 * n_out))
        last = len(a) - 1
        for i in range(n_out):
            x = i * step
            j = int(x)
            if j >= last:
                out[i] = a[last]
            else:
                f = x - j
                out[i] = int(a[j] + (a[j + 1] - a[j]) * f)
        a = out
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def write_wav(path: str, pcm: bytes, channels: int = 1) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def interleave(left: bytes, right: bytes) -> bytes:
    """Two mono S16LE tracks as one stereo track (the shorter padded with silence)."""
    n = max(len(left), len(right)) // 2
    l_ = array.array("h", left.ljust(2 * n, b"\0"))
    r_ = array.array("h", right.ljust(2 * n, b"\0"))
    out = array.array("h", bytes(4 * n))
    out[0::2] = l_
    out[1::2] = r_
    return out.tobytes()


def _sip_uri(who: str) -> str:
    """A caller ID as pjsua's remoteUri shows it: <sip:+15551234567@host>. Something
    already shaped like a URI is passed through."""
    if who.startswith(("<", "sip:", "sips:", '"')):
        return who
    return f"<sip:{who}@{DEFAULT_HOST}>"


def _listening_on(path: str) -> bool:
    """Is a LISTENING unix socket bound at `path`? Read from /proc/net/unix rather than
    probed with a connect: a connect to the real gateway would be served as a brain
    (hello, replay) and logged there as a brain that came and went."""
    try:
        with open("/proc/net/unix", encoding="utf-8", errors="replace") as f:
            next(f)
            for line in f:
                parts = line.split()
                # Num RefCount Protocol Flags Type St Inode [Path]; __SO_ACCEPTCON = 0x10000
                if len(parts) >= 8 and parts[7] == path and int(parts[3], 16) & 0x10000:
                    return True
        return False
    except (OSError, StopIteration, ValueError):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            s.connect(path)
            return True
        except OSError:
            return False
        finally:
            s.close()


# --- one call ---------------------------------------------------------------------

class Call:
    """One call at the fake gateway, from the INVITE to `disconnected`.

    Times are call-relative seconds from `confirmed` (the media start), on the media
    clock: tick n is n * 20 ms, so they line up with sample n * 320 of the recordings.
    """

    def __init__(self, gw: "FakeSipGateway", call_id: str, from_uri: str, to_uri: str):
        self.gw = gw
        self.call_id = call_id
        self.from_uri = from_uri
        self.to_uri = to_uri
        self.state = ""                # the last call.state sent: the replay record
        self.answered = asyncio.Event()  # call.answer seen (auto_answer=False)
        self.confirmed = asyncio.Event()
        self.ended = asyncio.Event()
        self.ended_by: str | None = None  # "caller" | "brain" | "gateway"
        self.ticks = 0                 # 20 ms media ticks since confirmed
        self.caller_track = bytearray()  # what went to the brain, tick by tick
        self.bot_track = bytearray()     # what the caller heard, tick by tick
        self.audio_out_frames = 0      # audio_out datagrams taken for this call
        self.playout_dropped = 0       # dropped by the bounded queue (brain ran ahead)
        self.bot_onsets: list[float] = []  # starts of each stretch of bot talk
        self.last_bot_speech: float | None = None  # last tick the bot was audible
        self.wav_end: float | None = None  # when the last queued caller audio went out
        self._caller = bytearray()
        self._caller_done = asyncio.Event()
        self._caller_done.set()
        self._playout: deque = deque()
        self._media_task: asyncio.Task | None = None

    # -- clock ---------------------------------------------------------------------

    @property
    def t(self) -> float:
        """Call time now (media clock), 0 until confirmed."""
        return self.ticks * FRAME_S

    @property
    def active(self) -> bool:
        return self.confirmed.is_set() and not self.ended.is_set()

    @property
    def bot_speech_s(self) -> float:
        return sum(1 for i in range(0, len(self.bot_track), BYTES_PER_FRAME)
                   if frame_rms(self.bot_track[i:i + BYTES_PER_FRAME]) > SPEECH_RMS) * FRAME_S

    def bot_speaking(self) -> bool:
        return (self.last_bot_speech is not None
                and self.t - self.last_bot_speech < SPURT_GAP_S)

    # -- media ---------------------------------------------------------------------

    def _start_media(self):
        self._media_task = asyncio.ensure_future(self._media())

    async def _stop_media(self):
        t, self._media_task = self._media_task, None
        if t is not None and t is not asyncio.current_task():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._caller.clear()
        self._caller_done.set()

    async def _media(self):
        """The gateway's media clock: every 20 ms one caller frame out, one playout
        frame in, whether or not a brain is there to take or give them."""
        next_t = time.monotonic()
        while not self.ended.is_set():
            if self._caller:
                frame = bytes(self._caller[:BYTES_PER_FRAME]).ljust(BYTES_PER_FRAME, b"\0")
                del self._caller[:BYTES_PER_FRAME]
                if not self._caller:
                    self.wav_end = self.t + FRAME_S
                    self._caller_done.set()
            else:
                frame = SILENCE
            self.caller_track += frame
            self.gw._send_audio_in(frame)
            out = self._playout.popleft() if self._playout else SILENCE
            self.bot_track += out
            if frame_rms(out) > SPEECH_RMS:
                if self.last_bot_speech is None or self.t - self.last_bot_speech >= SPURT_GAP_S:
                    self.bot_onsets.append(self.t)
                    self.gw._event(f"call {self.call_id}: bot speech starts at t={self.t:.2f}s")
                self.last_bot_speech = self.t
            self.ticks += 1
            next_t += FRAME_S
            delay = next_t - time.monotonic()
            if delay < -0.2:
                # Fell behind (a loaded box): re-anchor rather than burst to catch up.
                next_t, delay = time.monotonic(), 0.0
            await asyncio.sleep(max(0.0, delay))

    def _playout_push(self, pcm: bytes):
        self.audio_out_frames += 1
        self._playout.append(pcm)
        while len(self._playout) > PLAYOUT_QUEUE_FRAMES:
            self._playout.popleft()
            self.playout_dropped += 1

    def _flush_playout(self) -> int:
        n = len(self._playout)
        self._playout.clear()
        return n

    # -- what a test or the CLI drives ------------------------------------------------

    def say(self, pcm: bytes):
        """Queue caller audio (16 kHz mono S16LE); it goes out at real time."""
        if not pcm:
            return
        self._caller += pcm
        self._caller_done.clear()

    async def play_wav(self, path: str):
        """Say a WAV and return once its last frame has gone out."""
        self.say(load_wav(path))
        await self.wait_said()

    async def wait_said(self):
        await self._caller_done.wait()

    async def hangup(self, by: str = "caller"):
        """The caller hangs up (BYE): a live call.state disconnected."""
        await self.gw._hangup(self, by)

    async def wait_bot_speech(self, after: float = 0.0, timeout: float = 20.0) -> float | None:
        """The call time of the first stretch of bot talk starting at or after `after`,
        or None if none starts within `timeout` s (or the call ends)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.ended.is_set():
            for t in self.bot_onsets:
                if t >= after:
                    return t
            await asyncio.sleep(FRAME_S)
        return next((t for t in self.bot_onsets if t >= after), None)

    async def wait_bot_quiet(self, quiet_s: float = 1.0, timeout: float = 30.0) -> float | None:
        """Once the bot has said something: the call time it went quiet for `quiet_s`.
        None on timeout or if the call ends first."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.ended.is_set():
            if self.last_bot_speech is not None and self.t - self.last_bot_speech >= quiet_s:
                return self.last_bot_speech + FRAME_S
            await asyncio.sleep(FRAME_S)
        return None

    def save(self, bot_path: str | None = None, stereo_path: str | None = None):
        """Write what the caller heard (mono), and/or caller+bot (left, right)."""
        if bot_path:
            write_wav(bot_path, bytes(self.bot_track))
        if stereo_path:
            write_wav(stereo_path, interleave(bytes(self.caller_track), bytes(self.bot_track)), 2)


# --- the gateway ------------------------------------------------------------------

class FakeSipGateway:
    """The gateway's brain-facing side: a SEQPACKET listener and protocol v0.

    `auto_answer` and `replay` are the gateway's own switches (teaport-sip.conf
    auto_answer=, and a gateway from before teaport-sip#3 for replay=False).
    Everything that happens is kept for assertions: `controls` (brain -> gateway
    control messages), `sent` (gateway -> brain control messages, with the
    connection number), `violations` (anything the real gateway would mis-handle)."""

    def __init__(self, path: str, *, auto_answer: bool = True, replay: bool = True,
                 allow_live_path: bool = False):
        self.path = path
        self.auto_answer = auto_answer
        self.replay = replay
        self.allow_live_path = allow_live_path
        self.call: Call | None = None   # the active call (protocol v0: one)
        self.calls: list[Call] = []
        self.controls: list[tuple[float, dict]] = []
        self.sent: list[tuple[int, dict]] = []
        self.violations: list[str] = []
        self.events: list[tuple[float, str]] = []
        self.connects = 0
        self.brain_connected = asyncio.Event()
        self.brain_gone = asyncio.Event()
        self.stray_audio_out = 0          # audio_out with no call up
        self.audio_in_dropped = 0         # caller frames the socket would not take
        self._t0 = time.monotonic()
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None   # set once the handshake is done
        self._serve_task: asyncio.Task | None = None
        # Held across the replay and every live state change, so a state change is
        # either in the replay or sent live after it, never both and never neither
        # (the gateway's ctlMtx_).
        self._ctl_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------------------

    async def start(self):
        path = os.path.abspath(self.path)
        if len(path.encode()) > 107:  # sun_path is 108 bytes with the NUL
            raise RuntimeError(f"socket path too long for AF_UNIX ({len(path)} > 107): {path}")
        if path == LIVE_SOCKET and not self.allow_live_path:
            raise RuntimeError(
                f"{LIVE_SOCKET} is the live gateway's socket; pass allow_live_path "
                "(--allow-live-path) only with teaport-sip stopped")
        if os.path.exists(path):
            if _listening_on(path):
                raise RuntimeError(f"something is already listening on {path} (a real "
                                   "gateway?) - refusing to take its socket over")
            os.unlink(path)  # stale
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        s.bind(path)
        s.listen(4)
        s.setblocking(False)
        self._listener = s
        self._event(f"listening on {path} (auto_answer={self.auto_answer}, "
                    f"replay={self.replay})")
        self._serve_task = asyncio.ensure_future(self._serve())

    async def close(self):
        """Shut down as the gateway does: hang up a live call (the brain hears a
        disconnected), then close the socket."""
        if self.call is not None and not self.call.ended.is_set():
            await self._hangup(self.call, "gateway")
        if self._serve_task is not None:
            self._serve_task.cancel()
            try:
                await self._serve_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for s in (self._conn, self._listener):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._conn = self._listener = None
        try:
            os.unlink(self.path)
        except OSError:
            pass

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def wait_brain(self, timeout: float = 30.0) -> bool:
        """Until a brain is connected and handshaken; False on timeout."""
        try:
            await asyncio.wait_for(self.brain_connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -- calls -----------------------------------------------------------------------

    async def place_call(self, caller: str = DEFAULT_CALLER, *, to: str = "teaport",
                         call_id: str | None = None, ring_s: float = 0.2,
                         answer_timeout: float = 30.0) -> Call:
        """An inbound call: call.incoming, `incoming` (ringing), then on answer
        `connecting` and `confirmed`. Returns once confirmed and media is running.
        With auto_answer off, a brain that never sends call.answer leaves the caller
        to give up after `answer_timeout`: disconnected, and TimeoutError."""
        if self.call is not None and not self.call.ended.is_set():
            raise RuntimeError("protocol v0 carries one call at a time; hang up first")
        c = Call(self, call_id or f"{uuid.uuid4().hex[:16]}@{DEFAULT_HOST}",
                 _sip_uri(caller), _sip_uri(to))
        self.call = c
        self.calls.append(c)
        self._event(f"INVITE from {c.from_uri} (call {c.call_id})")
        async with self._ctl_lock:
            await self._send_live({"type": "call.incoming", "call_id": c.call_id,
                                   "from": c.from_uri, "to": c.to_uri})
        await self._set_state(c, "incoming")
        if self.auto_answer:
            await asyncio.sleep(ring_s)
        else:
            try:
                await asyncio.wait_for(c.answered.wait(), answer_timeout)
            except asyncio.TimeoutError:
                await self._hangup(c, "caller")
                raise TimeoutError(f"no call.answer within {answer_timeout:g}s")
        if c.ended.is_set():
            return c
        await self._set_state(c, "connecting")
        await self._set_state(c, "confirmed")
        c.confirmed.set()
        c._start_media()
        return c

    async def _set_state(self, c: Call, state: str):
        async with self._ctl_lock:
            if c.ended.is_set():
                return
            c.state = state
            await self._send_live({"type": "call.state", "call_id": c.call_id,
                                   "state": state})

    async def _hangup(self, c: Call, by: str):
        async with self._ctl_lock:
            if c.ended.is_set():
                return
            c.state = "disconnected"
            c.ended_by = by
            c.ended.set()   # no longer replayable
            await self._send_live({"type": "call.state", "call_id": c.call_id,
                                   "state": "disconnected"})
        await c._stop_media()
        self._event(f"call {c.call_id} hung up by the {by} at t={c.t:.2f}s")

    # -- the socket ------------------------------------------------------------------

    async def _serve(self):
        loop = asyncio.get_running_loop()
        while True:
            conn, _ = await loop.sock_accept(self._listener)
            conn.setblocking(False)
            self.connects += 1
            n = self.connects
            ok = await self._handshake(conn, n)
            if ok:
                try:
                    await self._receive(conn)
                except asyncio.CancelledError:
                    conn.close()
                    raise
            async with self._ctl_lock:
                self._conn = None
            self.brain_connected.clear()
            self.brain_gone.set()
            c = self.call
            if c is not None and not c.ended.is_set():
                self._event(f"brain {n} gone mid-call ({c.call_id}, state={c.state}); the "
                            f"call stays up and is replayed to the next brain; flushed "
                            f"{c._flush_playout()} playout frames")
            else:
                self._event(f"brain {n} gone")
            try:
                conn.close()
            except OSError:
                pass

    async def _handshake(self, conn: socket.socket, n: int) -> bool:
        """hello, then the replay of a call in progress, then open the stream."""
        async with self._ctl_lock:
            c = self.call
            if c is not None:
                c._flush_playout()   # start the new brain clean
            hello = {"type": "hello", "proto": PROTO_VERSION, "role": "gateway",
                     "codec": "s16le", "rate": SAMPLE_RATE, "channels": CHANNELS,
                     "ptime_ms": PTIME_MS}
            frames = [hello]
            if self.replay:
                hello["replay"] = True
                if c is not None and not c.ended.is_set():
                    frames.append({"type": "call.incoming", "call_id": c.call_id,
                                   "from": c.from_uri, "to": c.to_uri, "replay": True})
                    if c.state:
                        frames.append({"type": "call.state", "call_id": c.call_id,
                                       "state": c.state, "replay": True})
            try:
                for m in frames:
                    await self._send_control(conn, m, n)
            except OSError as e:
                self._event(f"brain {n}: handshake failed ({e!r}); dropping it")
                return False
            self._conn = conn
        what = ("hello, no call to replay" if len(frames) == 1 else
                f"hello + replay of call {c.call_id} (state={c.state or 'none sent yet'})")
        self._event(f"brain {n} connected: {what}")
        self.brain_gone.clear()
        self.brain_connected.set()
        return True

    async def _receive(self, conn: socket.socket):
        loop = asyncio.get_running_loop()
        while True:
            try:
                data = await loop.sock_recv(conn, _RECV_BYTES)
            except (ConnectionError, OSError):
                return
            if not data:
                return
            tag, payload = data[0], data[1:]
            if tag == MSG_AUDIO_OUT:
                self._on_audio_out(payload)
            elif tag == MSG_CONTROL:
                await self._on_control(payload)
            else:
                self._violation(f"datagram with unknown tag 0x{tag:02x} ({len(payload)} B)")

    def _on_audio_out(self, pcm: bytes):
        if len(pcm) != BYTES_PER_FRAME:
            # The gateway plays each datagram as one 20 ms frame, and truncates past
            # MAX_FRAME_BYTES: anything else is lost or mistimed on a real line.
            self._violation(f"audio_out of {len(pcm)} B (want {BYTES_PER_FRAME})")
            pcm = pcm[:BYTES_PER_FRAME].ljust(BYTES_PER_FRAME, b"\0")
        c = self.call
        if c is None or not c.active:
            self.stray_audio_out += 1
            return
        c._playout_push(pcm)

    async def _on_control(self, payload: bytes):
        try:
            msg = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._violation(f"unparseable control ({len(payload)} B)")
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            self._violation(f"control with no type: {msg!r}")
            return
        c = self.call
        t = c.t if c is not None else 0.0
        self.controls.append((t, msg))
        self._event(f"<- brain {json.dumps(msg, ensure_ascii=False)}")
        mtype = msg["type"]
        if mtype == "hello":
            pass  # the brain's ack; nothing required
        elif mtype == "call.answer":
            if c is not None and not c.ended.is_set() and c.state == "incoming":
                c.answered.set()
        elif mtype == "call.hangup":
            if c is not None and not c.ended.is_set():
                await self._hangup(c, "brain")
        else:
            self._event(f"unhandled control type: {mtype}")

    async def _send_control(self, conn: socket.socket, msg: dict, n: int):
        data = bytes([MSG_CONTROL]) + json.dumps(msg, ensure_ascii=False).encode("utf-8")
        async with self._send_lock:
            await asyncio.get_running_loop().sock_sendall(conn, data)
        self.sent.append((n, msg))
        self._event(f"-> brain {json.dumps(msg, ensure_ascii=False)}")

    async def _send_live(self, msg: dict):
        """A live control event, under _ctl_lock: sent to the brain if one is
        connected, otherwise dropped, as the gateway's sendControl drops it."""
        conn = self._conn
        if conn is None:
            self._event(f"(no brain) {json.dumps(msg, ensure_ascii=False)}")
            return
        try:
            await self._send_control(conn, msg, self.connects)
        except OSError as e:
            self._event(f"send failed ({e!r}): {msg}")

    def _send_audio_in(self, frame: bytes):
        conn = self._conn
        if conn is None:
            return
        try:
            conn.send(bytes([MSG_AUDIO_IN]) + frame)
        except BlockingIOError:
            self.audio_in_dropped += 1   # a stalled brain: drop, never block the clock
        except OSError:
            pass  # gone; the receive loop sees the EOF

    # -- bookkeeping -----------------------------------------------------------------

    def _event(self, text: str):
        t = time.monotonic() - self._t0
        self.events.append((t, text))
        log.info(f"+{t:7.3f}s {text}")

    def _violation(self, text: str):
        self.violations.append(text)
        log.warning(f"PROTOCOL: {text}")


# --- a scripted call --------------------------------------------------------------

async def run_call(gw: FakeSipGateway, *, caller: str = DEFAULT_CALLER,
                   wav: str | None = None, ring_s: float = 0.2,
                   wait_greeting: bool = True, greet_timeout: float = 25.0,
                   quiet_s: float = 1.2, hangup: str = "done",
                   mid_reply_s: float = 1.0, hangup_after: float | None = None,
                   reply_timeout: float = 45.0, record: str | None = None,
                   record_stereo: str | None = None) -> dict:
    """Place one call and script it. Returns a summary dict (call times in s).

    1. Ring and answer. 2. With a `wav`: if `wait_greeting`, wait for the bot to
    speak and go quiet (`quiet_s`), then say the WAV. 3. Hang up per `hangup`:
         "done"      once the bot has answered and gone quiet for `quiet_s`
         "mid-reply" `mid_reply_s` into the bot's answer, while it is talking
         "brain"     never as the caller: wait for the brain's call.hangup
    The answer is the reply to the WAV, or with no WAV the greeting itself.
    `hangup_after` caps the call at that many seconds from answer, whatever the mode.
    """
    call = await gw.place_call(caller, ring_s=ring_s)
    summary: dict = {"call_id": call.call_id, "from": call.from_uri}
    answer = "reply" if wav else "greeting"

    async def script():
        if wav and wait_greeting:
            onset = await call.wait_bot_speech(0.0, greet_timeout)
            summary["greeting_onset"] = onset
            if onset is not None:
                summary["greeting_end"] = await call.wait_bot_quiet(quiet_s, greet_timeout)
        if wav:
            await call.play_wav(wav)
            summary["wav_end"] = call.wav_end
        if hangup == "brain":
            await call.ended.wait()
            return
        onset = await call.wait_bot_speech(call.wav_end if wav else 0.0,
                                           reply_timeout if wav else greet_timeout)
        summary[f"{answer}_onset"] = onset
        if onset is None:
            return  # no answer: hang up now
        if hangup == "mid-reply":
            while call.t < onset + mid_reply_s and not call.ended.is_set():
                await asyncio.sleep(FRAME_S)
            summary["bot_talking_at_hangup"] = call.bot_speaking()
        else:
            summary[f"{answer}_end"] = await call.wait_bot_quiet(quiet_s, reply_timeout)

    try:
        await asyncio.wait_for(script(), hangup_after)
    except asyncio.TimeoutError:
        summary["capped"] = True
    if not call.ended.is_set():
        await call.hangup("caller")
    if summary.get("reply_onset") is not None and summary.get("wav_end") is not None:
        summary["reply_latency"] = round(summary["reply_onset"] - summary["wav_end"], 3)
    call.save(record, record_stereo)
    summary.update({
        "ended_by": call.ended_by,
        "duration": round(call.t, 2),
        "bot_speech_s": round(call.bot_speech_s, 2),
        "bot_onsets": [round(t, 2) for t in call.bot_onsets],
        "audio_out_frames": call.audio_out_frames,
        "playout_dropped": call.playout_dropped,
        "controls": [m for _, m in gw.controls],
        "brain_connects": gw.connects,
        "violations": list(gw.violations),
    })
    for k in ("greeting_onset", "greeting_end", "wav_end", "reply_onset", "reply_end"):
        if summary.get(k) is not None:
            summary[k] = round(summary[k], 2)
    return summary


# --- CLI ----------------------------------------------------------------------------

def _parse(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m tests.fake_sip_gateway",
        description="Fake teaport-sip gateway: serve the SIP brain's socket and place "
                    "calls at it, with no SIP line or registration involved.")
    p.add_argument("--socket", required=True,
                   help="socket path to bind; the SIP brain's --socket must match")
    p.add_argument("--allow-live-path", action="store_true",
                   help=f"permit binding {LIVE_SOCKET} (teaport-sip must be stopped)")
    p.add_argument("--from", dest="caller", default=DEFAULT_CALLER,
                   help="caller ID (a number, or a full SIP URI)")
    p.add_argument("--wav", help="what the caller says after the greeting (any rate, "
                                 "16-bit; sent as 16 kHz mono like the gateway)")
    p.add_argument("--record", help="write what the caller heard (16 kHz mono WAV); "
                                    "with --calls > 1, -N is added before .wav")
    p.add_argument("--record-stereo", help="write caller (left) + bot (right)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--hangup-when-done", dest="hangup", action="store_const",
                   const="done", help="hang up once the bot has answered and gone "
                                      "quiet (default)")
    g.add_argument("--hangup-mid-reply", type=float, metavar="S",
                   help="hang up S seconds into the bot's answer")
    g.add_argument("--wait-brain-hangup", dest="hangup", action="store_const",
                   const="brain", help="never hang up as the caller; wait for the "
                                       "brain's call.hangup")
    p.add_argument("--hangup-after", type=float, metavar="N",
                   help="hang up N seconds after answer at the latest")
    p.add_argument("--no-greeting-wait", action="store_true",
                   help="say the WAV straight away instead of after the greeting")
    p.add_argument("--quiet", type=float, default=1.2,
                   help="seconds of bot silence that count as done (default 1.2)")
    p.add_argument("--ring", type=float, default=0.2, help="ringing time before answer")
    p.add_argument("--no-auto-answer", action="store_true",
                   help="wait for the brain's call.answer (gateway auto_answer=false)")
    p.add_argument("--no-replay", action="store_true",
                   help="behave as a gateway from before the reconnect replay")
    p.add_argument("--calls", type=int, default=1, help="calls to place, one after another")
    p.add_argument("--between", type=float, default=2.0, help="seconds between calls")
    p.add_argument("--wait-brain", type=float, default=120.0,
                   help="seconds to wait for the brain to connect")
    p.add_argument("--linger", type=float, default=1.0,
                   help="seconds to keep the socket open after the last call")
    p.add_argument("--summary-json", help="also write the summaries here")
    return p.parse_args(argv)


def _numbered(path: str | None, n: int, of: int) -> str | None:
    """out.wav -> out-2.wav when more than one call is placed."""
    if not path or of == 1:
        return path
    stem, ext = os.path.splitext(path)
    return f"{stem}-{n}{ext}"


async def _main(args) -> int:
    hangup = args.hangup or ("mid-reply" if args.hangup_mid_reply is not None else "done")
    gw = FakeSipGateway(args.socket, auto_answer=not args.no_auto_answer,
                        replay=not args.no_replay, allow_live_path=args.allow_live_path)
    await gw.start()
    summaries, failed = [], []
    try:
        log.info(f"waiting up to {args.wait_brain:g}s for the brain to connect to {args.socket}")
        if not await gw.wait_brain(args.wait_brain):
            log.error("no brain connected")
            return 2
        for i in range(args.calls):
            if i:
                await asyncio.sleep(args.between)
            if not gw.brain_connected.is_set() and not await gw.wait_brain(args.wait_brain):
                log.error("the brain went away between calls and did not come back")
                return 2
            rec, st = (_numbered(p, i + 1, args.calls)
                       for p in (args.record, args.record_stereo))
            s = await run_call(gw, caller=args.caller, wav=args.wav, ring_s=args.ring,
                               wait_greeting=not args.no_greeting_wait, quiet_s=args.quiet,
                               hangup=hangup, mid_reply_s=args.hangup_mid_reply or 1.0,
                               hangup_after=args.hangup_after, record=rec,
                               record_stereo=st)
            summaries.append(s)
            print(json.dumps(s, indent=2, ensure_ascii=False), flush=True)
            greeted = (s.get("greeting_onset") is not None
                       or (args.wav and args.no_greeting_wait)
                       or (not args.wav and hangup == "brain"))
            if not greeted:
                failed.append(f"call {i + 1}: no greeting audio")
            if args.wav and hangup != "brain" and s.get("reply_onset") is None:
                failed.append(f"call {i + 1}: no answer to the caller's audio")
            if s["violations"]:
                failed.append(f"call {i + 1}: {len(s['violations'])} protocol violation(s)")
        await asyncio.sleep(args.linger)
    finally:
        await gw.close()
        if args.summary_json:
            with open(args.summary_json, "w", encoding="utf-8") as f:
                json.dump(summaries, f, indent=2, ensure_ascii=False)
    for f in failed:
        log.error(f"FAIL: {f}")
    return 1 if failed else 0


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="[fake-sip-gw] %(message)s",
                        stream=sys.stderr)
    sys.exit(asyncio.run(_main(_parse(argv))))


if __name__ == "__main__":
    main()
