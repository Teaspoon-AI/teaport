# SPDX-License-Identifier: MIT
#
# reply_hold.py — don't start a reply over a caller who has started talking again.
#
# The turn commits on the endpointing policy's say-so (endpointing.py): the VAD stop,
# Smart Turn's verdict, and the SMARTTURN_STOP_SECS ceiling on an INCOMPLETE one. The
# ceiling is a guess about a pause, and a caller who is mid-sentence takes it as one:
# on a 2026-10-05 test call (SMARTTURN_STOP_SECS=0.6) 14 verdicts came back INCOMPLETE
# and 10 of those turns were committed by the ceiling while the caller was still in the
# middle of a sentence. The reply's first audio plays ~0.2 s after the commit, and
# nothing looked at the line in between, so the bot started answering the fragment
# while the caller was already saying the rest of it. Their new words only interrupt
# once transcribed (~0.5-1 s), so every one of those exchanges began with the bot
# talking over the caller about something a second or two old.
#
# ReplyHoldGate is the look at the line. It sits directly below the TTS and is ARMED by
# every user-turn commit (the aggregator's on_user_turn_inference_triggered). While
# armed -- from the commit until the reply's first audio has gone through to the
# transport -- the caller starting to speak makes the gate HOLD: it stops processing its
# input queue, so the reply's frames wait in it and none reach the transport. "Starting
# to speak" is the VAD's start OR the faster onset test in speech_onset.py, and a commit
# that lands while either already says "speaking" holds at once. The VAD alone is too
# slow for this: replayed over the same call, its SPEAKING edge came before the reply's
# first audio in 2 of the 13 ceiling commits, and the caller was back in 7. Then one of
# two things happens:
#
#   * The caller's new words arrive. They start a user turn (MinWordsUserTurnStartStrategy;
#     the bot has not started speaking, so one word is enough), the aggregator broadcasts
#     an interruption, and pipecat's own queue reset drops the held frames (keeping the
#     UninterruptibleFrames, as everywhere else). The gate asks HeardContextCorrector to
#     fold the turn the caller is now finishing into the one whose reply was dropped
#     (TurnMerge), so the model answers ONE user message holding everything they said,
#     not the fragment and then its continuation.
#   * No words come: a cough, a breath, an "mm" the STT drops. TEAPORT_REPLY_HOLD_RELEASE_S
#     after the line goes quiet (both signals), the gate releases the reply and it plays.
#     TEAPORT_REPLY_HOLD_MAX_S caps the hold from its start, for a VAD that never stops.
#
# Nothing is held while the caller is silent: the queue is only paused when speech
# starts, so a reply to a caller who stayed quiet passes straight through.
#
# WHY A PAUSED QUEUE AND NOT A HELD LIST. The frames wait in pipecat's own input queue,
# unprocessed. The TranscriptLedger (an observer) charts a frame where it is first
# processed below the TTS, which is HERE: a held reply is simply not seen by the ledger
# until it is released, and a dropped one never is -- its playout layout starts when its
# audio really reaches the transport, and nothing of it is ever charted as said. The
# assistant aggregator likewise never sees the dropped reply's words, so nothing of it
# can be committed to the context. And an interruption's queue reset is pipecat's,
# with its Uninterruptible rules, rather than a copy of them.
#
# Word times. The TTS stamps each TTSTextFrame with its playout time (pts) when it
# synthesizes it. A held reply plays later than that by however long it was held, so
# the gate adds the wait to the pts of every word of that TTS context, BEFORE the
# ledger sees the frame (super().process_frame is where the observer is called). The
# ledger's heard-text cut is made on pts, and early pts would credit the caller with
# words they had not heard yet. The wait is measured per frame (queue arrival to
# processing), so it needs no knowledge of which frames the hold caught.
#
# The TTS is told about the hold too (PlayoutHoldFrame, pushed upstream): its own model
# of what has played (_play_end, behind "lead" pacing and the playout-gap accounting
# that shifts word times) would otherwise run on through the hold.
#
# Known limits:
#   * A commit whose finalization the turn controller REFUSED (the caller was audibly
#     speaking when it landed; endpointing.keep_barge_in_reachable re-applies it later)
#     arms the gate at the inference, and the caller's further words then join the turn
#     that is still open instead of starting a new one, so no interruption drops the
#     held reply: it plays after the release window, followed by the answer to the rest.
#     That is no worse than without the gate. 0 of 27 commits on the 2026-10-05 call.
#   * Only a reply to a USER commit is checked. The greeting, a consult follow-up and
#     a client-note reaction are not armed: they already wait for a quiet moment
#     (FollowupGate) or open the call.
#   * The merge needs the two user messages to be adjacent. A reply that called a tool
#     before it was dropped leaves the tool call between them, and the turns stay apart.
#
import asyncio
from dataclasses import dataclass

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    SystemFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.env import env_flag, env_num

