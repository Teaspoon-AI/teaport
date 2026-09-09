#
# teaport — endpointing policy (how long until the bot decides you're done).
#
# Extracted from gateway_server.py; the constants + analyzer live together because
# they ARE the policy: ENDPOINT_STOP_SECS is the VAD's silence floor, SMARTTURN_STOP_SECS
# the ceiling on honouring Smart Turn's "not done", the Smart Turn threshold the
# semantic eagerness, INTERRUPT_MIN_WORDS how much speech counts as a barge-in — and
# keep_barge_in_reachable at the bottom, which is what stops an open user turn from
# swallowing every barge-in there is.
#
import os

from loguru import logger

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

# Endpointing silence, the VAD's: how long the user must pause before Silero VAD
# reports them stopped -- which is what asks Smart Turn for its verdict. History: 0.8
# was conservative, 0.5 trimmed ~0.3s off it; now 0.2. This floor IS the dominant fixed
# latency on every turn (the VAD model itself is ~1ms/frame), and 0.2 is pipecat's
# recommended VAD default, the one its built-in STT p99 latencies assume -- so it also
# silences the turn strategy's stop_secs warning. Tune via ENDPOINT_STOP_SECS.
ENDPOINT_STOP_SECS = float(os.getenv("ENDPOINT_STOP_SECS", "0.2"))

# Smart Turn's OWN silence limit -- pipecat's SmartTurnParams.stop_secs -- is not a
# floor but a CEILING: BaseSmartTurn.append_audio force-completes the turn once this
# much silence has accumulated since the user's last speech, model verdict or not (and
# empties its buffer, so no verdict is asked again). Both used to be fed
# ENDPOINT_STOP_SECS, harmless at 0.5 + 0.5 and broken at 0.2: an INCOMPLETE verdict
# was overridden one audio chunk later, so the "mid-thought protection now rests on
# Smart Turn" that the cut to 0.2 claimed did not exist. This is how long an
# INCOMPLETE verdict keeps the turn open, counted from the start of the silence: the
# user pauses mid-sentence, the model says "not done", and they have until here to
# resume before the turn commits anyway. 1.0 is the protection the old 0.5 + 0.5 gave.
# Tune via SMARTTURN_STOP_SECS.
SMARTTURN_STOP_SECS = float(os.getenv("SMARTTURN_STOP_SECS", "1.0"))

# Smart Turn v3 decides "user is done" when its end-of-turn probability clears this
# threshold; below it the utterance is "incomplete" and we wait out the silence
# (up to SMARTTURN_STOP_SECS before responding). pipecat hardcodes 0.5.
# LOWER = the classifier lets go EASIER / snappier endpointing, at the cost of more
# mid-thought cutoffs; higher = more patient. ENDPOINT_STOP_SECS is when the question
# is asked, SMARTTURN_STOP_SECS how long a "no" is honoured; this is the *semantic*
# eagerness. Tune via SMARTTURN_COMPLETE_THRESHOLD.
SMARTTURN_COMPLETE_THRESHOLD = float(os.getenv("SMARTTURN_COMPLETE_THRESHOLD", "0.5"))

