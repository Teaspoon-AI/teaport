#
# teaport — local audio bridge (USB mic array + speaker -> the brain's /talk)
#
# Talks to the agent through a sound card on the box itself, with no browser, phone or
# OpenClaw client in between. Built for the Seeed ReSpeaker XVF3800 (USB mic array with
# a 3.5 mm speaker jack and hardware echo cancellation), but any ALSA card that does
# 16 kHz stereo S16_LE both ways works.
#
#   mic --arecord--> THIS --binary PCM16 24k mono--> ws://127.0.0.1:7861/talk (brain)
#   speaker <--aplay-- THIS <--binary PCM16 24k mono-- brain (TTS)
#
# It is a /talk client exactly like the OpenClaw plugin and the Discord bridge (the wire
# format is gateway_serializer.py's), so the brain is unchanged. The brain's session
# arbiter (session_arbiter.py) gives the engine to one conversation at a time: a dial
# while another conversation is live is refused (BUSY_CLOSE_CODE; the room hears the busy
# line if it asked to talk), and only the bridge's own reconnect (?client=local-audio)
# replaces its session. Still, the bridge never dials on a timer:
#   * The card comes first. aplay and arecord are opened, and capture must deliver
#     data, BEFORE /talk is dialled: a dead or unplugged card evicts nobody. A card that
#     fails is retried with a backoff (RETRY_SECS doubling to RETRY_MAX_SECS) — but a
#     card that was unplugged is retried the moment it is plugged back in (CARD_POLL_SECS,
#     CARD_ABSENT_MIN_SECS: a card that only blinks off and on keeps its backoff).
#   * The first session after start dials at once (and so is greeted). After any session
#     ends — the brain hung up, its idle timeout, the STT-busy line — the card stays open
#     and the bridge waits for a VOICE in the room (LOCAL_AUDIO_WAKE_DB) before it dials
#     again. The last PREROLL_SECS of mic audio go out first on the new connection, so
#     the words that woke it reach the STT.
#   * With wake words (LOCAL_AUDIO_WAKE_WORDS) the room is heard, but only for them. The
#     bridge holds a /talk session ASLEEP (?wake=): the brain's own STT transcribes the
#     room, in any language the engine hears, and its wake gate (wake_gate.py) drops
#     every word until a final holds a wake phrase -- inside the STT service, so nothing
#     of it reaches the LLM, the agent, a caption or a log. A wake word wakes that
#     session; what was said after it ("hey teaport, what's the weather") is the first
#     turn. Awake, the conversation needs no wake word until nobody (user or bot) has
#     spoken for LOCAL_AUDIO_KEEPALIVE_SECS, or LOCAL_AUDIO_AWAKE_MAX_SECS have passed
#     since the last wake word, or the user sends the box to sleep (end_conversation):
#     then the session ENDS and a new asleep one opens. The conversation itself goes on:
#     a wake within LOCAL_AUDIO_CONVERSATION_SECS continues it, ungreeted (the brain
#     keeps its messages). Nothing here falls back to listening without the wake words:
#     a brain or STT that cannot be reached leaves the bridge deaf, retrying. A phone
#     call takes the engine from an asleep session (/talk/call): the bridge stays off
#     until the brain reports no call. The OLED face sleeps only while nothing is live
#     anywhere (the brain's /talk/status "live"): while another session or a call has
#     the box it stays awake, idle, and it sleeps once our asleep session is up again.
#   * Unless another client has the box: a Talk session took it from an asleep mic
#     (TAKEN_CLOSE_CODE), or the brain refused our dial because another conversation is
#     live (BUSY_CLOSE_CODE). Then the box is theirs. The bridge does not listen for a
#     voice — the dashboard user talking to their own session is a voice in the room too —
#     until the brain has reported no live session (/talk/status) for
#     LOCAL_AUDIO_BACKOFF_SECS. An unused session ends on the brain's idle timeout (~5 min),
#     so this is bounded. A bridge that starts (a crash, a unit restart) while another
#     client's session is live backs off the same way instead of dialling (taken_at_start).
#     A dial refused for a phone call (YIELD_CLOSE_CODE), or a conversation a call ended
#     (CALL_CLOSE_CODE), waits the call out instead.
#
# Two hardware facts shape it:
#   * The XVF3800 only streams capture while a playback stream is open on the same
#     card (arecord alone dies with "Input/output error" and tegra-xusb logs buffer
#     overruns). So playback never stops: when the brain is silent we feed the card
#     digital silence at the real-time rate, which also keeps the card's AEC clocked.
#   * It speaks 16 kHz / 2 channels only. Channel 0 is the beamformed, echo-cancelled
#     conversation mix; channel 1 is the ASR-tuned beam. LOCAL_AUDIO_CAPTURE_CHANNEL
#     picks one.
#
# Playback is paced LEAD_SECS ahead of what the card has PLAYED, read from the ALSA
# status of aplay's own substream (/proc/asound/<card>/pcm<dev>p/sub*/status) when the
# device is a plain hw: one; otherwise from the wall clock since the first write.
#
# The face tap: when an OLED avatar daemon (github.com/Teaspoon-AI/teaport-oled-avatar,
# `oled_face.py --serve`) is listening on LOCAL_AUDIO_FACE_SOCK, the bridge drives it
# from the same /talk events it already sees: the user's transcript (listening,
# thinking), the bot's captions (speaking, the reply text for its sentiment mood) and the
# speaker going quiet (idle). The mouth follows the AUDIO, not the words: each 20 ms
# chunk handed to the card carries its loudness, sent as {"mouth": 0..1} at the moment
# that chunk becomes audible, so the jaw moves on syllables. Only audio a caption has
# vouched for counts as speech: the brain's thinking sound (a typing loop during a long
# consult) is audio too, and it shows as "thinking", jaw shut. Datagrams are
# fire-and-forget: no daemon, no face, nothing else changes.
#
# arecord/aplay (alsa-utils) rather than a Python audio binding: no new dependency in
# the brain venv, and a dead card is a dead child process we can see and restart. No
# Pipecat import either: this process should stay small on the 8 GB box.
#
# Usage:  python -m teaport_brain.local_audio            (env below)
#
import asyncio
import glob
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.parse
import urllib.request
from collections import deque

import numpy as np
import soxr
from loguru import logger

from teaport_brain import display
from teaport_brain.wake_words import parse_phrases as wake_phrases

DEVICE_RATE = 16000
DEVICE_CHANNELS = 2
RELAY_RATE = 24000
CHUNK_MS = 20
CHUNK_FRAMES = DEVICE_RATE * CHUNK_MS // 1000
CHUNK_BYTES = CHUNK_FRAMES * DEVICE_CHANNELS * 2
# How far ahead of the card's playback we let the queue run. Bounds how much speech
# is still buffered, and so still audible, after a barge-in "clear".
LEAD_SECS = 0.12

