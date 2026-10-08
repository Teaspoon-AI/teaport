#
# teaport — asking a live conversation whether to take a phone call (issue #111).
#
# A call that lands while a conversation is live -- a remote Talk session, or the room
# mic awake -- is the people in that conversation's to decide (the session arbiter's
# PROMPT). The agent asks them, inside the conversation and in its language:
#
#   "Someone's calling me — +1 346 234 8500. Should I step away for a moment?"
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
#   * No answer within TEAPORT_CALL_PROMPT_SECS of the question: TEAPORT_CALL_PROMPT_DEFAULT,
#     "take" (pick up: the room cannot always answer, and a call let ring by nobody's
#     choice is a call missed) or "ring". A turn still under way at the deadline (the user
#     answering, the model about to call the tool) gets a few seconds more.
#   * The caller hangs up while it is asked: the question is withdrawn. A late "yes" is
#     told the caller is gone.
#
import asyncio

from loguru import logger
from pipecat.frames.frames import InterruptionWorkerFrame, TTSSpeakFrame

from teaport_brain import session_arbiter as arb
from teaport_brain.env import env_choice, env_num

# How long the user has to answer, from the end of the question.
PROMPT_SECS = env_num("TEAPORT_CALL_PROMPT_SECS", "12", float)
# What happens when nobody answers: "take" (pick up) or "ring" (let it ring).
PROMPT_DEFAULT = env_choice("TEAPORT_CALL_PROMPT_DEFAULT", "take", ("take", "ring"))
# A turn in flight at the deadline gets this much longer to become the answer.
ANSWER_GRACE_SECS = 5.0
# How long the question itself may take to be said before its answer time starts anyway.
QUESTION_MAX_SECS = 12.0
# How long the line said on the way to the hold ("back in a moment") may take.
STEP_AWAY_MAX_SECS = 8.0
# How long whatever was being said gets to stop before the question is put.
_SETTLE_SECS = 0.2

TAKEN, DECLINED, GONE, NONE = "taken", "declined", "gone", "none"


class CallPrompt:
    """One conversation's side of the question: asks it (ask), takes the answer from the
    model's tool call (answer), and says what goes with the hold. Bound to its session by
    build_agent_session; only a session whose model has answer_phone_call has one."""

    def __init__(self):
        self.session = None
        self._pending: asyncio.Future | None = None
        # What became of the last question, for an answer that comes after it closed.
        self._last: str | None = None

    @property
    def pending(self) -> bool:
        return self._pending is not None and not self._pending.done()

    def _lang(self) -> str | None:
        return getattr(getattr(self.session, "tts", None), "espeak_language", None)

    async def ask(self, caller: str | None) -> bool:
        """Ask the user whether to take a call from `caller` (None: withheld or unknown);
        True to take it. Cancelled when the caller hangs up: the question is withdrawn."""
        session = self.session
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending, self._last = fut, None
        try:
            line = arb.prompt_line(caller, self._lang())
            # A phone ringing stops the sentence it rings over.
            await session.task.queue_frames([InterruptionWorkerFrame()])
            await asyncio.sleep(_SETTLE_SECS)
            delivery = session.followup_gate.watch_delivery()
            await session.task.queue_frames([TTSSpeakFrame(line)])
            said = asyncio.ensure_future(delivery.done.wait())
            try:
                await asyncio.wait({fut, said}, timeout=QUESTION_MAX_SECS,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                said.cancel()
                session.followup_gate.drop_delivery(delivery)
            if not fut.done():
                try:
                    await asyncio.wait_for(asyncio.shield(fut), PROMPT_SECS)
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
                return take
            take = PROMPT_DEFAULT == "take"
            fut.set_result(take)
            self._last = TAKEN if take else DECLINED
            logger.info(f"phone call prompt: no answer within {PROMPT_SECS:g} s — "
                        + ("taking the call" if take else "letting it ring")
                        + " (TEAPORT_CALL_PROMPT_DEFAULT)")
            return take
        except asyncio.CancelledError:
            if not fut.done():
                fut.cancel()
                self._last = GONE
                logger.info("phone call prompt: withdrawn (the caller hung up)")
            raise
        finally:
            if self._pending is fut:
                self._pending = None

    def answer(self, take: bool) -> str:
        """The model's reading of the user's reply (the answer_phone_call tool): TAKEN or
        DECLINED when it answered the question, GONE when the caller hung up first, NONE
        when nothing was asked (or it was already decided)."""
        fut = self._pending
        if fut is None or fut.done():
            return self._last or NONE
        fut.set_result(bool(take))
        self._last = TAKEN if take else DECLINED
        return self._last

    async def hold(self) -> None:
        """Taken: the agent says it is stepping away, and the conversation goes on hold."""
        await self.session.hold(arb.step_away_line(self._lang()), STEP_AWAY_MAX_SECS)

    async def resume(self) -> None:
        """The call is over: back, with an apology for the wait."""
        await self.session.resume(arb.back_line(self._lang()))
