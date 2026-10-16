#
# ReplyHoldGate: a fresh reply does not start over a caller who has started talking
# again, and the turn they then finish is answered as ONE turn (reply_hold.py).
#
# The failure, from a 2026-10-05 test call: an INCOMPLETE verdict's 0.6 s ceiling
# committed the turn while the caller was mid-sentence, the reply's first audio played
# ~0.2 s later, and the caller's resumed words only interrupted it once transcribed --
# so the bot answered a stale fragment over them, then answered again. 10 of the call's
# 14 INCOMPLETE verdicts were committed that way.
#
# Two layers:
#   * the gate on its own, in a real pipeline (pipecat's queue pause and interruption
#     reset are what hold and drop the frames, so they are exercised, not mocked):
#     silence costs nothing, an unarmed reply (the greeting) is never held, a held reply
#     is dropped on words, released after the window on a cough, capped, and its word
#     times move with it;
#   * the call's shape end to end: real LLMUserAggregator with the brain's MinWords start,
#     a scripted ceiling commit, the real HeardContextCorrector and TranscriptLedger, a
#     scripted LLM+TTS and a playout recorder -- run with the gate and without it. With
#     it no stale audio plays and the model gets one merged user turn; without it, the
#     stale reply plays and the model gets the fragment and its continuation apart.
#
# All utterances are synthetic.
#
# Run: python test_reply_hold.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
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
)
from teaport_brain.agent_session import user_aggregator_params  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop.base_user_turn_stop_strategy import (  # noqa: E402
    BaseUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402
from pipecat.utils.time import nanoseconds_to_seconds  # noqa: E402

from teaport_brain.endpointing import (  # noqa: E402
    INTERRUPT_MIN_WORDS,
    CutReplyFence,
    keep_barge_in_reachable,
)
from teaport_brain.heard_context import HeardContextCorrector  # noqa: E402
from teaport_brain.reply_hold import (  # noqa: E402
    PlayoutHoldFrame,
    ReplyHoldGate,
    TurnMerge,
)
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

SR = 24000
CHUNK_S = 0.1
# Short windows so the suite stays quick; the logic does not depend on their size.
RELEASE_S = 0.4
MAX_S = 1.5
STARTUP_S = 0.5


def audio(ctx, secs=CHUNK_S):
    return TTSAudioRawFrame(b"\x01\x00" * int(SR * secs), SR, 1, context_id=ctx)


def word(text, ctx, pts_ns):
    f = TTSTextFrame(text, aggregated_by="word", context_id=ctx)
    f.pts = pts_ns
    return f


class Tap(FrameProcessor):
    """Records what passes, with when, in each direction."""

    def __init__(self):
        super().__init__()
        self.down = []   # (monotonic, frame)
        self.up = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        (self.down if direction == FrameDirection.DOWNSTREAM else self.up).append(
            (time.monotonic(), frame))
        await self.push_frame(frame, direction)

    def audio(self):
        return [(t, f) for t, f in self.down if isinstance(f, TTSAudioRawFrame)]


class GateRig:
    """[top tap] -> gate -> [bottom tap], in a running PipelineTask."""

    def __init__(self, **gate_kwargs):
        self.merge = TurnMerge()
        self.gate = ReplyHoldGate(merge=self.merge, release_secs=RELEASE_S, max_secs=MAX_S,
                                  **gate_kwargs)
        self.top, self.bottom = Tap(), Tap()
        self.task = PipelineTask(Pipeline([self.top, self.gate, self.bottom]),
                                 cancel_on_idle_timeout=False)
        self._run = None

    async def __aenter__(self):
        self._run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.sleep(STARTUP_S)
        return self

    async def __aexit__(self, *exc):
        await self.task.cancel()
        await self._run

    async def send(self, *frames):
        await self.task.queue_frames(list(frames))

    def clock_ns(self):
        return self.gate.get_clock().get_time()

    def holds_up(self):
        return [f.active for _, f in self.top.up if isinstance(f, PlayoutHoldFrame)]


def reply(ctx, rig, n=3):
    now = rig.clock_ns()
    return [LLMFullResponseStartFrame(), TTSStartedFrame(context_id=ctx),
            word("Sure,", ctx, now), audio(ctx),
            word("here", ctx, now + int(0.1e9)), audio(ctx),
            *[audio(ctx) for _ in range(n - 2)],
            LLMFullResponseEndFrame(), TTSStoppedFrame(context_id=ctx)]


async def test_a_silent_caller_gets_the_reply_at_once():
    async with GateRig() as rig:
        await rig.gate.arm()
        t0 = time.monotonic()
        await rig.send(*reply("c1", rig))
        await asyncio.sleep(0.2)
        got = rig.bottom.audio()
        assert len(got) == 3, f"the reply did not pass: {len(got)} chunks"
        assert got[0][0] - t0 < 0.1, f"a reply to a silent caller was delayed {got[0][0]-t0:.3f}s"
        assert rig.gate.holds == 0 and not rig.holds_up()
        assert not rig.gate.armed, "the reply's first audio should disarm the gate"


async def test_a_reply_already_playing_is_not_held():
    # Once the first audio has gone through, the caller talking is a barge-in, and the
    # barge-in path owns it: the gate must not freeze the rest of the reply.
    async with GateRig() as rig:
        await rig.gate.arm()
        frames = reply("c1", rig, n=4)
        await rig.send(*frames[:4])                        # start, ctx, word, first audio
        await asyncio.sleep(0.1)
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        await rig.send(*frames[4:])
        await asyncio.sleep(0.2)
        assert len(rig.bottom.audio()) == 4 and rig.gate.holds == 0


async def test_an_unarmed_reply_is_never_held():
    # The greeting, a follow-up: no user commit armed the gate, so a caller talking
    # over it is barge-in territory, not this gate's.
    async with GateRig() as rig:
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        await rig.send(*reply("g", rig))
        await asyncio.sleep(0.2)
        assert len(rig.bottom.audio()) == 3 and rig.gate.holds == 0


async def test_resumed_speech_with_words_drops_the_held_reply():
    async with GateRig() as rig:
        await rig.gate.arm()
        await rig.send(VADUserStartedSpeakingFrame())     # the caller resumes
        await asyncio.sleep(0.05)
        await rig.send(*reply("c1", rig))                  # the stale reply arrives
        await asyncio.sleep(0.5)
        assert not rig.bottom.audio(), "a held reply reached the transport"
        assert rig.gate.holding and rig.holds_up() == [True]
        await rig.send(InterruptionFrame())               # their words start a turn
        await asyncio.sleep(RELEASE_S + 0.3)
        assert not rig.bottom.audio(), "the dropped reply played after the interruption"
        assert rig.gate.dropped == 1 and rig.gate.released == 0
        assert rig.merge.pending, "the next commit must be folded into the unanswered turn"
        assert rig.holds_up() == [True, False], rig.holds_up()
        assert not rig.gate.armed, "the interruption is a new turn: nothing is armed"
        # The caller goes on talking before their turn commits: nothing to hold.
        await rig.send(VADUserStoppedSpeakingFrame(), VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        assert rig.gate.holds == 1 and not rig.gate.holding
        # The queue is live again: the next reply (to the merged turn) plays at once.
        await rig.send(VADUserStoppedSpeakingFrame())
        await asyncio.sleep(0.05)
        await rig.gate.arm()
        await rig.send(*reply("c2", rig))
        await asyncio.sleep(0.2)
        assert len(rig.bottom.audio()) == 3


async def test_a_cough_releases_the_reply_after_the_window_with_its_word_times_moved():
    async with GateRig() as rig:
        await rig.gate.arm()
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        frames = reply("c1", rig)
        pts0 = [f.pts for f in frames if isinstance(f, TTSTextFrame)]
        await rig.send(*frames)
        await asyncio.sleep(0.25)
        await rig.send(VADUserStoppedSpeakingFrame())     # a cough: no words follow
        t_stop = time.monotonic()
        await asyncio.sleep(RELEASE_S - 0.1)
        assert not rig.bottom.audio(), "released before the window was up"
        await asyncio.sleep(0.3)
        got = rig.bottom.audio()
        assert len(got) == 3, f"the held reply did not play after the cough: {len(got)}"
        waited = got[0][0] - t_stop
        assert RELEASE_S - 0.05 <= waited <= RELEASE_S + 0.2, f"released {waited:.3f}s after the stop"
        assert rig.gate.released == 1 and not rig.merge.pending
        assert not rig.gate.armed
        # The words play when the audio does, so their pts moved by the hold (~0.65 s).
        pts1 = [f.pts for _, f in rig.bottom.down if isinstance(f, TTSTextFrame)]
        moved = [nanoseconds_to_seconds(b - a) for a, b in zip(pts0, pts1)]
        assert all(0.5 <= m <= 0.9 for m in moved), f"word times moved by {moved}"


async def test_a_vad_that_never_stops_is_capped():
    async with GateRig() as rig:
        await rig.gate.arm()
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        await rig.send(*reply("c1", rig))
        await asyncio.sleep(MAX_S - 0.3)
        assert not rig.bottom.audio()
        await asyncio.sleep(0.5)
        assert len(rig.bottom.audio()) == 3, "the cap did not release the reply"


async def test_a_short_blip_before_the_reply_is_waited_out_from_its_stop():
    # The caller's noise came and went between the commit and the reply: the reply is
    # held only for what is left of the window after that stop.
    async with GateRig() as rig:
        await rig.gate.arm()
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.1)
        await rig.send(VADUserStoppedSpeakingFrame())
        t_stop = time.monotonic()
        await asyncio.sleep(0.15)
        await rig.send(*reply("c1", rig))
        await asyncio.sleep(RELEASE_S + 0.2)
        got = rig.bottom.audio()
        assert len(got) == 3
        assert got[0][0] - t_stop <= RELEASE_S + 0.15


async def test_a_tool_call_response_keeps_the_gate_armed_for_the_spoken_answer():
    async with GateRig() as rig:
        await rig.gate.arm()
        # As the LLM service does it: the calls are announced (a SystemFrame) before the
        # response's End; the in-progress frame comes later, from the call's own task.
        await rig.send(LLMFullResponseStartFrame(),
                       FunctionCallsStartedFrame(function_calls=[]),
                       LLMFullResponseEndFrame(),
                       FunctionCallInProgressFrame(function_name="f", tool_call_id="t1",
                                                   arguments={}))
        await asyncio.sleep(0.1)
        assert rig.gate.armed, "a tool-call response is not the reply"
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        await rig.send(*reply("c1", rig))
        await asyncio.sleep(0.2)
        assert not rig.bottom.audio(), "the spoken answer after the tool call was not checked"
        # And an empty response (nothing spoken, no tool) disarms.
        await rig.send(InterruptionFrame(), VADUserStoppedSpeakingFrame())
        await asyncio.sleep(0.1)
        await rig.gate.arm()
        await rig.send(LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
        await asyncio.sleep(0.1)
        assert not rig.gate.armed


async def test_the_faster_onset_holds_where_the_vad_never_starts():
    # The VAD's SPEAKING edge never came for 5 of the call's 16 barge-ins; the onset
    # test (speech_onset.py) is what catches a caller like that at a reply's start.
    async with GateRig() as rig:
        await rig.gate.arm()
        await rig.gate.on_onset(True)
        await rig.send(*reply("c1", rig))
        await asyncio.sleep(0.3)
        assert not rig.bottom.audio() and rig.gate.holding
        await rig.gate.on_onset(False)
        t_quiet = time.monotonic()
        await asyncio.sleep(RELEASE_S + 0.2)
        got = rig.bottom.audio()
        assert len(got) == 3 and got[0][0] - t_quiet >= RELEASE_S - 0.05


async def test_release_waits_for_both_signals_to_go_quiet():
    async with GateRig() as rig:
        await rig.gate.arm()
        await rig.send(VADUserStartedSpeakingFrame())
        await rig.gate.on_onset(True)
        await asyncio.sleep(0.05)
        await rig.send(*reply("c1", rig))
        await rig.send(VADUserStoppedSpeakingFrame())   # the VAD lets go first ...
        await asyncio.sleep(RELEASE_S + 0.2)
        assert not rig.bottom.audio(), "released while the onset test still hears speech"
        await rig.gate.on_onset(False)                  # ... the onset test after
        await asyncio.sleep(RELEASE_S + 0.2)
        assert len(rig.bottom.audio()) == 3


def test_onset_mixin_fires_on_a_short_run_and_lets_go_after_stop_secs():
    from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams
    from teaport_brain import speech_onset
    from teaport_brain.speech_onset import SpeechOnsetMixin

    class Scripted(VADAnalyzer):
        def __init__(self, confs):
            super().__init__(sample_rate=16000, params=VADParams(stop_secs=0.2))
            self.confs = list(confs)

        def num_frames_required(self):
            return 512

        def voice_confidence(self, buffer):
            return self.confs.pop(0) if self.confs else 0.0

    # 0.65 sits under the VAD's 0.7: this is speech the VAD alone would not start on.
    need = int(-(-speech_onset.ONSET_MIN_MS // 32))
    confs = [0.1, 0.65, 0.2] + [0.65] * (need + 3) + [0.3] * 10
    vad = type("Onset", (SpeechOnsetMixin, Scripted), {})(confs)
    vad.set_sample_rate(16000)   # what the VAD controller does at the StartFrame
    events = []

    async def listener(started):
        events.append((len(events), started, chunk[0]))

    vad.onset_listeners.append(listener)
    chunk = [0]

    async def run():
        for i in range(len(confs)):
            chunk[0] = i
            await vad.analyze_audio(b"\x00\x00" * 512)

    asyncio.run(run())
    starts = [c for _, s, c in events if s]
    stops = [c for _, s, c in events if not s]
    assert starts == [3 + need - 1], f"onset at chunk {starts}, want {3 + need - 1}"
    # stop_secs 0.2 = 7 chunks of 32 ms below the threshold.
    assert stops == [3 + need + 3 + 7 - 1], f"offset at chunk {stops}"
    assert vad._vad_state.name == "QUIET", "the VAD itself never started on 0.65"


def test_merge_only_folds_adjacent_words_of_the_caller():
    from types import SimpleNamespace

    class Ctx:
        def __init__(self, m):
            self.m = m

        def get_messages(self, *a, **k):
            return self.m

        def set_messages(self, m):
            self.m[:] = m

    from teaport_brain.followup_gate import SYSTEM_NOTICE_TAG

    def corrector(msgs):
        merge = TurnMerge()
        c = HeardContextCorrector(SimpleNamespace(events=[]), Ctx(msgs), merge=merge)
        c._reconcile()           # the fragment's commit: it is the tail user message
        return c, merge

    a = {"role": "user", "content": "so I was thinking"}
    msgs = [{"role": "system", "content": "s"}, a]
    c, merge = corrector(msgs)
    msgs.append({"role": "user", "content": "we could go"})
    merge.request()
    c._reconcile()
    assert [m["content"] for m in msgs] == ["s", "so I was thinking we could go"]
    assert c._mark == len(msgs)

    # Not without a request: two turns the caller meant as two stay two.
    a = {"role": "user", "content": "one"}
    msgs = [{"role": "system", "content": "s"}, a]
    c, merge = corrector(msgs)
    msgs.append({"role": "user", "content": "two"})
    c._reconcile()
    assert len(msgs) == 3

    # A tool call in between, or an injected order at the tail: left alone.
    a = {"role": "user", "content": "look it up"}
    msgs = [{"role": "system", "content": "s"}, a]
    c, merge = corrector(msgs)
    msgs += [{"role": "assistant", "content": None, "tool_calls": []},
             {"role": "tool", "tool_call_id": "t", "content": "{}"},
             {"role": "user", "content": "and the other one"}]
    merge.request()
    c._reconcile()
    assert len(msgs) == 5 and not merge.pending
    a = {"role": "user", "content": "hello"}
    msgs = [{"role": "system", "content": "s"}, a]
    c, merge = corrector(msgs)
    msgs.append({"role": "user", "content": f"{SYSTEM_NOTICE_TAG}\ntell them"})
    merge.request()
    c._reconcile()
    assert len(msgs) == 3
    # ... nor INTO one: an order at the tail of the previous commit is not the caller's.
    order = {"role": "user", "content": f"{SYSTEM_NOTICE_TAG}\ntell them"}
    msgs = [{"role": "system", "content": "s"}, order]
    c, merge = corrector(msgs)
    msgs.append({"role": "user", "content": "what was that"})
    merge.request()
    c._reconcile()
    assert len(msgs) == 3 and msgs[1]["content"] == order["content"]
    # Not when the newest message is not the caller's.
    a = {"role": "user", "content": "one"}
    msgs = [{"role": "system", "content": "s"}, a]
    c, merge = corrector(msgs)
    msgs.append({"role": "assistant", "content": "an answer"})
    merge.request()
    c._reconcile()
    assert len(msgs) == 3 and msgs[1]["content"] == "one"

    # A MemoryRecall note between the two (it lands just before the turn it recalled
    # for) does not split them; it stays after the merged turn.
    a = {"role": "user", "content": "so I was thinking"}
    note = {"role": "system", "content": "You remember these things"}
    msgs = [{"role": "system", "content": "s"}, a]
    c, merge = corrector(msgs)
    msgs += [note, {"role": "user", "content": "we could go"}]
    merge.request()
    c._reconcile()
    assert [m["content"] for m in msgs] == ["s", note["content"], "so I was thinking we could go"], (
        "the note moves ahead of the merged turn: the context ends on the caller's words")

    # The speculative reply's snapshot reconcile (on B's final, before B is committed)
    # must not take the merge: it belongs to B's commit.
    a = {"role": "user", "content": "so I was thinking"}
    msgs = [{"role": "system", "content": "s"}, a]
    c, merge = corrector(msgs)
    merge.request()
    c.reconcile_for_snapshot()
    msgs.append({"role": "user", "content": "we could go"})
    c._reconcile()
    assert [m["content"] for m in msgs] == ["s", "so I was thinking we could go"], msgs


def test_merge_folds_across_a_tool_round_the_drop_cancelled():
    # The dropped reply had a sync tool call in the context, and the interruption that
    # dropped it cancelled the call (pipecat writes CANCELLED over IN_PROGRESS). The
    # round did nothing: it goes with the merge, and the model decides again on the
    # whole turn. Kept, it left the fragment and its continuation apart around it
    # (formal/UserTurn.tla, ut_hold_gateNoFoldCancelled_nosplitturn). A FINISHED round
    # -- a result, the async consult's placeholder -- is the turn acted on: it stays,
    # and so do the two turns.
    from types import SimpleNamespace

    class Ctx:
        def __init__(self, m):
            self.m = m

        def get_messages(self, *a, **k):
            return self.m

        def set_messages(self, m):
            self.m[:] = m

    def call(tid, *more):
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": i, "type": "function",
                                "function": {"name": "web_search", "arguments": "{}"}}
                               for i in (tid, *more)]}

    def corrector(msgs):
        merge = TurnMerge()
        c = HeardContextCorrector(SimpleNamespace(events=[]), Ctx(msgs), merge=merge)
        c._reconcile()
        return c, merge

    a = {"role": "user", "content": "look up the trains"}
    note = {"role": "system", "content": "You remember these things"}
    msgs = [{"role": "system", "content": "s"}, a]
    c, merge = corrector(msgs)
    msgs += [call("t1", "t2"), {"role": "tool", "tool_call_id": "t1", "content": "CANCELLED"},
             {"role": "tool", "tool_call_id": "t2", "content": "CANCELLED"}, note,
             {"role": "user", "content": "to Leeds tomorrow"}]
    merge.request()
    c._reconcile()
    assert [m["content"] for m in msgs] == ["s", note["content"],
                                            "look up the trains to Leeds tomorrow"], msgs
    assert c._mark == len(msgs)

    # A finished round between them (here: one call of two settled) keeps them apart.
    for settled in ('{"status": "working_in_background"}', "IN_PROGRESS"):
        a = {"role": "user", "content": "look up the trains"}
        msgs = [{"role": "system", "content": "s"}, a]
        c, merge = corrector(msgs)
        msgs += [call("t1", "t2"),
                 {"role": "tool", "tool_call_id": "t1", "content": "CANCELLED"},
                 {"role": "tool", "tool_call_id": "t2", "content": settled},
                 {"role": "user", "content": "to Leeds tomorrow"}]
        merge.request()
        c._reconcile()
        assert len(msgs) == 6 and msgs[1]["content"] == "look up the trains", (settled, msgs)


def test_the_tts_playout_model_stands_still_through_a_hold():
    from teaport_brain.engine_tts import EngineTTSService

    async def run():
        tts = EngineTTSService(voice="af_heart")
        tts._play_end = time.monotonic() + 1.0
        await tts.process_frame(PlayoutHoldFrame(active=True), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.3)
        assert 0.95 <= tts._lead_secs() <= 1.01, f"lead ran on through the hold: {tts._lead_secs()}"
        # Audio emitted during the hold queues behind what was held: no playout gap.
        tts._ctx_audio_secs["c"] = 1.0
        tts._note_playout("c", 0.5, mid_sentence=False)
        assert tts._ctx_audio_secs["c"] == 1.0, "a hold was counted as a playout gap"
        await tts.process_frame(PlayoutHoldFrame(active=False), FrameDirection.UPSTREAM)
        assert 1.4 <= tts._lead_secs() <= 1.51, f"after the hold: {tts._lead_secs()}"

    asyncio.run(run())


async def test_the_session_puts_the_gate_right_below_the_tts_and_on_the_onset():
    # Directly below the TTS is what keeps a held reply out of the ledger (the first
    # processor below the TTS is where it charts frames); the onset listener is what
    # lets it hold before the VAD's slow SPEAKING edge.
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
    from teaport_brain.agent_session import build_agent_session
    from test_service_unusable import _Transport

    session = build_agent_session(_Transport(), reply_hold_enabled=True)
    gate = session.tts.next
    assert isinstance(gate, ReplyHoldGate), f"below the TTS: {gate!r}"
    p = session.tts
    while p is not None and not isinstance(p, LLMUserAggregator):
        p = p.previous
    vad = p._params.vad_analyzer
    assert gate.on_onset in vad.onset_listeners
    assert gate._merge is not None
    # Armed by a push that carried words, not by every inference trigger.
    arms = [h.__name__ for h in p._event_handlers["on_user_turn_message_added"].handlers]
    assert "_arm_reply_gate" in arms, arms
    assert "_arm_reply_gate" not in [
        h.__name__ for h in p._event_handlers["on_user_turn_inference_triggered"].handlers]
    # The fence asks this gate whether the last reply has played, before every commit.
    fence = p.next._fence
    assert fence.unplayed_reply is not None
    assert fence.unplayed_reply() is False
    gate._armed = True
    assert fence.unplayed_reply() is True
    # Per front-end, and off unless the front-end asks (Talk's default).
    assert not isinstance(build_agent_session(_Transport()).tts.next, ReplyHoldGate)
    from teaport_brain import reply_hold
    assert reply_hold.SIP_ENABLED is True and reply_hold.TALK_ENABLED is False


async def test_speaking_at_the_commit_holds_at_once():
    # The ceiling commits while the onset test still hears the caller (the VAD has
    # stopped under them): about 5 of the 9 holds in the call's replay start this way.
    async with GateRig() as rig:
        await rig.gate.on_onset(True)
        await asyncio.sleep(0.05)
        assert not rig.gate.holding, "unarmed: nothing to hold yet"
        await rig.gate.arm()
        assert rig.gate.holding and rig.gate.onset_only_holds == 1
        await rig.send(*reply("c1", rig))
        await asyncio.sleep(0.2)
        assert not rig.bottom.audio()
        await rig.gate.on_onset(False)
        await asyncio.sleep(RELEASE_S + 0.2)
        assert len(rig.bottom.audio()) == 3


async def test_an_onset_only_hold_has_the_shorter_cap():
    async with GateRig(onset_max_secs=0.6) as rig:
        await rig.gate.arm()
        await rig.gate.on_onset(True)                 # steady noise: never goes quiet
        await rig.send(*reply("c1", rig))
        await asyncio.sleep(0.45)
        assert not rig.bottom.audio()
        await asyncio.sleep(0.4)
        assert len(rig.bottom.audio()) == 3, "an onset-only hold must end at its own cap"
    async with GateRig(onset_max_secs=0.6) as rig:
        await rig.gate.arm()
        await rig.gate.on_onset(True)
        await asyncio.sleep(0.1)
        await rig.send(VADUserStartedSpeakingFrame())  # the VAD confirms: the full cap
        await rig.send(*reply("c1", rig))
        await asyncio.sleep(0.9)
        assert not rig.bottom.audio(), "a VAD-confirmed hold keeps the full cap"
        await asyncio.sleep(MAX_S - 0.9)
        assert len(rig.bottom.audio()) == 3


async def test_a_drop_with_an_uninterruptible_frame_in_hand_does_not_silence_the_bot():
    # The paused queue had already dequeued an uninterruptible frame when the words came:
    # pipecat then only resets the queue and leaves its task waiting on the pause. The
    # gate must lift the pause itself, or no reply ever plays again.
    async with GateRig() as rig:
        await rig.gate.arm()
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        await rig.send(FunctionCallInProgressFrame(function_name="f", tool_call_id="t",
                                                   arguments={}), *reply("c1", rig))
        await asyncio.sleep(0.3)
        await rig.send(InterruptionFrame(), VADUserStoppedSpeakingFrame())
        await asyncio.sleep(0.2)
        await rig.gate.arm()
        await rig.send(*reply("c2", rig))
        await asyncio.sleep(0.4)
        got = [f.context_id for _, f in rig.bottom.audio()]
        assert got.count("c2") == 3 and "c1" not in got, got


def _call_frames(rig):
    return [(f.tool_call_id, type(f).__name__) for _, f in rig.bottom.down
            if isinstance(f, (FunctionCallInProgressFrame, FunctionCallResultFrame))]


async def test_a_dropped_hold_drops_its_tool_round_too():
    # The held reply called a tool: its IN_PROGRESS and its result (the async consult's
    # placeholder) waited in the paused queue with the rest. Both are pipecat
    # UninterruptibleFrames, which the interruption's reset keeps: let through, they
    # would put the dropped reply's tool round in the context after the turn that
    # dropped it -- a cancelled sync call as an IN_PROGRESS nothing settles.
    # (formal/UserTurn.tla, ut_hold_gateKeepsTool_nolatetool.)
    async with GateRig() as rig:
        await rig.gate.arm()
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        await rig.send(
            FunctionCallInProgressFrame(function_name="web_search", tool_call_id="t1",
                                        arguments={}),
            FunctionCallInProgressFrame(function_name="ask_openclaw", tool_call_id="t2",
                                        arguments={}),
            FunctionCallResultFrame(function_name="ask_openclaw", tool_call_id="t2",
                                    arguments={}, result={"status": "working"}),
            *reply("c1", rig))
        await asyncio.sleep(0.3)
        assert rig.gate.holding and not _call_frames(rig)
        await rig.send(InterruptionFrame(), VADUserStoppedSpeakingFrame())
        await asyncio.sleep(0.3)
        assert not _call_frames(rig), f"the dropped reply's tool round passed: {_call_frames(rig)}"
        assert not rig.bottom.audio()
        # The next reply's tool round passes, held or not, and so does a released one's.
        await rig.gate.arm()
        await rig.send(FunctionCallInProgressFrame(function_name="web_search",
                                                   tool_call_id="t3", arguments={}),
                       *reply("c2", rig))
        await asyncio.sleep(0.3)
        assert _call_frames(rig) == [("t3", "FunctionCallInProgressFrame")], _call_frames(rig)
        await rig.gate.arm()
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        await rig.send(FunctionCallInProgressFrame(function_name="web_search",
                                                   tool_call_id="t4", arguments={}),
                       *reply("c3", rig))
        await asyncio.sleep(0.1)
        await rig.send(VADUserStoppedSpeakingFrame())          # a cough: released
        await asyncio.sleep(RELEASE_S + 0.3)
        assert ("t4", "FunctionCallInProgressFrame") in _call_frames(rig), _call_frames(rig)
        assert rig.gate.released == 1 and rig.gate.dropped == 1


async def test_a_held_result_whose_call_passed_settles_it_without_an_answer():
    # A sync tool (web_search) takes seconds: its IN_PROGRESS passed the gate before the
    # caller resumed, so the call is in the context, and its result came during the
    # hold. A drop that took the result with the rest of the held reply left the call
    # unsettled for good (nothing cancels a call that has finished): the merge could not
    # fold across it, and strict OpenAI-compatible servers refuse a context with an
    # unanswered tool call (formal/UserTurn.tla, ut_hold_gateDropsResult_noorphancall).
    # The result goes through and settles the call -- without the inference it would
    # ask for, whose reply is the one just dropped.
    from pipecat.frames.frames import FunctionCallFromLLM, LLMContextFrame

    context = LLMContext([{"role": "system", "content": "sys"},
                          {"role": "user", "content": "look up the trains"}])
    pair = LLMContextAggregatorPair(context)
    gate = ReplyHoldGate(merge=TurnMerge(), release_secs=RELEASE_S, max_secs=MAX_S)
    top = Tap()
    task = PipelineTask(Pipeline([top, gate, pair.assistant()]), cancel_on_idle_timeout=False)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(STARTUP_S)
    try:
        await gate.arm()
        await task.queue_frames([
            FunctionCallsStartedFrame(function_calls=[FunctionCallFromLLM(
                function_name="web_search", tool_call_id="t1", arguments={}, context=None)]),
            FunctionCallInProgressFrame(function_name="web_search", tool_call_id="t1",
                                        arguments={}, cancel_on_interruption=True)])
        await asyncio.sleep(0.1)
        assert context.get_messages()[-1].get("content") == "IN_PROGRESS", context.get_messages()
        await task.queue_frame(VADUserStartedSpeakingFrame())      # the caller resumes
        await asyncio.sleep(0.05)
        assert gate.holding
        await task.queue_frame(FunctionCallResultFrame(
            function_name="web_search", tool_call_id="t1", arguments={},
            result={"hits": ["the 9:14 to Leeds"]}))
        await asyncio.sleep(0.2)
        await task.queue_frames([InterruptionFrame(), VADUserStoppedSpeakingFrame()])
        await asyncio.sleep(0.3)
        tool = [m for m in context.get_messages() if m.get("role") == "tool"]
        assert len(tool) == 1 and "9:14" in str(tool[0]["content"]), (
            f"the call was left unsettled: {context.get_messages()}")
        asked = [f for _, f in top.up if isinstance(f, LLMContextFrame)]
        assert not asked, "the dropped reply's result asked the model for an answer"
        assert gate.dropped == 1
    finally:
        await task.cancel()
        await run


async def test_words_placed_after_the_release_move_with_the_held_ones():
    # The TTS keeps placing the held context's words on its pre-hold baseline after the
    # release: they move by the same wait, or they would run ahead of their audio.
    async with GateRig() as rig:
        await rig.gate.arm()
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        frames = reply("c1", rig)
        base = frames[2].pts
        await rig.send(*frames[:-2])                    # the first clause, held
        await asyncio.sleep(0.3)
        await rig.send(VADUserStoppedSpeakingFrame())
        await asyncio.sleep(RELEASE_S + 0.15)
        assert rig.gate.released == 1
        later = word("later", "c1", base + int(2e9))     # placed after the release
        end = frames[-2]
        end.pts = later.pts                               # the End: the last word's pts
        await rig.send(later, end)
        await asyncio.sleep(0.1)
        got = {getattr(f, "text", type(f).__name__): f.pts for _, f in rig.bottom.down
               if getattr(f, "pts", None)}
        moved = nanoseconds_to_seconds(got["Sure,"] - base)
        assert 0.5 <= moved <= 0.9, moved
        assert got["later"] - (base + int(2e9)) == got["Sure,"] - base, (
            "a word placed after the release must move by the held context's wait")
        assert got["LLMFullResponseEndFrame"] == got["later"], (
            "the End (no context of its own) must move with its context's words")


async def test_a_hang_up_during_a_hold_ends_at_once_and_plays_nothing():
    from pipecat.frames.frames import EndFrame
    rig = GateRig()
    await rig.__aenter__()
    await rig.gate.arm()
    await rig.send(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.05)
    await rig.send(*reply("c1", rig))
    await asyncio.sleep(0.2)
    t0 = time.monotonic()
    await rig.send(EndFrame())
    await asyncio.wait_for(rig._run, timeout=MAX_S)
    assert time.monotonic() - t0 < 0.5, f"the end waited {time.monotonic() - t0:.2f}s on the hold"
    assert not rig.bottom.audio(), "the held reply played into the closing session"
    assert rig.gate.dropped == 1 and not rig.gate.holding


async def test_a_cancelled_session_leaves_no_timer_to_release_into():
    from pipecat.frames.frames import CancelFrame
    rig = GateRig()
    await rig.__aenter__()
    await rig.gate.arm()
    await rig.send(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.05)
    await rig.send(*reply("c1", rig))
    await rig.send(VADUserStoppedSpeakingFrame())     # the release timer is running
    await asyncio.sleep(0.05)
    await rig.gate.queue_frame(CancelFrame())
    await asyncio.sleep(0.05)
    assert rig.gate._release_task is None and rig.gate._cap_task is None
    await rig.task.cancel()
    await rig._run
    await asyncio.sleep(RELEASE_S + 0.1)
    assert rig.gate.released == 0


async def test_cleanup_cancels_the_timers():
    gate = ReplyHoldGate(merge=TurnMerge(), release_secs=RELEASE_S, max_secs=MAX_S)
    gate._release_task = asyncio.get_running_loop().create_task(asyncio.sleep(10))
    gate._cap_task = asyncio.get_running_loop().create_task(asyncio.sleep(10))
    t1, t2 = gate._release_task, gate._cap_task
    try:
        await gate.cleanup()
    except Exception:  # noqa: BLE001 -- never set up; only the timers matter here
        pass
    await asyncio.sleep(0)
    assert t1.cancelled() and t2.cancelled()


async def test_a_released_reply_commits_every_word_through_the_real_transport():
    # The review's probe, kept: a held-then-released reply through the real output
    # transport (its clock releases timed frames at their pts) and the real assistant
    # aggregator. Moving only the words let the re-pushed End (pts = the last word's)
    # overtake them; the aggregator committed "Sure here" and lost the rest.
    from pipecat.frames.frames import OutputAudioRawFrame
    from pipecat.processors.aggregators.llm_context import LLMContext as Ctx
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair as Pair)
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import TransportParams

    words = "Sure here is the whole answer to your question".split()

    class StubOutput(BaseOutputTransport):
        def __init__(self):
            super().__init__(TransportParams(audio_out_enabled=True, audio_out_sample_rate=SR))

        async def start(self, frame):
            await super().start(frame)
            await self.set_transport_ready(frame)

        async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
            await asyncio.sleep(len(frame.audio) / 2 / SR)
            return True

    async def run(hold):
        context = Ctx([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}])
        pair = Pair(context)
        gate = ReplyHoldGate(merge=TurnMerge(), release_secs=0.4, max_secs=3.0)
        task = PipelineTask(Pipeline([gate, StubOutput(), pair.assistant()]),
                            cancel_on_idle_timeout=False)
        runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
        await asyncio.sleep(STARTUP_S)
        await gate.arm()
        if hold:
            await task.queue_frame(VADUserStartedSpeakingFrame())
            await asyncio.sleep(0.05)
        now = gate.get_clock().get_time()
        frames = [LLMFullResponseStartFrame(), TTSStartedFrame(context_id="c")]
        last = now
        for i, w in enumerate(words):
            f = TTSTextFrame(w, aggregated_by="word", context_id="c")
            f.pts = last = now + int(i * 0.15e9)
            frames.append(f)
        frames += [TTSAudioRawFrame(b"\x01\x00" * int(SR * 0.1), SR, 1, context_id="c")
                   for _ in range(14)]
        end = LLMFullResponseEndFrame()
        end.pts = last
        frames += [TTSStoppedFrame(context_id="c"), end]
        await task.queue_frames(frames)
        if hold:
            await asyncio.sleep(0.6)
            await task.queue_frame(VADUserStoppedSpeakingFrame())
        await asyncio.sleep(3.0)
        await task.cancel()
        await runner
        return [m.get("content") for m in context.get_messages() if m["role"] == "assistant"]

    assert await run(hold=False) == [" ".join(words)]
    assert await run(hold=True) == [" ".join(words)], (
        "a released reply must commit every word it played")


