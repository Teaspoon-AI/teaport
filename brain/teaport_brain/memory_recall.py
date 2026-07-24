#
# teaport — shared-memory recall for the voice loop (Phase 1, read side).
#
import asyncio

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.openclaw_client import memory_search


class MemoryRecall(FrameProcessor):
    """Share the OpenClaw text agent's memory with the voice loop (Phase 1, read).

    Fires `memory_search` over the loopback gateway on the user's INTERIM transcript
    (re-firing as it grows), so the query-embedding round-trip overlaps endpointing.
    Searches are SINGLE-FLIGHT (a fresher interim cancels the in-flight one) and the
    result is freshness-RANKED by query length — not last-writer-by-completion-order,
    which let a stale short-prefix embed finishing late clobber a fresher long-prefix
    result. On the FINAL transcript the freshest hit is injected into the LLM context
    as a system note — synchronously, BEFORE the frame is forwarded to the user
    aggregator, so it lands before the LLM runs — and any still-in-flight search is
    cancelled (its result can no longer be injected and would only steal bandwidth).
    It deliberately NEVER awaits in the frame path: awaiting there races the pipeline's
    turn-advance/teardown and gets cancelled (observed). So recall is strictly
    best-effort — a slow or empty search just means no note this turn; the turn never
    stalls and nothing raises into the pipeline."""

    def __init__(self, context, *, min_chars: int = 12, max_snippets: int = 3):
        super().__init__()
        self._context = context
        self._min_chars = min_chars
        self._max = max_snippets
        self._fired_len = 0   # chars of the query last searched (0 = none yet this turn)
        self._fire_count = 0  # cap re-fires per turn
        self._latest: list[str] = []  # freshest non-empty hit this turn (set by callback)
        self._latest_len = 0  # query length behind _latest (freshness rank key)
        self._inflight: asyncio.Task | None = None  # single-flight: only the newest search
        self._bg: set[asyncio.Task] = set()  # keep refs so tasks aren't GC'd mid-flight
        self._turn = 0  # generation tag: a search completing after its turn's final
                        # transcript must not repopulate _latest for the NEXT turn (its
                        # done-callback runs after our reset — cancellation can't cover
                        # the already-completed case)

    def _spawn(self, query: str) -> None:
        qlen = len(query)
        turn = self._turn
        # Single-flight, replace-newest: a fresher interim supersedes the in-flight
        # search. A late embed can't be injected (we inject at the final transcript)
        # and only steals STT bandwidth, so cancel the older one rather than run both.
        if self._inflight is not None and not self._inflight.done():
            self._inflight.cancel()
        task = asyncio.create_task(memory_search(query, max_results=self._max))
        self._inflight = task
        self._bg.add(task)

        def _done(t: asyncio.Task):
            self._bg.discard(t)
            if self._inflight is t:
                self._inflight = None
            if t.cancelled():
                return  # superseded/aborted — never touch _latest
            if turn != self._turn:
                return  # completed for a PREVIOUS turn — stale snippets, drop
            try:
                snippets = t.result() or []
            except Exception:  # noqa: BLE001 — best-effort; never surface into the loop
                snippets = []
            # Freshness-rank: only overwrite if this query is at least as complete as
            # the one behind the current _latest, so out-of-order completions can't
            # regress to a staler prefix.
            if snippets and qlen >= self._latest_len:
                self._latest = snippets
                self._latest_len = qlen

        task.add_done_callback(_done)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InterimTranscriptionFrame):
            text = (frame.text or "").strip()
            # Re-fire as the interim GROWS so a near-complete query gets searched —
            # firing only on the first partial searches too little of a long utterance.
            grew = len(text) - self._fired_len >= 25
            if len(text) >= self._min_chars and (self._fired_len == 0 or grew) \
                    and self._fire_count < 6:
                self._fired_len = len(text)
                self._fire_count += 1
                self._spawn(text)
                logger.debug(f"MemoryRecall: firing search on interim ({len(text)} chars)")
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame):
            # Inject whatever search has already completed for this turn, synchronously,
            # before forwarding the final transcript (which triggers the LLM downstream).
            if self._latest:
                joined = "\n".join(f"- {s}" for s in self._latest)
                self._context.add_message({
                    "role": "system",
                    "content": (
                        "You remember these things about the user from earlier chats "
                        "(by text or by voice). Use them if relevant; don't mention "
                        f"that you looked them up:\n{joined}"
                    ),
                })
                logger.info(f"MemoryRecall injected {len(self._latest)} snippet(s)")
            # The injection point has passed: a search still running can no longer be
            # injected and would only steal STT bandwidth from the next turn — cancel it.
            if self._inflight is not None and not self._inflight.done():
                self._inflight.cancel()
            self._inflight = None
            self._turn += 1  # invalidate done-callbacks still pending from this turn
            self._latest = []
            self._latest_len = 0
            self._fired_len = 0
            self._fire_count = 0
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)
