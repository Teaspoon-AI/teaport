#
# wake_gate.py — the mic path's wake words, applied to the brain's own transcripts.
#
# The local audio bridge (local_audio.py) with LOCAL_AUDIO_WAKE_WORDS set opens its /talk
# session ASLEEP (?wake=<phrases>): the box's STT hears the room, in any language the
# engine hears, but nothing it hears goes anywhere until a final transcript holds a wake
# phrase. The gate sits INSIDE the STT service (stt.py asks it before every push), so a
# transcript of the room never becomes a pipeline frame at all: not the LLM context, not
# the agent, not the client's caption, not the ledger (an observer, which sees every
# frame pushed anywhere), not a log line. While asleep:
#
#   * interims are not pushed;
#   * a final with no wake phrase in it is pushed as a wordless close (SegmentDoneFrame),
#     which is what the stop strategy needs to stay in step with the STT's segments
#     (endpointing.LateStartTurnStopStrategy) -- to the rest of the brain the room said
#     nothing;
#   * a final WITH a wake phrase wakes the session. What came before the phrase is
#     dropped; what follows it ("hey teaport, what's the weather" -> "what's the
#     weather") is pushed as the final, and becomes the user's first turn the ordinary
#     way. Nothing follows: a wordless close, and the user's next words are the turn.
#
# Matching: wake_words.py (normalized text, whole words, any script, no fuzziness).
#
# The mic conversation. A session ends when the bridge's keep-alive runs out (or the user
# sends the box to sleep, end_conversation), but the CONVERSATION does not: the next wake
# within LOCAL_AUDIO_CONVERSATION_SECS (the bridge passes it as ?conversation_secs=)
# picks it up where it stopped -- the previous session's messages, kept here in memory --
# and is not greeted. A wake after that starts fresh, with a greeting. Only the mic path
# (a wake session of a client that says it is at the box, ?features=local) saves to or
# restores from this store; nothing else ever reads it.
#
# What the caller is still owed travels with the conversation (owed.py, #203): the owed
# records are saved and restored beside the messages, since the messages they are about
# are. Without them the next wake meets a bare history, with the answer only in an old
# tool result, and the model looks it up again (19/20 re-lookups in the #203 replays
# with no note). A consult still running when the session ends cannot report back: its
# placeholder becomes its failure record first (owed.Owed.close_session).
#
import asyncio
import time

from loguru import logger
from pipecat.frames.frames import LLMRunFrame, OutputTransportMessageUrgentFrame

from teaport_brain.wake_words import carry_tail, find_wake, join_carry, normalize, parse_phrases  # noqa: F401

# The kept conversation: at most this many messages and characters, the newest. There is
# no context cap elsewhere in the brain (a session is one sitting); one that is carried
# across sittings for hours needs one, on a box this short of memory.
KEEP_MESSAGES = 40
KEEP_CHARS = 24000
# A wake phrase the STT split across two finals ("Hey." / "Teaport, what's ...") still
# wakes it: the end of an asleep final (wake_words.carry_tail: never more than the
# longest phrase less a word) is held this long, in memory only, and matched joined to
# the next. Never logged; never pushed unless it is part of the phrase, which the cut
# then drops with everything before it.
CARRY_SECS = 2.0

def _cap(messages: list) -> list:
    """The newest of `messages` within KEEP_MESSAGES / KEEP_CHARS, starting at a user
    message so no tool result is kept without the call it answers."""
    kept, chars = [], 0
    for m in reversed(messages):
        n = len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or ""))
        if len(kept) >= KEEP_MESSAGES or chars + n > KEEP_CHARS:
            break
        kept.append(m)
        chars += n
    kept.reverse()
    while kept and kept[0].get("role") != "user":
        kept.pop(0)
    return kept