def test_the_tts_moves_the_anchor_of_a_reply_queued_behind_a_hold():
    # A reply queued behind the held one is anchored where the held one's audio ends
    # (_prev_audio_end_ns, on the pipeline clock): that end moves with the hold.
    from teaport_brain.engine_tts import EngineTTSService

    class Clock:
        t = 10_000_000_000

        def get_time(self):
            return self.t

    async def run():
        tts = EngineTTSService(voice="af_heart")
        clk = Clock()
        tts.get_clock = lambda: clk
        tts._prev_audio_end_ns = clk.t + 2_000_000_000          # 2 s still to play
        await tts.process_frame(PlayoutHoldFrame(active=True), FrameDirection.UPSTREAM)
        clk.t += 700_000_000
        await tts.process_frame(PlayoutHoldFrame(active=False), FrameDirection.UPSTREAM)
        assert tts._prev_audio_end_ns == 10_000_000_000 + 2_700_000_000
        # A context in progress: its end is anchored when it completes, plus the hold.
        tts._initial_word_timestamp = clk.t
        await tts.process_frame(PlayoutHoldFrame(active=True), FrameDirection.UPSTREAM)
        clk.t += 500_000_000
        await tts.process_frame(PlayoutHoldFrame(active=False), FrameDirection.UPSTREAM)
        assert tts._ctx_held_ns == 500_000_000

    asyncio.run(run())


