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
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterruptionFrame,
    LLMContextFrame,
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
# How long a turn-free window stays claimed by the waiter it was given to, at most. The
# claim ends when the claimant's completion starts (LLMFullResponseStartFrame, which
# follows its queued LLMRunFrame within milliseconds), at an interruption, or when the
# claimant lets it go; this only bounds a claimant that queued nothing and forgot.
_CLAIM_SECS = 3.0
# How long a waiter whose wait ran out goes on waiting for a claimed window's completion
# to finish before it goes ahead anyway (wait_out_claim).
_CLAIMANT_WAIT_SECS = 30.0

# The tag on every order the brain writes into the context as a user message (the
# consult follow-up's trigger, ClientNotes' reaction order). One constant: the model is
# told what it means once, and both injectors' orders must read the same.
SYSTEM_NOTICE_TAG = "[automated system notice, not spoken by the user]"


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


class TurnCommitMark(FrameProcessor):
    """Tells the gate a completion was just asked for, the moment it is.

    A committed turn (the user's words, or an injector's LLMRunFrame) reaches the LLM as
    an LLMContextFrame, and the gate cannot see that: it sits after the output transport,
    and the LLM consumes the frame. Until the completion's LLMFullResponseStartFrame gets
    to the gate, the gate would call the moment turn-free, and an injector could post
    after the user's unanswered words and queue a second completion. So each one claims
    the window (FollowupGate.turn_committed) until its completion starts.

    PLACEMENT — just above the LLM, below everything that edits the context on the way
    (the heard corrector, ClientNotes): the frame it sees is the one the LLM gets."""

    def __init__(self, gate: "FollowupGate"):
        super().__init__()
        self._gate = gate

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            self._gate.turn_committed()
        await self.push_frame(frame, direction)


class Delivery:
    """One reply being watched from the moment it is queued until it settles.

    The follow-up injector needs to know whether the caller got the answer, and the
    two outcomes have very different signals. A reply that is CUT is charted by the
    ledger straight away, with the fraction that played. A reply that finishes is not
    charted until something else happens: _window_closed advances playout from a layout
    anchored on BotStartedSpeaking, which lands ~0.2s after the audio really starts, so
    the estimate overshoots the end and the last fraction of a second is never
    accounted. The turn stays open until the NEXT BotStoppedSpeaking, which never comes
    while the caller is silent.

    Live 2026-09-10 17:37: a 27.9s delivery played in full, and the injector sat on the
    ledger for the whole 45s timeout before reporting "unknown". Harmless for the
    ledger's own consumer -- a fully heard turn has nothing to correct -- but it left
    the heard check inert on the quiet path, which is the common one.

    So completion comes from the transport's own frames, which are prompt and mean
    exactly what they say, and the ledger is consulted only for the fraction, only when
    there was a cut.
    """

    __slots__ = ("spoke", "interrupted", "done")

    def __init__(self):
        self.spoke = False
        self.interrupted = False
        self.done = asyncio.Event()

    def _settle(self, interrupted: bool) -> None:
        self.interrupted = interrupted
        self.done.set()


