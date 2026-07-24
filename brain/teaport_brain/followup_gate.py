#
# followup_gate.py — "is it a good moment to speak?" gate for unprompted turns.
#
# The async ask_openclaw path speaks its answer as an UNPROMPTED follow-up turn
# whenever the background consult lands. Firing that blindly steps on whoever is
# talking — the user mid-utterance, OR the assistant itself mid-answer about
# something else (the LLM is generating / the bot is still speaking a prior turn).
#
# This processor tracks conversation activity from three signals and exposes
# wait_until_idle(), which resolves at the next DEBOUNCED quiet window — both sides
# silent AND no LLM response in flight, held for a short beat so a mid-thought pause
# doesn't count — or after a max wait, so a very chatty conversation can't strand
# the answer forever.
#
# Placed right after transport.output(): the output transport pushes
# Bot{Started,Stopped}SpeakingFrame downstream (as well as upstream), and the user /
# LLM frames propagate downstream through the whole pipeline, so all three activity
# signals are visible at that one spot.
#
import asyncio
import os
import time

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# How long the conversation must stay quiet before a window counts (rejects
# mid-thought pauses and between-turn gaps).
_QUIET_SECS = float(os.getenv("TEAPORT_FOLLOWUP_QUIET_S", "0.7"))
# Ceiling on how long to hold an answer waiting for a gap; past this, speak anyway.
_MAX_WAIT = float(os.getenv("TEAPORT_FOLLOWUP_MAX_WAIT_S", "60"))


class FollowupGate(FrameProcessor):
    """Tracks whether the user is speaking, the bot is speaking, or the LLM is
    mid-response, and lets an unprompted turn wait for a clear moment."""

    def __init__(self, quiet_secs: float = _QUIET_SECS, max_wait: float = _MAX_WAIT):
        super().__init__()
        self._quiet_secs = quiet_secs
        self._max_wait = max_wait
        self._user = False
        self._bot = False
        self._llm = False
        # Set == conversation idle. Starts idle (nobody has spoken yet).
        self._idle = asyncio.Event()
        self._idle.set()

    def _refresh(self):
        if self._user or self._bot or self._llm:
            self._idle.clear()
        else:
            self._idle.set()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            self._user = True
            self._refresh()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user = False
            self._refresh()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot = True
            self._refresh()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot = False
            self._refresh()
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._llm = True
            self._refresh()
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._llm = False
            self._refresh()
        await self.push_frame(frame, direction)

    async def wait_until_idle(self) -> bool:
        """Block until a debounced quiet window. Returns True at a genuine window,
        False if it gave up at max_wait (caller should speak anyway rather than lose
        the answer). Cancellation propagates (session teardown)."""
        start = time.monotonic()
        while True:
            remaining = self._max_wait - (time.monotonic() - start)
            if remaining <= 0:
                logger.info("followup_gate: max wait reached; delivering anyway")
                return False
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                logger.info("followup_gate: max wait reached; delivering anyway")
                return False
            # Idle right now — require it to STAY idle through the debounce so we
            # don't jump into a brief pause between the user's (or bot's) phrases.
            await asyncio.sleep(min(self._quiet_secs, max(0.05, remaining)))
            if self._idle.is_set():
                return True
            # Someone resumed during the debounce — wait for the next window.
