#
# Unit test: a final that answers an EARLIER utterance's commit does not end the turn
# the caller has since begun -- the turn waits for the close of the STT's segment that
# answers its own VAD stop.
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
# in two halves and both are pinned here. stt.py stamps every final, and every
# wordless close (SegmentDoneFrame), with the VAD stop it answers -- a COUNT of
# VADUserStoppedSpeakingFrames, taken at the commit the done answers (commits and
# dones pair up in order), and by three rules for a done the engine sent on its own.
# LateStartTurnStopStrategy counts the same frames and, once the VAD has reported a
# stop, ends the turn only on a close stamped with that stop while the caller has not
# started again: neither the verdict, the ceiling nor the p99 safety net can end it on
# anything else. The first cut of this fix compared CLOCK readings instead (the
# commit's time against the strategy's read of the VAD start) and stamped an unasked
# done with its arrival; the review of PR #49 found three orderings it got wrong, and
# brain/formal/SttCommit.tla now has them as rows. They are pinned here too: an unasked
# close landing after the next start, the engine's own split of one utterance with the
# safety net on the first half, and the wordless tail of a commit landing after the
# next stop. The STOCK strategy's behaviour is pinned as well: if pipecat ever stops
# taking the earlier final, that test goes red and the rule can go.
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
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop.base_user_turn_stop_strategy import (  # noqa: E402
    UserTurnStoppedParams,
)
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (  # noqa: E402
    TurnAnalyzerUserTurnStopStrategy,
)
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
    TurnVerdictFrame,
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


def _final(text, stop_n):
    return FinalTranscriptionFrame(text, "u", "t", None, finalized=True, stop_n=stop_n)


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

    async def vad_start(self):
        await self.controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))

    async def vad_stop(self):
        await self.controller.process_frame(
            VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))

    async def interim(self, text):
        await self.controller.process_frame(InterimTranscriptionFrame(text, "u", "t", None))

    async def utterance_a(self):
        """A, up to and including its VAD stop (stop 1): the STT commits on the
        COMPLETE verdict now, and A's final is still FINAL_LAG_SECS away when the
        caller starts B."""
        await self.vad_start()
        await self.audio(400)
        await self.interim(A_TEXT)
        assert len(self.starts) == 1, "A's interim must open the turn"
        await self.audio(400)
        await self.vad_stop()

    async def burst(self, *, b_interims):
        """A committed, B started RESUME_SECS later, A's final (stamped with stop 1)
        landing inside B, then B's VAD stop (stop 2). B's own final is not delivered."""
        await self.utterance_a()
        await self.audio(int(RESUME_SECS * 1000))
        await self.vad_start()
        await self.audio(int((FINAL_LAG_SECS - RESUME_SECS) * 1000))
        await self.controller.process_frame(_final(A_TEXT, 1))
        assert not self.stops, "A's final arrived inside B: the turn must not end on it"
        if b_interims:
            # The engine's deltas for B, as they trail its speech.
            for n in (1, 2, 3):
                await self.interim(" ".join(B_TEXT.split()[:n]))
                await self.audio(100)
        else:
            # A short B: the engine's first delta on the fresh segment trails the
            # reset by ~0.6 s, so nothing of B has streamed by its stop.
            await self.audio(300)
        await self.vad_stop()

    async def settle(self, secs):
        """Let timers run while audio keeps arriving, as it would live."""
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline and not self.stops:
            await self.audio(CHUNK_MS)

    async def expect_end_on(self, frame):
        """Deliver `frame` and require the turn to end on it, promptly, with no
        second turn opened."""
        t = time.monotonic()
        await self.controller.process_frame(frame)
        await self.settle(0.5)
        assert len(self.stops) == 1, "the turn never ended on the segment's close"
        lag = self.stops[0] - t
        assert lag < PROMPT_SECS, f"turn ended {lag:.2f}s after the close, not on it"
        assert len(self.starts) == 1, (
            f"{len(self.starts)} turn starts: the final opened a second turn (and its "
            "interruption would have cut the reply)")


