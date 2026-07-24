#
# Unit test: UserTranscriptEmitter's display-text contract.
#
# The invariant: the {"type":"transcript","role":"user"} messages sent to the relay
# carry the FULL utterance with surrounding whitespace stripped, while the frame
# forwarded downstream (to the user aggregator → LLM context, TranscriptLedger, and
# heard-grounding) stays byte-identical.
#
# Why strip: engine STT interims are a cumulative buffer of SentencePiece deltas, so each
# partial begins with a leading space (" What", " What is", ...). OpenClaw's Talk
# reducer treats any whitespace-leading transcript as a delta to APPEND rather than
# REPLACE, which stacks the growing partials into one bubble ("What What is What is
# the ..."). Stripping the DISPLAY copy keeps each partial a clean full-text
# replacement of the active turn. Mirrors CaptionTap's assistant-side .strip().
#
# Why display-only: the leading space is harmless in the LLM context but the internal
# consumers must see exactly what the STT emitted, so only the copy placed in the
# transport message is stripped — never frame.text itself.
#
# Run: python test_user_transcript.py   (or via pytest test_suite.py)

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipecat.frames.frames import (  # noqa: E402
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain.captions import UserTranscriptEmitter, VoiceActivity  # noqa: E402


class Harness:
    def __init__(self):
        self.em = UserTranscriptEmitter(VoiceActivity())
        self.sent = []       # (text, final) transcript messages — the DISPLAY copy
        self.forwarded = []  # frame.text of the frames passed downstream — internal path
        sent, forwarded = self.sent, self.forwarded

        async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
            m = getattr(frame, "message", None)
            if m and m.get("type") == "transcript":
                sent.append((m["text"], m["final"]))
            elif isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)):
                forwarded.append(frame.text)
        self.em.push_frame = fake_push

    async def interim(self, text):
        await self.em.process_frame(
            InterimTranscriptionFrame(text, "u", "0"), FrameDirection.DOWNSTREAM)

    async def final(self, text):
        await self.em.process_frame(
            TranscriptionFrame(text, "u", "0"), FrameDirection.DOWNSTREAM)


async def test_leading_space_stripped_forward_verbatim():
    # The live failure: cumulative STT partials each carry a leading space.
    h = Harness()
    await h.interim(" What")
    await h.interim(" What is")
    await h.final(" What is the time?")
    assert h.sent == [
        ("What", False), ("What is", False), ("What is the time?", True),
    ], h.sent
    assert all(t == t.strip() for t, _ in h.sent), h.sent
    # Internal path (LLM ctx / ledger / heard-grounding) must be byte-identical.
    assert h.forwarded == [" What", " What is", " What is the time?"], h.forwarded
    print("  PASS leading-space partials → display stripped, forwarded frame verbatim")


async def test_clean_text_and_inner_spaces_unchanged():
    # Already-clean text is untouched; interior spaces are never collapsed (only
    # the surrounding whitespace is stripped, not word gaps).
    h = Harness()
    await h.interim("hello there")
    await h.final("hello there world")
    assert h.sent == [("hello there", False), ("hello there world", True)], h.sent
    print("  PASS clean text and inner spaces preserved")


async def test_trailing_and_whitespace_only():
    # Trailing whitespace is stripped too; a whitespace-only frame collapses to ""
    # (the strip must not raise). The forwarded frame still carries the raw text.
    h = Harness()
    await h.interim("  hi  ")
    await h.final("   ")
    assert h.sent == [("hi", False), ("", True)], h.sent
    assert h.forwarded == ["  hi  ", "   "], h.forwarded
    print("  PASS trailing/whitespace-only handled, no over-collapse")


def test_user_transcript():
    async def main():
        await test_leading_space_stripped_forward_verbatim()
        await test_clean_text_and_inner_spaces_unchanged()
        await test_trailing_and_whitespace_only()
    asyncio.run(main())


if __name__ == "__main__":
    test_user_transcript()
    print("ALL PASS")
