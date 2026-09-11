#
# Unit test: a turn that opens AFTER its own VAD stop commits on its final, at once.
#
# Every barge-in on this brain has the same shape, because the transcriber returns
# nothing for words spoken over the bot until the VAD stop flushes the segment
# (teagram-engine#7): VAD stop -> Smart Turn verdict -> the interim that opens the
# turn, ~0.2 s later -> the final. pipecat resets the stop strategy at every turn start,
# which discards the VAD stop and the verdict it had just reached for THIS utterance;
# the final then lands in the strategy's "transcript without VAD" fallback and waits out
# the STT safety net (ttfs_p99 - stop_secs = 0.45 s) before the turn commits.
#
# Live 2026-09-10 (three calls, 58 commits): 7 commits at 0.65-0.71 s after the VAD stop
# instead of ~0.24 s, every one an "Okay, stop." / "Stop." over the bot. The other two
# late bands in that log (INCOMPLETE -> 1.0 s ceiling; an empty second segment -> the
# same 0.45 s safety net) are different mechanisms and are not what this pins.
#
# teaport_brain.endpointing.LateStartTurnStopStrategy keeps the verdict across a late
# start. Both halves are pinned: the brain's strategy must commit on the final, and the
# STOCK one must still wait the safety net -- if pipecat ever changes the reset,
# test_the_stock_strategy_still_waits_the_safety_net goes red and the subclass can go.
#
# Run: python test_late_start_keeps_verdict.py  (or via pytest test_suite.py)
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
)
from teaport_brain.stt import TEAPORT_TTFS_P99  # noqa: E402

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)

# The safety net the stock strategy waits out on a late start. Derived, not typed, so
# the test follows the brain's configuration rather than a number copied from a log.
SAFETY_NET_SECS = TEAPORT_TTFS_P99 - ENDPOINT_STOP_SECS

# How long after the VAD stop the caller's interim reaches the aggregator, live: the
# engine's commit -> first-delta on a flushed segment (~0.2 s, see the segment log
# lines "commit=vad-stop -> done in 0.22s").
FLUSH_DELAY_SECS = 0.2

# Well inside the safety net, well outside any event-loop jitter. A commit that lands
# here came from the final; one that lands at SAFETY_NET_SECS came from the timer.
PROMPT_SECS = 0.15

UTTERANCE = "Okay, stop."


class Complete(BaseSmartTurn):
    def _predict_endpoint(self, audio_array):
        return {"prediction": 1, "probability": 0.98}


class Incomplete(BaseSmartTurn):
    def _predict_endpoint(self, audio_array):
        return {"prediction": 0, "probability": 0.02}


def _analyzer(cls):
    analyzer = cls(sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=SMARTTURN_STOP_SECS))
    analyzer.set_sample_rate(SAMPLE_RATE)
    return analyzer


class Rig:
    """A real UserTurnController with the brain's start strategy and either stop
    strategy, recording WHEN the turn stopped."""

    def __init__(self, stop_cls, analyzer_cls=Complete):
        self.controller = UserTurnController(
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[stop_cls(turn_analyzer=_analyzer(analyzer_cls))],
            ),
            # The 5 s force-stop is not under test; keep it out of the way.
            user_turn_stop_timeout=3600,
        )
        self.started_at = None
        self.stopped_at = None

        async def ignore(*args, **kwargs):
            pass

        for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                      "on_user_turn_inference_triggered", "on_user_turn_stop_timeout"):
            self.controller.add_event_handler(event, ignore)

        async def on_started(_c, _s, _p):
            self.started_at = time.monotonic()

        async def on_stopped(_c, _s, _p):
            self.stopped_at = time.monotonic()

        self.controller.add_event_handler("on_user_turn_started", on_started)
        self.controller.add_event_handler("on_user_turn_stopped", on_stopped)

    async def audio(self, ms):
        """Mic audio keeps arriving whether or not the user is talking -- the live
        shape, and what drives the analyzer's own silence clock."""
        for _ in range(ms // CHUNK_MS):
            await self.controller.process_frame(
                InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1)
            )

    async def barge_in(self):
        """The live sequence of a stop spoken over the bot, up to and including the
        final. Returns the moment the final was delivered."""
        c = self.controller
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await self.audio(600)
        # No interim yet: the engine heard nothing through the bot's voice.
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        assert self.started_at is None, "no transcript yet, so no turn should be open"
        # The flush: everything arrives at once, ~0.2 s after the VAD stop.
        await asyncio.sleep(FLUSH_DELAY_SECS)
        await c.process_frame(InterimTranscriptionFrame(UTTERANCE, "u", "t", None))
        assert self.started_at is not None, "the interim must open the turn"
        final = TranscriptionFrame(UTTERANCE, "u", "t", None)
        final.finalized = True
        t_final = time.monotonic()
        await c.process_frame(final)
        return t_final

    async def settle(self, secs):
        """Let timers run while audio keeps arriving, as it would live."""
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline and self.stopped_at is None:
            await self.audio(CHUNK_MS)
            await asyncio.sleep(CHUNK_MS / 1000)