DEVICE = os.getenv("LOCAL_AUDIO_DEVICE", "hw:CARD=Array,DEV=0")
CAPTURE_CHANNEL = int(os.getenv("LOCAL_AUDIO_CAPTURE_CHANNEL", "0"))
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")
URL = os.getenv("LOCAL_AUDIO_URL") or f"ws://127.0.0.1:{os.getenv('BRAIN_PORT', '7861')}/talk"
# A card that will not open, or a first dial the brain refuses, is retried this long
# after, doubling per consecutive failure up to the max; a good session resets it.
RETRY_SECS = 3.0
RETRY_MAX_SECS = 60.0
# While a card that is not plugged in waits out its backoff, look for it this often: it
# is retried as soon as it appears (a replug is noticed in seconds, not a minute).
# Looking costs nothing and dials nobody, so the backoff still protects other sessions.
CARD_POLL_SECS = 2.0
# ...but only a card gone this long counts as unplugged and plugged back in. One that
# drops off the bus and comes straight back (a brownout, a bad cable) is a flapping card:
# it keeps its backoff, or the bridge would retry it -- and, between failures, redial the
# brain -- every few seconds forever.
CARD_ABSENT_MIN_SECS = 5.0
# Capture must deliver its first byte within this, or the card counts as dead.
CAPTURE_START_SECS = 5.0
# A voice in the room, between sessions: chunks louder than WAKE_DB (dBFS RMS on the
# capture channel) for WAKE_SECS out of the last WAKE_WINDOW_SECS. The room measured
# ~-50 dBFS (rms 90-100) idle; speech at a few metres is well above -40.
WAKE_DB = float(os.getenv("LOCAL_AUDIO_WAKE_DB", "-40"))
WAKE_SECS = 0.26
WAKE_WINDOW_SECS = 0.4
# Mic audio kept while waiting, sent first on the connection the voice opens.
PREROLL_SECS = 1.0
# Wake words: a comma-separated list of phrases, any language ("hey teaport, tea port").
# Any value at all means wake mode; the brain matches them (wake_words.py).
WAKE_WORDS = os.getenv("LOCAL_AUDIO_WAKE_WORDS", "")
# Awake, the session ends once nobody (user or bot) has spoken for KEEPALIVE_SECS, or
# AWAKE_MAX_SECS after the last wake word with the bot quiet: a TV the bot keeps
# answering must not hold the room open for good. Saying a wake word again restarts it.
KEEPALIVE_SECS = float(os.getenv("LOCAL_AUDIO_KEEPALIVE_SECS", "45"))
AWAKE_MAX_SECS = float(os.getenv("LOCAL_AUDIO_AWAKE_MAX_SECS", "300"))
KEEPALIVE_POLL_SECS = 0.25
# A wake this soon after the last mic conversation ended continues it (the brain keeps
# its messages) and is not greeted; later, a new conversation and a greeting.
CONVERSATION_SECS = float(os.getenv("LOCAL_AUDIO_CONVERSATION_SECS", "7200"))
# A wake session that ends this soon after it opened (the brain refused it, or went
# away) is retried after a backoff, not at once.
SHORT_SESSION_SECS = 5.0
# A wake session sends no mic audio until the brain's hello says it gates the room
# ("wake": "asleep"|"awake"); one that has not said so within this is left.
HELLO_SECS = 15.0
# Taken by another Talk client: no voice wake until the brain has had no live /talk
# session for this long (Backoff), asked every BACKOFF_POLL_SECS. 0 or less: no back-off.
BACKOFF_SECS = float(os.getenv("LOCAL_AUDIO_BACKOFF_SECS", "60"))
BACKOFF_POLL_SECS = 5.0
# At process start a Talk session may still be our own previous process's (a crash, a
# unit restart): the brain lets it go within a second of its socket closing (the kernel
# closes it when the process dies). Only one still live after this long is another
# client's, which the first dial would evict.
START_SETTLE_SECS = 3.0
START_POLL_SECS = 0.5
# The brain's close codes (gateway_server.*_CLOSE_CODE; not imported: that pulls Pipecat
# into this process). TAKEN: our asleep session gave the box to a Talk session (or our own
# reconnect replaced it).
TAKEN_CLOSE_CODE = 4001
# ...an asleep session closed or refused for a phone call (YIELD), and a wake session
# whose STT could not connect (STT_CLOSE_CODE; the reason says why).
YIELD_CLOSE_CODE = 4002
STT_CLOSE_CODE = 4003
# ...a dial refused because another conversation is live (BUSY), and a conversation a
# phone call ended (CALL; not sent to the room today -- a call is refused during an awake
# room conversation -- but it means "wait the call out" if it ever is).
BUSY_CLOSE_CODE = 4004
CALL_CLOSE_CODE = 4005
# Who we are to the brain's session arbiter (?client=): one bridge per box, so a restarted
# bridge replaces its own previous session rather than being refused by it.
CLIENT_ID = "local-audio"
# Not listening for a voice until our own playback has been silent this long (the
# card's AEC is good, not perfect: the tail of the STT-busy line must not redial).
ECHO_TAIL_SECS = 0.3
# Mic chunks queued for a slow socket before the oldest are dropped (5 s).
MIC_QUEUE_CHUNKS = 250
# How often the card's playback position is read back from ALSA.
STATUS_EVERY_SECS = 0.3
# Lines of aplay/arecord stderr kept for the error message when one of them dies.
STDERR_TAIL_LINES = 20
# The avatar's socket; its path lives in display.py (the screens use it too).
FACE_SOCK = os.getenv("LOCAL_AUDIO_FACE_SOCK", display.SOCK)
# Assistant finals that are not speech, told apart from reply text only by these
# prefixes: TEAPORT_ENDPOINT_DEBUG's timing chips (endpoint_debug.py) and the tool-call
# bubbles (tools._wrap). Kept here rather than imported: importing either pulls Pipecat
# into this process.
DEBUG_CHIP_PREFIXES = ("🎙️ VAD:", "⏱️ turn committed", "🔊 first audio", "> 🔧", "> ⚠️")
# What this client can do, announced on /talk as ?features= (tools.py, THE TOOL
# CONTRACT): volume and restart are client tools it performs (set_volume,
# restart_session); local says someone is at the box itself, which Wi-Fi setup needs
# (the brain runs that one). Each is offered only where its TEAPORT_TOOL_* switch is on.
FEATURES = ("volume", "restart", "local")
# ...and with wake words, end_conversation (back to sleep): with none there is no sleep.
WAKE_FEATURES = ("sleep",)
# set_volume: a percent kept across sessions and reboots. 100 is the card's own level
# (no boost: it would clip); below it, a gain on the samples over VOLUME_RANGE_DB, and
# 0 is silent. "louder"/"quieter" move it by VOLUME_STEP.
VOLUME_FILE = os.path.expanduser("~/.local/state/teaport/local-audio.json")
VOLUME_STEP = 10
VOLUME_RANGE_DB = 40.0
# restart_session: the model calls it, says one goodbye, and the session restarts once
# that has played (Restart). Wait this long for the goodbye before restarting without it.
RESTART_SPEECH_WAIT_SECS = 5.0
# The speaker has been quiet this long after a reply: the face goes back to idle (or
# to thinking, while the thinking sound plays).
FACE_IDLE_SECS = 0.8
# Uncaptioned audio (the thinking sound) this long while the face is idle: thinking.
FACE_BED_SECS = 1.0
# The final caption is released AT the last word, ahead of that word's audio: audio
# arriving this soon after it is still the reply's tail, not the thinking sound.
VOICED_TAIL_SECS = 0.6
# Thinking or listening with no transcript and no audio for this long: idle. Timed
# from the last activity, not from entry: a consult thinks for ~40 s, typing all along.
FACE_STALE_SECS = 12.0
# Send each mouth level this much before its audio is heard, to cover the avatar's own
# delay (a 30 fps frame plus the I2C write of the panel).
FACE_ADVANCE_SECS = int(os.getenv("LOCAL_AUDIO_FACE_ADVANCE_MS", "50")) / 1000
# Chunk loudness -> mouth opening: closed at or below the floor, fully open SPAN dB above.
MOUTH_FLOOR_DB = -45.0
MOUTH_SPAN_DB = 25.0
MOUTH_STEP = 0.05  # smaller changes are not worth a datagram...
# ...but an open mouth is re-sent this often, so the avatar's 0.5 s "levels have
# stopped" timeout never lapses on a steady (or clamped-at-1.0) vowel.
MOUTH_KEEPALIVE_SECS = 0.2


def level_db(pcm: np.ndarray) -> float:
    """RMS level of int16 samples in dBFS (-180 for digital silence)."""
    if not pcm.size or not pcm.any():
        return -180.0
    x = pcm.astype(np.float32)
    return float(20 * np.log10(np.sqrt(np.mean(x * x)) / 32768 + 1e-9))


def mouth_level(chunk: bytes) -> float:
    """Loudness of one chunk of device PCM as a mouth opening, 0 (closed) to 1."""
    db = level_db(np.frombuffer(chunk, dtype="<i2"))
    return float(min(1.0, max(0.0, (db - MOUTH_FLOOR_DB) / MOUTH_SPAN_DB)))


