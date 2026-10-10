#
# Unit tests: the speculative reply (teaport_brain.speculate, TEAPORT_SPECULATIVE_REPLY).
#
# Three parts. The Speculator itself, against the real BoundedOpenAILLMService with its
# stream-opening seam faked: a hit hands the buffered-then-live chunks to the service's
# ordinary path and opens no second request; a context that changed under the snapshot,
# a text that grew or differs by a word, a comma or a capital, a cancelled, failed or
# empty speculation each fall back to a fresh request, while whitespace at the ends of
# the user message does not; a start for what is already being asked keeps its stream;
# the candidate is what the REAL user aggregator writes at the commit; a stale stream's
# teardown never sits on the turn's path; the session's end closes what is live.
#
# Then the trigger, against a real UserTurnController: LateStartTurnStopStrategy opens a
# speculation on the SETTLED INTERIM of a turn under an INCOMPLETE verdict -- not before
# the settle window, not on a COMPLETE verdict, not with the bot still speaking -- cancels
# and re-arms when the interim changes, cancels when the caller resumes or a new turn
# opens, and closes at the session's end. The final-triggered paths that the verdict
# hold still produces (the over-the-bot flush, the engine's own close before the stop,
# a final under the wait) keep asking, a final that confirms the interim keeps its
# stream, and the final that opens the turn over the bot does not ask.
#
# Then the whole path over a real pipeline -- the real aggregator, the real strategy,
# the real Speculator -- to the commit: the ceiling's final equal to the interim (modulo
# the engine's leading space) is a hit; one that differs in its punctuation is a miss.
#
# Run: python test_speculative_reply.py  (or via pytest test_suite.py)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from types import SimpleNamespace  # noqa: E402

from pipecat.audio.turn.smart_turn.base_smart_turn import (  # noqa: E402
    BaseSmartTurn,
    SmartTurnParams,
)
from loguru import logger  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMContextFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
    LLMUserAggregator,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.services.openai.llm import OpenAILLMService  # noqa: E402
from pipecat.utils.string import TextPartForConcatenation  # noqa: E402
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_turn_controller import UserTurnController  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from stt_harness import WireRecorder  # noqa: E402
from turn_harness import running_controller  # noqa: E402

from teaport_brain.endpointing import (  # noqa: E402
    ENDPOINT_STOP_SECS,
    INTERRUPT_MIN_WORDS,
    SMARTTURN_STOP_SECS,
    LateStartTurnStopStrategy,
    SegmentDoneFrame,
    SegmentResetFrame,
    TurnVerdictFrame,
)
from teaport_brain.services import BoundedOpenAILLMService  # noqa: E402
from teaport_brain.speculate import Speculator  # noqa: E402
from teaport_brain.stt import TeaportSTTService  # noqa: E402

# ---------------------------------------------------------------- the Speculator

_END = object()


class FakeStream:
    """What _open_stream hands back: chunks fed by the test, closed by the consumer."""

    def __init__(self, close_gate: asyncio.Event | None = None):
        self.q: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.close_gate = close_gate  # when set, close() blocks until it is released

    def feed(self, *chunks):
        for c in chunks:
            self.q.put_nowait(c)

    def end(self):
        self.q.put_nowait(_END)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        while True:
            item = await self.q.get()
            if item is _END:
                return
            yield item

    async def close(self):
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True


class FakeAggregator:
    """What the Speculator reads off the user aggregator -- the context, the pending
    finals -- and the controller flag it reaches for."""

    def __init__(self, context, parts=(), turn_open=True):
        self.context = context
        self.parts = list(parts)
        self._user_turn_controller = SimpleNamespace(_user_turn=turn_open)

    @property
    def _aggregation(self):
        return [TextPartForConcatenation(p, includes_inter_part_spaces=False)
                for p in self.parts]


def _llm(opened, fail=False, close_gate=None):
    """A real BoundedOpenAILLMService whose request-opening seam records and fakes."""
    llm = BoundedOpenAILLMService(
        api_key="test", base_url="http://127.0.0.1:9/v1",
        settings=OpenAILLMService.Settings(model="m"),
    )

    async def open_stream(context):
        opened.append(context)
        if fail:
            raise RuntimeError("provider down")
        return FakeStream(close_gate)

    llm._open_stream = open_stream
    return llm


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def _drain(stream, n):
    it = stream.__aiter__()
    return [await it.__anext__() for _ in range(n)], it


class _SpecLines:
    """The [SPEC] journal lines, which are the feature's only account of itself."""

    def __init__(self):
        self.lines: list[str] = []

    def __enter__(self):
        self._id = logger.add(lambda m: self.lines.append(m.record["message"]),
                              filter=lambda r: r["message"].startswith("[SPEC]"),
                              format="{message}")
        return self

    def __exit__(self, *exc):
        logger.remove(self._id)


def _rig(parts=("what time is it",), turn_open=True, fail=False, close_gate=None):
    opened = []
    ctx = LLMContext([{"role": "system", "content": "be brief"}])
    agg = FakeAggregator(ctx, parts, turn_open=turn_open)
    llm = _llm(opened, fail=fail, close_gate=close_gate)
    spec = Speculator(llm=llm, aggregator=agg)
    llm.speculator = spec
    return ctx, agg, llm, spec, opened


