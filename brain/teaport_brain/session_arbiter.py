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
#                call  a phone call (sip_server.py, the SIP front-end in the same process)
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
#     * a live Talk session or an awake    -> PROMPT: the agent asks inside that
#       room, newcomer a call                 conversation ("Someone's calling me --
#                                             should I step away for a moment?"). Take it:
#                                             the conversation is put ON HOLD, context
#                                             kept, and resumed when the call is over.
#                                             Don't: the call is DECLINED, never answered,
#                                             and the caller keeps hearing it ring
#     * anything else                      -> REFUSED: the newcomer is told the agent is
#                                             busy (a spoken line, unless it is a sleeping
#                                             room that nobody asked anything)
#
# So nothing is ever evicted without a word: a live conversation either keeps the slot,
# is asked for it and held, or hears why it lost it, and the only silent ends are a
# client replacing its own session and a mic that was asleep.
#
# A holder that cannot be asked (its session has no way to hear the answer: the
# answer_phone_call tool is switched off) gets the policy from before the prompt: a call
# ends a Talk session with a spoken line (CALL_IN), and is refused by an awake room (the
# caller hears the busy line).
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
# The hold (issue #111). A held conversation is not the holder: it has let the engine go
# (its STT is disconnected, its microphone muted) but its session, its client and its
# context stay. The arbiter keeps it beside the holder, and when the call that took the
# slot releases it, the held conversation gets it back at once (nothing can slip in
# between) and is resumed: its STT reconnects and the agent says it is back. A held
# session whose client leaves meanwhile is simply forgotten.
#
import asyncio
import unicodedata
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
PROMPT = "prompt"      # decide()'s answer when the holder is to be asked (a call comes in)

# How long a holder being ended may take to let the slot go (its pipeline's teardown
# closes the engine socket), and how long after that the engine gets to free it.
END_WAIT_SECS = 5.0
SETTLE_SECS = 0.3
# A holder whose client has sent nothing for this long is gone (every /talk client
# streams its microphone continuously, silence included: the relay, the Discord bridge's
# re-clocked uplink, the mic bridge). Well past any network hiccup.
STALE_SECS = 20.0
# How long a holder being put on hold may take to let the engine go (its last line plays
# first: AgentSession.hold) before it is ended instead.
HOLD_WAIT_SECS = 15.0


class Claim:
    """One session's ask for the slot, and its hold on it once granted.

    `end(why)` is how the arbiter ends this session when another takes the slot (why is
    REPLACED, REAPED, TAKEN or CALL_IN): the front-end says what it has to say, closes
    its client with a code that says why, and cancels its pipeline; the arbiter then
    waits for release(). `asleep()` says whether it is a sleeping room right now, and
    `gone()` whether its client has already left (closed, or silent for STALE_SECS).

    A conversation that can be asked to make way for a phone call (issue #111) also has
    `ask(call)`: ask the user, in the session, and return True to take the call; `hold()`:
    let the engine go but keep the session and its context; and `resume()`: take the
    engine back and carry on. `can_ask()` says whether it can be asked right now."""

    def __init__(self, kind: str, *, client: str | None = None, label: str | None = None,
                 asleep: Callable[[], bool] | None = None,
                 gone: Callable[[], bool] | None = None,
                 end: Callable[[str], Awaitable[None]] | None = None,
                 caller: str | None = None,
                 answered: asyncio.Event | None = None,
                 ask: Callable[["Claim"], Awaitable[bool]] | None = None,
                 can_ask: Callable[[], bool] | None = None,
                 hold: Callable[[], Awaitable[None]] | None = None,
                 resume: Callable[[], Awaitable[None]] | None = None):
        self.kind = kind
        self.client = client or None
        self.label = label or kind
        self._asleep = asleep
        self._gone = gone
        self.end = end
        # A call's: who it is from, for the prompt (None: withheld or unknown), and set
        # once the caller is connected (the gateway answered it by itself, or it was up
        # before this brain started): such a call is not asked about, it is taken.
        self.caller = caller
        self.answered = answered
        self.ask = ask
        self._can_ask = can_ask
        self.hold = hold
        self.resume = resume
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

    @property
    def askable(self) -> bool:
        """It can be asked to step away for a call, and put on hold if it says yes."""
        return (self.ask is not None and self.hold is not None and self.resume is not None
                and (self._can_ask is None or bool(self._can_ask())))


