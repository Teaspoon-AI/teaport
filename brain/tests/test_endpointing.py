#
# Unit test: the turn ALWAYS ends, and never before the user has finished.
#
# These pin the pipecat behaviour teaport's endpointing depends on. They exist because
# every one of these properties was broken at some point in pipecat 1.5.0, each failure
# costing a live session: a turn that could not end at all, a turn committed on a timer a
# barge-in cancelled, a turn committed on the previous utterance's words. All were fixed
# upstream in 1.6.0/1.7.0, and the workarounds teaport carried for them were deleted when
# it moved to 1.7.0 — these tests are what makes that deletion safe to keep.
#
# The window that used to matter is ONE audio chunk wide, so the first test walks the
# boundary rather than guessing at it: with stop_secs=0.3 and 20ms chunks, 300ms of
# post-start speech was the exact offset that wedged.
#
# The model is stubbed to vote COMPLETE unconditionally, so any INCOMPLETE the strategy
# acts on can only have come from the empty-buffer path, never from a real verdict.
#
# The STT's p99 is NOT stubbed out: turn_harness delivers the STTMetadataFrame the STT
# service broadcasts live (TEAPORT_TTFS_P99 = 0.8), because the strategy's safety-net
# timer is sized from it and every timer here is 0s without it. That is not a detail —
# test_a_turn_that_starts_from_its_own_final_commits_on_the_p99_timer below exists
# only under the real value, and a harness that omitted it hid that path for two pin
# bumps (git show e983f90).
#
# Run: on the appliance only — see pinned_pipecat.py.
#
import asyncio
import os
import sys

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
)
from teaport_brain.stt import TEAPORT_TTFS_P99  # noqa: E402

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)
STOP_SECS = 0.3          # jetson01's ENDPOINT_STOP_SECS, and the boundary under test


def _safety_net_secs(vad_stop_secs=0.0):
    """How long the stop strategy's no-VAD fallback waits before committing on a timer.

    `timeout = max(0, self._stt_timeout - self._stop_secs)`, where _stt_timeout is the
    STT's broadcast p99 (turn_harness sends the real one) and _stop_secs is whatever the
    LAST VADUserStoppedSpeakingFrame carried — 0.0 if the strategy has seen none, which
    is why the two callers below pass different things."""
    return max(0.0, TEAPORT_TTFS_P99 - vad_stop_secs)


