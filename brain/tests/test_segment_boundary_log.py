#
# Unit test: one line per segment at the commit/final boundary.
#
# Live 2026-09-10 05:57:44 the caller spoke for 1.2 s -- Silero at conf 0.89 against a
# 0.15 gate, volume 0.75, a full QUIET->STARTING->SPEAKING->STOPPING->QUIET cycle -- and
# the pipeline produced no transcript at all. They waited ~2.5 s, tried again, and the
# retry transcribed in 197 ms. From the caller's side that is four and a half seconds of
# talking at a bot that keeps going.
#
# Nothing in the log could say whether the BRAIN failed to commit that audio or the
# ENGINE returned nothing for it, so the fault could not be placed -- and the engine is
# owned elsewhere, so placing it is the whole point. This line is the seam: everything
# left of it is ours, everything right of it is the engine's.
#
# THE EMPTY RATE (#29) is the same misses counted across segments. Live 2026-09-10
# 06:17, 6 of 21 segments came back empty in ones and twos -- 29% of the call -- and the
# consecutive-run warning (test_stt_final.py) never fired, because a healthy segment
# reset it every time. The rate warns once per episode and logs once when it is over;
# the run stays the fast path for a dead microphone.
#
# Run: python test_segment_boundary_log.py   (or via the suite)
#

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger  # noqa: E402

import teaport_brain  # noqa: E402, F401
from stt_harness import WireRecorder  # noqa: E402

from teaport_brain.stt import TeaportSTTService  # noqa: E402

SR = 16000


class Recorder(TeaportSTTService):
    def __init__(self):
        super().__init__(url="ws://127.0.0.1:1/none")
        # The real _send_commit, recorded instead of sent: the segment line reads the
        # commit's time and reason off the queue the send fills.
        self._websocket = WireRecorder()

    async def push_frame(self, frame, direction=None):
        pass

    async def stop_processing_metrics(self):
        pass

    def create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro)

    async def cancel_task(self, task, timeout=None):
        task.cancel()


async def _segment(deltas, final_text, seconds=1.0, why="vad-stop"):
    """Drive one segment; return (message, level) of the segment line."""
    sink = []
    logger.remove()
    logger.add(lambda m: sink.append((m.record["message"], m.record["level"].name)),
               level="DEBUG")
    try:
        s = Recorder()
        s._seg_bytes = int(seconds * SR * 2)
        for d in deltas:
            await s._handle_message({"type": "transcription.delta", "delta": d})
        await s._send_commit(why=why)
        await s._handle_message({"type": "transcription.done", "text": final_text})
        seg = [x for x in sink if "segment" in x[0]]
        return seg[-1] if seg else ("", "")
    finally:
        logger.remove()


async def test_a_healthy_segment_is_debug_and_reports_the_shape():
    msg, level = await _segment(["Okay, ", "stop there."], "Okay, stop there.")
    assert level == "DEBUG", f"a healthy segment must not warn: {msg}"
    assert "1.00s audio" in msg, msg
    assert "2 interims" in msg, msg
    assert "commit=vad-stop" in msg, msg
    assert "17 chars" in msg, msg


async def test_interims_then_an_empty_final_points_at_the_engine():
    # The shape that matters: the engine had words a moment ago and returned none.
    msg, level = await _segment(["Okay, ", "stop"], "")
    assert level == "WARNING", msg
    assert "EMPTY" in msg and "streamed interims then returned no text" in msg, msg


async def test_audio_with_no_words_at_all_is_called_out_separately():
    msg, level = await _segment([], "", seconds=1.2)
    assert level == "WARNING", msg
    assert "audio in, no words out" in msg, msg
    assert "0 interims" in msg, msg


async def test_the_commit_trigger_is_named():
    # vad-stop vs backstop is the difference between the normal path and the fallback,
    # and it decides where to look next.
    msg, _ = await _segment(["hi "], "hi", why="backstop")
    assert "commit=backstop" in msg, msg


async def test_a_done_we_never_asked_for_says_so():
    # The engine can close a segment on its own; that is not our commit and the line
    # must not imply we sent one.
    sink = []
    logger.remove()
    logger.add(lambda m: sink.append(m.record["message"]), level="DEBUG")
    try:
        s = Recorder()
        s._seg_bytes = SR * 2
        await s._handle_message({"type": "transcription.delta", "delta": "hello"})
        await s._handle_message({"type": "transcription.done", "text": "hello"})
        msg = [x for x in sink if "segment" in x][-1]
    finally:
        logger.remove()
    assert "no-commit" in msg, msg


