#
# Unit test: a consult answer the caller did not HEAR has not been delivered.
#
# The injector could already tell that a completion READ its trigger, and treated that
# as success. But the trigger is read the moment the model starts answering, which is
# before a single sample reaches the caller — so a delivery barged over at 0% heard was
# retired as delivered and the answer was gone.
#
# Live 2026-09-10 17:13, the call that prompted this:
#
#   17:13:21.811  BotStoppedSpeaking t=96.08        the previous reply ends
#   17:13:22.512  consult follow-up: delivering     the gate releases into real silence
#   17:13:23.571  TTSStartedFrame t=97.84           "About those pastries at Neon Belly…"
#   17:13:23.655  InterimTranscriptionFrame ' I'    84 ms later
#   17:13:23.656  User started speaking -> InterruptionFrame
#   17:13:23.657  LEDGER +assistant CUT heard~0%
#
# The caller had started speaking at t=96.8 and the gate released at t=96.79: it lost
# by about ten milliseconds, which no amount of waiting fixes. What fixes it is noticing
# afterwards. The caller then asked for that same answer three more times.
#
# heard_fraction is the signal, and only the ledger has it. These tests pin the loop's
# use of it, and the last one pins the ledger's side of the contract against the REAL
# class -- three bugs in this repo have passed against stubs that were easier to satisfy
# than the thing they stood for.
#
# Run: python test_followup_heard.py   (or via the suite)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from teaport_brain import agent_session  # noqa: E402
from teaport_brain.agent_session import _make_consult_followup  # noqa: E402

from test_followup_injection import (  # noqa: E402
    ANSWER, CALL_ID, REQUEST, _Context, _Gate, _Ledger, _Retirer, _Task,
)

agent_session._DELIVERY_START_TIMEOUT = 0.05
agent_session._DELIVERY_HEARD_TIMEOUT = 0.05


async def _deliver(heard, read_on_attempt=1, text=ANSWER):
    ctx = _Context()
    retirer = _Retirer()
    task = _Task(ctx, retirer, read_on_attempt)
    gate = _Gate()
    ledger = _Ledger(heard)
    await _make_consult_followup(task, ctx, gate, retirer, ledger)(REQUEST, text, CALL_ID)
    return ctx, task, retirer, gate, ledger


async def test_a_delivery_the_caller_heard_is_not_repeated():
    _ctx, task, _r, _g, _l = await _deliver(1.0)
    assert task.attempts == 1, (
        f"queued {task.attempts} turns for an answer that was fully heard — repeating a "
        "delivery the caller already got is the recital bug this must not reintroduce")


async def test_an_answer_barged_over_at_zero_percent_is_said_again():
    """The live failure. heard~0% means the caller got nothing at all."""
    _ctx, task, _r, gate, _l = await _deliver([0.0, 1.0])
    assert task.attempts == 2, (
        f"queued {task.attempts} turn(s) — an answer cut before any of it played was "
        "retired as delivered, which is exactly how the caller lost it live")
    handed = task.at_run[-1]["content"]
    assert ANSWER in handed and "Tell the user now" in handed, (
        "the retry handed the model a trigger that had already been retired — the "
        "restore is missing, so the second attempt says nothing useful")
    assert gate.idle_waits >= 2, "each retry must wait for its own clear moment"


async def test_a_mostly_heard_answer_is_left_alone():
    """Above the line the caller has the answer; saying it again is the worse failure."""
    _ctx, task, _r, _g, _l = await _deliver(0.6)
    assert task.attempts == 1, f"repeated an answer heard at 60% ({task.attempts} turns)"


async def test_the_threshold_is_where_it_says_it_is():
    below, above = agent_session._MIN_HEARD - 0.01, agent_session._MIN_HEARD
    _c, t_below, _r, _g, _l = await _deliver([below, 1.0])
    _c, t_above, _r, _g, _l = await _deliver(above)
    assert t_below.attempts == 2, "just below the threshold must retry"
    assert t_above.attempts == 1, "at the threshold must not"


async def test_it_stops_after_the_attempt_budget():
    ctx, task, _r, _g, _l = await _deliver([0.0] * 6)
    assert task.attempts == agent_session._DELIVERY_ATTEMPTS, (
        f"{task.attempts} attempts for a budget of {agent_session._DELIVERY_ATTEMPTS}")
    after = ctx.messages[-1]["content"]
    assert "already given to the user" in after, (
        "the trigger was left live after the last attempt — a standing 'tell the user "
        "now' is recited by the next turn that has nothing else to do")


async def test_an_uncharted_reply_is_not_repeated():
    """If the ledger never charts the reply we cannot tell, and not knowing is not
    grounds to say it twice."""
    _ctx, task, _r, _g, _l = await _deliver(None)
    assert task.attempts == 1, (
        "repeated a delivery whose outcome was unknown — an unnecessary repeat is worse "
        "than an unverified delivery")


async def test_a_flushed_turn_still_retries_on_its_own_terms():
    """The pre-existing path must survive: read_on_attempt=2 is a barge-in eating the
    queued turn before any completion reads it, which is a different failure from one
    that was read and not heard."""
    _ctx, task, _r, _g, ledger = await _deliver(1.0, read_on_attempt=2)
    assert task.attempts == 2, "a flushed turn must still be re-queued"
    assert ledger.handed == 2, (
        "the heard-waiter must be armed once per attempt, and cancelled with the "
        "attempt that was flushed")


async def test_the_REAL_ledger_resolves_the_waiter():
    """Pin the ledger's half of the contract against the real class.

    The stub above could drift from TranscriptLedger and every test here would still
    pass while production never resolved the future and every delivery waited out
    _DELIVERY_HEARD_TIMEOUT before returning 'unknown'. That is the shape of the
    read-only `sample_rate` bug (test_vad_sample_rate.py) and of the sync `cancel_task`
    bug: a stub easier to satisfy than the class it stands for tests nothing.
    """
    from teaport_brain.transcript_ledger import TranscriptLedger, Utterance

    led = TranscriptLedger()
    fut = led.next_assistant()

    # a USER utterance must not satisfy it — the injector is waiting for the reply
    led._add(Utterance("user", "unrelated", 0.0, 1.0))
    assert not fut.done(), "a user utterance resolved the assistant waiter"

    led._add(Utterance("assistant", "About those pastries", 1.0, 2.0,
                       heard_fraction=0.0, heard_text=""))
    assert fut.done(), (
        "TranscriptLedger._add no longer resolves next_assistant() — every consult "
        "delivery will now wait out the heard timeout and report 'unknown'")
    assert fut.result().heard_fraction == 0.0, fut.result()

    # and it is one-shot: the next utterance must not need a waiter to exist
    led._add(Utterance("assistant", "another", 2.0, 3.0))


def main():
    async def run_all():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and asyncio.iscoroutinefunction(fn):
                await fn()
                print(f"  ok {name}")
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
    print("ALL PASS")