class FollowupGate(FrameProcessor):
    """Tracks whether the user is speaking, the bot is speaking, or the LLM is
    mid-response -- and, separately, whether a TURN is still in flight -- and lets
    an unprompted turn wait for a clear moment.

    Two waits, because two callers need different things across a tool call:

      * The consult narrator wants "nobody is speaking": a synchronous consult runs
        inside its tool call for up to 45s, and that silence is exactly the gap a
        progress line exists to fill. So _llm is released the moment a function
        call starts (the completion is done producing speech).
      * The follow-up injector wants "no turn in flight": appended during a tool
        call, its trigger is read by the tool's own answering completion, which
        then answers two things at once. So _turn stays set from the response's
        start until that answering completion ends, a result that runs no
        inference closes the turn, or an interruption kills it -- and only
        `wait_until_idle(turn_free=True)` waits for it.

    brain/formal/Followup.tla checks both sides (NoInterjectMidTurn and
    NoDeadAirDuringTool); this is its "turnAware" design.

    A turn-free window is CLAIMED by the waiter it is reported to. Two injectors wait
    for it (the consult follow-up and ClientNotes' reactions), and both woken by one
    window would each queue a completion: two replies back to back, and the first one's
    text retires BOTH one-shot triggers (FollowupTrigger fires everything armed), so
    the second never happens as intended. The claim is taken synchronously as the
    window is reported, holds the window shut for everyone else, and ends when the
    claimant's completion starts, at an interruption, on release_claim(), or after
    _CLAIM_SECS. A lone waiter sees no difference. A committed turn claims the window the
    same way (TurnCommitMark), so the gap between a commit and its completion's start is
    never reported as turn-free."""

    def __init__(self, quiet_secs: float = _QUIET_SECS, max_wait: float = _MAX_WAIT,
                 claim_secs: float = _CLAIM_SECS):
        super().__init__()
        self._quiet_secs = quiet_secs
        self._max_wait = max_wait
        self._claim_secs = claim_secs
        # A turn-free window was handed to a waiter whose completion has not started.
        self._claimed = False
        self._claim_timer = None
        self._user = False
        self._bot = False
        self._llm = False
        # A turn is in flight: a response started and neither its answering
        # completion has ended nor a no-inference result closed it. Survives the
        # function-call release of _llm above -- see the class docstring.
        self._turn = False
        # Set == conversation idle. Starts idle (nobody has spoken yet).
        self._idle = asyncio.Event()
        self._idle.set()
        # The complement, so a caller can wait for activity to START as well as stop.
        self._busy = asyncio.Event()
        # Set == idle AND no turn in flight: what the follow-up injector waits for.
        self._clear = asyncio.Event()
        self._clear.set()
        # Set == no window claimed and no turn in flight, busy or not (wait_out_claim).
        self._settled = asyncio.Event()
        self._settled.set()
        # Replies being watched to completion — see Delivery and watch_delivery.
        self._deliveries: list = []
        # For the session arbiter's view of a room mic without wake words: whether the
        # user has taken a turn at all, and when anyone (user or bot) last spoke or the
        # model last answered (time.monotonic(); refreshed while that is still going on).
        self.user_heard = False
        self.last_active = time.monotonic()
        # Async consult answers still on their way (tools._consult_and_followup).
        self.owed = 0
        self._was_busy = False
        # Clear == the conversation is on hold for a phone call (AgentSession.hold): no
        # window opens while it is, and a turn-free waiter (a consult answer, a client
        # note's reaction) waits it out rather than speaking into a held conversation.
        self._unheld = asyncio.Event()
        self._unheld.set()

    @property
    def held(self) -> bool:
        return not self._unheld.is_set()

    def set_held(self, held: bool) -> None:
        """On hold for a phone call, or back (AgentSession.hold / resume)."""
        if held:
            self._unheld.clear()
        else:
            self._unheld.set()
        self._refresh()

    @property
    def quiet_secs(self) -> float:
        """The debounce: how long a quiet moment must last to count as a window."""
        return self._quiet_secs

    def is_clear(self) -> bool:
        """Idle, no turn in flight and no window claimed, right now -- undebounced, so a
        snapshot for reporting, not a moment to act in (wait_until_idle(turn_free=True)
        is that)."""
        return self._clear.is_set()

    def _claim(self) -> None:
        self._claimed = True
        if self._claim_timer is not None:
            self._claim_timer.cancel()
        self._claim_timer = asyncio.get_running_loop().call_later(
            self._claim_secs, self.release_claim)
        self._refresh()

    def turn_committed(self) -> None:
        """A completion was just asked for (TurnCommitMark): hold the window shut until
        it starts, as a claimant's is."""
        self._claim()

    async def wait_out_claim(self, max_wait: float = _CLAIMANT_WAIT_SECS) -> None:
        """For a waiter whose wait_until_idle ran out and that goes ahead anyway: wait
        (bounded) until no window is claimed AND no completion is running, so its own
        trigger is not armed while another completion is under way. That completion's
        first text would retire every armed one-shot (FollowupTrigger fires them all),
        this waiter's included, unread. A claim ends when its completion STARTS, so
        waiting only while one is claimed is not enough. Speech alone does not hold it:
        a waiter that ran out has already given up on a quiet moment."""
        if self._settled.is_set():
            return
        try:
            await asyncio.wait_for(self._settled.wait(), timeout=max_wait)
        except asyncio.TimeoutError:
            logger.info("followup_gate: the turn in flight outlasted the wait")

    def release_claim(self) -> None:
        """Give back a window wait_until_idle(turn_free=True) reported, for a claimant
        that queues no completion after all. Idempotent."""
        if self._claim_timer is not None:
            self._claim_timer.cancel()
            self._claim_timer = None
        if self._claimed:
            self._claimed = False
            self._refresh()

    def watch_delivery(self) -> Delivery:
        """Watch the reply about to be queued, until it finishes playing or is cut.

        Arm it BEFORE queueing the turn: an interruption can land between the queue
        and the first audio, and that reply was never heard either.
        """
        d = Delivery()
        self._deliveries.append(d)
        return d

    def drop_delivery(self, d: Delivery) -> None:
        """Stop watching a reply that will never come — its turn was flushed before any
        completion read the trigger, so nothing will start or cut it."""
        if d in self._deliveries:
            self._deliveries.remove(d)

    def _settle_deliveries(self, interrupted: bool, *, only_if_spoke: bool = False):
        for d in list(self._deliveries):
            if only_if_spoke and not d.spoke:
                continue        # not our reply yet: the transport is still draining
            d._settle(interrupted)
            self._deliveries.remove(d)

    def _refresh(self):
        busy = self._user or self._bot or self._llm or self.held
        if busy or self._was_busy:
            self.last_active = time.monotonic()
        self._was_busy = busy
        if busy:
            self._idle.clear()
            self._busy.set()
        else:
            self._idle.set()
            self._busy.clear()
        if busy or self._turn or self._claimed:
            self._clear.clear()
        else:
            self._clear.set()
        if self._turn or self._claimed:
            self._settled.clear()
        else:
            self._settled.set()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            self._user = True
            self.user_heard = True
            self._refresh()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user = False
            self._refresh()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot = True
            for d in self._deliveries:
                d.spoke = True
            self._refresh()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot = False
            # The transport stops speaking once its queue has drained, so a watched
            # reply that had started is now fully played. One that has not started yet
            # belongs to a later turn and keeps waiting.
            self._settle_deliveries(interrupted=False, only_if_spoke=True)
            self._refresh()
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._llm = True
            self._turn = True
            # A claimant's completion has started; _turn holds the window shut from here.
            self.release_claim()
            self._refresh()
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._llm = False
            self._turn = False
            self._refresh()
        elif isinstance(frame, FunctionCallResultFrame):
            # A result that runs no inference (tools.no_inference(): the async
            # ask_openclaw placeholders) is the end of the turn -- nothing will
            # answer from it. Any other result is followed by the answering
            # completion, whose start/end frames carry the turn from here. The LLM
            # service BROADCASTS the result frame (llm_service.broadcast_frame, both
            # directions) and this gate sits before the assistant aggregator, so the
            # frame is seen. getattr for both fields:
            # run_llm is the legacy duplicate of properties.run_llm.
            props = getattr(frame, "properties", None)
            if (getattr(frame, "run_llm", None) is False
                    or (props is not None and getattr(props, "run_llm", None) is False)):
                self._turn = False
                self._refresh()
        elif isinstance(frame, InterruptionFrame):
            # An interruption kills the in-flight response by definition, and
            # discards any end frame the TTS service was holding.
            self._llm = False
            self._turn = False
            # Every watched reply is cut — including one queued but not yet speaking,
            # which the caller heard none of. A claimant's queued completion is flushed
            # with it, so its claim goes too.
            self._settle_deliveries(interrupted=True)
            self.release_claim()
            self._refresh()
        elif isinstance(frame, FunctionCallInProgressFrame):
            # The end frame is NOT guaranteed to arrive: for a completion that
            # produced no synthesizable text (a bare tool call), the TTS service
            # holds LLMFullResponseEndFrame waiting for an audio context that empty
            # text never creates, and an interruption discards a held one outright.
            # Either way _llm stayed latched and the gate read "mid-response"
            # forever — every narrator line was skipped and the follow-up injector
            # burned its full max_wait, i.e. total dead air in genuine silence. A
            # function call starting means the completion is done producing speech
            # (any audio it did produce is tracked by _bot), and an interruption
            # kills the in-flight response by definition.
            #
            # The call's RESULT is deliberately not waited for HERE. The agent-first
            # consult runs synchronously inside the call for up to 45s, and that
            # silence is the very gap the narrator exists to fill. What the release
            # must not do is let the follow-up injector append its trigger into a
            # turn whose answering completion has not started -- read by that
            # completion, the trigger and the tool result get answered together.
            # That is _turn's job (class docstring): it outlives this release and
            # only the injector's wait requires it clear. brain/formal/Followup.tla
            # found the interleaving in 7 steps (LATCH = "clearedOnToolCall",
            # invariant NoInterjectMidTurn).
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

    async def wait_until_idle(self, max_wait: float | None = None, *,
                              turn_free: bool = False) -> bool:
        """Block until a debounced quiet window. Returns True at a genuine window --
        idle, and STILL idle a full quiet_secs later -- and False when max_wait ran
        out first, including when the budget left could not hold a full quiet
        window: a pause shorter than the debounce is exactly what the debounce
        exists to reject, so it is never reported as a window. `max_wait` overrides
        the instance default for this call -- the consult narrator passes a short
        one so it fits into a gap or gives up quickly, where the follow-up injector
        wants the long default and delivers on False regardless. `turn_free=True`
        additionally waits for no TURN to be in flight (see the class docstring) --
        the follow-up injector's setting; the narrator leaves it False so it can
        speak during a tool call's silence. A True with turn_free=True also CLAIMS the
        window (class docstring): the caller queues its completion, or calls
        release_claim() if it decides not to. Cancellation propagates (session
        teardown).

        The budget is measured against one deadline throughout. It used to be a
        `remaining` computed BEFORE the idle wait and reused to bound the debounce
        after it, so a call could overrun max_wait by a whole quiet window -- or,
        with little budget left, sleep a fraction of the window and report a
        mid-utterance pause as a clear moment."""
        max_wait = self._max_wait if max_wait is None else max_wait
        ev = self._clear if turn_free else self._idle
        deadline = time.monotonic() + max_wait
        while True:
            if turn_free and self.held:
                # On hold for a phone call: the time it lasts is not the conversation's,
                # and what is waiting goes in once it is back (set_held).
                held_at = time.monotonic()
                await self._unheld.wait()
                deadline += time.monotonic() - held_at
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.info("followup_gate: max wait reached with no quiet window")
                return False
            try:
                await asyncio.wait_for(ev.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                if turn_free and self.held:
                    continue  # put on hold while it waited: wait the hold out
                logger.info("followup_gate: max wait reached with no quiet window")
                return False
            # Idle right now -- require it to STAY idle through the debounce so we
            # don't jump into a brief pause between the user's (or bot's) phrases.
            if deadline - time.monotonic() < self._quiet_secs:
                logger.info("followup_gate: max wait reached before a full quiet window")
                return False
            await asyncio.sleep(self._quiet_secs)
            if ev.is_set():
                if turn_free:
                    # No await between the check and the claim: one window, one waiter.
                    self._claim()
                return True
            # Someone resumed during the debounce -- wait for the next window.
