#
# Unit test: a final that answers an EARLIER utterance's commit does not end the turn
# the caller has since begun -- the turn waits for that utterance's own final, or for
# its segment to close empty.
#
# The engine's finish takes ~0.7 s, so a caller who speaks in bursts starts the next
# utterance before the previous one's final lands. Live 2026-09-19 13:31:46 (SIP):
#
#   46.26  VAD stop A ("Yeah, I mean, that's only letters...") -> COMPLETE -> commit A
#   46.62  VAD start B                                  (0.35 s later; A's done not in)
#   47.19  final A lands, inside the turn that now holds B
#   48.28  VAD stop B ("They care about phonemes.") -> COMPLETE
#   48.59  TURN-COMMIT +304 ms: the STT p99 safety net fired on A's text (B's own
#          interims had cleared `_transcript_finalized`; with none -- a short B --
#          the verdict fires on A's final at once instead)
#   49.47  final B lands -> new turn -> InterruptionFrame -> A's reply CUT heard~0%
#   49.79  TURN-COMMIT again, on A+B, +1.5 s after B's stop
#
# 38 of 431 replies since 2026-09-16 were cut at 0% this way or near it. The fix is
# in two halves and both are pinned here: stt.py stamps every final with the time of
# the commit it answers and marks every segment close with a SegmentDoneFrame;
# LateStartTurnStopStrategy takes a final committed before the caller's latest VAD
# start as an earlier utterance's and holds it back from `_text`, so neither the
# verdict nor the safety net can end the turn on it. The STOCK strategy's behaviour
# is pinned too: if pipecat ever stops taking the earlier final, that test goes red
# and the rule can go.
#
# Run: python test_earlier_final_does_not_end_the_turn.py  (or via pytest test_suite.py)
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
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (  # noqa: E402
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_controller import UserTurnController  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from turn_harness import running_controller  # noqa: E402

from teaport_brain.endpointing import (  # noqa: E402
    ENDPOINT_STOP_SECS,
    INTERRUPT_MIN_WORDS,
    SMARTTURN_STOP_SECS,
    LateStartTurnStopStrategy,
    SegmentDoneFrame,
)
from teaport_brain.stt import (  # noqa: E402
    TEAPORT_TTFS_P99,
    FinalTranscriptionFrame,
    TeaportSTTService,
)

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)

# The STT safety net, from the VAD stop. Derived, not typed, so the test follows the
# brain's configuration. This is the timer that fired at +304 ms live.
SAFETY_NET_SECS = TEAPORT_TTFS_P99 - ENDPOINT_STOP_SECS

# The live shape, rounded: the caller resumes 0.35 s after A's stop, A's final lands
# 0.9 s after its commit, B's final 0.9 s after B's stop.
RESUME_SECS = 0.35
FINAL_LAG_SECS = 0.9

# Well inside the safety net, well outside event-loop jitter: a stop that lands here
# came from the frame just delivered.
PROMPT_SECS = 0.15

A_TEXT = "Yeah, I mean, that's only letters."
B_TEXT = "They care about phonemes."


class Complete(BaseSmartTurn):
    def _predict_endpoint(self, audio_array):
        return {"prediction": 1, "probability": 0.98}


def _analyzer():
    analyzer = Complete(sample_rate=SAMPLE_RATE,
                        params=SmartTurnParams(stop_secs=SMARTTURN_STOP_SECS))
    analyzer.set_sample_rate(SAMPLE_RATE)
    return analyzer


def _final(text, committed_at):
    return FinalTranscriptionFrame(text, "u", "t", None, finalized=True,
                                   committed_at=committed_at)


