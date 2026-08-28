#
# Unit test: FollowupTrigger retires the consult follow-up's one-shot AT the read.
#
# The follow-up's trigger message ("tell the user now...") is a standing order in the
# LLM context. Retiring it is a two-sided constraint and each side has cost a live
# session — see brain/formal/Followup.tla, which model-checks all three designs:
#
#   retired before any completion read it -> the answer is never spoken
#                                            (NoSilentLoss,     MODE = "asWritten")
#   still live after delivery             -> a later turn recites it again
#                                            (NoRepeatRecital,  MODE = "gateOnOwn")
#
# Only retiring at the read satisfies both, and THIS module is where that lands. Its
# correctness rests entirely on two facts about pipecat that are easy to get backwards,
# so they are asserted here rather than trusted:
#
#   1. LLMFullResponseStartFrame is pushed BEFORE the context is serialized into the
#      request (pipecat/services/openai/base_llm.py pushes it, then awaits
#      _process_context). Firing on it would neutralise the trigger in place before the
#      model ever saw it — losing EVERY answer, silently.
#   2. LLMTextFrame is the first frame that proves the request carried the trigger.
#
# Run: python test_followup_trigger.py
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pinned_pipecat  # noqa: F401,E402  — refuse to pass against the wrong pipecat

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain.followup_gate import FollowupTrigger  # noqa: E402


def _harness():
    """A FollowupTrigger whose push_frame is stubbed, plus the list of what it forwarded."""
    trigger = FollowupTrigger()
    forwarded = []

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        forwarded.append(frame)
    trigger.push_frame = fake_push
    return trigger, forwarded


async def _feed(trigger, *frames):
    for f in frames:
        await trigger.process_frame(f, FrameDirection.DOWNSTREAM)


async def test_the_read_retires_the_one_shot():
    trigger, _ = _harness()
    fired = []
    shot = trigger.arm(lambda: fired.append(1))
    await _feed(trigger, LLMFullResponseStartFrame(), LLMTextFrame("About that forecast"))
    assert fired == [1], "the completion produced text and the trigger was not retired"
    assert shot.fired.is_set()


async def test_the_response_start_alone_does_not_retire_it():
    """THE load-bearing assertion. pipecat pushes LLMFullResponseStartFrame BEFORE
    _process_context serializes the context, so retiring there neutralises the trigger
    before the model has seen it — every answer lost, and silently."""
    trigger, _ = _harness()
    fired = []
    trigger.arm(lambda: fired.append(1))
    await _feed(trigger, LLMFullResponseStartFrame())
    assert fired == [], (
        "retired on LLMFullResponseStartFrame — that frame is pushed before the request "
        "is built, so the model would be handed an already-neutralised trigger")


async def test_someone_elses_activity_does_not_retire_it():
    """The asWritten bug: gate.wait_until_delivered() keyed on _busy/_idle, which the
    USER's speech sets just as readily as our own turn."""
    trigger, _ = _harness()
    fired = []
    trigger.arm(lambda: fired.append(1))
    await _feed(trigger,
                UserStartedSpeakingFrame(), UserStoppedSpeakingFrame(),
                BotStartedSpeakingFrame(), LLMFullResponseEndFrame())
    assert fired == [], "conversational activity retired a trigger nothing had read"


async def test_it_fires_once_across_two_completions():
    """NoRepeatRecital: a second completion must not be able to read it too."""
    trigger, _ = _harness()
    fired = []
    trigger.arm(lambda: fired.append(1))
    await _feed(trigger, LLMTextFrame("first"), LLMTextFrame("second"))
    await _feed(trigger, LLMFullResponseStartFrame(), LLMTextFrame("a later turn"))
    assert fired == [1], f"fired {len(fired)} times — the bot would recite twice"


async def test_whitespace_is_not_an_answer():
    """A delta with no actual characters is not the model answering from the trigger."""
    trigger, _ = _harness()
    fired = []
    trigger.arm(lambda: fired.append(1))
    await _feed(trigger, LLMTextFrame(" "), LLMTextFrame("\n\n"))
    assert fired == [], "retired on a whitespace-only delta"
    await _feed(trigger, LLMTextFrame("Right"))
    assert fired == [1]


async def test_disarm_withdraws_it():
    """speak_followup disarms when it gives up on an attempt; a withdrawn one-shot must
    not fire later on an unrelated turn."""
    trigger, _ = _harness()
    fired = []
    shot = trigger.arm(lambda: fired.append(1))
    trigger.disarm(shot)
    await _feed(trigger, LLMTextFrame("an unrelated reply"))
    assert fired == [], "a disarmed one-shot still fired"


async def test_every_frame_is_forwarded():
    """It is a tap: it must never swallow anything."""
    trigger, forwarded = _harness()
    trigger.arm(lambda: None)
    frames = [LLMFullResponseStartFrame(), LLMTextFrame("hi"),
              LLMFullResponseEndFrame(), UserStartedSpeakingFrame()]
    await _feed(trigger, *frames)
    assert [type(f) for f in forwarded] == [type(f) for f in frames], (
        "the tap dropped or reordered frames")


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
