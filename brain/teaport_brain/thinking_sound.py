#
# thinking_sound.py — "assistant is thinking" comfort audio for long tool calls.
#
# During an ask_openclaw consult the brain hands off to the full desktop agent and
# waits ~15-35s for the answer (web search + fetch + reasoning). On a voice call
# that is dead air. This processor fills ONLY that silence with a soft keyboard-
# typing loop: a short grace period after the consult's tool call begins, then a
# looping bed streamed as output audio, stopped when the consult RESULT returns (or
# a barge-in). Quick tools (time, host status, web_search) never trigger it — only
# the tools in _TRIGGER, which is just ask_openclaw.
#
# Placed just before transport.output(): it sees the consult's
# FunctionCallsStartedFrame (start) and FunctionCallResultFrame (stop) coming DOWN
# from the LLM, and injects its own audio DOWNSTREAM. Stop is keyed on the RESULT,
# NOT on reply audio: ask_openclaw speaks a preamble/filler ("I'll work on that…")
# right after the call starts, and that filler audio would otherwise trip an
# audio-based stop and kill the bed during its grace period before it plays a note.
# The result frame arrives only after the consult finishes, cleanly separating the
# filler (before) from the reply (after); the ~1-2s until reply audio is a natural
# beat, not dead air.
#
# The bed is synthesized procedurally (royalty-free, no external asset) and cached
# to assets/typing-v2.wav next to this file. The name changes with the synthesis: a
# cached wav is used whenever it exists (and the wheel packages whatever sits in
# assets/), so an older bed under the same name would keep playing. Drop your own
# 24 kHz mono wav at TEAPORT_THINKING_WAV to override it. Kill switch:
# TEAPORT_THINKING_SOUND=0.
#
import asyncio
import os
import wave

import numpy as np
from loguru import logger

from pipecat.frames.frames import (
    Frame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.settings import setting

ENABLED = setting("TEAPORT_THINKING_SOUND")

_SR = 24000                 # engine TTS rate; the relay is 24 kHz both ways
_CHUNK_MS = 40
_GRACE_S = setting("TEAPORT_THINKING_GRACE_S")   # silence before the bed starts
_GAIN = setting("TEAPORT_THINKING_GAIN")         # 1.0 = the synthesized peak
_MAX_S = setting("TEAPORT_THINKING_MAX_S")        # hard cap (> ask_openclaw timeout)
_WAV = setting("TEAPORT_THINKING_WAV",
               default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "typing-v2.wav"))
# Only genuinely-long tool calls get a thinking bed; the fast tools resolve before
# the grace period even elapses, so a bed there would just be a blip.
_TRIGGER = {"ask_openclaw"}


