#
# teaport — speculative reply: ask the LLM while endpointing is still deciding.
#
# Every turn on this brain pays the LLM's round trip (~0.47 s to first text) AFTER the
# turn commits, and the commit waits on endpointing. When Smart Turn says INCOMPLETE the
# wait is the analyzer's silence ceiling, SMARTTURN_STOP_SECS, counted from the VAD stop,
# and on the turns where the caller does not resume the words are known long before it
# ends. Measured on the appliance 2026-09-16 (SIP, 51 verdicts): 41% INCOMPLETE, and 13
# of those 21 fell through to the ceiling with the text they already had.
#
# So: when the caller's words have stopped changing on a turn the stop strategy has not
# concluded on, open the request now, into a buffer, against a snapshot of the context
# plus those words as the user message. When the turn does commit, the LLM service asks
# here first: if the context it was handed equals the snapshot, the buffered stream is
# handed over and the live one continues from it -- the LLM's head start is the time
# endpointing took. Anything else is a MISS: the speculation is closed and the ordinary
# request is made, exactly as without this module.
#
# The words are the settled INTERIM. Since #43 the STT holds its segment open through an
# INCOMPLETE verdict's wait (stt.py) -- a caller who resumes keeps one utterance -- so no
# final lands before the ceiling commits; this module used to need
# TEAPORT_STT_COMMIT_ON=vad-stop for a final to work on, and under the default found
# none (2 [SPEC] lines a day on the appliance, both barge-in edge cases). What made the
# interim unusable when this was written -- it equalled the final on 0 of 34 SIP turns
# (2026-09-16), the engine's deltas lagging speech and the final a re-decode -- is no
# longer true of the engine. Measured 2026-10-09 on the appliance: the last interim
# equalled the final on 48 of 48 phone turns and 23 of 23 local-mic turns, the final
# landing ~0.01 s after it. On the 8 of 57 phone replies that waited out the ceiling
# (committed +1.08 s after the VAD stop, against +0.41 s for a COMPLETE verdict) the
# interim had settled 0.17-0.80 s before the commit, median ~0.6 s; the LLM's first
# token takes 0.55-0.94 s, so a request opened on the settled interim takes ~0.5 s off
# each of those turns. It also makes a longer SMARTTURN_STOP_SECS -- fewer callers cut
# off mid-sentence -- cost nothing on the turns that fall through it. The stop strategy
# owns the trigger (endpointing.LateStartTurnStopStrategy); SETTLE_SECS is the window.
#
# The candidate user message is built here (candidate()) the way the user aggregator
# will build it at the commit: its pending finals, plus the interim as the final that
# will close the segment. A COMPLETE verdict is never speculated on -- it commits at
# once and the final trails the last interim by ~0.01 s, so there is nothing to gain.
#
# What makes it safe:
#   * The context is never touched. The speculation runs on a deep copy; the aggregator
#     writes the user message and the ledger charts the reply on the ordinary path.
#   * Promotion is on WHOLE-CONTEXT equality, not on the text. Anything that wrote the
#     context between snapshot and commit makes the reply stale, and a stale reply is a
#     miss. brain/formal/UserTurn.tla (SPEC = "byText") is the counterexample for
#     promoting on text alone, and SPEC = "byHistory" for the converse: promoting on
#     the context before the user message while the words moved after the snapshot.
#     The one allowance is whitespace at the ends of the user message (the interim
#     carries the engine's leading space, the final is stripped); a word, a comma or a
#     capital that differs is a miss, never an adoption -- the model answered other
#     words. Which writers can land in the window: MemoryRecall injects its note on the
#     FINAL, before the aggregator sees it. That put it inside a snapshot taken on the
#     final, and puts it AFTER one taken on the interim -- so the snapshot includes the
#     note MemoryRecall would inject now (Speculator's `pending`), and a fresher hit
#     that replaces it before the final is a ctx-changed miss, never a stale reply. The
#     consult follow-up and ClientNotes (a Talk client's context notes) post only with
#     the turn free, and a speculation only exists inside an open turn. ClientNotes
#     also folds pending notes into the context at the commit, after the snapshot: that
#     turn is a miss, never a stale reply. The one writer that is OURS and ran at the
#     commit, the heard-context corrector, is run before the snapshot instead so its
#     rewrite is inside it (see Speculator). The equality check stays whole-context
#     regardless, because nothing in the machine prevents a new writer, and the check
#     is what makes a new one a miss rather than a wrong reply.
#   * Replay is at the CHUNK level, below base_llm's parser. A speculated tool call is
#     buffered as deltas and parsed and dispatched by _process_context only once the real
#     turn adopts the stream; nothing executes early.
#   * Single-flight: a changed interim or a new final supersedes the speculation, a VAD
#     start (the caller resumed) cancels it -- the text will differ, so the tokens are
#     wasted either way and stopping the stream stops the bill. A start for the words
#     and context already being asked (the final confirming the interim) keeps the
#     stream it has. The session's end closes it too (the strategy's cleanup): a stream
#     nothing will adopt must not keep billing after the hangup.
#   * Nothing waits on a stale stream's teardown. A miss and a cancel hand the HTTP
#     close to a background task and return: the ordinary request (or the VAD start
#     being handled) is on the turn's critical path, the close is not.
#
# The cost is a wasted request on the INCOMPLETE turns where the caller does resume (8 of
# 21 above), and one per superseded interim. Every outcome logs one [SPEC] line with its
# reason and running totals, so the waste is a number in the journal. Off by default:
# TEAPORT_SPECULATIVE_REPLY=1.
#
import asyncio
import copy
import time

