#
# A barge-in's turn is committed AFTER the reply it cut (endpointing.CutReplyFence).
#
# The failure, live 2026-10-13 (SIP): asked to count from one to twenty, the caller said
# "Okay, stop there." over the count. Playback stopped -- and the model started counting
# again. The request it was sent had the caller's two turns back to back and no count
# between them; the count was committed only afterwards, after the stop and at full
# length, so the next request showed it twice after the caller's "stop".
#
# The shape is the late-start barge-in (LateStartTurnStopStrategy, d78396d): the
# transcriber returns nothing for words spoken over the bot until the VAD stop, Smart Turn
# has answered COMPLETE by then, and the 2-word guard is met only by the final -- so the
# final opens the turn, broadcasts the interruption and commits the turn in ONE controller
# call. The interruption still has the whole pipeline to cross before the assistant
# aggregator commits the cut reply, and HeardContextCorrector, which finds that reply by
# position, finds nothing to correct.
#
# Driven here through the REAL pieces that decide the order: the LLM user aggregator with
# the brain's start and stop strategies and keep_barge_in_reachable, the real
# TranscriptLedger, HeardContextCorrector, ReplyHoldGate and assistant aggregator, with a
# scripted LLM and a TTS that plays its audio in real time. The transport's handling of
# the interruption is given the time it takes live (the bot stopped ~70 ms after the
# broadcast). Run with the fence and without it: without it, the context the model gets is
# the reported one, so the test is known to be testing the failure. The reply hold is
# checked on the way: the same order made it read the barge-in's own interruption as a
# dropped reply and request a TurnMerge that the next turn then received. And the
# speculative reply's snapshot, whose corrector pass consumed the ledger's cut on a
# context the cut reply had not reached yet (live 2026-10-13, 15 ms after the
# interruption), waits on the same fence.
#
# All utterances are synthetic.
#
# Run: python test_barge_commit_order.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.audio.turn.smart_turn.base_smart_turn import (  # noqa: E402
    BaseSmartTurn,
    SmartTurnParams,
)
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from teaport_brain.barge_pause import PauseAwareMinWordsStrategy  # noqa: E402
from teaport_brain.endpointing import (  # noqa: E402
    CUT_COMMIT_WAIT_SECS,
    CutReplyFence,
    LateStartTurnStopStrategy,
    SegmentDoneSink,
    SegmentResetFrame,
    keep_barge_in_reachable,
)
from teaport_brain.heard_context import HeardContextCorrector, _clean_prefix  # noqa: E402
from teaport_brain.reply_hold import ReplyHoldGate, TurnMerge  # noqa: E402
from teaport_brain.speculate import Speculator  # noqa: E402
from teaport_brain.stt import FinalTranscriptionFrame  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

REQUEST = "Count from one to twenty."
STOP = "Okay, stop there."
COUNT = ("One, two, three, four, five, six, seven, eight, nine, ten, eleven, twelve, "
         "thirteen, fourteen, fifteen, sixteen, seventeen, eighteen, nineteen, twenty.")
ANSWER = "Okay, stopping there."

IN_SR = 16000
OUT_SR = 24000
MIC_CHUNK = b"\x00\x02" * int(IN_SR * 0.02)       # 20 ms of caller audio
OUT_CHUNK_S = 0.1
WORD_S = 0.25                                       # each counted word's playout slot
# What the interruption's crossing costs live, put where it is paid: the bot stopped
# ~70 ms after the broadcast on 2026-10-13 (the transport drops its queued audio, the TTS
# tears down its synthesis). In the scripted pipeline the crossing is otherwise ~free.
CROSSING_S = 0.06
STARTUP_S = 0.5


class Scripted(BaseSmartTurn):
    """Smart Turn answering what the test says: COMPLETE unless told otherwise."""

    complete = True

    def _predict_endpoint(self, audio_array):
        return ({"prediction": 1, "probability": 0.98} if self.complete
                else {"prediction": 0, "probability": 0.1})


