#
# Driving a real UserTurnController from a test, in one place.
#
# The tests in test_endpointing.py and test_stt_final.py drive the REAL controller with
# the REAL strategies -- that is what makes them worth having -- so they have to
# reproduce the lifecycle a pipeline would give it. pipecat 1.8.0 changed that lifecycle twice over:
# setup() now takes a FrameProcessorSetup rather than a bare task manager, and the
# turn-stop watchdog moved out of setup() into a new start(). Both call sites had to be
# fixed at the 1.7.0 -> 1.8.1 bump; keeping the shape here means the NEXT such change is
# one edit, not a hunt through the tests for the ones that happen to construct a
# controller.
#
# Being the shared helper is also why the pin guard is imported HERE and not left to the
# importer. This module binds pipecat at import time; a future test that imports it
# before pinned_pipecat would bind the wrong pipecat before require_pinned() could
# refuse, and get a raw TypeError deep in a strategy instead of the WRONG PIPECAT
# message. Both current importers happen to call it first -- that is not something a
# helper should depend on. (It is also what gets teaport_brain/__init__.py's
# HF_HUB_OFFLINE set before anything imports a model loader; see pinned_pipecat.py.)
#
import os
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.clocks.system_clock import SystemClock  # noqa: E402
from pipecat.frames.frames import STTMetadataFrame  # noqa: E402
from pipecat.processors.frame_processor import FrameProcessorSetup  # noqa: E402
from pipecat.utils.asyncio.task_manager import TaskManager  # noqa: E402

from teaport_brain.stt import TEAPORT_TTFS_P99  # noqa: E402

# The rate the brain runs the turn path at. Passed explicitly below -- see
# start_controller's audio_in_sample_rate note.
SAMPLE_RATE = 16000


async def start_controller(controller, *, sample_rate=SAMPLE_RATE,
                           ttfs_p99_latency=TEAPORT_TTFS_P99):
    """setup() + start() a UserTurnController with no pipeline around it.

    Mirrors UserTurnProcessor: setup() -> start() -> process_frame() ... Prefer
    `running_controller()` below, which also guarantees the cleanup.
    """
    clock = SystemClock()
    clock.start()
    await controller.setup(
        FrameProcessorSetup(
            clock=clock,
            # TaskManager takes its loop from the running one now: TaskManagerParams and
            # TaskManager.setup() are deprecated since 1.5.0 (removed in 2.0.0) and warn.
            task_manager=TaskManager(),
            # There deliberately is no pipeline. Required (FrameProcessorSetup gives it
            # no default) but never read here: UserTurnController is a BaseObject, not a
            # FrameProcessor, so it has no .pipeline_worker to expose, and nothing on the
            # turn path reaches for one. pipecat's own out-of-pipeline setup passes None
            # the same way (evals/speech.py:236, with a pyright suppression for it).
            pipeline_worker=None,
            # Explicit, because the turn path READS it: 1.8.0 moved the analyzer's rate
            # out of the StartFrame into TurnAnalyzerUserTurnStopStrategy.setup, which
            # does set_sample_rate(setup.audio_in_sample_rate). Omitting it silently
            # supplies pipecat's own 16000 default, which is right here only by
            # coincidence -- and production is the case that would break, because
            # agent_session builds its EagerSmartTurnAnalyzer with no sample_rate of its
            # own, so nothing else would correct a narrowband 8 kHz variant. pipecat's
            # out-of-pipeline setup passes its rate explicitly too (evals/speech.py:232
            # -- the OUT rate, being a TTS harness; this is the turn path's IN rate).
            audio_in_sample_rate=sample_rate,
        )
    )
    await controller.start()
    # What the STT service broadcasts at pipeline start, and what the stop strategy's
    # safety-net timer is sized from: without it _stt_timeout is 0.0 and every timer
    # derived from it fires instantly, which is NOT how production runs and has hidden
    # a real ordering bug before (git show e983f90 -- the test deleted there says so in
    # its own docstring). A harness that leaves it out tests a configuration the brain
    # never ships.
    await controller.process_frame(
        STTMetadataFrame(service_name="teaport", ttfs_p99_latency=ttfs_p99_latency)
    )


@asynccontextmanager
async def running_controller(controller, **kwargs):
    """start_controller(), and cleanup() however the body leaves.

    The cleanup is not a nicety: start() now creates the turn-stop watchdog task
    (1.8.0 moved it out of setup()), and only cleanup() -> stop() reaps it. A test that
    asserts before its own cleanup line -- as these did -- leaks that task and a live
    ThreadPoolExecutor on the way out, so a real assertion message arrives buried in
    "Task was destroyed but it is pending", and any remaining timer in a suite that
    calls this many times per event loop keeps firing against the tests after it.
    """
    await start_controller(controller, **kwargs)
    try:
        yield controller
    finally:
        await controller.cleanup()