# On by default for both front-ends: the gate only acts while the caller is speaking at
# a reply's start, which is the failure on either surface.
ENABLED = env_flag("TEAPORT_REPLY_HOLD", True)
# How long after the caller's VAD stop a held reply waits for their words before it
# plays anyway. The words of a short resumption ("to the store") often have no interim
# before the VAD stop -- the engine's first delta on a fresh segment trails by ~0.6 s --
# and their final lands after the verdict and the engine's finish, ~0.5-0.7 s after the
# stop. 1.0 s covers that; with ENDPOINT_STOP_SECS=0.2 it is 1.2 s after the speech ends.
RELEASE_SECS = env_num("TEAPORT_REPLY_HOLD_RELEASE_S", "1.0", float)
# The longest a reply is ever held, from the start of the hold, whatever the VAD says.
MAX_SECS = env_num("TEAPORT_REPLY_HOLD_MAX_S", "6.0", float)

# A frame that waited in the queue at least this long was held; less is scheduling.
_HELD_NS = 20_000_000
# Per-context word-time shifts kept at most (a context's shift is only needed while its
# words are still coming).
_MAX_SHIFTS = 8


@dataclass
class PlayoutHoldFrame(SystemFrame):
    """A reply's audio has stopped reaching the transport (`active`) or reaches it
    again (not `active`). Pushed UPSTREAM by ReplyHoldGate for the TTS, whose model of
    what has played freezes for the duration (engine_tts.py)."""

    active: bool = True


class TurnMerge:
    """The gate's request to HeardContextCorrector: fold the next committed user turn
    into the previous one, whose reply was dropped before any of it was played."""

    def __init__(self):
        self.pending = False

    def request(self) -> None:
        self.pending = True

    def take(self) -> bool:
        pending, self.pending = self.pending, False
        return pending