async def test_the_shift_starts_at_the_first_audio_not_the_context_open():
    # Review N1: the engine opens a TTS context (TTSStartedFrame) up to its first-audio
    # latency before the first audio, and anchors the words at that audio. A shift
    # fixed on the TTSStartedFrame overshot every word by that latency.
    async with GateRig() as rig:
        await rig.gate.arm()
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        await rig.send(LLMFullResponseStartFrame(), TTSStartedFrame(context_id="c1"))
        await asyncio.sleep(0.25)                      # the engine's first-audio latency
        base = rig.clock_ns()
        t_first = time.monotonic()
        end = LLMFullResponseEndFrame()
        end.pts = base + int(0.1e9)
        await rig.send(word("Sure,", "c1", base), audio("c1"),
                       word("here", "c1", base + int(0.1e9)), audio("c1"),
                       TTSStoppedFrame(context_id="c1"), end)
        await asyncio.sleep(0.2)
        await rig.send(VADUserStoppedSpeakingFrame())
        await asyncio.sleep(RELEASE_S + 0.3)
        out = [(t, f) for t, f in rig.bottom.down if isinstance(f, TTSTextFrame)]
        t_release = rig.bottom.audio()[0][0]
        applied = nanoseconds_to_seconds(out[0][1].pts - base)
        correct = t_release - t_first
        assert abs(applied - correct) < 0.05, (
            f"applied {applied:.3f}s, the first audio waited {correct:.3f}s")