class FaceTap:
    """Conversation events -> the OLED avatar's datagram protocol.

    {"state": idle|listening|thinking|speaking}, {"mouth": 0..1} (jaw opening, from
    the played audio), {"text": t} (VADER mood). States are sent only on change, mouth
    levels when they move (and every MOUTH_KEEPALIVE_SECS while open), and only while
    speaking: leaving "speaking" shuts the mouth.
    """

    def __init__(self, path: str | None = None, send=None, clock=time.monotonic,
                 features=None):
        self._path = path or FACE_SOCK
        self._features = features or (lambda: display.features(self._path))
        self._sock = None
        self._send = send or self._sendto
        self._clock = clock
        self._state = None
        self._caption = ""
        self._mouth = 0.0
        self._mouth_t = 0.0
        self._active_t = clock()
        self._bed_since = None
        self._audio_t = None
        # True from a caption with new words until the utterance's final (plus
        # VOICED_TAIL_SECS): the audio arriving meanwhile is speech.
        self._voiced = False
        self._voiced_until = 0.0

    def _sendto(self, data: bytes) -> None:
        try:
            if self._sock is None:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self._sock.setblocking(False)
            self._sock.sendto(data, self._path)
        except OSError:
            pass  # no daemon, or its queue is full: the face is optional

    def _emit(self, ev: dict) -> None:
        self._send(json.dumps(ev).encode())

    @property
    def voiced(self) -> bool:
        return self._voiced or self._clock() < self._voiced_until

    def _activity(self) -> None:
        self._active_t = self._clock()

    def _shut(self) -> None:
        if self._mouth:
            self._mouth = 0.0
            self._emit({"mouth": 0.0})

    def state(self, s: str) -> None:
        if s != "speaking":
            self._shut()
        if s != self._state:
            self._state = s
            self._emit({"state": s})

    def user(self, text: str, final: bool) -> None:
        self._activity()
        if final:
            if text.strip():
                self.state("thinking")
        elif text:
            self.state("listening")

    def assistant(self, text: str, final: bool) -> bool:
        """A caption. True when it brought new words: the audio still queued is speech."""
        self._activity()
        text = text or ""
        prev = self._caption
        new = text[len(prev):] if text.startswith(prev) else text
        self._caption = "" if final else text
        fresh = bool(new.strip())
        if fresh:
            self._bed_since = None
            self.state("speaking")
        if final:
            if self._voiced:
                self._voiced_until = self._clock() + VOICED_TAIL_SECS
            self._voiced = False
        elif fresh:
            self._voiced = True
        if final and text:
            self._emit({"text": text})
        return fresh

    def audio(self) -> None:
        """A chunk of bot audio arrived (speech, or the uncaptioned thinking sound)."""
        now = self._clock()
        self._active_t = now
        if self.voiced:
            self._bed_since = None
        elif self._bed_since is None or now - (self._audio_t or 0) > 0.5:
            self._bed_since = now
        self._audio_t = now

    def mouth(self, level: float) -> None:
        if self._state != "speaking":
            return self._shut()
        level = round(level, 2)
        now = self._clock()
        if (abs(level - self._mouth) >= MOUTH_STEP or (level == 0 and self._mouth)
                or (level and self._mouth and now - self._mouth_t >= MOUTH_KEEPALIVE_SECS)):
            self._mouth = level
            self._mouth_t = now
            self._emit({"mouth": level})

    def interrupted(self) -> None:
        self._activity()
        self._caption = ""
        self._voiced, self._voiced_until = False, 0.0
        self.state("listening")

    def quiet(self) -> None:
        """The speech has been heard out: idle, or thinking while the thinking sound plays."""
        if self._state == "speaking":
            self._caption = ""
            # Only a sustained run of uncaptioned audio is the thinking sound; a stray
            # tail frame is not (the 1 s idle -> thinking rule catches a bed that starts
            # right after the speech).
            now = self._clock()
            bed = (self._bed_since is not None and self._audio_t is not None
                   and now - self._audio_t < 0.5 and now - self._bed_since >= FACE_BED_SECS)
            self.state("thinking" if bed else "idle")

    def tick(self) -> None:
        now = self._clock()
        if (self._state == "idle" and self._bed_since is not None
                and self._audio_t is not None and now - self._audio_t < 0.5
                and now - self._bed_since >= FACE_BED_SECS):
            self.state("thinking")
        elif self._state in ("thinking", "listening") and now - self._active_t >= FACE_STALE_SECS:
            self.state("idle")

    def ended(self) -> None:
        """The session is over: idle, mouth shut, whatever we thought it was."""
        self._caption = ""
        self._voiced, self._voiced_until = False, 0.0
        self._bed_since = self._audio_t = None
        self._mouth = 0.0
        self._emit({"mouth": 0.0})
        self._state = "idle"
        self._emit({"state": "idle"})

    def sleep(self) -> None:
        """Waiting for a wake word: "sleeping" (closed eyes) where the avatar draws it --
        it says so in its features file, read now, as display.py reads it -- else idle."""
        self.state("sleeping" if self._features().get("sleep") else "idle")

    def woke(self) -> None:
        """A wake word: listening."""
        self._activity()
        self.state("listening")


class Volume:
    """The set_volume level: a percent, saved to `path` so it outlives the session."""

    def __init__(self, path: str | None = None):
        self._path = path or VOLUME_FILE
        self.percent = 100
        try:
            with open(self._path) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return  # first run, or a file we cannot read: full volume
        percent = _percent(saved.get("volume") if isinstance(saved, dict) else None)
        if percent is not None:
            self.percent = percent  # anything else in the file: full volume

    @property
    def gain(self) -> float:
        if self.percent <= 0:
            return 0.0
        return 10 ** (-VOLUME_RANGE_DB * (1 - self.percent / 100) / 20)

    def apply(self, args) -> dict:
        """The set_volume tool: {"direction": "louder"|"quieter"} or {"level": 0..100}.
        Given both, the level wins (it is the exact one) and the note says so."""
        if not isinstance(args, dict):
            return {"ok": False, "error": "give a direction (louder or quieter) or a level"}
        direction, level = args.get("direction"), args.get("level")
        note = None
        if level is not None:
            target = _percent(level)
            if target is None:
                return {"ok": False, "error": f"level must be 0 to 100, not {level!r}"}
            if direction is not None:
                note = f"set to the level given; the direction ({direction}) was ignored"
        elif direction in ("louder", "quieter"):
            target = max(0, min(100, self.percent
                                + (VOLUME_STEP if direction == "louder" else -VOLUME_STEP)))
            if target == self.percent:
                note = f"already at the {'top' if direction == 'louder' else 'bottom'}"
        else:
            return {"ok": False, "error": "give a direction (louder or quieter) or a level"}
        result = {"ok": True, "volume": target}
        if note:
            result["note"] = note
        self.percent = target
        self._save()
        logger.info(f"volume -> {target}%")
        return result

    def _save(self) -> None:
        # Written aside and renamed over the old file: a kill or a power cut mid-write
        # leaves the old level, never an empty file (which would read back as 100%).
        tmp = f"{self._path}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump({"volume": self.percent}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning(f"volume {self.percent}% applied but not saved ({e})")
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _percent(value) -> int | None:
    """A volume level, 0..100 (clamped), from a number or a numeric string; None for
    anything else (a bool, NaN, infinity, a list, ...)."""
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return max(0, min(100, round(f)))


def capture_to_relay(raw: bytes, resampler: "soxr.ResampleStream", channel: int) -> bytes:
    """One chunk of device capture (16 kHz stereo S16_LE) -> relay audio (24 kHz mono)."""
    mono = capture_channel(raw, channel)
    return resampler.resample_chunk(mono).astype("<i2").tobytes()


def capture_channel(raw: bytes, channel: int) -> np.ndarray:
    """One channel of device capture, as contiguous int16 samples."""
    pcm = np.frombuffer(raw, dtype="<i2")
    pcm = pcm[: len(pcm) - len(pcm) % DEVICE_CHANNELS].reshape(-1, DEVICE_CHANNELS)
    return np.ascontiguousarray(pcm[:, channel])


def relay_to_device(raw: bytes, resampler: "soxr.ResampleStream") -> bytes:
    """One chunk of brain audio (24 kHz mono S16_LE) -> device playback (16 kHz stereo)."""
    pcm = np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype="<i2")
    mono = resampler.resample_chunk(pcm).astype("<i2")
    return np.repeat(mono[:, None], DEVICE_CHANNELS, axis=1).tobytes()


# ------------------------------------------------------------ the card's own clock
# /proc/asound/<card>/pcm<dev>p/sub<n>/status (sound/core/pcm.c,
# snd_pcm_substream_proc_status_read) is "closed" while nobody has the substream open,
# else "state: RUNNING" (PREPARED before the start threshold, XRUN after an underrun)
# followed by owner_pid, trigger_time, tstamp, delay, avail, avail_max, a "-----" line,
# hw_ptr and appl_ptr. hw_ptr counts the frames the hardware has taken; appl_ptr those
# the application has written; delay = (appl_ptr - hw_ptr) plus what the driver still
# holds past hw_ptr (snd-usb-audio's URBs in flight).

def card_present(device: str) -> bool | None:
    """Is the card `device` names plugged in? None when the device string does not name
    one card (plughw:, default, a pipewire alias...): then nobody can tell."""
    pcm_dir = pcm_status_dir(device)
    return None if pcm_dir is None else os.path.isdir(os.path.dirname(pcm_dir))