class SpecStream:
    """A speculation's stream: one chunk, then done."""

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        yield "chunk"

    async def close(self):
        pass


class SpecLLM:
    """The request-opening seam the Speculator calls: records what it was asked."""

    def __init__(self):
        self.asked = []

    async def _open_stream(self, ctx):
        self.asked.append([dict(m) for m in ctx.get_messages()])
        return SpecStream()


class ScriptedLLM(FrameProcessor):
    """Answers each context the way the LLM service streams a completion: the count for
    the request, a short answer for anything after it."""

    def __init__(self):
        super().__init__()
        self.contexts = []
        self.asked_at = []
        self._task = None
        # As the LLM service does, when the session speculates: asks the Speculator
        # for its stream first. True for each request it was adopted for.
        self.speculator = None
        self.adopted = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            self.contexts.append([dict(m) for m in frame.context.get_messages()])
            self.asked_at.append(time.monotonic())
            if self.speculator is not None:
                self.adopted.append(await self.speculator.take(frame.context) is not None)
            text = COUNT if len(self.contexts) == 1 else ANSWER
            self._task = self.create_task(self._complete(text))
            return
        if isinstance(frame, InterruptionFrame) and self._task is not None:
            await self.cancel_task(self._task)
            self._task = None
        await self.push_frame(frame, direction)

    async def _complete(self, text):
        await asyncio.sleep(0.05)
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(text))
        await self.push_frame(LLMFullResponseEndFrame())


class PacedTTS(FrameProcessor):
    """Speaks each completion as the engine TTS does -- a context, word frames stamped
    with their playout pts, then the audio -- but hands the audio over in real time, so
    the bot is still speaking when the caller cuts in. Word frames go first and all at
    once, as the engine's do: the text runs ahead of the audio, which is why pipecat
    commits a cut reply at more than was heard."""

    def __init__(self):
        super().__init__()
        self._text = []
        self._task = None
        self._n = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self._text.append(frame.text)
            return
        if isinstance(frame, LLMFullResponseEndFrame) and direction == FrameDirection.DOWNSTREAM:
            text, self._text = "".join(self._text), []
            self._n += 1
            self._task = self.create_task(self._speak(f"r{self._n}", text, frame))
            return
        if isinstance(frame, InterruptionFrame):
            self._text = []
            if self._task is not None:
                await self.cancel_task(self._task)
                self._task = None
        await self.push_frame(frame, direction)

    async def _speak(self, ctx, text, end):
        await asyncio.sleep(0.1)
        now = self.get_clock().get_time()
        await self.push_frame(TTSStartedFrame(context_id=ctx))
        words = text.split()
        for i, w in enumerate(words):
            f = TTSTextFrame(w, aggregated_by="word", context_id=ctx)
            f.pts = now + int(i * WORD_S * 1e9)
            await self.push_frame(f)
        for _ in range(round(len(words) * WORD_S / OUT_CHUNK_S)):
            await self.push_frame(TTSAudioRawFrame(b"\x01\x00" * int(OUT_SR * OUT_CHUNK_S),
                                                   OUT_SR, 1, context_id=ctx))
            await asyncio.sleep(OUT_CHUNK_S)
        await self.push_frame(end)
        await self.push_frame(TTSStoppedFrame(context_id=ctx))


class Playout(FrameProcessor):
    """Stands in for the output transport: announces the bot speaking around each
    context's audio, both ways, and takes the live time to handle an interruption."""

    def __init__(self):
        super().__init__()
        self._speaking = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSAudioRawFrame) and not self._speaking:
                self._speaking = True
                await self.push_frame(BotStartedSpeakingFrame())
                await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
            elif isinstance(frame, InterruptionFrame) and self._speaking:
                await asyncio.sleep(CROSSING_S)
            if isinstance(frame, (TTSStoppedFrame, InterruptionFrame)) and self._speaking:
                self._speaking = False
                await self.push_frame(frame, direction)
                await self.push_frame(BotStoppedSpeakingFrame())
                await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
                return
        await self.push_frame(frame, direction)