# Silero VAD gates. These were tightened to 0.8 / 0.75 to reject ambient noise,
# but that put the min_volume gate right in the middle of real speech loudness —
# the [VAD] traces showed speech hugging 0.74-0.84, so the detector flickered
# SPEAKING<->STOPPING on normal amplitude dips (felt "over-active", and matters
# more now the STT commit rides VADUserStoppedSpeaking). Back to pipecat's
# defaults (0.7 / 0.6): speech clears the gate with margin -> stable detection.
# pipecat 1.8.0 then integrated the volume measurement over a rolling 400 ms BS.1770
# window (AudioVolumeTracker) instead of per 32 ms VAD frame. That does remove the dip
# source these values were chosen against, and the scale is unchanged -- the two
# normalizations agree to within 0.002 on the same buffer -- so the numbers carry over.
# But it is a LAG, not just a smoother, and BOTH edges of the gate moved. Measured at
# 16 kHz over Silero's 512-sample frames, stable across amplitudes:
#
#   * Leading edge: volume reads 0 until the window holds 400 ms of audio, so the gate
#     cannot pass until ~0.55 s of speech has arrived, against ~0.16 s at 1.7.0. And
#     this is NOT once per session -- sip_server builds a fresh pipeline, and so a
#     fresh AudioVolumeTracker, per CALL (its _bring_up; reset() otherwise fires only
#     on a sample-rate change). Every caller gets it, so one who speaks over or
#     straight after the greeting cannot start a turn or barge in for that half second.
#   * Trailing edge, which moved further and matters more: the window still holds
#     400 ms of speech after the user falls silent, so volume stays above min_volume
#     for ~0.45 s past the end of speech, against ~0.1 s at 1.7.0. This gate is a VETO
#     on a Silero confidence spike, and ~0.35 s more of it is ~0.35 s in which AEC
#     residue, half-duplex echo or line noise can hold SPEAKING and delay
#     VADUserStoppedSpeaking -- and with it the STT commit that now rides that frame.
#
# Left at 0.7 / 0.6 rather than re-tuned, deliberately: the trailing lag is a window
# length, not a threshold, so no value of min_volume removes it. Lowering it buys a
# faster leading edge and pays with a longer trailing one; raising it puts the gate
# back in the middle of real speech loudness, which is the flicker this paragraph
# opens with. Revisit here first if turns start committing late.
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


