#
# teaport — endpointing policy (how long until the bot decides you're done).
#
# Extracted from gateway_server.py; the constants + analyzer live together because
# they ARE the policy: ENDPOINT_STOP_SECS is the silence floor, the Smart Turn
# threshold is the semantic eagerness.
#
import os

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)

# Endpointing silence: how long the user must pause before we treat the turn as done
# and start responding. Applied to BOTH Silero VAD (raw silence detection) and Smart
# Turn v3 (the neural end-of-turn classifier that then confirms it). 0.8 was conservative;
# 0.5 trims ~0.3s off every turn's perceived latency while Smart Turn still guards against
# cutting the user off mid-thought. The VAD *model* isn't the cost (Silero is ~1ms/frame) —
# this silence policy is. Tune via ENDPOINT_STOP_SECS.
ENDPOINT_STOP_SECS = float(os.getenv("ENDPOINT_STOP_SECS", "0.5"))

# Smart Turn v3 decides "user is done" when its end-of-turn probability clears this
# threshold; below it the utterance is "incomplete" and we wait out the silence
# fallback (~ENDPOINT_STOP_SECS longer before responding). pipecat hardcodes 0.5.
# LOWER = the classifier lets go EASIER / snappier endpointing, at the cost of more
# mid-thought cutoffs; higher = more patient. ENDPOINT_STOP_SECS is the silence floor
# (clip protection); this is the *semantic* eagerness. Tune via
# SMARTTURN_COMPLETE_THRESHOLD.
SMARTTURN_COMPLETE_THRESHOLD = float(os.getenv("SMARTTURN_COMPLETE_THRESHOLD", "0.5"))

# Silero VAD gates. These were tightened to 0.8 / 0.75 to reject ambient noise,
# but that put the min_volume gate right in the middle of real speech loudness —
# the [VAD] traces showed speech hugging 0.74-0.84, so the detector flickered
# SPEAKING<->STOPPING on normal amplitude dips (felt "over-active", and matters
# more now the STT commit rides VADUserStoppedSpeaking). Back to pipecat's
# defaults (0.7 / 0.6): speech clears the gate with margin -> stable detection.
# Tune via VAD_CONFIDENCE / VAD_MIN_VOLUME.
VAD_CONFIDENCE = float(os.getenv("VAD_CONFIDENCE", "0.7"))
VAD_MIN_VOLUME = float(os.getenv("VAD_MIN_VOLUME", "0.6"))

# Barge-in guard. WHILE THE BOT IS SPEAKING, require the user's interrupting speech
# to reach this many transcribed words before it counts as a real turn and cuts the
# reply. A single-word STT garble or noise blip (a mis-heard cough) then can't
# truncate the bot mid-sentence and make the LLM re-answer (the "speech isn't
# in chat" + "repeats itself" symptoms — an interrupted reply is spoken but not
# charted, and the re-run regenerates it). When the bot is NOT speaking the strategy
# self-relaxes to 1 word, so it never delays a normal turn. 1 disables the guard.
# 2, not 3: pipecat counts split() tokens, and the NATURAL stop command is two of
# them ("Okay, stop." / "please stop") — at 3 the bot talked straight through it
# until the user repeated themselves (observed live 2026-07-21, 4.1s to cut). The
# cost is that a two-word garble can now barge; accepted for a responsive stop.
# Tune via TEAPORT_INTERRUPT_MIN_WORDS.
INTERRUPT_MIN_WORDS = int(os.getenv("TEAPORT_INTERRUPT_MIN_WORDS", "2"))


class EagerSmartTurnAnalyzer(LocalSmartTurnAnalyzerV3):
    """Smart Turn v3 with a tunable end-of-turn probability threshold.

    pipecat's model declares Complete at a fixed probability > 0.5. We re-threshold
    the same ONNX output so the turn can be called done sooner (lower threshold)
    without retraining or touching inference. Override runs in the analyzer's
    executor thread, exactly like the parent's _predict_endpoint."""

    def __init__(self, *, complete_threshold: float = 0.5, **kwargs):
        super().__init__(**kwargs)
        self._complete_threshold = complete_threshold

    def _predict_endpoint(self, audio_array):
        result = super()._predict_endpoint(audio_array)
        result["prediction"] = 1 if result["probability"] > self._complete_threshold else 0
        return result


