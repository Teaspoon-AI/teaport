#
# Unit test: MoodTag takes a leading [mood: x] tag off a reply, tells the client, and
# leaves every other reply byte-for-byte alone (the deltas, in order, on their frames).
#
import asyncio
import os
import sys

import pytest

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
    run.starts = [f.message["starts"] for f in out
                  if isinstance(f, OutputTransportMessageUrgentFrame)]
    return text, moods


def test_a_leading_tag_is_stripped_and_sent():
    assert run(["[mood: sad] I'm sorry to hear that."]) == ("I'm sorry to hear that.", ["sad"])


def test_the_mood_carries_the_first_words_of_its_reply():
    run(["[mood: sad] I'm so sorry to hear that, truly."])
    assert run.starts == [["i'm", "so", "sorry", "to"]]
    run(["[mood: sad]", " Oh", " no,", " that", " is", " hard."])     # words over several deltas
    assert run.starts == [["oh", "no", "that", "is"]]
    run(["[mood: sad] Oh dear."])                                      # a short reply: what it had
    assert run.starts == [["oh", "dear"]]
    run(["[mood: sad]"])
    assert run.starts == []                                            # nothing to match: none


def test_a_tag_split_across_deltas():
    assert run(["[", "mo", "od: ha", "ppy", "] Gla", "d to."]) == ("Glad to.", ["happy"])


def test_a_spaced_bracket_is_a_tag_and_is_not_spoken():
    assert run(["[ mood: angry ] It irritates me."]) == ("It irritates me.", ["angry"])
    assert run(["[ ", "mood", ": angry ", "] It irritates", " me."]) == ("It irritates me.", ["angry"])


def test_a_leading_emoji_is_a_tag_too():
    assert run(["😟 I'm so sorry to hear that."]) == ("I'm so sorry to hear that.", ["concerned"])
    text, moods = run(["😠", " That is", " infuriating, truly."])      # (the delta's own space stays)
    assert text.strip() == "That is infuriating, truly." and moods == ["angry"]
    assert run(["  😊\ufe0f Glad to help."]) == ("Glad to help.", ["happy"])
    text, moods = run(["🎉 Congratulations!"])       # an emoji that is no mood: left alone
    assert text == "🎉 Congratulations!" and moods == []
    text, moods = run(["Nice 😊 work."])             # not at the start: left alone
    assert text == "Nice 😊 work." and moods == []


def test_an_emoji_tags_modifiers_and_extras_are_not_spoken():
    assert run(["😊", "\ufe0f", " Glad to help you today."])[0].strip() == "Glad to help you today."
    assert run(["😊\U0001F3FD Glad to help."]) == ("Glad to help.", ["happy"])
    assert run(["😊😊 Hi there my friend."]) == ("Hi there my friend.", ["happy"])
    assert run(["😊 [mood: sad] Hi there my friend."]) == ("Hi there my friend.", ["happy"])
    text, moods = run(["😮\u200d💨 Phew, that was close."])   # a sequence: not ours, left whole
    assert text == "😮\u200d💨 Phew, that was close." and moods == []


def test_an_emoji_tag_waits_for_the_words_after_it():
    # (the decision waits for what follows, so a split modifier, joiner or second tag
    # cannot slip past it)
    assert run(["😮", "\ufe0f", "\u200d💨 Phew."]) == ("😮\ufe0f\u200d💨 Phew.", [])
    assert run(["😊", " ", "😠", " Hi there my friend."]) == ("Hi there my friend.", ["happy"])
    assert run(["😊", " [mo", "od: sad] Hi there my friend."]) == ("Hi there my friend.", ["happy"])
    assert run(["😊😮\u200d💨 hi"])[1] == []
    assert run(["😊 \ufe0f hi there my friend"]) == ("hi there my friend", ["happy"])
    # a reply of only the emoji still ends: nothing to say, so the recovery line is spoken
    text, moods = run(["😊"])
    assert moods == [] and text.strip() and "😊" not in text


def test_every_emoji_names_a_mood_the_avatar_knows():
    assert set(mood_tag.EMOJI.values()) == set(mood_tag.MOODS)
    assert all(m in mood_tag.MOODS for m in mood_tag.EMOJI.values())
    assert all(e in mood_tag.PARAGRAPH for e in mood_tag.EMOJI)


def test_case_spaces_and_a_tag_with_nothing_after_it():
    # (a reply of only a tag is apologised for, not left silent: see below; with no
    # words of its own there is nothing to match its mood to, so none is sent)
    assert run(["  [Mood : CALM ]"])[1] == []


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
    # a model spaces its brackets now and then ("[ mood: angry ]" was spoken aloud live)
    assert parse_leading_tag("[ mood: angry ] It irritates me") == ("tag", "angry", "It irritates me")
    assert parse_leading_tag("[ ") == ("more",) and parse_leading_tag("[ mo") == ("more",)
    assert parse_leading_tag("[ note: hi]")[0] == "plain"
    assert parse_leading_tag("[ " + " " * 500)[0] == "plain"          # not held for ever


def test_the_prompt_names_every_mood():
    assert all(m in mood_tag.PARAGRAPH for m in mood_tag.MOODS)


def test_a_reply_of_only_a_tag_is_not_left_silent():
    from teaport_brain.llm_text_guard import RECOVERY_TEXT
    assert run(["[mood: sad]"]) == (RECOVERY_TEXT, [])
    text, moods = run(["[mood: sad]", " \n"])
    assert text.strip() == RECOVERY_TEXT.strip() and moods == []
    assert run(["[mood: sad] ok"]) == ("ok", ["sad"])


def test_an_emoji_alone_before_a_tool_call_is_not_apologised_for():
    from pipecat.frames.frames import FunctionCallsStartedFrame
    tag = MoodTag()
    out = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        out.append(frame)
    tag.push_frame = push

    async def go():
        for f in [LLMFullResponseStartFrame(), LLMTextFrame(text="😟"),
                  FunctionCallsStartedFrame(function_calls=[]), LLMFullResponseEndFrame()]:
            await tag.process_frame(f, FrameDirection.DOWNSTREAM)
    asyncio.run(go())
    assert not any(isinstance(f, LLMTextFrame) for f in out)       # nothing spoken


def test_sympathy_in_an_untagged_reply_shows_concerned_not_angry():
    from teaport_brain.face_mood import LazyVader, score_to_mood
    assert score_to_mood(-0.8) == "concerned" and score_to_mood(0.8) == "happy"
    text = "I'm really sorry for your loss. If you need to talk or just a moment of quiet, I'm here."
    assert LazyVader().mood(text) == "concerned"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