class Refusal:
    """The newcomer did not get the slot: `holder` is the kind of session that has it.
    `declined`: the user in that session said not to pick up (or the prompt's default
    said so) -- a call that is to keep ringing, never be told busy."""

    def __init__(self, holder: Claim, declined: bool = False):
        self.holder = holder.kind
        self.holder_label = holder.label
        self.declined = declined

    @property
    def reason(self) -> str:
        """For a close frame's reason (ASCII, short)."""
        return "busy: a phone call" if self.holder == CALL else "busy: another conversation"

    def __repr__(self):
        return f"Refusal({self.holder_label}{', declined' if self.declined else ''})"


def decide(holder: Claim | None, new: Claim) -> str | None:
    """The policy. None: the slot is free, granted. Otherwise why the holder ends
    (REAPED, REPLACED, TAKEN, CALL_IN), PROMPT when the holder is to be asked, or REFUSED
    when the newcomer does not get it."""
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
    if new.kind == CALL and holder.kind in (TALK, ROOM):
        # A call during a conversation: the people in it decide (issue #111).
        if holder.askable:
            return PROMPT
        # A conversation that cannot be asked: a remote Talk session is told and ended
        # (a call outranks it), an awake room keeps the box and the caller hears busy.
        return CALL_IN if holder.kind == TALK else REFUSED
    return REFUSED