class Rig:
    """A real UserTurnController with the brain's start strategy and either stop
    strategy, recording every turn start and stop."""

    def __init__(self, stop_cls):
        self.controller = UserTurnController(
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[stop_cls(turn_analyzer=_analyzer())],
            ),
            # The 5 s force-stop is not under test; keep it out of the way.
            user_turn_stop_timeout=3600,
        )
        self.starts = []
        self.stops = []

        async def ignore(*args, **kwargs):
            pass

        for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                      "on_user_turn_inference_triggered", "on_user_turn_stop_timeout"):
            self.controller.add_event_handler(event, ignore)

        async def on_started(_c, _s, _p):
            self.starts.append(time.monotonic())

        async def on_stopped(_c, _s, _p):
            self.stops.append(time.monotonic())

        self.controller.add_event_handler("on_user_turn_started", on_started)
        self.controller.add_event_handler("on_user_turn_stopped", on_stopped)

    async def audio(self, ms):
        """Mic audio keeps arriving whether or not the user is talking -- the live
        shape, and what drives the analyzer's own silence clock."""
        for _ in range(ms // CHUNK_MS):
            await self.controller.process_frame(
                InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1)
            )
            await asyncio.sleep(CHUNK_MS / 1000)

    async def utterance_a(self):
        """A, up to and including its commit. Returns the commit time: A's final is
        still FINAL_LAG_SECS away when the caller starts B."""
        c = self.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await self.audio(400)
        await c.process_frame(InterimTranscriptionFrame(A_TEXT, "u", "t", None))
        assert len(self.starts) == 1, "A's interim must open the turn"
        await self.audio(400)
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        return time.monotonic()  # the STT commits on the COMPLETE verdict, now

    async def burst(self, *, b_interims):
        """A committed, B started RESUME_SECS later, A's final landing inside B, then
        B's VAD stop. Returns B's commit time; B's own final is not delivered."""
        c = self.controller
        commit_a = await self.utterance_a()
        await self.audio(int(RESUME_SECS * 1000))
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await self.audio(int((FINAL_LAG_SECS - RESUME_SECS) * 1000))
        await c.process_frame(_final(A_TEXT, commit_a))
        assert not self.stops, "A's final arrived inside B: the turn must not end on it"
        if b_interims:
            # The engine's deltas for B, as they trail its speech.
            for n in (1, 2, 3):
                await c.process_frame(InterimTranscriptionFrame(
                    " ".join(B_TEXT.split()[:n]), "u", "t", None))
                await self.audio(100)
        else:
            # A short B: the engine's first delta on the fresh segment trails the
            # reset by ~0.6 s, so nothing of B has streamed by its stop.
            await self.audio(300)
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        return time.monotonic()

    async def settle(self, secs):
        """Let timers run while audio keeps arriving, as it would live."""
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline and not self.stops:
            await self.audio(CHUNK_MS)


async def _the_turn_waits_for_b(*, b_interims):
    rig = Rig(LateStartTurnStopStrategy)
    c = rig.controller
    async with running_controller(c):
        commit_b = await rig.burst(b_interims=b_interims)
        # Past the safety net, well past the immediate verdict: neither may fire.
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert not rig.stops, (
            f"the turn ended {rig.stops[0] - commit_b:.2f}s after B's stop, before B's "
            f"final -- on A's final, which was committed before B began")
        await rig.audio(int((FINAL_LAG_SECS - SAFETY_NET_SECS - 0.3) * 1000))
        t_final_b = time.monotonic()
        await c.process_frame(_final(B_TEXT, commit_b))
        await c.process_frame(SegmentDoneFrame(committed_at=commit_b))
        await rig.settle(0.5)
        assert len(rig.stops) == 1, "the turn never ended on B's final"
        lag = rig.stops[0] - t_final_b
        assert lag < PROMPT_SECS, f"turn ended {lag:.2f}s after B's final, not on it"
        assert len(rig.starts) == 1, (
            f"{len(rig.starts)} turn starts: B's final opened a second turn (and its "
            "interruption would have cut A's reply)")


async def test_a_short_b_with_no_interims_waits_for_its_own_final():
    """The verdict at B's stop finds A's final finalized and would end the turn on it
    at once; the brain's strategy holds it back and waits for B's."""
    await _the_turn_waits_for_b(b_interims=False)


async def test_a_b_with_interims_is_not_ended_by_the_safety_net_either():
    """B's interims clear `_transcript_finalized`, so the stock path is the p99
    safety net firing on A's text at +SAFETY_NET_SECS; A's text is never `_text`
    here, so the net has nothing to fire on."""
    await _the_turn_waits_for_b(b_interims=True)


async def test_the_stock_strategy_ends_the_turn_on_the_earlier_final():
    """The reason the rule exists. If this goes red, pipecat no longer takes an
    earlier utterance's final as the current one's, and the rule can be retired."""
    rig = Rig(TurnAnalyzerUserTurnStopStrategy)
    async with running_controller(rig.controller):
        commit_b = await rig.burst(b_interims=True)
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert rig.stops, (
            "the stock strategy waited for B's final -- pipecat has changed; retire "
            "the earlier-final rule in LateStartTurnStopStrategy")
        assert rig.stops[0] - commit_b < FINAL_LAG_SECS


