#
# teaport — asking a live conversation whether to take a phone call (issue #111).
#
# A call that lands while a conversation is live -- a remote Talk session, or the room
# mic awake -- is the people in that conversation's to decide (the session arbiter's
# PROMPT). The agent asks them, inside the conversation and in its language:
#
#   "Someone's calling me — Ann Smith. Should I step away for a moment?"
#   "Someone's calling me — a number ending in 8 5 0 0. Should I step away for a moment?"
#
# said as the agent's own words (a TTSSpeakFrame into the context), so the model hears
# the user's reply as the answer to it. The answer is understood the way the rest of the
# brain understands what the user means: by the model, which calls the answer_phone_call
# tool (tools.py) with take=true or false -- in any language, however it is put ("sure, go
# ahead", "no, let it ring", "nimm ruhig ab"). A fixed yes/no word list (wifi_voice.py's
# way) is for when there is no model to ask; here there always is one, and a list would
# have to guess at "no, go ahead". Nothing is decided on words alone: a reply that does
# not answer it is answered like any other turn, and the question waits.
#
#   * Take it: the agent says it is stepping away, and the conversation goes on hold
#     (AgentSession.hold): context kept, the engine's STT handed to the call. When the
#     call is over it comes back: "Sorry about that — where were we?".
#   * Don't pick up: the call is never answered. The caller keeps hearing it ring until
#     they give up or their side times out -- never a busy line, never a hangup.
#   * No answer within TEAPORT_CALL_PROMPT_SECS of the start of the question (and never
#     sooner than ANSWER_MIN_SECS after it has been said): TEAPORT_CALL_PROMPT_DEFAULT,
#     "take" (pick up: the room cannot always answer, and a call let ring by nobody's
#     choice is a call missed) or "ring". A turn still under way at the deadline (the
#     user answering, the model about to call the tool) gets a few seconds more.
#   * The caller hangs up while it is asked: the question is withdrawn. A late "yes" is
#     told the caller is gone.
#
# An answer counts only to a question the user has HEARD: played through, or cut once
# its ask ("Should I step away…") had been heard -- at least ASK_HEARD_SHARE of it, by
# the heard prefix the ledger charts for the line. Nothing pauses the question (#362),
# so the user's own talk cuts it, and a cut at "Someone's calling me" left the model
# reading "You know." as the answer, and the call was taken (seen live, 2026-11-07).
# Unheard, the tool is told so (UNHEARD) and the question waits; once the user has
# finished speaking (REASK_WAIT_SECS, REASK_BY_SECS at most) it is asked again, the same line the
# same way -- once. A second cut leaves it to the default when the time is up.
#
# The caller hears it ring all the while, and a carrier sends a call nobody answers to
# voicemail 20-30 s in. So the answer time runs from the ring, not from the end of the
# question, and the question is short (a number is said by its last digits:
# session_arbiter.prompt_line). With nobody answering, the caller is connected 12-14 s
# after it rang: the question ~3.5-6 s, the deadline 8 s after it began (~9 s for a
# number: ANSWER_MIN_SECS after it), then "back in a moment" and the call's pipeline,
# greeting and answer (sip_server), ~4 s. It was ~26 s while the deadline ran 12 s from
# the end of a question that read the whole number (8 s), and the caller had given up by
# then (seen live, 2026-11-06). Someone talking at the deadline adds up to
# ANSWER_GRACE_SECS. A question cut before its ask and asked again still counts from the
# ring: the re-ask's ANSWER_MIN_SECS then sets the deadline, and the caller is connected
# ~16.5 s after the ring, ~17.5 s at most (the user talking on: REASK_WAIT_SECS).
#
import asyncio
from dataclasses import dataclass

from loguru import logger

from teaport_brain import session_arbiter as arb
from teaport_brain.settings import setting
from teaport_brain.transcript_ledger import _unheard

