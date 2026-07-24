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
# to assets/typing.wav next to this file. Drop your own 24 kHz mono wav at
# TEAPORT_THINKING_WAV to override it. Kill switch: TEAPORT_THINKING_SOUND=0.
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

ENABLED = os.getenv("TEAPORT_THINKING_SOUND", "1").strip().lower() not in ("0", "false", "no")

_SR = 24000                 # engine TTS rate; the relay is 24 kHz both ways
_CHUNK_MS = 40
_GRACE_S = float(os.getenv("TEAPORT_THINKING_GRACE_S", "1.5"))   # silence before the bed starts
_GAIN = float(os.getenv("TEAPORT_THINKING_GAIN", "0.8"))         # 1.0 = the synthesized peak
_MAX_S = float(os.getenv("TEAPORT_THINKING_MAX_S", "60"))        # hard cap (> ask_openclaw timeout)
_WAV = os.getenv(
    "TEAPORT_THINKING_WAV",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "typing.wav"),
)
# Only genuinely-long tool calls get a thinking bed; the fast tools resolve before
# the grace period even elapses, so a bed there would just be a blip.
_TRIGGER = {"ask_openclaw"}


def _synthesize(seconds: float = 6.0) -> np.ndarray:
    """Procedural keyboard-typing loop → int16 PCM at _SR. Fixed seed = reproducible;
    each key = a bright click transient + a short noise 'tap' body + a soft low thock,
    sequenced at human cadence with occasional word/thinking pauses."""
    rng = np.random.default_rng(7)

    def key(kind):
        n = int(_SR * rng.uniform(0.018, 0.032))
        t = np.arange(n) / _SR
        click = rng.standard_normal(n) * np.exp(-t * rng.uniform(450, 700))
        body = rng.standard_normal(n) * np.exp(-t * rng.uniform(160, 240)) * 0.5
        thock = np.sin(2 * np.pi * rng.uniform(90, 140) * t) * np.exp(-t * 90) * 0.15
        s = click + body + thock
        amp = {"soft": 0.35, "mid": 0.6, "hard": 0.9}[kind]
        return s * amp / (np.max(np.abs(s)) + 1e-9)

    out = np.zeros(int(_SR * seconds), dtype=np.float32)
    pos = 0.05
    while pos < seconds - 0.1:
        kind = rng.choice(["soft", "mid", "hard"], p=[0.4, 0.45, 0.15])
        k = key(kind)
        i = int(pos * _SR)
        end = min(i + len(k), len(out))
        out[i:end] += k[: end - i]
        pos += rng.uniform(0.35, 0.7) if rng.random() < 0.12 else rng.uniform(0.07, 0.16)
    out *= 0.5 / (np.max(np.abs(out)) + 1e-9)   # normalize to -6 dBFS peak
    return (np.clip(out, -1, 1) * 32767).astype(np.int16)


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
        _CHUNKS = [pcm[i:i + n] for i in range(0, max(0, len(pcm) - n), n)]
    return _CHUNKS


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
