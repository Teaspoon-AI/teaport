#
# Unit tests: the speculative reply (teaport_brain.speculate, TEAPORT_SPECULATIVE_REPLY).
#
# Two halves. The Speculator itself, against the real BoundedOpenAILLMService with its
# stream-opening seam faked: a hit hands the buffered-then-live chunks to the service's
# ordinary path and opens no second request; a context that changed under the snapshot,
# a text that grew, a cancelled or failed speculation each fall back to a fresh request.
# Then the trigger, against a real UserTurnController: LateStartTurnStopStrategy asks
# for a speculation on a final the turn did not conclude on (INCOMPLETE verdict), not on
# one it did (COMPLETE), and cancels it when the caller resumes or a new turn opens.
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
from pipecat.frames.frames import (  # noqa: E402
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

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.closed = False

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


def _llm(opened, fail=False):
    """A real BoundedOpenAILLMService whose request-opening seam records and fakes."""
    llm = BoundedOpenAILLMService(
        api_key="test", base_url="http://127.0.0.1:9/v1",
        settings=OpenAILLMService.Settings(model="m"),
    )

    async def open_stream(context):
        opened.append(context)
        if fail:
            raise RuntimeError("provider down")
        return FakeStream()

    llm._open_stream = open_stream
    return llm


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def _drain(stream, n):
    it = stream.__aiter__()
    return [await it.__anext__() for _ in range(n)], it


def _rig(parts=("what time is it",), turn_open=True, fail=False):
    opened = []
    ctx = LLMContext([{"role": "system", "content": "be brief"}])
    agg = FakeAggregator(ctx, parts, turn_open=turn_open)
    llm = _llm(opened, fail=fail)
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