async def test_a_hit_hands_over_the_buffered_then_live_stream_and_opens_nothing_new():
    ctx, agg, llm, spec, opened = _rig()
    assert await spec.start(spec.candidate(), "test")
    await _settle()
    assert len(opened) == 1, "the speculation opens the request"
    # The speculation was asked against the snapshot: system + the candidate user turn.
    assert opened[0].messages[-1] == {"role": "user", "content": "what time is it"}
    assert ctx.messages == [{"role": "system", "content": "be brief"}], \
        "the live context is never written by the speculation"
    # Two chunks arrive before the turn commits.
    live = spec._current
    stream_in = live._stream
    stream_in.feed("c1", "c2")
    await _settle()
    assert live.chunks == ["c1", "c2"]

    # The commit: the aggregator writes exactly that user message and the service asks.
    ctx.add_message({"role": "user", "content": "what time is it"})
    out = await llm.get_chat_completions(ctx)
    assert len(opened) == 1, "a hit must not open a second request"
    assert spec.hits == 1 and spec.misses == 0
    got, it = await _drain(out, 2)
    assert got == ["c1", "c2"], "buffered chunks replay first, in order"
    stream_in.feed("c3")
    assert await it.__anext__() == "c3", "then the stream continues live"
    stream_in.end()
    try:
        await it.__anext__()
        raise AssertionError("the adopted stream must end when the real one does")
    except StopAsyncIteration:
        pass
    await out.close()
    assert stream_in.closed, "closing the adopted stream releases the real one"


async def test_a_context_written_after_the_snapshot_is_a_miss():
    """A memory note (or a follow-up, or the heard-context truncation) landing between
    the snapshot and the commit makes the speculated reply stale: fresh request."""
    ctx, agg, llm, spec, opened = _rig()
    await spec.start(spec.candidate())
    await _settle()
    real = spec._current._stream
    ctx.add_message({"role": "system", "content": "You remember: the user likes tea"})
    ctx.add_message({"role": "user", "content": "what time is it"})
    out = await llm.get_chat_completions(ctx)
    assert len(opened) == 2, "a miss opens the ordinary request"
    assert opened[1] is ctx
    assert spec.misses == 1 and spec.hits == 0
    await _settle()
    assert real.closed, "the stale speculation's stream is released"
    assert out is not None


async def test_the_before_snapshot_hook_puts_its_write_inside_the_snapshot():
    """HeardContextCorrector rewrites a cut reply on the LLMContextFrame, i.e. at the
    commit -- after a snapshot taken at the final. Live 2026-09-16 that was the one
    ctx-changed miss. Wired as before_snapshot, its rewrite is what the model is asked
    against, and the same rewrite at the commit changes nothing: a hit."""
    ctx, agg, llm, spec, opened = _rig()
    ctx.add_message({"role": "assistant", "content": "a long reply that was cut"})

    def reconcile():
        # Idempotent, like the corrector: truncate once, then nothing to do.
        last = ctx.messages[-1]
        if last["role"] == "assistant" and last["content"] != "a long":
            last["content"] = "a long"

    spec._before_snapshot = reconcile
    await spec.start(spec.candidate())
    await _settle()
    assert opened[0].messages[1] == {"role": "assistant", "content": "a long"}, \
        "the speculation was asked against the reconciled context"
    reconcile()  # the corrector runs again at the commit, as it does live
    ctx.add_message({"role": "user", "content": "what time is it"})
    out = await llm.get_chat_completions(ctx)
    assert spec.hits == 1 and len(opened) == 1
    await out.close()


async def test_text_that_grew_is_a_miss():
    ctx, agg, llm, spec, opened = _rig(parts=("how would a DGX",))
    await spec.start(spec.candidate())
    await _settle()
    ctx.add_message({"role": "user", "content": "how would a DGX compare"})
    await llm.get_chat_completions(ctx)
    assert len(opened) == 2 and spec.misses == 1


async def test_cancel_releases_the_stream_and_the_commit_asks_fresh():
    ctx, agg, llm, spec, opened = _rig()
    await spec.start(spec.candidate())
    await _settle()
    real = spec._current._stream
    await spec.cancel("resumed")
    await _settle()
    assert real.closed and spec.misses == 1
    ctx.add_message({"role": "user", "content": "what time is it"})
    await llm.get_chat_completions(ctx)
    assert len(opened) == 2, "nothing left to adopt: the ordinary request"
    assert spec.misses == 1, "a cancelled speculation is counted once"


async def test_a_new_speculation_supersedes_the_old_one():
    ctx, agg, llm, spec, opened = _rig(parts=("hello",))
    await spec.start(spec.candidate())
    await _settle()
    first = spec._current._stream
    agg.parts.append("there")
    await spec.start(spec.candidate())
    await _settle()
    assert first.closed and len(opened) == 2 and spec.misses == 1
    assert spec._current._stream is not first
    ctx.add_message({"role": "user", "content": "hello there"})
    out = await llm.get_chat_completions(ctx)
    assert len(opened) == 2 and spec.hits == 1
    await out.close()


async def test_a_failed_speculation_falls_back_to_a_fresh_request():
    ctx, agg, llm, spec, opened = _rig(fail=True)
    await spec.start(spec.candidate())
    await _settle()
    assert spec._current.error is not None
    ctx.add_message({"role": "user", "content": "what time is it"})
    llm._open_stream = _llm(opened)._open_stream  # the provider is back
    out = await llm.get_chat_completions(ctx)
    assert len(opened) == 2 and spec.misses == 1 and out is not None


async def test_no_speculation_without_an_open_turn_or_text():
    ctx, agg, llm, spec, opened = _rig(turn_open=False)
    assert not await spec.start(spec.candidate())
    ctx2, agg2, llm2, spec2, opened2 = _rig(parts=())
    assert not await spec2.start(spec2.candidate())
    assert not opened and not opened2


