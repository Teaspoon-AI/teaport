# SPDX-License-Identifier: MIT
#
# teaport — the session arbiter: who may hold the engine's one speech-to-text slot.
#
# The engine transcribes one session at a time, and the box holds one conversation at a
# time (issue #58). Every front-end asks this arbiter for the slot BEFORE its pipeline
# connects to the engine, and gets the same answer from one explicit policy:
#
#   front-ends   talk  a /talk client: the OpenClaw app or dashboard, the Discord bridge
#                room  the box's own microphone (the local audio bridge, also /talk, with
#                      ?features=local); ASLEEP while it waits for a wake word
#                call  a phone call (for now the SIP brain's lease, POST /talk/call)
#
#   the policy, newcomer against the session holding the slot:
#     * nobody holds it                    -> granted
#     * the same client, reconnecting      -> granted; its old session is REPLACED
#     * a room asleep, newcomer talk/call  -> granted; a sleeping mic is not a
#                                             conversation, it yields (quietly)
#     * a Talk session, newcomer a call    -> granted; a call outranks Talk, and the Talk
#                                             user is TOLD (a spoken line) before it ends
#     * anything else                      -> REFUSED: the newcomer is told the agent is
#                                             busy (a spoken line, unless it is a sleeping
#                                             room that nobody asked anything)
#
# So nothing is ever evicted without a word: a live conversation either keeps the slot or
# hears why it lost it, and the only silent ends are a client replacing its own session
# and a mic that was asleep.
#
# "The same client" is the ?client= id a /talk client sends: an opaque name it keeps
# across its own reconnects ("local-audio", "discord", "openclaw:<hash of the paired
# device>"). The old /talk rule, newest wins, existed for exactly one case -- an app whose
# old connection was frozen and never closed, holding the slot -- and that case is a
# client reconnecting, so it keeps that power over its own session and nobody else's. A
# client that sends no id is never "the same" as anything: it can be refused, never
# replace. The id is self-asserted, like everything a GATEWAY_TOKEN holder sends; it only
# decides which of the token holder's own sessions a reconnect may end.
#
# Not here yet (#58's later steps), with their seams marked: a call that lands during an
# AWAKE room conversation should ask the room "take the call?" and put the room on hold;
# until then it is refused (the caller hears the busy line, as before the arbiter). See
# decide().
#
import asyncio
from typing import Awaitable, Callable

from loguru import logger

from teaport_brain import i18n

TALK, ROOM, CALL = "talk", "room", "call"
# Why a holder loses the slot (what its front-end's end() is told).
REPLACED = "replaced"  # the same client opened a new session
TAKEN = "taken"        # a sleeping room gave way to a Talk session
CALL_IN = "call"       # a phone call needs the engine
REFUSED = "refused"    # decide()'s answer when the newcomer does not get it

# How long a holder being ended may take to let the slot go (its pipeline's teardown
# closes the engine socket), and how long after that the engine gets to free it.
END_WAIT_SECS = 5.0
SETTLE_SECS = 0.3


class Claim:
    """One session's ask for the slot, and its hold on it once granted.

    `end(why)` is how the arbiter ends this session when another takes the slot (why is
    REPLACED, TAKEN or CALL_IN): the front-end says what it has to say, closes its
    client with a code that says why, and cancels its pipeline; the arbiter then waits
    for release(). `asleep()` says whether it is a sleeping room right now."""

    def __init__(self, kind: str, *, client: str | None = None, label: str | None = None,
                 asleep: Callable[[], bool] | None = None,
                 end: Callable[[str], Awaitable[None]] | None = None):
        self.kind = kind
        self.client = client or None
        self.label = label or kind
        self._asleep = asleep
        self.end = end
        self.released = asyncio.Event()

    @property
    def asleep(self) -> bool:
        return bool(self._asleep is not None and self._asleep())

    @property
    def live(self) -> bool:
        """A conversation: anything but a sleeping room."""
        return not self.asleep


class Refusal:
    """The newcomer did not get the slot: `holder` is the kind of session that has it."""

    def __init__(self, holder: Claim):
        self.holder = holder.kind
        self.holder_label = holder.label

    @property
    def reason(self) -> str:
        """For a close frame's reason (ASCII, short)."""
        return "busy: a phone call" if self.holder == CALL else "busy: another conversation"

    def __repr__(self):
        return f"Refusal({self.holder_label})"