# How long the user has to answer, from the start of the question (the phone rang then).
PROMPT_SECS = setting("TEAPORT_CALL_PROMPT_SECS")
# However long the question took to say, the answer time ends no sooner than this after
# it (or PROMPT_SECS, when that is shorter): time to start answering, which the grace
# below then lets finish.
ANSWER_MIN_SECS = 3.0
# What happens when nobody answers: "take" (pick up) or "ring" (let it ring).
PROMPT_DEFAULT = setting("TEAPORT_CALL_PROMPT_DEFAULT")
# A turn in flight at the deadline gets this much longer to become the answer.
ANSWER_GRACE_SECS = 5.0
# How long the question itself may take to be said before it counts as said anyway (the
# longest, a 40-character name, takes ~6 s).
QUESTION_MAX_SECS = 8.0
# How long the line said on the way to the hold ("back in a moment") may take.
STEP_AWAY_MAX_SECS = 8.0
# A question cut with at least this share of its ask heard (by letters and digits) was
# heard: someone answering "yes" over "…step away for a moment?" heard what was asked.
ASK_HEARD_SHARE = 0.5
# A question cut before its ask is asked again this many times at most.
REASKS = 1
# The re-ask waits for the user to finish speaking, but no longer than REASK_WAIT_SECS,
# and goes in by REASK_BY_SECS after the ring whatever they are doing (the phone
# outranks the room): with the question said again (~6 s), ANSWER_MIN_SECS after it and
# the hold, the caller is connected by ~18 s.
REASK_WAIT_SECS = 2.5
REASK_BY_SECS = 4.0
# How long a cut question's line may take to be charted by the ledger (it charts at the
# cut's sighting at the TTS, a few milliseconds from the gate's).
CHART_WAIT_SECS = 0.5
# What answer() made of the user's answer. TAKEN / DECLINED: it answered the question.
# Once the question has closed, an answer changes nothing and is told what did happen:
# CLOSED_TAKEN (the call was taken: nobody answered in time, or the caller was already
# connected), CLOSED_RINGING (it was left ringing), GONE (the caller hung up first), or
# NONE (nothing was asked, or the conversation has been back from the call since).
# UNHEARD: the question is open but the user has not heard it (cut before its ask, or
# not said yet); the answer is not taken.
TAKEN, DECLINED, GONE, NONE, UNHEARD = "taken", "declined", "gone", "none", "unheard"
CLOSED_TAKEN, CLOSED_RINGING = "closed-taken", "closed-ringing"


def ask_heard(line: str, heard_text: str, ask: str) -> bool:
    """`line` was cut with `heard_text` of it heard: had its `ask` (prompt_ask, the line's
    ending) been heard, ASK_HEARD_SHARE of it at least? By letters and digits, as the
    ledger aligns a heard prefix (_unheard); not aligned at all, it was not heard. An
    empty ask (forms that no longer share one) needs the whole line."""
    rest = _unheard(line, heard_text)
    if rest is None:
        return False
    unheard = sum(ch.isalnum() for ch in rest)
    whole = sum(ch.isalnum() for ch in ask)
    if whole == 0:
        return unheard == 0
    return whole - min(unheard, whole) >= ASK_HEARD_SHARE * whole


@dataclass
class _Asked:
    """One saying of the question: its line and ask, the watch on its delivery (None until
    it is queued), and where the ledger's chart stood before it."""
    line: str
    ask: str
    ledger: object
    mark: int
    delivery: object = None


