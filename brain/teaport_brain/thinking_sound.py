#
# thinking_sound.py — "assistant is thinking" comfort audio for long agent consults.
#
# During an ask_openclaw consult the brain hands off to the full desktop agent and
# waits ~15-35 s, sometimes minutes, for the answer (web search + fetch + reasoning).
# This processor types through it: from the consult's start until its answer is in, a
# typist types, heard whenever the line is quiet — nobody has spoken, and no reply has
# been on its way, for TEAPORT_THINKING_GRACE_S — and for at most TEAPORT_THINKING_MAX_S
# of each consult. Quick tools (time, host status, web_search) never type: only the
# tools in _TRIGGER, which is just ask_openclaw. Phone calls get no typing (agent_session
# builds none for SIP) -- but in agent-first, where it is the only cover for the wait
# (no "I'll work on that", no progress lines). Nor while the session is held for one.
#
# A consult's start and end. A background consult (the default: the turn ends at once
# on a placeholder, owed.py) is the brain's {"type": "working"} message to the client,
# sent when it starts and when its outcome is in (tools._consult_and_land). An
# agent-first consult (TEAPORT_AGENT_FIRST: the turn waits for it) is its
# FunctionCallsStartedFrame and FunctionCallResultFrame, or a barge-in, which cancels
# it. Neither is keyed on reply audio: ask_openclaw speaks a preamble ("I'll work on
# that…") right after the call starts, and the typing just waits for quiet again.
#
# Placed just before transport.output(): it sees all of that coming DOWN (and the
# transport's Bot{Started,Stopped}SpeakingFrames coming up), and injects its own audio
# DOWNSTREAM as plain OutputAudioRawFrames, which the transport plays without calling
# them bot speech: nothing that waits for the bot to finish (barge-in, the door the
# brain's own runs wait at, the ledger) takes the typing for a reply. A client that
# types along ("typing" in its ?features=: the room's bridge, for the OLED face) is
# also told when a consult starts and stops typing ({"type": "typing"}) and each key
# press ahead of its click ({"type": "keys"}): pinned to the typing's audio, or, while
# the typing is not heard, to the message itself — the face types while it talks.
#
# The bed is typed live (royalty-free, no external asset): a keyboard of modal keys is
# built once when the brain starts (warm()), and each bed gets a typist that schedules
# its keystrokes as it plays and renders each one on a worker thread, so the typing
# never repeats and every key press is known before it is heard. The brain holds
# ~0.8 MB more for it, numpy.fft's import included (the 15 s loop it replaced: ~5 MB,
# most of it numpy.random's import, which this never touches). Point
# TEAPORT_THINKING_WAV at your own 24 kHz mono 16-bit wav to loop that instead. Kill
# switch: TEAPORT_THINKING_SOUND=0.
#
import asyncio
import math
import random
import time
import wave
from itertools import accumulate
from typing import NamedTuple

import numpy as np
from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.followup_gate import RunSilentFrame
from teaport_brain.settings import setting

ENABLED = setting("TEAPORT_THINKING_SOUND")

_SR = 24000                 # engine TTS rate; the relay is 24 kHz both ways
_CHUNK_MS = 40
_CHUNK = _SR * _CHUNK_MS // 1000                  # samples per chunk
_GRACE_S = setting("TEAPORT_THINKING_GRACE_S")   # quiet on the line before typing is heard
_GAIN = setting("TEAPORT_THINKING_GAIN")         # 1.0 = the typing's limited peak
_MAX_S = setting("TEAPORT_THINKING_MAX_S")        # most typing heard per consult
_WAV = setting("TEAPORT_THINKING_WAV")            # "" = live typing
# Only genuinely-long tool calls type; the fast tools resolve before the grace period
# even elapses, so typing there would just be a blip.
_TRIGGER = {"ask_openclaw"}
# A consult's typing never outlives this, whatever its end (its answer is due within
# TEAPORT_ASYNC_CONSULT_TIMEOUT, and the brain says so when it is in).
_TYPING_MAX_S = 600.0
# A reply is on its way from its LLMFullResponseStartFrame until its End -- for at most
# this long, to cover the model's and the TTS's start before its first audio (which,
# with the transport's BotStarted, speaks for it after). A response with no words of
# its own has its End held at the TTS, and its Start, held there too, can reach us
# after its own tool call (a system frame): so no frame order leaves it stuck.
_REPLY_WAIT_S = 3.0