async def wait_for_card(device: str, secs: float) -> bool:
    """Wait out a card backoff of `secs`, ending early (True) if the card is plugged back
    in after being gone at least CARD_ABSENT_MIN_SECS. A card that is there but failed,
    or one that only blinked off and on (flapping), waits the whole backoff."""
    if card_present(device) is not False:
        await asyncio.sleep(secs)
        return False
    deadline = absent_since = time.monotonic()
    deadline += secs
    while time.monotonic() < deadline:
        await asyncio.sleep(min(CARD_POLL_SECS, max(0.0, deadline - time.monotonic())))
        if not card_present(device):
            if absent_since is None:
                absent_since = time.monotonic()  # gone again: a new absence starts
        elif absent_since is not None:
            if time.monotonic() - absent_since >= CARD_ABSENT_MIN_SECS:
                return True
            absent_since = None  # back too soon: a flap, not a replug
    return False


def pcm_status_dir(device: str) -> str | None:
    """`hw:` ALSA device string -> its playback PCM's /proc dir; None for anything else
    (plughw:, default, a pipewire alias...), which keeps the wall-clock timeline."""
    if not device.startswith("hw:"):
        return None
    card = dev = None
    positional = []
    for arg in device[3:].split(","):
        key, eq, val = arg.partition("=")
        if not eq:
            positional.append(arg)
        elif key.upper() == "CARD":
            card = val
        elif key.upper() == "DEV":
            dev = val
    if card is None and positional:
        card = positional.pop(0)
    if dev is None:
        dev = positional.pop(0) if positional else "0"
    card, dev = (card or "").strip("\"'"), dev.strip("\"'")
    if not card or not dev.isdigit() or not re.fullmatch(r"[A-Za-z0-9_-]+", card):
        return None
    return f"/proc/asound/{'card' + card if card.isdigit() else card}/pcm{dev}p"


def parse_pcm_status(text: str) -> dict | None:
    """The fields of one substream status file; None when closed or unparsable."""
    fields = {}
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if sep:
            fields[key.strip()] = val.strip()
    try:
        return {"state": fields["state"], "owner_pid": int(fields["owner_pid"]),
                "hw_ptr": int(fields["hw_ptr"]), "appl_ptr": int(fields["appl_ptr"]),
                "delay": int(fields["delay"])}
    except (KeyError, ValueError):
        return None


def played_frames(status: dict) -> int | None:
    """Frames the card has actually played, from a RUNNING status: hw_ptr, less what the
    driver has taken but not yet sent out (delay beyond the ring buffer's own fill)."""
    if status["state"] != "RUNNING":
        return None
    in_flight = status["delay"] - (status["appl_ptr"] - status["hw_ptr"])
    if not 0 <= in_flight < DEVICE_RATE:
        in_flight = 0
    return max(0, status["hw_ptr"] - in_flight)


def read_pcm_status(pcm_dir: str, pid: int) -> dict | None:
    """The status of the substream `pid` (our aplay) has open under pcm_dir, if any."""
    for path in sorted(glob.glob(os.path.join(pcm_dir, "sub*", "status"))):
        try:
            with open(path) as f:
                status = parse_pcm_status(f.read())
        except OSError:
            continue
        if status and status["owner_pid"] == pid:
            return status
    return None


class Playback:
    """Real-time paced feed to the card, silence when the brain has nothing to say.

    `write` is the sink (aplay's stdin in production). The timeline maps a frame
    position to the moment it is heard: _start + position / rate. It starts at the first
    write and is re-anchored by `resync` whenever the card reports what it has played,
    so aplay's start-up time, an underrun or a drifting card clock cannot leave the
    queue (and the mouth) running ahead. The clock is injectable so the pacing and the
    barge-in flush can be tested without sleeping.
    """

    def __init__(self, write, clock=time.monotonic, gain=lambda: 1.0):
        self._write = write
        self._clock = clock
        self._gain = gain  # the set_volume level, read per chunk
        # [device bytes, voiced]: voiced audio is speech a caption has vouched for.
        self._pending: deque[list] = deque()
        self._pending_bytes = 0
        self._resampler = soxr.ResampleStream(RELAY_RATE, DEVICE_RATE, 1, dtype="int16")
        self._start: float | None = None
        self._written = 0      # frames handed to the card
        self._sound_end = 0    # frame position just past the last non-silent chunk
        # [frame position, mouth level, voiced] for every chunk not yet heard.
        self._levels: deque[list] = deque()
        self.synced = False

    def push(self, relay_audio: bytes, voiced: bool = False) -> None:
        data = relay_to_device(relay_audio, self._resampler)
        if not data:
            return
        if self._pending and self._pending[-1][1] == voiced:
            self._pending[-1][0] += data
        else:
            self._pending.append([bytearray(data), voiced])
        self._pending_bytes += len(data)

    def mark_voiced(self) -> None:
        """A caption with new words: the audio it belongs to is speech — what is still
        queued here and what the card has not played yet (captions trail their audio)."""
        for seg in self._pending:
            seg[1] = True
        for lv in self._levels:
            lv[2] = True

    def clear(self) -> None:
        """Barge-in: drop what has not been handed to the card yet."""
        self._pending.clear()
        self._pending_bytes = 0
        # A fresh resampler: the old one holds the tail of the interrupted speech.
        self._resampler = soxr.ResampleStream(RELAY_RATE, DEVICE_RATE, 1, dtype="int16")

    @property
    def speaking(self) -> bool:
        """Speech is still queued for the card."""
        return any(seg[1] for seg in self._pending)

    def echo_free(self, now: float) -> bool:
        """Nothing queued, and the last sound played out ECHO_TAIL_SECS ago."""
        if self._pending_bytes:
            return False
        return self._start is None or now >= self._start + self._sound_end / DEVICE_RATE + ECHO_TAIL_SECS

    def _take(self) -> tuple[bytes, bool]:
        out, voiced = bytearray(), False
        while self._pending and len(out) < CHUNK_BYTES:
            seg = self._pending[0]
            n = CHUNK_BYTES - len(out)
            out += seg[0][:n]
            voiced = voiced or seg[1]
            del seg[0][:n]
            if not seg[0]:
                self._pending.popleft()
        self._pending_bytes -= len(out)
        # Short tail of a reply, or nothing: pad with silence so the card's clock never
        # stops (the capture side dies without it).
        return bytes(out.ljust(CHUNK_BYTES, b"\0")), voiced

    def pump(self) -> None:
        """Hand the card as many chunks as keep it LEAD_SECS ahead of what it has played."""
        now = self._clock()
        if self._start is None:
            self._start = now
        while self._written / DEVICE_RATE - (now - self._start) < LEAD_SECS:
            chunk, voiced = self._take()
            # The mouth follows the voice as synthesized, not the volume knob.
            self._levels.append([self._written, mouth_level(chunk), voiced])
            gain = self._gain()
            if gain != 1.0 and chunk.strip(b"\0"):
                pcm = np.frombuffer(chunk, dtype="<i2").astype(np.float32) * gain
                self._write(np.clip(pcm, -32768, 32767).astype("<i2").tobytes())
            else:
                self._write(chunk)
            self._written += CHUNK_FRAMES
            if chunk.strip(b"\0"):
                self._sound_end = self._written

    def resync(self, played: int, now: float | None = None) -> None:
        """The card has played `played` frames by `now`: re-anchor the timeline on it.

        After an underrun hw_ptr can count up to a period the card played past our data;
        the timeline then runs that much early, never late, which the next pumps absorb."""
        now = self._clock() if now is None else now
        self._start = now - played / DEVICE_RATE
        self.synced = True

    def mouth_due(self, now: float) -> float | None:
        """The loudest speech chunk heard by `now` since the last call (0 for a chunk
        that is not speech), None if no chunk came due."""
        level = None
        while self._levels and self._start + self._levels[0][0] / DEVICE_RATE <= now:
            _, lv, voiced = self._levels.popleft()
            lv = lv if voiced else 0.0
            level = lv if level is None else max(level, lv)
        return level


class VoiceGate:
    """Someone talking in the room: WAKE_SECS of loud chunks within WAKE_WINDOW_SECS."""

    def __init__(self, db: float = WAKE_DB):
        self._db = db
        self._need = round(WAKE_SECS * 1000 / CHUNK_MS)
        self._hits: deque[bool] = deque(maxlen=round(WAKE_WINDOW_SECS * 1000 / CHUNK_MS))

    def feed(self, db: float) -> bool:
        self._hits.append(db >= self._db)
        return sum(self._hits) >= self._need

    def reset(self) -> None:
        self._hits.clear()