async def test_an_empty_segment_ends_the_turn_on_the_earlier_final():
    """B's segment closes with no words (the caller's resumption was a noise the
    engine found nothing in): the SegmentDoneFrame says so, A's final is all the turn
    holds, and the turn ends on it -- promptly, not after some timer."""
    rig = Rig(LateStartTurnStopStrategy)
    c = rig.controller
    async with running_controller(c):
        commit_b = await rig.burst(b_interims=False)
        await rig.audio(int(FINAL_LAG_SECS * 1000))
        assert not rig.stops
        t_done = time.monotonic()
        await c.process_frame(SegmentDoneFrame(committed_at=commit_b))
        await rig.settle(0.5)
        assert len(rig.stops) == 1, "the turn never ended after B's empty segment"
        lag = rig.stops[0] - t_done
        assert lag < PROMPT_SECS, f"turn ended {lag:.2f}s after the segment close, not on it"


async def test_the_engine_closing_empty_mid_utterance_keeps_waiting():
    """A segment close that lands while the caller is still speaking B -- no VAD stop
    yet -- is not B's answer; the earlier final stays held and B's own final ends
    the turn."""
    rig = Rig(LateStartTurnStopStrategy)
    c = rig.controller
    async with running_controller(c):
        commit_a = await rig.utterance_a()
        await rig.audio(int(RESUME_SECS * 1000))
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(300)
        await c.process_frame(_final(A_TEXT, commit_a))
        await c.process_frame(SegmentDoneFrame(committed_at=time.monotonic()))
        await rig.audio(600)
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        commit_b = time.monotonic()
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert not rig.stops, "the turn ended before B's final"
        t_final_b = time.monotonic()
        await c.process_frame(_final(B_TEXT, commit_b))
        await c.process_frame(SegmentDoneFrame(committed_at=commit_b))
        await rig.settle(0.5)
        assert len(rig.stops) == 1 and rig.stops[0] - t_final_b < PROMPT_SECS


async def test_a_final_committed_after_the_start_ends_the_turn_as_before():
    """The ordinary turn, with the stamp: the final answers this utterance's own
    commit and ends the turn on arrival."""
    rig = Rig(LateStartTurnStopStrategy)
    c = rig.controller
    async with running_controller(c):
        commit_a = await rig.utterance_a()
        await rig.audio(200)
        t_final = time.monotonic()
        await c.process_frame(_final(A_TEXT, commit_a))
        await c.process_frame(SegmentDoneFrame(committed_at=commit_a))
        await rig.settle(0.5)
        assert len(rig.stops) == 1 and rig.stops[0] - t_final < PROMPT_SECS


class Recorder(TeaportSTTService):
    """Real _handle_message, frames captured instead of pushed."""

    def __init__(self):
        super().__init__(url="ws://127.0.0.1:1/none")
        self.pushed = []

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)

    async def stop_processing_metrics(self):
        pass


async def test_the_stt_stamps_the_final_and_marks_every_segment_close():
    """The other half: the final carries the commit's time, and a SegmentDoneFrame
    follows every done -- behind the final when there is one, alone when the engine
    found no words, and stamped with the close time when nothing was committed."""
    stt = Recorder()
    stt._commit_at = committed = time.monotonic() - 0.7
    await stt._handle_message({"type": "transcription.delta", "delta": B_TEXT})
    await stt._handle_message({"type": "transcription.done", "text": B_TEXT})
    kinds = [type(f).__name__ for f in stt.pushed]
    assert kinds == ["InterimTranscriptionFrame", "FinalTranscriptionFrame",
                     "SegmentDoneFrame"], kinds
    final, done = stt.pushed[1], stt.pushed[2]
    assert final.committed_at == committed and done.committed_at == committed

    stt.pushed.clear()
    stt._commit_at = committed = time.monotonic()
    await stt._handle_message({"type": "transcription.done", "text": ""})
    assert [type(f).__name__ for f in stt.pushed] == ["SegmentDoneFrame"]
    assert stt.pushed[0].committed_at == committed

    stt.pushed.clear()
    before = time.monotonic()
    await stt._handle_message({"type": "transcription.done", "text": "unasked"})
    assert [type(f).__name__ for f in stt.pushed] == ["FinalTranscriptionFrame",
                                                      "SegmentDoneFrame"]
    assert stt.pushed[0].committed_at == stt.pushed[1].committed_at >= before


def main():
    # Discovered, not listed: a test added below and left off a hand-kept list would
    # silently never run.
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