# The keyboard, modelled on a close-miked clicky mechanical keyboard (an ASMR typing
# recording, measured, not sampled). A key is a physical object, so it is built as
# one: a set of damped sinusoids (modes) that ring the same way every time it is
# struck. In the recording, presses of the same key are near-identical (0-12 ms
# waveforms correlate ~0.9), and that repeatable ring is what reads as a plastic shell
# rather than noise. Two kinds of sound per key. The SHELL is the keycap and housing:
# modes at 0.6-3.4 kHz (most near 1.1-1.7 and 2.1-3.4 kHz) that die in 1-4 ms, over
# the case's ~870 Hz ring. The CLICK is the switch: short modes at 2-12 kHz, the
# brightest thing in the recording, that also set the shell ringing a little and a
# third of the time strike twice. Each key draws its modes from the recording's
# per-band statistics: (lo Hz, hi Hz, modes per key, level, decay ms). A key's lead
# mode (level 1) comes from one of the bands in the *_LEAD tuple.
_SHELL_BANDS = ((600, 800, 0.4, (0.3, 0.6), (1.3, 1.95)),
                (800, 950, 0.5, (0.3, 0.7), (1.9, 3.75)),
                (950, 1250, 1.0, (0.3, 1.0), (1.35, 2.6)),
                (1250, 1700, 1.7, (0.25, 0.6), (1.5, 2.4)),
                (1700, 2100, 0.15, (0.3, 0.6), (0.9, 1.65)),
                (2100, 2700, 1.4, (0.25, 0.6), (1.1, 2.25)),
                (2700, 3400, 1.2, (0.3, 0.7), (0.75, 1.5)),
                (3400, 4500, 0.4, (0.3, 0.6), (0.5, 0.9)),
                (4500, 8000, 0.15, (0.4, 0.6), (0.3, 0.45)),
                (8000, 11500, 0.15, (0.35, 0.65), (0.3, 0.5)))
_SHELL_LEAD = (1, 2, 2, 3)                          # 0.8-1.7 kHz, mostly ~1.1
_CLICK_BANDS = ((950, 1700, 0.6, (0.2, 0.5), (1.3, 3.0)),
                (2100, 2700, 0.65, (0.15, 0.5), (0.7, 1.7)),
                (2700, 3400, 0.9, (0.2, 0.7), (0.7, 1.7)),
                (3400, 4500, 0.65, (0.3, 0.8), (0.6, 1.2)),
                (4500, 8000, 2.5, (0.5, 1.2), (0.5, 1.0)),
                (8000, 11500, 2.0, (0.6, 1.3), (0.3, 0.8)))
_CLICK_LEAD = (1, 2, 3, 5, 5)
_CASE = (870, 0.004, 0.3)                           # under every key: Hz, decay s, level
# The twenty keys, most used first, by the letters they stand for (English letter
# frequency): who reads the key presses (the OLED face, through the room's bridge)
# maps a letter to the finger that types it. The space bar is "space".
_LETTERS = "etaoinshrdlucmfwypvb"

_STRIKE = int(_SR * 0.04)                           # a strike's longest span
_NORM = int(_SR * 0.004)                            # a strike is unit RMS over its first 4 ms
_GRIT = int(_SR * 0.002)                            # the click's grit: 13 of its 0.15 ms decays
_THOCK_NOISE = int(_SR * 0.008)                     # a thock's noise: 10 of its 0.8 ms decays
_T = np.arange(_STRIKE) / _SR
_TT = np.arange(int(_SR * 0.08)) / _SR              # the desk thump's span
_GRIT_ENV = np.exp(-_T[:_GRIT] / 0.00015)
_THOCK_ENV = np.exp(-_T[:_THOCK_NOISE] / 0.0008)
_THUMP_ENV = np.exp(-_TT / 0.012) * (1 - np.exp(-_TT / 0.001))
_LEAD = int(_SR * 0.05)          # a keystroke's landing comes up to 45 ms before its click
_SPAN = int(_SR * 0.2)           # landing, click, bottom-out, bounce, release, rattle
_FFT = 8192                      # one keystroke's span and the room's 0.12 s tail
_RING = 17 * _CHUNK              # rendered audio not yet played: 0.68 s, whole chunks
_AHEAD = int(_SR * 0.15)         # keystrokes are rendered this far ahead of the chunk
# The typing's level before the limiter: ~-34.8 dBFS RMS, as loud as the bed it replaced
# (so TEAPORT_THINKING_GAIN keeps its level). Measured over 10 min of this keyboard.
_LEVEL = 0.0405


