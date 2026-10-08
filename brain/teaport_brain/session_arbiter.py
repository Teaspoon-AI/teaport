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
#                      ?features=local); ASLEEP while it waits for a wake word -- and,
#                      without wake words, until someone in the room has spoken, and
#                      again once nobody has for the bridge's keep-alive
#                call  a phone call (for now the SIP brain's lease, POST /talk/call)
#
#   the policy, newcomer against the session holding the slot:
#     * nobody holds it                    -> granted
#     * a holder whose client is gone      -> granted; the dead session is REAPED (its
#       (socket closed, or nothing           socket is already closed, or it has sent
#       received for STALE_SECS)             nothing for STALE_SECS: every /talk client
#                                            streams its mic, silence included)
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
# decides which of the token holder's own sessions a reconnect may end (a live session
# replaced is logged as a warning, since a real reconnect usually finds its old one dead).
# A client whose id does not survive its own reconnect -- the OpenClaw Control UI over
# plain HTTP gets no device identity and a fresh instance id per page load -- is covered
# by the reaping above: a reload closes the old socket, and a frozen one stops sending.
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
REAPED = "reaped"      # its client was already gone (socket closed, or silent)
TAKEN = "taken"        # a sleeping room gave way to a Talk session
CALL_IN = "call"       # a phone call needs the engine
REFUSED = "refused"    # decide()'s answer when the newcomer does not get it

# How long a holder being ended may take to let the slot go (its pipeline's teardown
# closes the engine socket), and how long after that the engine gets to free it.
END_WAIT_SECS = 5.0
SETTLE_SECS = 0.3
# A holder whose client has sent nothing for this long is gone (every /talk client
# streams its microphone continuously, silence included: the relay, the Discord bridge's
# re-clocked uplink, the mic bridge). Well past any network hiccup.
STALE_SECS = 20.0


