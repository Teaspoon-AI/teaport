#
# Unit tests: the speculative reply (teaport_brain.speculate, TEAPORT_SPECULATIVE_REPLY).
#
# Two halves. The Speculator itself, against the real BoundedOpenAILLMService with its
# stream-opening seam faked: a hit hands the buffered-then-live chunks to the service's
# ordinary path and opens no second request; a context that changed under the snapshot,
# a text that grew, a cancelled, failed or empty speculation each fall back to a fresh
# request; a stale stream's teardown never sits on the turn's path; the session's end
# closes what is live. Then the trigger, against a real UserTurnController:
# LateStartTurnStopStrategy asks for a speculation on a final the turn did not conclude
# on (INCOMPLETE verdict) -- at the final when the VAD stop came first, at the VAD stop
# when the final did -- not on one it did (COMPLETE), not on the final that opened the
# turn over the bot, and cancels it when the caller resumes or a new turn opens.
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
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.services.openai.llm import OpenAILLMService  # noqa: E402
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_turn_controller import UserTurnController  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from turn_harness import running_controller  # noqa: E402

from teaport_brain.endpointing import (  # noqa: E402
    ENDPOINT_STOP_SECS,
    INTERRUPT_MIN_WORDS,
    SMARTTURN_STOP_SECS,
    LateStartTurnStopStrategy,
)
from teaport_brain.services import BoundedOpenAILLMService  # noqa: E402
from teaport_brain.speculate import Speculator  # noqa: E402

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
    """The two things the Speculator reads off the user aggregator, and the controller
    flag it reaches for."""

    def __init__(self, context, parts=(), turn_open=True):
        self.context = context
        self.parts = list(parts)
        self._user_turn_controller = SimpleNamespace(_user_turn=turn_open)

    def aggregation_string(self):
        return " ".join(self.parts)


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
    assert await spec.start("test")
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
    await spec.start()
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
    await spec.start()
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
    await spec.start()
    await _settle()
    ctx.add_message({"role": "user", "content": "how would a DGX compare"})
    await llm.get_chat_completions(ctx)
    assert len(opened) == 2 and spec.misses == 1


async def test_cancel_releases_the_stream_and_the_commit_asks_fresh():
    ctx, agg, llm, spec, opened = _rig()
    await spec.start()
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
    await spec.start()
    await _settle()
    first = spec._current._stream
    agg.parts.append("there")
    await spec.start()
    await _settle()
    assert first.closed and len(opened) == 2 and spec.misses == 1
    assert spec._current._stream is not first
    ctx.add_message({"role": "user", "content": "hello there"})
    out = await llm.get_chat_completions(ctx)
    assert len(opened) == 2 and spec.hits == 1
    await out.close()


async def test_a_failed_speculation_falls_back_to_a_fresh_request():
    ctx, agg, llm, spec, opened = _rig(fail=True)
    await spec.start()
    await _settle()
    assert spec._current.error is not None
    ctx.add_message({"role": "user", "content": "what time is it"})
    llm._open_stream = _llm(opened)._open_stream  # the provider is back
    out = await llm.get_chat_completions(ctx)
    assert len(opened) == 2 and spec.misses == 1 and out is not None


async def test_no_speculation_without_an_open_turn_or_text():
    ctx, agg, llm, spec, opened = _rig(turn_open=False)
    assert not await spec.start()
    ctx2, agg2, llm2, spec2, opened2 = _rig(parts=())
    assert not await spec2.start()
    assert not opened and not opened2


async def test_a_speculation_in_flight_is_adopted_before_its_first_chunk():
    """The commit can come before the provider has sent anything (the ceiling is only
    ~0.2 s behind the final on the appliance): the adopted stream waits, then flows."""
    ctx, agg, llm, spec, opened = _rig()
    await spec.start()
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
    await spec.start()
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
    await spec.start()
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
    await spec.start()
    await _settle()
    first = spec._current._stream
    await asyncio.wait_for(spec.cancel("resumed"), timeout=0.5)
    assert not first.closed, "the close is still pending -- and cancel() has returned"

    await spec.start()
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
    await spec.start()
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


# ---------------------------------------------------------------- the trigger

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)
UTTERANCE = "tell me a story"


class Complete(BaseSmartTurn):
    def _predict_endpoint(self, audio_array):
        return {"prediction": 1, "probability": 0.98}


class Incomplete(BaseSmartTurn):
    def _predict_endpoint(self, audio_array):
        return {"prediction": 0, "probability": 0.02}


class RecordingSpeculator:
    """Records the strategy's calls, with the real Speculator's one piece of state: a
    cancel with nothing live is a no-op, so only cancels that end something count."""

    def __init__(self):
        self.calls = []
        self.live = False

    async def start(self, why=""):
        self.calls.append(("start", why))
        self.live = True
        return True

    async def cancel(self, reason):
        if self.live:
            self.calls.append(("cancel", reason))
            self.live = False

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
            self.inferences += 1

        async def on_stopped(_c, _s, _p):
            self.stopped = True

        self.controller.add_event_handler("on_user_turn_inference_triggered", on_inference)
        self.controller.add_event_handler("on_user_turn_stopped", on_stopped)

    async def audio(self, ms):
        for _ in range(ms // CHUNK_MS):
            await self.controller.process_frame(
                InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1))

    async def utterance(self, text=UTTERANCE):
        """Ordinary turn: speech, an interim that opens the turn, the VAD stop, the final."""
        c = self.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await self.audio(400)
        await c.process_frame(InterimTranscriptionFrame(text, "u", "t", None))
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        await self.audio(200)
        final = TranscriptionFrame(text, "u", "t", None)
        final.finalized = True
        await c.process_frame(final)

    async def settle(self, secs):
        deadline = asyncio.get_running_loop().time() + secs
        while asyncio.get_running_loop().time() < deadline and not self.stopped:
            await self.audio(CHUNK_MS)
            await asyncio.sleep(CHUNK_MS / 1000)