class CardError(RuntimeError):
    """The sound card failed (would not open, or a stream died)."""


class DialError(RuntimeError):
    """The brain's /talk could not be reached."""


async def _spawn(*argv: str, **kw) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(*argv, **kw)


class Card:
    """The sound card, held across /talk sessions: aplay fed paced silence (or the
    brain's speech), arecord read continuously into the session or, between sessions,
    a pre-roll ring and the voice gate. `failed` resolves with a CardError once any of
    it dies; the owner then closes the card and opens a new one."""

    FMT = ["-f", "S16_LE", "-r", str(DEVICE_RATE), "-c", str(DEVICE_CHANNELS), "-q", "-t", "raw"]

    def __init__(self, face: FaceTap, device: str | None = None, volume: Volume | None = None):
        self.face = face
        self.volume = volume or Volume()
        self.device = device = device or DEVICE
        self.play: Playback | None = None
        self.failed: asyncio.Future = asyncio.get_running_loop().create_future()
        self.preroll: deque[bytes] = deque(maxlen=round(PREROLL_SECS * 1000 / CHUNK_MS))
        self.gate = VoiceGate()
        self.voice = asyncio.Event()
        # The live session's mic queue; None between sessions.
        self.sink: asyncio.Queue | None = None
        self._aplay = None
        self._procs: list[asyncio.subprocess.Process] = []
        self._tails: dict[int, tuple[asyncio.Task, deque]] = {}
        self._tasks: list[asyncio.Task] = []
        self._pcm_dir = pcm_status_dir(device)

    # -- processes
    async def _start_proc(self, *argv: str, **kw) -> asyncio.subprocess.Process:
        try:
            p = await _spawn(*argv, stderr=asyncio.subprocess.PIPE, **kw)
        except OSError as e:  # alsa-utils missing: a card we cannot use, retried like one
            raise CardError(f"cannot run {argv[0]}: {e}") from None
        self._procs.append(p)
        tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._tails[p.pid] = (asyncio.create_task(self._drain(p.stderr, tail)), tail)
        return p

    @staticmethod
    async def _drain(stream: asyncio.StreamReader, tail: deque) -> None:
        # Read continuously: an unread stderr pipe (an underrun line per xrun) would
        # fill and block the child. Only the last lines are kept, for the error message.
        rest = b""
        while data := await stream.read(4096):
            *lines, rest = (rest + data).split(b"\n")
            rest = rest[-1024:]
            tail.extend(ln.decode(errors="replace").strip() for ln in lines if ln.strip())
        if rest.strip():
            tail.append(rest.decode(errors="replace").strip())

    async def _stderr_of(self, p: asyncio.subprocess.Process) -> str:
        task, tail = self._tails[p.pid]
        try:
            await asyncio.wait_for(asyncio.shield(task), 1.0)
        except Exception:  # noqa: BLE001 — best effort, for a message (timeout included)
            pass
        return " | ".join(tail) or "no output"

    def _run(self, coro) -> None:
        t = asyncio.create_task(coro)
        t.add_done_callback(self._task_done)
        self._tasks.append(t)

    def _task_done(self, t: asyncio.Task) -> None:
        if t.cancelled() or self.failed.done():
            return
        e = t.exception()
        if not isinstance(e, CardError):
            e = CardError(f"audio task ended: {e!r}")
        self.failed.set_exception(e)

    def _check(self) -> None:
        if self.failed.done():
            raise self.failed.exception()

    # -- lifecycle
    async def open(self) -> None:
        """Playback first, then capture, which must deliver data; raises CardError."""
        # aplay starts the stream only once its buffer is full, and capture opened before
        # that fails at once. A buffer smaller than the LEAD_SECS we prime it with means
        # the first pump() starts playback.
        period_us, buffer_us = CHUNK_MS * 1000, int(LEAD_SECS * 1e6) - CHUNK_MS * 1000
        self._aplay = await self._start_proc(
            "aplay", *self.FMT, "-D", self.device,
            f"--period-time={period_us}", f"--buffer-time={buffer_us}",
            stdin=asyncio.subprocess.PIPE)
        self.play = Playback(self._aplay.stdin.write, gain=lambda: self.volume.gain)
        self._run(self._watch(self._aplay, "playback"))
        self._run(self._feed())
        if self._pcm_dir is None:
            logger.info(f"playback timeline: wall clock ({self.device} is not a hw: device)")
        arecord, first = await self._open_capture()
        self._run(self._capture(arecord, first))

    async def _open_capture(self) -> tuple[asyncio.subprocess.Process, bytes]:
        # Playback is running by now; give it a moment, and retry a capture that
        # still lost the race rather than failing the whole card.
        for attempt in range(1, 4):
            await asyncio.sleep(0.2 * attempt)
            self._check()
            arecord = await self._start_proc(
                "arecord", *self.FMT, "-D", self.device, stdout=asyncio.subprocess.PIPE)
            try:
                first = await asyncio.wait_for(arecord.stdout.read(1), CAPTURE_START_SECS)
            except asyncio.TimeoutError:
                first = b""
            if first:
                return arecord, first
            if arecord.returncode is None:
                try:
                    arecord.terminate()
                except ProcessLookupError:
                    pass
            logger.warning(f"capture did not start (attempt {attempt}): {await self._stderr_of(arecord)}")
        self._check()
        raise CardError(f"capture on {self.device} would not start")

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._aplay is not None:
            self._aplay.stdin.close()
        for p in self._procs:
            if p.returncode is None:
                try:
                    p.terminate()
                except ProcessLookupError:
                    pass
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for p in self._procs:
            try:
                await asyncio.wait_for(p.wait(), 2.0)
            except asyncio.TimeoutError:
                p.kill()
                try:
                    await asyncio.wait_for(p.wait(), 2.0)
                except asyncio.TimeoutError:
                    logger.warning(f"pid {p.pid} would not die; leaving it")
        for task, _ in self._tails.values():
            task.cancel()
        await asyncio.gather(*(t for t, _ in self._tails.values()), return_exceptions=True)
        if self.failed.done():
            self.failed.exception()  # retrieved: no "never retrieved" noise
        else:
            self.failed.cancel()

    # -- tasks
    async def _watch(self, p: asyncio.subprocess.Process, what: str) -> None:
        rc = await p.wait()
        raise CardError(f"{what} exited ({rc}): {await self._stderr_of(p)}")

    async def _feed(self) -> None:
        play, face = self.play, self.face
        quiet_since, next_status, logged = None, 0.0, False
        opened = time.monotonic()
        while True:
            now = time.monotonic()
            if self._pcm_dir and now >= next_status:
                next_status = now + STATUS_EVERY_SECS
                status = read_pcm_status(self._pcm_dir, self._aplay.pid)
                played = played_frames(status) if status else None
                if played is not None:
                    play.resync(played, now)
                    if not logged:
                        logger.info(f"playback timeline: following the card ({self._pcm_dir})")
                        logged = True
                elif not logged and now - opened > 3:
                    logger.info(f"playback timeline: wall clock (no running status under {self._pcm_dir})")
                    logged = True
            play.pump()
            try:
                await self._aplay.stdin.drain()
            except ConnectionError:
                await asyncio.sleep(1)  # aplay is gone; _watch says why
                raise CardError("playback pipe closed") from None
            level = play.mouth_due(time.monotonic() + FACE_ADVANCE_SECS)
            if level is not None:
                face.mouth(level)
            if play.speaking:
                quiet_since = None
            else:
                quiet_since = quiet_since or now
                if now - quiet_since >= FACE_IDLE_SECS:
                    face.quiet()
            face.tick()
            await asyncio.sleep(CHUNK_MS / 2000)

    async def _capture(self, arecord: asyncio.subprocess.Process, buf: bytes) -> None:
        resampler = soxr.ResampleStream(DEVICE_RATE, RELAY_RATE, 1, dtype="int16")
        logger.info("capture running")
        while True:
            try:
                buf += await arecord.stdout.readexactly(CHUNK_BYTES - len(buf))
            except asyncio.IncompleteReadError:
                raise CardError(f"capture stopped: {await self._stderr_of(arecord)}") from None
            mono = capture_channel(buf, CAPTURE_CHANNEL)
            relay = resampler.resample_chunk(mono).astype("<i2").tobytes()
            buf = b""
            if self.sink is not None:
                if relay:
                    if self.sink.full():
                        self.sink.get_nowait()
                    self.sink.put_nowait(relay)
                continue
            if relay:
                self.preroll.append(relay)
            if not self.play.echo_free(time.monotonic()):
                self.gate.reset()
            elif self.gate.feed(level_db(mono)):
                self.voice.set()

    # -- waiting
    async def _until(self, aw) -> None:
        """Await `aw`, or raise the card's failure if that comes first."""
        task = asyncio.ensure_future(aw)
        try:
            await asyncio.wait([task, self.failed], return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not task.done():
                task.cancel()
        self._check()

    async def wait_for_voice(self) -> None:
        self.gate.reset()
        self.voice.clear()
        await self._until(self.voice.wait())

    async def idle_for(self, secs: float) -> None:
        await self._until(asyncio.sleep(secs))


def wake_mode() -> bool:
    """LOCAL_AUDIO_WAKE_WORDS set at all: wake words, and nothing heard without one."""
    return bool(WAKE_WORDS.strip())


def _url(awake: bool = False) -> str:
    query = {"features": ",".join(FEATURES + (WAKE_FEATURES if wake_mode() else ())),
             "client": CLIENT_ID}
    if not wake_mode():
        # Without wake words the brain counts our session as asleep (a Talk client may take
        # it) until someone speaks, and again after this long with nobody speaking.
        query["keepalive"] = f"{KEEPALIVE_SECS:g}"
    if wake_mode():
        # The phrases ride the URL (they are settings, not speech); the brain parses them.
        query["wake"] = WAKE_WORDS
        query["conversation_secs"] = f"{CONVERSATION_SECS:g}"
        if awake:
            query["awake"] = "1"
    if GATEWAY_TOKEN:
        query["token"] = GATEWAY_TOKEN
    return URL + ("&" if "?" in URL else "?") + urllib.parse.urlencode(
        query, safe=",", quote_via=urllib.parse.quote)


def _status_url() -> str:
    """/talk/status next to the /talk we dial (gateway_server.talk_status). No query:
    the token goes as a header, where uvicorn's access log does not record it."""
    parts = urllib.parse.urlsplit(URL)
    return urllib.parse.urlunsplit(("https" if parts.scheme == "wss" else "http", parts.netloc,
                                    parts.path.rstrip("/") + "/status", "", ""))


def _talk_status_sync() -> dict | None:
    headers = {"Authorization": f"Bearer {GATEWAY_TOKEN}"} if GATEWAY_TOKEN else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(_status_url(), headers=headers),
                                    timeout=3) as r:
            status = json.load(r)
            return status if isinstance(status, dict) else None
    except Exception:  # noqa: BLE001 — brain down or restarting, a brain without it: unknown
        return None