def _synthesize(seconds: float = 15.0) -> np.ndarray:
    """Procedural keyboard-typing loop → int16 PCM at _SR, modelled on a close-miked
    clicky mechanical keyboard (an ASMR typing recording, measured, not sampled):
    steady touch typing at ~8 keys/s.

    A key is a physical object, so it is built as one: a set of damped sinusoids
    (modes) that ring the same way every time it is struck. In the recording, presses
    of the same key are near-identical (0-12 ms waveforms correlate ~0.9), and that
    repeatable ring is what reads as a plastic shell rather than noise. Two kinds of
    sound per key. The SHELL is the keycap and housing: modes at 0.6-3.4 kHz (most
    near 1.1-1.7 and 2.1-3.4 kHz) that die in 1-4 ms, over the case's ~870 Hz ring.
    The CLICK is the switch: short modes at 2-12 kHz, the brightest thing in the
    recording, that also set the shell ringing a little and a third of the time
    strike twice. Each key draws its modes from the recording's per-band statistics
    (how many modes, how loud, how fast they die), so no two keys are alike.

    A keystroke: the finger lands (the shell, softly), the switch clicks, the key
    bottoms out 4-12 ms later (the shell over a ~50 Hz desk thump), bounces, and on
    the release rings the shell again, often with a softer click; landing, bottom-out
    and release each strike the shell in their own way. The clicks come at a steady
    level and the thocks vary widely, as in the recording. Twenty keys used as
    unevenly as letters, and a deeper space bar every few. No reverb wash: the
    recording's gaps are dark, and a long tail blurs the clicks into hiss. Fixed seed
    = reproducible; every tail wraps past the end, so the loop has no seam."""
    rng = np.random.default_rng(7)
    out = np.zeros(int(_SR * seconds))
    t = np.arange(int(_SR * 0.04)) / _SR                  # one strike's span
    tt = np.arange(int(_SR * 0.08)) / _SR                 # the desk thump's
    # The recording's modes, per band: (lo Hz, hi Hz, modes per key, level, decay ms).
    # A key's lead mode (level 1) comes from one of the bands in the *_lead tuple.
    shell_bands = ((600, 800, 0.4, (0.3, 0.6), (1.3, 1.95)),
                   (800, 950, 0.5, (0.3, 0.7), (1.9, 3.75)),
                   (950, 1250, 1.0, (0.3, 1.0), (1.35, 2.6)),
                   (1250, 1700, 1.7, (0.25, 0.6), (1.5, 2.4)),
                   (1700, 2100, 0.15, (0.3, 0.6), (0.9, 1.65)),
                   (2100, 2700, 1.4, (0.25, 0.6), (1.1, 2.25)),
                   (2700, 3400, 1.2, (0.3, 0.7), (0.75, 1.5)),
                   (3400, 4500, 0.4, (0.3, 0.6), (0.5, 0.9)),
                   (4500, 8000, 0.15, (0.4, 0.6), (0.3, 0.45)),
                   (8000, 11500, 0.15, (0.35, 0.65), (0.3, 0.5)))
    shell_lead = (1, 2, 2, 3)                             # 0.8-1.7 kHz, mostly ~1.1
    click_bands = ((950, 1700, 0.6, (0.2, 0.5), (1.3, 3.0)),
                   (2100, 2700, 0.65, (0.15, 0.5), (0.7, 1.7)),
                   (2700, 3400, 0.9, (0.2, 0.7), (0.7, 1.7)),
                   (3400, 4500, 0.65, (0.3, 0.8), (0.6, 1.2)),
                   (4500, 8000, 2.5, (0.5, 1.2), (0.5, 1.0)),
                   (8000, 11500, 2.0, (0.6, 1.3), (0.3, 0.8)))
    click_lead = (1, 2, 3, 5, 5)
    case = (870, 0.004, 0.3)                              # under every key: Hz, decay s, level

    def modes(bands, lead, scale=1.0):
        """A key's modes [(Hz, decay s, level)], drawn band by band."""
        j0 = lead[rng.integers(len(lead))]
        ms = []
        for j, (lo, hi, n, level, decay) in enumerate(bands):
            for c in range(max(rng.poisson(n), j == j0)):
                hz = np.exp(rng.uniform(np.log(lo), np.log(hi))) * scale
                ms.append((hz, rng.uniform(*decay) / 1000,
                           1.0 if j == j0 and c == 0 else rng.uniform(*level)))
        return ms

    def bank(ms, rise):
        """The modes' damped sines and cosines, precomputed: a strike weighs and sums
        them, so every strike of a key rings its same modes. `rise`: the impact's rise
        time, s."""
        hz, decay, level = (np.array(v)[:, None] for v in zip(*ms))
        env = np.exp(-t / decay) * (1 - np.exp(-t / rise))
        return (level[:, 0], rng.uniform(0, 2 * np.pi, len(ms)),
                env * np.sin(2 * np.pi * hz * t), env * np.cos(2 * np.pi * hz * t))

    def strike(b):
        """One strike of a bank, at unit level. Each mode comes out a little louder or
        softer and a little shifted in phase from strike to strike, as no two presses
        of a real key are identical."""
        level, phase, sin, cos = b
        level = level * np.maximum(0.1, 1 + rng.normal(0, 0.1, len(level)))
        phase = phase + rng.normal(0, 0.15, len(phase))
        s = (level * np.cos(phase)) @ sin + (level * np.sin(phase)) @ cos
        return s / np.sqrt(np.mean(s[:int(_SR * 0.004)] ** 2))

    def key(space=False):
        """One key: its shell as rung by the landing, the bottom-out and the release,
        its click, and its bottom-out thump. The space bar is lower and rings longer."""
        shell = [(hz, decay * (1.5 if space else 1.0), level)
                 for hz, decay, level in modes(shell_bands, shell_lead, 0.65 if space else 1.0)]
        hits = [bank([(hz, decay, level * rng.uniform(0.5, 1.0)) for hz, decay, level in shell] + [case],
                     0.00015) for _ in range(3)]
        click = bank(modes(click_bands, click_lead, 0.93 if space else 1.0), 0.00003)
        hz = rng.uniform(44, 58) * (0.9 if space else 1.0)
        thump = ((0.28 if space else 0.18) * np.exp(-tt / 0.012)
                 * (np.sin(2 * np.pi * hz * tt) + 0.2 * np.sin(2 * np.pi * 2.4 * hz * tt))
                 * (1 - np.exp(-tt / 0.001)))
        return hits, click, thump

    def click(k):
        s = strike(k[1]) + 0.2 * strike(k[0][1])          # the snap shakes the keycap too
        if rng.random() < 0.35:                           # and the switch strikes twice
            d = int(rng.uniform(0.0006, 0.0015) * _SR)
            s[d:] += 10 ** (rng.normal(-4, 2) / 20) * strike(k[1])[:len(s) - d]
        grit = np.diff(rng.standard_normal(len(t)) * np.exp(-t / 0.00015), prepend=0)
        s += 0.3 * grit / np.sqrt(np.mean(grit[:int(_SR * 0.001)] ** 2))
        return 10 ** (2 / 20) * s

    def thock(k, hit, bottom_out=False):
        """The key's shell, rung by the landing (0), the bottom-out (1) or the release (2)."""
        s = strike(k[0][hit]) + 0.15 * rng.standard_normal(len(t)) * np.exp(-t / 0.0008)
        return np.pad(s, (0, len(tt) - len(t))) + k[2] if bottom_out else s

    def add(s, at, db):
        i = int(at * _SR) % len(out)
        s = s * 10 ** (db / 20)
        n = min(len(s), len(out) - i)
        out[i:i + n] += s[:n]
        out[:len(s) - n] += s[n:]                         # wrap: the loop has no seam

    keys = [key() for _ in range(20)]
    space = key(space=True)
    usage = 1 / np.arange(1, len(keys) + 1) ** 0.8        # some keys far more than others
    usage /= usage.sum()
    pos, to_space = 0.0, rng.integers(4, 7)
    while pos < seconds:
        to_space -= 1
        if to_space == 0:
            k, to_space = space, rng.integers(4, 7)
        else:
            k = keys[rng.choice(len(keys), p=usage)]
        db = rng.normal(0, 2)
        if rng.random() < 0.8:
            add(thock(k, 0), pos - rng.uniform(0.004, 0.045), db + rng.normal(-11, 5))  # landing
        add(click(k), pos, db + rng.normal(0, 1.5))
        add(thock(k, 1, bottom_out=True), pos + rng.uniform(0.004, 0.012), db + rng.normal(-2, 7))
        if rng.random() < 0.8:
            add(thock(k, 1), pos + rng.uniform(0.013, 0.025), db + rng.normal(-7, 5))   # bounce
        if rng.random() < 0.8:
            at = pos + rng.uniform(0.05, 0.11)
            add(thock(k, 2), at, db + rng.normal(-8, 5))                              # release
            if rng.random() < 0.6:
                add(click(k), at - 0.004, db + rng.normal(-7, 3))
        for _ in range(rng.poisson(2)):                   # rattle
            add(thock(k, rng.integers(3)), pos + rng.uniform(-0.03, 0.08), db + rng.normal(-20, 5))
        pos += float(np.clip(0.115 * np.exp(rng.normal(0, 0.35)), 0.045, 0.26))
        if rng.random() < 0.035:
            pos += rng.uniform(0.15, 0.45)                # a rare pause between words
    # The room: a short, dark tail (0.12 s, under 3 kHz, -18 dB), convolved circularly
    # so it too carries over the seam.
    m = int(_SR * 0.12)
    tail = rng.standard_normal(m) * np.exp(-6.9 * np.arange(m) / m)
    fm = np.maximum(np.fft.rfftfreq(m, 1 / _SR), 1.0)
    tail = np.fft.irfft(np.fft.rfft(tail) / np.sqrt(1 + (fm / 3000) ** 4), m)
    room = np.zeros(len(out))
    room[:m] = tail * 10 ** (-18 / 20) / np.sqrt(np.sum(tail ** 2))
    room[0] = 1.0
    out = np.fft.irfft(np.fft.rfft(out) * np.fft.rfft(room), len(out))
    # As loud as the sparser bed this replaced (~-34 LKFS), so TEAPORT_THINKING_GAIN
    # keeps its level.
    out *= 10 ** (-34.8 / 20) / np.sqrt(np.mean(out ** 2))
    out = 0.5 * np.tanh(out / 0.5)                    # stacked clicks: held under -6 dBFS
    return (out * 32767).astype(np.int16)


