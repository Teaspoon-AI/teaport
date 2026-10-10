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
# every user-turn commit (the aggregator's on_user_turn_message_added). While
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
#     after the line goes quiet -- the VAD's stop AND the onset test's offset, whichever
#     is later -- the gate releases the reply and it plays. TEAPORT_REPLY_HOLD_MAX_S caps
#     a hold from its start, for a VAD that never stops; a hold only the onset test ever
#     asked for (no VAD start in it) is capped sooner, at TEAPORT_REPLY_HOLD_ONSET_MAX_S:
#     that test has no volume gate, so steady room noise above its threshold (a TV, a fan)
#     would otherwise hold every reply to the full cap.
#   * The session ends (an EndFrame or StopFrame -- the Talk client's close, a hang-up):
#     the held reply is dropped and the end goes straight through, rather than waiting
#     out the hold and playing the reply into a closing session.
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
# Playout times. The TTS stamps its frames with their playout time (pts) as it
# synthesizes them: every word (TTSTextFrame), and also the response's End when it is
# re-pushed after the audio (tts_service: the last word's pts), plus the aggregated-text
# frames captions read. The output transport releases all of them on its clock at that
# pts. A held reply plays later by however long it was held, so the gate moves the pts of
# EVERY such frame of that TTS context by the wait -- frames with no context of their own
# (the End) belong to the context last seen -- BEFORE the ledger sees the frame
# (super().process_frame is where the observer is called). Moving only the words let the
# End overtake them on the transport's clock, and the assistant aggregator committed the
# reply's first word or two and leaked the rest into the next message. The ledger's
# heard-text cut is made on the same pts. The wait is measured per frame (queue arrival
# to processing) and kept per context, so a word the TTS places after the release, on
# the same pre-hold baseline, moves by the same amount.
#
# The TTS is told about the hold too (PlayoutHoldFrame, pushed upstream): its model of
# what has played (_play_end, behind "lead" pacing and the playout-gap accounting) stands
# still through it, and the playout clock a reply queued BEHIND the held one is anchored
# to (_prev_audio_end_ns) moves with it (engine_tts._freeze_playout).
#
# Known limits:
#   * The release is a timer, not knowledge: words that arrive later than
#     TEAPORT_REPLY_HOLD_RELEASE_S after the line went quiet (a slow STT), or a hold the
#     cap ends while the caller is still talking, let the reply play and be cut by those
#     words as before. The accepted residual (formal/README: ut_hold_gate_timed).
#   * A commit whose finalization the turn controller REFUSED (the caller was audibly
#     speaking when it landed; endpointing.keep_barge_in_reachable re-applies it later)
#     arms the gate at its push, and the caller's further words then join the turn
#     that is still open instead of starting a new one, so no interruption drops the
#     held reply: it plays after the release window, followed by the answer to the rest.
#     That is no worse than without the gate. 0 of 27 commits on the 2026-10-05 call.
#   * Only a reply to a USER commit is checked. The greeting, a consult follow-up and
#     a client-note reaction are not armed: they already wait for a quiet moment
#     (FollowupGate) or open the call.
#   * The merge needs nothing but system messages between the two user messages (a
#     MemoryRecall note lands there). A reply that called a tool before it was dropped
#     leaves the tool call between them, and the turns stay apart.
#   * During a tool call, a filler line (a tool acknowledgement, the consult narrator)
#     is audio like any other: it passes the gate and disarms it, so the answer spoken
#     after the tool returns is not checked.
#
import asyncio
import os
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
    StopFrame,
    SystemFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
    UninterruptibleFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.settings import setting

# Per front-end. On for SIP, where the failure was measured (a 2026-10-05 test call);
# off for Talk until a Talk session has been measured with it -- the only data so far is
# telephony, and Talk's wideband audio and client-side echo cancelling are different.
# agent_session takes the front-end's value from its caller (sip_server / gateway_server).
SIP_ENABLED = setting("TEAPORT_REPLY_HOLD_SIP")
TALK_ENABLED = setting("TEAPORT_REPLY_HOLD_TALK")
if os.getenv("TEAPORT_REPLY_HOLD") is not None:
    logger.warning("TEAPORT_REPLY_HOLD is no longer read: the reply hold is set per front-end "
                   "by TEAPORT_REPLY_HOLD_SIP (default on) and TEAPORT_REPLY_HOLD_TALK "
                   "(default off)")