async def test_a_speculation_in_flight_is_adopted_before_its_first_chunk():
    """The commit can come before the provider has sent anything (the ceiling is only
    ~0.2 s behind the final on the appliance): the adopted stream waits, then flows."""
    ctx, agg, llm, spec, opened = _rig()
    await spec.start(spec.candidate())
    await _settle()
    real = spec._current._stream
    ctx.add_message({"role": "user", "content": "what time is it"})
    out = await llm.get_chat_completions(ctx)
    assert spec.hits == 1
    it = out.__aiter__()
    first = asyncio.ensure_future(it.__anext__())
    await _settle()
    assert not first.done(), "nothing to yield yet"
    real.feed("late")
    assert await first == "late"
    await out.close()


async def test_an_empty_completed_speculation_is_a_miss():
    """A 200 whose body closed at once: nothing buffered, no error. Adopting it would be
    a turn that says nothing; the ordinary request is made instead."""
    ctx, agg, llm, spec, opened = _rig()
    await spec.start(spec.candidate())
    await _settle()
    spec._current._stream.end()
    await _settle()
    assert spec._current.done and spec._current.chunks == [] and spec._current.error is None
    ctx.add_message({"role": "user", "content": "what time is it"})
    with _SpecLines() as journal:
        out = await llm.get_chat_completions(ctx)
    assert len(opened) == 2 and spec.misses == 1 and out is not None
    assert any("reason=empty" in line for line in journal.lines), journal.lines


async def test_a_request_that_is_not_the_commit_is_named_as_such():
    """A tool-result re-run (or an LLMRunFrame on a context whose tail is not the
    caller's words) while a speculation is live: the speculation is over -- the reply
    will write the context -- but the journal must not call it a text mismatch."""
    ctx, agg, llm, spec, opened = _rig()
    await spec.start(spec.candidate())
    await _settle()
    ctx.add_message({"role": "tool", "tool_call_id": "c1", "content": "{}"})
    with _SpecLines() as journal:
        out = await llm.get_chat_completions(ctx)
    assert len(opened) == 2 and spec.misses == 1 and out is not None
    assert any("reason=other-request" in line for line in journal.lines), journal.lines
    assert not any("text-differs" in line for line in journal.lines)


async def test_a_stale_stream_is_released_off_the_turn_path():
    """The provider is slow to close its socket. A cancel (the aggregator's VAD-start
    handling) and a miss (ahead of the ordinary request) both return without waiting
    for it; the close completes on its own."""
    gate = asyncio.Event()
    ctx, agg, llm, spec, opened = _rig(close_gate=gate)
    await spec.start(spec.candidate())
    await _settle()
    first = spec._current._stream
    await asyncio.wait_for(spec.cancel("resumed"), timeout=0.5)
    assert not first.closed, "the close is still pending -- and cancel() has returned"

    await spec.start(spec.candidate())
    await _settle()
    second = spec._current._stream
    ctx.add_message({"role": "user", "content": "what time is it, please"})
    out = await asyncio.wait_for(llm.get_chat_completions(ctx), timeout=0.5)
    assert out is not None and len(opened) == 3, "the ordinary request was opened"
    assert not second.closed, "without waiting on the stale stream's teardown"

    gate.set()
    await _settle()
    assert first.closed and second.closed, "both closes completed in the background"


async def test_close_at_session_end_stops_a_live_speculation_and_waits_for_its_teardown():
    gate = asyncio.Event()
    ctx, agg, llm, spec, opened = _rig(close_gate=gate)
    await spec.start(spec.candidate())
    await _settle()
    live = spec._current
    stream, reader = live._stream, live.task
    closing = asyncio.ensure_future(spec.close())
    await _settle()
    assert not closing.done(), "close() waits for the HTTP teardown, unlike cancel()"
    assert reader.done(), "but the reader is already stopped"
    gate.set()
    await asyncio.wait_for(closing, timeout=0.5)
    assert stream.closed and spec._current is None and spec.misses == 1
    assert not spec._tasks, "nothing is left pending for the loop to complain about"
    with _SpecLines() as journal:
        await spec.close()
    assert journal.lines == [], "a second close has nothing to do and says nothing"


async def test_whitespace_at_the_ends_of_the_user_message_is_forgiven():
    """The interim carries the engine's leading space; the final is stripped. The words
    are the same, so the reply is the reply to them: a hit, whichever side the space
    is on."""
    for asked, committed in ((" what time is it", "what time is it"),
                             ("what time is it", " what time is it \n")):
        ctx, agg, llm, spec, opened = _rig(parts=())
        assert await spec.start(asked)
        await _settle()
        ctx.add_message({"role": "user", "content": committed})
        out = await llm.get_chat_completions(ctx)
        assert spec.hits == 1 and len(opened) == 1, (asked, committed)
        await out.close()


async def test_a_word_a_comma_or_a_capital_that_differs_is_a_miss():
    """The 2026-10-09 measurement says the interim equals the final; the check does not
    take that on trust. Any difference past whitespace is other words, and the reply
    was generated for the ones asked: the ordinary request is made."""
    for asked, committed in (("what time is it", "what time is it?"),
                             ("what time is it", "What time is it"),
                             ("so what time", "so, what time"),
                             ("what time is it", "what time it is")):
        ctx, agg, llm, spec, opened = _rig(parts=())
        await spec.start(asked)
        await _settle()
        ctx.add_message({"role": "user", "content": committed})
        with _SpecLines() as journal:
            await llm.get_chat_completions(ctx)
        assert len(opened) == 2 and spec.misses == 1 and spec.hits == 0, (asked, committed)
        assert any("reason=text-differs" in line for line in journal.lines), journal.lines