class Call:
    """One session's turn path, as agent_session builds it, minus STT and audio I/O."""

    def __init__(self, with_fence: bool, speculate: bool = False):
        self.context = LLMContext([{"role": "system", "content": "sys"}])
        self.smart_turn = Scripted(sample_rate=IN_SR, params=SmartTurnParams(stop_secs=1.0))
        self.stop = LateStartTurnStopStrategy(turn_analyzer=self.smart_turn)
        self.pair = LLMContextAggregatorPair(
            self.context,
            user_params=LLMUserAggregatorParams(
                user_mute_strategies=[],
                user_turn_strategies=UserTurnStrategies(
                    start=[PauseAwareMinWordsStrategy(min_words=2)],
                    stop=[self.stop],
                ),
            ),
        )
        user = self.pair.user()
        keep_barge_in_reachable(user._user_turn_controller)
        self.fence = CutReplyFence() if with_fence else None
        if self.fence is not None:
            self.fence.hold_commits(user)
        self.llm, self.tts = ScriptedLLM(), PacedTTS()
        self.ledger = TranscriptLedger(tts=self.tts)
        self.merge = TurnMerge()
        self.gate = ReplyHoldGate(merge=self.merge)
        if self.fence is not None:
            # As agent_session wires it: a commit with the gate still armed continues
            # the turn. None of these turns does -- each opens with an interruption --
            # so this must never fire here (the merge and wait counts below say so).
            self.fence.unplayed_reply = lambda: self.gate.armed
        self.requested = 0
        self.committed_at = []
        self.started_at = []

        @user.event_handler("on_user_turn_message_added")
        async def _arm(_a, _m):
            self.committed_at.append(time.monotonic())
            await self.gate.arm()

        @user.event_handler("on_user_turn_started")
        async def _started(_a, _s):
            self.started_at.append(time.monotonic())

        def merge_request(orig=self.merge.request):
            self.requested += 1
            orig()
        self.merge.request = merge_request

        self.corrector = HeardContextCorrector(self.ledger, self.context, merge=self.merge)
        # As agent_session wires it.
        self.spec_llm = SpecLLM()
        self.speculator = Speculator(
            llm=self.spec_llm, aggregator=user,
            before_snapshot=self.corrector.reconcile_for_snapshot,
            cut_fence=self.fence) if speculate else None
        self.stop.speculator = self.llm.speculator = self.speculator

        head = self.fence.head if self.fence is not None else None
        tail = self.fence.tail if self.fence is not None else None
        self.task = PipelineTask(Pipeline([p for p in [
            user, head, SegmentDoneSink(), self.corrector,
            self.llm, self.tts, self.gate, Playout(), self.pair.assistant(), tail,
        ] if p is not None]), observers=[self.ledger], cancel_on_idle_timeout=False)

    async def utterance(self, text, stop_n, late=True, speech_s=0.6, flush_s=0.2):
        """The VAD start, the audio, the VAD stop (Smart Turn: COMPLETE), and the
        final ~0.2 s later. `late`: the transcriber returns nothing until the VAD stop
        has flushed the segment -- the late-start shape, every barge-in's; otherwise an
        interim opens the turn while the caller is still speaking."""
        await self.task.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        for i in range(round(speech_s / 0.02)):
            await self.task.queue_frame(
                InputAudioRawFrame(audio=MIC_CHUNK, sample_rate=IN_SR, num_channels=1))
            await asyncio.sleep(0.02)
            if not late and i == round(speech_s / 0.04):
                await self.task.queue_frame(InterimTranscriptionFrame(
                    " ".join(text.split()[:2]), "caller", "t"))
        await self.task.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        await asyncio.sleep(flush_s)
        final = FinalTranscriptionFrame(text, "caller", "t", stop_n=stop_n)
        final.finalized = True
        await self.task.queue_frame(final)

    async def incomplete_barge_in(self, text, stop_n, opened_by, speech_s=0.6,
                                  flush_s=0.2, silence_s=1.4):
        """The over-the-bot stop under an INCOMPLETE verdict, as the speculative reply
        sees it: the words land ~0.2 s after the VAD stop, opening the turn -- on their
        last interim with the final ~10 ms behind it (`opened_by` "interim"), or on the
        final itself -- and the analyzer's silence ceiling commits the turn later. The
        line stays open, as it does live, for the ceiling to count."""
        self.smart_turn.complete = False
        await self.task.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        for _ in range(round(speech_s / 0.02)):
            await self.task.queue_frame(
                InputAudioRawFrame(audio=MIC_CHUNK, sample_rate=IN_SR, num_channels=1))
            await asyncio.sleep(0.02)
        await self.task.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.5))
        words_at = round(flush_s / 0.02)
        for i in range(round(silence_s / 0.02)):
            if i == words_at:
                if opened_by == "interim":
                    await self.task.queue_frame(InterimTranscriptionFrame(text, "caller", "t"))
                    await asyncio.sleep(0.01)
                final = FinalTranscriptionFrame(text, "caller", "t", stop_n=stop_n)
                final.finalized = True
                await self.task.queue_frame(final)
            await self.task.queue_frame(
                InputAudioRawFrame(audio=MIC_CHUNK, sample_rate=IN_SR, num_channels=1))
            await asyncio.sleep(0.02)