async def test_no_hold_starts_behind_the_end_of_the_session():
    from pipecat.frames.frames import EndFrame
    rig = GateRig()
    await rig.__aenter__()
    await rig.gate.arm()
    await rig.send(LLMFullResponseStartFrame())           # something ahead of the End
    await rig.gate.queue_frame(EndFrame())                # the End reaches the gate
    await rig.send(VADUserStartedSpeakingFrame())         # the caller speaks after it
    await asyncio.sleep(0.05)
    assert rig.gate.holds == 0 and not rig.gate.holding, "a hold behind the End"
    await asyncio.wait_for(rig._run, timeout=2.0)


async def test_an_empty_commit_does_not_arm_the_gate():
    # on_user_turn_message_added fires only for a push with words in it; the inference
    # trigger also fires for an empty aggregation, which asks for no reply.
    context = LLMContext([{"role": "system", "content": "sys"}])
    stop = ScriptedStop()
    pair = LLMContextAggregatorPair(
        context,
        user_params=user_aggregator_params(
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[stop])))
    gate = ReplyHoldGate(merge=TurnMerge(), release_secs=RELEASE_S, max_secs=MAX_S)

    @pair.user().event_handler("on_user_turn_message_added")
    async def _arm(_a, _m):
        await gate.arm()

    task = PipelineTask(Pipeline([pair.user(), gate, pair.assistant()]),
                        cancel_on_idle_timeout=False)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(STARTUP_S)
    try:
        await task.queue_frame(InterimTranscriptionFrame("hello", "u", "t"))   # opens a turn
        await asyncio.sleep(0.1)
        await stop.commit()                                # nothing aggregated
        await asyncio.sleep(0.1)
        assert not gate.armed, "an empty commit armed the gate"
        await task.queue_frame(InterimTranscriptionFrame("so then", "u", "t"))
        await asyncio.sleep(0.1)       # the turn's start flushes what is queued behind it
        await task.queue_frame(TranscriptionFrame("so then", "u", "t"))
        await asyncio.sleep(0.1)
        await stop.commit()
        await asyncio.sleep(0.1)
        assert gate.armed, "a commit with words must arm it"
    finally:
        await task.cancel()
        await run