def _poisson(rng: random.Random, lam: float) -> int:
    """A Poisson draw (Knuth's: lam is at most a few here)."""
    limit, k, p = math.exp(-lam), 0, rng.random()
    while p > limit:
        k += 1
        p *= rng.random()
    return k


class _Bank(NamedTuple):
    """One way a key rings: its modes as poles (one step of each damped phasor), their
    levels and phases, how many samples it lasts, and the impact's rise."""
    pole: np.ndarray
    level: np.ndarray
    phase: np.ndarray
    span: int
    rise: np.ndarray


class _Key(NamedTuple):
    letter: str
    hits: tuple          # the shell as rung by the landing, the bottom-out, the release
    click: _Bank
    thump: tuple         # the bottom-out's desk thump: Hz, level


class _Keyboard:
    """The twenty keys and the space bar, the noise the strikes draw from, and the room;
    built once (warm()) from a fixed seed, so it is always the same keyboard. Read-only
    after that: every bed's typist shares it.

    Keyboards drawn from these statistics differ in how often a keystroke's click
    outshines its thock; seed 49 is the one of 200 closest to the bed that passed the
    listening test (keystrokes with only a bright click / only a dull thock: 17% / 29%,
    against 17% / 26% there and 17-26% / 23-32% in the recording)."""

    def __init__(self, seed: int = 49):
        rng = random.Random(seed)
        # Gaussian noise the typists slice from (strike jitter, grit, thock noise)
        self.noise = np.array([rng.gauss(0, 1) for _ in range(4096)])
        self.keys = [self._key(rng, letter) for letter in _LETTERS]
        self.space = self._key(rng, "space", space=True)
        usage = [1 / (i + 1) ** 0.8 for i in range(len(self.keys))]   # some keys far more
        self.usage = list(accumulate(usage))
        # The room: a short, dark tail (0.12 s, under 3 kHz, -18 dB). No reverb wash:
        # the recording's gaps are dark, and a long tail blurs the clicks into hiss.
        m = int(_SR * 0.12)
        tail = self.noise[:m] * np.exp(-6.9 * np.arange(m) / m)
        fm = np.fft.rfftfreq(m, 1 / _SR)
        tail = np.fft.irfft(np.fft.rfft(tail) / np.sqrt(1 + (fm / 3000) ** 4), m)
        room = tail * 10 ** (-18 / 20) / np.sqrt(np.sum(tail ** 2))
        room[0] = 1.0
        self.room = np.fft.rfft(room, _FFT).astype(np.complex64)

    @staticmethod
    def _modes(rng, bands, lead, scale=1.0):
        """A key's modes [(Hz, decay s, level)], drawn band by band."""
        j0 = lead[rng.randrange(len(lead))]
        ms = []
        for j, (lo, hi, n, level, decay) in enumerate(bands):
            for c in range(max(_poisson(rng, n), j == j0)):
                hz = math.exp(rng.uniform(math.log(lo), math.log(hi))) * scale
                ms.append((hz, rng.uniform(*decay) / 1000,
                           1.0 if j == j0 and c == 0 else rng.uniform(*level)))
        return ms

    @staticmethod
    def _bank(rng, ms, rise) -> _Bank:
        hz, decay, level = (np.array(v) for v in zip(*ms))
        # Long enough for the slowest mode to die ~45 dB, never under the 4 ms norm
        span = min(_STRIKE, max(_NORM, int(5.2 * decay.max() * _SR) + 1))
        return _Bank(np.exp(-1 / (decay * _SR) + 2j * np.pi * hz / _SR), level,
                     np.array([rng.uniform(0, 2 * math.pi) for _ in ms]), span,
                     1 - np.exp(-_T[:int(7 * rise * _SR) + 1] / rise))

    def _key(self, rng, letter, space=False) -> _Key:
        """One key: its shell rung three ways, its click, its desk thump. The space bar
        is lower and rings longer."""
        shell = [(hz, decay * (1.5 if space else 1.0), level)
                 for hz, decay, level in self._modes(rng, _SHELL_BANDS, _SHELL_LEAD,
                                                     0.65 if space else 1.0)]
        hits = tuple(self._bank(rng, [(hz, decay, level * rng.uniform(0.5, 1.0))
                                      for hz, decay, level in shell] + [_CASE], 0.00015)
                     for _ in range(3))
        click = self._bank(rng, self._modes(rng, _CLICK_BANDS, _CLICK_LEAD,
                                            0.93 if space else 1.0), 0.00003)
        thump = (rng.uniform(44, 58) * (0.9 if space else 1.0), 0.28 if space else 0.18)
        return _Key(letter, hits, click, thump)


