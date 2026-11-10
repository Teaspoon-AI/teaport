#
# #386: a consult that fails before its placeholder is in the context.
#
# The issue's repro, through the real consult handler, the real LLM service's function
# call path and the real assistant aggregator: TEAPORT_ASYNC_CONSULT_TIMEOUT=5 leaves the
# gateway's lane less than its 5 s minimum, so every consult fails at once with "no budget
# for the warm lane" -- no I/O, nothing scripted. The call's frames are held on their way
# to the aggregator until the consult has landed, so the landing comes first, every time:
#
#   - ahead of both frames: the rewrite finds no tool message at all;
#   - between them: it finds pipecat's IN_PROGRESS, which the placeholder's result frame
#     then writes over.
#
# Either way the tool result must end up as the failure, as after a slow landing, and the
# one owed record closes on a reply that tells it. Before #386 the placeholder ("nothing
# has come back yet") stayed for good, and the model said "I don't have those results
# yet" beside a note saying the lookup had failed (Owed.tla ow_rewriteOnce).
#
# Run: python test_early_landing.py   (or via the suite)
#
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    FunctionCallFromLLM,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402

from test_pipecat_premises import Run, _llm, _marked, _until  # noqa: E402
from test_reply_hold import Tap  # noqa: E402

from teaport_brain import openclaw_client as oc  # noqa: E402
from teaport_brain import owed as owed_mod  # noqa: E402
from teaport_brain import tools  # noqa: E402
from teaport_brain.owed import CLOSED, OWED, Owed, Reply  # noqa: E402

REQUEST = "Find bookshops on Elm Street in Riverton."
QUESTION = "Can you find bookshops on Elm Street in Riverton?"
TOLD = "Sorry, the bookshop search for Elm Street ran out of time, so I don't have a list."


class Gate:
    """The gate the book asks its run of (FollowupGate.ask), recording it."""
    caller_request_pending = False

    def __init__(self):
        self.asked = []

    def ask(self, keys, purpose, context):
        self.asked.append((list(keys), purpose))


class Hold(FrameProcessor):
    """Holds the call's frames of the classes in `held` (downstream) until `release`, in
    order, so the consult lands before the assistant aggregator takes them."""

    def __init__(self, held):
        super().__init__()
        self.held = held
        self.release = asyncio.Event()
        self.holding = asyncio.Event()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, self.held):
            self.holding.set()
            await self.release.wait()
        await self.push_frame(frame, direction)


def _tool(context):
    return next((m.get("content") for m in context.get_messages()
                 if m.get("role") == "tool" and m.get("tool_call_id") == "call-1"), None)


async def _fails_before(held) -> tuple:
    """One consult through the handler with its `held` frames held until it has landed.
    Returns (the tool result as the context ends up, the book, the gate, the tool result
    the rewrite found at the landing)."""
    context = LLMContext([{"role": "user", "content": QUESTION}])
    gate = Gate()
    book = Owed(context, gate=gate)
    llm, hold, below = _llm(), Hold(held), Tap()
    assistant = LLMContextAggregatorPair(context).assistant()
    llm.register_function(OPENCLAW, tools._bind_openclaw_agent_consult(
        tools.ToolContext(owed=book)))
    saved = (tools._ASYNC_CONSULT_TIMEOUT, oc._token)
    tools._ASYNC_CONSULT_TIMEOUT, oc._token = 5, lambda: "a-token"
    try:
        async with Run(llm, hold, assistant, below) as run:
            await llm.run_function_calls([FunctionCallFromLLM(
                function_name=OPENCLAW, tool_call_id="call-1",
                arguments={"question": REQUEST}, context=context)])
            assert await _until(lambda: book.record("call-1") is not None), \
                "the consult never landed"
            at_landing = _tool(context)
            assert hold.holding.is_set(), "the call's frames were not held"
            hold.release.set()
            await _marked(run, below)
            # The placeholder's on_context_updated runs in a task of the aggregator's: wait
            # for the tool result to settle on something other than IN_PROGRESS and the
            # placeholder (before #386 it settles on the placeholder, and the checks fail).
            await _until(lambda: (c := _tool(context)) is not None and c != "IN_PROGRESS"
                         and "working_in_background" not in c)
            return _tool(context), book, gate, at_landing
    finally:
        tools._ASYNC_CONSULT_TIMEOUT, oc._token = saved


OPENCLAW = tools.OPENCLAW_AGENT_CONSULT.name


def _is_the_failure(content, book):
    rec = book.record("call-1")
    assert rec is not None and rec.failure == "timeout", rec
    assert json.loads(content) == {"status": "timeout", "error": rec.outcome}, content
    assert "ran out of time" in rec.outcome


async def test_a_consult_that_fails_before_its_call_is_in_the_context():
    """The landing comes before both frames: no tool message to rewrite."""
    content, book, gate, at_landing = await _fails_before(
        (FunctionCallInProgressFrame, FunctionCallResultFrame))
    assert at_landing is None, at_landing
    _is_the_failure(content, book)
    assert gate.asked == [(["call-1"], owed_mod.RUN)], gate.asked
    assert [r.key for r in book.owed()] == ["call-1"]


async def test_a_consult_that_fails_before_its_placeholder_is_in_the_context():
    """The landing comes between the frames: it writes over IN_PROGRESS, and the
    placeholder's result frame writes over that."""
    content, book, _, at_landing = await _fails_before((FunctionCallResultFrame,))
    assert at_landing is not None and at_landing != "IN_PROGRESS", (
        "the landing was not written over IN_PROGRESS")
    _is_the_failure(content, book)


async def test_the_early_failure_closes_on_a_reply_that_tells_it():
    """One owed record, and a reply about it heard to its end closes it."""
    _, book, _, _ = await _fails_before((FunctionCallInProgressFrame, FunctionCallResultFrame))
    assert book.record("call-1").state == OWED
    reply = Reply(owed_mod.RUN, ("call-1",))
    book.noted(reply, ("call-1",), book.context.get_messages())
    book.settle(reply, TOLD, cut=False, audio=True, text=TOLD)
    # The failure judge's verdict (none here: no judge wired) lands on the book (#380);
    # the failure's own run closes it on no verdict.
    await _until(lambda: book.record("call-1").state == CLOSED)
    assert book.record("call-1").state == CLOSED and book.owed() == []


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
