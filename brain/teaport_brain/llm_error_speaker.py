#
# llm_error_speaker.py — say something when the model call fails.
#
# An LLM failure currently reaches the journal as an ErrorFrame and reaches the caller
# as nothing at all. On a voice call that is indistinguishable from being ignored, and
# it has now cost three debugging sessions: a 402 that silenced nine turns while the key
# still had credit (2026-08-18), and a stalled request that hung a turn until the
# operator tore the session down (2026-08-19). In both the logs held the answer and the
# room held silence.
#
# A timeout (services._llm_timeout) bounds how long the silence lasts. It does not make
# the silence mean anything — that is this processor's job. It sits immediately after
# the LLM service, where ErrorFrames pass on their way upstream to the task, and turns
# one into a short spoken line.
#
# TTSSpeakFrame, not LLMTextFrame: this is the pipeline talking, not the model. It goes
# straight to the TTS without entering the conversation as an assistant turn, so a
# failed call cannot poison the context the way a degenerate completion does (see
# llm_text_guard).
#
# Debounced because a failing endpoint errors per turn and sometimes per retry; saying
# it once per window is information, saying it five times is noise.
#
# Disable with TEAPORT_LLM_ERROR_SPEECH=0.
#
import time

from loguru import logger

from pipecat.frames.frames import ErrorFrame, Frame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.env import env_flag, env_num

ENABLED = env_flag("TEAPORT_LLM_ERROR_SPEECH", True)

# Deliberately vague about the cause: the caller cannot act on "402" or "read timeout",
# and guessing wrong is worse than saying nothing specific. The journal has the detail.
ERROR_TEXT = "Sorry, I couldn't reach my language model just then. Could you try again?"

# One spoken notice per window, however many frames arrive.
_DEBOUNCE_SECS = env_num("TEAPORT_LLM_ERROR_SPEECH_DEBOUNCE", "30", float)


class LLMErrorSpeaker(FrameProcessor):
    """Speaks a short line when the LLM call fails; forwards the error untouched."""

    def __init__(self):
        super().__init__()
        self._last = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, ErrorFrame):
            now = time.monotonic()
            if now - self._last >= _DEBOUNCE_SECS:
                self._last = now
                logger.warning(
                    f"LLMErrorSpeaker: speaking the failure notice for {frame}"
                )
                # Downstream regardless of which way the error is travelling — the TTS
                # is downstream of here, and the error itself still needs to reach the
                # task upstream.
                await self.push_frame(TTSSpeakFrame(ERROR_TEXT), FrameDirection.DOWNSTREAM)
            else:
                logger.debug("LLMErrorSpeaker: notice debounced")
        await self.push_frame(frame, direction)
