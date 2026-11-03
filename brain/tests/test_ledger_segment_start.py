#
# Unit test (#288): the ledger dates each user line from the start of the speech
# segment its words came from, not from the first VAD start since the last line it
# logged. A segment that closes with no words (a cough, echo over the bot, every turn
# the wake gate sleeps through) logs no line, and its start must not date the next one.
# Synthetic frames only.
#
# Run: python test_ledger_segment_start.py
#

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import teaport_brain  # noqa: E402, F401  (before pipecat: sets HF_HUB_OFFLINE)

from pipecat.frames.frames import (  # noqa: E402
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.services.stt_service import STTService  # noqa: E402

from teaport_brain.endpointing import NO_STOP, SegmentDoneFrame, SegmentResetFrame  # noqa: E402
from teaport_brain.stt import FinalTranscriptionFrame  # noqa: E402
from teaport_brain.taken_turn import handed_back  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402


class _Stt(STTService):
    """Stands in for the STT: the processor the ledger counts VAD frames at."""

    def __init__(self):  # noqa: D107 -- no FrameProcessor setup: only isinstance matters
        pass

    async def run_stt(self, audio):  # pragma: no cover -- never run
        yield None


STT = _Stt()
OTHER = SimpleNamespace(name="transport-input")   # any processor that is not the STT


def start():
    return VADUserStartedSpeakingFrame(start_secs=0.2)


def stop():
    return VADUserStoppedSpeakingFrame(stop_secs=0.2)


def final(text, stop_n):
    return FinalTranscriptionFrame(text, "caller", "", None, finalized=True, stop_n=stop_n)


async def feed(seq, ledger=None):
    """(frame, t) or (frame, t, processor) items, as the observer sees them."""
    ledger = ledger or TranscriptLedger()
    for item in seq:
        f, t, proc = item if len(item) == 3 else (*item, STT)
        await ledger.on_process_frame(SimpleNamespace(frame=f, timestamp=int(t * 1e9),
                                                      processor=proc))
    return ledger


def spans(ledger):
    return [(round(u.t_start, 1), round(u.t_end, 1), u.text)
            for u in ledger.events if u.speaker == "user"]


async def test_the_asleep_stretch_does_not_date_the_first_line_after_the_wake():
    # People talk near the box from t=100 while it sleeps; the wake word at 200 is its
    # own wordless close (the gate keeps only what follows the phrase); the question at
    # 205-207 is the first line.
    L = await feed([
        (start(), 100.0), (stop(), 102.0), (SegmentDoneFrame(stop_n=1), 102.7),
        (start(), 150.0), (stop(), 151.0), (SegmentDoneFrame(stop_n=2), 151.6),
        (start(), 200.0), (stop(), 200.8), (SegmentDoneFrame(stop_n=3), 201.4),
        (start(), 205.0), (stop(), 207.0), (final("what's the weather", 4), 207.6),
    ])
    assert spans(L) == [(205.0, 207.6, "what's the weather")], spans(L)
    assert not L.events[-1].overlap


async def test_a_wordless_segment_mid_call_does_not_date_the_next_line():
    L = await feed([
        (start(), 10.0), (stop(), 10.4), (SegmentDoneFrame(stop_n=1), 11.0),   # a cough
        (start(), 20.0), (stop(), 22.0), (final("I have a question", 2), 22.6),
    ])
    assert spans(L) == [(20.0, 22.6, "I have a question")], spans(L)


async def test_speaking_again_inside_the_finish_keeps_each_segments_own_start():
    # The caller starts the next utterance before the engine has closed the last one
    # (its finish takes ~0.7 s): the late final is the first segment's, and the second
    # keeps its own start for its own close.
    L = await feed([
        (start(), 10.0), (stop(), 11.0),
        (start(), 11.3),
        (final("first part", 1), 11.7),
        (stop(), 13.0), (final("second part", 2), 13.6),
    ])
    assert spans(L) == [(10.0, 11.7, "first part"), (11.3, 13.6, "second part")], spans(L)


async def test_one_close_covering_two_segments_takes_the_first_ones_start():
    # The caller paused and went on before the commit went out: one final, stamped
    # with the second stop, closes both; the next line is not dated from either.
    L = await feed([
        (start(), 10.0), (stop(), 11.0),
        (start(), 11.5), (stop(), 13.0), (final("one two three four", 2), 13.6),
        (start(), 20.0), (stop(), 21.0), (final("next", 3), 21.6),
    ])
    assert spans(L) == [(10.0, 13.6, "one two three four"), (20.0, 21.6, "next")], spans(L)


async def test_the_engines_own_close_dates_the_rest_of_the_utterance_after_it():
    # The engine closes words mid-utterance (NO_STOP); the rest comes after that close.
    L = await feed([
        (start(), 10.0), (final("so the thing is", NO_STOP), 12.0),
        (stop(), 14.0), (final("it broke again", 1), 14.6),
    ])
    assert spans(L) == [(10.0, 12.0, "so the thing is"), (12.0, 14.6, "it broke again")], \
        spans(L)


async def test_the_engines_own_close_after_the_stop_dates_the_tail_after_it():
    # The engine cuts at a short pause on its own; the caller goes on briefly and stops,
    # and the brain's VAD stop is counted before the engine's close of the first part
    # lands. The stop's commit then returns the rest: dated after the first part.
    L = await feed([
        (start(), 10.0), (stop(), 12.0),
        (final("first part", NO_STOP), 12.5),
        (final("tail words", 1), 13.0),
    ])
    assert spans(L) == [(10.0, 12.5, "first part"), (12.5, 13.0, "tail words")], spans(L)


async def test_a_reset_segment_does_not_date_the_next_line():
    L = await feed([
        (start(), 10.0), (stop(), 11.0), (SegmentResetFrame(stop_n=1), 11.2),
        (start(), 20.0), (stop(), 21.0), (final("are you there", 2), 21.6),
    ])
    assert spans(L) == [(20.0, 21.6, "are you there")], spans(L)


async def test_handed_back_words_close_no_segment():
    # The ledger sees the final first, just below the STT; Wi-Fi setup gives taken words
    # back ahead of it further down, by when its segment is closed. The handed-back line
    # moves no open segment: the caller's next one keeps its start.
    f = final("and the second", 1)
    L = await feed([
        (start(), 10.0), (stop(), 11.0),
        (f, 11.6),
        (start(), 11.8),
        (handed_back(f, "the first"), 11.9), (f, 11.9),     # f again: the same frame
        (stop(), 13.0), (final("later", 2), 13.6),
    ])
    assert spans(L) == [(10.0, 11.6, "and the second"), (11.8, 11.9, "the first"),
                        (11.8, 13.6, "later")], spans(L)


async def test_a_backstop_commit_keeps_its_start_and_dates_the_rest_after_it():
    # The STT commits a segment whose VAD stop never came (the stranded backstop),
    # stamped with the stops counted so far: the segment's own number, not a later one.
    L = await feed([
        (start(), 2.0), (stop(), 3.0), (final("hi", 1), 3.5),
        (start(), 10.0), (final("backstop part", 1), 14.0),
        (stop(), 17.0), (final("rest after the cut", 2), 17.6),
    ])
    assert spans(L) == [(2.0, 3.5, "hi"), (10.0, 14.0, "backstop part"),
                        (14.0, 17.6, "rest after the cut")], spans(L)


async def test_late_words_of_a_closed_turn_do_not_take_the_next_segment():
    # Slice mode: an empty provisional closes the turn at once (SegmentDoneFrame), and
    # its words come later in the engine's own close (NO_STOP), after the caller has
    # started again. The next segment keeps its own start.
    L = await feed([
        (start(), 10.0), (stop(), 12.0), (SegmentDoneFrame(stop_n=1), 12.5),
        (start(), 13.0),
        (final("late words", NO_STOP), 13.4),
        (stop(), 15.0), (final("next words", 2), 15.6),
    ])
    late, nxt = spans(L)
    assert late[0] >= 12.0, f"never earlier than the speech before it: {late}"
    assert nxt[0] >= 13.0 and nxt[0] <= 13.4, nxt
    assert all(a[0] <= b[0] for a, b in zip(spans(L), spans(L)[1:])), spans(L)


async def test_a_second_close_for_the_same_words_never_dates_the_rest_early():
    # The barge-in judge lets the reply carry on over words it was cut by and pushes a
    # fresh SegmentDoneFrame with the final's stamp (barge_pause._close_of): a new frame,
    # so not deduped. Under the engine's own stamp (NO_STOP) it reads as a cut: the
    # rest is dated from it, late but never before the words already charted.
    L = await feed([
        (start(), 10.0), (final("uh huh", NO_STOP), 11.0),
        (stop(), 12.0), (SegmentDoneFrame(stop_n=NO_STOP), 12.3),
        (final("and the rest", 1), 12.8),
    ])
    (a0, a1, _), (b0, b1, _) = spans(L)
    assert (a0, a1) == (10.0, 11.0) and 11.0 <= b0 <= 12.3 and b1 == 12.8, spans(L)


async def test_a_wordless_engine_cut_dates_what_follows_from_itself():
    # The engine cuts on its own inside one long VAD segment (room chatter while asleep,
    # or the bot's echo) and the wake gate / echo guard empties the words: the line that
    # follows in the same segment starts at that cut, not at the chatter's start.
    L = await feed([
        (start(), 10.0), (SegmentDoneFrame(stop_n=NO_STOP), 40.0),
        (stop(), 45.0), (final("what's the weather", 1), 45.6),
    ])
    assert spans(L) == [(40.0, 45.6, "what's the weather")], spans(L)


async def test_vad_frames_count_where_the_stt_counts_them():
    # The observer sees each VAD frame at every processor it passes; only the STT's
    # sighting counts, so the ledger's stop count is the stamps' (one frame, one stop).
    a, b = stop(), start()
    L = await feed([
        (start(), 10.0, OTHER), (start(), 10.0),
        (a, 11.0, OTHER), (a, 11.0),
        (b, 20.0, OTHER), (b, 20.0),
        (final("hello", 1), 20.2),        # the first segment's final, landing late
    ])
    assert L._vad_stops == 1, L._vad_stops
    assert spans(L) == [(10.0, 20.2, "hello")], spans(L)
    assert [s[1] for s in L._starts] == [20.0], "the second segment stays open"


async def test_another_stts_unstamped_final_is_dated_from_its_start():
    L = await feed([
        (start(), 5.0, None), (stop(), 6.5, None),
        (TranscriptionFrame("hi there", "u", ""), 7.0, None),
        (start(), 9.0, None), (stop(), 9.6, None),
        (TranscriptionFrame("again", "u", ""), 10.0, None),
    ])
    assert spans(L) == [(5.0, 7.0, "hi there"), (9.0, 10.0, "again")], spans(L)


async def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"  ok {fn.__name__}")
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