async def test_a_start_for_what_is_already_asked_keeps_the_stream():
    """The final confirming the interim a speculation was started on: restarting would
    throw the head start away. A context written in between is a new snapshot."""
    ctx, agg, llm, spec, opened = _rig(parts=())
    await spec.start("tell me a story")
    await _settle()
    first = spec._current
    assert await spec.start("tell me a story")
    assert spec._current is first and len(opened) == 1 and spec.misses == 0
    ctx.add_message({"role": "system", "content": "You remember: the user likes tea"})
    await spec.start("tell me a story")
    await _settle()
    assert spec._current is not first and len(opened) == 2 and spec.misses == 1, \
        "the context moved: the old snapshot would miss at the commit"
    await spec.close()
    # Nor is a stream kept that has nothing to give: failed, or ended empty.
    ctx, agg, llm, spec, opened = _rig(parts=(), fail=True)
    await spec.start("tell me a story")
    await _settle()
    assert spec._current.error is not None
    llm._open_stream = _llm(opened)._open_stream  # the provider is back
    await spec.start("tell me a story")
    await _settle()
    assert spec._current.error is None and len(opened) == 2 and spec.misses == 1
    spec._current._stream.end()
    await _settle()
    await spec.start("tell me a story")
    await _settle()
    assert len(opened) == 3 and spec.misses == 2, "an empty stream is asked again"
    await spec.close()


_NOTE = {"role": "system", "content": "You remember these things about the user: tea"}
_FRESHER = {"role": "system", "content": "You remember these things about the user: coffee"}


async def test_memory_recalls_pending_note_is_in_the_snapshot_and_the_commit_hits():
    """MemoryRecall injects its note on the FINAL -- after a snapshot taken on the
    interim. Its pending note goes into the snapshot where the final will put it, so the
    commit that carries it is the context that was asked: a hit."""
    pending = [_NOTE]
    ctx, agg, llm, spec, opened = _rig(parts=())
    spec._pending = lambda: list(pending)
    await spec.start("what do I like")
    await _settle()
    assert opened[0].messages == [{"role": "system", "content": "be brief"}, _NOTE,
                                  {"role": "user", "content": "what do I like"}]
    assert ctx.messages == [{"role": "system", "content": "be brief"}], \
        "the note is the final's to inject, not the snapshot's"
    ctx.add_message(dict(_NOTE))                     # the final's injection
    pending.clear()                                  # ... which spends it
    assert await spec.start("what do I like"), "the final confirms: the stream is kept"
    assert len(opened) == 1
    ctx.add_message({"role": "user", "content": "what do I like"})
    out = await llm.get_chat_completions(ctx)
    assert spec.hits == 1 and spec.misses == 0 and len(opened) == 1
    await out.close()


async def test_a_note_that_changes_after_the_snapshot_is_a_miss():
    """A fresher search lands between the snapshot and the final: the final injects a
    note the speculation was not asked with. Whole-context equality makes that a miss."""
    ctx, agg, llm, spec, opened = _rig(parts=())
    spec._pending = lambda: [_NOTE]
    await spec.start("what do I like")
    await _settle()
    ctx.add_message(dict(_FRESHER))
    ctx.add_message({"role": "user", "content": "what do I like"})
    with _SpecLines() as journal:
        await llm.get_chat_completions(ctx)
    assert len(opened) == 2 and spec.misses == 1 and spec.hits == 0
    assert any("reason=ctx-changed" in line for line in journal.lines), journal.lines
    # And a note that never came (the search was cancelled at the final) is a miss too.
    ctx, agg, llm, spec, opened = _rig(parts=())
    spec._pending = lambda: [_NOTE]
    await spec.start("what do I like")
    await _settle()
    ctx.add_message({"role": "user", "content": "what do I like"})
    await llm.get_chat_completions(ctx)
    assert len(opened) == 2 and spec.misses == 1 and spec.hits == 0


async def test_the_candidate_is_what_the_real_aggregator_writes():
    """The candidate is built as push_aggregation builds the user message: the pending
    finals, then the interim as the segment's final -- joined by pipecat's own
    concatenation, with the final's includes_inter_frame_spaces. Checked against the
    real LLMUserAggregator taking the real finals."""
    agg = LLMUserAggregator(LLMContext([{"role": "system", "content": "s"}]))
    spec = Speculator(llm=_llm([]), aggregator=agg)
    assert spec.candidate(" so I was thinking") == "so I was thinking"
    await agg._handle_transcription(TranscriptionFrame("So I was", "u", "t"))
    interim = " thinking about tea, maybe."           # as the STT pushes it
    candidate = spec.candidate(interim)
    await agg._handle_transcription(TranscriptionFrame(interim.strip(), "u", "t"))
    assert candidate == agg.aggregation_string() == "So I was thinking about tea, maybe."


# ---------------------------------------------------------------- the trigger

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)
UTTERANCE = "tell me a story"
SETTLE = 0.06   # the window, shortened so the suite stays quick


class Complete(BaseSmartTurn):
    def _predict_endpoint(self, audio_array):
        return {"prediction": 1, "probability": 0.98}


class Incomplete(BaseSmartTurn):
    def _predict_endpoint(self, audio_array):
        return {"prediction": 0, "probability": 0.02}


class RecordingSpeculator:
    """Records the strategy's calls, with the real Speculator's state: the pending
    finals the candidate is built on, the one live speculation's words, a start for the
    words already asked kept, a cancel with nothing live a no-op."""

    def __init__(self):
        self.calls = []
        self.live = None
        self.finals = []
        self.settle_secs = SETTLE

    def candidate(self, interim=""):
        return " ".join(self.finals + ([interim.strip()] if interim.strip() else []))

    @property
    def text(self):
        return self.live

    async def start(self, text, why=""):
        if text == self.live:
            return True
        await self.cancel("superseded")
        self.calls.append(("start", why, text))
        self.live = text
        return True

    async def cancel(self, reason):
        if self.live is not None:
            self.calls.append(("cancel", reason))
            self.live = None

    async def close(self):
        await self.cancel("session-end")
        self.calls.append(("close",))


