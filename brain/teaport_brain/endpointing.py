#
# teaport — endpointing policy (how long until the bot decides you're done).
#
# Extracted from gateway_server.py; the constants + analyzer live together because
# they ARE the policy: ENDPOINT_STOP_SECS is the silence floor, the Smart Turn
# threshold is the semantic eagerness.
#
import os

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

# Endpointing silence: how long the user must pause before we treat the turn as done
# and start responding. Applied to BOTH Silero VAD (raw silence detection) and Smart
# Turn v3 (the neural end-of-turn classifier that then confirms it). History: 0.8 was
# conservative, 0.5 trimmed ~0.3s off it; now 0.2. This silence floor IS the dominant
# fixed latency on every turn (the VAD model itself is ~1ms/frame), and 0.2 is what
# pipecat's STT p99 benchmark assumes — so it also silences the turn strategy's stop_secs
# warning. At 0.2 the floor no longer does the mid-thought protection: Smart Turn's
# INCOMPLETE verdict is now the sole guard against clipping a genuine pause, and the cost
# is more premature commits when that verdict is wrong. Tune via ENDPOINT_STOP_SECS.
ENDPOINT_STOP_SECS = float(os.getenv("ENDPOINT_STOP_SECS", "0.2"))

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
