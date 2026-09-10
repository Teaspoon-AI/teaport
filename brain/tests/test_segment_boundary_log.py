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
# Run: python test_segment_boundary_log.py   (or via the suite)
#

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger  # noqa: E402

import teaport_brain  # noqa: E402, F401
from teaport_brain.stt import TeaportSTTService  # noqa: E402

SR = 16000


class Recorder(TeaportSTTService):
    def __init__(self):
        super().__init__(url="ws://127.0.0.1:1/none")

    async def push_frame(self, frame, direction=None):
        pass

    async def stop_processing_metrics(self):
        pass

    def create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro)

    async def cancel_task(self, task, timeout=None):
        task.cancel()

    async def _send_commit(self, final: bool = True, why: str = "other"):
        import time
        self._commit_at = time.monotonic()
        self._commit_why = why


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
    assert s._commit_at is None and s._commit_why == ""


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