async def test_an_incomplete_verdict_speculates_on_the_final_and_the_ceiling_commits():
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.utterance()
        assert rig.inferences == 0, "INCOMPLETE: the final must not commit the turn"
        assert rig.speculator.calls == [("start", "verdict INCOMPLETE, ceiling running")]
        await rig.settle(SMARTTURN_STOP_SECS + 0.5)
        assert rig.stopped and rig.inferences == 1, "the ceiling still ends the turn"
        assert rig.speculator.calls == [("start", "verdict INCOMPLETE, ceiling running")], \
            "the commit adopts the speculation through the LLM service, not the strategy"


async def test_a_complete_verdict_commits_on_the_final_and_speculates_nothing():
    rig = Rig(Complete)
    async with running_controller(rig.controller):
        await rig.utterance()
        assert rig.inferences == 1, "COMPLETE: the final commits the turn"
        assert rig.speculator.calls == [], "the real request follows at once"


async def test_the_caller_resuming_cancels_the_speculation():
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.utterance()
        await rig.controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        assert rig.speculator.calls[-1] == ("cancel", "resumed")


async def test_a_final_while_the_caller_is_still_audible_speculates_nothing():
    """The engine closed a segment on its own (or the backstop did) mid-speech: more
    text is coming, so the request would be wasted."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        c = rig.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await c.process_frame(InterimTranscriptionFrame(UTTERANCE, "u", "t", None))
        final = TranscriptionFrame(UTTERANCE, "u", "t", None)
        final.finalized = True
        await c.process_frame(final)
        assert rig.speculator.calls == []


async def test_a_final_that_beat_the_vad_stop_speculates_at_the_stop():
    """The engine's segmenter finalizes on shorter silences than the VAD floor, so on
    the appliance the final usually lands BEFORE the VAD stop. The verdict is only
    reached at the stop; an INCOMPLETE one then has a finalized transcript in hand and
    the ceiling running -- the exact wait the feature is for. This was silently inert
    (no speculation, no [SPEC] line) before the trigger was added at the stop."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        c = rig.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await c.process_frame(InterimTranscriptionFrame(UTTERANCE, "u", "t", None))
        final = TranscriptionFrame(UTTERANCE, "u", "t", None)
        final.finalized = True
        await c.process_frame(final)
        assert rig.speculator.calls == [], "still audible: nothing yet"
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        assert rig.inferences == 0
        assert rig.speculator.calls == [("start", "final before the VAD stop, verdict INCOMPLETE")]
        await rig.settle(SMARTTURN_STOP_SECS + 0.5)
        assert rig.stopped and rig.inferences == 1, "the ceiling still ends the turn"


async def test_a_final_that_beat_a_complete_verdict_commits_at_the_stop_and_speculates_nothing():
    rig = Rig(Complete)
    async with running_controller(rig.controller):
        c = rig.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(400)
        await c.process_frame(InterimTranscriptionFrame(UTTERANCE, "u", "t", None))
        final = TranscriptionFrame(UTTERANCE, "u", "t", None)
        final.finalized = True
        await c.process_frame(final)
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        assert rig.inferences == 1, "COMPLETE at the stop, final in hand: commits now"
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
        await c.process_frame(InterimTranscriptionFrame(interim, "u", "t", None))
    final = TranscriptionFrame(final_text, "u", "t", None)
    final.finalized = True
    await c.process_frame(final)


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
        assert rig.speculator.calls == []


async def test_a_final_that_opens_the_turn_with_the_bot_quiet_speculates():
    """The same shape with nothing to interrupt: the context is stable, so the
    speculation is worth having."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await _barge_in(rig, None, "Okay, stop.")
        assert rig.controller._user_turn
        assert rig.speculator.calls == [("start", "verdict INCOMPLETE, ceiling running")]


async def test_the_next_final_after_the_opening_one_speculates():
    """The skip is for the one frame that opened the turn; a second final on the same
    turn (the caller went on) is the ordinary case again."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        c = rig.controller
        await c.process_frame(BotStartedSpeakingFrame())
        await _barge_in(rig, "Okay", "Okay, stop.")
        assert rig.speculator.calls == []
        final = TranscriptionFrame("Okay, stop. And tell me a story.", "u", "t", None)
        final.finalized = True
        await c.process_frame(final)
        assert rig.speculator.calls == [("start", "verdict INCOMPLETE, ceiling running")]


async def test_the_session_end_closes_the_speculator():
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.utterance()
        assert rig.speculator.live
    assert rig.speculator.calls[-2:] == [("cancel", "session-end"), ("close",)]
    assert not rig.speculator.live


async def test_a_new_turn_cancels_a_speculation_left_from_the_last():
    """A turn that opens from a transcript with no VAD start before it (speech under
    the VAD gate) is the one shape the resume hook does not cover."""
    rig = Rig(Incomplete)
    async with running_controller(rig.controller):
        await rig.utterance()
        await rig.settle(SMARTTURN_STOP_SECS + 0.5)
        assert rig.stopped and rig.speculator.live
        await rig.controller.process_frame(
            InterimTranscriptionFrame("and another", "u", "t", None))
        assert rig.speculator.calls[-1] == ("cancel", "new-turn")


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
