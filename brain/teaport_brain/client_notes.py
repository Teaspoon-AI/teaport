#
# teaport — client context notes for live Talk sessions (issue #71).
#
# A Talk client can reach the voice LLM only through the microphone. Some things the
# character should know about never make a sound: a tap on the on-screen character, a
# changed setting, a new camera view. The teaport-realtime plugin's
# `teaport.talk.context` gateway method carries such an event here as a short text
# note, over /talk:
#
#   plugin -> brain   {"type":"context","id":"c1","text":"…","respond":false,"kind":"ui-event"}
#   brain -> plugin   {"type":"context_result","id":"c1","ok":true,"status":"applied"|"queued"}
#                     {"type":"context_result","id":"c1","ok":false,"error":"too_long"|
#                      "empty"|"rate_limited","message":"…"[,"retry_after_ms":N]}
#
# A note goes into the LLM context and nowhere else: it is never spoken and never
# captioned (no transcript event). With respond:true the model also gets a turn to
# react to it aloud.
#
# WHEN a note enters the context is the whole design, because the context is shared
# with turns already in flight:
#
#   * Only at a turn boundary. Appended while a completion is being requested, a note
#     becomes the tail message and the model answers IT instead of the user; appended
#     while a reply plays, it lands before that reply's assistant message, which the
#     assistant aggregator commits only when the reply ends. So a note waits in
#     `_pending` until one of two moments:
#       - the next completion that answers a user message is requested (an
#         LLMContextFrame passes on its way to the LLM): the notes are inserted just
#         before that message, which keeps them in arrival order and leaves the user's
#         words as the tail. A pending reaction is dropped here: the user's turn is the
#         model's chance to react, and a second reply after it would be a burst.
#       - a quiet moment (FollowupGate, the same debounced "no turn in flight" window
#         the consult follow-up waits for): the notes are appended, and a reaction, if
#         one was asked for, runs as its own turn.
#   * A reaction is a one-shot. Its "react now" line is retired by FollowupTrigger at
#     the completion that reads it, for the reason the consult follow-up's trigger is:
#     left in the context it is a standing order a later turn re-executes. A reaction
#     flushed by a barge-in before any completion read it stays armed, so the user's
#     barge-in turn reads it and reacts in the same reply.
#
# Limits (issue #71): a note is at most TEAPORT_CONTEXT_MAX_CHARS long; at most
# TEAPORT_CONTEXT_MAX_NOTES stay in the context (the oldest are removed); respond:true
# notes are refused within TEAPORT_CONTEXT_RESPOND_INTERVAL_S of the last one accepted,
# and every note draws on a small burst allowance. Refusals are answered, never queued.
#
import time
from dataclasses import dataclass

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMRunFrame,
    OutputTransportMessageUrgentFrame,
    SystemFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain import endpoint_debug
from teaport_brain.env import env_num

# env_num, not bare casts: brain.env is hand-edited and installer repairs preserve it
# verbatim, so a bad value must warn and fall back rather than crash-loop the brain.
MAX_CHARS = env_num("TEAPORT_CONTEXT_MAX_CHARS", "1000", int)
MAX_NOTES = env_num("TEAPORT_CONTEXT_MAX_NOTES", "20", int)
RESPOND_INTERVAL_S = env_num("TEAPORT_CONTEXT_RESPOND_INTERVAL_S", "15", float)
# How long a queued reaction may wait for a quiet moment. Past it the note stays as
# context only: a reaction to a tap half a minute ago reads as a non sequitur.
REACT_MAX_WAIT_S = 20.0
# Burst allowance over all notes, respond or not: BURST at once, refilled at
# REFILL_PER_S. A UI that sends an event per frame is a client bug; this makes it a
# refusal the client sees rather than a context churned to nothing but notes.
BURST = 10
REFILL_PER_S = 1.0

# As a USER message, tagged, for the reasons the consult follow-up found live (see
# _make_consult_followup in agent_session.py): a trailing system message is not a turn
# the model answers, and an untagged user message that outlives its turn gets
# attributed to the user.
_TAG = "[app event, not spoken by the user]"
_REACT = ("React to this now, as yourself, in one short spoken sentence. Don't mention "
          "the app, notes or events.")


@dataclass
class ClientContextFrame(SystemFrame):
    """A context note from the Talk client (gateway_serializer, {"type":"context"}).

    A SystemFrame so a barge-in's interruption cannot discard it on the way down: a
    note the client was told nothing about must not simply vanish."""

    text: str = ""
    respond: bool = False
    kind: str | None = None
    request_id: str | None = None


@dataclass
class _Note:
    text: str
    respond: bool
    kind: str | None
    deadline: float  # respond only: react by then or not at all


