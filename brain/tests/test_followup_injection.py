#
# Unit test: the async ask_openclaw follow-up must be injected as a USER turn.
#
# When a background consult finishes, the injector rewrites the placeholder tool result
# and appends a delivery instruction, then runs the LLM so the bot speaks the answer.
# That instruction used to be appended with role "system", and a trailing system message
# is not a turn the model answers — it re-answers the last real user question instead.
#
# Live 2026-08-26 07:59: asked for pastry shops on Burnett Road, the consult came back
# with "Upper Crust Bakery" while the user had moved on to what pastry pairs with an
# espresso. The follow-up fired and the bot answered the espresso question a SECOND time;
# the shop name never reached the user. Replaying that exact 66-message context against
# the live model, the system role delivered the answer 0/4 times with an unrelated
# exchange in between and 1/4 with none, leaking "background task"/"agent" wording 2/4.
# As a user message it delivered 8/8 and leaked nothing.
#
# The tag is part of the contract, not decoration: the message outlives the turn, so
# without it the rest of the session reads a request the user never made.
#
# Run: python test_followup_injection.py
#
import asyncio
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from teaport_brain import agent_session  # noqa: E402
from teaport_brain.agent_session import _make_consult_followup  # noqa: E402
from teaport_brain.followup_gate import OneShot  # noqa: E402

# Don't sit through the real 20s flush timeout in the retry tests.
agent_session._DELIVERY_START_TIMEOUT = 0.05

REQUEST = "Find good pastry shops on or near Burnett Road in Austin, Texas."
ANSWER = "The clear standout on Burnet Road is Upper Crust Bakery (4508 Burnet Rd)."
CALL_ID = "fc_dccd2674"


class _Context:
    def __init__(self):
        # The placeholder the async path left behind, as the live context carries it.
        self.messages = [
            {"role": "user", "content": REQUEST},
            {"role": "assistant", "tool_calls": [{"id": CALL_ID, "type": "function"}]},
            {"role": "tool", "tool_call_id": CALL_ID,
             "content": json.dumps({"status": "working_in_background"})},
        ]

    def get_messages(self):
        return self.messages

    def add_message(self, m):
        self.messages.append(m)


class _Retirer:
    """Stands in for FollowupTrigger. Its contract is the whole point: an armed
    one-shot fires when — and only when — a completion ANSWERS FROM the context."""

    def __init__(self):
        self.armed = []

    def arm(self, retire):
        shot = OneShot(retire)
        self.armed.append(shot)
        return shot

    def disarm(self, shot):
        if shot in self.armed:
            self.armed.remove(shot)

    def read(self):
        """A completion produced text from the context it was given."""
        armed, self.armed = self.armed, []
        for shot in armed:
            shot.fire()


class _Task:
    """`read_on_attempt` is which queued turn actually reaches the model. Anything
    earlier stands for a turn flushed by a barge-in before the model read it."""

    def __init__(self, ctx, retirer, read_on_attempt=1):
        self.ctx = ctx
        self.retirer = retirer
        self.read_on_attempt = read_on_attempt
        self.frames = []
        self.attempts = 0
        self.at_run = None      # what the model would actually have seen

    async def queue_frames(self, frames):
        self.frames.extend(frames)
        self.attempts += 1
        # Snapshot BEFORE the read: this is the context the completion was handed.
        self.at_run = copy.deepcopy(self.ctx.messages)
        if self.attempts >= self.read_on_attempt:
            self.retirer.read()


class _Gate:
    def __init__(self):
        self.idle_waits = 0

    async def wait_until_idle(self):
        self.idle_waits += 1
        return True


async def _deliver(text, read_on_attempt=1):
    ctx = _Context()
    retirer = _Retirer()
    task = _Task(ctx, retirer, read_on_attempt)
    gate = _Gate()
    await _make_consult_followup(task, ctx, gate, retirer)(REQUEST, text, CALL_ID)
    return ctx, task, retirer, gate


async def test_the_instruction_is_a_user_turn_not_a_system_message():
    _, task, _r, _g = await _deliver(ANSWER)
    assert task.frames, "the follow-up must actually run the LLM"
    last = task.at_run[-1]
    assert last["role"] == "user", (
        f"injected as {last['role']!r} — a trailing system message is not a turn the "
        f"model answers, so it re-answers the previous question instead")
    assert "not spoken by the user" in last["content"], "missing the provenance tag"
    assert ANSWER in last["content"], "the answer itself must reach the model"


