#
# teaport — live per-turn latency taps (observation only).
#
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


class TurnTimer(FrameProcessor):
    """Live per-turn latency taps (observation only). Three instances — after STT,
    after the LLM, after TTS — share one per-session `marks` dict and log one line
    per turn: time from user-stopped to stt-final / llm-start / llm-first-token /
    tts-first-audio. Lets us read the WARM pipeline latency the cold e2e harness
    can't see. The dict is per-session (not class-level) because single-slot
    eviction briefly overlaps two sessions — a dead session's t0 would otherwise
    corrupt the replacement's first TURN-TIMING line."""

    def __init__(self, marks: dict):
        super().__init__()
        self._marks = marks

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        t = time.monotonic()
        if isinstance(frame, UserStoppedSpeakingFrame):
            self._marks.clear()
            self._marks["t0"] = t
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