def _load_pcm() -> np.ndarray:
    """Load the override/cached wav; synthesize (and cache) if absent/mismatched."""
    try:
        with wave.open(_WAV, "rb") as w:
            if w.getframerate() == _SR and w.getnchannels() == 1 and w.getsampwidth() == 2:
                return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        logger.warning(f"thinking_sound: {_WAV} not 24kHz/mono/16-bit — regenerating")
    except (FileNotFoundError, wave.Error, EOFError):
        pass
    pcm = _synthesize()
    try:
        os.makedirs(os.path.dirname(_WAV), exist_ok=True)
        with wave.open(_WAV, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(_SR)
            w.writeframes(pcm.tobytes())
        logger.info(f"thinking_sound: synthesized typing bed → {_WAV}")
    except OSError as e:
        logger.warning(f"thinking_sound: could not cache wav ({e}); using in-memory")
    return pcm


# Chunked once, process-wide — every session reuses the same immutable byte chunks.
_CHUNKS: list = None


def _get_chunks() -> list:
    global _CHUNKS
    if _CHUNKS is None:
        pcm = (_load_pcm().astype(np.float32) * _GAIN)
        pcm = np.clip(pcm, -32768, 32767).astype(np.int16).tobytes()
        n = (_SR * _CHUNK_MS // 1000) * 2       # bytes per chunk (int16 mono)
        # Every whole chunk, the last included: the synthesized loop is seamless only
        # if it plays to its end (a partial tail chunk is dropped).
        _CHUNKS = [pcm[i:i + n] for i in range(0, len(pcm) - n + 1, n)]
    return _CHUNKS


def warm() -> None:
    """Build the bed at the brain's start, not in the first session's pipeline build:
    with no cached wav (the first start, or an install whose package dir is read-only)
    synthesizing it holds the event loop ~0.3 s on an Orin NX."""
    if ENABLED:
        _get_chunks()


class ThinkingSound(FrameProcessor):
    """Streams a soft typing loop into the output while a long consult runs, so the
    wait isn't dead air. See module docstring for placement/stop-signal rationale."""

    def __init__(self):
        super().__init__()
        self._chunks = _get_chunks()
        self._task = None
        self._active = False
        self._pushed = 0

    def _start(self):
        if self._active or not self._chunks:
            return
        self._active = True
        self._pushed = 0
        self._task = self.create_task(self._run())
        logger.info(f"thinking_sound: bed armed (grace {_GRACE_S}s)")

    async def _stop(self, reason: str):
        if not self._active and self._task is None:
            return
        self._active = False
        if self._task is not None:
            task, self._task = self._task, None
            await self.cancel_task(task)
        logger.info(f"thinking_sound: bed stopped ({reason}); pushed {self._pushed} chunks")

    async def _run(self):
        try:
            await asyncio.sleep(_GRACE_S)
            i = 0
            elapsed = _GRACE_S
            # Pace at ~1× real time: the transport plays from a small buffer, so a
            # touch slower than real time (loop overhead) keeps the backlog near zero
            # and the real reply isn't delayed behind queued typing.
            while self._active and elapsed < _MAX_S:
                await self.push_frame(
                    TTSAudioRawFrame(self._chunks[i % len(self._chunks)], _SR, 1),
                    FrameDirection.DOWNSTREAM,
                )
                i += 1
                self._pushed = i
                elapsed += _CHUNK_MS / 1000
                await asyncio.sleep(_CHUNK_MS / 1000)
        except asyncio.CancelledError:
            raise
        finally:
            self._active = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, FunctionCallsStartedFrame):
            names = {getattr(c, "function_name", None) for c in (frame.function_calls or [])}
            if names & _TRIGGER:
                self._start()
        elif self._active and isinstance(frame, FunctionCallResultFrame) \
                and frame.function_name in _TRIGGER:
            # The consult's answer is back — stop the bed. (Keyed on the RESULT, not
            # reply audio: the tool's own preamble/filler audio comes first and would
            # otherwise kill the bed during its grace period.)
            await self._stop("consult result")
        elif self._active and isinstance(frame, InterruptionFrame):
            await self._stop("barge-in")
        await self.push_frame(frame, direction)