async def _the_call(with_fence: bool, late_request: bool = False,
                    speculate_opened_by: str | None = None):
    call = Call(with_fence, speculate=speculate_opened_by is not None)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(call.task))
    await asyncio.sleep(STARTUP_S)
    # The request, with the bot silent: one word is a turn, and the count begins. As
    # live, its interim opens the turn while the caller speaks -- unless `late_request`.
    await call.utterance(REQUEST, stop_n=1, late=late_request)
    await asyncio.sleep(1.8)          # with the utterance below, ~2.5 s of the count plays
    # "Okay, stop there." over the count.
    if speculate_opened_by is None:
        await call.utterance(STOP, stop_n=2)
    else:
        await call.incomplete_barge_in(STOP, stop_n=2, opened_by=speculate_opened_by)
    await asyncio.sleep(1.0)
    await call.task.cancel()
    await run
    return call


def _assistant_text(m):
    return m.get("content") if isinstance(m.get("content"), str) else ""


def _check_the_fixed_order(call):
    asked = call.llm.contexts
    assert len(asked) == 2, f"expected the request and the barge-in to ask once each: {asked!r}"
    roles = [(m["role"], m.get("content")) for m in asked[1]]
    assert [r for r, _ in roles] == ["system", "user", "assistant", "user"], (
        f"the barge-in's request must carry the cut reply between the two turns: {roles!r}")
    assert roles[1][1] == REQUEST and roles[3][1] == STOP, roles
    heard = roles[2][1]
    cut = [u for u in call.ledger.events if u.speaker == "assistant" and u.interrupted]
    assert len(cut) == 1, f"the ledger must chart one cut reply: {call.ledger.events!r}"
    assert heard == _clean_prefix(cut[0].heard_text), (
        f"the cut reply must be what was heard ({cut[0].heard_text!r}), got {heard!r}")
    assert heard and COUNT.startswith(heard) and len(heard) < len(COUNT) - 20, (
        f"the heard prefix must be a real, partial count: {heard!r}")
    # Nothing of the count after the caller's words: not now, and not later.
    final = [(m["role"], _assistant_text(m)) for m in call.context.get_messages()]
    after = final[final.index(("user", STOP)) + 1:]
    assert not any("twenty" in t or t == heard for _, t in after), (
        f"the cut reply was committed again after the barge-in: {after!r}")
    assert sum(1 for r, t in final if r == "assistant" and t.startswith("One,")) == 1, final
    # The reply hold saw the interruption before the commit armed it: no merge.
    assert call.requested == 0 and not call.merge.pending, (
        "the barge-in requested a TurnMerge: the gate saw its own commit before its "
        "own interruption")