def _talk_active_sync() -> bool | None:
    # "active": any session holds the engine (a call included), not just a /talk one.
    status = _talk_status_sync()
    return None if status is None else bool(status.get("active"))


async def talk_active() -> bool | None:
    """Does a /talk session hold the brain's slot? None when the brain cannot say."""
    return await asyncio.to_thread(_talk_active_sync)


def _live_sync() -> bool | None:
    """Is a conversation live anywhere on the box (Talk, a call, the room awake)? None
    when the brain cannot say. A brain without "live" is read from what it does report."""
    status = _talk_status_sync()
    if status is None:
        return None
    if "live" in status:
        return bool(status["live"])
    return bool(status.get("call") or (status.get("active") and not status.get("asleep")))


async def anything_live() -> bool | None:
    return await asyncio.to_thread(_live_sync)


async def rest_face(face: "FaceTap") -> None:
    """The face between sessions. Sleeping means the voice loop is not active anywhere
    (the user's rule): so idle -- awake -- while another session or a call is live, and
    sleeping only once the brain reports nothing live. A brain that cannot say: sleeping,
    as before."""
    if await anything_live():
        face.state("idle")
    else:
        face.sleep()


async def call_live() -> bool:
    """Is a phone call up (the SIP brain's lease on /talk/call)? A brain that cannot
    say: no -- an asleep session it then refuses says so with YIELD_CLOSE_CODE."""
    status = await asyncio.to_thread(_talk_status_sync)
    return bool(status and status.get("call"))


async def wait_out_call() -> None:
    while await call_live():
        await asyncio.sleep(BACKOFF_POLL_SECS)


class Backoff:
    """Another Talk client took the box: it is theirs while the brain reports a live
    /talk session, and for BACKOFF_SECS after it last did. A brain that cannot say
    (down, restarting) counts as none: its sessions went with it."""

    def __init__(self, secs: float | None = None, clock=time.monotonic):
        self._secs = BACKOFF_SECS if secs is None else secs
        self._clock = clock
        self._last_active = clock()  # the taker was live at the eviction itself

    def over(self, active: bool | None) -> bool:
        now = self._clock()
        if active:
            self._last_active = now
        return now - self._last_active >= self._secs

    async def wait(self) -> None:
        while not self.over(await talk_active()):
            await asyncio.sleep(BACKOFF_POLL_SECS)


async def taken_at_start() -> bool:
    """Is the box another client's when this process starts? A Talk session still live
    after START_SETTLE_SECS (see there); a brain that cannot say: no."""
    deadline = time.monotonic() + START_SETTLE_SECS
    while await talk_active():
        if time.monotonic() >= deadline:
            return True
        await asyncio.sleep(START_POLL_SECS)
    return False


def close_code(e: BaseException | None) -> int | None:
    """The code the brain closed our socket with, if that is how the session ended."""
    import websockets

    if isinstance(e, websockets.ConnectionClosed) and e.rcvd is not None:
        return e.rcvd.code
    return None


def taken(e: BaseException | None) -> bool:
    """Did the brain end the session because another client took the slot?"""
    return close_code(e) == TAKEN_CLOSE_CODE


class Ungated(RuntimeError):
    """The brain did not say it gates the room (a brain older than wake words)."""


class KeepAlive:
    """An awake wake-word conversation: over once nobody (user or bot) has spoken for
    KEEPALIVE_SECS, or AWAKE_MAX_SECS after the last wake word with the bot quiet."""

    def __init__(self, secs: float | None = None, cap_secs: float | None = None,
                 clock=time.monotonic):
        self._secs = KEEPALIVE_SECS if secs is None else secs
        self._cap = AWAKE_MAX_SECS if cap_secs is None else cap_secs
        self._clock = clock
        self._last = self._woke = clock()

    def activity(self) -> None:
        self._last = self._clock()

    def woke(self) -> None:
        """A wake word (again): the cap counts from here."""
        self._last = self._woke = self._clock()

    def over(self, busy: bool = False) -> str | None:
        """"keepalive" or "awake-max" once it is over, else None. `busy`: the bot is
        still being heard -- that is speech, and no cap cuts a reply off."""
        now = self._clock()
        if busy:
            self._last = now
            return None
        if now - self._last >= self._secs:
            return "keepalive"
        if now - self._woke >= self._cap:
            return "awake-max"
        return None


# How often Restart looks at the speaker while it waits.
RESTART_POLL_SECS = 0.05


class Restart:
    """restart_session on this side: armed by the tool, it ends the session once the
    goodbye has been heard out.

    The goodbye is the model's reply to the tool result, so it is a NEW utterance
    (captions carry their TTS context id as "utterance", and are released as their words
    play). A line the model spoke before calling the tool, still playing when the result
    goes back, is not it: the goodbye can queue right behind that line, and ending at
    the gap between the two would cut it off. So: wait for an utterance that started
    after the restart was armed (up to RESTART_SPEECH_WAIT_SECS — the model may say
    nothing), then for the speaker to go quiet."""

    def __init__(self, play: "Playback", face: FaceTap, clock=time.monotonic):
        self._play, self._face, self._clock = play, face, clock
        self._seen: set = set()            # assistant utterances captioned so far
        self._before: set | None = None    # ... those seen when the restart was armed
        self.goodbye = False               # an utterance newer than that was captioned
        self.armed = asyncio.Event()
        # What the session ends for: "restart" (a fresh one at once) or "sleep"
        # (end_conversation: back to waiting for a wake word).
        self.kind = "restart"

    def arm(self, kind: str = "restart") -> None:
        if self._before is None:
            self._before = set(self._seen)
            self.kind = kind
            self.armed.set()

    def caption(self, utterance) -> None:
        """An assistant caption (partial or final) of `utterance` arrived."""
        if utterance is None:
            return
        self._seen.add(utterance)
        if self._before is not None and utterance not in self._before:
            self.goodbye = True

    def _heard_out(self) -> bool:
        play, face = self._play, self._face
        return not (play.speaking or face.voiced) and play.echo_free(self._clock())

    async def wait(self) -> None:
        await self.armed.wait()
        deadline = self._clock() + RESTART_SPEECH_WAIT_SECS
        while not self.goodbye and self._clock() < deadline:
            await asyncio.sleep(RESTART_POLL_SECS)
        while not self._heard_out():
            await asyncio.sleep(RESTART_POLL_SECS)