def test_the_tts_adds_the_hold_to_the_end_of_the_context_in_progress():
    from teaport_brain.engine_tts import EngineTTSService

    class Clock:
        t = 10_000_000_000

        def get_time(self):
            return self.t

    async def run():
        tts = EngineTTSService(voice="af_heart")
        clk = Clock()
        tts.get_clock = lambda: clk
        tts._initial_word_timestamp = base = clk.t
        tts._ctx_audio_secs["c"] = 2.0
        await tts.process_frame(PlayoutHoldFrame(active=True), FrameDirection.UPSTREAM)
        clk.t += 500_000_000
        await tts.process_frame(PlayoutHoldFrame(active=False), FrameDirection.UPSTREAM)
        await tts._maybe_reset_word_timestamps("c")
        assert tts._prev_audio_end_ns == base + 2_000_000_000 + 500_000_000, (
            "a reply queued behind this context starts where it really ends")
        assert tts._ctx_held_ns == 0

    asyncio.run(run())


class _Clock:
    t = 10_000_000_000

    def get_time(self):
        return self.t


def _tts_on(clk):
    # The clock through the processor's setup, not a patched get_clock: the TTS reads
    # the playout clock through get_clock, and so does pipecat's word baseline.
    from types import SimpleNamespace
    from teaport_brain.engine_tts import EngineTTSService
    tts = EngineTTSService(voice="af_heart")
    tts._setup = SimpleNamespace(clock=clk)
    return tts