from loguru import logger

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.utils.string import TextPartForConcatenation, concatenate_aggregated_text

from teaport_brain.env import env_flag, env_num

ENABLED = env_flag("TEAPORT_SPECULATIVE_REPLY", False)

# How long the interim must stop changing, under an INCOMPLETE verdict with the caller
# quiet, before a speculation is opened on it. The engine emits deltas per 80 ms decoder
# token, and after the VAD stop it is draining its transcription delay (480 ms since
# 2026-09-30): the caller's last words come out in bursts, with an empty token between
# two words an ordinary gap. Shorter than one token, the window fires between any two
# deltas; one token, on every such gap; two (160 ms) is the shortest that a single empty
# token cannot trip. What it costs is lead: on the 2026-10-09 fallthroughs (header) the
# interim had settled a median ~0.6 s before the commit, so a request opened 0.16 s after
# that still starts ~0.44 s ahead of it, all of which the LLM's 0.55-0.94 s to first token
# absorbs; the tightest one (0.17 s) gains next to nothing at any window. Too short shows
# in the journal as "[SPEC] miss reason=superseded" lines, each a request opened on words
# that then grew; that count is the number to size this from.
SETTLE_SECS = max(0, env_num("TEAPORT_SPECULATE_SETTLE_MS", "160", int)) / 1000.0


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


def _ends_stripped(message):
    """A message with the whitespace at the ends of its text content dropped: the one
    difference take() forgives in the user message (see the module header)."""
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return message
    return {**message, "content": content.strip()}


def _same_ask(live: list, asked: list) -> bool:
    """Whether the context the commit produced is the one the speculation asked: every
    message equal, the last (the user message) modulo whitespace at its ends."""
    return (len(live) == len(asked) and live[:-1] == asked[:-1]
            and _ends_stripped(live[-1]) == _ends_stripped(asked[-1]))