async def _the_turn_waits_for_b(*, b_interims):
    rig = Rig(LateStartTurnStopStrategy)
    async with running_controller(rig.controller):
        await rig.burst(b_interims=b_interims)
        # Past the safety net, well past the immediate verdict: neither may fire.
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert not rig.stops, (
            f"the turn ended {rig.stops[0] - rig.starts[0]:.2f}s after opening, before "
            "B's final -- on A's final, which answers stop 1, not B's stop 2")
        await rig.audio(int((FINAL_LAG_SECS - SAFETY_NET_SECS - 0.3) * 1000))
        await rig.expect_end_on(_final(B_TEXT, 2))


async def test_a_short_b_with_no_interims_waits_for_its_own_final():
    """The verdict at B's stop finds A's final finalized and would end the turn on it
    at once; the brain's strategy waits for the close that answers stop 2."""
    await _the_turn_waits_for_b(b_interims=False)


async def test_a_b_with_interims_is_not_ended_by_the_safety_net_either():
    """B's interims clear `_transcript_finalized`, so the stock path is the p99
    safety net firing on A's text at +SAFETY_NET_SECS; the net is gated on the
    stop's close like every other path."""
    await _the_turn_waits_for_b(b_interims=True)


async def test_the_stock_strategy_ends_the_turn_on_the_earlier_final():
    """The reason the rule exists. If this goes red, pipecat no longer takes an
    earlier utterance's final as the current one's, and the rule can be retired."""
    rig = Rig(TurnAnalyzerUserTurnStopStrategy)
    async with running_controller(rig.controller):
        await rig.burst(b_interims=True)
        t_stop_b = time.monotonic()
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert rig.stops, (
            "the stock strategy waited for B's final -- pipecat has changed; retire "
            "the stop-count rule in LateStartTurnStopStrategy")
        assert rig.stops[0] - t_stop_b < FINAL_LAG_SECS


async def test_an_empty_segment_ends_the_turn_on_the_earlier_final():
    """B's segment closes with no words (the caller's resumption was a noise the
    engine found nothing in): the wordless close answers stop 2, A's final is all
    the turn holds, and the turn ends on it -- promptly, not after some timer."""
    rig = Rig(LateStartTurnStopStrategy)
    async with running_controller(rig.controller):
        await rig.burst(b_interims=False)
        await rig.audio(int(FINAL_LAG_SECS * 1000))
        assert not rig.stops
        await rig.expect_end_on(SegmentDoneFrame(stop_n=2))


async def test_the_engine_closing_empty_mid_utterance_keeps_waiting():
    """A wordless close that lands while the caller is still speaking B -- stamped,
    as the STT stamps one, with the last commit's stop (1) -- is not B's answer.
    Nor is A's final. B's own final ends the turn."""
    rig = Rig(LateStartTurnStopStrategy)
    async with running_controller(rig.controller):
        await rig.utterance_a()
        await rig.audio(int(RESUME_SECS * 1000))
        await rig.vad_start()
        await rig.audio(300)
        await rig.controller.process_frame(_final(A_TEXT, 1))
        await rig.controller.process_frame(SegmentDoneFrame(stop_n=1))
        await rig.audio(600)
        await rig.vad_stop()
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert not rig.stops, "the turn ended before B's final"
        await rig.expect_end_on(_final(B_TEXT, 2))


async def test_the_wordless_tail_of_the_earlier_commit_landing_after_the_next_stop():
    """Review of PR #49, finding 2. The engine closed A on its own; the brain's commit
    for stop 1 closed the silent tail behind it; the tail's wordless done lands after
    B's stop. It is stamped with the commit it answers (stop 1), so it is not stop
    2's close and does not end the turn on A while B's commit is still out."""
    rig = Rig(LateStartTurnStopStrategy)
    async with running_controller(rig.controller):
        await rig.burst(b_interims=False)
        await rig.audio(200)
        await rig.controller.process_frame(SegmentDoneFrame(stop_n=1))
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert not rig.stops, "the wordless tail of stop 1 ended the turn at stop 2"
        await rig.expect_end_on(_final(B_TEXT, 2))


