#
# teaport — speculative reply: ask the LLM while endpointing is still deciding.
#
# Every turn on this brain pays the LLM's round trip (~0.47 s to first text) AFTER the
# turn commits, and the commit waits on endpointing. When Smart Turn says INCOMPLETE the
# wait is the analyzer's silence ceiling, SMARTTURN_STOP_SECS, counted from the VAD stop:
# the final transcript is in hand for most of it and nothing is done with it. Measured on
# the appliance 2026-09-16 (SIP, 51 verdicts): 41% INCOMPLETE, and 13 of those 21 fell
# through to the ceiling with the text they already had.
#
# So: when a FINAL lands and the stop strategy does not conclude on it, open the request
# now, into a buffer, against a snapshot of the context plus that text as the user message.
# When the turn does commit, the LLM service asks here first: if the context it was handed
# equals the snapshot, the buffered stream is handed over and the live one continues from
# it -- the LLM's head start is the time endpointing took. Anything else is a MISS: the
# speculation is closed and the ordinary request is made, exactly as without this module.
#
# What makes it safe:
#   * The context is never touched. The speculation runs on a deep copy; the aggregator
#     writes the user message and the ledger charts the reply on the ordinary path.
#   * Promotion is on WHOLE-CONTEXT equality, not on the text. A memory note, a consult
#     follow-up -- anything that wrote the context between snapshot and commit -- makes
#     the reply stale, and a stale reply is a miss. brain/formal/UserTurn.tla
#     (SPEC = "byText") is the counterexample for promoting on text alone. The one such
#     writer that is OURS and runs at the commit, the heard-context corrector, is run
#     before the snapshot instead so its rewrite is inside it (see Speculator).
#   * Replay is at the CHUNK level, below base_llm's parser. A speculated tool call is
#     buffered as deltas and parsed and dispatched by _process_context only once the real
#     turn adopts the stream; nothing executes early.
#   * Single-flight: a new final supersedes the speculation, a VAD start (the caller
#     resumed) cancels it -- the text will differ, so the tokens are wasted either way and
#     stopping the stream stops the bill.
#
# What it does NOT do: speculate on interims. Measured the same day, the interim at the
# VAD stop equalled the final on 0 of 34 SIP turns -- the engine's deltas lag speech by
# more than the VAD floor and the final is a re-decode, not the deltas joined. There is
# no text to speculate on before the final; a shorter trigger would only add misses.
#
# The cost is a wasted request on the INCOMPLETE turns where the caller does resume (8 of
# 21 above). Every outcome logs one [SPEC] line with its reason and running totals, so
# the waste is a number in the journal. Off by default: TEAPORT_SPECULATIVE_REPLY=1.
#
import asyncio
import copy
import time

from loguru import logger

from pipecat.processors.aggregators.llm_context import LLMContext

from teaport_brain.env import env_flag

ENABLED = env_flag("TEAPORT_SPECULATIVE_REPLY", False)


class _Speculation:
    """One shadow request: the snapshot it was asked against, and its chunks so far."""

    def __init__(self, messages: list, tools, text: str):
        self.messages = messages   # deep copy of the context, candidate user message last
        self.tools = tools         # by identity: LLMSetToolsFrame replaces the object
        self.text = text
        self.chunks: list = []
        self.done = False
        self.error: BaseException | None = None
        self.started = time.monotonic()
        self.changed = asyncio.Event()
        self.task: asyncio.Task | None = None
        self._stream = None
        self._iter = None

    async def run(self, llm, ctx: LLMContext):
        try:
            self._stream = await llm._open_stream(ctx)
            self._iter = self._stream.__aiter__()
            async for chunk in self._iter:
                self.chunks.append(chunk)
                self.changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — reported at take(); the turn falls back
            self.error = e
        finally:
            self.done = True
            self.changed.set()

    async def close(self):
        """Stop reading and release the HTTP stream. Idempotent."""
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Iterator first, then the stream -- the order base_llm's _closing keeps, for the
        # same uvloop reason (_RenumberedStream's docstring in services.py).
        it, self._iter = self._iter, None
        if it is not None and hasattr(it, "aclose"):
            try:
                await it.aclose()
            except Exception:  # noqa: BLE001
                pass
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                await stream.close()
            except Exception:  # noqa: BLE001
                pass