# How long a held reply waits for the caller's words once the line is quiet -- the VAD's
# stop and the onset test's offset both -- before it plays anyway. The words of a short
# resumption ("to the store") often have no interim before the VAD stop -- the engine's
# first delta on a fresh segment trails by ~0.6 s -- and their final lands after the
# verdict and the engine's finish, ~0.5-0.7 s after the stop. 1.0 s covers that; with
# ENDPOINT_STOP_SECS=0.2 it is ~1.2 s after the speech ends.
RELEASE_SECS = setting("TEAPORT_REPLY_HOLD_RELEASE_S")
# The longest a reply is ever held, from the start of the hold, whatever the VAD says.
# Never less than RELEASE_SECS (the gate raises it to that).
MAX_SECS = setting("TEAPORT_REPLY_HOLD_MAX_S")
# The cap for a hold only the onset test asked for: no VAD start during it.
ONSET_MAX_SECS = setting("TEAPORT_REPLY_HOLD_ONSET_MAX_S")

# A frame that waited in the queue at least this long was held; less is scheduling.
_HELD_NS = 20_000_000
# Per-context playout-time shifts kept at most (a context's shift is only needed while
# its frames are still coming).
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
                 release_secs: float = RELEASE_SECS, max_secs: float = MAX_SECS,
                 onset_max_secs: float = ONSET_MAX_SECS):
        super().__init__()
        self._merge = merge
        self._release_secs = max(0.0, release_secs)
        self._max_secs = max(self._release_secs, max_secs)
        self._onset_max_secs = min(self._max_secs, max(self._release_secs, onset_max_secs))
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
        # Bumped by every hold; a release or cap timer only acts on the hold it was
        # started for (a timer that wakes as its hold ends and a new one begins must
        # not end the new one).
        self._hold_gen = 0
        # No VAD start in this hold so far: it gets the onset-only cap.
        self._onset_only = False
        self._release_task = None
        self._cap_task = None
        # A session end arrived while holding: drop what is queued ahead of it.
        self._discard_to_end = False
        # A session end has reached the gate: no new hold starts behind it.
        self._ending = False
        # Queue arrival (pipeline clock, ns) of every frame, the per-context pts shift,
        # the context last seen (for frames that carry none), and when the last hold was
        # released (a frame that arrived before it was held).
        self._arrivals: dict = {}
        self._shift: dict = {}
        self._last_ctx = None
        self._released_ns = None
        # Tallies, for the log and the tests.
        self.holds = 0
        self.onset_only_holds = 0
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
        from the user aggregator's on_user_turn_message_added -- a push with the caller's
        words in it. (on_user_turn_inference_triggered also fires for an empty
        aggregation, which asks for no reply, and armed the gate for whatever spoke next:
        a consult follow-up, held and dropped, then left its delivery waiter guessing.)"""
        self._armed = True
        self._tool_call = False
        if self._speaking and not self._holding and not self._ending:
            # The commit landed while the caller is already talking (the onset test
            # often still hears them when the ceiling commits under a VAD stop).
            await self._start_hold("the caller is speaking at the commit",
                                   onset_only=not self._sources["vad"])

    async def on_onset(self, started: bool):
        """speech_onset.SpeechOnsetMixin listener: the caller's speech began / ended on
        the faster test. Called from the user aggregator's task."""
        await self._speech("onset", started)

    async def _speech(self, source: str, started: bool):
        self._sources[source] = started
        if started:
            self._cancel_release()
            if source == "vad" and self._holding and self._onset_only:
                # The VAD confirms it: the hold gets the full cap.
                self._onset_only = False
                self._schedule_cap()
            if self._armed and not self._holding and not self._ending:
                await self._start_hold("the caller started speaking before the reply played"
                                       f" ({'VAD' if source == 'vad' else 'speech onset'})",
                                       onset_only=source == "onset")
        elif self._holding and not self._speaking:
            self._schedule_release()

    # ------------------------------------------------------------------ frames

    async def queue_frame(self, frame: Frame, direction=FrameDirection.DOWNSTREAM,
                          callback=None):
        if not isinstance(frame, SystemFrame):
            try:
                self._arrivals[frame.id] = self.get_clock().get_time()
            except Exception:  # noqa: BLE001 -- no clock before setup; nothing to hold then
                pass
            if isinstance(frame, (EndFrame, StopFrame)):
                # The session is ending: no new hold may start behind its End, and a
                # hold in force ends now -- the held reply must not play into the
                # closing session, nor the End wait the hold out (Talk's close took
                # 5.7 s before this).
                self._ending = True
                if self._holding:
                    self._discard_to_end = True
                    await self._end_hold(released=False, why="the session is ending")
        await super().queue_frame(frame, direction, callback)

    def _restamp(self, frame) -> None:
        """Move a held TTS context's playout times by how long it waited here."""
        arrived = self._arrivals.pop(frame.id, None)
        ctx = getattr(frame, "context_id", None)
        if ctx is not None:
            self._last_ctx = ctx
        else:
            ctx = self._last_ctx
        if (arrived is not None and ctx is not None and self._released_ns is not None
                and arrived <= self._released_ns):
            # It arrived before the last release: the hold caught it. (A frame that came
            # through later did not wait here, but plays after the ones that did.)
            waited = self.get_clock().get_time() - arrived
            if (waited >= _HELD_NS and ctx not in self._shift
                    and isinstance(frame, (TTSAudioRawFrame, TTSTextFrame))):
                # The context's first held AUDIO or WORD fixes its shift: the TTS anchors
                # the context's word times at its first audio, so that is what the
                # release delays -- not the TTSStartedFrame, which opens the context
                # up to the engine's first-audio latency earlier (fixing the shift on it
                # overshot every word by that latency). The frames behind it play on
                # after it, whatever microseconds their own processing took.
                self._shift[ctx] = waited
                while len(self._shift) > _MAX_SHIFTS:
                    self._shift.pop(next(iter(self._shift)))
        if getattr(frame, "pts", None) and ctx in self._shift:
            frame.pts += self._shift[ctx]

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if self._discard_to_end and not isinstance(frame, SystemFrame):
            if isinstance(frame, (EndFrame, StopFrame)):
                self._discard_to_end = False
            elif not isinstance(frame, UninterruptibleFrame):
                self._arrivals.pop(frame.id, None)
                return  # the held reply, dropped on the way out
        if not isinstance(frame, SystemFrame):
            # Before super(): that is where the ledger reads the pts.
            self._restamp(frame)
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self._speech("vad", True)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._speech("vad", False)
        elif isinstance(frame, InterruptionFrame):
            # A user turn started: the caller's words arrived. pipecat's interruption
            # handling (super) has already dropped the queued frames; what is left is to
            # unpause and say why.
            await self._on_interruption()
        elif isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
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

    async def _start_hold(self, why: str, *, onset_only: bool):
        self._holding = True
        self._hold_gen += 1
        self._hold_t0 = asyncio.get_running_loop().time()
        self._onset_only = onset_only
        self.holds += 1
        if onset_only:
            self.onset_only_holds += 1
        await self.pause_processing_frames()
        logger.info(f"REPLY-HOLD: {why} -- holding the reply until their words arrive "
                    f"(or {self._release_secs:.1f}s after they stop without any)"
                    + (f" [onset-only hold #{self.onset_only_holds}]" if onset_only else ""))
        await self.push_frame(PlayoutHoldFrame(active=True), FrameDirection.UPSTREAM)
        self._schedule_cap()
        if not self._speaking:
            self._schedule_release()

    async def _end_hold(self, *, released: bool, why: str, gen: int | None = None):
        if not self._holding or (gen is not None and gen != self._hold_gen):
            return
        # Every state change, and the unpause, before the first await: nothing can
        # start a new hold half-way through ending this one.
        self._holding = False
        held = asyncio.get_running_loop().time() - self._hold_t0
        self._cancel_release()
        self._cancel_cap()
        if released:
            self.released += 1
            self._released_ns = self.get_clock().get_time()
        else:
            self.dropped += 1
        # Also after a drop: the interruption normally unpauses the queue, but not when
        # the frame in hand was Uninterruptible (pipecat then only resets the queue).
        await self.resume_processing_frames()
        if released:
            logger.info(f"REPLY-HOLD: released after {held:.2f}s ({why}) -- the reply plays")
        else:
            logger.info(f"REPLY-HOLD: dropped the held reply after {held:.2f}s ({why})")
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
        self._release_task = self.create_task(self._release_after(self._hold_gen),
                                              name="reply-hold-release")

    def _schedule_cap(self):
        self._cancel_cap()
        cap = self._onset_max_secs if self._onset_only else self._max_secs
        left = max(0.0, self._hold_t0 + cap - asyncio.get_running_loop().time())
        self._cap_task = self.create_task(self._cap(self._hold_gen, left, cap),
                                          name="reply-hold-cap")

    async def _release_after(self, gen: int):
        await asyncio.sleep(self._release_secs)
        await self._end_hold(released=True, gen=gen,
                             why=f"no words within {self._release_secs:.1f}s of the caller stopping")

    async def _cap(self, gen: int, left: float, cap: float):
        await asyncio.sleep(left)
        await self._end_hold(released=True, gen=gen,
                             why=f"{'onset-only ' if self._onset_only else ''}hold cap "
                                 f"{cap:.1f}s")

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
        if self.holds:
            logger.info(f"REPLY-HOLD summary: {self.holds} hold(s), {self.onset_only_holds} "
                        f"on the onset test alone; {self.released} released, "
                        f"{self.dropped} dropped")
        await super().cleanup()