def decide(holder: Claim | None, new: Claim) -> str | None:
    """The policy. None: the slot is free, granted. Otherwise why the holder ends
    (REPLACED, TAKEN, CALL_IN), or REFUSED when the newcomer does not get it."""
    if holder is None:
        return None
    if new.client is not None and new.client == holder.client and new.kind == holder.kind:
        return REPLACED
    if holder.kind == ROOM and holder.asleep:
        if new.kind == CALL:
            return CALL_IN
        if new.kind == TALK:
            return TAKEN
        return REFUSED  # another room client: the box has one microphone
    if new.kind == CALL and holder.kind == TALK:
        return CALL_IN  # a call outranks a remote Talk session; its user is told
    # A call during an AWAKE room conversation: refused for now. #58's next step asks
    # the room ("take the call or not?") and holds the room conversation for the call;
    # that prompt plugs in here, as a decision of its own.
    return REFUSED


class SessionArbiter:
    """Holds the slot's one Claim and applies decide() to every newcomer. One per
    process; every front-end of that process goes through it."""

    def __init__(self):
        self._holder: Claim | None = None
        self._lock = asyncio.Lock()

    @property
    def holder(self) -> Claim | None:
        return self._holder

    def held(self) -> bool:
        return self._holder is not None

    def call_live(self) -> bool:
        return self._holder is not None and self._holder.kind == CALL

    def would_refuse(self, new: Claim) -> Refusal | None:
        """decide() against the holder now, without taking anything: a front-end asks
        this before it builds a session it would only have to refuse."""
        holder = self._holder
        return Refusal(holder) if decide(holder, new) == REFUSED else None

    def status(self) -> dict:
        h = self._holder
        return {"active": h is not None, "call": self.call_live(),
                "holder": None if h is None else h.kind,
                "asleep": bool(h is not None and h.asleep)}

    async def acquire(self, new: Claim) -> Refusal | None:
        """Ask for the slot. None: granted (release(new) when the session ends). A
        Refusal: not granted, and nothing was touched. A holder that has to go is ended
        through its end() and waited out before this returns, so the newcomer's STT finds
        the engine free."""
        async with self._lock:
            holder = self._holder
            why = decide(holder, new)
            if why == REFUSED:
                logger.info(f"session arbiter: {new.label} refused — {holder.label} has the "
                            "speech engine (one conversation at a time)")
                return Refusal(holder)
            if holder is not None:
                logger.info(f"session arbiter: {new.label} takes the speech engine from "
                            f"{holder.label} ({why})")
                await self._end(holder, why)
            self._holder = new
            return None

    async def _end(self, holder: Claim, why: str) -> None:
        if holder.end is not None:
            try:
                await holder.end(why)
            except Exception as e:  # noqa: BLE001 — its end failed; it still has to go
                logger.warning(f"session arbiter: ending {holder.label} failed: {e!r}")
        try:
            await asyncio.wait_for(holder.released.wait(), timeout=END_WAIT_SECS)
        except asyncio.TimeoutError:
            logger.warning(f"session arbiter: {holder.label} did not let the speech engine "
                           f"go within {END_WAIT_SECS:g} s")
        if self._holder is holder:
            self._holder = None
        await asyncio.sleep(SETTLE_SECS)  # the engine processes the close, frees the slot

    def release(self, claim: Claim) -> None:
        """The session is over. A late release (one already ended and replaced) leaves
        the newer holder alone."""
        claim.released.set()
        if self._holder is claim:
            self._holder = None


# The one arbiter of this process.
ARBITER = SessionArbiter()


# --- What the agent says when it refuses or ends a conversation ----------------------
# The brain's own words, not the model's: translated here (i18n.py), in the session's
# voice language.

def busy_line(holder_kind: str, espeak_language: str | None) -> str:
    """For a newcomer refused because `holder_kind` has the box."""
    t = i18n.for_espeak(espeak_language)
    if holder_kind == CALL:
        return t._("Sorry, I'm on a phone call right now. Please try again when it's over.")
    return t._("Sorry, I'm in another conversation right now. Please try again in a little while.")


def call_line(espeak_language: str | None) -> str:
    """For a Talk session a phone call ends."""
    t = i18n.for_espeak(espeak_language)
    return t._("Sorry, a phone call is coming in and I have to take it. We can talk again when it's over.")
