#
# teaport — appliance memory hygiene (8 GB unified-RAM Jetson).
#
# Everything here exists because the brain shares one small memory pool with the
# engine: glibc arena trimming and torch CUDA-cache reclaim. The division
# of labor (per-turn vs session-end) is deliberate — see MemoryReclaim's docstring
# for the deadlock that shaped it. (the TTS prewarm helper left with the onnx backend.)
#
import asyncio
import ctypes
import gc

from pipecat.frames.frames import BotStoppedSpeakingFrame, Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Return freed heap pages to the OS after each session. The per-session pipeline
# churns many short-lived native buffers (onnxruntime tensors, resampled audio,
# numpy); Python frees them — object counts and tracemalloc stay flat across
# sessions — but glibc keeps the pages in its arenas, so RSS ratchets up
# ~35 MB/session. That is allocator fragmentation, not a Python leak; malloc_trim(0)
# hands the pages back and RSS then plateaus. No-op on a non-glibc libc.
try:
    _LIBC = ctypes.CDLL("libc.so.6")

    def release_heap():
        gc.collect()
        _LIBC.malloc_trim(0)
except OSError:
    def release_heap():
        gc.collect()


def empty_cuda_cache():
    # torch caches freed CUDA blocks and never returns them to the driver on its
    # own; on the unified 8GB pool that ratchets until it OOMs (NvMap). The
    # appliance venv is torch-free since the TTS moved into the engine (this is a
    # silent no-op there) — kept for torch-ful dev environments, where skipping
    # it re-opens the old per-session GPU ratchet. Lazy import, best-effort.
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def turn_reclaim():
    """Full reclaim (heap + CUDA) — session-end only; see MemoryReclaim for why."""
    release_heap()
    empty_cuda_cache()


class MemoryReclaim(FrameProcessor):
    """Hand freed glibc arena pages back to the OS at every bot-turn boundary. The
    per-turn pipeline churns native buffers (onnxruntime/numpy/resampled audio);
    Python frees them but glibc keeps the arena pages, so RSS ratchets. malloc_trim
    hands them back. Runs OFF the event loop, single-flight, best-effort.

    Deliberately does NOT call torch.cuda.empty_cache() here: empty_cache locks the
    CUDA caching allocator, and if it overlaps the next turn's TTS synth (e.g. the
    user barges in over the reply), the TTS task can't be cancelled — it's blocked
    on that lock — which deadlocks the turn (unresponsive bot + 'timed out waiting
    for task to cancel'). The CUDA cache is reclaimed at session end instead
    (turn_reclaim in the talk() finally), where nothing is mid-synthesis."""

    def __init__(self):
        super().__init__()
        self._busy = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, BotStoppedSpeakingFrame) and not self._busy:
            self._busy = True
            fut = asyncio.get_running_loop().run_in_executor(None, release_heap)
            fut.add_done_callback(lambda _f: setattr(self, "_busy", False))
