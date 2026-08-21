#
# teaport — live per-turn latency taps, and the watchdog that says when a turn
# produced no reply at all.
#
import asyncio
import os
import time

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# How long a committed turn may stay silent before it is reported. Long enough to clear
# a slow ask_openclaw consult (the ThinkingSound bed covers those), short enough that the
# line lands while the session is still live.
_SILENT_TURN_SECS = float(os.getenv("TEAPORT_SILENT_TURN_SECS", "12"))


class TurnTimer(FrameProcessor):
    """Live per-turn latency taps (observation only). Three instances — after STT,
    after the LLM, after TTS — share one per-session `marks` dict and log one line
    per turn: time from user-stopped to stt-final / llm-start / llm-first-token /
    tts-first-audio. Lets us read the WARM pipeline latency the cold e2e harness
    can't see. The dict is per-session (not class-level) because single-slot
    eviction briefly overlaps two sessions — a dead session's t0 would otherwise
    corrupt the replacement's first TURN-TIMING line."""

    def __init__(self, marks: dict, watchdog: bool = False):
        super().__init__()
        self._marks = marks
        # Only one of the three taps arms the silent-turn watchdog. They all see
        # UserStoppedSpeakingFrame and share one dict, so without an owner every silent
        # turn would be reported three times.
        self._owns_watchdog = watchdog
        self._watchdog = None
        self._turn = 0

    # A committed turn that never speaks is the single most common way this pipeline
    # fails, and until now it left NOTHING in the journal — TURN-TIMING only logs on
    # first audio, so a turn that produced none simply had no line. Five distinct causes
    # were found this way on 2026-08-20, each costing a live session and a long forensic
    # dig, and every one of them would have been named instantly by this warning:
    # whichever marks are missing say exactly how far the turn got.
    async def _bark(self, turn: int):
        try:
            await asyncio.sleep(_SILENT_TURN_SECS)
        except asyncio.CancelledError:
            return
        m = self._marks
        if turn != self._turn or m.get("done"):
            return
        reached = [k for k in ("stt_final", "llm_start", "llm_first_token") if k in m]
        logger.warning(
            f"SILENT TURN: {_SILENT_TURN_SECS:.0f}s after user-stopped and no audio. "
            f"reached={reached or ['nothing']} — "
            + ("the model was never asked (empty aggregation, or the turn was "
               "force-stopped without inference)" if "llm_start" not in m
               else "the model answered but nothing was spoken")
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        t = time.monotonic()
        if isinstance(frame, UserStoppedSpeakingFrame):
            self._marks.clear()
            self._marks["t0"] = t
            if self._owns_watchdog:
                self._turn += 1
                if self._watchdog:
                    await self.cancel_task(self._watchdog)
                self._watchdog = self.create_task(self._bark(self._turn))
        elif self._marks and not self._marks.get("done"):
            m = self._marks
            if isinstance(frame, TranscriptionFrame):
                m.setdefault("stt_final", t)
            elif isinstance(frame, LLMFullResponseStartFrame):
                m.setdefault("llm_start", t)
            elif isinstance(frame, LLMTextFrame):
                m.setdefault("llm_first_token", t)
            elif isinstance(frame, TTSAudioRawFrame):
                m.setdefault("tts_first_audio", t)
                m["done"] = True
                t0 = m["t0"]
                seg = "  ".join(
                    f"{k}=+{m[k]-t0:.2f}s"
                    for k in ("stt_final", "llm_start", "llm_first_token", "tts_first_audio")
                    if k in m
                )
                logger.info(f"TURN-TIMING (after user-stopped)  {seg}")
        await self.push_frame(frame, direction)