class _AdoptedStream:
    """The speculation's chunks, buffered then live, in the shape base_llm tears down:
    __aiter__() gives an async generator (aclose), close() releases the real stream."""

    __slots__ = ("_spec", "_iter")

    def __init__(self, spec: _Speculation):
        self._spec = spec
        self._iter = self._replay()

    async def _replay(self):
        spec = self._spec
        i = 0
        while True:
            while i < len(spec.chunks):
                yield spec.chunks[i]
                i += 1
            if spec.done:
                if spec.error is not None:
                    raise spec.error
                return
            # No await between the length check and clear(), so an append cannot slip
            # between them and be waited past.
            spec.changed.clear()
            await spec.changed.wait()

    def __aiter__(self):
        return self._iter

    async def close(self):
        await self._spec.close()


class Speculator:
    """Owns at most one speculation per session.

    start() is called by the stop strategy when a final lands and the turn does not
    conclude on it; take() by the LLM service when the turn does commit; cancel() by the
    strategy when the caller resumes or a new turn opens.

    Reaches into the aggregator's controller for `_user_turn`, deliberately undefended:
    a speculation for a final that opened no turn (a one-word garble under the barge-in
    guard) is a request nothing will ever adopt, and the controller is the only thing
    that knows. A pipecat rename raises here on the first final, loudly, rather than
    letting the guard silently lapse into a billed request per garble.
    """

    def __init__(self, *, llm, aggregator, before_snapshot=None):
        self._llm = llm
        self._agg = aggregator
        self._controller = aggregator._user_turn_controller
        # HeardContextCorrector._reconcile, when wired: it rewrites the previous reply to
        # what was heard on the LLMContextFrame -- i.e. at the commit, AFTER a snapshot
        # taken at the final. Live 2026-09-16, first call with the feature on: the one
        # ctx-changed miss was exactly that ("spoken reply -> heard 'Got it'" landing
        # between a 0.62 s head start and the commit). The cut it records happened at the
        # barge-in, before the final, so applying it here is applying it earlier, not
        # differently; it is idempotent (it tracks the ledger events it has consumed).
        self._before_snapshot = before_snapshot
        self._current: _Speculation | None = None
        # Strong refs: the loop holds tasks weakly, and a reader whose speculation
        # nothing else references any more would be destroyed pending, mid-read.
        self._tasks: set[asyncio.Task] = set()
        self.hits = 0
        self.misses = 0

    def _tally(self) -> str:
        return f"hits={self.hits} misses={self.misses}"

    async def start(self, why: str = "") -> bool:
        if not self._controller._user_turn:
            return False
        text = self._agg.aggregation_string().strip()
        if not text:
            return False
        await self.cancel("superseded")
        if self._before_snapshot is not None:
            self._before_snapshot()
        ctx = self._agg.context
        # Deep, not shallow: the follow-up injector retires its trigger by rewriting a
        # message IN PLACE, which a shallow copy would share and equality would miss.
        snapshot = copy.deepcopy(ctx.messages) + [{"role": "user", "content": text}]
        request = LLMContext(list(snapshot), tools=ctx.tools, tool_choice=ctx.tool_choice)
        spec = _Speculation(snapshot, ctx.tools, text)
        spec.task = asyncio.create_task(spec.run(self._llm, request))
        self._tasks.add(spec.task)
        spec.task.add_done_callback(self._tasks.discard)
        self._current = spec
        logger.info(f"[SPEC] start {text[:60]!r}{f' ({why})' if why else ''}")
        return True

    async def cancel(self, reason: str):
        spec, self._current = self._current, None
        if spec is None:
            return
        self.misses += 1
        logger.info(f"[SPEC] miss reason={reason} after {time.monotonic() - spec.started:.2f}s "
                    f"{spec.text[:40]!r} {self._tally()}")
        await spec.close()

    async def take(self, context: LLMContext):
        """The stream for `context` if the live speculation was asked exactly that,
        else None -- and the speculation is over either way."""
        spec, self._current = self._current, None
        if spec is None:
            return None
        live = context.messages
        if spec.done and spec.error is not None:
            reason = f"spec-failed ({type(spec.error).__name__})"
        elif context.tools is not spec.tools:
            reason = "tools-changed"
        elif live != spec.messages:
            reason = "ctx-changed" if live and live[-1] == spec.messages[-1] else "text-differs"
        else:
            reason = None
        if reason is not None:
            self.misses += 1
            logger.info(f"[SPEC] miss reason={reason} {spec.text[:40]!r} {self._tally()}")
            await spec.close()
            return None
        self.hits += 1
        lead = time.monotonic() - spec.started
        logger.info(f"[SPEC] hit lead=+{lead * 1000:.0f}ms buffered={len(spec.chunks)} chunks"
                    f"{' (complete)' if spec.done else ''} {self._tally()}")
        return _AdoptedStream(spec)
