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
import resource

from loguru import logger

from pipecat.frames.frames import BotStoppedSpeakingFrame, Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Return freed heap pages to the OS after each session. The per-session pipeline
# churns many short-lived native buffers (onnxruntime tensors, resampled audio,
# numpy); Python frees them — object counts and tracemalloc stay flat across
# sessions — but glibc keeps the pages in its arenas, so RSS ratchets up
# ~35 MB/session. That is allocator fragmentation, not a Python leak; malloc_trim(0)
# hands the pages back and RSS then plateaus. No-op on a non-glibc libc.
_PAGE_SIZE = resource.getpagesize()

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


def _rss_mb() -> float | None:
    """Resident set size in MiB, or None where /proc isn't available."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * _PAGE_SIZE / (1024 * 1024)
    except (OSError, IndexError, ValueError):
        return None


def turn_reclaim():
    """Full reclaim (heap + CUDA) — session-end only; see MemoryReclaim for why.

    Logs the RSS it handed back. This is the ONLY reclaim a session ever gets, and
    what it defends against — ~35 MB/session of glibc arena ratchet on an 8 GB
    unified pool — is invisible until the box OOMs days later. It used to log
    nothing at all, so on the appliance there was no way to answer "did it run?"
    except by watching RSS from outside; a run on 2026-08-28 could confirm the call
    tore down but not that anything was reclaimed. One line per session is cheap
    (two /proc reads) and turns a silent invariant into a checkable one.
    """
    before = _rss_mb()
    release_heap()
    empty_cuda_cache()
    after = _rss_mb()
    if before is None or after is None:
        logger.info("session-end reclaim done (RSS unavailable)")
    else:
        logger.info(f"session-end reclaim: RSS {before:.0f} -> {after:.0f} MiB "
                    f"({before - after:+.0f})")


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