class Rig:
    def __init__(self, analyzer_cls):
        analyzer = analyzer_cls(sample_rate=SAMPLE_RATE,
                                params=SmartTurnParams(stop_secs=SMARTTURN_STOP_SECS))
        analyzer.set_sample_rate(SAMPLE_RATE)
        self.speculator = RecordingSpeculator()
        self.strategy = LateStartTurnStopStrategy(turn_analyzer=analyzer,
                                                  speculator=self.speculator)
        self.controller = UserTurnController(
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[self.strategy],
            ),
            user_turn_stop_timeout=3600,
        )
        self.inferences = 0
        self.stopped = False

        async def ignore(*args, **kwargs):
            pass

        for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                      "on_user_turn_started", "on_user_turn_stop_timeout"):
            self.controller.add_event_handler(event, ignore)

        async def on_inference(_c, _s):
            # The aggregator's push_aggregation: the pending finals are spent.
            self.inferences += 1
            self.speculator.finals.clear()

        async def on_stopped(_c, _s, _p):
            self.stopped = True

        self.controller.add_event_handler("on_user_turn_inference_triggered", on_inference)
        self.controller.add_event_handler("on_user_turn_stopped", on_stopped)

    async def audio(self, ms):
        for _ in range(ms // CHUNK_MS):
            await self.controller.process_frame(
                InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1))

    async def interim(self, text):
        await self.controller.process_frame(InterimTranscriptionFrame(text, "u", "t", None))

    async def final(self, text):
        # The aggregator takes the final's words before the strategies see the frame.
        self.speculator.finals.append(text.strip())
        await self.controller.process_frame(
            TranscriptionFrame(text, "u", "t", None, finalized=True))

    async def speech(self, interim=" " + UTTERANCE):
        """The caller speaks; an interim opens the turn; they stop. Under the verdict
        hold no final follows until the ceiling's commit."""
        c = self.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await self.audio(400)
        await self.interim(interim)
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))

    async def wait(self, secs):
        """Real time passes with silence flowing, as it does under the ceiling."""
        deadline = asyncio.get_running_loop().time() + secs
        while asyncio.get_running_loop().time() < deadline and not self.stopped:
            await self.audio(CHUNK_MS)
            await asyncio.sleep(CHUNK_MS / 1000)


START = "interim settled, verdict INCOMPLETE"


async def test_an_incomplete_verdict_speculates_on_the_settled_interim():
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.speech()
        assert rig.inferences == 0, "INCOMPLETE: nothing commits at the stop"
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.calls == [("start", START, UTTERANCE)], \
            "the interim, its leading space dropped, once the window has passed"
        # The ceiling, then the final its commit brings: the turn ends on the final, and
        # the commit is where the LLM service adopts the stream (not the strategy).
        await rig.wait(SMARTTURN_STOP_SECS + 0.1)
        assert rig.inferences == 0, "the ceiling waits for the final the commit returns"
        await rig.final(UTTERANCE)
        assert rig.stopped and rig.inferences == 1
        assert rig.speculator.calls == [("start", START, UTTERANCE)], \
            "the final confirming the interim keeps the speculation"


async def test_nothing_is_asked_before_the_interim_has_settled():
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.speech()
        await asyncio.sleep(SETTLE / 3)
        await rig.interim(" " + UTTERANCE + " about")   # a lagging delta
        await asyncio.sleep(SETTLE * 2 / 3)
        assert rig.speculator.calls == [], "the window restarts at each change"
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.calls == [("start", START, UTTERANCE + " about")]


async def test_the_window_counts_from_the_interim_not_the_stop():
    """Words that stopped changing before the VAD stop are settled already: the window
    that was running before the stop is not restarted by it."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        c = rig.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await rig.interim(" " + UTTERANCE)
        await asyncio.sleep(SETTLE + 0.02)              # still "speaking" per the VAD
        assert rig.speculator.calls == [], "the caller has not stopped: nothing to ask"
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        await asyncio.sleep(0.01)
        assert rig.speculator.calls == [("start", START, UTTERANCE)]


async def test_a_changed_interim_supersedes_the_speculation_and_rearms():
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.speech()
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.calls == [("start", START, UTTERANCE)]
        await rig.interim(" " + UTTERANCE + " about tea")
        assert rig.speculator.calls[-1] == ("cancel", "superseded"), \
            "the words moved: stop the stream now, not at the mismatch"
        await rig.interim(" " + UTTERANCE + " about tea")    # the same words again
        assert rig.speculator.calls[-1] == ("cancel", "superseded")
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.calls[-1] == ("start", START, UTTERANCE + " about tea")
        assert len(rig.speculator.calls) == 3


async def test_the_caller_resuming_cancels_the_speculation_and_the_window():
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.speech()
        await rig.wait(SETTLE + 0.1)
        await rig.controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        assert rig.speculator.calls[-1] == ("cancel", "resumed")
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.speech()
        await rig.controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await asyncio.sleep(SETTLE + 0.05)
        assert rig.speculator.calls == [], "a resume inside the window asks nothing"


async def test_a_complete_verdict_speculates_nothing():
    """COMPLETE commits at once: the final trails the last interim by ~0.01 s, so a
    speculation would only race the real request."""
    rig = Rig(Complete)
    async with running_controller(rig.controller):
        await rig.speech()
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.calls == []
        await rig.final(UTTERANCE)
        assert rig.inferences == 1 and rig.speculator.calls == []


async def test_the_settled_interim_waits_for_the_bot_to_stop():
    """Over the bot the STT commits at the stop (the flush is the barge-in); the
    interruption has not reached the transport while the bot is still speaking, so the
    cut reply is not in the context. The barge-in's own final asks instead."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        c = rig.controller
        await c.process_frame(BotStartedSpeakingFrame())
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        await rig.interim(" okay stop there")     # two words: opens the turn
        assert c._user_turn
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.calls == [], "the bot is still speaking"
        await c.process_frame(BotStoppedSpeakingFrame())
        await rig.final("Okay, stop there.")
        assert rig.speculator.calls == [
            ("start", "verdict INCOMPLETE, ceiling running", "Okay, stop there.")]