async def test_the_duration_is_real_before_the_sample_rate_is_negotiated():
    # sample_rate is 0 until the StartFrame; dividing by 1 turned a one-second segment
    # into "16000.00s audio", a line worse than none because it reads as a fault itself.
    msg, _ = await _segment(["hi "], "hi", seconds=2.0)
    assert "2.00s audio" in msg, msg


async def test_counters_reset_between_segments():
    s = Recorder()
    s._seg_bytes = SR * 2
    await s._handle_message({"type": "transcription.delta", "delta": "one"})
    await s._handle_message({"type": "transcription.done", "text": "one"})
    assert s._seg_bytes == 0 and s._seg_interims == 0, "a segment must not bleed into the next"
    assert not s._commits, "the answered commit must leave the queue with its done"


# ---- THE EMPTY RATE (#29) ----------------------------------------------------

RATE = "segments the caller was heard in came back with no words"
RECOVERED = "segments are coming back with words again"
RUN = "finals in a row with no words"
RUN_OVER = "hearing speech again"

# The 2026-09-10 06:17 call's shape: 6 of 21 segments EMPTY, in ones and twos. Its
# opening five are the journal's as #29 quotes it (EMPTY, words, EMPTY, words, EMPTY,
# every miss with 0 interims); the rest is not quoted, and places the other three misses
# in ones and twos as the issue describes them.
CALL_0910 = "EWEWE" "WWWEEWWWWWEWWWWW"


async def _drive(pattern, s=None, why="bot-speaking"):
    """Drive `s` (a fresh service by default) through `pattern`, one segment a letter:
    W  words back, after interims, on a VAD-stop commit (`why`);
    E  1 s of audio in, no interims, EMPTY back, on a VAD-stop commit: VAD said
       speech, STT said nothing;
    S  interims streamed, then EMPTY back, on a VAD-stop commit;
    T  1 s in, EMPTY back on a `tail` commit: the appliance's ordinary empty after the
       engine's own close of the utterance, evidence of nothing;
    D  EMPTY back with no commit (the engine's own close; a dead mic in a quiet room).
    Returns the service and the lines that are not the per-segment one, in order."""
    sink = []
    logger.remove()
    logger.add(lambda m: sink.append((m.record["level"].name, m.record["message"])),
               level="INFO")
    try:
        if s is None:
            s = Recorder()
        for c in pattern:
            s._seg_bytes = SR * 2
            if c in "WS":
                await s._handle_message({"type": "transcription.delta", "delta": "hi there"})
            if c != "D":
                await s._send_commit(why="tail" if c == "T" else why)
            await s._handle_message({"type": "transcription.done",
                                     "text": "hi there" if c == "W" else ""})
        return s, [(lvl, m) for lvl, m in sink if " segment " not in m]
    finally:
        logger.remove()


def _lines(lines, text):
    return [m for _, m in lines if text in m]


async def test_the_intermittent_call_that_said_nothing_now_warns_once():
    assert len(CALL_0910) == 21 and CALL_0910.count("E") == 6
    _, lines = await _drive(CALL_0910)
    warned = _lines(lines, RATE)
    assert len(warned) == 1, lines
    # At its fourth segment, the smallest window judged: the warning is not waiting for
    # a window to fill.
    assert "2 of the last 4" in warned[0], warned[0]
    assert "2 VAD said speech, STT said nothing" in warned[0], warned[0]
    assert "0 interims streamed, final EMPTY" in warned[0], warned[0]
    assert [lvl for lvl, m in lines if RATE in m] == ["WARNING"]
    # The run it slipped past is still blind to it -- which is the point.
    assert not _lines(lines, RUN), lines


async def test_the_two_shapes_are_kept_apart():
    # They point at different owners: the engine that had words and returned none, and
    # the engine that produced nothing for audio the VAD called speech.
    _, lines = await _drive("SWEW")
    warned = _lines(lines, RATE)
    assert len(warned) == 1, lines
    assert "1 VAD said speech, STT said nothing (audio in, no interims, no final)" \
        in warned[0], warned[0]
    assert "1 interims streamed, final EMPTY" in warned[0], warned[0]