async def _wait_for(predicate, timeout):
    """True as soon as `predicate()` holds, False if it never does within `timeout`.

    Polled rather than slept-through: these assertions are about WHETHER a commit
    arrives, and a fixed sleep long enough to be safe makes the whole file slow."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


class AlwaysCompleteSmartTurn(BaseSmartTurn):
    """Votes COMPLETE whenever it is actually asked."""

    def _predict_endpoint(self, audio_array):
        return {"prediction": 1, "probability": 0.99}


class AlwaysIncompleteSmartTurn(BaseSmartTurn):
    """Votes INCOMPLETE whenever it is actually asked."""

    def _predict_endpoint(self, audio_array):
        return {"prediction": 0, "probability": 0.01}


async def run_turn(speech_ms_after_start, analyzer_cls=AlwaysCompleteSmartTurn,
                   stop_secs=STOP_SECS, silence_ms_after_stop=0):
    """One barge-in-shaped turn. Returns True if it ended on its own.

    The user is already speaking when the first interim transcript lands, which is
    what starts the user turn; the strategy's reset() then clears _vad_user_speaking
    while the mic is still live, so every later chunk counts as silence. The VAD frame
    arrives with no audio chunk after it, which is the phase that wedges.
    """
    analyzer = analyzer_cls(
        sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=stop_secs)
    )
    stop = TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)
    controller = UserTurnController(
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
            stop=[stop],
        ),
        # The 5s force-stop is the symptom, not the cure: disable it so the test sees
        # whether the STRATEGY ended the turn.
        user_turn_stop_timeout=3600,
    )
    stopped = []

    async def ignore(*args, **kwargs):
        pass

    for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                  "on_user_turn_started", "on_user_turn_stop_timeout"):
        controller.add_event_handler(event, ignore)

    async def on_stopped(controller, strategy, params):
        stopped.append(True)

    controller.add_event_handler("on_user_turn_stopped", on_stopped)
    # The context manager, not a bare start + a cleanup line at the end: start() creates
    # the turn-stop watchdog task and only cleanup() reaps it, so an assertion that
    # raises past a trailing cleanup leaves a one-hour timer and a thread pool running
    # into whatever this suite does next. run_turn is called 14 times per event loop.
    async with running_controller(controller):
        analyzer.set_sample_rate(SAMPLE_RATE)

        async def speak(ms):
            for _ in range(ms // CHUNK_MS):
                await controller.process_frame(
                    InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE,
                                       num_channels=1)
                )

        await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await speak(400)
        await controller.process_frame(
            InterimTranscriptionFrame("hello there", "u", "t", None))
        await speak(speech_ms_after_start)
        vad_stop = VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS)
        vad_stop.timestamp = 0.0
        await controller.process_frame(vad_stop)
        final = TranscriptionFrame("hello there", "u", "t", None)
        final.finalized = True
        await controller.process_frame(final)
        await speak(silence_ms_after_stop)  # the mic stays live: silence keeps arriving
        await asyncio.sleep(0.2)
    return bool(stopped)


# The offsets that bracket the one-chunk window, plus the window itself.
OFFSETS = [0, 100, 200, 260, 280, 300, 320, 340, 400, 600, 1000]


async def test_every_offset_ends_its_turn():
    for ms in OFFSETS:
        assert await run_turn(ms), (
            f"turn never ended with {ms}ms of speech after the turn started"
        )


async def test_an_incomplete_verdict_keeps_the_turn_open():
    """Smart Turn's "they are mid-thought" veto must keep the turn open.

    The counterpart to the test above: a turn has to end on its own, but never before
    the user has finished. At 100ms of post-start speech the silence backstop has not
    fired, so the model's INCOMPLETE is the only verdict there is — anything that
    commits here cuts people off mid-sentence."""
    assert not await run_turn(100, analyzer_cls=AlwaysIncompleteSmartTurn), (
        "the latch ended a turn Smart Turn had judged incomplete"
    )


async def test_smart_turns_silence_limit_is_a_ceiling_on_an_incomplete_verdict():
    """SmartTurnParams.stop_secs is not a silence floor: BaseSmartTurn.append_audio
    force-completes the turn once that much silence has accumulated, INCOMPLETE
    verdict or not, and empties its buffer so the model is never asked again.
    Feeding it the VAD's 0.2 floor (commit 7136c88) left a mid-sentence pause one
    audio chunk before it was committed anyway -- the "sole guard" was no guard.
    The brain keeps the two apart: the VAD's ENDPOINT_STOP_SECS asks the question,
    SMARTTURN_STOP_SECS is how long a "not done" is honoured."""
    # The silence has to clear the floor being tested, or the premise arm is vacuous:
    # a hard-coded 400ms demonstrated the old coupling only while ENDPOINT_STOP_SECS was
    # below 0.4, and silently stopped demonstrating anything when 2026-09-09 raised the
    # floor to 0.5 (it then asserted a force-complete that correctly did not happen).
    # Deriving it keeps both arms meaningful at any floor: past the floor, short of the
    # ceiling. At the old 0.2 this is still exactly 400ms.
    silence_ms = int(ENDPOINT_STOP_SECS * 1000) + 200
    assert silence_ms < SMARTTURN_STOP_SECS * 1000, (
        f"the probe silence ({silence_ms}ms) must sit between ENDPOINT_STOP_SECS and "
        f"SMARTTURN_STOP_SECS, or the second arm tests the ceiling firing, not surviving")
    # The premise: at the old coupling, silence past the floor commits over the veto.
    assert await run_turn(100, analyzer_cls=AlwaysIncompleteSmartTurn,
                          stop_secs=ENDPOINT_STOP_SECS, silence_ms_after_stop=silence_ms), (
        "stop_secs did not force-complete over an INCOMPLETE verdict; the ceiling "
        "semantics this split rests on have changed")
    # The fix: at the brain's ceiling the veto survives the same silence.
    assert not await run_turn(100, analyzer_cls=AlwaysIncompleteSmartTurn,
                              stop_secs=SMARTTURN_STOP_SECS,
                              silence_ms_after_stop=silence_ms), (
        f"an INCOMPLETE verdict was overridden within {silence_ms}ms of silence at "
        f"SMARTTURN_STOP_SECS={SMARTTURN_STOP_SECS}")
    assert SMARTTURN_STOP_SECS >= ENDPOINT_STOP_SECS + 0.5, (
        "SMARTTURN_STOP_SECS must leave an INCOMPLETE verdict real room past the VAD "
        "floor, or the model's veto is decorative")


async def test_no_commit_before_this_utterance_has_a_transcript():
    """A turn must not be committed on the previous utterance's words.

    _text is the strategy's "did the user say anything" gate. Anything that satisfies it
    before the CURRENT utterance has been transcribed lets the silence backstop commit
    the turn early — and the aggregator's own buffer is still empty at that point, so
    push_aggregation() returns without asking the model and the user gets nothing back.

    Live on pipecat 1.5.0, 2026-08-20: committed at 22:15:28.157, and the transcript it
    was supposedly for did not arrive until 22:15:28.568."""
    stops = []
    analyzer = AlwaysCompleteSmartTurn(
        sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=STOP_SECS)
    )
    analyzer.set_sample_rate(SAMPLE_RATE)
    controller = UserTurnController(
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)],
        ),
        user_turn_stop_timeout=3600,
    )

    async def ignore(*args, **kwargs):
        pass

    for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                  "on_user_turn_started", "on_user_turn_stop_timeout"):
        controller.add_event_handler(event, ignore)

    async def on_stopped(controller, strategy, params):
        stops.append(True)

    controller.add_event_handler("on_user_turn_stopped", on_stopped)
    async with running_controller(controller):

        async def speak(ms):
            for _ in range(ms // CHUNK_MS):
                await controller.process_frame(
                    InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE,
                                       num_channels=1)
                )

        # An utterance that completes normally, leaving its words in _text. VAD stop
        # first, then the final — the order the live pipeline produces.
        await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await speak(400)
        stop_a = VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS)
        stop_a.timestamp = 0.0
        await controller.process_frame(stop_a)
        first = TranscriptionFrame("four", "u", "t", None)
        first.finalized = True
        await controller.process_frame(first)
        # WAITED for, not assumed: this commit lands on the p99 safety-net timer about
        # half a second later, not on the transcript — see the test below, which is
        # about that timer. The old 0.05s sleep here only ever passed because the
        # harness left _stt_timeout at 0, making every such timer fire instantly.
        safety_net = _safety_net_secs(STOP_SECS)
        assert await _wait_for(lambda: len(stops) == 1, safety_net + 0.5), (
            f"the first utterance should have committed once, got {len(stops)}")

        # A NEW utterance. Its turn starts from an interim, and STT has not finalized it
        # yet — exactly the live shape.
        await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await speak(400)
        await controller.process_frame(
            InterimTranscriptionFrame("the one with", "u", "t", None))
        await speak(300)          # trips the silence backstop
        # Long enough to matter: the early commit this guards against would arrive on
        # the same (p99 - stop_secs) timer as the one above, which the old 0.05s window
        # ended well before. A quiet 50ms proved nothing.
        await asyncio.sleep(safety_net + 0.5)
        assert len(stops) == 1, (
            "turn committed on the previous utterance's text, before its own transcript"
        )


async def test_a_turn_that_starts_from_its_own_final_commits_on_the_p99_timer():
    """A turn started BY its own final transcript commits on a timer, not on the words.

    MinWordsUserTurnStartStrategy starts the turn from that final; the start resets the
    stop strategy, which clears the VAD-stopped state before the strategy has seen the
    final — so it takes the no-VAD fallback and arms a (ttfs_p99 - stop_secs) timer
    instead of committing on the transcript it already has. This is what pipecat 1.7.0
    deliberately replaced "commit on every finalized transcript" with (that caused
    multiple inferences per utterance under MinWords), so it is behaviour to KNOW, not
    to work around.

    It is pinned because it is only visible with the real p99: at ttfs_p99=0 the timer
    fires immediately and this looks like an instant commit. It is also the shape with a
    live edge — a barge-in cancels that timer, and a turn whose only route to a commit
    was the timer then never commits at all (the 1.5.0 bug e983f90's deleted test was
    written for). If this ever starts committing at once again, that is pipecat changing
    its mind, and the fallback in test_no_commit_before_this_utterance_has_a_transcript
    is sized on it."""
    stops = []
    analyzer = AlwaysCompleteSmartTurn(
        sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=STOP_SECS)
    )
    analyzer.set_sample_rate(SAMPLE_RATE)
    controller = UserTurnController(
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)],
        ),
        user_turn_stop_timeout=3600,
    )

    async def ignore(*args, **kwargs):
        pass

    for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                  "on_user_turn_started", "on_user_turn_stop_timeout"):
        controller.add_event_handler(event, ignore)

    async def on_stopped(controller, strategy, params):
        stops.append(asyncio.get_running_loop().time())

    controller.add_event_handler("on_user_turn_stopped", on_stopped)
    async with running_controller(controller):
        # No VADUserStoppedSpeakingFrame in this shape, so the strategy's _stop_secs is
        # still 0 and the timer is the whole p99 rather than p99 - stop_secs.
        safety_net = _safety_net_secs()
        final = TranscriptionFrame("say exactly what is it saying", "u", "t", None)
        final.finalized = True
        started = asyncio.get_running_loop().time()
        await controller.process_frame(final)
        # Deliberately shorter than the timer: a commit inside this window would mean
        # the transcript committed the turn directly.
        await asyncio.sleep(safety_net / 2)
        assert not stops, (
            f"the turn committed {stops[0] - started:.3f}s after its own final — "
            f"pipecat used to do this and stopped in 1.7.0; if it is back, the timer "
            f"this file waits out is no longer the commit path")
        assert await _wait_for(lambda: bool(stops), safety_net + 0.5), (
            f"no commit within {safety_net + 0.5}s of the final: the turn's only route "
            f"to a commit is the safety-net timer, and it did not fire")
        assert stops[0] - started >= safety_net * 0.9, (
            f"committed after {stops[0] - started:.3f}s, sooner than the {safety_net}s "
            f"(ttfs_p99 - stop_secs) timer it should be riding")


def main():
    sync = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    for fn in sync:
        fn()
        print(f"  ok {fn.__name__}")

    async def run_aio():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