class SessionArbiter:
    """Holds the slot's one Claim -- and at most one conversation on hold beside it --
    and applies decide() to every newcomer. One per process; every front-end of that
    process goes through it."""

    def __init__(self):
        self._holder: Claim | None = None
        # A conversation put on hold for the call that holds the slot (PROMPT, taken),
        # and that call's claim.
        self._held: Claim | None = None
        self._held_for: Claim | None = None
        self._lock = asyncio.Lock()
        # Ends in flight, by the holder being ended: held so a cancelled acquire cannot
        # drop one, and so a second newcomer waits on it rather than ending it again.
        self._endings: dict = {}
        # Holds in flight, by the holder going on hold: a newcomer waits one out (the
        # call it was for may be gone by then, and the conversation back) rather than
        # deciding against -- and asking again -- a conversation saying goodbye.
        self._holdings: dict = {}
        # Resumes in flight, by the conversation coming back: a newcomer waits one out
        # too, so a call's question never talks over "Sorry about that -- where were
        # we?" (and a resume that ends the session is seen as the end it is).
        self._resumes: dict = {}
        # Calls asking for the slot right now (in acquire): status() counts one as a call
        # already, so a room mic that yielded to it, polling, does not take the moment
        # between the room letting go and the call being granted for the call's end.
        self._calls_waiting = 0
        # Called (sync, no arguments) whenever the holder changes: who drives the face's
        # sleep/awake state reads anything_live() from here.
        self.listeners: list = []

    @property
    def holder(self) -> Claim | None:
        return self._holder

    @property
    def held_claim(self) -> Claim | None:
        return self._held

    def held(self) -> bool:
        return self._holder is not None

    def anything_live(self) -> bool:
        """A conversation is live on some front-end: a holder that is not a sleeping room
        mic, or one on hold. The face sleeps only when this is False ("a sleeping face
        means the voice loop is not active anywhere"); the asleep/awake flip of a room
        session itself is the room's to report."""
        return (self._holder is not None and self._holder.live) or self._held is not None

    def _notify(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — a listener's bug is not the arbiter's
                logger.warning(f"session arbiter listener failed: {e!r}")

    def _set_holder(self, claim) -> None:
        if claim is self._holder:
            return
        self._holder = claim
        self._notify()

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
        return {"active": h is not None,
                "call": self.call_live() or self._calls_waiting > 0,
                "live": self.anything_live(),
                "holder": None if h is None else h.kind,
                "asleep": bool(h is not None and h.asleep),
                "held": None if self._held is None else self._held.kind}

    async def acquire(self, new: Claim) -> Refusal | None:
        """Ask for the slot. None: granted (release(new) when the session ends). A
        Refusal: not granted, and nothing was touched (a declined one: the holder was
        asked and said no). A holder that has to go is ended through its end() -- or, one
        that said yes to a call, put on hold through its hold() -- and waited out before
        this returns, so the newcomer's STT finds the engine free."""
        if new.kind == CALL:
            self._calls_waiting += 1
        try:
            async with self._lock:
                return await self._acquire(new)
        finally:
            if new.kind == CALL:
                self._calls_waiting -= 1

    async def _acquire(self, new: Claim) -> Refusal | None:
        holder = self._holder
        while holder is not None and (holder in self._endings or holder in self._holdings
                                      or holder in self._resumes):
            # Being ended already (a newcomer before this one gave up mid-way, and the
            # shielded end ran on): it is going, not a reason to refuse, and must not
            # be ended twice. Wait it out and decide against whoever holds it then.
            # Likewise one going on hold for a call that gave up mid-way (by the end of
            # it the conversation is held for nobody, and back), and one coming back
            # from hold (its "where were we?" is not to be talked over by a question).
            await asyncio.shield(self._endings.get(holder) or self._holdings.get(holder)
                                 or self._resumes[holder])
            holder = self._holder
        why = decide(holder, new)
        if why == PROMPT:
            take = await self._ask(holder, new)
            if take is False:
                logger.info(f"session arbiter: {new.label} declined in {holder.label} — "
                            "it keeps the speech engine; the call is left ringing")
                return Refusal(holder, declined=True)
            if take:
                logger.info(f"session arbiter: {holder.label} goes on hold for {new.label}")
                if not await self._hold(holder, new):
                    # It would not let go: ended, as a call ends a conversation that
                    # cannot be asked (its end says why, or it is already gone).
                    await self._end(holder, CALL_IN)
            if new.released.is_set():
                # The call ended while it was being made way for: the conversation held
                # for it is back (or is coming back), and the call never holds anything.
                logger.info(f"session arbiter: {new.label} ended before it was granted")
                self._resume_held()
                return None
            # Taken (the slot is free now), or None: the holder left while it was asked.
            holder = self._holder
            why = decide(holder, new)
            if why == PROMPT:
                why = REFUSED  # not reachable under the lock; never ask twice
        if why == REFUSED:
            logger.info(f"session arbiter: {new.label} refused — {holder.label} has the "
                        "speech engine (one conversation at a time)")
            return Refusal(holder)
        if holder is not None and why is not None:
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
            # the engine, so nothing is left behind for nobody to end -- and a
            # conversation held for it comes straight back.
            logger.info(f"session arbiter: {new.label} ended before it was granted")
            self._resume_held()
            return None
        self._set_holder(new)
        return None

    async def _ask(self, holder: Claim, new: Claim) -> bool | None:
        """The holder's answer to the call: True (take it), False (don't), or None when
        the holder let the slot go while it was being asked (its client left). A call
        whose caller is already connected is not asked about -- they would sit on a
        silent line for the question, and a "no" would have to hang up on them -- and
        one that gets connected while it is asked (the gateway answered it) stops the
        question: either way it is taken."""
        answered = new.answered
        if answered is not None and answered.is_set():
            logger.info(f"session arbiter: {new.label} is answered already — taking it "
                        f"without asking {holder.label}")
            return True
        logger.info(f"session arbiter: {new.label} — asking {holder.label} whether to take it")
        asking = asyncio.ensure_future(holder.ask(new))
        gone = asyncio.ensure_future(holder.released.wait())
        waits = {asking, gone}
        connected = None
        if answered is not None:
            connected = asyncio.ensure_future(answered.wait())
            waits.add(connected)
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            gone.cancel()
            if connected is not None:
                connected.cancel()
            if not asking.done():
                # The caller hung up (this acquire is cancelled), the holder went, or
                # the caller got connected: the question is withdrawn.
                asking.cancel()
                await asyncio.gather(asking, return_exceptions=True)
        if answered is not None and answered.is_set() and not holder.released.is_set():
            logger.info(f"session arbiter: {new.label} got answered while asked — taking it")
            return True
        if not asking.done() or asking.cancelled():
            return None
        if asking.exception() is not None:
            logger.warning(f"session arbiter: asking {holder.label} failed "
                           f"({asking.exception()!r}) — taking the call")
            return True
        return bool(asking.result())

    async def _hold(self, holder: Claim, new: Claim) -> bool:
        """Put the holder on hold for `new`: it lets the engine go and is kept beside the
        slot. False when it could not (it is to be ended instead). Shielded like _end: a
        caller who hangs up while the user hears "back in a moment" must not leave the
        conversation half held -- the hold runs on, and finds the call gone at its end."""
        holding = asyncio.ensure_future(self._holding(holder, new))
        self._holdings[holder] = holding
        holding.add_done_callback(lambda _t, h=holder: self._holdings.pop(h, None))
        return await asyncio.shield(holding)

    async def _holding(self, holder: Claim, new: Claim) -> bool:
        try:
            await asyncio.wait_for(holder.hold(), HOLD_WAIT_SECS)
        except Exception as e:  # noqa: BLE001 — it stays, or is ended; never wedged
            logger.warning(f"session arbiter: {holder.label} could not be put on hold "
                           f"({e!r}) — ending it instead")
            return False
        if holder.released.is_set():
            if self._holder is holder:
                self._set_holder(None)
            return True  # it left meanwhile: nothing to hold
        self._held, self._held_for = holder, new
        if self._holder is holder:
            self._set_holder(None)
        if new.released.is_set():
            self._resume_held()  # the call is gone already (hung up while it rang)
            return True
        await asyncio.sleep(SETTLE_SECS)  # the engine processes the close, frees the slot
        return True

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

    def _resume_held(self) -> None:
        """The slot is free: the conversation on hold gets it back, at once (a newcomer
        deciding now meets it, not an empty slot), and is resumed."""
        held, self._held, self._held_for = self._held, None, None
        if held is None:
            return
        if held.released.is_set():
            self._notify()
            return  # its session ended while it was held
        logger.info(f"session arbiter: {held.label} comes back from hold")
        self._set_holder(held)
        t = asyncio.ensure_future(self._resuming(held))
        self._resumes[held] = t
        t.add_done_callback(lambda _t, h=held: self._resumes.pop(h, None))

    async def _resuming(self, held: Claim) -> None:
        try:
            await held.resume()
        except Exception as e:  # noqa: BLE001 — its session ends on its own if it cannot
            logger.warning(f"session arbiter: resuming {held.label} failed: {e!r}")

    def release(self, claim: Claim) -> None:
        """The session is over. A late release (one already ended and replaced) leaves
        the newer holder alone. A held one is forgotten; the end of the session that
        holds the slot brings back the one held for it."""
        claim.released.set()
        if self._held is claim:
            self._held = self._held_for = None
            self._notify()
        if self._holder is claim:
            self._set_holder(None)
            self._resume_held()
        elif claim is self._held_for and self._holder is None:
            # The call a conversation was held for ended before it was granted (the
            # caller hung up as the room said "back in a moment").
            self._resume_held()


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


# --- What the agent says when a call comes in during a conversation (issue #111) -----
# In the conversation's own language. Consumer wording: what a person would say.

# A caller id is the far end's to choose: said aloud and kept in the conversation's
# context, it is cut to this many characters and to what a name or a number is made of.
CALLER_MAX_CHARS = 40
# Besides letters and digits (any script): spaces and the punctuation names and numbers
# use. No full stop or question mark: the voice would split the question there.
_CALLER_PUNCT = frozenset(" '’-+(),&")


def speakable_caller(caller: str | None) -> str | None:
    """`caller` (sip_server.caller_id) as the question may say it: only letters, digits,
    spaces and a name's or number's punctuation, at most CALLER_MAX_CHARS (cut at a word
    when it can be). None when nothing of it is left."""
    if not isinstance(caller, str):
        return None
    # NFC first, and combining marks kept: Devanagari's vowel signs and viramas, Arabic's
    # harakat, Thai's tone marks are marks, not letters ("राम शर्मा" must not become
    # "र म शर म"), and a decomposed "José" composes.
    caller = unicodedata.normalize("NFC", caller)
    kept = "".join(ch if (ch.isalnum() or ch in _CALLER_PUNCT
                          or unicodedata.category(ch).startswith("M")) else " "
                   for ch in caller)
    kept = " ".join(kept.split())
    if len(kept) > CALLER_MAX_CHARS:
        cut = kept[:CALLER_MAX_CHARS + 1].rsplit(" ", 1)[0]
        kept = cut if len(cut) >= CALLER_MAX_CHARS // 2 else kept[:CALLER_MAX_CHARS]
    kept = kept.strip(" ,'’-&(")
    return kept if any(ch.isalnum() for ch in kept) else None


def prompt_line(caller: str | None, espeak_language: str | None, *,
                withheld: bool | None = None) -> str:
    """The question put to a live conversation when a call comes in. `caller`: who the
    call is from (sip_server.caller_id), None when withheld or unknown. `withheld`
    (default: no caller) picks the withheld-number wording when there is no caller to
    name; a caller id with nothing speakable in it is just "someone"."""
    t = i18n.for_espeak(espeak_language)
    said = speakable_caller(caller)
    if said:
        # Translators: {caller} is a name or a phone number, as the caller ID gives it.
        return t._("Someone's calling me — {caller}. Should I step away for a moment?").format(
            caller=said)
    if withheld is False or (withheld is None and caller):
        return t._("Someone's calling me. Should I step away for a moment?")
    return t._("Someone's calling me from a withheld number. Should I step away for a moment?")


def step_away_line(espeak_language: str | None) -> str:
    """Said as the conversation goes on hold for the call."""
    t = i18n.for_espeak(espeak_language)
    return t._("I'll take the call — back in a moment.")


def back_line(espeak_language: str | None) -> str:
    """Said when the call is over and the held conversation is back."""
    t = i18n.for_espeak(espeak_language)
    return t._("Sorry about that — where were we?")