def client_tool(card: "Card", m: dict, restart: Restart) -> dict:
    """Perform one client_tool request from the brain (tools.py, THE TOOL CONTRACT);
    the result goes back as a tool_result for the model to speak around. Never raises:
    a request this bridge cannot carry out is an error result, not a dead session."""
    name, args = m.get("name"), m.get("args")
    try:
        if args is not None and not isinstance(args, dict):
            return {"ok": False, "error": f"{name}: args must be an object, not {args!r}"}
        args = args or {}
        if name == "set_volume":
            return card.volume.apply(args)
        if name == "restart_session":
            restart.arm()
            return {"ok": True, "note": "Now say one short goodbye; the session restarts "
                                        "with a fresh conversation once it has played."}
        if name == "end_conversation" and wake_mode():
            restart.arm("sleep")
            return {"ok": True, "note": "Now say one short goodbye; the box goes back to "
                                        "sleep once it has played."}
        return {"ok": False, "error": f"this device cannot do {name}"}
    except Exception as e:  # noqa: BLE001 — one bad request must not end the session
        logger.warning(f"client_tool {name}: {e!r}")
        return {"ok": False, "error": f"{name} failed on the device"}


async def run_session(card: Card, preroll: bool, wake: bool = False,
                      awake: bool = False) -> str | None:
    """One /talk connection over an open card. Raises DialError when the brain cannot be
    reached, CardError when the card dies; returns (or raises the socket's error) when
    the session ends — "restart" when the model asked for a fresh one, "taken" when
    another client took the slot, "busy" when the brain refused us (another
    conversation is live), "yield" when a phone call has the engine.

    `wake`: a wake-word session, opened asleep (or `awake`: restart_session's fresh
    conversation). It also ends "sleep" (end_conversation), "keepalive" / "awake-max"
    (KeepAlive) or "stt:<reason>" (its STT could not connect)."""
    import websockets

    face, play = card.face, card.play
    try:
        ws = await websockets.connect(_url(awake=wake and awake), max_size=None, open_timeout=10)
    except Exception as e:  # noqa: BLE001 — refused, timed out, 403: all "not reachable"
        raise DialError(repr(e)) from e
    logger.info(f"connected to {URL}")
    early = list(card.preroll) if preroll else []
    card.preroll.clear()
    card.sink = asyncio.Queue(maxsize=MIC_QUEUE_CHUNKS)
    # Wake mode: None while asleep, the conversation's KeepAlive once awake.
    alive = KeepAlive() if wake and awake else None
    # Wake mode: no mic audio until the brain's hello says it gates the room.
    gated = asyncio.Event()
    if not wake:
        gated.set()
    # Answers still owed (an async consult: the brain's "working"): call_id -> deadline.
    # The conversation does not sleep on one.
    owed: dict = {}
    if wake and awake:
        face.woke()

    async def send_mic():
        await gated.wait()
        # The words that woke us first (they are ~1 s behind; the STT takes them as a burst).
        for chunk in early:
            await ws.send(chunk)
        while True:
            await ws.send(await card.sink.get())

    async def receive():
        nonlocal alive
        async for msg in ws:
            if isinstance(msg, bytes):
                play.push(msg, voiced=face.voiced)
                face.audio()
                if alive is not None:
                    alive.activity()
                continue
            try:
                m = json.loads(msg)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            kind = m.get("type")
            if kind == "clear":
                play.clear()
                face.interrupted()
            elif kind == "ready":
                logger.info("session ready")
                if wake and alive is None:
                    # Our asleep session has the engine, so nothing else is live: the face
                    # may sleep now (not at the dial, which the brain may refuse).
                    face.sleep()
            elif kind == "hello" and wake:
                if m.get("wake") not in ("asleep", "awake"):
                    raise Ungated("the brain's hello does not say it gates the room")
                gated.set()
            elif kind == "working":
                if m.get("done"):
                    owed.pop(m.get("call_id"), None)
                else:
                    try:
                        secs = float(m.get("secs") or 0)
                    except (TypeError, ValueError):
                        secs = 0.0
                    owed[m.get("call_id")] = time.monotonic() + max(0.0, min(secs, 600.0))
            elif kind == "wake" and wake:
                # The brain's wake gate heard a wake phrase (wake_gate.py). Only its name
                # is logged, never what was said around it.
                if alive is None:
                    alive = KeepAlive()
                    face.woke()
                    logger.info(f'wake word "{m.get("phrase")}" heard — '
                                + ("continuing the conversation" if m.get("resumed")
                                   else "a new conversation"))
                else:
                    alive.woke()
                    logger.info(f'wake word "{m.get("phrase")}" heard again — '
                                f"awake for up to {AWAKE_MAX_SECS:g} s more")
            elif kind == "client_tool":
                result = client_tool(card, m, restart)
                await ws.send(json.dumps({"type": "tool_result", "call_id": m.get("call_id"),
                                          "result": result}))
            elif kind == "transcript":
                role, text, final = m.get("role"), m.get("text") or "", bool(m.get("final"))
                if role == "assistant" and text.startswith(DEBUG_CHIP_PREFIXES):
                    continue
                if alive is not None and (role == "assistant" or text.strip()):
                    alive.activity()
                if role == "assistant":
                    restart.caption(m.get("utterance"))
                if role == "user":
                    face.user(text, final)
                elif role == "assistant" and face.assistant(text, final):
                    play.mark_voiced()
                if final:
                    logger.info(f"{role}: {text}")

    restart = Restart(play, face)

    async def restart_when_heard_out():
        await restart.wait()
        logger.info("going to sleep, as asked — ending this session"
                    if restart.kind == "sleep" else
                    "restart requested — ending this session for a fresh one")

    async def keep_alive():
        # Awake wake-word conversations only: their end (see KeepAlive). And the hello's
        # deadline: a brain that has not said it gates the room is left.
        opened = time.monotonic()
        while True:
            await asyncio.sleep(KEEPALIVE_POLL_SECS)
            now = time.monotonic()
            if not gated.is_set() and now - opened >= HELLO_SECS:
                raise Ungated(f"no hello from the brain in {HELLO_SECS:g} s")
            if alive is None:
                continue
            waiting = any(deadline > now for deadline in owed.values())
            why = alive.over(busy=play.speaking or face.voiced or waiting
                             or not play.echo_free(now))
            if why:
                logger.info(f"no speech for {KEEPALIVE_SECS:g} s — the conversation sleeps"
                            if why == "keepalive" else
                            f"{AWAKE_MAX_SECS:g} s since the last wake word — the "
                            "conversation sleeps")
                return why

    restarter = asyncio.create_task(restart_when_heard_out())
    tasks = [asyncio.create_task(send_mic()), asyncio.create_task(receive()), restarter]
    sleeper = asyncio.create_task(keep_alive()) if wake else None
    if sleeper is not None:
        tasks.append(sleeper)
    try:
        done, _ = await asyncio.wait([*tasks, card.failed], return_when=asyncio.FIRST_COMPLETED)
        card._check()
        codes = [close_code(t.exception()) for t in done if not t.cancelled()]
        if TAKEN_CLOSE_CODE in codes:
            return "taken"
        if BUSY_CLOSE_CODE in codes:
            return "busy"
        if YIELD_CLOSE_CODE in codes or CALL_CLOSE_CODE in codes:
            return "yield"
        if wake:
            if any(isinstance(t.exception(), Ungated) for t in done if not t.cancelled()):
                return "ungated"
            for t in done:
                e = None if t.cancelled() else t.exception()
                if close_code(e) == STT_CLOSE_CODE:
                    return f"stt:{e.rcvd.reason or 'unavailable'}"
            if sleeper in done:
                return sleeper.result()
        for t in done:
            t.result()
        if restarter in done:
            return "sleep" if restart.kind == "sleep" else "restart"
        return None
    finally:
        card.sink = None
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ws.close()
        face.ended()