class ReplyHoldGate(FrameProcessor):
    """Hold a fresh reply while the caller is talking; drop it if they say more, play
    it if they don't. See the module header. Place it DIRECTLY below the TTS: the
    processor that first processes the TTS's frames is where the ledger sees them."""

    def __init__(self, *, merge: TurnMerge | None = None,
                 release_secs: float = RELEASE_SECS, max_secs: float = MAX_SECS):
        super().__init__()
        self._merge = merge
        self._release_secs = max(0.0, release_secs)
        self._max_secs = max(self._release_secs, max_secs)
        # From a user-turn commit until its reply's first audio has gone through.
        self._armed = False
        # The response streaming now called a tool: its End does not end the reply.
        # Consumed by that End.
        self._tool_call = False
        # Who says the caller is speaking: the VAD's frames and the faster onset test
        # (speech_onset.py, via on_onset). Either is enough to hold; both must be quiet
        # for the release window to start.
        self._sources = {"vad": False, "onset": False}
        self._holding = False
        self._hold_t0 = 0.0
        self._release_task = None
        self._cap_task = None
        # Queue arrival (pipeline clock, ns) of TTS frames, the per-context pts shift,
        # and when the last hold was released (a frame that arrived before it was held).
        self._arrivals: dict = {}
        self._shift: dict = {}
        self._released_ns = None
        # Tallies, for the log and the tests.
        self.holds = 0
        self.released = 0
        self.dropped = 0

    @property
    def _speaking(self) -> bool:
        return any(self._sources.values())

    @property
    def holding(self) -> bool:
        return self._holding

    @property
    def armed(self) -> bool:
        return self._armed

    # ------------------------------------------------------------------ arming

    async def arm(self):
        """A user turn was just committed: its reply is checked before it plays. Called
        from the user aggregator's on_user_turn_inference_triggered."""
        self._armed = True
        self._tool_call = False
        if self._speaking and not self._holding:
            # The commit landed while the VAD already hears the caller.
            await self._start_hold("the caller is speaking at the commit")

    async def on_onset(self, started: bool):
        """speech_onset.SpeechOnsetMixin listener: the caller's speech began / ended on
        the faster test. Called from the user aggregator's task."""
        await self._speech("onset", started)

    async def _speech(self, source: str, started: bool):
        self._sources[source] = started
        if started:
            self._cancel_release()
            if self._armed and not self._holding:
                await self._start_hold("the caller started speaking before the reply played"
                                       f" ({'VAD' if source == 'vad' else 'speech onset'})")
        elif self._holding and not self._speaking:
            self._schedule_release()

    # ------------------------------------------------------------------ frames

    async def queue_frame(self, frame: Frame, direction=FrameDirection.DOWNSTREAM,
                          callback=None):
        if isinstance(frame, (TTSTextFrame, TTSAudioRawFrame)):
            try:
                self._arrivals[frame.id] = self.get_clock().get_time()
            except Exception:  # noqa: BLE001 -- no clock before setup; nothing to hold then
                pass
        await super().queue_frame(frame, direction, callback)

    def _restamp(self, frame) -> None:
        """Move a held TTS context's word times by how long it waited here."""
        arrived = self._arrivals.pop(frame.id, None)
        ctx = getattr(frame, "context_id", None)
        if arrived is not None and self._released_ns is not None and arrived <= self._released_ns:
            # It arrived before the last release: the hold caught it. (A frame that came
            # through later did not wait here, but plays after the ones that did.)
            waited = self.get_clock().get_time() - arrived
            if waited >= _HELD_NS and waited > self._shift.get(ctx, 0):
                self._shift.pop(ctx, None)
                self._shift[ctx] = waited
                while len(self._shift) > _MAX_SHIFTS:
                    self._shift.pop(next(iter(self._shift)))
        if isinstance(frame, TTSTextFrame) and frame.pts and ctx in self._shift:
            frame.pts += self._shift[ctx]

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, (TTSTextFrame, TTSAudioRawFrame)):
            # Before super(): that is where the ledger reads the pts.
            self._restamp(frame)
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self._speech("vad", True)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._speech("vad", False)
        elif isinstance(frame, InterruptionFrame):
            # A user turn started: the caller's words arrived. pipecat's interruption
            # handling (super) has already dropped the queued frames and unpaused the
            # queue; what is left is to say why.
            await self._on_interruption()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._cancel_release()
            self._cancel_cap()
        elif self._armed and direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSAudioRawFrame):
                # The reply's first audio is on its way to the caller: the check is done.
                self._armed = False
            elif isinstance(frame, (FunctionCallsStartedFrame, FunctionCallInProgressFrame)):
                # FunctionCallsStartedFrame is the one that counts: a SystemFrame the LLM
                # broadcasts before its response's End, while the in-progress frame comes
                # from the call's own task and can trail the End.
                self._tool_call = True
            elif isinstance(frame, LLMFullResponseEndFrame):
                if self._tool_call:
                    # Its tool's result runs the model again: that answer is the reply.
                    self._tool_call = False
                else:
                    # A response that ended without a word of audio and called no tool:
                    # there is no reply to check. Staying armed would hold whatever
                    # speaks next.
                    self._armed = False

        await self.push_frame(frame, direction)

    # ------------------------------------------------------------------ the hold

    async def _start_hold(self, why: str):
        self._holding = True
        self._hold_t0 = asyncio.get_running_loop().time()
        self.holds += 1
        await self.pause_processing_frames()
        logger.info(f"REPLY-HOLD: {why} -- holding the reply until their words arrive "
                    f"(or {self._release_secs:.1f}s after they stop without any)")
        await self.push_frame(PlayoutHoldFrame(active=True), FrameDirection.UPSTREAM)
        self._cap_task = self.create_task(self._cap(), name="reply-hold-cap")
        if not self._speaking:
            self._schedule_release()

    async def _end_hold(self, *, released: bool, why: str):
        if not self._holding:
            return
        self._holding = False
        held = asyncio.get_running_loop().time() - self._hold_t0
        self._cancel_release()
        self._cancel_cap()
        if released:
            self.released += 1
            self._released_ns = self.get_clock().get_time()
            logger.info(f"REPLY-HOLD: released after {held:.2f}s ({why}) -- the reply plays")
        else:
            self.dropped += 1
            logger.info(f"REPLY-HOLD: dropped the held reply after {held:.2f}s ({why})")
        # Also after a drop: the interruption normally unpauses the queue, but not when
        # the frame in hand was Uninterruptible (pipecat then only resets the queue).
        await self.resume_processing_frames()
        await self.push_frame(PlayoutHoldFrame(active=False), FrameDirection.UPSTREAM)

    async def _on_interruption(self):
        was_armed, self._armed = self._armed, False
        self._shift.clear()
        self._arrivals.clear()
        if self._holding:
            await self._end_hold(released=False, why="the caller's new words start a turn")
        if was_armed and self._merge is not None:
            # None of the reply to the last commit reached the caller: what they say now
            # finishes that turn rather than answering a reply they never heard.
            self._merge.request()
            logger.info("REPLY-HOLD: the reply to the last turn was never played -- its "
                        "words and the caller's next ones go to the model as one turn")

    def _schedule_release(self):
        self._cancel_release()
        self._release_task = self.create_task(self._release_after(), name="reply-hold-release")

    async def _release_after(self):
        await asyncio.sleep(self._release_secs)
        self._release_task = None
        await self._end_hold(released=True,
                             why=f"no words within {self._release_secs:.1f}s of the caller stopping")

    async def _cap(self):
        await asyncio.sleep(self._max_secs)
        self._cap_task = None
        await self._end_hold(released=True, why=f"hold cap {self._max_secs:.1f}s")

    def _cancel_release(self):
        task, self._release_task = self._release_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _cancel_cap(self):
        task, self._cap_task = self._cap_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def cleanup(self):
        self._cancel_release()
        self._cancel_cap()
        await super().cleanup()