class _Typist:
    """One bed's typing, generated as it plays: steady touch typing at ~8 keys/s, keys
    used as unevenly as letters, a space bar every few, a rare pause between words.

    A keystroke: the finger lands (the shell, softly), the switch clicks, the key
    bottoms out 4-12 ms later (the shell over a ~50 Hz desk thump), bounces, and on the
    release rings the shell again, often with a softer click; landing, bottom-out and
    release each strike the shell in their own way. The clicks come at a steady level
    and the thocks vary widely, as in the recording. Each keystroke is rendered whole
    when it is scheduled, through the room, into a ring that chunks are read from; its
    click's place is its key press. A seed makes the typing reproducible (tests);
    None types differently every time."""

    def __init__(self, keyboard: _Keyboard, seed: int | None = None):
        self.kb = keyboard
        self.rng = random.Random(seed)
        self.ring = np.zeros(_RING, dtype=np.float32)
        self.read = 0                       # the next chunk's first sample (absolute)
        self.next_key = _LEAD               # the next keystroke's click (absolute)
        self.to_space = self.rng.randint(4, 6)

    def _noise(self, n: int) -> np.ndarray:
        i = self.rng.randrange(len(self.kb.noise) - n)
        return self.kb.noise[i:i + n]

    def strike(self, bank: _Bank) -> np.ndarray:
        """One strike of a bank, at unit level. Each mode comes out a little louder or
        softer and a little shifted in phase from strike to strike, as no two presses
        of a real key are identical. Each mode is a damped phasor stepped by its pole
        (a running product), so no exp or sin is evaluated per sample."""
        k, n = len(bank.pole), bank.span
        z = np.empty((k, n), complex)
        z[:, 0] = (bank.level * np.maximum(0.1, 1 + 0.1 * self._noise(k))
                   * np.exp(1j * (bank.phase + 0.15 * self._noise(k))))
        z[:, 1:] = bank.pole[:, None]
        s = np.zeros(_STRIKE)
        s[:n] = np.cumprod(z, axis=1).imag.sum(0)
        s[:len(bank.rise)] *= bank.rise
        return s / math.sqrt(np.dot(s[:_NORM], s[:_NORM]) / _NORM)

    def click(self, key: _Key) -> np.ndarray:
        rng = self.rng
        s = self.strike(key.click) + 0.2 * self.strike(key.hits[1])   # the snap shakes the keycap too
        if rng.random() < 0.35:                                       # and the switch strikes twice
            d = int(rng.uniform(0.0006, 0.0015) * _SR)
            s[d:] += 10 ** (rng.gauss(-4, 2) / 20) * self.strike(key.click)[:_STRIKE - d]
        grit = np.diff(self._noise(_GRIT) * _GRIT_ENV, prepend=0)
        s[:_GRIT] += 0.3 * grit / math.sqrt(np.dot(grit[:24], grit[:24]) / 24)
        return 10 ** (2 / 20) * s

    def thock(self, key: _Key, hit: int, bottom_out: bool = False) -> np.ndarray:
        """The key's shell, rung by the landing (0), the bottom-out (1) or the release (2);
        the bottom-out lands on the desk too. (The bounce rings hit 1 without the thump.)"""
        s = self.strike(key.hits[hit])
        s[:_THOCK_NOISE] += 0.15 * self._noise(_THOCK_NOISE) * _THOCK_ENV
        if not bottom_out:
            return s
        hz, level = key.thump
        out = level * _THUMP_ENV * (np.sin(2 * np.pi * hz * _TT) + 0.2 * np.sin(2 * np.pi * 2.4 * hz * _TT))
        out[:_STRIKE] += s
        return out

    def keystroke(self, render: bool = True) -> tuple[int, str]:
        """The next keystroke, rendered into the ring (unless `render` is off: typing
        that is not heard); its click's sample and letter."""
        rng, kb = self.rng, self.kb
        self.to_space -= 1
        if self.to_space == 0:
            key, self.to_space = kb.space, rng.randint(4, 6)
        else:
            key = kb.keys[rng.choices(range(len(kb.keys)), cum_weights=kb.usage)[0]]
        if render:
            self._render(key)
        pressed = (self.next_key, key.letter)
        gap = min(max(0.115 * math.exp(rng.gauss(0, 0.35)), 0.045), 0.26)
        if rng.random() < 0.035:
            gap += rng.uniform(0.15, 0.45)                                               # between words
        self.next_key += int(gap * _SR)
        return pressed

    def _render(self, key: _Key) -> None:
        rng = self.rng
        buf = np.zeros(_SPAN)

        def add(s, at, db):
            i = _LEAD + int(at * _SR)
            buf[i:i + len(s)] += s[:_SPAN - i] * 10 ** (db / 20)

        db = rng.gauss(0, 2)
        if rng.random() < 0.8:
            add(self.thock(key, 0), -rng.uniform(0.004, 0.045), db + rng.gauss(-11, 5))   # landing
        add(self.click(key), 0, db + rng.gauss(0, 1.5))
        add(self.thock(key, 1, bottom_out=True), rng.uniform(0.004, 0.012), db + rng.gauss(-2, 7))
        if rng.random() < 0.8:
            add(self.thock(key, 1), rng.uniform(0.013, 0.025), db + rng.gauss(-7, 5))     # bounce
        if rng.random() < 0.8:
            at = rng.uniform(0.05, 0.11)
            add(self.thock(key, 2), at, db + rng.gauss(-8, 5))                           # release
            if rng.random() < 0.6:
                add(self.click(key), at - 0.004, db + rng.gauss(-7, 3))
        for _ in range(_poisson(rng, 2)):                                                # rattle
            add(self.thock(key, rng.randrange(3)), rng.uniform(-0.03, 0.08), db + rng.gauss(-20, 5))
        wet = np.fft.irfft(np.fft.rfft(buf, _FFT) * self.kb.room, _FFT)
        i = (self.next_key - _LEAD) % _RING
        n = min(_FFT, _RING - i)
        self.ring[i:i + n] += wet[:n]
        self.ring[:_FFT - n] += wet[n:]                                                  # the ring wraps

    def chunk(self, heard: bool = True) -> tuple[bytes | None, list]:
        """The next 40 ms of typing (int16 PCM; None when not `heard`: the keystrokes
        it schedules are not rendered) and the key presses scheduled for it:
        [(sample from this chunk's start, letter)], up to _AHEAD (+ the landing's lead)
        past its end. At most one keystroke beyond what this chunk needs, so the render
        cost stays near one keystroke per chunk."""
        pressed = []
        while self.next_key - _LEAD < self.read + _CHUNK:                # this chunk needs it
            pressed.append(self.keystroke(heard))
        if self.next_key - _LEAD < self.read + _CHUNK + _AHEAD:          # one ahead
            pressed.append(self.keystroke(heard))
        i = self.read % _RING
        x = self.ring[i:i + _CHUNK].copy()
        self.ring[i:i + _CHUNK] = 0
        pressed = [(at - self.read, letter) for at, letter in pressed]
        self.read += _CHUNK
        if not heard:
            return None, pressed
        out = 0.5 * np.tanh(x * (_LEVEL / 0.5)) * (32767 * _GAIN)   # stacked clicks: held under -6 dBFS
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes(), pressed