class Speculator:
    """Owns at most one speculation per session.

    start() is called by the stop strategy with the candidate user message -- the
    settled interim under an INCOMPLETE verdict, or a final the turn did not conclude
    on -- built by candidate(); take() by the LLM service when the turn does commit;
    cancel() by the strategy when the words change, the caller resumes or a new turn
    opens; close() by the strategy's cleanup at the session's end.

    Reaches into the aggregator's controller for `_user_turn`, deliberately undefended:
    a speculation for a final that opened no turn (a one-word garble under the barge-in
    guard) is a request nothing will ever adopt, and the controller is the only thing
    that knows. A pipecat rename raises here on the first final, loudly, rather than
    letting the guard silently lapse into a billed request per garble. candidate()
    reaches for the aggregator's `_aggregation` the same way, for the same reason.
    """

    def __init__(self, *, llm, aggregator, before_snapshot=None, pending=None):
        self._llm = llm
        self._agg = aggregator
        self._controller = aggregator._user_turn_controller
        # Read by the stop strategy, which owns the timer (SETTLE_SECS).
        self.settle_secs = SETTLE_SECS
        # HeardContextCorrector._reconcile, when wired: it rewrites the previous reply to
        # what was heard on the LLMContextFrame -- i.e. at the commit, AFTER a snapshot
        # taken under the ceiling. Live 2026-09-16, first call with the feature on: the one
        # ctx-changed miss was exactly that ("spoken reply -> heard 'Got it'" landing
        # between a 0.62 s head start and the commit). The cut it records happened at the
        # barge-in, before the final, so applying it here is applying it earlier, not
        # differently; it is idempotent (it tracks the ledger events it has consumed).
        self._before_snapshot = before_snapshot
        # MemoryRecall.pending_messages, when wired: the note the final will inject
        # between the snapshot (taken on the interim) and the commit. Live 2026-10-09,
        # 5 injections in ~57 phone turns -- each one a certain ctx-changed miss with the
        # note left out. Placed in the snapshot where the injection will put it (after
        # the context, before the user message); if a fresher hit replaces it before the
        # final, the context the commit has is not the snapshot and take() misses.
        self._pending = pending
        self._current: _Speculation | None = None
        # Strong refs, for the readers and the detached closes: the loop holds tasks
        # weakly, and a reader whose speculation nothing else references any more would
        # be destroyed pending, mid-read.
        self._tasks: set[asyncio.Task] = set()
        self.hits = 0
        self.misses = 0

    def _tally(self) -> str:
        return f"hits={self.hits} misses={self.misses}"

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _release(self, spec: _Speculation):
        """Tear the stale stream down off the critical path. cancel() runs inside the
        aggregator's handling of the VAD start and a miss in take() runs ahead of the
        ordinary request; neither should wait on a provider closing a socket."""
        self._spawn(spec.close())

    def candidate(self, interim: str = "") -> str:
        """The user message the aggregator will write if the turn commits with `interim`
        as the open segment's final: its pending finals, then that text, joined by
        pipecat's own concatenate_aggregated_text exactly as push_aggregation joins
        them. The STT's final is a TranscriptionFrame, whose includes_inter_frame_spaces
        is False (TextFrame sets it); the interim is stripped as the final is (stt.py:
        the interim is the deltas joined, with the engine's leading space)."""
        parts = list(self._agg._aggregation)
        interim = (interim or "").strip()
        if interim:
            parts.append(TextPartForConcatenation(interim, includes_inter_part_spaces=False))
        return concatenate_aggregated_text(parts)

    @property
    def text(self) -> str | None:
        """The user message the live speculation asked, or None."""
        return self._current.text if self._current is not None else None

    async def start(self, text: str, why: str = "") -> bool:
        if not self._controller._user_turn:
            return False
        text = (text or "").strip()
        if not text:
            return False
        if self._before_snapshot is not None:
            self._before_snapshot()
        ctx = self._agg.context
        # Deep, not shallow: the follow-up injector retires its trigger by rewriting a
        # message IN PLACE, which a shallow copy would share and equality would miss.
        asked = copy.deepcopy(ctx.messages) + copy.deepcopy(
            self._pending() if self._pending is not None else [])
        live = self._current
        if (live is not None and live.text == text and ctx.tools is live.tools
                and asked == live.messages[:-1] and live.error is None
                and not (live.done and not live.chunks)):
            # Already asked: the final confirming the interim it was started on, with
            # nothing written since. Restarting would throw the head start away -- unless
            # the stream it has failed or came back empty, which take() would only miss.
            return True
        await self.cancel("superseded")
        snapshot = asked + [{"role": "user", "content": text}]
        request = LLMContext(list(snapshot), tools=ctx.tools, tool_choice=ctx.tool_choice)
        spec = _Speculation(snapshot, ctx.tools, text)
        spec.task = self._spawn(spec.run(self._llm, request))
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
        self._release(spec)

    async def close(self):
        """The session is ending: stop whatever is live and wait for every teardown.
        Awaited, unlike cancel(): the loop is about to go away with the session, and a
        detached close would be the "Task was destroyed but it is pending" it leaves."""
        await self.cancel("session-end")
        pending, self._tasks = set(self._tasks), set()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def take(self, context: LLMContext):
        """The stream for `context` if the live speculation was asked exactly that,
        else None -- and the speculation is over either way."""
        spec, self._current = self._current, None
        if spec is None:
            return None
        live = context.messages
        if spec.done and spec.error is not None:
            reason = f"spec-failed ({type(spec.error).__name__})"
        elif spec.done and not spec.chunks:
            # A 200 with nothing in it (a router hiccup, a provider that closed the body
            # at once). Adopting it would be a turn with no reply, where the ordinary
            # request would most likely have got one.
            reason = "empty"
        elif context.tools is not spec.tools:
            reason = "tools-changed"
        elif not live or not isinstance(live[-1], dict) or live[-1].get("role") != "user":
            # Not the aggregator's commit at all: a tool-result re-run, an LLMRunFrame
            # on a context whose tail is not the caller's words. The reply it produces
            # will write the context, so the speculation is stale either way -- but the
            # journal should not call that a text or context mismatch.
            reason = "other-request"
        elif not _same_ask(live, spec.messages):
            reason = ("ctx-changed"
                      if _ends_stripped(live[-1]) == _ends_stripped(spec.messages[-1])
                      else "text-differs")
        else:
            reason = None
        if reason is not None:
            self.misses += 1
            logger.info(f"[SPEC] miss reason={reason} {spec.text[:40]!r} {self._tally()}")
            self._release(spec)
            return None
        self.hits += 1
        lead = time.monotonic() - spec.started
        logger.info(f"[SPEC] hit lead=+{lead * 1000:.0f}ms buffered={len(spec.chunks)} chunks"
                    f"{' (complete)' if spec.done else ''} {self._tally()}")
        return _AdoptedStream(spec)