# Every barge-in in this brain is a USER TURN START: MinWordsUserTurnStartStrategy
# counts the words, the aggregator broadcasts the interruption from
# on_user_turn_started, and there is no other path. So anything that stops a turn
# from starting stops the bot from being interruptible — and pipecat's controller
# refuses to start one while a turn is already open (user_turn_controller.py,
# "Prevent two consecutive user turn starts").
#
# It also refuses to CLOSE one while VAD says the user is audibly speaking ("Never
# finalize while the user is audibly speaking"), which is right for the case it was
# written for: a latent stop signal — an LLM end-of-turn verdict that resolves after
# the user resumed — is stale by the time it lands, and the turn should stay open so
# the next inference re-evaluates.
#
# The two together make `user turn open AND user speaking` an ABSORBING STATE for
# barge-in, and the only way out is the 5s user_turn_stop_timeout — an INACTIVITY
# timer that any VAD frame, any transcript and any audio re-arms. So the recovery
# path needs the user to STOP talking, which is the opposite of what someone does
# when the bot is not responding to them: continuing to speak, or repeating the
# request, holds the turn open instead of clearing it. Measured here: 52
# interjections over 8s, every one >= INTERRUPT_MIN_WORDS, every one logged by the
# strategy as `should_trigger=True`, and not one interruption. Six seconds of
# complete silence clears it. See tests/test_barge_in_survives_an_open_turn.py and formal/UserTurn.tla.
#
# How the turn comes to be open while the bot talks is the other half.
# BaseUserTurnStopStrategy.trigger_user_turn_stopped() is TWO events:
#
#     await self.trigger_user_turn_inference_triggered()   # -> push_aggregation()
#     await self.trigger_user_turn_finalized(...)          # <- refused here
#
# The first is gated on the turn being open, the second on the user being quiet, and
# the first awaits all the way through push_aggregation() -> push_context_frame() ->
# push_frame() across the whole pipeline below the aggregator. When the trigger came
# from the stop strategy's own _timeout_handler TASK, the aggregator's input task is
# free to process a queued VADUserStartedSpeakingFrame in that gap. Inference has
# then run — the user's words are in the context and the LLM is answering them —
# while the finalize is refused and the turn stays open. The bot starts speaking with
# barge-in already dead.
#
# This is not a pipecat regression: the controller is byte-identical on 1.5.0, 1.7.0
# and 1.8.1 (only a comment character moved), so the hazard has been here since the
# initial release. What changed is exposure — e983f90 deleted LatchedTurnStopStrategy,
# whose overrides were the only thing that re-attempted a lost stop; f23503b cut
# ENDPOINT_STOP_SECS 0.5 -> 0.2, multiplying the VAD edges per utterance; and 1.8.1's
# rolling volume window holds `speaking` ~0.35s longer past the end of speech.
#
# The fix below is the smallest one that is TRUE rather than merely effective. It does
# not weaken the guard: a refusal with no inference behind it is still honoured, which
# is the case the guard exists for. It only says that once the inference HAS run, the
# turn's content is already spent — refusing the finalize cannot un-ask the question,
# it can only strand the turn — so that decision is re-applied at the user's very next
# quiet moment (~ENDPOINT_STOP_SECS, against the watchdog's unreachable 5s) instead of
# being dropped. That is the guarantee the deleted latch used to give by accident,
# restored at the level the failure is actually at.
def keep_barge_in_reachable(controller) -> None:
    """Re-apply a turn finalization that was refused after its inference had run.

    Wraps ONE UserTurnController instance (not the class): the brain builds one per
    session, and a class-level patch would reach every pipecat user in the process.

    Deliberately NOT defensive about the private names it binds. If a future pipecat
    renames _trigger_user_turn_start / _trigger_user_turn_inference_triggered /
    _trigger_user_turn_stop / _user_turn / _user_speaking, this raises AttributeError
    at session build — loudly, on the first call, in CI — which is the outcome to
    want. Silently doing nothing would restore a bug whose whole signature is that
    nothing is logged when it happens.

    Known limit: the re-attempt rides the frame that reports the user quiet, so it
    needs VAD to report a stop at all. It does NOT rescue a VAD frozen in SPEAKING —
    SIP_HALF_DUPLEX left on, or a desynced gateway AEC feeding the bot's own audio
    back in. Those kill barge-in on their own, upstream of any of this, and papering
    over them here would only hide them.
    """
    trigger_start = controller._trigger_user_turn_start
    trigger_inference = controller._trigger_user_turn_inference_triggered
    trigger_stop = controller._trigger_user_turn_stop
    process_frame = controller.process_frame

    # inference: this turn's words have already been pushed to the context and the LLM
    # asked. owed: (strategy, params) of a finalization the controller refused after
    # that, to be re-applied at the next quiet moment.
    turn = {"inference": False, "owed": None}

    async def _start(strategy, params):
        was_open = controller._user_turn
        await trigger_start(strategy, params)
        # Only on a turn that actually STARTED. The controller drops a start while one
        # is open, and clearing here on that no-op would forget the owed finalization
        # of the very turn that is stuck.
        if not was_open and controller._user_turn:
            turn["inference"], turn["owed"] = False, None

    async def _inference(strategy):
        was_open = controller._user_turn
        await trigger_inference(strategy)
        if was_open:
            turn["inference"] = True

    async def _stop(strategy, params):
        was_open = controller._user_turn
        await trigger_stop(strategy, params)
        # Still open after a stop that had a turn to close = refused. `_user_speaking`
        # is the only reason the controller has for that.
        refused = was_open and controller._user_turn
        if refused and turn["inference"]:
            turn["owed"] = (strategy, params)
        elif not controller._user_turn:
            turn["owed"] = None

    async def _process_frame(frame):
        await process_frame(frame)
        # AFTER pipecat's own handling: this frame may be the VADUserStoppedSpeaking
        # that cleared _user_speaking, and the stop strategies have already seen it.
        owed = turn["owed"]
        if owed and controller._user_turn and not controller._user_speaking:
            turn["owed"] = None
            logger.debug(f"{controller}: re-applying the turn finalization refused "
                         "while the user was speaking — an open turn takes no barge-in")
            await trigger_stop(*owed)

    controller._trigger_user_turn_start = _start
    controller._trigger_user_turn_inference_triggered = _inference
    controller._trigger_user_turn_stop = _stop
    controller.process_frame = _process_frame
