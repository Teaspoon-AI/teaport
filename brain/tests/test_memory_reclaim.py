#
# The in-session memory reclaim (memory_hygiene.MemoryReclaim) waits for a quiet moment.
#
# gc.collect() and malloc_trim(0) stop the event loop wherever they run: the collect
# holds the GIL, the trim holds glibc's arena locks. Run at every reply's end, as it
# was, that put a ~220 ms freeze on the box right where the user starts to speak, and a
# barge-in landing in it waited that long to stop the reply (2026-10-15). A reply now
# only makes the reclaim due. It runs once the conversation has been clear for the
# quiet time, at most once per interval, off the loop. Also pinned: the startup objects
# gc.freeze() takes out of every later collect, without freezing anything a session
# makes.
#
# Run: python test_memory_reclaim.py   (or via pytest test_suite.py)
#
import asyncio
import os
import subprocess
import sys
import threading
import time

import appliance

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import BotStoppedSpeakingFrame, EndFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402

from teaport_brain import memory_hygiene  # noqa: E402
from teaport_brain.memory_hygiene import MemoryReclaim, release_heap  # noqa: E402

QUIET = 0.6
EVERY = 1.6
POLL = 0.02


class _Gate:
    """FollowupGate's two readings MemoryReclaim takes."""

    def __init__(self):
        self.clear = True
        self.last_active = time.monotonic()

    def is_clear(self) -> bool:
        return self.clear

    def speak(self):
        self.clear = False
        self.last_active = time.monotonic()

    def fall_quiet(self):
        self.clear = True
        self.last_active = time.monotonic()


class _Harness:
    def __init__(self):
        self.gate = _Gate()
        self.reclaims = []   # (what, ran on the loop's thread)
        self.loop_thread = None

    async def __aenter__(self):
        self.loop_thread = threading.get_ident()
        self._real = memory_hygiene._reclaim
        memory_hygiene._reclaim = lambda what, **kw: self.reclaims.append(
            (what, threading.get_ident() == self.loop_thread))
        self.reclaim = MemoryReclaim(self.gate, quiet_secs=QUIET, every_secs=EVERY,
                                     poll_secs=POLL)
        self.task = PipelineTask(Pipeline([self.reclaim]))
        self.runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.sleep(0.1)
        return self

    async def reply_ends(self):
        self.gate.fall_quiet()
        await self.task.queue_frame(BotStoppedSpeakingFrame())

    async def __aexit__(self, *exc):
        try:
            await self.task.queue_frame(EndFrame())
            await asyncio.wait_for(self.runner, 5)
        finally:
            memory_hygiene._reclaim = self._real


async def test_a_reply_end_reclaims_only_after_the_quiet_time_and_off_the_loop():
    async with _Harness() as h:
        await h.reply_ends()
        await asyncio.sleep(QUIET / 2)
        assert h.reclaims == [], "reclaimed in the gap after the reply, where the user speaks"
        await asyncio.sleep(QUIET + 0.3)
        assert h.reclaims == [("idle", False)], (
            f"{h.reclaims}: no reclaim once quiet, or one ON the event loop's thread")


async def test_nothing_is_reclaimed_while_the_conversation_goes_on():
    async with _Harness() as h:
        await h.reply_ends()
        h.gate.speak()   # the user answers straight away
        await asyncio.sleep(QUIET * 2)
        assert h.reclaims == [], "reclaimed while the user was speaking"
        h.gate.fall_quiet()
        await asyncio.sleep(QUIET / 2)
        assert h.reclaims == [], "the quiet time counts from the last activity, not the reply"
        await asyncio.sleep(QUIET + 0.3)
        assert len(h.reclaims) == 1, h.reclaims