def _load_override() -> np.ndarray | None:
    """The TEAPORT_THINKING_WAV override if one is set and playable, else None (live
    typing). Never raises — warm() runs this at the brain's start, where an exception
    would crash-loop the service over one bad path — and never writes: the override
    is the user's own file."""
    if _WAV:
        try:
            with wave.open(_WAV, "rb") as w:
                playable = (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (_SR, 1, 2)
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16) if playable else None
            if pcm is None:
                logger.warning(f"thinking_sound: {_WAV} is not 24 kHz mono 16-bit; "
                               "playing live typing")
            elif len(pcm) < _CHUNK:                         # not one chunk: no bed at all
                logger.warning(f"thinking_sound: {_WAV} is shorter than {_CHUNK_MS} ms; "
                               "playing live typing")
            else:
                return pcm
        except Exception as e:  # noqa: BLE001 — wave raises more than wave.Error: OSError,
            # EOFError, ValueError (a torn file), RuntimeError (a chunk past the RIFF end)
            logger.warning(f"thinking_sound: can't read {_WAV} ({e!r}); playing live typing")
    return None


# What the beds play, built once, process-wide: the override's chunks (a loop every
# session shares), or the keyboard every bed's typist types on.
_LOOP: list | None = None
_KEYBOARD: _Keyboard | None = None


