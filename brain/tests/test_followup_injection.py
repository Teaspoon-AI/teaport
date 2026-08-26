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

from teaport_brain.gateway_server import _make_consult_followup  # noqa: E402

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


class _Task:
    def __init__(self, ctx):
        self.ctx = ctx
        self.frames = []
        self.at_run = None      # what the model would actually have seen

    async def queue_frames(self, frames):
        self.frames.extend(frames)
        self.at_run = copy.deepcopy(self.ctx.messages)


class _Gate:
    async def wait_until_idle(self):
        return

    async def wait_until_delivered(self, start_timeout: float = 10.0):
        return


async def _deliver(text):
    ctx = _Context()
    task = _Task(ctx)
    await _make_consult_followup(task, ctx, _Gate())(REQUEST, text, CALL_ID)
    return ctx, task


async def test_the_instruction_is_a_user_turn_not_a_system_message():
    _, task = await _deliver(ANSWER)
    assert task.frames, "the follow-up must actually run the LLM"
    last = task.at_run[-1]
    assert last["role"] == "user", (
        f"injected as {last['role']!r} — a trailing system message is not a turn the "
        f"model answers, so it re-answers the previous question instead")
    assert "not spoken by the user" in last["content"], "missing the provenance tag"
    assert ANSWER in last["content"], "the answer itself must reach the model"


async def test_the_trigger_does_not_outlive_its_turn():
    """A standing 'Tell the user now...' gets re-executed on the next empty turn."""
    ctx, _ = await _deliver(ANSWER)
    after = ctx.messages[-1]["content"]
    assert "Tell the user now" not in after, (
        "the delivery instruction is still in the context — the model will recite the "
        "answer again the next time a turn gives it nothing else to do")
    assert ANSWER not in after, "the answer must not remain as a standing order"
    assert "already given to the user" in after, after


async def test_the_placeholder_tool_result_is_rewritten():
    ctx, _ = await _deliver(ANSWER)
    tool = [m for m in ctx.messages if m.get("role") == "tool"][0]
    body = json.loads(tool["content"])
    assert body["status"] == "complete", body
    assert body["answer"] == ANSWER, body


async def test_a_consult_that_never_reported_is_not_called_a_failure():
    """It can die on teardown AFTER the action landed, so asserting failure can lie."""
    ctx, task = await _deliver(None)
    tool = [m for m in ctx.messages if m.get("role") == "tool"][0]
    assert json.loads(tool["content"])["status"] == "unknown"
    assert ctx.messages[-1]["role"] == "user"
    assert task.frames


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
