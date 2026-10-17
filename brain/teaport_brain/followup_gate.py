#
# followup_gate.py — "is it a good moment to speak?" gate for unprompted turns.
#
# The brain says some things nobody asked for at that moment: an async consult's answer
# as it lands (owed.py), a client note's reaction, the consult narrator's progress
# lines. Firing those blindly steps on whoever is talking — the user mid-utterance, OR
# the assistant itself mid-answer about something else (the LLM is generating / the bot
# is still speaking a prior turn).
#
# This processor tracks conversation activity from three signals and exposes
# wait_until_idle(), which resolves at the next DEBOUNCED quiet window — both sides
# silent AND no LLM response in flight, held for a short beat so a mid-thought pause
# doesn't count — or after a max wait.
#
# Placed right after transport.output(): the output transport pushes
# Bot{Started,Stopped}SpeakingFrame downstream (as well as upstream), and the user /
# LLM frames propagate downstream through the whole pipeline, so all three activity
# signals are visible at that one spot.
#
# THE ANSWER AFTER A TOOL (#17). The gate is also the one door for the completion a sync
# tool's result asks for. pipecat's LLMAssistantAggregator pushes it UPSTREAM as an
# LLMContextFrame once the result lands (deferred to BotStoppedSpeaking while the bot is
# audible), and only `if not self._user_speaking` -- the user TURN, which here starts
# only once the caller's words are transcribed (MinWords). A caller who has started
# talking again (the VAD) without words yet gets the answer asked under them, against a
# context without what they are saying; the TTS holds it to their VAD stop, it plays
# there, their words then cut it, and their turn asks the model again. The gate sits
# directly above that aggregator's push, so it HOLDS the frame while caller speech is
# pending -- from its VAD start until a turn starts with the caller quiet (then it is
# DROPPED: that turn's commit reads the result, already in the context), until a commit
# of the caller's words has taken that speech's (its VAD stop made before the commit, and
# that stop's close no longer owed), or until RELEASE_SECS after the VAD stop with no
# turn (a cough: it goes on). Nothing is held on speech whose words are in: with nobody
# talking, the answer is asked at once. A blip costs a release window too: see
# formal/README.md, "A blip costs a release window".
# brain/formal/UserTurn.tla, POSTTOOL = "gate"; the rejected designs are rows there too.
#
# THE RUN (#203). The brain's own unprompted request -- an async consult's answer as it
# lands, or its one re-offer at a long lull (owed.py) -- goes through the same door:
# ask() puts it in a slot of its own beside the post-tool answer's, so neither displaces
# the other (formal/Owed.tla, HoldNoLoss; ow_oneSlot is the single slot). It is held
# while caller speech is pending, as the answer is, but a run that reaches the hold cap
# is DROPPED, never sent (room TV noise would otherwise make it speak over whatever kept
# the VAD up; Owed.tla Cap). Out of the hold it waits for a window that is turn-free as
# well (no turn open, no committed words unanswered, no tool round out -- a call put
# out with a line before it included, endClearsTool -- nothing claimed: ow_turnLevel and
# ow_vadLevel are each half alone, #197), the gate claims the window itself --
# TurnCommitMark never sees a frame pushed up from here -- and pushes it UP to the LLM as
# a RunFrame, an LLMContextFrame the LLM answers whatever its direction
# (test_pipecat_premises.py). Where it renders, owed.py asks again whether its records
# are still owed (a post-tool answer released ahead of it may have told them:
# ow_askOnly); a run whose records were heard sends no request.
# A caller turn drops a waiting run: its own request carries the note (Owed.tla
# TurnStart).
#
import asyncio
import time
from dataclasses import dataclass

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    ControlFrame,
    StopFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.reply_hold import MAX_SECS as _ANSWER_MAX_SECS
from teaport_brain.reply_hold import RELEASE_SECS as _ANSWER_RELEASE_SECS
from teaport_brain.settings import setting