async def test_an_earlier_final_landing_after_the_next_stop_is_not_taken_either():
    """A's final can land after B's stop too (a slow finish). Stop 1 is not the
    latest stop; the turn waits for stop 2's close."""
    rig = Rig(LateStartTurnStopStrategy)
    async with running_controller(rig.controller):
        await rig.utterance_a()
        await rig.audio(int(RESUME_SECS * 1000))
        await rig.vad_start()
        await rig.audio(600)
        await rig.vad_stop()
        await rig.audio(200)
        await rig.controller.process_frame(_final(A_TEXT, 1))
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert not rig.stops, "A's final, landing after B's stop, ended the turn"
        await rig.expect_end_on(_final(B_TEXT, 2))


async def _the_engines_split_of_one_utterance(*, tail):
    """Review of PR #49, finding 3. One VAD utterance, a breath inside it shorter
    than ENDPOINT_STOP_SECS that the engine's own endpointer cuts: its final for the
    first half lands mid-utterance, stamped (as the STT stamps an unasked close with
    words) with the stop still to come. The verdict at the stop must not end the
    turn on that half, and neither may the safety net once the second half's
    interims have cleared `_transcript_finalized`; the turn ends on the close the
    stop itself produces -- the second half's final, or a wordless tail, which then
    finalizes the first half."""
    rig = Rig(LateStartTurnStopStrategy)
    async with running_controller(rig.controller):
        await rig.vad_start()
        await rig.audio(400)
        await rig.interim(A_TEXT)
        await rig.audio(400)
        # The engine's close of the first half: an unasked done, stamped 1 = the
        # stop that has not come yet.
        await rig.controller.process_frame(_final(A_TEXT, 1))
        if tail == "words":
            for n in (1, 2, 3):
                await rig.interim(" ".join(B_TEXT.split()[:n]))
                await rig.audio(100)
        else:
            await rig.audio(300)
        await rig.vad_stop()  # stop 1: COMPLETE, the first half finalized in hand
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert not rig.stops, (
            f"the turn ended {rig.stops[0] - rig.starts[0]:.2f}s after opening on the "
            "first half of the utterance, while the stop's own commit was still out")
        if tail == "words":
            await rig.expect_end_on(_final(B_TEXT, 1))
        else:
            await rig.expect_end_on(SegmentDoneFrame(stop_n=1))


async def test_the_engines_split_waits_for_the_second_halfs_final():
    await _the_engines_split_of_one_utterance(tail="words")


async def test_the_engines_split_waits_for_the_stops_wordless_tail():
    await _the_engines_split_of_one_utterance(tail="empty")


class Incomplete(BaseSmartTurn):
    def _predict_endpoint(self, audio_array):
        return {"prediction": 0, "probability": 0.1}


async def test_the_backstops_half_does_not_end_a_turn_closed_under_the_ceiling():
    """Found by SttCommit.tla (NoStaleTurnEnd, MaxCloses = 0). The backstop commits
    half a sentence while the caller is still talking (stamp 0: no stop yet); the
    stop's verdict is INCOMPLETE and the STT holds the rest; the controller closes
    the turn under the ceiling (its watchdog, or keep_barge_in_reachable re-applying
    a refused stop -- driven here as the watchdog drives it). The backstop's half
    then lands, opens a NEW turn, and pipecat's fallback for a transcript with no
    VAD stop in sight would end it 0.3 s later -- with the held half still owed,
    which would then cut the reply. An older stop's words end no turn; the held
    half's final (stamp 1) does."""
    rig = Rig(LateStartTurnStopStrategy)
    rig.controller = UserTurnController(
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
            stop=[LateStartTurnStopStrategy(turn_analyzer=Incomplete(
                sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=SMARTTURN_STOP_SECS)))],
        ),
        user_turn_stop_timeout=3600,
    )
    rig.controller.add_event_handler("on_user_turn_started",
                                     lambda _c, _s, _p: rig.starts.append(time.monotonic()))
    rig.controller.add_event_handler("on_user_turn_stopped",
                                     lambda _c, _s, _p: rig.stops.append(time.monotonic()))

    async def ignore(*args, **kwargs):
        pass

    for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                  "on_user_turn_inference_triggered", "on_user_turn_stop_timeout"):
        rig.controller.add_event_handler(event, ignore)
    c = rig.controller
    async with running_controller(c):
        await rig.vad_start()
        await rig.audio(400)
        await rig.interim(A_TEXT)
        assert len(rig.starts) == 1
        await rig.audio(400)
        await rig.vad_stop()                       # stop 1: INCOMPLETE, the rest held
        await rig.audio(200)
        # The controller closes the turn under the ceiling: what its watchdog does.
        await c._trigger_user_turn_stop(None, UserTurnStoppedParams(enable_user_speaking_frames=True))
        assert len(rig.stops) == 1
        rig.stops.clear()
        await c.process_frame(_final(A_TEXT, 0))   # the backstop's half, stamp 0
        assert len(rig.starts) == 2, "the backstop's half must open a new turn"
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert not rig.stops, ("the new turn ended on the backstop's half, "
                               "with stop 1's held words still owed")
        await c.process_frame(_final(B_TEXT, 1))   # the held half: the turn-stop's commit
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert len(rig.stops) == 1, "the turn never ended on the held half"