async def test_a_quiet_call_with_one_empty_does_not_warn():
    # One early miss is ordinary -- a cough, a door -- and nothing under four segments
    # is judged at all.
    for pattern in ("E", "EW", "EWW", "EWWW", "EWWWWWWWWWWWWW", "WEWWW", "WWWWE"):
        _, lines = await _drive(pattern)
        assert not _lines(lines, RATE), (pattern, lines)


async def test_ordinary_empties_are_not_counted():
    # The appliance's engine closes an utterance before the VAD stop, and the tail its
    # stop commits comes back EMPTY: half the finals of a healthy call. Nor is the
    # engine's own close of room noise a miss. Counted, either would warn on every call.
    for pattern in ("WT" * 8, "WD" * 8, "WTD" * 6):
        _, lines = await _drive(pattern)
        assert not _lines(lines, RATE), (pattern, lines)


async def test_a_dead_microphone_still_trips_the_run_and_only_the_run():
    # VAD stops on room noise into a muted mic: every segment a miss. The run reports
    # it at its fifth, as before, and the rate does not say it a segment sooner as
    # something else -- a window of nothing but empties is the run's.
    _, lines = await _drive("E" * 8)
    assert len(_lines(lines, RUN)) == 1, lines
    assert not _lines(lines, RATE), lines
    # A dead mic in a quiet room never reaches the rate at all: the VAD heard nothing.
    _, lines = await _drive("D" * 8)
    assert len(_lines(lines, RUN)) == 1 and not _lines(lines, RATE), lines


async def test_a_run_once_over_does_not_warn_again_as_a_rate():
    # Left in the window, the run's empties would make the next healthy segments read
    # as "5 of the last 7": the run's own line closes its episode.
    _, lines = await _drive("E" * 5 + "W" * 6)
    assert len(_lines(lines, RUN)) == 1 and len(_lines(lines, RUN_OVER)) == 1, lines
    assert not _lines(lines, RATE), lines
    # A run too short for the run check is the rate's to catch.
    _, lines = await _drive("EEEEW")
    assert not _lines(lines, RUN), lines
    assert len(_lines(lines, RATE)) == 1 and "4 of the last 5" in _lines(lines, RATE)[0]


async def test_recovery_logs_once_and_the_next_episode_warns_again():
    s, lines = await _drive("EWEWE" + "W" * 20)
    assert len(_lines(lines, RATE)) == 1, lines
    recovered = _lines(lines, RECOVERED)
    assert len(recovered) == 1, lines
    # Not at the first healthy segment: when the episode's empties have aged out to
    # the clear line (2 of a full window), with the episode's own count.
    assert "2 of the last 12 empty, 3 of 13 over the episode" in recovered[0], recovered[0]
    assert [lvl for lvl, m in lines if RECOVERED in m] == ["INFO"]
    # Latched no longer: a later episode on the same service is its own warning.
    _, more = await _drive("EWEWEWEWE", s)
    assert len(_lines(more, RATE)) == 1 and "5 of the last 12" in _lines(more, RATE)[0], more


async def test_a_rate_hovering_at_the_line_is_one_episode():
    # 40% steady, every fifth segment two misses: never back to 20%, so never a second
    # warning however long the call goes.
    _, lines = await _drive("EEWWW" * 10)
    assert len(_lines(lines, RATE)) == 1, lines
    assert not _lines(lines, RECOVERED), lines


async def test_an_asleep_room_is_not_counted():
    # #116: a room mic waiting for its wake word. The rate counts from the wake, as the
    # run does.
    class Gate:
        asleep = True

        async def final(self, text, push):
            return text

    s = Recorder()
    s.wake_gate = Gate()
    _, lines = await _drive("EEEEEEEE", s)
    assert not _lines(lines, RATE) and not _lines(lines, RUN), lines
    s.wake_gate.asleep = False
    _, lines = await _drive("WEWE", s)
    assert len(_lines(lines, RATE)) == 1 and "2 of the last 4" in _lines(lines, RATE)[0], lines


def main():
    async def run_all():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and asyncio.iscoroutinefunction(fn):
                await fn()
                print(f"  ok {name}")
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
    print("ALL PASS")