async def test_at_most_one_reclaim_per_interval():
    async with _Harness() as h:
        await h.reply_ends()
        await asyncio.sleep(QUIET + 0.3)
        assert len(h.reclaims) == 1, h.reclaims
        await h.reply_ends()
        await asyncio.sleep(QUIET + 0.3)
        assert len(h.reclaims) == 1, "a second reclaim inside the interval"
        await asyncio.sleep(EVERY)
        assert len(h.reclaims) == 2, "the reply's reclaim never came after the interval"
        await asyncio.sleep(EVERY + QUIET)
        assert len(h.reclaims) == 2, "reclaimed again with no reply since"


async def test_a_session_ending_before_the_quiet_time_drops_the_wait():
    async with _Harness() as h:
        await h.reply_ends()
        h.gate.speak()
        await asyncio.sleep(0.05)
    assert h.reclaims == [] and h.reclaim._task is None, (
        "a wait outlived its session: the session-end reclaim covers that memory")


def test_release_heap_runs_both_halves_and_times_each():
    calls = []
    real_collect, real_trim = memory_hygiene.gc.collect, memory_hygiene._malloc_trim

    def collect():
        calls.append("collect")
        time.sleep(0.03)

    def trim(pad):
        calls.append(("trim", pad))
        time.sleep(0.09)
    memory_hygiene.gc.collect, memory_hygiene._malloc_trim = collect, trim
    try:
        collect_ms, trim_ms = release_heap()
    finally:
        memory_hygiene.gc.collect, memory_hygiene._malloc_trim = real_collect, real_trim
    assert calls == ["collect", ("trim", 0)], calls
    assert 25 <= collect_ms < 85 and trim_ms >= 85, (collect_ms, trim_ms)


def _child(code: str) -> str:
    """Run `code` in a fresh interpreter: gc.freeze() is process-wide and would change
    every test after this one."""
    brain = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": brain}, timeout=120)
    assert p.returncode == 0, p.stderr[-3000:]
    return p.stdout.strip().splitlines()[-1]


def test_the_server_freezes_its_startup_objects_before_any_session():
    # The real start-up path (main's TTS priming, then _ReadyServer.startup): a pipeline
    # alive at the freeze would be frozen with its cycles and never collected.
    out = _child(
        "import asyncio, gc, uvicorn\n"
        "from pipecat.processors.frame_processor import FrameProcessor\n"
        "from teaport_brain import gateway_server, memory_hygiene\n"
        "seen = {}\n"
        "def freeze():\n"
        "    gc.collect()\n"
        "    seen['processors'] = sum(isinstance(o, FrameProcessor) for o in gc.get_objects())\n"
        "    memory_hygiene.freeze_startup_objects()\n"
        "    seen['frozen'] = gc.get_freeze_count()\n"
        "gateway_server.freeze_startup_objects = freeze\n"
        "async def main():\n"
        "    gateway_server.make_tts()\n"
        "    config = uvicorn.Config(gateway_server.app, host='127.0.0.1', port=0, lifespan='off')\n"
        "    config.load()\n"
        "    server = gateway_server._ReadyServer(config)\n"
        "    server.lifespan = config.lifespan_class(config)\n"
        "    await server.startup()\n"
        "    server.sip.cancel()\n"
        "    await asyncio.gather(server.sip, return_exceptions=True)\n"
        "    await server.shutdown()\n"
        "asyncio.run(main())\n"
        "print(seen.get('processors'), seen.get('frozen', 0))\n"
    )
    processors, frozen = (int(x) if x != "None" else None for x in out.split())
    assert processors is not None, "the server came up without freezing its startup objects"
    assert processors == 0, f"{processors} pipeline processors were alive at the freeze"
    assert frozen > 1000, frozen


def _sync(fn):
    """An async case as appliance.run_tests runs one: called, to completion."""
    if not asyncio.iscoroutinefunction(fn):
        return fn

    def run():
        asyncio.run(fn())
    run.__name__ = fn.__name__
    return run


if __name__ == "__main__":
    appliance.run_tests([_sync(v) for k, v in sorted(globals().items()) if k.startswith("test_")])