async def test_the_trigger_does_not_outlive_its_turn():
    """A standing 'Tell the user now...' gets re-executed on the next empty turn."""
    ctx, _t, _r, _g = await _deliver(ANSWER)
    after = ctx.messages[-1]["content"]
    assert "Tell the user now" not in after, (
        "the delivery instruction is still in the context — the model will recite the "
        "answer again the next time a turn gives it nothing else to do")
    assert ANSWER not in after, "the answer must not remain as a standing order"
    assert "already given to the user" in after, after


async def test_the_placeholder_tool_result_is_rewritten():
    ctx, _t, _r, _g = await _deliver(ANSWER)
    tool = [m for m in ctx.messages if m.get("role") == "tool"][0]
    body = json.loads(tool["content"])
    assert body["status"] == "complete", body
    assert body["answer"] == ANSWER, body


async def test_a_consult_that_never_reported_is_not_called_a_failure():
    """It can die on teardown AFTER the action landed, so asserting failure can lie."""
    ctx, task, _r, _g = await _deliver(None)
    tool = [m for m in ctx.messages if m.get("role") == "tool"][0]
    assert json.loads(tool["content"])["status"] == "unknown"
    assert ctx.messages[-1]["role"] == "user"
    assert task.frames


# --- retirement timing: the TLC counterexample, as a test -----------------------
#
# The three tests below come straight out of brain/formal/Followup.tla. Retiring the
# one-shot trigger is a two-sided constraint and the old code satisfied only one side:
#
#   too early -> the answer is never spoken   (NoSilentLoss,     MODE = "asWritten")
#   too late  -> a later turn recites it again (NoRepeatRecital, MODE = "gateOnOwn")
#
# gate.wait_until_delivered() waited on _busy/_idle, which ANY activity sets, so the
# user speaking just after a consult landed satisfied it and the trigger was retired
# having never been read. TLC's counterexample is 8 steps and needs nothing exotic:
#
#   FWaitIdle -> FQueue -> UserStart -> FWaitBusy -> UserStop -> FWaitIdle2
#             -> FNeutralize   (reads = 0)
#
# `read_on_attempt=2` is that trace: the first queued turn is flushed by the barge-in
# before any completion reads it.


async def test_a_flushed_turn_does_not_retire_the_trigger_unread():
    """The TLC counterexample. The answer must survive a barge-in that eats the turn."""
    ctx, task, retirer, gate = await _deliver(ANSWER, read_on_attempt=2)
    assert task.attempts == 2, (
        f"queued {task.attempts} turn(s) — a turn flushed before the model read it must "
        f"be re-queued, not counted as delivered")
    handed = task.at_run[-1]["content"]
    assert ANSWER in handed and "Tell the user now" in handed, (
        "the retried turn handed the model a trigger that had already been retired — "
        "this is the silent-loss bug (NoSilentLoss, Followup.tla)")
    assert gate.idle_waits >= 2, "each retry must wait for its own clear moment"
    after = ctx.messages[-1]["content"]
    assert "already given to the user" in after, "retired once delivered, as before"


async def test_the_trigger_is_retired_at_the_read_not_after_it():
    """Between reading the trigger and retiring it, a second completion must not be
    able to read it too — that is the bot reciting the same answer twice."""
    ctx, task, retirer, _g = await _deliver(ANSWER)
    assert not retirer.armed, (
        "a one-shot is still armed after delivery — it would fire on an unrelated "
        "later turn")
    # The read and the retirement are the same event, so the message the model was
    # handed is live and the message left behind is not.
    assert "Tell the user now" in task.at_run[-1]["content"]
    assert "Tell the user now" not in ctx.messages[-1]["content"]


async def test_an_undeliverable_answer_is_not_left_as_a_standing_order():
    """If no turn ever reads it, give up — but do NOT leave the instruction live."""
    ctx, task, retirer, _g = await _deliver(ANSWER, read_on_attempt=99)
    assert task.attempts == agent_session._DELIVERY_ATTEMPTS, task.attempts
    assert not retirer.armed, "left an armed one-shot behind after giving up"
    after = ctx.messages[-1]["content"]
    assert "Tell the user now" not in after, (
        "gave up but left the delivery instruction in the context — the next turn "
        "with nothing else to do will execute it")
    # The answer itself is still recoverable from the rewritten tool result.
    tool = [m for m in ctx.messages if m.get("role") == "tool"][0]
    assert json.loads(tool["content"])["answer"] == ANSWER


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run_aio():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
