#
# Unit test: MoodTag takes a leading [mood: x] tag off a reply, tells the client, and
# leaves every other reply byte-for-byte alone (the deltas, in order, on their frames).
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain import mood_tag  # noqa: E402
from teaport_brain.mood_tag import MoodTag, parse_leading_tag  # noqa: E402


def run(deltas, end=True):
    """Feed one completion through MoodTag: (text that went on, mood messages sent)."""
    tag = MoodTag()
    out = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        out.append(frame)
    tag.push_frame = push

    async def go():
        for f in [LLMFullResponseStartFrame(), *[LLMTextFrame(text=d) for d in deltas],
                  *([LLMFullResponseEndFrame()] if end else [])]:
            await tag.process_frame(f, FrameDirection.DOWNSTREAM)
    asyncio.run(go())
    text = "".join(f.text for f in out if isinstance(f, LLMTextFrame))
    moods = [f.message["mood"] for f in out if isinstance(f, OutputTransportMessageUrgentFrame)]
    return text, moods


def test_a_leading_tag_is_stripped_and_sent():
    assert run(["[mood: sad] I'm sorry to hear that."]) == ("I'm sorry to hear that.", ["sad"])


def test_a_tag_split_across_deltas():
    assert run(["[", "mo", "od: ha", "ppy", "] Gla", "d to."]) == ("Glad to.", ["happy"])


def test_case_spaces_and_a_tag_with_nothing_after_it():
    assert run(["  [Mood : CALM ]"]) == ("", ["calm"])


def test_no_tag_passes_through_untouched():
    for deltas in (["Sure, ", "done."], ["[1] first, ", "then"], ["[mo", "ve it]"], ["", " hi"]):
        text, moods = run(deltas)
        assert text == "".join(deltas) and moods == []


def test_a_tag_not_at_the_start_is_left_alone():
    text, moods = run(["Well. [mood: sad] oh."])
    assert text == "Well. [mood: sad] oh." and moods == []


def test_an_unknown_mood_is_stripped_and_sent_nowhere():
    assert run(["[mood: grumpy] Hmph."]) == ("Hmph.", [])


def test_an_unclosed_opener_is_released_in_order():
    long = "[mood: " + "x" * 60
    assert run([long, " tail"]) == (long + " tail", [])
    assert run(["[mood: sa"]) == ("[mood: sa", [])      # the End releases what is held


def test_the_next_completion_may_carry_its_own_tag():
    tag = MoodTag()
    out = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        out.append(frame)
    tag.push_frame = push

    async def go():
        for d in (["[mood: happy] a"], ["b"], ["[mood: sad] c"]):
            await tag.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
            await tag.process_frame(LLMTextFrame(text=d[0]), FrameDirection.DOWNSTREAM)
            await tag.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    asyncio.run(go())
    assert [f.message["mood"] for f in out if isinstance(f, OutputTransportMessageUrgentFrame)] \
        == ["happy", "sad"]
    assert "".join(f.text for f in out if isinstance(f, LLMTextFrame)) == "abc"


def test_a_barge_in_drops_the_held_opener():
    tag = MoodTag()
    out = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        out.append(frame)
    tag.push_frame = push

    async def go():
        await tag.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        await tag.process_frame(LLMTextFrame(text="[mo"), FrameDirection.DOWNSTREAM)
        await tag.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await tag.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    asyncio.run(go())
    assert not any(isinstance(f, LLMTextFrame) for f in out)


def test_the_parser():
    assert parse_leading_tag("") == ("more",)
    assert parse_leading_tag("[mo") == ("more",)
    assert parse_leading_tag("hi")[0] == "plain"
    assert parse_leading_tag("[mood: sad] x") == ("tag", "sad", "x")


def test_the_prompt_names_every_mood():
    assert all(m in mood_tag.PARAGRAPH for m in mood_tag.MOODS)