def _opened_to_committed(call, turn=-1):
    return call.committed_at[turn] - call.started_at[turn]


async def test_the_barge_in_turn_is_committed_after_the_reply_it_cut():
    call = await _the_call(with_fence=True)
    _check_the_fixed_order(call)
    assert call.fence.waits == 1 and call.fence.timeouts == 0, (call.fence.waits,
                                                                call.fence.timeouts)
    # What the order costs: the commit waits for the interruption's crossing, no more
    # (no timeout above; the bound itself is the only ceiling a loaded machine cannot
    # stretch past).
    wait = _opened_to_committed(call)
    print(f"    barge-in turn: opened -> committed in {wait * 1000:.0f} ms "
          f"(the interruption's crossing: {CROSSING_S * 1000:.0f} ms)")
    assert CROSSING_S - 0.01 <= wait < CUT_COMMIT_WAIT_SECS, wait


async def test_without_the_fence_the_turn_overtakes_the_cut_reply():
    # The brain as it was, and the context the model was sent live: the caller's two
    # turns back to back, the count committed after them at full length -- and a
    # TurnMerge requested by the barge-in, for whichever turn comes next.
    call = await _the_call(with_fence=False)
    print(f"    without the fence: opened -> committed in "
          f"{_opened_to_committed(call) * 1000:.0f} ms")
    asked = call.llm.contexts
    assert len(asked) == 2, asked
    roles = [(m["role"], m.get("content")) for m in asked[1]]
    assert roles == [("system", "sys"), ("user", REQUEST), ("user", STOP)], (
        f"expected the caller's two turns back to back, no count between: {roles!r}")
    final = [(m["role"], _assistant_text(m)) for m in call.context.get_messages()]
    after = final[final.index(("user", STOP)) + 1:]
    assert ("assistant", COUNT) in after, (
        f"expected the full count committed after the barge-in: {final!r}")
    assert call.requested == 1 and call.merge.pending, "expected the false TurnMerge"


async def test_a_late_start_with_the_bot_silent_merges_nothing():
    # The same shape with nothing playing: the request's own final opens its turn after
    # the VAD stop. Its interruption cuts nothing, and costs next to nothing to wait
    # for (no audio to tear down) -- but without the fence it too reached the reply
    # hold after the commit had armed it, and the merge it asked for folded the
    # caller's NEXT turn into this one.
    call = await _the_call(with_fence=True, late_request=True)
    _check_the_fixed_order(call)
    assert call.fence.waits == 2, call.fence.waits
    print(f"    request turn, bot silent: opened -> committed in "
          f"{_opened_to_committed(call, 0) * 1000:.0f} ms")

    bare = await _the_call(with_fence=False, late_request=True)
    users = [m["content"] for m in bare.llm.contexts[-1] if m["role"] == "user"]
    assert users == [f"{REQUEST} {STOP}"], (
        f"without the fence the request's false merge should swallow the stop: {users!r}")


async def test_a_speculation_waits_for_the_reply_its_turn_cut():
    # The barge-in's words land under an INCOMPLETE verdict, the turn opened by their
    # last interim and the final ~10 ms behind it: the final asks for a speculation
    # while the interruption is still crossing. Its snapshot runs the corrector, which
    # used to consume the ledger's cut right then, on a context without the cut reply
    # -- which then landed whole, and the request at the ceiling carried every word
    # of the count (live 2026-10-13). Now the snapshot waits for the cut reply, the
    # request carries the heard prefix, and it is the speculation's.
    call = await _the_call(with_fence=True, speculate_opened_by="interim")
    _check_the_fixed_order(call)
    assert call.spec_llm.asked == [call.llm.contexts[1]], (call.spec_llm.asked,
                                                          call.llm.contexts)
    assert call.llm.adopted == [False, True], call.llm.adopted