class Claim:
    """One session's ask for the slot, and its hold on it once granted.

    `end(why)` is how the arbiter ends this session when another takes the slot (why is
    REPLACED, REAPED, TAKEN or CALL_IN): the front-end says what it has to say, closes
    its client with a code that says why, and cancels its pipeline; the arbiter then
    waits for release(). `asleep()` says whether it is a sleeping room right now, and
    `gone()` whether its client has already left (closed, or silent for STALE_SECS)."""

    def __init__(self, kind: str, *, client: str | None = None, label: str | None = None,
                 asleep: Callable[[], bool] | None = None,
                 gone: Callable[[], bool] | None = None,
                 end: Callable[[str], Awaitable[None]] | None = None):
        self.kind = kind
        self.client = client or None
        self.label = label or kind
        self._asleep = asleep
        self._gone = gone
        self.end = end
        self.released = asyncio.Event()

    @property
    def asleep(self) -> bool:
        return bool(self._asleep is not None and self._asleep())

    @property
    def gone(self) -> bool:
        return bool(self._gone is not None and self._gone())

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
    (REAPED, REPLACED, TAKEN, CALL_IN), or REFUSED when the newcomer does not get it."""
    if holder is None:
        return None
    if holder.gone:
        return REAPED
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
        # Ends in flight, by the holder being ended: held so a cancelled acquire cannot
        # drop one, and so a second newcomer waits on it rather than ending it again.
        self._endings: dict = {}
        # Called (sync, no arguments) whenever the holder changes: who drives the face's
        # sleep/awake state reads anything_live() from here.
        self.listeners: list = []

    @property
    def holder(self) -> Claim | None:
        return self._holder

    def held(self) -> bool:
        return self._holder is not None

    def anything_live(self) -> bool:
        """A conversation is live on some front-end: a holder that is not a sleeping room
        mic. The face sleeps only when this is False ("a sleeping face means the voice
        loop is not active anywhere"); the asleep/awake flip of a room session itself is
        the room's to report."""
        return self._holder is not None and self._holder.live

    def _set_holder(self, claim) -> None:
        if claim is self._holder:
            return
        self._holder = claim
        for fn in list(self.listeners):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — a listener's bug is not the arbiter's
                logger.warning(f"session arbiter listener failed: {e!r}")

    def call_live(self) -> bool:
        return self._holder is not None and self._holder.kind == CALL

    def would_refuse(self, new: Claim) -> Refusal | None:
        """decide() against the holder now, without taking anything: a front-end asks
        this before it builds a session it would only have to refuse."""
        holder = self._holder
        if holder is not None and holder in self._endings:
            return None  # going: the slot is about to be free; acquire() waits it out
        return Refusal(holder) if decide(holder, new) == REFUSED else None

    def status(self) -> dict:
        h = self._holder
        return {"active": h is not None, "call": self.call_live(),
                "live": self.anything_live(),
                "holder": None if h is None else h.kind,
                "asleep": bool(h is not None and h.asleep)}

    async def acquire(self, new: Claim) -> Refusal | None:
        """Ask for the slot. None: granted (release(new) when the session ends). A
        Refusal: not granted, and nothing was touched. A holder that has to go is ended
        through its end() and waited out before this returns, so the newcomer's STT finds
        the engine free."""
        async with self._lock:
            holder = self._holder
            while holder is not None and holder in self._endings:
                # Being ended already (a newcomer before this one gave up mid-way, and the
                # shielded end ran on): it is going, not a reason to refuse, and must not
                # be ended twice. Wait it out and decide against whoever holds it then.
                await asyncio.shield(self._endings[holder])
                holder = self._holder
            why = decide(holder, new)
            if why == REFUSED:
                logger.info(f"session arbiter: {new.label} refused — {holder.label} has the "
                            "speech engine (one conversation at a time)")
                return Refusal(holder)
            if holder is not None:
                if why == REPLACED and holder.live:
                    # A reconnect usually finds its old session dead or asleep; a live one
                    # replaced may be a second device claiming the same id.
                    logger.warning(f"session arbiter: {new.label} replaces a LIVE session "
                                   f"of the same client id ({holder.label})")
                logger.info(f"session arbiter: {new.label} takes the speech engine from "
                            f"{holder.label} ({why})")
                await self._end(holder, why)
            if new.released.is_set():
                # Ended while it waited (a call hung up during its start): it never holds
                # the engine, so nothing is left behind for nobody to end.
                logger.info(f"session arbiter: {new.label} ended before it was granted")
                return None
            self._set_holder(new)
            return None

    async def _end(self, holder: Claim, why: str) -> None:
        # Shielded: a newcomer that gives up mid-way (a caller who hangs up while a Talk
        # user is being told about the call) must not leave the holder half-ended, told
        # but never closed. The end runs to completion either way.
        ending = self._endings.get(holder)
        if ending is None:
            ending = asyncio.ensure_future(self._ending(holder, why))
            self._endings[holder] = ending
            ending.add_done_callback(lambda _t, h=holder: self._endings.pop(h, None))
        await asyncio.shield(ending)

    async def _ending(self, holder: Claim, why: str) -> None:
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
            self._set_holder(None)
        await asyncio.sleep(SETTLE_SECS)  # the engine processes the close, frees the slot

    def release(self, claim: Claim) -> None:
        """The session is over. A late release (one already ended and replaced) leaves
        the newer holder alone."""
        claim.released.set()
        if self._holder is claim:
            self._set_holder(None)


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


def tts_for(voice: str | None, language: str | None):
    """A voice outside any pipeline, for a line said on a connection about to close (a
    module function so tests stub it). Imported here, not at the top: this module stays
    cheap."""
    from teaport_brain.services import make_tts
    return make_tts(voice=voice, language=language)


# The busy lines already synthesized, by the voice the engine was actually asked for, its
# language and the line: there are a handful of them, and a refused client should not
# wait on (or load the engine with) a fresh synth. Capped, oldest out, since the voice a
# client asks for is its own string.
_BUSY_PCM: dict = {}
_BUSY_PCM_MAX = 16
# How long a busy line may take to synthesize; past it the client goes without it.
BUSY_SYNTH_SECS = 6.0


async def busy_line_audio(holder_kind: str, voice: str | None = None,
                          language: str | None = None) -> tuple[str, bytes]:
    """The busy line for a newcomer refused because `holder_kind` has the box, in its
    voice's language, and its audio: PCM16 mono at 24 kHz (b"" when the engine cannot say
    it). Cached in-process; a failed synth is not cached. For any front-end that refuses
    without building a session (a /talk client, a phone call)."""
    tts = tts_for(voice, language)
    line = busy_line(holder_kind, getattr(tts, "espeak_language", None))
    key = (getattr(tts, "_voice", voice), getattr(tts, "espeak_language", None), line)
    pcm = _BUSY_PCM.get(key)
    if pcm is None:
        try:
            pcm = await asyncio.wait_for(tts.synthesize(line), BUSY_SYNTH_SECS)
            while len(_BUSY_PCM) >= _BUSY_PCM_MAX:
                _BUSY_PCM.pop(next(iter(_BUSY_PCM)))
            _BUSY_PCM[key] = pcm
        except Exception as e:  # noqa: BLE001 — no voice: the refusal still happens
            logger.warning(f"busy line not synthesized ({e!r}) — refusing without it")
            pcm = b""
    return line, pcm


def call_line(espeak_language: str | None) -> str:
    """For a Talk session a phone call ends."""
    t = i18n.for_espeak(espeak_language)
    return t._("Sorry, a phone call is coming in and I have to take it. We can talk again when it's over.")
