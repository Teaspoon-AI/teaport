#
# Unit test: the bot can ALWAYS be interrupted, however the last turn ended.
#
# test_endpointing.py pins the other half of the turn contract — "the turn always
# ends, and never before the user has finished" — and every case in it disables the
# 5 s force-stop to watch the STRATEGY. Nothing pinned the START side, and the start
# side is the whole of barge-in: MinWordsUserTurnStartStrategy counts the words, the
# aggregator broadcasts the interruption from on_user_turn_started, and there is no
# other path. Every InterruptionFrame elsewhere in this suite is hand-fed straight
# into the ledger or the playout under test, so nothing here ever drove the machinery
# that PRODUCES one.
#
# The failure that gap hid: pipecat refuses to start a turn while one is open, and
# refuses to close one while VAD says the user is speaking, so `open AND speaking`
# is an absorbing state for barge-in. The bot keeps speaking; every interjection is
# logged by the strategy as should_trigger=True and dropped by the controller; and the
# 5 s user_turn_stop_timeout that is meant to recover it is an INACTIVITY timer that
# further speech re-arms, so continuing to talk holds it open rather than clearing it.
# It gets in when trigger_user_turn_stopped()'s two awaits straddle a VAD start:
# inference has run — the words are in the context, the LLM is answering — and the
# finalize is refused.
#
# teaport_brain.endpointing.keep_barge_in_reachable re-applies that refused
# finalization at the user's next quiet moment. Both halves are pinned below: the
# fixed controller must interrupt, and the STOCK one must still wedge — if pipecat
# ever fixes this upstream, test_the_stock_controller_still_wedges goes red and the
# wrapper can go with it. (That is what test_stock_strategy_still_wedges did for
# LatchedTurnStopStrategy, and it is why deleting the latch at e983f90 was safe to
# keep for the three bugs 1.7.0 really had fixed — and unsafe for this one, which
# lives in the controller rather than the strategy and was never covered.)
#
# Run: python test_barge_in_survives_an_open_turn.py  (or via pytest test_suite.py)
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
    BotStartedSpeakingFrame,
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
    keep_barge_in_reachable,
)

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)

# Production's 5 s. NOT raised out of the way as test_endpointing.py raises it: here
# the watchdog is part of what is under test — it is the escape hatch that is supposed
# to make the wedge survivable, and the point is that further speech starves it.
STOP_TIMEOUT_SECS = 5.0

# Long enough that the watchdog would have fired several times over if anything ever
# let it, short enough to keep the script under the suite's timeout.
INTERJECT_SECS = 8.0

# Transcript fixtures for the interjections. Neutral filler on purpose: only the WORD
# COUNT is load-bearing, and each clears INTERRUPT_MIN_WORDS with room to spare so the
# guard is never the reason nothing happens — the strategy logs should_trigger=True for
# every one of them. Three of them rather than one so successive interims differ, as
# they would live.
INTERJECTIONS = ["one two three", "four five six", "seven eight nine"]

# The utterance whose turn gets stranded. "hello there" is test_endpointing.py's
# placeholder; the content is never read, only its length and its arrival order.
UTTERANCE = "hello there"


class Incomplete(BaseSmartTurn):
    """Votes 'they are mid-thought' — the realistic verdict at a 0.2 s pause.

    Deliberately not AlwaysComplete: this test must not depend on the model getting
    the end of the turn WRONG. Here it gets it right and the turn still strands, on
    BaseSmartTurn.append_audio's own silence backstop at SMARTTURN_STOP_SECS.
    """

    def _predict_endpoint(self, audio_array):
        return {"prediction": 0, "probability": 0.2}