async def test_the_session_end_closes_the_speculator():
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.speech()
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.live
    assert rig.speculator.calls[-2:] == [("cancel", "session-end"), ("close",)]
    assert rig.speculator.live is None


async def test_the_session_end_stops_a_running_window():
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.speech()
    await asyncio.sleep(SETTLE + 0.05)
    assert rig.speculator.calls == [("close",)], "nothing asked after the hangup"


async def test_a_new_turn_cancels_a_speculation_left_from_the_last():
    """A turn that opens from a transcript with no VAD start before it (speech under
    the VAD gate) is the one shape the resume hook does not cover."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.speech()
        await rig.wait(SETTLE + 0.1)
        await rig.wait(SMARTTURN_STOP_SECS + 0.1)
        await rig.final(UTTERANCE)
        assert rig.stopped and rig.speculator.live
        await rig.final("and another thing")
        assert ("cancel", "new-turn") in rig.speculator.calls


# -- the final-triggered paths the verdict hold still produces

async def test_a_final_under_the_wait_speculates_at_once():
    """A final lands with the ceiling running: the over-the-bot flush's (with the bot
    gone quiet by then), or the engine closing a segment on its own under the hold. The
    segment is closed, so there is nothing to settle."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.speech()
        await rig.final(UTTERANCE)
        assert rig.inferences == 0, "INCOMPLETE: the final must not commit the turn"
        assert rig.speculator.calls == [("start", "verdict INCOMPLETE, ceiling running",
                                         UTTERANCE)]
        await rig.wait(SETTLE + 0.1)
        assert len(rig.speculator.calls) == 1, "the cancelled window asks nothing more"
        await rig.wait(SMARTTURN_STOP_SECS + 0.5)
        assert rig.stopped and rig.inferences == 1, "the ceiling still ends the turn"


async def test_a_final_that_beat_the_vad_stop_speculates_at_the_stop():
    """The engine's segmenter finalizes on shorter silences than the VAD floor, so its
    final can land BEFORE the VAD stop. The verdict is only reached at the stop; an
    INCOMPLETE one then has a finalized transcript in hand and the ceiling running --
    the exact wait the feature is for. (The STT commits the tail at that stop, so the
    stop's own close follows at once; the turn waits for the ceiling as before.)"""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        c = rig.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await rig.interim(" " + UTTERANCE)
        await rig.final(UTTERANCE)
        assert rig.speculator.calls == [], "still audible: nothing yet"
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        assert rig.inferences == 0
        assert rig.speculator.calls == [
            ("start", "final before the VAD stop, verdict INCOMPLETE", UTTERANCE)]
        await rig.wait(SMARTTURN_STOP_SECS + 0.5)
        assert rig.stopped and rig.inferences == 1, "the ceiling still ends the turn"
        assert len(rig.speculator.calls) == 1, "no interim since the final: no window"


async def test_a_final_that_beat_a_complete_verdict_commits_at_the_stop_and_speculates_nothing():
    rig = Rig(Complete)
    async with running_controller(rig.controller):
        c = rig.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await rig.interim(" " + UTTERANCE)
        await rig.final(UTTERANCE)
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        assert rig.inferences == 1, "COMPLETE at the stop, final in hand: commits now"
        assert rig.speculator.calls == []


async def test_a_final_while_the_caller_is_still_audible_speculates_nothing():
    """The engine closed a segment on its own (or the backstop did) mid-speech: more
    text is coming, so the request would be wasted."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        c = rig.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await rig.interim(" " + UTTERANCE)
        await rig.final(UTTERANCE)
        assert rig.speculator.calls == []


async def _barge_in(rig, interim, final_text):
    """The barge-in shape on this brain: the bot is speaking, the transcriber returns
    nothing until the VAD stop flushes the segment, then the interim, then the final."""
    c = rig.controller
    await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
    await rig.audio(400)
    await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
    await rig.audio(200)
    if interim is not None:
        await rig.interim(interim)
    await rig.final(final_text)


async def test_the_final_that_opens_the_turn_over_the_bot_speculates_nothing():
    """'Okay, stop.' spoken over the bot: the interim is one word, under the guard, so
    the FINAL opens the turn -- and broadcasts the interruption -- in the same
    controller call that would speculate on it. The cut reply is not in the context
    yet, so the snapshot would be missing the truncation the commit makes: a
    guaranteed ctx-changed miss, one billed request per such barge-in."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.controller.process_frame(BotStartedSpeakingFrame())
        assert INTERRUPT_MIN_WORDS == 2
        await _barge_in(rig, "Okay", "Okay, stop.")
        assert rig.controller._user_turn, "the final opened the turn"
        assert rig.inferences == 0, "INCOMPLETE was kept across the late start"
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.calls == []


