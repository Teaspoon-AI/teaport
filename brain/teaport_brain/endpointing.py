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
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)

# Endpointing silence, the VAD's: how long the user must pause before Silero VAD
# reports them stopped -- which is what asks Smart Turn for its verdict. This floor IS
# the dominant fixed latency on every turn (the VAD model itself is ~1ms/frame).
# History: 0.8 was conservative, 0.5 trimmed ~0.3s off it, f23503b cut it to 0.2, and
# 2026-09-09 put it back to 0.5.
#
# 0.2 is pipecat's recommended VAD default -- but that recommendation assumes Smart Turn
# is doing the semantic work of deciding whether the user is finished, so that asking
# early is safe. Measured on a live SIP call 2026-09-09, it is not: the model scored
# "How would a DGX" at 0.9759 and 0.9685, and a bare "Hey," at 0.9605, while the one
# utterance it called INCOMPLETE was "compare again." at 0.0303. Its output is close to
# uncorrelated with whether the phrase is actually finished, on this audio. 33 of the
# 37 verdicts on the preceding call were COMPLETE.
#
# With that premise gone, the floor has to do the job by itself: at 0.5 the ~0.2s breath
# before the next word never asks the question at all, and the user's sentence survives.
# See SMARTTURN_COMPLETE_THRESHOLD below for why the semantic knob cannot substitute.
#
# The likely root cause is upstream of this file and not fixed here: Smart Turn v3 judges
# the waveform, and this is 8 kHz telephony band upsampled to 16 kHz, which is not what it
# was trained on. If that is ever addressed, 0.2 becomes safe again and ~0.3s comes off
# every turn -- so revisit this together with the analyzer, not alone.
# Tune via ENDPOINT_STOP_SECS.
ENDPOINT_STOP_SECS = float(os.getenv("ENDPOINT_STOP_SECS", "0.5"))

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
# eagerness.
#
# It is also, on telephony audio, close to INERT -- know this before reaching for it.
# The probabilities are bimodal and extreme (2026-09-09, thr raised 0.5 -> 0.7 as an
# experiment): five of six decisions landed at 0.911-0.976 and the sixth at 0.030, so
# the raise changed exactly zero verdicts. Sparing the mid-phrase cuts would need a
# threshold above 0.976, at which point almost nothing is ever COMPLETE and every turn
# falls through to SMARTTURN_STOP_SECS -- that is not tuning the classifier, it is
# disabling it and paying a flat second for it. The knob only bites where the model is
# uncertain, and here it is confidently wrong instead. ENDPOINT_STOP_SECS is the lever
# that works; see its note. Tune via SMARTTURN_COMPLETE_THRESHOLD.
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
        p = result["probability"]
        prediction = 1 if p > self._complete_threshold else 0
        # The NUMBER, not just the verdict. pipecat logs "End of Turn result: COMPLETE"
        # and puts the probability behind logger.trace, so a live journal shows every
        # decision and none of the evidence — and the one knob that moves these
        # decisions (SMARTTURN_COMPLETE_THRESHOLD) could only ever be guessed at.
        # Live 2026-09-09: "How would a DGX" was judged COMPLETE and answered as a
        # question; the threshold sat at pipecat's stock 0.5 with no idea whether that
        # utterance scored 0.51 or 0.99, which is the difference between a threshold
        # that would have saved it and one that could not.
        #
        # `margin` is what a re-threshold has to cross to change THIS decision, so a
        # journal of a bad call sizes the change directly: every mid-phrase cut with a
        # small positive margin is one a higher threshold would have kept open.
        logger.debug(
            f"smart-turn p={p:.4f} thr={self._complete_threshold:.2f} "
            f"-> {'COMPLETE' if prediction else 'INCOMPLETE'} "
            f"(margin {p - self._complete_threshold:+.4f})"
        )
        result["prediction"] = prediction
        return result