def test_a_reply_ending_inside_a_hold_moves_by_the_hold_since_its_first_audio():
    # formal/README, PlayoutPause pp_hold_anchor: the hold is armed at the commit and
    # begins before the reply's first audio; a short reply's context ends inside it.
    # Its end (where a reply queued behind it is anchored) moved by the WHOLE hold,
    # though it was anchored at its first audio, 0.3 s into the hold.
    async def run():
        clk = _Clock()
        tts = _tts_on(clk)
        await tts.process_frame(PlayoutHoldFrame(active=True), FrameDirection.UPSTREAM)
        clk.t += 300_000_000                       # the engine's first-audio latency
        await tts.start_word_timestamps()          # the first audio: its words anchor here
        assert tts._initial_word_timestamp == 10_300_000_000
        tts._ctx_audio_secs["r"] = 1.0
        await tts._maybe_reset_word_timestamps("r")    # its whole context, inside the hold
        clk.t += 400_000_000
        await tts.process_frame(PlayoutHoldFrame(active=False), FrameDirection.UPSTREAM)
        # It plays from the release, 10.7 s, to 11.7 s.
        assert tts._prev_audio_end_ns == 11_700_000_000, (
            f"the reply's end moved to {tts._prev_audio_end_ns / 1e9:.2f}s, not 11.70s: "
            "the hold counted from before its first audio")

    asyncio.run(run())