async def test_a_final_that_opens_the_turn_with_the_bot_quiet_speculates():
    """The same shape with nothing to interrupt: the context is stable, so the
    speculation is worth having."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await _barge_in(rig, None, "Okay, stop.")
        assert rig.controller._user_turn
        assert rig.speculator.calls == [("start", "verdict INCOMPLETE, ceiling running",
                                         "Okay, stop.")]


async def test_the_next_final_after_the_opening_one_speculates():
    """The skip is for the one frame that opened the turn; a second final on the same
    turn (the caller went on) is the ordinary case again."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        c = rig.controller
        await c.process_frame(BotStartedSpeakingFrame())
        await _barge_in(rig, "Okay", "Okay, stop.")
        assert rig.speculator.calls == []
        await rig.final("And tell me a story.")
        assert rig.speculator.calls == [("start", "verdict INCOMPLETE, ceiling running",
                                         "Okay, stop. And tell me a story.")]


# -- with the real STT on the other side: the verdict hold still delivers these finals

class SttRig(Rig):
    """The real STT service wired to the strategy as the pipeline wires them: the VAD
    edges reach it first, its transcripts and wordless closes go down to the
    controller, the strategy's verdicts come back up to it. The service commits on the
    verdict -- the only rule it has -- so whatever speculates here does so under it."""

    def __init__(self, analyzer_cls):
        super().__init__(analyzer_cls)
        controller = self.controller

        class Stt(TeaportSTTService):
            def __init__(self):
                super().__init__(url="ws://127.0.0.1:1/none")
                self._websocket = WireRecorder()
                self.whys = []

            async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
                if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame,
                                      SegmentDoneFrame, SegmentResetFrame)):
                    await controller.process_frame(frame)

            async def stop_processing_metrics(self):
                pass

            def create_task(self, coro, name=None):
                return asyncio.get_running_loop().create_task(coro)

            async def cancel_task(self, task, timeout=None):
                task.cancel()

            async def _send_commit(self, final=True, why="other"):
                self.whys.append(why)
                await super()._send_commit(final=final, why=why)

        self.stt = Stt()

        async def verdict_up(_c, frame, direction=FrameDirection.DOWNSTREAM):
            if isinstance(frame, TurnVerdictFrame):
                await self.stt.process_frame(frame, FrameDirection.UPSTREAM)

        controller.add_event_handler("on_push_frame", verdict_up)

    async def both(self, frame):
        await self.stt.process_frame(frame, FrameDirection.DOWNSTREAM)
        await self.controller.process_frame(frame)

    async def delta(self, text):
        await self.stt._handle_message({"type": "transcription.delta", "delta": text})

    async def done(self, text, reason):
        await self.stt._handle_message({"type": "transcription.done", "text": text,
                                        "reason": reason})