class LatchedTurnStopStrategy(TurnAnalyzerUserTurnStopStrategy):
    """Make sure a turn the user has finished always ends on its own.

    Everything in this class defends one invariant, because everything that breaks it
    fails the same way. The base strategy commits a turn only when
    _maybe_trigger_user_turn_stopped() finds BOTH a transcript and a completed turn, and
    nothing re-runs that check unprompted. Lose either fact and the turn can never end:
    the aggregator force-stops it 5s later on user_turn_stop_timeout, and THAT PATH RUNS
    NO INFERENCE. The user gets no reply at all rather than a slow one, which is why
    every one of these reads as the agent ignoring them. In the journal it is
    "User stopped speaking (strategy: None)" — the one line worth alerting on. Any VAD
    or transcription frame re-arms that timeout too, so on a live mic in a room with
    noise the turn hangs indefinitely instead of for five seconds.

    Four ways the pair gets lost, one per override below, all measured live on
    2026-08-20 and each pinned by a test in tests/test_endpointing.py:

      reset                            one utterance starting a second turn
      process_frame                    a late interim of an already-final utterance
      _handle_vad_user_stopped_speaking  a re-analysis revoking the silence backstop
      _handle_transcription            a commit left to a timer a barge-in cancels

    Behaviour verified against the appliance's pipecat 1.5.0; the same code paths are
    present in 0.0.108.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # What we already know about the utterance being spoken right now, kept across
        # the resets that a mid-utterance turn start causes. Cleared when the VAD says a
        # genuinely new utterance has begun.
        self._carried = None

    async def reset(self):
        """Keep what we know about the current utterance across a restart.

        One utterance can start TWO user turns. MinWordsUserTurnStartStrategy fires on
        INTERIM transcripts, so a later interim of an utterance whose turn has already
        committed starts a second turn -- and starting a turn resets this strategy,
        discarding the text and the completion the first turn established.

        That second turn can never recover them. The stop strategy ignores interim
        frames, so only a final TranscriptionFrame could set _text again, and the turn
        start broadcasts an interruption that flushes the queued frames -- including the
        final that was right behind the interim. _text stays empty for good.

        Live 2026-08-20 21:00:52: a 15-word interim committed turn A and started the
        model; the 16-word interim of the SAME sentence arrived 313ms later, started
        turn B, cancelled turn A's completion, and was force-stopped at 21:00:57.591 as
        "strategy: None" having never reached the model.

        Carrying is bounded by the utterance: VADUserStartedSpeakingFrame drops it, so
        nothing survives into a new one.
        """
        if self._text or self._turn_complete:
            self._carried = (self._text, self._turn_complete, self._transcript_finalized)
        await super().reset()
        if self._carried:
            self._text, self._turn_complete, self._transcript_finalized = self._carried

    async def process_frame(self, frame: Frame):
        """Let a late interim of an already-final utterance end its turn.

        reset() restores what we knew, but nothing would act on it: the base strategy
        ignores interim frames entirely, so the turn that a late interim starts has no
        event left that would call _maybe_trigger_user_turn_stopped(). The controller
        runs start strategies before stop strategies, so by the time this runs the turn
        has started and been reset, and the carried state is back in place.

        Gated on _transcript_finalized, which is only true once the final for THIS
        utterance has been seen -- so this can never commit a turn mid-sentence, and an
        ordinary interim stream is untouched.
        """
        result = await super().process_frame(frame)
        if isinstance(frame, InterimTranscriptionFrame) and self._transcript_finalized:
            await self._maybe_trigger_user_turn_stopped()
        return result

    async def _handle_vad_user_started_speaking(self, frame: VADUserStartedSpeakingFrame):
        # A new utterance owes nothing to the last one.
        self._carried = None
        await super()._handle_vad_user_started_speaking(frame)

    async def _handle_vad_user_stopped_speaking(self, frame: VADUserStoppedSpeakingFrame):
        """Do not let a vacuous re-analysis revoke the silence backstop.

        The turn is decided in two places and the second can undo the first.
        BaseSmartTurn.append_audio counts silence itself; at stop_secs it returns
        COMPLETE *and clears its own audio buffer*, and the strategy sets _turn_complete.
        super() then calls analyze_end_of_turn() again on that emptied buffer, so
        _process_speech_segment returns (INCOMPLETE, None) without the model running at
        all, and `_turn_complete = state == COMPLETE` puts it back to False.

        Measured 2026-08-20: 20 of 246 turns committed at 5.8-6.2s instead of the usual
        0.85s, a bimodal split with nothing in between. The race window is ONE audio
        chunk wide — the buffer clear has to land between the last InputAudioRawFrame
        and the VAD frame — so anything that shifts the phase between those two changes
        how often it is hit, which is what makes it look environmental.

        The silence timeout is the backstop, so it is authoritative: an analysis that
        produced no prediction is not evidence about the turn and may not revoke it.
        """
        completed = self._turn_complete
        await super()._handle_vad_user_stopped_speaking(frame)
        if completed and not self._turn_complete:
            self._turn_complete = True
            # The final transcript may already be in hand, in which case this ends the
            # turn now rather than waiting out the STT fallback timeout super() armed.
            await self._maybe_trigger_user_turn_stopped()

    async def _handle_transcription(self, frame: TranscriptionFrame):
        """Act on a finalized transcript now, rather than on a timer that can be lost.

        When a turn starts FROM a final transcript -- the user answers while the bot is
        still speaking, so MinWordsUserTurnStartStrategy fires on that transcript -- the
        turn start resets this strategy first, wiping the completion the VAD stop had
        already established. super() then takes its no-VAD fallback, sets _turn_complete
        again, and arms a timer for (stt_p99 - stop_secs) instead of committing. The
        transcript is already final, so there is nothing left to wait for, and the wait
        is where the turn is lost: that same transcript starts the user turn, the user
        turn broadcasts an interruption, and the interruption takes the timer with it.

        Live 2026-08-20, three times in ninety seconds. "say exactly what is it saying."
        landed at 20:50:46.983, started the turn at .987, cancelled the previous
        completion at .992, and was force-stopped at 20:50:51.988 as "strategy: None"
        having never reached the model.

        Triggering here is safe: _maybe_trigger_user_turn_stopped() still returns early
        unless the turn is complete AND the transcript is final, so a Smart Turn
        INCOMPLETE verdict keeps the turn open exactly as before.
        """
        await super()._handle_transcription(frame)
        if frame.finalized:
            await self._maybe_trigger_user_turn_stopped()