class MicConversation:
    """The mic path's conversation between its sessions: the messages (no system ones;
    every session builds its own), the owed records (owed.Owed.snapshot), and when the
    last session holding them ended."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._messages: list = []
        self.records: list = []
        self._ended: float | None = None

    def save(self, messages: list, records: list = ()) -> None:
        convo = [dict(m) for m in messages if isinstance(m, dict) and m.get("role") != "system"]
        self._messages = _cap(convo)
        self.records = [dict(r) for r in records]
        self._ended = self._clock()

    def take(self, window_secs: float) -> list | None:
        """The kept messages if the conversation ended within `window_secs`, else None
        (and it is forgotten: what comes next is a new conversation)."""
        if self._ended is not None and self._clock() - self._ended <= window_secs and self._messages:
            return list(self._messages)
        self.clear()
        return None

    def clear(self) -> None:
        self._messages, self.records, self._ended = [], [], None


# The one store: the mic path is one bridge, and this process is its one Talk brain.
MIC = MicConversation()


class WakeGate:
    """One mic session's wake state, consulted by the STT service (stt.py) before it
    pushes a transcript. `asleep`: waiting for a wake phrase (see the module note)."""

    def __init__(self, phrases: list[str], *, asleep: bool = True,
                 conversation_secs: float = 7200.0, greeting: str = "",
                 store: MicConversation | None = None, clock=time.monotonic):
        self.phrases = phrases
        self.asleep = asleep
        self.conversation_secs = conversation_secs
        self.greeting = greeting
        self.store = store or MIC
        self.context = None   # the session's LLMContext, set by build_agent_session
        self.owed = None      # the session's owed records (owed.Owed), likewise
        # The conversation-start notice (build_agent_session, #280): notice_due() says
        # whether one is due, and start_with_notice() says it, heard in full, then asks
        # for the greeting (AgentSession.start_with_notice). None: there is none.
        self.notice_due = None
        self.start_with_notice = None
        self._starting: set = set()  # its task, held while it runs
        # Awaited once, at the wake: a session built without OpenClaw's voice context
        # (the gateway could not answer when it opened) asks for it again
        # (build_agent_session's refresh_host). None: nothing to ask.
        self.refresh = None
        # Keeps the mic conversation (saves it at the end, restores it at a wake): False
        # for a session with no model (#277), whose turns nobody answered.
        self.remembers = True
        self.woke = not asleep
        self._clock = clock
        self._carry: tuple[str, float] | None = None   # (tail, when) -- see CARRY_SECS

    async def final(self, text: str, push) -> str | None:
        """The text to push as this final, or None for a wordless close. `push` pushes a
        frame downstream from the STT."""
        if not self.asleep:
            found = find_wake(text, self.phrases)
            if found:
                # A wake phrase in the conversation: the bridge's cap counts from it.
                await push(OutputTransportMessageUrgentFrame(
                    message={"type": "wake", "phrase": found[0], "again": True}))
            return text
        carry, self._carry = self._carry, None
        joined = text
        if carry is not None and self._clock() - carry[1] <= CARRY_SECS:
            joined = join_carry(carry[0], text)
        found = find_wake(joined, self.phrases)
        if not found:
            tail = carry_tail(text, self.phrases)
            if tail:
                self._carry = (tail, self._clock())
            return None
        phrase, rest = found
        await self.wake(push, phrase, run_greeting=not rest)
        return rest or None

    async def wake(self, push, phrase: str | None, *, why: str | None = None,
                   greet: bool = True, run_greeting: bool = True) -> None:
        """Wake the session: a wake `phrase` heard (final), or the box waking itself for
        `why` with no phrase (a new box going into setup mode, first_boot.py, with `greet`
        False: its welcome is the greeting). The voice context is asked again, a recent
        conversation restored, and the client told ({"type": "wake"}, with its "phrase" or
        its "why"); a new conversation is greeted unless `greet` is False -- its cue goes
        into the context, and with `run_greeting` the reply is asked for now (not when
        words follow the phrase: they are the turn)."""
        self.asleep = False
        self.woke = True
        refresh, self.refresh = self.refresh, None
        if refresh is not None:
            # Before anything is answered: the greeting below, or the words after the
            # phrase. Bounded by its own short timeout; a failure keeps the persona.
            try:
                await refresh()
            except Exception as e:  # noqa: BLE001 — the wake must go on
                logger.warning(f"wake: the voice context could not be asked again ({e!r})")
        restored = self.store.take(self.conversation_secs) if self.remembers else None
        if restored and self.context is not None:
            self.context.add_messages(restored)
            if self.owed is not None:
                self.owed.restore(self.store.records)
        greet = greet and not restored
        logger.info((f'wake word "{phrase}" heard — ' if phrase is not None
                     else f"woken for {why} — ")
                    + (f"continuing the conversation ({len(restored)} messages)" if restored
                       else "a new conversation"))
        message = {"type": "wake", "greeted": greet, "resumed": bool(restored)}
        message.update({"phrase": phrase} if phrase is not None else {"why": why})
        await push(OutputTransportMessageUrgentFrame(message=message))
        if greet and self.greeting and self.context is not None:
            self.context.add_message({"role": "user", "content": self.greeting})
            if run_greeting and self.notice_due is not None and self.notice_due():
                # A new conversation with a notice due (#280): it plays first, heard in
                # full, then the greeting -- from a task, as the line has to pass this
                # STT. Not when words followed the wake phrase: they are the turn, and
                # the notice waits for the next conversation.
                task = asyncio.ensure_future(self.start_with_notice())
                self._starting.add(task)
                task.add_done_callback(self._starting.discard)
            elif run_greeting:
                await push(LLMRunFrame())

    def ended(self) -> None:
        """The session is over: what it said is the mic conversation, to be continued."""
        if self.woke and self.remembers and self.context is not None:
            records = []
            if self.owed is not None:
                self.owed.close_session()
                records = self.owed.snapshot()
            self.store.save(self.context.get_messages(), records)