class Rig:
    """A real UserTurnController with the brain's real strategies, and a count of
    the two things that matter: turn starts (each one an interruption in production)
    and turn stops."""

    def __init__(self, *, patched: bool):
        analyzer = Incomplete(
            sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=SMARTTURN_STOP_SECS)
        )
        analyzer.set_sample_rate(SAMPLE_RATE)
        self.controller = UserTurnController(
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)],
            ),
            user_turn_stop_timeout=STOP_TIMEOUT_SECS,
        )
        if patched:
            keep_barge_in_reachable(self.controller)
        self.starts, self.stops, self.inferences = [], [], []
        # The user resumes speaking inside push_aggregation()'s await — see the header.
        # Armed for one inference only; production's window is however long the event
        # loop takes to carry an LLMContextFrame across the pipeline, which on the
        # appliance is measured in hundreds of ms under CUDA contention.
        self.resume_in_the_gap = False

        async def ignore(*args, **kwargs):
            pass

        for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                      "on_user_turn_stop_timeout"):
            self.controller.add_event_handler(event, ignore)

        async def on_started(_c, _s, _p):
            self.starts.append(True)

        async def on_stopped(_c, _s, _p):
            self.stops.append(True)

        async def on_inference(_c, _s):
            self.inferences.append(True)
            if self.resume_in_the_gap:
                self.resume_in_the_gap = False
                await asyncio.sleep(0)
                await self.controller.process_frame(
                    VADUserStartedSpeakingFrame(start_secs=0.2))
                await asyncio.sleep(0)

        self.controller.add_event_handler("on_user_turn_started", on_started)
        self.controller.add_event_handler("on_user_turn_stopped", on_stopped)
        self.controller.add_event_handler("on_user_turn_inference_triggered", on_inference)

    async def speak(self, ms):
        """Mic audio. It keeps arriving whether or not the user is talking — the live
        shape, and what makes the analyzer's silence backstop reachable."""
        for _ in range(ms // CHUNK_MS):
            await self.controller.process_frame(
                InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1)
            )

    async def strand_a_turn(self):
        """Drive the exact entry: a turn opened from an interim, a pause the analyzer
        judges incomplete, the final for it, then the silence backstop firing the stop
        while the user resumes inside the inference's await."""
        await self.controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await self.speak(400)
        await self.controller.process_frame(
            InterimTranscriptionFrame(UTTERANCE, "u", "t", None))
        await self.speak(300)
        await self.controller.process_frame(
            VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        final = TranscriptionFrame(UTTERANCE, "u", "t", None)
        final.finalized = True
        await self.controller.process_frame(final)
        self.resume_in_the_gap = True
        await self.speak(int(SMARTTURN_STOP_SECS * 1000) + 200)
        await asyncio.sleep(0.05)

    async def interject_for(self, secs):
        """Repeated attempts to interrupt, with the ordinary pauses between phrases.

        The pauses matter: they are what makes each attempt a fresh VAD start/stop
        cycle, and what re-arms the stop watchdog. Returns how many attempts it made."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + secs
        attempts = 0
        while loop.time() < deadline:
            await self.controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
            await self.controller.process_frame(
                InterimTranscriptionFrame(
                    INTERJECTIONS[attempts % len(INTERJECTIONS)], "u", "t", None))
            await self.speak(600)
            await self.controller.process_frame(
                VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
            await self.speak(300)
            await asyncio.sleep(0.15)
            attempts += 1
        return attempts


async def test_an_interjection_over_the_bot_always_reaches_a_turn_start():
    """The regression test. A turn stranded by a refused finalization must not cost
    the user their ability to interrupt."""
    rig = Rig(patched=True)
    async with running_controller(rig.controller, sample_rate=SAMPLE_RATE):
        await rig.strand_a_turn()
        assert rig.inferences, (
            "the scenario never ran the inference, so it is not testing the wedge — "
            "check SMARTTURN_STOP_SECS against the silence this feeds"
        )
        # The bot is now saying whatever that inference produced.
        await rig.controller.process_frame(BotStartedSpeakingFrame())
        before = len(rig.starts)
        attempts = await rig.interject_for(INTERJECT_SECS)
        assert attempts, "the interjection loop never ran"
        assert len(rig.starts) > before, (
            f"{attempts} interjections of >= {INTERRUPT_MIN_WORDS} words over "
            f"{INTERJECT_SECS}s produced no user turn start, so no interruption: the "
            f"bot stays uninterruptible for as long as the user goes on speaking"
        )


async def test_the_refused_finalization_is_re_applied_not_dropped():
    """The mechanism, not just its symptom: the stranded turn must actually close.

    Asserted separately because a barge-in that only worked by starting a SECOND turn
    on top of a permanently open first one would satisfy the test above while leaving
    the aggregator's turn bookkeeping broken underneath it."""
    rig = Rig(patched=True)
    async with running_controller(rig.controller, sample_rate=SAMPLE_RATE):
        await rig.strand_a_turn()
        # The refusal happens first — the user IS speaking at that instant, and the
        # guard that refuses it is one this fix deliberately does not weaken.
        assert rig.controller._user_turn, (
            "the finalization was not refused, so this scenario no longer reproduces "
            "the entry path — pipecat may have changed trigger_user_turn_stopped()"
        )
        await rig.interject_for(1.5)
        assert rig.stops, (
            "the refused finalization was never re-applied: the turn is still open "
            "after the user has been quiet, which is the absorbing state itself"
        )


async def test_a_refusal_with_no_inference_behind_it_is_still_honoured():
    """The guard pipecat wrote must survive: a stop signal that arrives stale, with
    no inference behind it, still may not end a turn while the user is speaking.

    This is the case the guard exists for, and the fix must be narrower than it. The
    watchdog's own force-stop is the shape — trigger_user_turn_stop(None, ...) with
    no inference_triggered ahead of it."""
    from pipecat.turns.user_stop import UserTurnStoppedParams

    rig = Rig(patched=True)
    async with running_controller(rig.controller, sample_rate=SAMPLE_RATE):
        await rig.controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await rig.speak(400)
        await rig.controller.process_frame(
            InterimTranscriptionFrame(UTTERANCE, "u", "t", None))
        assert rig.controller._user_turn, "the turn should be open"
        # A latent stop landing while the user is still audibly speaking.
        await rig.controller._trigger_user_turn_stop(
            None, UserTurnStoppedParams(enable_user_speaking_frames=True))
        assert rig.controller._user_turn and not rig.stops, (
            "a stale stop ended a turn while the user was speaking — the fix must not "
            "widen the guard, only re-apply decisions whose inference already ran"
        )
        # And it stays refused across the user going quiet: nothing is owed, so there
        # is nothing to re-apply. The turn ends the ordinary way or not at all.
        await rig.controller.process_frame(
            VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        await rig.speak(100)
        assert not rig.stops, (
            "a refusal with no inference behind it was re-applied at the next quiet "
            "moment; the fix is meant to be narrower than the guard"
        )


async def test_the_stock_controller_still_wedges():
    """The counterpart, and the deletion notice for the wrapper above.

    Written to detect the moment pipecat fixes this upstream. While it passes, the
    hazard is real and keep_barge_in_reachable is load-bearing. When it FAILS, the
    stock controller has stopped stranding the turn — and the wrapper, this test, and
    the endpointing.py essay above it can all go.

    Verified identical on pipecat 1.5.0, 1.7.0 and 1.8.1: the controller's
    "Prevent two consecutive user turn starts" / "Never finalize while the user is
    audibly speaking" pair, and the inactivity timer any user frame re-arms, are
    byte-for-byte the same across all three.
    """
    rig = Rig(patched=False)
    async with running_controller(rig.controller, sample_rate=SAMPLE_RATE):
        await rig.strand_a_turn()
        await rig.controller.process_frame(BotStartedSpeakingFrame())
        before = len(rig.starts)
        attempts = await rig.interject_for(INTERJECT_SECS)
        assert len(rig.starts) == before, (
            f"the STOCK controller interrupted after {attempts} interjections — "
            f"pipecat appears to have fixed the stranded turn upstream. Check it, "
            f"then delete keep_barge_in_reachable and this test with it"
        )
        assert not rig.stops, (
            "the stock controller closed the stranded turn on its own; the "
            "user_turn_stop_timeout is supposed to be starved by the continuing speech"
        )


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run_aio():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