async def test_a_final_answering_the_stop_ends_the_turn_as_before():
    """The ordinary turn, with the stamp: the final answers this utterance's own stop
    and ends the turn on arrival."""
    rig = Rig(LateStartTurnStopStrategy)
    async with running_controller(rig.controller):
        await rig.utterance_a()
        await rig.audio(200)
        await rig.expect_end_on(_final(A_TEXT, 1))


async def test_a_final_without_a_stamp_is_taken_as_stock():
    """Another STT service's final carries no stop: it answers, as it always did."""
    rig = Rig(LateStartTurnStopStrategy)
    async with running_controller(rig.controller):
        await rig.utterance_a()
        await rig.audio(200)
        await rig.expect_end_on(TranscriptionFrame(A_TEXT, "u", "t", None, finalized=True))


# ---- the STT's half: the stamps --------------------------------------------------

class Recorder(TeaportSTTService):
    """The real service with the wire recorded instead of sent; frames captured."""

    def __init__(self):
        super().__init__(url="ws://127.0.0.1:1/none", commit_on="verdict")
        self._websocket = WireRecorder()
        self.pushed = []

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.pushed.append(frame)

    async def stop_processing_metrics(self):
        pass

    def create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro)

    async def cancel_task(self, task, timeout=None):
        task.cancel()

    async def vad_stop(self):
        await self.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS),
                                 FrameDirection.DOWNSTREAM)

    async def vad_start(self):
        await self.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2),
                                 FrameDirection.DOWNSTREAM)

    async def verdict(self, complete):
        await self.process_frame(TurnVerdictFrame(complete=complete, source="verdict"),
                                 FrameDirection.UPSTREAM)

    async def delta(self, text):
        await self._handle_message({"type": "transcription.delta", "delta": text})

    async def done(self, text):
        await self._handle_message({"type": "transcription.done", "text": text})

    def closes(self):
        """(kind, stamp) per segment close pushed, in order."""
        out = []
        for f in self.pushed:
            if isinstance(f, FinalTranscriptionFrame):
                out.append(("final", f.stop_n))
            elif isinstance(f, SegmentDoneFrame):
                out.append(("done", f.stop_n))
        return out


async def test_the_stt_stamps_each_final_with_the_stop_its_commit_answered():
    """Two commits out at once -- the live burst -- and the dones answer them in
    order: A's carries stop 1 although it lands after stop 2's commit. A final with
    words is its own close; no SegmentDoneFrame follows it."""
    s = Recorder()
    await s.delta(A_TEXT)
    await s.vad_stop()                       # stop 1
    await s.verdict(True)                    # commit 1
    await s.vad_start()
    await s.delta(B_TEXT)
    await s.vad_stop()                       # stop 2
    await s.verdict(True)                    # commit 2, before A's done
    assert [c["final"] for c in s._websocket.commits()] == [True, True]
    await s.done(A_TEXT)
    await s.done(B_TEXT)
    assert s.closes() == [("final", 1), ("final", 2)], s.closes()