async def test_without_the_fence_the_speculation_spends_the_cut():
    # The same call with nothing to wait on (the brain before the fence reached the
    # speculation): the snapshot lacks the count, the corrector's cut is spent on it,
    # and the count reaches the model whole -- not adopted, and not corrected either.
    call = await _the_call(with_fence=False, speculate_opened_by="interim")
    asked = call.llm.contexts
    assert len(asked) == 2, asked
    roles = [(m["role"], m.get("content")) for m in asked[1]]
    assert roles == [("system", "sys"), ("user", REQUEST), ("assistant", COUNT),
                     ("user", STOP)], f"expected the count whole in the request: {roles!r}"
    assert call.spec_llm.asked[0][-2:] == [{"role": "user", "content": REQUEST},
                                           {"role": "user", "content": STOP}], (
        call.spec_llm.asked)
    assert call.llm.adopted == [False, False], call.llm.adopted


async def test_the_final_that_opens_the_turn_over_the_bot_speculates():
    # The barge-in's final opening the turn itself under an INCOMPLETE verdict: it used
    # to be refused a speculation (its snapshot would have missed the cut reply), a
    # second check for what the fence now covers. It waits like the rest, and hits.
    call = await _the_call(with_fence=True, speculate_opened_by="final")
    _check_the_fixed_order(call)
    assert call.spec_llm.asked == [call.llm.contexts[1]], call.spec_llm.asked
    assert call.llm.adopted == [False, True], call.llm.adopted


class _Aggregator:
    """What the fence and the Speculator touch on the user aggregator."""

    def __init__(self, words=()):
        self._aggregation = list(words)
        self.context = LLMContext([{"role": "system", "content": "sys"}])
        self._user_turn_controller = type("Ctl", (), {"_user_turn": True})()
        self.pushed = 0

    async def push_aggregation(self):
        self.pushed += 1
        self._aggregation = []

    async def _maybe_emit_user_turn_stopped(self, strategy=None, on_session_end=False):
        await self.push_aggregation()


async def test_the_session_end_commit_never_drops_the_reply_ahead_of_the_end():
    # pipecat pushes the EndFrame on and THEN commits an open turn's leftover words
    # (_maybe_emit_user_turn_stopped(on_session_end=True)), through the fenced
    # push_aggregation. With the gate still armed the fence used to take that commit for
    # a continuation and send its interruption, which overtook the End and dropped the
    # reply queued ahead of it -- a goodbye -- for words nothing would answer
    # (formal/UserTurn.tla, ut_retryMergesAtEnd_nolostgoodbye).
    from pipecat.frames.frames import EndFrame

    class Tap(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.seen = []

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if direction == FrameDirection.DOWNSTREAM:
                self.seen.append(type(frame).__name__)
            await self.push_frame(frame, direction)

    from pipecat.turns.user_stop.base_user_turn_stop_strategy import (
        BaseUserTurnStopStrategy,
    )

    class NoStopYet(BaseUserTurnStopStrategy):
        """The turn is still open when the session ends."""

    context = LLMContext([{"role": "system", "content": "sys"}])
    pair = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
        user_mute_strategies=[],
        user_turn_strategies=UserTurnStrategies(
            start=[PauseAwareMinWordsStrategy(min_words=2)], stop=[NoStopYet()])))
    user = pair.user()
    fence = CutReplyFence()
    fence.hold_commits(user)
    asked = []
    fence.unplayed_reply = lambda: asked.append(1) or True     # the gate, still armed
    tap = Tap()
    task = PipelineTask(Pipeline([user, fence.head, tap, fence.tail]),
                        cancel_on_idle_timeout=False)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(STARTUP_S)
    final = FinalTranscriptionFrame("and one more thing", "caller", "t", stop_n=1)
    final.finalized = True
    await task.queue_frame(final)                  # opens a turn; no stop yet
    await asyncio.sleep(0.1)
    assert tap.seen.count("InterruptionFrame") == 1, tap.seen     # the turn start's
    await task.queue_frame(EndFrame())
    await asyncio.wait_for(run, timeout=5)
    assert tap.seen.count("InterruptionFrame") == 1, (
        f"the session-end commit sent an interruption past the End: {tap.seen}")
    assert any(m.get("content") == "and one more thing" for m in context.get_messages()), (
        "the leftover words were not committed")