async def test_with_the_real_stt_the_flush_over_the_bot_speculates_on_its_final():
    """Over the bot the STT commits at the VAD stop -- not on the verdict -- so the
    barge-in's final lands while the strategy's INCOMPLETE stands: a final under the
    wait, asked on at once. (The interim it follows opened the turn over the bot, and
    the settled-interim trigger left it alone.)"""
    rig = SttRig(Incomplete)
    async with running_controller(rig.controller):
        await rig.both(BotStartedSpeakingFrame())
        await rig.both(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await rig.both(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        assert rig.stt.whys == ["bot-speaking"], "the flush is the barge-in"
        await rig.delta(" okay stop there")
        assert rig.controller._user_turn
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.calls == []
        await rig.both(BotStoppedSpeakingFrame())
        rig.speculator.finals.append("Okay, stop there.")   # the aggregator's take
        await rig.done("Okay, stop there.", "commit")
        assert rig.inferences == 0
        assert rig.speculator.calls == [("start", "verdict INCOMPLETE, ceiling running",
                                         "Okay, stop there.")]
        assert rig.stt.whys == ["bot-speaking"], "the INCOMPLETE verdict held nothing"


async def test_with_the_real_stt_the_engines_own_close_before_the_stop_speculates_at_it():
    """The engine's segmenter closed the utterance before the VAD stop (its own close,
    marked "vad"); the stop then commits the tail at once rather than hold it, and the
    INCOMPLETE verdict reached there has the final in hand: asked on at the stop."""
    rig = SttRig(Incomplete)
    async with running_controller(rig.controller):
        await rig.both(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await rig.delta(" " + UTTERANCE)
        rig.speculator.finals.append(UTTERANCE)
        await rig.done(UTTERANCE, "vad")
        assert rig.speculator.calls == [], "still audible"
        await rig.both(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        assert rig.stt.whys == ["tail"]
        assert rig.speculator.calls == [
            ("start", "final before the VAD stop, verdict INCOMPLETE", UTTERANCE)]


async def test_with_the_real_stt_the_held_segment_speculates_on_its_settled_interim():
    """The ordinary fallthrough under the hold: the segment stays open, the interim
    settles, and the speculation is asked on it long before the ceiling's commit."""
    rig = SttRig(Incomplete)
    async with running_controller(rig.controller):
        await rig.both(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await rig.delta(" tell me")
        await rig.both(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        await rig.delta(" a story")                      # the decoder's lagging tail
        assert rig.stt.whys == [] and rig.stt._commit_pending, "held for the ceiling"
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.calls == [("start", START, UTTERANCE)]
        assert rig.stt.whys == [], "asked while the segment is still held"


async def test_a_reconnect_voids_the_interim_it_dropped():
    """The engine session goes (a reconnect, a hold) with a segment open: its words go
    with it, and no final or wordless close will come. The STT says so with a
    SegmentResetFrame; without it the strategy kept the dead interim, and the next
    INCOMPLETE stop found it "settled" long ago and asked at once on words the turn
    will never carry."""
    rig = SttRig(Incomplete)
    async with running_controller(rig.controller):
        await rig.both(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await rig.delta(" tell me a")
        await asyncio.sleep(SETTLE + 0.02)                # "settled" well before the stop
        rig.stt._websocket = None          # the recorder has no close handshake to run
        await rig.stt._disconnect_websocket()             # the reconnect's first half
        assert rig.strategy._interim == ""
        await rig.both(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.calls == [], "nothing to ask: the words are gone"
        # The new session's words arm it as usual.
        await rig.delta(" story")
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.calls == [("start", START, "story")]


async def test_a_reset_cancels_a_speculation_asked_on_the_dropped_words():
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.speech()
        await rig.wait(SETTLE + 0.1)
        assert rig.speculator.live == UTTERANCE
        await rig.controller.process_frame(SegmentResetFrame())
        assert rig.speculator.calls[-1] == ("cancel", "segment-reset")


async def test_the_session_end_pushes_no_reset():
    rig = SttRig(Incomplete)
    pushed = []
    original = rig.stt.push_frame

    async def record(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)
        await original(frame, direction)

    rig.stt.push_frame = record
    rig.stt._ending = True                 # stop()/cancel() set it before disconnecting
    rig.stt._websocket = None
    await rig.stt._disconnect_websocket()
    assert not any(isinstance(f, SegmentResetFrame) for f in pushed)


async def test_a_turn_stop_forgets_the_interim():
    """A final the follow-mute dropped (the session's last line) never clears the
    interim; the turn's close does, so no later stop finds it settled."""
    from pipecat.turns.user_stop import UserTurnStoppedParams

    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.speech()
        assert rig.strategy._interim == UTTERANCE
        await rig.controller._trigger_user_turn_stop(
            None, UserTurnStoppedParams(enable_user_speaking_frames=True))
        assert not rig.controller._user_turn and rig.strategy._interim == ""


# ---------------------------------------------------------------- the whole path

class TakeAtCommit(FrameProcessor):
    """Stands where the LLM service does: at the commit's LLMContextFrame it asks the
    Speculator for the stream, as BoundedOpenAILLMService.get_chat_completions does
    (that half is tested against the real service above)."""

    def __init__(self, speculator):
        super().__init__()
        self.speculator = speculator
        self.taken = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            adopted = await self.speculator.take(frame.context)
            self.taken.append((frame.context.messages[-1], adopted is not None))
            if adopted is not None:
                await adopted.close()
        await self.push_frame(frame, direction)


async def _over_a_pipeline(final_text):
    """VAD start, the interim (the engine's leading space and all), the VAD stop, the
    model's INCOMPLETE, silence through the settle window and the ceiling, then the
    final the ceiling's commit returns."""
    ceiling = 0.3
    opened = []
    analyzer = Incomplete(sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=ceiling))
    strategy = LateStartTurnStopStrategy(turn_analyzer=analyzer)
    context = LLMContext([{"role": "system", "content": "be brief"}])
    pair = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_mute_strategies=[],
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[strategy],
            ),
            user_turn_stop_timeout=3600,
        ),
    )
    llm = _llm(opened)
    speculator = Speculator(llm=llm, aggregator=pair.user())
    speculator.settle_secs = SETTLE
    strategy.speculator = speculator
    commit = TakeAtCommit(speculator)
    task = PipelineTask(Pipeline([pair.user(), commit]), observers=[])
    runner = PipelineRunner(handle_sigint=False)
    running = asyncio.create_task(runner.run(task))
    agg = pair.user()

    async def silence(secs):
        deadline = asyncio.get_running_loop().time() + secs
        while asyncio.get_running_loop().time() < deadline:
            await agg.queue_frame(InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE,
                                                     num_channels=1))
            await asyncio.sleep(CHUNK_MS / 1000)

    try:
        await asyncio.sleep(0.2)
        await agg.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        for _ in range(400 // CHUNK_MS):                 # the speech the analyzer judges
            await agg.queue_frame(InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE,
                                                     num_channels=1))
        await agg.queue_frame(InterimTranscriptionFrame(" " + UTTERANCE, "u", "t", None))
        await agg.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        await silence(SETTLE + 0.1)
        assert len(opened) == 1 and speculator.text == UTTERANCE, (opened, speculator.text)
        assert opened[0].messages == [{"role": "system", "content": "be brief"},
                                      {"role": "user", "content": UTTERANCE}]
        await silence(ceiling + 0.1)
        assert not commit.taken, "the ceiling waits for the final its commit returns"
        await agg.queue_frame(TranscriptionFrame(final_text, "u", "t", None, finalized=True))
        await silence(0.1)
        assert len(commit.taken) == 1, commit.taken
        return commit.taken[0], speculator, opened
    finally:
        await task.cancel()
        await running


async def test_over_a_pipeline_the_final_equal_to_the_interim_is_a_hit():
    (message, adopted), speculator, opened = await _over_a_pipeline(UTTERANCE)
    assert message == {"role": "user", "content": UTTERANCE}
    assert adopted and speculator.hits == 1 and speculator.misses == 0
    assert len(opened) == 1, "the commit opened nothing of its own"


async def test_over_a_pipeline_a_final_with_other_punctuation_is_a_miss():
    (message, adopted), speculator, opened = await _over_a_pipeline("Tell me a story.")
    assert message == {"role": "user", "content": "Tell me a story."}
    assert not adopted and speculator.hits == 0 and speculator.misses == 1


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