def test_a_reply_queued_behind_a_held_one_is_anchored_on_the_frozen_schedule():
    # formal/README, PlayoutClock pc_second_asWritten: a second context (the answer
    # after a tool call) starts while the first is still held. It plays right behind the
    # first, which plays from the release; the gate moves every context it held by the
    # first one's wait (test_the_contexts_held_behind_the_first_move_with_it), so the TTS
    # must anchor it where the first ends on the pre-hold schedule -- not on the real
    # clock, which ran on while nothing played -- and its own end moves by that same wait.
    async def run():
        clk = _Clock()
        tts = _tts_on(clk)
        await tts.process_frame(PlayoutHoldFrame(active=True), FrameDirection.UPSTREAM)
        clk.t += 300_000_000
        await tts.start_word_timestamps()                  # the held reply: 10.3 s
        tts._ctx_audio_secs["r1"] = 1.0
        await tts._maybe_reset_word_timestamps("r1")       # pre-hold, it ends at 11.3 s
        clk.t = 11_800_000_000                             # still held
        await tts.start_word_timestamps()                  # the second context's first audio
        assert tts._initial_word_timestamp == 11_300_000_000, (
            f"anchored at {tts._initial_word_timestamp / 1e9:.2f}s: with the gate's shift "
            "(release - 10.3 s) its words would play that much after the first reply's end")
        clk.t = 12_000_000_000
        await tts.process_frame(PlayoutHoldFrame(active=False), FrameDirection.UPSTREAM)
        tts._ctx_audio_secs["r2"] = 0.5
        await tts._maybe_reset_word_timestamps("r2")
        # The first plays 12.0-13.0 s, the second 13.0-13.5 s.
        assert tts._prev_audio_end_ns == 13_500_000_000, (
            f"the second reply ends at {tts._prev_audio_end_ns / 1e9:.2f}s, not 13.50s")

    asyncio.run(run())


async def test_the_contexts_held_behind_the_first_move_with_it():
    # The other half of the test above, on the gate: a context held behind the first
    # plays right behind it, so its pts move by the first one's wait -- not by its own,
    # which is shorter by however long after the first it reached the gate.
    async with GateRig() as rig:
        await rig.gate.arm()
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        b1 = rig.clock_ns()
        await rig.send(TTSStartedFrame(context_id="c1"), word("Sure,", "c1", b1),
                       audio("c1"), TTSStoppedFrame(context_id="c1"))
        await asyncio.sleep(0.3)
        b2 = b1 + int(CHUNK_S * 1e9)                   # right behind c1, pre-hold
        await rig.send(TTSStartedFrame(context_id="c2"), word("Then", "c2", b2),
                       audio("c2"), TTSStoppedFrame(context_id="c2"))
        await asyncio.sleep(0.05)
        await rig.send(VADUserStoppedSpeakingFrame())
        await asyncio.sleep(RELEASE_S + 0.3)
        assert rig.gate.released == 1
        got = {f.text: f.pts for _, f in rig.bottom.down if isinstance(f, TTSTextFrame)}
        first, second = got["Sure,"] - b1, got["Then"] - b2
        assert second == first, (
            f"c1 moved {first / 1e9:.3f}s, c2 {second / 1e9:.3f}s: c2's words would run "
            "ahead of its audio")



# ---------------------------------------------------------------- the call's shape

class ScriptedStop(BaseUserTurnStopStrategy):
    """Commits when the test says so: the SMARTTURN_STOP_SECS ceiling firing on an
    INCOMPLETE verdict, as far as the aggregator can tell."""

    async def commit(self):
        await self.trigger_user_turn_stopped()


class ScriptedLLM(FrameProcessor):
    """Answers each context the way the LLM service streams a completion."""

    def __init__(self):
        super().__init__()
        self.contexts = []
        self._task = None
        self._n = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            self.contexts.append([dict(m) for m in frame.context.get_messages()])
            self._n += 1
            self._task = self.create_task(self._complete(f"r{self._n}"))
            return
        if isinstance(frame, InterruptionFrame) and self._task is not None:
            await self.cancel_task(self._task)
            self._task = None
        await self.push_frame(frame, direction)

    async def _complete(self, ctx):
        await asyncio.sleep(0.05)                     # first token
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(f"Reply {ctx} goes here."))
        await self.push_frame(LLMFullResponseEndFrame())


class ScriptedTTS(FrameProcessor):
    """Speaks each completion the way the engine TTS does: a context, word frames
    stamped with their playout pts, the audio, then the response's End re-pushed after
    the audio and the context's stop. The text is taken from the LLM stream, which the
    ledger reads here -- at the TTS, as it does live."""

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
        await asyncio.sleep(0.1)                      # first clause
        now = self.get_clock().get_time()
        await self.push_frame(TTSStartedFrame(context_id=ctx))
        for i, w in enumerate(text.split()):
            await self.push_frame(word(w, ctx, now + int(i * 0.25e9)))
        for _ in range(10):
            await self.push_frame(audio(ctx))
        await self.push_frame(end)
        await self.push_frame(TTSStoppedFrame(context_id=ctx))


class Playout(FrameProcessor):
    """Stands in for the output transport: what reaches it is what the caller hears.
    Announces the bot speaking around each context's audio, both ways, as the real one
    does (MinWords and the ledger read those)."""

    def __init__(self):
        super().__init__()
        self.played = []   # context ids, per audio chunk
        self._speaking = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSAudioRawFrame):
                if not self._speaking:
                    self._speaking = True
                    await self.push_frame(BotStartedSpeakingFrame())
                    await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
                self.played.append(frame.context_id)
            elif isinstance(frame, (TTSStoppedFrame, InterruptionFrame)) and self._speaking:
                self._speaking = False
                await self.push_frame(frame, direction)
                await self.push_frame(BotStoppedSpeakingFrame())
                await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
                return
        await self.push_frame(frame, direction)


async def _the_call(with_gate: bool):
    """The ceiling commits "so I was thinking we could" while the caller is between
    words; they go on ("maybe try the other place instead") 0.1 s after the commit."""
    context = LLMContext([{"role": "system", "content": "sys"}])
    stop = ScriptedStop()
    pair = LLMContextAggregatorPair(
        context,
        user_params=user_aggregator_params(
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[stop],
            ),
        ),
    )
    llm, tts, out = ScriptedLLM(), ScriptedTTS(), Playout()
    ledger = TranscriptLedger(tts=tts)
    merge = TurnMerge() if with_gate else None
    gate = (ReplyHoldGate(merge=merge, release_secs=RELEASE_S, max_secs=MAX_S)
            if with_gate else None)
    if gate is not None:
        @pair.user().event_handler("on_user_turn_message_added")
        async def _arm(_a, _m):
            await gate.arm()
    pipeline = Pipeline([p for p in [
        pair.user(), HeardContextCorrector(ledger, context, merge=merge), llm, tts,
        gate, out, pair.assistant()] if p is not None])
    task = PipelineTask(pipeline, observers=[ledger], cancel_on_idle_timeout=False)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(STARTUP_S)

    async def say(text, final=False):
        cls = TranscriptionFrame if final else InterimTranscriptionFrame
        await task.queue_frame(cls(text, "caller", "t"))

    # The fragment.
    await task.queue_frame(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.1)
    await say("so I was thinking")
    await asyncio.sleep(0.2)
    await task.queue_frame(VADUserStoppedSpeakingFrame())
    await say("so I was thinking we could", final=True)
    await asyncio.sleep(0.2)
    await stop.commit()                                 # the 0.6 s ceiling
    await asyncio.sleep(0.1)
    # The caller was only pausing.
    await task.queue_frame(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.5)                            # the stale reply is ready here
    await say("maybe try")                              # first interim of the rest
    await asyncio.sleep(0.4)
    await task.queue_frame(VADUserStoppedSpeakingFrame())
    await say("maybe try the other place instead", final=True)
    await asyncio.sleep(0.1)
    await stop.commit()
    await asyncio.sleep(0.6)                            # the real answer plays
    await task.cancel()
    await run
    return llm, out, context, ledger, gate


