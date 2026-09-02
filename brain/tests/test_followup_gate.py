#
# Unit test: FollowupGate's "LLM mid-response" latch must always release.
#
# The gate sets _llm on LLMFullResponseStartFrame and used to clear it ONLY on
# LLMFullResponseEndFrame. But the end frame is not guaranteed to arrive at the
# gate: for a completion with no synthesizable text (a bare tool call — exactly
# the async ask_openclaw shape, where no_inference() means no later completion
# comes either), the TTS service holds the end frame waiting for an audio context
# that empty text never creates; and an interruption discards a held one outright.
# Either way the gate read "mid-response" forever: every consult narrator line
# timed out its 6s gap wait and was skipped, and the follow-up injector burned its
# full 60s max_wait — total dead air in genuine silence, the exact condition the
# narrator exists to fill.
#
# The release points asserted here: a FunctionCallInProgressFrame (the completion
# is done producing speech; any audio it did produce is tracked via the bot
# speaking flags) and an InterruptionFrame (the in-flight response is dead by
# definition).
#
# Run: python test_followup_gate.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Refuse to run against the wrong pipecat (a bare `import pinned_pipecat` checked
# nothing: require_pinned() has to be CALLED). It imports the package first, for
# HF_HUB_OFFLINE.
from pinned_pipecat import require_pinned  # noqa: E402
require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    FunctionCallInProgressFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain.followup_gate import FollowupGate  # noqa: E402


def _gate(quiet_secs=0.01):
    """A FollowupGate with push_frame stubbed out (no pipeline downstream)."""
    gate = FollowupGate(quiet_secs=quiet_secs)

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass
    gate.push_frame = fake_push

    # The base FrameProcessor reacts to InterruptionFrame with task-manager
    # bookkeeping that a bare, unwired processor doesn't have; the gate's own
    # handling (what these tests assert) runs regardless, so stub the hook.
    async def _no_interruption_bookkeeping(*a, **k):
        pass
    gate._start_interruption = _no_interruption_bookkeeping
    return gate


async def _feed(gate, *frames):
    for f in frames:
        await gate.process_frame(f, FrameDirection.DOWNSTREAM)


def _tool_call():
    return FunctionCallInProgressFrame(
        function_name="ask_openclaw", tool_call_id="tc-1", arguments={})


async def test_the_latch_latches():
    """Sanity for the tests below: a start frame with no release DOES hold the
    gate busy until max_wait — otherwise the release assertions prove nothing."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame())
    assert not await gate.wait_until_idle(max_wait=0.05), (
        "gate reported idle mid-response; the latch tests are vacuous")


async def test_end_frame_releases():
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
    assert await gate.wait_until_idle(max_wait=0.5)


async def test_a_bare_tool_call_completion_releases_the_latch():
    """The stranded-end shape: start frame arrives, end frame never does (held by
    the TTS service for a text-less completion). The function call starting is
    the proof the completion is done speaking — the gate must read idle."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), _tool_call())
    assert await gate.wait_until_idle(max_wait=0.5), (
        "gate still busy after a bare tool-call completion — the _llm latch is "
        "stuck and every narrator line will be skipped")


async def test_an_interruption_releases_the_latch():
    """An interruption kills the in-flight response AND discards any end frame
    the TTS service was holding, so it must clear the latch itself."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), InterruptionFrame())
    assert await gate.wait_until_idle(max_wait=0.5), (
        "gate still busy after an interruption — the _llm latch is stuck")


# --- the budget: one deadline for the whole call ---

async def test_max_wait_bounds_the_whole_call_not_just_the_idle_wait():
    """The debounce sleep used to be bounded by a `remaining` computed BEFORE the
    idle wait and reused after it, so a call could overrun max_wait by a whole
    quiet window -- measured at 1.65s for max_wait=1.0 with the real 0.7s window.
    With 0.25s of budget and idle arriving at 0.15s, only 0.1s remains: less than
    the window, so the answer is False, and it is given by the deadline."""
    gate = _gate(quiet_secs=0.3)
    await _feed(gate, LLMFullResponseStartFrame())          # busy

    async def release():
        await asyncio.sleep(0.15)
        await _feed(gate, LLMFullResponseEndFrame())
    task = asyncio.create_task(release())
    t0 = time.monotonic()
    ok = await gate.wait_until_idle(max_wait=0.25)
    dt = time.monotonic() - t0
    await task
    assert not ok, "a debounce cut to a fraction of the quiet window was reported as a window"
    assert dt < 0.3, f"wait_until_idle overran max_wait=0.25 by {dt - 0.25:.2f}s"


async def test_a_pause_shorter_than_the_quiet_window_is_not_a_window():
    """Idle from the start, but with a budget smaller than the window: the old
    code slept for the budget and returned True -- the narrator could then push
    its line into a 0.1s pause between the user's words."""
    gate = _gate(quiet_secs=0.3)
    assert not await gate.wait_until_idle(max_wait=0.1)
    assert await gate.wait_until_idle(max_wait=1.0)         # the same gate, given room


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
