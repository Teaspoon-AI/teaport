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
import time

from loguru import logger
from pipecat.frames.frames import LLMRunFrame, OutputTransportMessageUrgentFrame

from teaport_brain.wake_words import find_wake, normalize, parse_phrases  # noqa: F401

# The kept conversation: at most this many messages and characters, the newest. There is
# no context cap elsewhere in the brain (a session is one sitting); one that is carried
# across sittings for hours needs one, on a box this short of memory.
KEEP_MESSAGES = 40
KEEP_CHARS = 24000

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
    every session builds its own), and when the last session holding them ended."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._messages: list = []
        self._ended: float | None = None

    def save(self, messages: list) -> None:
        convo = [dict(m) for m in messages if isinstance(m, dict) and m.get("role") != "system"]
        self._messages = _cap(convo)
        self._ended = self._clock()

    def take(self, window_secs: float) -> list | None:
        """The kept messages if the conversation ended within `window_secs`, else None
        (and it is forgotten: what comes next is a new conversation)."""
        if self._ended is not None and self._clock() - self._ended <= window_secs and self._messages:
            return list(self._messages)
        self.clear()
        return None

    def clear(self) -> None:
        self._messages, self._ended = [], None


# The one store: the mic path is one bridge, and this process is its one Talk brain.
MIC = MicConversation()


class WakeGate:
    """One mic session's wake state, consulted by the STT service (stt.py) before it
    pushes a transcript. `asleep`: waiting for a wake phrase (see the module note)."""

    def __init__(self, phrases: list[str], *, asleep: bool = True,
                 conversation_secs: float = 7200.0, greeting: str = "",
                 store: MicConversation | None = None):
        self.phrases = phrases
        self.asleep = asleep
        self.conversation_secs = conversation_secs
        self.greeting = greeting
        self.store = store or MIC
        self.context = None   # the session's LLMContext, set by build_agent_session
        self.woke = not asleep

    async def final(self, text: str, push) -> str | None:
        """The text to push as this final, or None for a wordless close. `push` pushes a
        frame downstream from the STT."""
        found = find_wake(text, self.phrases)
        if not self.asleep:
            if found:
                # A wake phrase in the conversation: the bridge's cap counts from it.
                await push(OutputTransportMessageUrgentFrame(
                    message={"type": "wake", "phrase": found[0], "again": True}))
            return text
        if not found:
            return None
        phrase, rest = found
        self.asleep = False
        self.woke = True
        restored = self.store.take(self.conversation_secs)
        if restored and self.context is not None:
            self.context.add_messages(restored)
        greet = not restored
        logger.info(f'wake word "{phrase}" heard — '
                    + (f"continuing the conversation ({len(restored)} messages)" if restored
                       else "a new conversation"))
        await push(OutputTransportMessageUrgentFrame(
            message={"type": "wake", "phrase": phrase, "greeted": greet,
                     "resumed": bool(restored)}))
        if greet and self.greeting and self.context is not None:
            self.context.add_message({"role": "user", "content": self.greeting})
            if not rest:
                await push(LLMRunFrame())
        return rest or None

    def ended(self) -> None:
        """The session is over: what it said is the mic conversation, to be continued."""
        if self.woke and self.context is not None:
            self.store.save(self.context.get_messages())