class CallPrompt:
    """One conversation's side of the question: asks it (ask), takes the answer from the
    model's tool call (answer), and says what goes with the hold. Bound to its session by
    build_agent_session; only a session whose model has answer_phone_call has one."""

    def __init__(self):
        self.session = None
        self._pending: asyncio.Future | None = None
        # What became of the last question, for an answer that comes after it closed.
        self._last: str | None = None
        # The question as last said (ask), for whether it was heard (heard).
        self._asked: _Asked | None = None

    @property
    def pending(self) -> bool:
        return self._pending is not None and not self._pending.done()

    def _lang(self) -> str | None:
        return getattr(getattr(self.session, "tts", None), "espeak_language", None)

    async def ask(self, caller: str | None) -> bool:
        """Ask the user whether to take a call from `caller` (None: no name or number);
        True to take it. Cancelled when the caller hangs up: the question is withdrawn."""
        session = self.session
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending, self._last = fut, None
        asked_at = loop.time()
        try:
            line = arb.prompt_line(caller, self._lang())
            ask = arb.prompt_ask(self._lang())
            for again in range(REASKS + 1):
                await self._say(line, ask, fut)
                if fut.done() or again == REASKS or await self._heard_once_charted():
                    break
                # Cut before its ask: the user did not hear what was asked. Once they
                # have finished speaking, asked again -- over the model's reply to what
                # they said, if one has started (the phone ringing over it, as at first).
                logger.info("phone call prompt: the question was cut before its ask — "
                            "asking again once the user has finished speaking")
                try:
                    await asyncio.wait_for(session.followup_gate.speech_over(),
                                           max(0.0, min(REASK_WAIT_SECS, asked_at
                                                        + REASK_BY_SECS - loop.time())))
                except asyncio.TimeoutError:
                    pass
                if fut.done():
                    break
            # The deadline counts from the ring, re-ask or not; a re-ask only moves its
            # floor (ANSWER_MIN_SECS after the line was said).
            if not fut.done():
                deadline = max(asked_at + PROMPT_SECS,
                               loop.time() + min(PROMPT_SECS, ANSWER_MIN_SECS))
                try:
                    await asyncio.wait_for(asyncio.shield(fut), deadline - loop.time())
                except asyncio.TimeoutError:
                    # The user may be answering right now: a turn in flight gets a little
                    # longer to reach the tool.
                    grace = loop.time() + ANSWER_GRACE_SECS
                    while (not fut.done() and not session.followup_gate.is_clear()
                           and loop.time() < grace):
                        await asyncio.wait({fut}, timeout=0.1)
            if fut.done():
                take = fut.result()
                logger.info("phone call prompt: the user said "
                            + ("take it" if take else "don't pick up"))
                self._last = CLOSED_TAKEN if take else CLOSED_RINGING
                return take
            take = PROMPT_DEFAULT == "take"
            fut.set_result(take)
            self._last = CLOSED_TAKEN if take else CLOSED_RINGING
            waited = loop.time() - asked_at
            logger.info(f"phone call prompt: no answer {waited:.1f} s after the question "
                        "began — "
                        + ("taking the call" if take else "letting it ring")
                        + " (TEAPORT_CALL_PROMPT_DEFAULT)")
            return take
        except asyncio.CancelledError:
            if not fut.done():
                fut.cancel()
                # Withdrawn: the caller hung up -- or got connected, and the call is
                # taken all the same (the arbiter; hold() then says so).
                self._last = GONE
                logger.info("phone call prompt: withdrawn")
            raise
        finally:
            if self._pending is fut:
                self._pending = None

    async def _say(self, line: str, ask: str, fut: asyncio.Future) -> None:
        """Say the question, and wait until it has played, been cut, or been answered
        (QUESTION_MAX_SECS at most)."""
        session = self.session
        ledger = getattr(session, "ledger", None)
        asked = self._asked = _Asked(line, ask, ledger,
                                     len(ledger.events) if ledger is not None else 0)
        # A phone ringing stops the sentence it rings over: the brain's own cut. And
        # nothing pauses the question -- the ringtone itself read as speech paused it
        # over and over (2026-11-06): the user answers over it or after it.
        async with session.own_line(line, append_to_context=True,
                                    unpausable=True) as delivery:
            asked.delivery = delivery
            said = asyncio.ensure_future(delivery.done.wait())
            try:
                await asyncio.wait({fut, said}, timeout=QUESTION_MAX_SECS,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                said.cancel()
                session.followup_gate.drop_delivery(delivery)

    def _charted(self, asked: _Asked):
        """The ledger's chart of this saying of the question, or None (not charted: cut
        before any of it played, or not yet)."""
        if asked.ledger is None:
            return None
        for u in asked.ledger.events[asked.mark:]:
            if u.speaker == "assistant" and u.text == asked.line.strip():
                return u
        return None

    def heard(self) -> bool:
        """Has the user heard the question as last said? Played through, or still playing
        uncut (heard as it goes); cut, only once its ask had been heard (ask_heard, by the
        ledger's chart of it). Not queued yet, or cut with nothing charted: not heard."""
        asked = self._asked
        if asked is None or asked.delivery is None:
            return False
        d = asked.delivery
        if not d.done.is_set() or not d.interrupted:
            return True
        u = self._charted(asked)
        if u is None:
            return False
        return not u.cut_short or ask_heard(u.text, u.heard_text, asked.ask)

    async def _heard_once_charted(self) -> bool:
        """heard(), giving a cut a moment to reach the ledger's chart first."""
        asked = self._asked
        loop = asyncio.get_running_loop()
        until = loop.time() + CHART_WAIT_SECS
        while (asked is not None and asked.delivery is not None and asked.ledger is not None
               and asked.delivery.done.is_set() and asked.delivery.interrupted
               and self._charted(asked) is None and loop.time() < until):
            await asyncio.sleep(0.02)
        return self.heard()

    def answer(self, take: bool) -> str:
        """The model's reading of the user's reply (the answer_phone_call tool): TAKEN or
        DECLINED when it answered the open question, UNHEARD (not taken) while the user
        has not heard it; once it has closed, what did happen (CLOSED_TAKEN,
        CLOSED_RINGING, GONE) or NONE -- see the outcome names above."""
        fut = self._pending
        if fut is None or fut.done():
            return self._last or NONE
        if not self.heard():
            logger.info("phone call prompt: an answer to a question the user has not "
                        "heard — not taken")
            return UNHEARD
        fut.set_result(bool(take))
        return TAKEN if take else DECLINED

    async def hold(self) -> None:
        """Taken: the agent says it is stepping away, and the conversation goes on hold."""
        self._last = CLOSED_TAKEN
        await self.session.hold(arb.step_away_line(self._lang()), STEP_AWAY_MAX_SECS)

    async def resume(self) -> None:
        """The call is over: back, with an apology for the wait. The question is history:
        an answer to it from here is answered as one to nothing."""
        self._last = None
        await self.session.resume(arb.back_line(self._lang()))
