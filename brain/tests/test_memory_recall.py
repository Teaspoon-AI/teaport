#
# MemoryRecall §4A free-wins, headless (no gateway needed): patches
# recall_mod.memory_search with a controllable fake and asserts the three new
# behaviors —
#   1. single-flight: a fresher interim cancels the in-flight (shorter-query) search;
#   2. cancel-at-final: a search still running at the final transcript is cancelled;
#   3. freshness-rank (§4A.1): a stale short-prefix result completing LATE must not
#      clobber a fresher long-prefix result that already landed.
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from teaport_brain import memory_recall as recall_mod  # noqa: E402
from teaport_brain.memory_recall import MemoryRecall  # noqa: E402

from pipecat.frames.frames import (  # noqa: E402
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402


class StubContext:
    def __init__(self):
        self.messages = []

    def add_message(self, m):
        self.messages.append(m)


def make_fake(delays, record, default=0.01):
    """A fake memory_search: returns ['hit-len-N'] after a per-length delay, recording
    which query lengths started / completed / were cancelled."""
    async def fake(query, max_results=3):
        qlen = len(query)
        record.setdefault("started", []).append(qlen)
        try:
            await asyncio.sleep(delays.get(qlen, default))
        except asyncio.CancelledError:
            record.setdefault("cancelled", []).append(qlen)
            raise
        record.setdefault("completed", []).append(qlen)
        return [f"hit-len-{qlen}"]
    return fake


def _interim(text):
    return InterimTranscriptionFrame(text, "", "")


def _final(text):
    return TranscriptionFrame(text, "", "")


class _Sink(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


def _make_source(steps):
    """Source that, once the pipeline starts, runs a list of (delay, frame) steps."""
    class _Src(FrameProcessor):
        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            if isinstance(frame, StartFrame):
                self.create_task(self._go())

        async def _go(self):
            for delay, f in steps:
                await asyncio.sleep(delay)
                await self.push_frame(f)
    return _Src()


async def _drive(mr, steps, settle=0.6):
    task = PipelineTask(Pipeline([_make_source(steps), mr, _Sink()]))
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(settle)
    await task.cancel()
    try:
        await asyncio.wait_for(run, timeout=5)
    except Exception:
        pass


async def scenario_single_flight():
    ctx = StubContext()
    record = {}
    recall_mod.memory_search = make_fake({}, record)  # all fast
    mr = MemoryRecall(ctx)
    short = "what's the weather"                              # len 18
    long = "what's the weather in paris this weekend please"  # len 47 (+29 -> re-fires)
    await _drive(mr, [
        (0.05, _interim(short)),
        (0.0, _interim(long)),
        (0.1, _final(long)),
    ])
    injected = [m for m in ctx.messages if "hit-len" in m.get("content", "")]
    assert len(injected) == 1, f"expected 1 injection, got: {ctx.messages}"
    assert "hit-len-47" in injected[0]["content"], injected[0]["content"]
    assert 47 in record.get("completed", []), record
    assert 18 not in record.get("completed", []), f"len-18 should have been superseded: {record}"
    print("OK single-flight: freshest (len-47) injected; len-18 search superseded")


async def scenario_cancel_at_final():
    ctx = StubContext()
    record = {}
    recall_mod.memory_search = make_fake({}, record, default=0.5)  # slow: still running
    mr = MemoryRecall(ctx)
    q = "tell me a story about cats"  # len 26
    await _drive(mr, [
        (0.05, _interim(q)),
        (0.05, _final(q)),   # final arrives long before the 0.5s search completes
    ])
    assert not any("hit-len" in m.get("content", "") for m in ctx.messages), ctx.messages
    assert 26 in record.get("cancelled", []), record
    assert 26 not in record.get("completed", []), record
    assert mr._inflight is None
    print("OK cancel-at-final: in-flight search cancelled, nothing injected")


async def scenario_freshness_guard():
    ctx = StubContext()
    record = {}
    # Short query completes LATE; long query completes EARLY. Bypass single-flight
    # (null _inflight) so BOTH run uncancelled — exactly the out-of-order completion
    # that imperfect cancellation produces in production with real executor threads.
    recall_mod.memory_search = make_fake({13: 0.3, 40: 0.02}, record)
    mr = MemoryRecall(ctx)
    mr._spawn("x" * 13)
    mr._inflight = None          # short search is no longer the single-flight handle
    mr._spawn("y" * 40)
    await asyncio.sleep(0.5)      # long lands at ~0.02, stale short lands at ~0.3
    assert set(record.get("completed", [])) == {13, 40}, f"both should complete: {record}"
    assert mr._latest == ["hit-len-40"], f"stale short clobbered fresh long: {mr._latest}"
    assert mr._latest_len == 40, mr._latest_len
    print("OK freshness-rank: late stale (len-13) did not clobber fresh (len-40)")


async def main():
    orig = recall_mod.memory_search
    try:
        await scenario_single_flight()
        await scenario_cancel_at_final()
        await scenario_freshness_guard()
    finally:
        recall_mod.memory_search = orig
    print("\nALL OK")


if __name__ == "__main__":
    asyncio.run(main())