class LateStartTurnStopStrategy(TurnAnalyzerUserTurnStopStrategy):
    """The stock stop strategy, minus a reset that costs every barge-in 0.45 s.

    A turn on this brain starts from a TRANSCRIPT (MinWordsUserTurnStartStrategy is
    the only start strategy), and while the bot is speaking the transcriber returns
    nothing for the caller's words until the VAD stop flushes the segment
    (teagram-engine#7: the model does not hear one voice through another). So every
    successful barge-in
    has the same shape: VAD stop, verdict, THEN the interim that opens the turn,
    ~0.2 s later, then the final.

    pipecat resets the stop strategy at every turn start (handle_user_turn_started
    -> _reset), which is right when the start precedes the stop -- the state is the
    previous turn's -- and wrong here: it throws away the VAD stop and the COMPLETE
    verdict the analyzer had just reached FOR THIS UTTERANCE. The final that follows
    then lands in the strategy's "transcript without VAD" fallback, which waits out
    the STT safety net (ttfs_p99 - stop_secs = 0.8 - 0.35 = 0.45 s) before it will
    commit. Measured live 2026-09-10: 7 of 58 turn commits sat 0.65-0.71 s after the
    VAD stop instead of ~0.24 s, and every one was an "Okay, stop." / "Stop." spoken
    over the bot -- the turns where the caller is already waiting on us.

    The rule: if the user is silent and the VAD stop for this utterance has already
    been seen, the pending verdict is about this utterance, so a turn opening now
    keeps it and the final commits at once. A VAD start in between clears the verdict
    on its own (_discard_pending_end_of_turn), so a stale one cannot survive into a
    new utterance. An INCOMPLETE verdict is kept just the same, and the analyzer's
    silence ceiling (SMARTTURN_STOP_SECS) ends the turn as it always did.
    """

    async def handle_user_turn_started(self):
        if self._vad_stopped and not self._vad_user_speaking:
            logger.debug(
                f"{self}: turn opened after its own VAD stop -- keeping the end-of-turn "
                f"verdict (complete={self._turn_complete}) instead of resetting"
            )
            # What _reset() would have cleared besides the verdict. The previous turn's
            # stop already cleared it, so this is belt-and-braces, not a behaviour.
            self._text = ""
            return
        await super().handle_user_turn_started()


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


# --- Silero's inference rate -----------------------------------------------------
# Silero VAD v5 runs at 8 kHz or 16 kHz and is a WAVEFORM model: what it sees is the
# spectrum, not an abstraction of it. On this SIP path the media is G.711 mu-law at
# 8 kHz — verified in the SDP, where the carrier's own offer is
# "m=audio 31720 RTP/AVP 0 18 101" (PCMU, G729, telephone-event) with no wideband codec
# in it at all, so an inbound call cannot be anything else. pjmedia decodes that and
# resamples to 16 kHz for our port, and everything downstream is handed "16 kHz" audio
# with no energy whatever above 4 kHz.
#
# That is not the signal Silero was trained on, and it is the leading explanation for
# the one thing no threshold could fix: measured 2026-09-09 over a full call, speech sits
# at p50 0.88 while ~8% of genuine speech frames collapse into the same 0.00-0.05 band as
# silence, so no gate above zero separates them (VAD_CONFIDENCE 0.70 -> 0.35 -> 0.15
# changed the flicker rate by nothing).
#
# VAD_SAMPLE_RATE=8000 hands Silero the TRUE narrowband signal instead. Decimating back
# to 8 kHz discards no information when the source was 8 kHz to begin with — it undoes
# the gateway's upsampling rather than degrading anything — and 512 samples at 16 kHz
# decimate to exactly the 256 Silero wants at 8 kHz, so pipecat's buffering is untouched.
#
# Off by default: it is a real behaviour change to the VAD and it is only correct while
# the carrier is narrowband. On a genuinely wideband trunk it would throw away the top
# half of the band, and the pair-averaging below (a crude half-band filter, kept cheap
# because it runs per frame in the VAD executor) would not fully prevent aliasing.
# Revisit this together with the SDP offer if a wideband carrier ever lands.
VAD_SAMPLE_RATE = int(os.getenv("VAD_SAMPLE_RATE", "16000"))


class NarrowbandSileroMixin:
    """Run Silero at 8 kHz on audio the gateway upsampled from 8 kHz.

    Mixed in ahead of SileroVADAnalyzer so `super()` still reaches the real model; the
    analyzer keeps its 16 kHz identity (pipecat's frame budget and every other rate in
    the pipeline are unchanged) and only the model's input is converted.
    """

    def voice_confidence(self, buffer) -> float:
        import numpy as np
        a = np.frombuffer(buffer, dtype=np.int16)
        if a.size < 2:
            return super().voice_confidence(buffer)
        # Average adjacent pairs, then keep one of each: decimation with a cheap
        # half-band lowpass in front of it, rather than dropping samples outright.
        if a.size % 2:
            a = a[:-1]
        half = a.reshape(-1, 2).mean(axis=1).astype(np.int16)
        # _sample_rate, not sample_rate: the latter is a READ-ONLY property on
        # pipecat's VADAnalyzer, so assigning it raises. Deployed 2026-09-10 and it
        # threw once per audio frame -- 3285 times in one call -- which killed the VAD
        # entirely: no state transitions, no census, and above all no VAD stop, so the
        # stt backstop ended 12 of 12 turns at 1.5s each and the caller felt it as a
        # latency regression. The exception was swallowed into an ErrorFrame by the
        # aggregator rather than crashing anything, which is why it read as "slow"
        # instead of "broken".
        prev, self._sample_rate = self._sample_rate, 8000
        try:
            return super().voice_confidence(half.tobytes())
        finally:
            self._sample_rate = prev
