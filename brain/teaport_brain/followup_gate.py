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
    LLMTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# How long the conversation must stay quiet before a window counts (rejects
# mid-thought pauses and between-turn gaps).
_QUIET_SECS = float(os.getenv("TEAPORT_FOLLOWUP_QUIET_S", "0.7"))
# Ceiling on how long to hold an answer waiting for a gap; past this, speak anyway.
_MAX_WAIT = float(os.getenv("TEAPORT_FOLLOWUP_MAX_WAIT_S", "60"))


class OneShot:
    """A pending retirement: `fire()` runs `retire` exactly once and sets `fired`."""

    def __init__(self, retire):
        self._retire = retire
        self.fired = asyncio.Event()

    def fire(self) -> None:
        if not self.fired.is_set():
            self.fired.set()
            self._retire()


class FollowupTrigger(FrameProcessor):
    """Retires a one-shot context trigger the instant the completion that READ it
    starts answering from it.

    The follow-up's trigger message ("tell the user now...") is a STANDING ORDER in
    the context. Retire it too early and the answer is never spoken; too late and a
    later turn re-executes it. Both were live incidents. The only sound retirement
    point is one causally tied to a completion having read the trigger — which is
    why this is a processor and not a timer on FollowupGate.

    PLACEMENT — directly below the LLM, and it cannot move:
      * FollowupGate sits after transport.output(), and LLMTextFrame never gets
        there: TTSService consumes text frames (`push_text_frames=False`, see
        engine_tts.py) rather than forwarding them.
      * The signal is the first LLMTextFrame, NOT LLMFullResponseStartFrame.
        pipecat pushes the start frame BEFORE `_process_context` serializes the
        context into the request (pipecat/services/openai/base_llm.py — push then
        `await self._process_context(...)`), so retiring there neutralises the
        trigger in place before the model ever sees it, losing EVERY answer. The
        first LLMTextFrame is the earliest point at which the request provably
        carried the trigger and the model is answering from it.

    A completion that reads the trigger and is cancelled before producing text
    leaves it armed on purpose: nothing was spoken, so the next completion should
    still deliver it.
    """

    def __init__(self):
        super().__init__()
        self._armed: list = []

    def arm(self, retire) -> OneShot:
        """Register `retire` to run when the next answering completion produces text."""
        shot = OneShot(retire)
        self._armed.append(shot)
        return shot

    def disarm(self, shot: OneShot) -> None:
        """Withdraw a pending retirement (the caller gave up waiting for it)."""
        if shot in self._armed:
            self._armed.remove(shot)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # Fire synchronously, with no await between the check and the retirement, so
        # a single completion can never be seen as two.
        if self._armed and isinstance(frame, LLMTextFrame) \
                and any(c.isalnum() for c in (frame.text or "")):
            armed, self._armed = self._armed, []
            for shot in armed:
                shot.fire()
        await self.push_frame(frame, direction)


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
        # The complement, so a caller can wait for activity to START as well as stop.
        self._busy = asyncio.Event()

    def _refresh(self):
        if self._user or self._bot or self._llm:
            self._idle.clear()
            self._busy.set()
        else:
            self._idle.set()
            self._busy.clear()

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

    async def wait_until_delivered(self, start_timeout: float = 10.0) -> None:
        """Wait for the next stretch of activity to start and then finish.

        Used by the SIP front-end to let the busy line play before hanging up, where
        nothing else is talking and "any activity" is precisely the right signal.

        NOT sound for retiring a one-shot context trigger, which is what it was
        originally written for. _busy is set by ANY activity — the user's speech as
        readily as the turn the caller queued — so this returns on someone else's
        turn and the trigger is retired having never been read. TLC found the
        interleaving in 8 steps (brain/formal/Followup.tla, MODE = "asWritten",
        invariant NoSilentLoss); it needs nothing more exotic than the user speaking
        just after a consult lands. Retirement now belongs to FollowupTrigger, which
        keys on the completion that actually read the trigger.
        """
        try:
            await asyncio.wait_for(self._busy.wait(), timeout=start_timeout)
        except asyncio.TimeoutError:
            return
        await self._idle.wait()

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
