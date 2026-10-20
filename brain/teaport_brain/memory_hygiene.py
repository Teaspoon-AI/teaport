#
# teaport — appliance memory hygiene (8 GB unified-RAM Jetson).
#
# Everything here exists because the brain shares one small memory pool with the
# engine: glibc arena trimming and torch CUDA-cache reclaim. The division
# of labor (in-session at a quiet moment vs session-end) is deliberate — see
# MemoryReclaim's docstring for the stall and the deadlock that shaped it. (the TTS
# prewarm helper left with the onnx backend.)
#
import asyncio
import ctypes
import gc
import resource
import time

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
    _malloc_trim = _LIBC.malloc_trim
except OSError:
    def _malloc_trim(_pad):
        pass


def release_heap() -> tuple[float, float]:
    """gc.collect() then malloc_trim(0); returns how long each took, in ms.

    Both stop the event loop even when run on a worker thread: the collect holds the
    GIL, and the trim holds glibc's arena locks, which the loop's own mallocs wait on.
    On the box the pair took 180-245 ms (2026-10-15)."""
    t0 = time.monotonic()
    gc.collect()
    t1 = time.monotonic()
    _malloc_trim(0)
    t2 = time.monotonic()
    return (t1 - t0) * 1000, (t2 - t1) * 1000


def freeze_startup_objects() -> None:
    """Move everything alive at startup (the imported modules, the brain's own
    singletons) into gc's permanent generation, so no later collect walks it. Called
    once, when the server is up and before the first session: a session's objects must
    stay collectable, or the cycles a torn-down pipeline leaves would never be freed.

    Then hand back the pages startup's temporaries freed (the typing bed's synthesis
    alone leaves ~9 MB in the heap): glibc keeps them until a trim, and the first
    session-end trim may be hours away. No session is running yet, so its stall
    (release_heap) costs nothing here."""
    gc.collect()
    gc.freeze()
    _malloc_trim(0)
    logger.info(f"gc: {gc.get_freeze_count()} startup objects frozen")


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

    Logs the RSS it handed back. The only CUDA reclaim a session gets, and the only
    heap reclaim for one too short to reach a MemoryReclaim quiet moment; what it
    defends against — ~35 MB/session of glibc arena ratchet on an 8 GB
    unified pool — is invisible until the box OOMs days later. It used to log
    nothing at all, so on the appliance there was no way to answer "did it run?"
    except by watching RSS from outside; a run on 2026-08-28 could confirm the call
    tore down but not that anything was reclaimed. One line per session is cheap
    (two /proc reads) and turns a silent invariant into a checkable one.
    """
    _reclaim("session-end", cuda=True)


def _reclaim(what: str, *, cuda: bool = False) -> None:
    before = _rss_mb()
    collect_ms, trim_ms = release_heap()
    if cuda:
        empty_cuda_cache()
    after = _rss_mb()
    took = f"gc {collect_ms:.0f} ms + trim {trim_ms:.0f} ms"
    if before is None or after is None:
        logger.info(f"{what} reclaim done ({took}; RSS unavailable)")
    else:
        logger.info(f"{what} reclaim: RSS {before:.0f} -> {after:.0f} MiB "
                    f"({before - after:+.0f}; {took})")


# An in-session reclaim waits for the conversation to have been quiet this long: not the
# gap after a reply, when the user is most likely to speak, and not a pause mid-thought.
RECLAIM_QUIET_SECS = 5.0
# And runs at most this often: memory comes back a minute late at worst, and a chatty
# conversation pays one stall a minute at most instead of one per reply.
RECLAIM_EVERY_SECS = 60.0
_POLL_SECS = 1.0


class MemoryReclaim(FrameProcessor):
    """Hand freed glibc arena pages back to the OS during a session, at a quiet moment.
    The per-turn pipeline churns native buffers (onnxruntime/numpy/resampled audio);
    Python frees them but glibc keeps the arena pages. A room conversation is one
    session that lasts all day, so the session-end reclaim alone would never run there.

    NOT at every reply's end, as it used to be. gc.collect() and malloc_trim(0) stop
    the event loop wherever they run (release_heap): on the box every reply was
    followed by a ~220 ms freeze, just when the user starts to speak, and a barge-in
    landing in it waited that long to stop the reply (2026-10-15). Now a reply only
    marks the heap worth trimming; the trim runs once the conversation has been clear
    (FollowupGate: nobody speaking, no turn in flight) for RECLAIM_QUIET_SECS, at most
    once per RECLAIM_EVERY_SECS. Off the loop, single-flight, best-effort.

    Deliberately does NOT call torch.cuda.empty_cache() here: empty_cache locks the
    CUDA caching allocator, and if it overlaps the next turn's TTS synth (e.g. the
    user barges in over the reply), the TTS task can't be cancelled — it's blocked
    on that lock — which deadlocks the turn (unresponsive bot + 'timed out waiting
    for task to cancel'). The CUDA cache is reclaimed at session end instead
    (turn_reclaim in the talk() finally), where nothing is mid-synthesis."""

    def __init__(self, gate, *, quiet_secs: float = RECLAIM_QUIET_SECS,
                 every_secs: float = RECLAIM_EVERY_SECS, poll_secs: float = _POLL_SECS):
        super().__init__()
        self._gate = gate
        self._quiet = quiet_secs
        self._every = every_secs
        self._poll = poll_secs
        self._last = float("-inf")   # time.monotonic() of the last reclaim's end
        self._task = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, BotStoppedSpeakingFrame) and self._task is None:
            self._task = self.create_task(self._reclaim_when_quiet(), "MemoryReclaim")

    async def _reclaim_when_quiet(self):
        try:
            while True:
                await asyncio.sleep(self._poll)
                now = time.monotonic()
                if (now - self._last >= self._every and self._gate.is_clear()
                        and now - self._gate.last_active >= self._quiet):
                    break
            await asyncio.get_running_loop().run_in_executor(None, _reclaim, "idle")
            self._last = time.monotonic()
        finally:
            self._task = None

    async def cleanup(self):
        if self._task is not None:
            await self.cancel_task(self._task)
            self._task = None
        await super().cleanup()