async def test_the_calls_ceiling_commit_then_resume():
    llm, out, context, ledger, gate = await _the_call(with_gate=True)
    assert "r1" not in out.played, (
        f"the stale reply to the fragment reached the caller: {out.played.count('r1')} chunks")
    assert "r2" in out.played, "the answer to the whole turn never played"
    assert gate.holds == 1 and gate.dropped == 1, (gate.holds, gate.dropped)
    users = [m["content"] for m in llm.contexts[-1] if m["role"] == "user"]
    assert users == ["so I was thinking we could maybe try the other place instead"], (
        f"the model must get ONE user turn with every word, got {users!r}")
    assert not any(m["role"] == "assistant" for m in llm.contexts[-1]), (
        "the dropped reply leaked into the context as said")
    said = [u for u in ledger.events if u.speaker == "assistant" and u.heard_fraction > 0]
    assert all("r1" not in u.text for u in said), "the ledger charted the dropped reply as heard"
    assert not any("r1" in str(m.get("content")) for m in context.get_messages()), (
        "the dropped reply is in the context")


async def test_the_calls_shape_without_the_gate_is_the_reported_failure():
    # The same script on the brain as it was: the failure the gate exists for, so the
    # test above is known to be testing it.
    llm, out, context, ledger, _ = await _the_call(with_gate=False)
    assert "r1" in out.played, "without the gate the stale reply should have played"
    cut = [u for u in ledger.events if u.speaker == "assistant" and "r1" in u.text]
    assert cut and cut[0].interrupted and cut[0].heard_fraction > 0, (
        f"the stale reply should have been charted as heard, then cut: {cut!r}")
    users = [m["content"] for m in llm.contexts[-1] if m["role"] == "user"]
    assert len(users) == 2, f"expected the fragment and its continuation apart, got {users!r}"


async def _the_refused_call(continues: bool):
    """The ceiling commits "so I was thinking we could", and the caller resumes INSIDE
    that commit's await: the controller refuses its finalize (they are audibly
    speaking) and keep_barge_in_reachable owes it. Their words join the turn still open
    -- no turn start, so no interruption -- and the engine's segmenter closes the final
    of them before their VAD stop, which re-applies the stop: a second commit, with the
    first reply held and unplayed (formal/UserTurn.tla, ut_retryOnQuiet_early_nosplitturn).
    Wired as agent_session wires it: the fence holds the commits and, `continues`, asks
    the gate whether the last reply has played."""
    context = LLMContext([{"role": "system", "content": "sys"}])
    stop = ScriptedStop()
    pair = LLMContextAggregatorPair(
        context,
        user_params=user_aggregator_params(
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[stop],
            ),
        ),
    )
    user = pair.user()
    controller = user._user_turn_controller
    keep_barge_in_reachable(controller)
    fence = CutReplyFence()
    fence.hold_commits(user)
    llm, tts, out = ScriptedLLM(), ScriptedTTS(), Playout()
    ledger = TranscriptLedger(tts=tts)
    merge = TurnMerge()
    gate = ReplyHoldGate(merge=merge, release_secs=RELEASE_S, max_secs=MAX_S)
    if continues:
        fence.unplayed_reply = lambda: gate.armed

    @user.event_handler("on_user_turn_message_added")
    async def _arm(_a, _m):
        await gate.arm()

    pipeline = Pipeline([user, fence.head, HeardContextCorrector(ledger, context, merge=merge),
                         llm, tts, gate, out, pair.assistant(), fence.tail])
    task = PipelineTask(pipeline, observers=[ledger], cancel_on_idle_timeout=False)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(STARTUP_S)

    async def say(text, final=False):
        cls = TranscriptionFrame if final else InterimTranscriptionFrame
        await task.queue_frame(cls(text, "caller", "t"))

    # The fragment.
    await task.queue_frame(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.1)
    await say("so I was thinking")
    await asyncio.sleep(0.2)
    await task.queue_frame(VADUserStoppedSpeakingFrame())
    await say("so I was thinking we could", final=True)
    await asyncio.sleep(0.2)
    # The ceiling's stop, its two awaits straddling the caller's resumption.
    await stop.trigger_user_turn_inference_triggered()     # the commit; arms the gate
    await task.queue_frame(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.05)
    await stop.trigger_user_turn_finalized()                # refused: they are speaking
    refused = controller._user_turn
    await asyncio.sleep(0.4)                                # the reply is ready, held
    await say("maybe try")
    await asyncio.sleep(0.3)
    await say("maybe try the other place instead", final=True)
    await asyncio.sleep(0.05)
    await task.queue_frame(VADUserStoppedSpeakingFrame())  # re-applies the stop: commits
    await asyncio.sleep(RELEASE_S + 1.0)                   # the release window, and more
    await task.cancel()
    await run
    return llm, out, context, gate, refused


async def test_a_refused_finalizes_second_commit_merges_into_the_unplayed_turn():
    llm, out, context, gate, refused = await _the_refused_call(continues=True)
    assert refused, "the script must leave the turn open (the finalize refused)"
    assert "r1" not in out.played, (
        f"the held reply to the fragment played: {out.played.count('r1')} chunks")
    assert "r2" in out.played, "the answer to the whole turn never played"
    assert gate.holds == 1 and gate.dropped == 1 and gate.released == 0, (
        gate.holds, gate.dropped, gate.released)
    users = [m["content"] for m in llm.contexts[-1] if m["role"] == "user"]
    assert users == ["so I was thinking we could maybe try the other place instead"], (
        f"the model must get ONE user turn with every word, got {users!r}")
    assert not any(m["role"] == "assistant" for m in llm.contexts[-1]), llm.contexts[-1]
    assert not any("r1" in str(m.get("content")) for m in context.get_messages()), (
        "the dropped reply is in the context")


async def test_without_the_continuation_the_refused_finalize_splits_the_turn():
    # The brain as it was: the second commit lands beside the first, the held reply is
    # released after the window and answers the fragment, then the rest is answered.
    llm, out, _context, gate, refused = await _the_refused_call(continues=False)
    assert refused
    assert "r1" in out.played and "r2" in out.played, out.played
    assert gate.released == 1 and gate.dropped == 0, (gate.released, gate.dropped)
    users = [m["content"] for m in llm.contexts[-1] if m["role"] == "user"]
    assert users == ["so I was thinking we could", "maybe try the other place instead"], (
        f"expected the fragment and its continuation apart, got {users!r}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        if not asyncio.iscoroutinefunction(fn):
            fn()
            print(f"  ok {fn.__name__}")

    async def run_aio():
        for fn in tests:
            if asyncio.iscoroutinefunction(fn):
                await fn()
                print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