async def test_a_commit_with_nothing_in_flight_does_not_wait():
    # The request above opened its turn the same way, with the bot silent: its
    # interruption crossed too, and cut nothing. Only what is in flight is waited on.
    fence = CutReplyFence()
    t = time.monotonic()
    assert await fence.wait_for_cuts()
    assert fence.waits == 0 and time.monotonic() - t < 0.005


async def test_an_empty_commit_does_not_wait():
    # pipecat's push_aggregation commits nothing for an empty aggregation, and the
    # session's end (EndFrame, a hangup's CancelFrame) calls it regardless: it must not
    # sit out an interruption's crossing for that.
    fence = CutReplyFence(timeout=0.2)
    agg = _Aggregator()
    fence.hold_commits(agg)
    fence._sent(InterruptionFrame())
    t = time.monotonic()
    await agg.push_aggregation()
    assert fence.waits == 0 and agg.pushed == 1 and time.monotonic() - t < 0.1
    agg._aggregation = ["words"]
    await agg.push_aggregation()
    assert fence.waits == 1 and fence.timeouts == 1 and agg.pushed == 2


async def test_a_lost_interruption_costs_one_bounded_wait():
    fence = CutReplyFence(timeout=0.1)
    fence._sent(InterruptionFrame())          # swallowed somewhere: never lands
    t = time.monotonic()
    assert not await fence.wait_for_cuts()
    assert 0.09 <= time.monotonic() - t < 0.3 and fence.timeouts == 1
    # Forgotten: the next commit does not pay for it again.
    t = time.monotonic()
    assert await fence.wait_for_cuts()
    assert time.monotonic() - t < 0.005 and fence.waits == 1


async def test_a_wait_sharing_a_forgotten_frame_gives_up_with_it():
    # Two waits on one lost frame (a commit and a snapshot): when the first times out
    # and forgets it, the second ends with it and reports it lost, rather than reading
    # the frame as landed.
    fence = CutReplyFence(timeout=0.2)
    fence._sent(InterruptionFrame())
    first = asyncio.create_task(fence.wait_for_cuts())
    await asyncio.sleep(0.1)
    second = asyncio.create_task(fence.wait_for_cuts())
    assert await first is False
    for _ in range(5):
        await asyncio.sleep(0)
    assert second.done(), "the second wait sat out its own bound on a forgotten frame"
    assert second.result() is False and fence.timeouts == 2


async def test_a_speculation_whose_wait_timed_out_is_not_started():
    # The cut reply may still land after a snapshot taken now, with the corrector's cut
    # spent: no speculation (so none to adopt), and the corrector is not run.
    fence = CutReplyFence(timeout=0.1)
    fence._sent(InterruptionFrame())
    reconciled = []
    spec = Speculator(llm=SpecLLM(), aggregator=_Aggregator(),
                      before_snapshot=lambda: reconciled.append(1), cut_fence=fence)
    assert await spec.start(STOP) is False
    assert spec.text is None and not reconciled and fence.timeouts == 1


