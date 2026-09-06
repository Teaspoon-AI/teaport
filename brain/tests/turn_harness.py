#
# Driving a real UserTurnController from a test, in one place.
#
# Three tests (test_endpointing.py, test_stt_final.py) drive the REAL controller with the
# REAL strategies -- that is what makes them worth having -- so they have to reproduce the
# lifecycle a pipeline would give it. pipecat 1.8.0 changed that lifecycle twice over:
# setup() now takes a FrameProcessorSetup rather than a bare task manager, and the
# turn-stop watchdog moved out of setup() into a new start(). Both call sites had to be
# fixed at the 1.7.0 -> 1.8.1 bump; keeping the shape here means the NEXT such change is
# one edit, not a hunt through the tests for the ones that happen to construct a
# controller.
#
from pipecat.clocks.system_clock import SystemClock
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager


async def start_controller(controller):
    """setup() + start() a UserTurnController with no pipeline around it.

    Mirrors UserTurnProcessor: setup() -> start() -> process_frame() ... The caller
    still ends with `await controller.cleanup()`, which stops the watchdog itself.
    """
    clock = SystemClock()
    clock.start()
    await controller.setup(
        FrameProcessorSetup(
            clock=clock,
            # TaskManager takes its loop from the running one now: TaskManagerParams and
            # TaskManager.setup() are deprecated since 1.5.0 (removed in 2.0.0) and warn.
            task_manager=TaskManager(),
            # There deliberately is no pipeline. FrameProcessor.pipeline_worker raises if
            # anything touches it, and nothing on the turn path does -- pipecat's own
            # out-of-pipeline setup (evals/speech.py) passes None the same way.
            pipeline_worker=None,
        )
    )
    await controller.start()