def _prepare() -> None:
    global _LOOP, _KEYBOARD
    if _LOOP is not None or _KEYBOARD is not None:
        return
    pcm = _load_override()
    if pcm is None:
        _KEYBOARD = _Keyboard()
        return
    pcm = np.clip(pcm.astype(np.float32) * _GAIN, -32768, 32767).astype(np.int16).tobytes()
    n = _CHUNK * 2                          # bytes per chunk (int16 mono)
    # Every whole chunk, the last included (a partial tail chunk is dropped).
    _LOOP = [pcm[i:i + n] for i in range(0, len(pcm) - n + 1, n)]


def warm() -> None:
    """Build the keyboard (or read the override) at the brain's start, not in the first
    session's pipeline build."""
    if ENABLED:
        _prepare()


class ThinkingSound(FrameProcessor):
    """Types while a long consult runs, so the wait isn't dead air. See the module
    comment for what starts and ends it, when it is heard, and where it sits.

    consults_async: the session's consults end the turn at once (owed.py) and say when
    they start and end with {"type": "working"}; else agent-first, by the call's own
    frames. keys: the client types along ("typing" in its ?features=). gate: the
    session's FollowupGate, whose hold (a phone call has the box) keeps the typing
    unheard."""

    def __init__(self, consults_async: bool = False, keys: bool = False, gate=None):
        super().__init__()
        _prepare()
        self._async = consults_async
        # (a loop of the user's own wav has no key presses to type along to)
        self._keys = keys and _LOOP is None
        self._gate = gate
        self._consults = set()      # background consults typing (their call_ids)
        self._task = None           # the typing, while a consult runs
        self._heard = 0             # chunks heard this consult
        # The line, as this place in the pipeline sees it: who is talking, and when
        # anyone last was (or a reply was on its way).
        self._user = self._bot = False
        self._reply_t = None        # the open reply's LLMFullResponseStart (time.monotonic())
        self._spoke = 0.0

    def _quiet(self) -> bool:
        """Nobody talking, no reply on its way, for the grace; and not on hold. Its own
        view of the line, not the FollowupGate's: that one is whether a run of ours may
        start (its LLM flag held up by an End held at the TTS), this one is whether
        anything is audible or about to be, from below the TTS (its start and audio)."""
        now = time.monotonic()
        replying = self._reply_t is not None and now - self._reply_t < _REPLY_WAIT_S
        held = self._gate is not None and self._gate.held
        return (not (self._user or self._bot or replying or held)
                and now - self._spoke >= _GRACE_S)

    async def _say(self, message: dict, urgent: bool = False):
        if self._keys:
            frame = (OutputTransportMessageUrgentFrame if urgent else OutputTransportMessageFrame)
            await self.push_frame(frame(message=message), FrameDirection.DOWNSTREAM)

    async def _start(self, why: str):
        if self._task is not None:
            return
        self._heard = 0
        self._spoke = time.monotonic()      # the consult's own turn is talking about it
        await self._say({"type": "typing", "on": True, "first_key_ms": round(_GRACE_S * 1000)},
                        urgent=True)
        self._task = self.create_task(self._run())
        logger.info(f"thinking_sound: typing ({why}); heard after {_GRACE_S}s of quiet")

    async def _stop(self, why: str, done: bool = False):
        if self._task is None:
            return
        task, self._task = self._task, None
        await self.cancel_task(task)
        self._consults.clear()
        await self._say({"type": "typing", "on": False, "done": done}, urgent=True)
        logger.info(f"thinking_sound: typing stopped ({why}); "
                    f"{self._heard * _CHUNK_MS / 1000:.1f}s heard")

    async def _run(self):
        await asyncio.sleep(_GRACE_S)
        typist = _Typist(_KEYBOARD) if _LOOP is None else None
        loop = asyncio.get_running_loop()
        started = due = loop.time()
        i = 0
        while loop.time() - started < _TYPING_MAX_S:
            heard = self._quiet() and self._heard * _CHUNK_MS / 1000 < _MAX_S
            chunk, pressed = None, []
            if typist is None:
                chunk = _LOOP[i % len(_LOOP)] if heard else None
            elif heard or self._keys:
                # A keystroke takes a few ms to render: off the event loop. (Not heard,
                # with no face typing along, there is nothing to do.)
                chunk, pressed = await asyncio.to_thread(typist.chunk, heard)
            # Asked again: a line that began during the render must not get a chunk of
            # typing pushed in behind its first audio.
            heard = heard and self._quiet()
            if pressed:
                # Ahead of the audio that carries their clicks, pinned to it. Unheard,
                # pinned to this message, and urgent: queued behind a reply's audio
                # they would all arrive at its end (the face types on while it talks).
                await self._say({"type": "keys", "anchor": "audio" if heard else "now",
                                 "keys": [[letter, round(at * 1000 / _SR)] for at, letter in pressed]},
                                urgent=not heard)
            if heard:
                # Not a TTSAudioRawFrame: the transport plays it without calling it bot
                # speech, so nothing waiting for the bot to finish waits on the typing.
                await self.push_frame(OutputAudioRawFrame(chunk, _SR, 1), FrameDirection.DOWNSTREAM)
                self._heard += 1
            i += 1
            # Paced on the clock, not by a sleep after each chunk: the render time
            # would add up and starve the transport, a gap in the typing. After a
            # stall one chunk is caught up, no more: a longer catch-up would sit
            # queued ahead of the real reply. (One such stall: MemoryReclaim's gc, at
            # most once a minute while the line is quiet, the typing being no speech
            # to the gate -- ~0.2 s on the box: a pause a typist makes anyway.)
            due = max(due + _CHUNK_MS / 1000, loop.time())
            await asyncio.sleep(due - loop.time())
        self._task = None
        self._consults.clear()
        await self._say({"type": "typing", "on": False, "done": False}, urgent=True)
        logger.info(f"thinking_sound: typing gave up after {_TYPING_MAX_S:.0f}s")

    def _track_line(self, frame: Frame) -> None:
        """Who is talking, as the frames passing here say."""
        now = time.monotonic()
        if isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)):
            self._user = isinstance(frame, VADUserStartedSpeakingFrame)
        elif isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
            self._bot = isinstance(frame, BotStartedSpeakingFrame)
        elif isinstance(frame, (LLMFullResponseStartFrame, LLMFullResponseEndFrame)):
            self._reply_t = now if isinstance(frame, LLMFullResponseStartFrame) else None
        elif isinstance(frame, (InterruptionFrame, FunctionCallsStartedFrame, RunSilentFrame)):
            # A response with no words of its own (cut, a tool call, a run that said
            # nothing) has its End held at the TTS until a later context ends: it is not
            # a reply on its way.
            self._reply_t = None
        elif not isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame)):
            return                          # (a line or a reply on its way; the typing's own is not one)
        self._spoke = now

    async def cleanup(self):
        """The session is over: so is its typing (a task of ours, not pipecat's)."""
        await super().cleanup()
        if self._task is not None:
            task, self._task = self._task, None
            await self.cancel_task(task)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self._track_line(frame)
        if self._async and isinstance(frame, (OutputTransportMessageFrame,
                                              OutputTransportMessageUrgentFrame)):
            m = frame.message if isinstance(frame.message, dict) else {}
            if m.get("type") == "working":
                if m.get("done"):
                    self._consults.discard(m.get("call_id"))
                    if not self._consults:
                        # (the face finishes its typing only on an answer)
                        answered = bool(m.get("answered"))
                        await self._stop("consult answered" if answered else "consult over, unanswered",
                                         done=answered)
                else:
                    self._consults.add(m.get("call_id"))
                    await self._start("background consult")
        elif not self._async and isinstance(frame, FunctionCallsStartedFrame):
            names = {getattr(c, "function_name", None) for c in (frame.function_calls or [])}
            if names & _TRIGGER:
                await self._start("consult")
        elif not self._async and isinstance(frame, FunctionCallResultFrame) \
                and frame.function_name in _TRIGGER:
            answered = isinstance(frame.result, dict) and bool(frame.result.get("answer"))
            await self._stop("consult result" if answered else "consult failed", done=answered)
        elif not self._async and isinstance(frame, InterruptionFrame):
            await self._stop("barge-in")      # (an agent-first consult is cancelled with its turn)
        await self.push_frame(frame, direction)