# How long the conversation must stay quiet before a window counts (rejects
# mid-thought pauses and between-turn gaps).
_QUIET_SECS = setting("TEAPORT_FOLLOWUP_QUIET_S")
# Ceiling on how long to hold an answer waiting for a gap; past this, speak anyway.
_MAX_WAIT = setting("TEAPORT_FOLLOWUP_MAX_WAIT_S")
# How long a turn-free window stays claimed by the waiter it was given to, at most. The
# claim ends when the claimant's completion starts (LLMFullResponseStartFrame, which
# follows its queued LLMRunFrame within milliseconds), at an interruption, or when the
# claimant lets it go; this only bounds a claimant that queued nothing and forgot.
_CLAIM_SECS = 3.0

# The tag on every message the brain writes as a user message that nobody said (owed.py's
# note, ClientNotes' notices and reaction order). One constant: the model is told what it
# means once, and every one must read the same.
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
    """Retires a one-shot context order the instant the completion that READ it starts
    answering from it: ClientNotes' reaction order ("React now ... to this app event").

    Such an order is a STANDING ORDER in the context. Retire it too early and the
    reaction never happens; too late and a later turn re-executes it. Both were live
    incidents when the consult follow-up was delivered this way (it is not any more:
    owed.py renders its note into each request and stores nothing). The only sound
    retirement point is one causally tied to a completion having read the order --
    brain/formal/Followup.tla, MODE = "retireOnRead" -- which is why this is a processor
    and not a timer on FollowupGate.

    PLACEMENT — directly below the LLM, and it cannot move:
      * FollowupGate sits after transport.output(), and LLMTextFrame never gets
        there: TTSService consumes text frames (`push_text_frames=False`, see
        engine_tts.py) rather than forwarding them.
      * The signal is the first LLMTextFrame, NOT LLMFullResponseStartFrame.
        pipecat pushes the start frame BEFORE `_process_context` serializes the
        context into the request (pipecat/services/openai/base_llm.py — push then
        `await self._process_context(...)`), so retiring there neutralises the
        order in place before the model ever sees it. The first LLMTextFrame is the
        earliest point at which the request provably carried it and the model is
        answering from it.

    An armed shot stays armed through an interruption: a reaction flushed by a barge-in
    is read by the caller's own turn, which reacts in the same reply (client_notes.py).
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


@dataclass
class RunFrame(LLMContextFrame):
    """The brain's own unprompted request (owed.py): pushed UP from FollowupGate to the
    LLM, which answers an LLMContextFrame whatever its direction. `keys`: the owed records
    it is for; `purpose`: "run" (the answer as it lands) or "reoffer" (the one re-offer
    at a long lull). The LLM service renders it with the note at the tail, the run line
    first (services.BoundedOpenAILLMService)."""

    keys: tuple = ()
    purpose: str = "run"


@dataclass
class RunSilentFrame(ControlFrame):
    """A run's completion that will say nothing (it called stay_silent, or it came back
    empty): pushed down by the LLM service so the gate ends the turn it opened. The TTS
    holds the End of a completion with no text until a later context ends
    (test_pipecat_premises.py), so without this the gate would read a turn in flight
    until the next interruption. A ControlFrame, not a SystemFrame: it must reach the
    gate BEHIND the completion's LLMFullResponseStartFrame, which a SystemFrame
    overtakes in the queues between them (and the Start would then open the turn
    again for good)."""


class TurnCommitMark(FrameProcessor):
    """Tells the gate a completion was just asked for, the moment it is.

    A committed turn (the user's words, or a client note's LLMRunFrame) reaches the LLM as
    an LLMContextFrame, and the gate cannot see that: it sits after the output transport,
    and the LLM consumes the frame. Until the completion's LLMFullResponseStartFrame gets
    to the gate, the gate would call the moment turn-free, and a run or a note could go
    after the user's unanswered words as a second completion. So each one claims the
    window (FollowupGate.turn_committed) until its completion starts.

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
    """A line the session speaks itself, watched from the moment it is queued until it
    settles: say_last_line, resume's "where were we?", the call prompt's question
    (AgentSession, call_prompt.py). Completion comes from the transport's own frames,
    which are prompt and mean exactly what they say: BotStoppedSpeaking once it spoke,
    or any interruption. Any audio counts as it speaking -- the line may well be
    append_to_context=False itself.

    (The consult follow-up's watch of a model REPLY was here too, with the reply's own
    audio telling it from a filler's. A reply's end now reaches owed.py through the
    transcript ledger, which charts each reply by its own audio; see owed.py.)
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
      * What the brain adds or says on its own wants "no turn in flight": ClientNotes'
        notes and reaction, appended during a tool call, are read by the tool's own
        answering completion, which then answers two things at once, and a run
        (owed.py) asked there would speak between a call and its answer. So _turn
        stays set from the response's start until that answering completion ends, a
        result that runs no inference closes the turn, or an interruption kills it --
        and only `wait_until_idle(turn_free=True)` and the run's door wait for it.

    brain/formal/Followup.tla checks both sides (NoInterjectMidTurn and
    NoDeadAirDuringTool); this is its "turnAware" design. Owed.tla's NoRunMidTurn is the
    run's half (ow_vadLevel).

    A turn-free window is CLAIMED by the waiter it is reported to. Two wait for it (the
    run's door and ClientNotes' reactions), and both woken by one window would each send
    a completion: two replies back to back. The claim is taken synchronously as the
    window is reported, holds the window shut for everyone else, and ends when the
    claimant's completion starts, at an interruption, on release_claim(), or after
    _CLAIM_SECS. A lone waiter sees no difference. A committed turn claims the window the
    same way (TurnCommitMark), so the gap between a commit and its completion's start is
    never reported as turn-free."""

    def __init__(self, quiet_secs: float = _QUIET_SECS, max_wait: float = _MAX_WAIT,
                 claim_secs: float = _CLAIM_SECS,
                 answer_release_secs: float = _ANSWER_RELEASE_SECS,
                 answer_max_secs: float = _ANSWER_MAX_SECS):
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
        # Set == idle AND no turn in flight AND nothing claimed: what a turn-free waiter
        # (the run's door, ClientNotes) waits for.
        self._clear = asyncio.Event()
        self._clear.set()
        # Lines being watched to completion — see Delivery and watch_delivery.
        self._deliveries: list = []
        # For the session arbiter's view of a room mic without wake words: whether the
        # user has taken a turn at all, and when anyone (user or bot) last spoke or the
        # model last answered (time.monotonic(); refreshed while that is still going on).
        self.user_heard = False
        self.last_active = time.monotonic()
        # Async consults still running (tools._consult_and_land).
        self.consults_running = 0
        # What the run's door reports (owed.Owed wires them): a caller turn started
        # (Owed.tla TurnStart arms the re-offer), a run dropped at the hold cap (keys),
        # the session's end (the gate's cleanup).
        self.on_caller_turn = None
        self.on_run_dropped = None
        self.on_session_end = None
        self._was_busy = False
        # Clear == the conversation is on hold for a phone call (AgentSession.hold): no
        # window opens while it is, and a turn-free waiter (a consult answer, a client
        # note's reaction) waits it out rather than speaking into a held conversation.
        self._unheld = asyncio.Event()
        self._unheld.set()
        # The answer after a tool (module header, #17). Caller speech is PENDING from its
        # VAD start until a turn starts or the release timer runs out after its VAD stop:
        # its words may still come. The answer's push is held while it is, and so is a run.
        self._answer_release_secs = max(0.0, answer_release_secs)
        self._answer_max_secs = max(self._answer_release_secs, answer_max_secs)
        self._vad = False
        self._speech_pending = False
        self._speech_since = None      # the id of the VAD start that made it pending
        self._speech_stop = None       # the id of the last VAD stop
        self._speech_timer = None
        self._held_answer = None
        self._answer_cap = None
        self.answers_held = 0
        self.answers_dropped = 0
        self.answers_released = 0
        # Set == no caller speech pending (not _vad, not _speech_pending): what a held
        # run waits for.
        self._speech_clear = asyncio.Event()
        self._speech_clear.set()
        # The run's slot (module header, THE RUN): (keys, purpose, context, task), or
        # None. Its own, beside the post-tool answer's _held_answer: HoldNoLoss.
        self._run = None
        self.runs_dropped = 0
        # A function call is out and nothing has answered it yet: set by its
        # FunctionCallInProgressFrame, cleared by the answering completion's start, a
        # result that runs no inference, or an interruption. _turn alone does not cover
        # it: a completion with text AND a call ("Let me check." + get_weather) ends --
        # its End reaches here -- while the call is out (formal/Owed.tla, endClearsTool).
        self._tool_round = False
        # A commit's LLMContextFrame passed TurnCommitMark and the LLM has not rendered it
        # yet (request_rendered): with _user, "a caller request is still to be rendered"
        # (owed.Owed.land; Owed.tla Land).
        self._commit_unrendered = False
        # When the line was last not quiet (quiet_for): the re-offer's lull.
        self._last_unquiet = time.monotonic()

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
        is that). MemoryReclaim acts on it, with its own debounce (last_active): it must
        not claim a window, which would hold off a run or a note waiting for one."""
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
        it starts, as a claimant's is. Until the LLM renders it (request_rendered), it is
        a caller request still to be rendered (caller_request_pending)."""
        self._commit_unrendered = True
        self._claim()

    def request_rendered(self) -> None:
        """The LLM is rendering a request that came down to it (services.py): the note
        goes into it from here, so it no longer stops a landing run."""
        self._commit_unrendered = False

    @property
    def caller_request_pending(self) -> bool:
        """A caller request is still to be RENDERED: their turn is open, or committed and
        not yet asked. A landing run is skipped then, the request carrying the note
        instead; one already rendered (in flight without the note) does not stop it, the
        run waits behind it (Owed.tla Land; DESIGN v3 #7)."""
        return self._user or self._commit_unrendered

    def words_committed(self, marker: int) -> None:
        """The caller's words are being committed (CutReplyFence.on_words_committed: every
        commit of them, the stop strategy's, the turn's stop and the watchdog's alike).
        `marker` is numbered off pipecat's frame counter at the commit, in the user
        aggregator's own task.

        A commit takes the words of speech that has STOPPED -- a segment's words are
        final once its VAD stop has flushed it (formal/SttCommit.tla's receipts) -- so
        pending speech whose VAD stop was made before the commit is not pending any
        more (a turn of two segments, "what's the time ... in Paris": the second VAD
        start came inside the open turn and met no turn start of its own). Speech still
        going on at the commit is not in it, nor speech begun after it, however its
        frames and this call are ordered on arrival: "before" is by frame number.
        brain/formal/UserTurn.tla, PendCommitted (gateNoCommit: no clear; gateById: a
        clear on the VAD START older than the commit, which a commit made while the
        caller still talked emptied)."""
        if (self._speech_pending and self._speech_since is not None
                and self._speech_stop is not None
                and self._speech_since < self._speech_stop < marker):
            self._speech_pending = False
            self._cancel(self._speech_timer)
            self._speech_timer = None
            self._speech_changed()

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
        """Watch the line about to be queued, until it finishes playing or is cut.

        Arm it BEFORE queueing the line: an interruption can land between the queue
        and the first audio, and that line was never heard either.
        """
        d = Delivery()
        self._deliveries.append(d)
        return d

    def drop_delivery(self, d: Delivery) -> None:
        """Stop watching a line that will never settle (its wait ran out)."""
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
        if busy or self._turn or self._claimed or self._tool_round:
            self._clear.clear()
        else:
            self._clear.set()
        self._mark_unquiet()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.UPSTREAM:
            # The assistant aggregator, below, asking for the answer to a tool's result.
            # Nothing else sends a context frame up past here: the aggregator also does
            # for an LLMRunFrame or an LLMMessages*Frame(run_llm) that reaches it, but
            # every one the brain sends enters at the top, where the user aggregator
            # takes it (test_pipecat_premises.py, check_run_frames_stop_at_the_user_
            # aggregator; test_followup_gate.py pins where the brain sends them).
            if self._user:
                # A turn is open: its commit reads the result. pipecat would not have
                # pushed this had the turn reached the aggregator before the push left.
                self.answers_dropped += 1
                logger.info("FOLLOWUP-GATE: dropped the answer to the tool's result -- the "
                            "caller's turn is open, and its commit reads the result")
                return
            if self._vad or self._speech_pending:
                self._hold_answer(frame)
                return
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad = True
            self._speech_pending = True
            self._speech_since = frame.id
            self._cancel(self._speech_timer)
            self._speech_timer = None
            self._speech_changed()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad = False
            self._speech_stop = frame.id
            if self._speech_pending:
                self._cancel(self._speech_timer)
                self._speech_timer = self.create_task(self._speech_over(),
                                                      name="followup-gate-speech")
            self._speech_changed()
        elif isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            self._drop_answer(None)
            self._drop_run(None)
            self._cancel(self._speech_timer)
            self._speech_timer = None
        if isinstance(frame, UserStartedSpeakingFrame):
            self._user = True
            self.user_heard = True
            if not self._vad:
                # Its words came: nothing of that speech is pending any more. A turn
                # that starts on an interim while the caller is still talking has not had
                # the rest of their words: those stay pending until a commit takes them
                # (words_committed) or the release timer runs out after the VAD stop.
                self._speech_pending = False
                self._cancel(self._speech_timer)
                self._speech_timer = None
                self._speech_changed()
            # A caller turn (Owed.tla TurnStart): a run waiting at the door is dropped --
            # this turn's request carries the note -- and the re-offer is armed.
            self._drop_run("the caller's turn started; its request carries the note")
            if self.on_caller_turn is not None:
                self.on_caller_turn()
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
            # line that had started is now fully played. One that has not started yet
            # belongs to a later turn and keeps waiting.
            self._settle_deliveries(interrupted=False, only_if_spoke=True)
            self._refresh()
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._llm = True
            self._turn = True
            self._tool_round = False   # the answering completion (or a new one) is here
            # A claimant's completion has started; _turn holds the window shut from here.
            self.release_claim()
            self._refresh()
        elif isinstance(frame, (LLMFullResponseEndFrame, RunSilentFrame)):
            # RunSilentFrame: a run's completion that says nothing (stay_silent, or
            # empty); its End is held at the TTS (RunSilentFrame's docstring).
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
                self._tool_round = False
                self._refresh()
        elif isinstance(frame, InterruptionFrame):
            # A held answer goes with it: the turn behind the interruption commits, and
            # that commit reads the result. (Only what is held here: a push already past
            # is the LLM's to cancel, or the reset's to drop while still queued.)
            self._drop_answer("the caller's words started a turn, whose commit reads "
                              "the result")
            # An interruption kills the in-flight response by definition, and
            # discards any end frame the TTS service was holding.
            self._llm = False
            self._turn = False
            self._commit_unrendered = False   # a commit still queued is flushed with it
            self._tool_round = False          # a sync call is cancelled
            # Every watched line is cut — including one queued but not yet speaking,
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
            # forever — every narrator line was skipped and what waited for a turn-free
            # window waited out its full max_wait, i.e. total dead air in genuine silence. A
            # function call starting means the completion is done producing speech
            # (any audio it did produce is tracked by _bot), and an interruption
            # kills the in-flight response by definition.
            #
            # The call's RESULT is deliberately not waited for HERE. The agent-first
            # consult runs synchronously inside the call for up to 45s, and that
            # silence is the very gap the narrator exists to fill. What the release
            # must not do is let a turn-free waiter (ClientNotes, the run's door) go
            # into a turn whose answering completion has not started -- read by that
            # completion, its message and the tool result get answered together.
            # That is _turn's job (class docstring): it outlives this release and
            # only the turn-free waits require it clear. brain/formal/Followup.tla
            # found the interleaving in 7 steps (LATCH = "clearedOnToolCall",
            # invariant NoInterjectMidTurn); Owed.tla's ow_vadLevel is the run's.
            self._llm = False
            self._tool_round = True
            self._refresh()
        self._mark_unquiet()
        await self.push_frame(frame, direction)

    # ------------------------------------------------------------ the answer after a tool

    @staticmethod
    def _cancel(task) -> None:
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _hold_answer(self, frame: LLMContextFrame) -> None:
        # One push per held answer: a newer one asks the same context frame's question
        # with every result in it, so it replaces the older.
        first = self._held_answer is None
        self._held_answer = frame
        if first:
            self.answers_held += 1
            self._answer_cap = self.create_task(self._answer_capped(),
                                                name="followup-gate-answer-cap")
            logger.info("FOLLOWUP-GATE: holding the answer to the tool's result -- the "
                        "caller is talking, and their words may take the turn")

    def _drop_answer(self, why: str | None) -> None:
        self._cancel(self._answer_cap)
        self._answer_cap = None
        if self._held_answer is not None:
            self._held_answer = None
            if why is not None:
                self.answers_dropped += 1
                logger.info(f"FOLLOWUP-GATE: dropped the held answer -- {why}")

    async def _release_answer(self, why: str) -> None:
        frame, self._held_answer = self._held_answer, None
        self._cancel(self._answer_cap)
        self._answer_cap = None
        if frame is not None:
            self.answers_released += 1
            logger.info(f"FOLLOWUP-GATE: the held answer goes on ({why})")
            await self.push_frame(frame, FrameDirection.UPSTREAM)

    async def _speech_over(self) -> None:
        """The release timer: the caller went quiet and no words started a turn."""
        await asyncio.sleep(self._answer_release_secs)
        self._speech_timer = None
        self._speech_pending = False
        self._speech_changed()
        if not self._vad:
            await self._release_answer(f"no words within {self._answer_release_secs:.1f}s "
                                       "of the caller stopping")

    async def _answer_capped(self) -> None:
        await asyncio.sleep(self._answer_max_secs)
        self._answer_cap = None
        if self._user:
            # A turn has opened (its start is here, its interruption not yet): its
            # commit reads the result. Released, the answer would be a second completion.
            self._drop_answer(f"hold cap {self._answer_max_secs:.1f}s with the caller's "
                              "turn open, whose commit reads the result")
            return
        await self._release_answer(f"hold cap {self._answer_max_secs:.1f}s")

    async def cleanup(self):
        self._drop_answer(None)
        self._drop_run(None)
        self._cancel(self._speech_timer)
        if self.on_session_end is not None:
            # The session is over: a consult still running cannot report back to it.
            self.on_session_end()
        await super().cleanup()

    # ------------------------------------------------------------ the run (#203)

    def _speech_changed(self) -> None:
        if self._vad or self._speech_pending:
            self._speech_clear.clear()
        else:
            self._speech_clear.set()
        self._mark_unquiet()

    def _quiet(self) -> bool:
        """Owed.tla's Quiet: no caller speech pending, no turn, no request or reply in
        flight, no tool round, nothing claimed, held or waiting at the door."""
        return (self._clear.is_set() and not self._vad and not self._speech_pending
                and self._held_answer is None and self._run is None)

    def _mark_unquiet(self) -> None:
        if not self._quiet():
            self._last_unquiet = time.monotonic()

    def quiet_for(self) -> float:
        """How long the line has been quiet (owed.py's lull: the re-offer waits for
        REOFFER_QUIET_S of it), 0 while it is not."""
        if not self._quiet():
            self._last_unquiet = time.monotonic()
            return 0.0
        return time.monotonic() - self._last_unquiet

    def _door_open(self) -> bool:
        """The run's window (Owed.tla Window): VAD-level AND turn-level -- no caller
        speech pending, no turn open, no request or reply in flight, no tool round out
        (_turn, _tool_round), nothing claimed (a commit not yet answered: TurnCommitMark; a client
        note's reaction), nothing held for a phone call, the post-tool answer's slot
        empty (it goes first)."""
        return (self._clear.is_set() and not self._vad and not self._speech_pending
                and self._held_answer is None and not self.held)

    def ask(self, keys, purpose: str, context) -> None:
        """gate.ask (Owed.tla Ask): a run of ours for the owed records `keys`, "run" or
        "reoffer", on `context`. One run waits at a time: one already waiting takes the
        new keys too (it renders every owed record anyway)."""
        keys = list(keys)
        if self._run is not None:
            old_keys, old_purpose, _, task = self._run
            merged = old_keys + [k for k in keys if k not in old_keys]
            # The landing run's line over the re-offer's: something new to tell.
            purpose = "run" if "run" in (old_purpose, purpose) else purpose
            self._run = (merged, purpose, context, task)
            return
        try:
            task = self.create_task(self._run_door(), name="followup-gate-run")
        except Exception as e:  # noqa: BLE001 -- the pipeline is gone (the session ended)
            logger.info(f"FOLLOWUP-GATE: no run for {keys}: the session is over ({e!r})")
            return
        self._run = (keys, purpose, context, task)
        self._mark_unquiet()

    def _drop_run(self, why: str | None) -> None:
        run, self._run = self._run, None
        if run is None:
            return
        self._cancel(run[3])
        if why is not None:
            self.runs_dropped += 1
            logger.info(f"FOLLOWUP-GATE: dropped the {run[1]} for {run[0]} -- {why}")

    async def _run_door(self) -> None:
        held_since = None
        while self._run is not None:
            if self.held:
                await self._unheld.wait()           # on hold for a phone call
                continue
            if self._vad or self._speech_pending:
                # The #198 hold, at the VAD level: their words may take the turn.
                if held_since is None:
                    held_since = time.monotonic()
                    logger.info(f"FOLLOWUP-GATE: holding the {self._run[1]} -- the caller "
                                "is talking, and their words may take the turn")
                left = self._answer_max_secs - (time.monotonic() - held_since)
                try:
                    await asyncio.wait_for(self._speech_clear.wait(), max(0.0, left))
                except asyncio.TimeoutError:
                    # Dropped at the cap, never sent (Owed.tla Cap): room noise would
                    # hold it until it spoke over whatever kept the VAD up.
                    keys = self._run[0]
                    self._drop_run(f"hold cap {self._answer_max_secs:.1f}s with the "
                                   "caller still talking")
                    if self.on_run_dropped is not None:
                        self.on_run_dropped(keys)
                    return
                continue
            held_since = None
            if not self._clear.is_set():
                await self._clear.wait()
                continue
            # Debounced, as every turn-free window here is: a mid-thought pause is not one.
            await asyncio.sleep(self._quiet_secs)
            if self._run is None or not self._door_open():
                continue
            # Whether its records are still owed is asked where it renders (owed.Owed.view):
            # in the quiet window nothing comes between this go and that render.
            keys, purpose, context, _ = self._run
            self._run = None
            # No await between the check and the claim: one window, one waiter.
            self._claim()
            logger.info(f"FOLLOWUP-GATE: the {purpose} for {keys} goes")
            await self.push_frame(RunFrame(context=context, keys=tuple(keys), purpose=purpose),
                                  FrameDirection.UPSTREAM)
            return

    async def wait_until_delivered(self, start_timeout: float = 10.0) -> None:
        """Wait for the next stretch of activity to start and then finish.

        Used by the SIP front-end to let the busy line play before hanging up, where
        nothing else is talking and "any activity" is precisely the right signal.

        NOT sound for retiring a one-shot context order, which is what it was
        originally written for. _busy is set by ANY activity — the user's speech as
        readily as the turn the caller queued — so this returns on someone else's
        turn and the order is retired having never been read. TLC found the
        interleaving in 8 steps (brain/formal/Followup.tla, MODE = "asWritten",
        invariant NoSilentLoss). Retirement belongs to FollowupTrigger, which keys on
        the completion that actually read the order.
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
        one so it fits into a gap or gives up quickly; a client note's waits pass
        their reaction's deadline, or wait again on False. `turn_free=True`
        additionally waits for no TURN to be in flight (see the class docstring) --
        ClientNotes' setting; the narrator leaves it False so it can
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