async def test_an_unasked_close_answers_no_stop_and_keeps_a_hold():
    """The engine's own close, landing after the caller resumed (stop 1's verdict
    never committed: the resume released it) or under stop 1's hold: it answers no
    stop of ours, so it is stamped as the NEXT stop's, and a hold it lands under
    stays -- it may be the previous utterance's words, not the held segment's. The
    first cut stamped such a done with its arrival time and took one landing after
    the next start as that utterance's (review of PR #49, finding 1); its release
    of the hold would have left the held words to the engine, unstamped."""
    s = Recorder()
    await s.delta(A_TEXT)
    await s.vad_stop()                       # stop 1, held for the verdict
    await s.vad_start()                      # the caller resumes first...
    assert s._commit_pending is False
    await s.done(A_TEXT)                     # ...and the engine's own close lands
    assert s._websocket.commits() == []
    assert s.closes() == [("final", 2)], s.closes()

    s = Recorder()
    await s.delta(A_TEXT)
    await s.vad_stop()                       # stop 1, held
    await s.verdict(False)                   # INCOMPLETE: still held
    await s.done(A_TEXT)                     # the engine's close lands under the hold
    assert s._commit_pending is True and s._websocket.commits() == []
    assert s.closes() == [("final", 2)], s.closes()
    await s.verdict(True)                    # the ceiling: commit 1 closes the tail
    await s.done("")
    assert s.closes() == [("final", 2), ("done", 1)], s.closes()


async def test_an_unasked_close_with_words_is_stamped_with_the_stop_to_come():
    """The engine's own close of the utterance under way -- a breath it cut, or its
    endpointer beating the VAD's floor: no stop has come for these words, so they
    are the next stop's, and answer it only when the stop's own close follows."""
    s = Recorder()
    await s.vad_start()
    await s.delta(A_TEXT)
    await s.done(A_TEXT)                     # nothing committed, nothing held
    assert s.closes() == [("final", 1)], s.closes()
    await s.vad_stop()                       # stop 1: the tail, committed at once
    assert [c["final"] for c in s._websocket.commits()] == [True]
    await s.done("")
    assert s.closes() == [("final", 1), ("done", 1)], s.closes()


async def test_an_unasked_wordless_close_is_stamped_with_the_last_commit():
    """Two dones for one commit: the engine closed on its own and the commit's answer
    is the silent tail. The engine's close took the commit's place in the queue,
    so the tail arrives unasked -- and wordless, which no close of the engine's own
    is (its endpointer closes on speech it heard). It is stamped with that commit's
    stop, never the stop of an utterance since, even when it lands after that
    utterance's stop (review of PR #49, finding 2).

    Known limit, deliberately NOT pinned: with a SECOND commit already out when the
    engine's close lands (a finish slower than the whole next utterance), the tail
    pairs with that commit instead; the wire cannot tell the two apart, and
    SttCommit.tla's unmarked row keeps that counterexample."""
    s = Recorder()
    await s.delta(A_TEXT)
    await s.vad_stop()                       # stop 1
    await s.verdict(True)                    # commit 1 -- the engine had closed already
    await s.vad_start()
    await s.delta(B_TEXT)
    await s.vad_stop()                       # stop 2, held: B's words streamed
    await s.done(A_TEXT)                     # the engine's close, paired with commit 1
    await s.done("")                         # commit 1's real answer: unasked now
    await s.verdict(True)                    # commit 2
    await s.done(B_TEXT)                     # commit 2's
    assert s.closes() == [("final", 1), ("done", 1), ("final", 2)], s.closes()


async def test_a_commit_whose_send_failed_owes_no_done():
    """A commit dropped on a dead socket gets no done; its stamp must not wait in
    the queue for the next done the engine sends on its own (the first cut left
    the stamp set, and the next done inherited it: review of PR #49, finding 6)."""
    s = Recorder()

    async def dead(raw):
        raise OSError("socket closed")

    s._websocket.send = dead
    await s.delta(A_TEXT)
    await s.vad_stop()                       # stop 1
    await s.verdict(True)                    # the commit's send fails
    assert len(s._commits) == 0, "a commit that never reached the engine is queued"
    s._websocket = WireRecorder()
    await s.vad_start()
    await s.delta(B_TEXT)
    await s.done(A_TEXT + " " + B_TEXT)      # the engine's own close, later
    assert s.closes() == [("final", 2)], s.closes()


async def test_a_disconnect_forgets_the_outstanding_commits():
    s = Recorder()
    await s.delta(A_TEXT)
    await s.vad_stop()
    await s.verdict(True)
    assert len(s._commits) == 1
    s._websocket = None                  # the recorder has no close handshake to run
    await s._disconnect_websocket()
    assert len(s._commits) == 0


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