async def test_the_brain_strategy_commits_on_the_final():
    """The fix: verdict kept across the late start, so the final ends the turn."""
    rig = Rig(LateStartTurnStopStrategy)
    async with running_controller(rig.controller):
        t_final = await rig.barge_in()
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert rig.stopped_at is not None, "the turn never ended"
        lag = rig.stopped_at - t_final
        assert lag < PROMPT_SECS, (
            f"turn committed {lag:.2f}s after the final -- that is the safety net "
            f"({SAFETY_NET_SECS:.2f}s), not the final")


async def test_the_stock_strategy_still_waits_the_safety_net():
    """The reason the subclass exists. If this goes red, pipecat has stopped discarding
    the verdict at a late start and LateStartTurnStopStrategy can be retired."""
    rig = Rig(TurnAnalyzerUserTurnStopStrategy)
    async with running_controller(rig.controller):
        t_final = await rig.barge_in()
        await rig.settle(SAFETY_NET_SECS + 0.3)
        assert rig.stopped_at is not None, "the turn never ended"
        lag = rig.stopped_at - t_final
        assert lag >= SAFETY_NET_SECS - 0.05, (
            f"stock strategy committed {lag:.2f}s after the final -- pipecat no longer "
            f"resets the verdict at a late start; retire LateStartTurnStopStrategy")


async def test_an_incomplete_verdict_is_kept_too_and_the_ceiling_still_ends_the_turn():
    """Keeping the verdict must not mean inventing one. An INCOMPLETE verdict at the
    late start is kept as INCOMPLETE: the final does not commit, and the analyzer's own
    silence ceiling (SMARTTURN_STOP_SECS) ends the turn as it always did."""
    rig = Rig(LateStartTurnStopStrategy, analyzer_cls=Incomplete)
    async with running_controller(rig.controller):
        t_final = await rig.barge_in()
        await rig.settle(SMARTTURN_STOP_SECS + 0.5)
        assert rig.stopped_at is not None, "the turn never ended"
        lag = rig.stopped_at - t_final
        # The ceiling is counted from the VAD stop, which was FLUSH_DELAY_SECS before
        # the final. Loose bounds: this pins "not on the final, and not never".
        assert lag > SAFETY_NET_SECS, (
            f"INCOMPLETE turn committed {lag:.2f}s after the final -- the verdict was "
            f"not kept, or the safety net fired on it")
        assert lag < SMARTTURN_STOP_SECS + 0.3, (
            f"INCOMPLETE turn took {lag:.2f}s after the final -- longer than the ceiling")


async def test_a_turn_that_opens_while_the_user_speaks_still_resets():
    """The ordinary path is untouched: an interim that arrives while the user is still
    talking opens the turn BEFORE the VAD stop, the strategy resets as stock, and the
    turn ends on the VAD stop + verdict + final like any other."""
    rig = Rig(LateStartTurnStopStrategy)
    c = rig.controller
    async with running_controller(c):
        await c.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.audio(300)
        await c.process_frame(InterimTranscriptionFrame("tell me a", "u", "t", None))
        assert rig.started_at is not None
        strategy = c._user_turn_strategies.stop[0]
        assert strategy._vad_stopped is False and strategy._turn_complete is False, (
            "an early start must leave the strategy reset, not carrying a verdict")
        await rig.audio(300)
        await c.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        final = TranscriptionFrame("tell me a story", "u", "t", None)
        final.finalized = True
        t_final = time.monotonic()
        await c.process_frame(final)
        await rig.settle(0.5)
        assert rig.stopped_at is not None, "the ordinary turn never ended"
        assert rig.stopped_at - t_final < PROMPT_SECS


def main():
    # Discovered, not listed: a test added below and left off a hand-kept list would
    # silently never run, which is how five gate tests once reported ALL PASS on nine.
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