class _Bucket:
    """Token bucket: `capacity` at once, refilled at `per_s`."""

    def __init__(self, capacity: int, per_s: float, now: float):
        self._cap = float(capacity)
        self._rate = per_s
        self._tokens = float(capacity)
        self._at = now

    def take(self, now: float) -> float:
        """Take a token; 0.0 when one was taken, else the seconds until one is due."""
        self._tokens = min(self._cap, self._tokens + (now - self._at) * self._rate)
        self._at = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) / self._rate if self._rate > 0 else float("inf")


class ClientNotes(FrameProcessor):
    """Takes client context notes into the LLM context at turn boundaries, and runs a
    reaction turn for the ones that ask for it. See the module comment.

    PLACEMENT — between the user aggregator (and the HeardContextCorrector below it)
    and the LLM: it has to see every LLMContextFrame on its way to the LLM, and only
    there does inserting before the tail message mean "before the turn being answered".
    Below the corrector so the corrector reconciles a cut reply against the context as
    it stood; the notes it adds are user messages, which the corrector's walk skips.

    `task` is bound once the PipelineTask exists (build_agent_session); a reaction is
    queued on it as an LLMRunFrame, the path the consult follow-up takes.
    """

    def __init__(self, context, gate, trigger, *, drop_messages=None,
                 max_chars: int = MAX_CHARS, max_notes: int = MAX_NOTES,
                 respond_interval_s: float = RESPOND_INTERVAL_S,
                 react_max_wait_s: float = REACT_MAX_WAIT_S,
                 burst: int = BURST, refill_per_s: float = REFILL_PER_S,
                 clock=time.monotonic):
        super().__init__()
        self._context = context
        self._gate = gate
        self._trigger = trigger
        # Removes messages from the context. build_agent_session passes the
        # HeardContextCorrector's, which keeps its positional window in step.
        self._drop = drop_messages or self._drop_from_context
        self._max_chars = max(1, max_chars)
        self._max_notes = max(1, max_notes)
        self._respond_interval = respond_interval_s
        self._react_max_wait = react_max_wait_s
        self._clock = clock
        self._bucket = _Bucket(burst, refill_per_s, clock())
        self._last_respond: float | None = None
        self._pending: list[_Note] = []
        self._posted: list[dict] = []  # note messages in the context, oldest first
        self._drainer = None
        self.task = None

    def limits(self) -> dict:
        """The limits a client can plan around (sent in the /talk hello)."""
        return {"max_chars": self._max_chars, "max_notes": self._max_notes,
                "respond_interval_s": self._respond_interval}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, ClientContextFrame):
            await self._receive(frame)
            return  # consumed: the note travels no further as a frame
        if (isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM
                and self._pending):
            # Synchronously, before the frame moves on: the LLM serializes the context
            # as soon as it has it.
            self._fold(frame.context)
        await self.push_frame(frame, direction)

    async def cleanup(self):
        await super().cleanup()
        if self._drainer is not None:
            task, self._drainer = self._drainer, None
            await self.cancel_task(task)

    # ---- intake --------------------------------------------------------------

    async def _receive(self, frame: ClientContextFrame):
        now = self._clock()
        text = frame.text.strip() if isinstance(frame.text, str) else ""
        kind = frame.kind or "note"
        if not text:
            await self._reply(frame, error="empty", message="the note has no text")
            return
        if len(text) > self._max_chars:
            await self._reply(frame, error="too_long",
                              message=f"the note is {len(text)} characters; the limit "
                                      f"is {self._max_chars}")
            return
        # The respond interval first, so a refused reaction spends no burst allowance.
        if frame.respond and self._last_respond is not None:
            wait = self._last_respond + self._respond_interval - now
            if wait > 0:
                await self._reply(frame, error="rate_limited", retry_after_s=wait,
                                  message=f"respond:true notes are limited to one per "
                                          f"{self._respond_interval:g} s; send it with "
                                          "respond:false to add it as context only")
                return
        wait = self._bucket.take(now)
        if wait > 0:
            await self._reply(frame, error="rate_limited", retry_after_s=wait,
                              message="too many notes in a short time")
            return

        if frame.respond:
            self._last_respond = now
        self._pending.append(_Note(text, bool(frame.respond), frame.kind,
                                   now + self._react_max_wait))
        if len(self._pending) > self._max_notes:
            # They could never all stay in the context anyway (see _retain).
            del self._pending[: len(self._pending) - self._max_notes]
        # "applied": nothing is in flight, so the note goes in (and its reaction starts)
        # at the next quiet check. "queued": it waits for the current turn to end.
        status = "applied" if self._gate.is_clear() else "queued"
        logger.info(f"client note: {kind} ({len(text)} chars, "
                    f"respond={bool(frame.respond)}) -> {status}")
        if endpoint_debug.ENABLED:
            logger.info(f"client note text: {text!r}")
        await self._reply(frame, status=status)
        self._ensure_drainer()

    async def _reply(self, frame: ClientContextFrame, *, status: str | None = None,
                     error: str | None = None, message: str | None = None,
                     retry_after_s: float | None = None):
        if error is None:
            reply = {"type": "context_result", "id": frame.request_id, "ok": True,
                     "status": status}
        else:
            logger.info(f"client note refused ({error}): {message}")
            reply = {"type": "context_result", "id": frame.request_id, "ok": False,
                     "error": error, "message": message}
            if retry_after_s is not None:
                reply["retry_after_ms"] = max(1, int(retry_after_s * 1000 + 0.999))
        await self.push_frame(OutputTransportMessageUrgentFrame(message=reply))

    # ---- into the context ----------------------------------------------------

    def _message(self, note: _Note) -> dict:
        return {"role": "user", "content": f"{_TAG}\n{note.text}"}

    def _is_note(self, msg) -> bool:
        return any(msg is m for m in self._posted)

    def _fold(self, context):
        """Insert the pending notes before the message about to be answered."""
        existing = context.get_messages()
        tail = existing[-1] if existing else None
        if tail is None or tail.get("role") != "user":
            # Not a turn answering a user message (the tail is a tool result or a
            # system note): appended, a note would be the tail and get the answer.
            # They keep for the next boundary; the drainer is still waiting.
            return
        notes, self._pending = self._pending, []
        msgs = [self._message(n) for n in notes]
        # Before the tail, which is what is being answered: the user's turn, the
        # greeting cue, a follow-up's trigger. After it when it is one of our own
        # notes, i.e. a reaction being run.
        at = len(existing) if self._is_note(tail) else len(existing) - 1
        context.set_messages(existing[:at] + msgs + existing[at:])
        self._retain(msgs)
        dropped = sum(n.respond for n in notes)
        logger.info(f"client notes: {len(notes)} folded into the turn"
                    + (f" ({dropped} reaction(s) answered by it instead)" if dropped else ""))

    def _post(self) -> bool:
        """Append the pending notes at a quiet moment. True when a reaction should run."""
        notes, self._pending = self._pending, []
        msgs = [self._message(n) for n in notes]
        react = any(n.respond for n in notes)
        if react and self.task is None:
            logger.warning("client notes: no task bound — adding the note without a reaction")
            react = False
        if react:
            last = msgs[-1]
            plain = last["content"]
            last["content"] = f"{plain}\n{_REACT}"
        self._context.add_messages(msgs)
        self._retain(msgs)
        if react:
            # Retired at the read, never before: see FollowupTrigger.
            self._trigger.arm(lambda: last.__setitem__("content", plain))
        logger.info(f"client notes: {len(notes)} added at a quiet moment"
                    + (" — running a reaction" if react else ""))
        return react

    def _retain(self, msgs: list[dict]):
        self._posted.extend(msgs)
        excess = len(self._posted) - self._max_notes
        if excess > 0:
            doomed, self._posted = self._posted[:excess], self._posted[excess:]
            self._drop(doomed)
            logger.info(f"client notes: removed the {excess} oldest from the context "
                        f"(keeping {self._max_notes})")

    def _drop_from_context(self, doomed: list[dict]):
        ids = {id(m) for m in doomed}
        self._context.set_messages([m for m in self._context.get_messages()
                                    if id(m) not in ids])

    # ---- waiting for a quiet moment -------------------------------------------

    def _ensure_drainer(self):
        if self._drainer is not None and not self._drainer.done():
            return  # the running one takes new notes too
        coro = self._drain()
        try:
            self._drainer = self.create_task(coro)
        except Exception as e:  # noqa: BLE001 — outside a started pipeline (tests)
            coro.close()
            self._drainer = None
            logger.warning(f"client notes: cannot wait for a quiet moment ({e!r}); the "
                           "notes join the next turn instead")

    async def _drain(self):
        while self._pending:
            deadlines = [n.deadline for n in self._pending if n.respond]
            cutoff = min(deadlines) if deadlines else None
            wait = None if cutoff is None else max(0.0, cutoff - self._clock())
            clear = await self._gate.wait_until_idle(max_wait=wait, turn_free=True)
            if not self._pending:
                continue  # a turn took them while we waited
            if not clear:
                # No quiet moment in time for the earliest reaction. Every round drops
                # at least that one, so this cannot spin; the notes stay as context.
                late = [n for n in self._pending
                        if n.respond and cutoff is not None and n.deadline <= cutoff]
                for n in late:
                    n.respond = False
                if late:
                    logger.info(f"client notes: no quiet moment within "
                                f"{self._react_max_wait:g} s — {len(late)} reaction(s) "
                                "dropped, the note(s) stay as context")
                continue
            if self._post():
                await self.task.queue_frames([LLMRunFrame()])