class WakeState:
    """What a wake-word bridge carries from one session (and one card) to the next."""

    def __init__(self, backoff: "Backoff | None" = None):
        self.backoff = backoff     # another Talk client has the box (#95)
        self.restart = False       # restart_session: the next dial is awake -- ONCE
        self.wait = RETRY_SECS     # backoff for a brain or STT that will not take us


async def run_wake_sessions(card: Card, st: WakeState) -> None:
    """Wake words: an asleep session on the open card, again and again; forever, or
    until the card fails (CardError). Fail closed: whatever goes wrong, the bridge is
    deaf until a session is up again -- it never listens without its wake words."""
    face = card.face
    while True:
        if st.backoff is not None:
            await card._until(st.backoff.wait())
            st.backoff = None
            logger.info(f"no other Talk session for {BACKOFF_SECS:g} s — back-off over, "
                        "asleep again")
        # One-shot: whatever this dial does -- a session, a DialError, a CardError --
        # the next one is asleep, so nothing reopens the room without a wake word.
        awake, st.restart = st.restart, False
        opened = time.monotonic()
        try:
            ended = await run_session(card, preroll=False, wake=True, awake=awake)
        except DialError as e:
            logger.warning(f"brain not reachable ({e}) — not listening; retrying in {st.wait:g} s")
            face.sleep()  # a brain that cannot be reached: nothing to say otherwise
            await card.idle_for(st.wait)
            st.wait = min(st.wait * 2, RETRY_MAX_SECS)
            continue
        except CardError:
            raise
        except Exception as e:  # noqa: BLE001 — the socket broke: as good as ended
            logger.warning(f"session ended ({e!r})")
            ended = None
        if ended in ("yield", "taken", "busy"):
            # Another session or a call has the box: awake (idle) while it lasts; the
            # face sleeps once our next asleep session is up (run_session, "ready").
            face.state("idle")
        else:
            await rest_face(face)
        if ended == "restart":
            st.restart = True
            st.wait = RETRY_SECS
            continue
        if ended in ("taken", "busy") and BACKOFF_SECS > 0:
            st.backoff = Backoff()
            logger.info(("session taken by another Talk client" if ended == "taken" else
                         "refused: another conversation has the box")
                        + " — backing off (asleep, no wake) until no session has been live "
                        f"for {BACKOFF_SECS:g} s")
            continue
        if ended == "busy":
            # No back-off configured, but a refusal is not to be redialled at once: the
            # brain would only refuse again.
            logger.info(f"refused: another conversation has the box — retrying in {st.wait:g} s")
            await card.idle_for(st.wait)
            st.wait = min(st.wait * 2, RETRY_MAX_SECS)
            continue
        if ended == "yield":
            logger.info("a phone call needs the speech engine — not listening until it ends")
            await card._until(wait_out_call())
            logger.info("the phone call is over — asleep again")
            continue
        if ended == "ungated":
            logger.error("the brain does not gate wake words (a release older than this "
                         "bridge?) — not listening, so the room is not sent to it; retrying "
                         f"in {st.wait:g} s")
            await card.idle_for(st.wait)
            st.wait = min(st.wait * 2, RETRY_MAX_SECS)
            continue
        if ended and ended.startswith("stt:"):
            logger.warning(f"speech recognition {ended[4:]} — not listening; retrying in "
                           f"{st.wait:g} s")
            await card.idle_for(st.wait)
            st.wait = min(st.wait * 2, RETRY_MAX_SECS)
            continue
        if ended in ("sleep", "keepalive", "awake-max"):
            st.wait = RETRY_SECS
            continue
        # The brain ended it (a restart of its own, an error): open the next one, but
        # not in a tight loop against a brain that keeps refusing.
        if time.monotonic() - opened < SHORT_SESSION_SECS:
            logger.warning(f"session ended at once — retrying in {st.wait:g} s")
            await card.idle_for(st.wait)
            st.wait = min(st.wait * 2, RETRY_MAX_SECS)
        else:
            st.wait = RETRY_SECS


async def run_bridge() -> None:
    """Open the card, dial, and after every session wait for a voice (or, with wake
    words, hold an asleep session: run_wake_sessions); forever."""
    face = FaceTap()
    face.state("idle")
    volume = Volume()
    logger.info(f"volume: {volume.percent}%")
    connect_now = True  # the first session dials at once (and is greeted)
    backoff = None  # another client took the box: a Backoff to wait out (kept across card reopens)
    card_wait = dial_wait = RETRY_SECS
    if BACKOFF_SECS > 0 and await taken_at_start():
        # A restart (a crash, the config page) must not evict a session in use either.
        connect_now, backoff = False, Backoff()
        logger.info("a Talk session is live at start — backing off (no voice wake) until "
                    f"no Talk session has been live for {BACKOFF_SECS:g} s")
    wake = WakeState(backoff) if wake_mode() else None
    if wake is not None:
        phrases = wake_phrases(WAKE_WORDS)
        logger.info(f"wake words: {', '.join(phrases) or 'NONE usable'} (keep-alive "
                    f"{KEEPALIVE_SECS:g} s, awake at most {AWAKE_MAX_SECS:g} s, "
                    f"conversation kept {CONVERSATION_SECS:g} s)")
        if not phrases:
            logger.warning(f"LOCAL_AUDIO_WAKE_WORDS={WAKE_WORDS!r} holds no word to listen "
                           "for: the box will never wake (clear it to listen without one)")
        await rest_face(face)
    try:
        while True:
            card = Card(face, volume=volume)
            try:
                await card.open()
                logger.info(f"card ready: {card.device}")
                if wake is not None:
                    await run_wake_sessions(card, wake)
                while True:
                    if backoff is not None:
                        await card._until(backoff.wait())
                        backoff = None
                        logger.info(f"no other Talk session for {BACKOFF_SECS:g} s — "
                                    "back-off over, waiting for voice")
                    if not connect_now:
                        await card.wait_for_voice()
                        logger.info("voice detected — connecting")
                    try:
                        ended = await run_session(card, preroll=not connect_now)
                        if ended == "restart":
                            # Dial straight back: a fresh context and a greeting, no voice wait.
                            connect_now = True
                            card_wait = dial_wait = RETRY_SECS
                            continue
                        if ended in ("taken", "busy") and BACKOFF_SECS > 0:
                            backoff = Backoff()
                            logger.info(("session taken by another Talk client"
                                         if ended == "taken" else
                                         "refused: another conversation has the box")
                                        + " — backing off (no voice wake) until no session "
                                        f"has been live for {BACKOFF_SECS:g} s")
                        elif ended == "yield":
                            logger.info("a phone call has the speech engine — not listening "
                                        "until it ends")
                            await card._until(wait_out_call())
                            logger.info("the phone call is over — waiting for voice")
                        else:
                            logger.info("session ended — waiting for voice")
                    except DialError as e:
                        if connect_now:
                            logger.warning(f"brain not reachable ({e}) — retrying in {dial_wait:g} s")
                            await card.idle_for(dial_wait)
                            dial_wait = min(dial_wait * 2, RETRY_MAX_SECS)
                        else:
                            logger.warning(f"brain not reachable ({e}) — waiting for voice")
                        continue
                    except CardError:
                        raise
                    except Exception as e:  # noqa: BLE001 — the socket broke: as good as ended
                        logger.warning(f"session ended ({e!r}) — waiting for voice")
                    connect_now = False
                    card_wait = dial_wait = RETRY_SECS  # a good session
            except CardError as e:
                logger.warning(f"sound card {card.device}: {e} — retrying in {card_wait:g} s "
                               "(sooner if it is unplugged and plugged back in)")
            finally:
                await card.close()
            if await wait_for_card(card.device, card_wait):
                logger.info(f"sound card {card.device} plugged in — retrying now")
                card_wait = RETRY_SECS
            else:
                card_wait = min(card_wait * 2, RETRY_MAX_SECS)
    finally:
        face.ended()


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, stop.set)
    wake = "wake words" if wake_mode() else f"wake={WAKE_DB:g} dBFS"
    logger.info(f"local audio bridge: device={DEVICE} channel={CAPTURE_CHANNEL} {wake} url={URL}")
    bridge = asyncio.create_task(run_bridge())
    stopper = asyncio.create_task(stop.wait())
    await asyncio.wait({bridge, stopper}, return_when=asyncio.FIRST_COMPLETED)
    stopper.cancel()
    if not bridge.done():
        bridge.cancel()
    try:
        await bridge
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    asyncio.run(main())
