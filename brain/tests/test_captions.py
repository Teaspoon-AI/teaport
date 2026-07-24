#
# Unit test: CaptionTap's utterance-scoped caption contract.
#
# The invariant (paid for three times over): every spoken utterance is charted in
# EXACTLY one bubble, in playout order — fillers included — and a barged utterance
# emits NO final (the client commits its in-progress bubble; a late final would
# render as a duplicate after the user's message). Utterances are segmented by
# the progress frame's context_id (one audio context per LLM turn / per filler),
# NOT by LLM-response boundaries, which do not align with playout when tool-call
# fillers and chained turns play as one continuous audio segment.
#
# Caption text rides pipecat 1.5.0's AggregatedTextProgressFrame: per-word
# frames whose accumulated_text is SLICED from the source sentence (exact
# spacing/punctuation — no rejoin heuristic), restarting at each sentence
# segment within a turn's shared context. The harness feeds token-boundary
# prefixes of the source string, which is exactly what the tracker's
# user-facing cursor yields in prod.
#
# The USER-HOLD rules exist because the Talk UI commits the active assistant
# bubble at the user's FIRST interim — 1-3 words before the barge machinery
# decides anything. Partials emitted in that window reopen a second bubble with
# near-identical text (the "seen twice" bug, observed live 2026-07-05 with a
# tool card then rendering into the stray bubble).
#
# Run: python test_captions.py   (or via pytest)
#

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipecat.frames.frames import (  # noqa: E402
    AggregatedTextProgressFrame,
    BotStoppedSpeakingFrame,
    LLMFullResponseEndFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.utils.text.base_text_aggregator import AggregationType  # noqa: E402

from teaport_brain.captions import CaptionTap, VoiceActivity  # noqa: E402


def P(ctx, seg, text, acc, pts=None):
    f = AggregatedTextProgressFrame(
        segment_id=seg, context_id=ctx, text=text,
        aggregated_by=AggregationType.SENTENCE,
        accumulated_text=acc, remaining_text=text[len(acc):],
    )
    if pts is not None:
        f.pts = pts
    return f


def END(pts):
    f = LLMFullResponseEndFrame()
    f.pts = pts
    return f


def prefixes(text):
    """Token-boundary prefixes of `text`, sliced from the source string — the
    accumulated_text sequence pipecat's user-facing cursor produces in prod
    (spacing and punctuation preserved verbatim, one prefix per word)."""
    return [text[: m.end()] for m in re.finditer(r"\S+", text)]


class Harness:
    _seg_counter = 1000  # unique segment ids across the run, like prod frame ids

    def __init__(self):
        self.activity = VoiceActivity()
        self.tap = CaptionTap(self.activity)
        self.sent = []  # (text, final) transcript messages, in emit order
        sent = self.sent

        async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
            m = getattr(frame, "message", None)
            if m and m.get("type") == "transcript":
                sent.append((m["text"], m["final"]))
        self.tap.push_frame = fake_push

    def new_seg(self):
        Harness._seg_counter += 1
        return Harness._seg_counter

    async def feed(self, frame):
        await self.tap.process_frame(frame, FrameDirection.DOWNSTREAM)

    async def sentence(self, text, ctx, seg=None, pts=None, stop_after=None):
        """Feed one sentence segment word-by-word; stop_after limits how many
        words play (a mid-segment cut, e.g. before a barge)."""
        seg = seg if seg is not None else self.new_seg()
        for acc in prefixes(text)[:stop_after]:
            await self.feed(P(ctx, seg, text, acc, pts))
        return seg

    async def barge(self):
        # pipecat delivers interruptions out-of-band via _start_interruption; the
        # override is the real entry point, so drive it directly.
        await self.tap._start_interruption()

    def user_talks(self):
        self.activity.stamp()  # what UserTranscriptEmitter does per interim/final

    def user_quiet(self):
        self.activity.user_ts -= 60.0  # age the stamp past the hold window

    def finals(self):
        return [t for t, f in self.sent if f]

    def partials(self):
        return [t for t, f in self.sent if not f]


async def test_seamless_multi_utterance_segment():
    # Filler then reply playing as ONE audio segment (no BotStopped between):
    # each utterance gets its own final, in playout order — never a concatenated
    # bubble ("Opening that page. He won…") next to a reply-only final.
    h = Harness()
    await h.sentence("Opening that page.", "ctx-filler")
    await h.sentence("He won six titles.", "ctx-reply")
    await h.feed(BotStoppedSpeakingFrame())
    assert h.finals() == ["Opening that page.", "He won six titles."], h.sent
    assert not any("page. He" in p for p in h.partials()), h.partials()
    print("  PASS seamless filler→reply → two ordered finals, no concatenation")


async def test_multi_segment_stitching():
    # A turn's sentences share ONE audio context but accumulated_text restarts
    # per segment: the bubble must stitch completed sentences + the live one,
    # and the final carries the whole utterance exactly once.
    h = Harness()
    await h.sentence("Hello!", "ctx-turn")
    await h.sentence("How can I help you today?", "ctx-turn")
    await h.feed(BotStoppedSpeakingFrame())
    assert h.finals() == ["Hello! How can I help you today?"], h.sent
    assert "Hello! How" in h.partials(), h.partials()  # stitch, not restart
    assert h.partials()[0] == "Hello!", h.partials()
    print("  PASS multi-segment turn → stitched partials, one exact final")


async def test_barge_in_no_final_no_stragglers():
    # Barge mid-utterance: client commits the bubble — no final from us, and
    # straggler progress frames already released by the transport clock are
    # dropped.
    h = Harness()
    text = "Sure thing—just send me the link."
    seg = await h.sentence(text, "ctx-a", stop_after=3)
    await h.barge()
    # Stragglers of the dead context (clock task raced the interruption).
    for acc in prefixes(text)[3:5]:
        await h.feed(P("ctx-a", seg, text, acc))
    await h.feed(BotStoppedSpeakingFrame())
    assert h.finals() == [], h.sent
    assert h.partials()[-1] == "Sure thing—just send", h.partials()
    h.user_quiet()
    await h.sentence("What specifically?", "ctx-b")
    await h.feed(BotStoppedSpeakingFrame())
    assert h.finals() == ["What specifically?"], h.sent
    print("  PASS barge → no final, stragglers dropped, next utterance clean")


async def test_user_hold_no_reopened_bubble():
    # The "seen twice" bug: the user starts talking (their interim commits the
    # active bubble client-side) 1-3 words BEFORE the barge fires. Words played
    # in that window must NOT emit partials (they'd reopen a second bubble that
    # a following tool card then renders into).
    h = Harness()
    text = "Top stories: NBC News highlights breaking coverage."
    seg = await h.sentence(text, "ctx-news", stop_after=4)
    h.user_talks()                        # first interim → client commits bubble
    for acc in prefixes(text)[4:6]:       # overlap window: held
        await h.feed(P("ctx-news", seg, text, acc))
    await h.barge()                       # turn machinery catches up
    await h.feed(BotStoppedSpeakingFrame())
    assert h.finals() == [], h.sent
    assert h.partials()[-1] == "Top stories: NBC News", h.partials()
    print("  PASS user-hold → no partials after the user's interim, no reopen")


async def test_user_hold_survivor_resumes_full_text():
    # Same overlap but NO barge (1-word garble, bot keeps talking): partials
    # resume once the user is quiet and the next one carries the FULL text.
    h = Harness()
    text = "It was the best of times, it was the worst of times."
    seg = await h.sentence(text, "ctx-d", stop_after=4)
    h.user_talks()
    for acc in prefixes(text)[4:8]:       # held
        await h.feed(P("ctx-d", seg, text, acc))
    h.user_quiet()
    for acc in prefixes(text)[8:]:
        await h.feed(P("ctx-d", seg, text, acc))
    await h.feed(BotStoppedSpeakingFrame())
    assert h.finals() == [text], h.sent
    assert "It was the best of times, it was" in h.partials()[-1], h.partials()
    print("  PASS user-hold survivor → resumed partial carries full text, one final")


async def test_reply_end_finalizes_at_last_word():
    # The playout-paced LLMFullResponseEndFrame (pts = last word) finalizes
    # immediately — the final must beat the user's next interim, not wait ~0.4s
    # for BotStopped silence detection (the duplicated-greeting race).
    h = Harness()
    await h.sentence("Hey there.", "ctx-greet", pts=1_000)
    await h.feed(END(pts=1_000))
    assert h.finals() == ["Hey there."], h.sent
    h.user_talks()                        # user replies AFTER the final went out
    await h.feed(BotStoppedSpeakingFrame())  # later silence detection: no double
    assert h.finals() == ["Hey there."], h.sent
    print("  PASS reply-end pts → final at last word, BotStopped no-ops after")


async def test_stale_end_ignored():
    # A wordless (pure tool-call) turn's End carries a stale pts predating the
    # currently-playing utterance's words: it must not cut that utterance short.
    h = Harness()
    text = "Looking up Austin weather now."
    seg = await h.sentence(text, "ctx-filler", pts=5_000, stop_after=3)
    await h.feed(END(pts=1_000))          # stale: belongs to an earlier turn
    assert h.finals() == [], h.sent
    for acc in prefixes(text)[3:]:
        await h.feed(P("ctx-filler", seg, text, acc, pts=6_000))
    await h.feed(BotStoppedSpeakingFrame())
    assert h.finals() == [text], h.sent
    print("  PASS stale End (pts < first word) ignored")


async def test_final_skipped_when_client_committed():
    # Utterance fully shown, user interim commits the bubble, THEN the boundary
    # fires: an identical final would render after the user's message as a
    # duplicate bubble — skip it.
    h = Harness()
    await h.sentence("Hey there.", "ctx-greet", pts=1_000)
    h.user_talks()                        # interim beat the boundary this time
    await h.feed(END(pts=1_000))
    assert h.finals() == [], h.sent
    assert h.partials()[-1] == "Hey there.", h.partials()
    print("  PASS final skipped when the client already committed identical text")


async def test_exact_source_spacing():
    # accumulated_text is a source slice: apostrophe-initial words keep their
    # space ("Whether 'tis", the #9 regression) and in-word apostrophes stay
    # attached ("It's") — spacing is exact by construction, not by heuristic.
    h = Harness()
    text = "Whether 'tis nobler in the mind."
    await h.sentence(text, "ctx-h")
    await h.feed(BotStoppedSpeakingFrame())
    assert h.finals() == [text], h.sent
    assert "Whether 'tis" in h.partials()[1], h.partials()
    h2 = Harness()
    await h2.sentence("It's here.", "ctx-p")
    await h2.feed(BotStoppedSpeakingFrame())
    assert h2.finals() == ["It's here."], h2.sent
    print("  PASS exact source spacing: \"Whether 'tis\" and \"It's\" render verbatim")


async def test_force_completed_tail_in_final():
    # The engine dropped word timestamps mid-segment (aligner hiccup): the tail
    # has no progress frames but its audio played. A finalize only fires at a
    # playout boundary, so the final uses the segment's FULL source text.
    h = Harness()
    text = "The forecast says rain tomorrow."
    await h.sentence(text, "ctx-f", stop_after=2)  # timestamps stop mid-segment
    await h.feed(BotStoppedSpeakingFrame())        # real silence: all audio played
    assert h.finals() == [text], h.sent
    print("  PASS force-completed tail → final carries the full source text")


async def test_clean_single_utterance():
    h = Harness()
    await h.sentence("It is forty two degrees.", "ctx-1")
    await h.feed(BotStoppedSpeakingFrame())
    assert h.finals() == ["It is forty two degrees."], h.sent
    assert h.partials()[0] == "It", h.partials()
    print("  PASS clean reply → partials grow, one final")


async def test_no_empty_or_double_finals():
    h = Harness()
    await h.sentence("Hello.", "ctx-1")
    await h.feed(BotStoppedSpeakingFrame())
    await h.feed(BotStoppedSpeakingFrame())  # extra BotStopped (drain) no-ops
    await h.sentence("Again.", "ctx-2")
    await h.sentence("More.", "ctx-3")       # switch finalizes ctx-2 exactly once
    await h.feed(BotStoppedSpeakingFrame())
    assert h.finals() == ["Hello.", "Again.", "More."], h.sent
    print("  PASS boundary spam → exactly one final per utterance, none empty")


def test_captions():
    async def main():
        await test_seamless_multi_utterance_segment()
        await test_multi_segment_stitching()
        await test_barge_in_no_final_no_stragglers()
        await test_user_hold_no_reopened_bubble()
        await test_user_hold_survivor_resumes_full_text()
        await test_reply_end_finalizes_at_last_word()
        await test_stale_end_ignored()
        await test_final_skipped_when_client_committed()
        await test_exact_source_spacing()
        await test_force_completed_tail_in_final()
        await test_clean_single_utterance()
        await test_no_empty_or_double_finals()
    asyncio.run(main())


if __name__ == "__main__":
    test_captions()
    print("ALL PASS")