async def test_the_session_places_the_fence_around_the_aggregators():
    # The head must be called INSIDE the user aggregator's push (directly below it), the
    # tail must see what the assistant aggregator has handled (directly below it, last).
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMAssistantAggregator,
        LLMUserAggregator,
    )
    from teaport_brain.agent_session import build_agent_session
    from teaport_brain.endpointing import _CutFenceHead, _CutFenceTail
    from test_service_unusable import _Transport

    session = build_agent_session(_Transport())
    p = session.tts
    while not isinstance(p, LLMUserAggregator):
        p = p.previous
    head = p.next
    assert isinstance(head, _CutFenceHead), f"below the user aggregator: {head!r}"
    assert p.push_aggregation.__name__ == "_push_aggregation", "the commit is not fenced"
    while not isinstance(p, LLMAssistantAggregator):
        p = p.next
    assert isinstance(p.next, _CutFenceTail), f"below the assistant aggregator: {p.next!r}"
    assert type(p.next.next).__name__ == "PipelineSink", f"the tail must be last: {p.next.next!r}"
    assert p.next._fence is head._fence, "head and tail of two different fences"



async def _waiting_start(text=STOP):
    """A Speculator whose start() is held at the fence by an interruption in flight:
    (speculator, its LLM, the frame to land, the waiting start)."""
    fence = CutReplyFence()
    frame = InterruptionFrame()
    fence._sent(frame)
    llm, agg = SpecLLM(), _Aggregator()
    spec = Speculator(llm=llm, aggregator=agg, cut_fence=fence)
    waiting = asyncio.create_task(spec.start(text))
    await asyncio.sleep(0.02)
    assert not waiting.done(), "start() should be waiting for the cut reply"
    return spec, llm, agg, fence, frame, waiting


async def _settled(spec):
    for _ in range(5):
        await asyncio.sleep(0)
    await spec.close()


async def test_a_cancel_during_the_wait_starts_nothing():
    # The caller resumed while the cut reply was landing: the VAD start's cancel ran on
    # the other task with nothing live to cancel. The waiting start must not go ahead
    # on the words it had.
    spec, llm, _agg, fence, frame, waiting = await _waiting_start()
    await spec.cancel("resumed")
    fence._landed(frame)
    assert await waiting is False
    await _settled(spec)
    assert spec.text is None and llm.asked == [], llm.asked


async def test_a_turn_committed_during_the_wait_starts_nothing():
    # The ceiling committed the turn while the snapshot waited: nothing cancels at a
    # commit (the speculation would be the commit's to take), so the turn is asked.
    spec, llm, agg, fence, frame, waiting = await _waiting_start()
    agg._user_turn_controller._user_turn = False
    fence._landed(frame)
    assert await waiting is False
    await _settled(spec)
    assert spec.text is None and llm.asked == [], llm.asked


async def test_a_newer_start_supersedes_one_still_waiting():
    # A start held on the input task, and the next final's start on the frame task:
    # only the newer words are asked, once.
    newer = "Okay, stop there. Thanks."
    spec, llm, _agg, fence, frame, older = await _waiting_start()
    newest = asyncio.create_task(spec.start(newer))
    await asyncio.sleep(0.02)
    fence._landed(frame)
    assert await older is False and await newest is True
    await _settled(spec)
    assert [m[-1]["content"] for m in llm.asked] == [newer], llm.asked


async def test_changed_or_dropped_words_during_the_wait_start_nothing():
    # The settle window's start held at the fence, then on the frame task the interim
    # changes (or the segment resets). Nothing is live, so the strategy used to cancel
    # nothing, and the waiting start went ahead on the words it had.
    for change in ("interim", "reset"):
        spec, llm, _agg, fence, frame, waiting = await _waiting_start()
        strategy = LateStartTurnStopStrategy(
            turn_analyzer=Scripted(sample_rate=IN_SR, params=SmartTurnParams(stop_secs=1.0)),
            speculator=spec)
        strategy._interim = STOP
        if change == "interim":
            await strategy._handle_interim(InterimTranscriptionFrame(STOP + " there", "u", "t"))
        else:
            await strategy._handle_segment_reset(SegmentResetFrame(stop_n=0))
        fence._landed(frame)
        assert await waiting is False, change
        await _settled(spec)
        assert spec.text is None and llm.asked == [], (change, llm.asked)


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
