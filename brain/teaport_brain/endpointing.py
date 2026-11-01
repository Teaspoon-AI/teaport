#
# teaport — endpointing policy (how long until the bot decides you're done).
#
# Extracted from gateway_server.py; the constants + analyzer live together because
# they ARE the policy: ENDPOINT_STOP_SECS is the VAD's silence floor, SMARTTURN_STOP_SECS
# the ceiling on honouring Smart Turn's "not done", the Smart Turn threshold the
# semantic eagerness, INTERRUPT_MIN_WORDS how much speech counts as a barge-in — and
# keep_barge_in_reachable at the bottom, which is what stops an open user turn from
# swallowing every barge-in there is, and CutReplyFence below it, which keeps a barge-in's
# turn from being committed ahead of the reply it cut.
#
import asyncio
import inspect
import os
import time
from dataclasses import astuple, dataclass, field

from loguru import logger

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    DataFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    SystemFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.utils.utils import obj_id

from teaport_brain import settings
from teaport_brain.settings import setting

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
# 37 verdicts on the preceding call were COMPLETE. (Re-measured 2026-09-10 at threshold
# 0.5 over three calls: INCOMPLETE was right 12 times in 20, so it carries some signal
# after all -- see SMARTTURN_STOP_SECS for what that is worth. COMPLETE is still handed
# out to plainly unfinished phrases, which is what the floor below guards against.)
#
# With that premise gone, the floor has to do the job by itself: at 0.5 the ~0.2s breath
# before the next word never asks the question at all, and the user's sentence survives.
# See SMARTTURN_COMPLETE_THRESHOLD below for why the semantic knob cannot substitute.
#
# The likely root cause is upstream of this file and not fixed here: Smart Turn v3 judges
# the waveform, and this is 8 kHz telephony band upsampled to 16 kHz, which is not what it
# was trained on. If that is ever addressed, 0.2 becomes safe again and ~0.3s comes off
# every turn -- so revisit this together with the analyzer, not alone.
#
# Half of what the floor was guarding is gone since #43, though: the STT's commit now
# follows the VERDICT (stt.py's process_frame), so an INCOMPLETE answer at a
# breath keeps the engine's segment open along with the pipecat turn, and the caller's
# next words join the same decode instead of starting a context-free one. What the floor
# still guards is the other half: a COMPLETE answer to an unfinished phrase, which
# commits at once whatever the floor. Where the verdicts are trustworthy (wideband
# audio: the Talk client, where a 350 ms mid-phrase pause drew INCOMPLETE at
# p=0.04-0.17 on 4 of 4 runs), 0.2 is safe now; on telephony it is not, for the reason
# above. Tune via ENDPOINT_STOP_SECS.
ENDPOINT_STOP_SECS = setting("ENDPOINT_STOP_SECS")

# Smart Turn's OWN silence limit -- pipecat's SmartTurnParams.stop_secs -- is not a
# floor but a CEILING: BaseSmartTurn.append_audio force-completes the turn once this
# much silence has accumulated since the user's last speech, model verdict or not (and
# empties its buffer, so no verdict is asked again). Both used to be fed
# ENDPOINT_STOP_SECS, harmless at 0.5 + 0.5 and broken at 0.2: an INCOMPLETE verdict
# was overridden one audio chunk later, so the "mid-thought protection now rests on
# Smart Turn" that the cut to 0.2 claimed did not exist. This is how long an
# INCOMPLETE verdict keeps the turn open, counted from the VAD STOP -- not from the
# end of speech. The analyzer's silence clock runs on the `is_speech` flag the strategy
# hands it, which is the VAD's, and the VAD reports speaking until ENDPOINT_STOP_SECS
# of silence has passed; so the caller's pause is ENDPOINT_STOP_SECS + this before the
# turn commits anyway (0.35 + 1.0 = 1.35 s live, and the journal says so: "End of Turn
# complete due to stop_secs. Silence in ms: 1000.0" lands 1.00 s after every [EP]
# VAD-STOP it follows). Measured 2026-09-10 over three calls: 20 INCOMPLETE verdicts, 12
# were right (the caller resumed, 0.22-0.92 s after the VAD stop) and 8 fell through to
# this ceiling with the same text they had at the verdict -- the full second bought
# nothing on those. Lowering it trades the two longest resumptions (0.82 and 0.92 s:
# mid-sentence pauses of ~1.2 s, both genuine) against 0.3-0.5 s on each fallthrough;
# that trade has not been made. Since #43 the STT holds its segment open for this same
# wait (the resumed words then join it), so a fallthrough also pays the engine's commit
# after it -- 0.1-0.2 s with the silence hint (stt.py): measured 2026-10-09, those turns
# commit +1.08 s after the VAD stop against +0.41 s for a COMPLETE verdict. With
# TEAPORT_SPECULATIVE_REPLY on (speculate.py) the LLM is asked on the settled interim
# under this wait -- it had settled a median ~0.6 s before the commit on those turns --
# so a fallthrough no longer pays the round trip after it, and that is what makes
# raising this affordable. Tune via SMARTTURN_STOP_SECS.
SMARTTURN_STOP_SECS = setting("SMARTTURN_STOP_SECS")

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
SMARTTURN_COMPLETE_THRESHOLD = setting("SMARTTURN_COMPLETE_THRESHOLD")

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
VAD_CONFIDENCE = setting("VAD_CONFIDENCE")
VAD_MIN_VOLUME = setting("VAD_MIN_VOLUME")

# The rate Silero runs at, 16000 or 8000: the SHARED value, which Talk and the room mic
# take unless they have their own; the phone has its own, 8000 by default (the per
# front-end block below). Why 8000 exists, and why only the phone wants it: "Silero's
# inference rate", above NarrowbandSileroMixin. Tune via VAD_SAMPLE_RATE.
VAD_SAMPLE_RATE = int(setting("VAD_SAMPLE_RATE"))


def phone_rate_notice(env=os.environ) -> str | None:
    """What to tell an operator whose VAD_SAMPLE_RATE used to reach phone calls and no
    longer does (issue #215), or None. Until #215 the one value was the phone's too; now
    SIP_VAD_SAMPLE_RATE has a default of its own (8000) that beats it, so a brain.env that
    set VAD_SAMPLE_RATE=16000 to keep the phone on the upsample silently stopped doing so.
    Only where it makes a difference: the shared value set, readable and not the phone's
    default, and no SIP_VAD_SAMPLE_RATE set."""
    shared, sip = settings.ROWS["VAD_SAMPLE_RATE"], settings.ROWS["SIP_VAD_SAMPLE_RATE"]
    text = settings.normalize(shared, env.get("VAD_SAMPLE_RATE"))
    if text is None or settings.normalize(sip, env.get("SIP_VAD_SAMPLE_RATE")) is not None:
        return None
    value, problem = settings.parse(shared, text)
    if problem is not None or value == sip["default"]:
        return None
    return (f"VAD_SAMPLE_RATE={value} no longer applies to phone calls: they use "
            f"SIP_VAD_SAMPLE_RATE (default {sip['default']}); set SIP_VAD_SAMPLE_RATE={value} "
            f"to keep the old rate")


# Once per process, here, where the shared value is read: the per-session reads
# (turn_settings) would say it on every call.
if (_notice := phone_rate_notice()) is not None:
    logger.warning(_notice)

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
INTERRUPT_MIN_WORDS = setting("TEAPORT_INTERRUPT_MIN_WORDS")


# --- Per front-end -----------------------------------------------------------------
# One brain process serves both the phone line and Talk (issue #58), and the two were
# tuned apart in blind A/Bs (Talk: ENDPOINT_STOP_SECS 0.2 / SMARTTURN_STOP_SECS 0.6;
# the phone stays on 0.5 / 1.0, with its own barge-in word count). So the turn-taking
# knobs above (four, and Silero's rate) are the SHARED values, and each front-end may
# override them with its own name: SIP_<name> for phone calls, TALK_<name> for /talk
# sessions, ROOM_<name> for the box's own mic (the interrupt count drops its TEAPORT_
# prefix: SIP_INTERRUPT_MIN_WORDS, ...). They are read when a session is built
# (build_agent_session), not at import.
#
# The box's own microphone (the XVF3800 room mic, through the local audio bridge) is a
# /talk client, but it is a third front-end here, "room", with ROOM_<name> of its own
# (issue #159). It had been taking Talk's 0.2 / 0.6, which were chosen on a headset or
# a laptop at arm's length; across a room people pause mid-sentence, and on 2026-10-11..13
# about 7 of 115 awake room transcripts were one sentence cut into 2 to 6 turns, with
# Smart Turn calling 244-377 ms tails ("... the, the,") COMPLETE at p~0.98. Its stop and
# ceiling have schema defaults of their own, 0.3 / 1.5, picked by replaying 20 min of
# room recordings through pipecat's Silero VAD and this file's Smart Turn analyzer
# (2026-10-13, three framings each; 105 pauses labelled from a local transcript):
#
#   stop/ceiling  mid-sentence splits (of 29)  turn end -> commit: median   p90
#   0.2 / 0.6     17.7  (the live setting)                         0.20 s   0.77 s
#   0.3 / 1.0     11.7                                             0.30 s   1.28 s
#   0.3 / 1.5      6.3                                             0.30 s   1.76 s
#   0.5 / 1.0      7.7                                             0.52 s   1.51 s
#   0.5 / 1.5      5.7                                             0.52 s   1.99 s
#
# The ceiling is the lever: a stop over 0.3 buys about half a split for 0.2 s on every
# turn. ~5 splits are in every row (a confident COMPLETE at a comma, and pauses the live
# bot's reply had already lengthened), so they are not the knobs' to fix. About a
# quarter of the turn ends draw INCOMPLETE and wait the whole 1.8 s; the speculative
# reply (speculate.py) runs under it. The verdicts at the splits scored 0.4-0.98, mostly
# over 0.8, so no threshold would have spared them: the threshold and the barge-in word
# count fall back to the shared values like every other override. Everything else a Talk
# session does (the relay, captions, context notes, the call prompt) a room session
# still does: only the turn-taking is keyed on "room".
#
# The fifth knob is the rate Silero runs at, VAD_SAMPLE_RATE (issue #215), and it is per
# front-end because the right value is a property of the AUDIO, and one process now
# hears two kinds: a phone call is 8 kHz G.711 the gateway upsampled, /talk and the room
# mic are real wideband. So SIP_VAD_SAMPLE_RATE has a schema default of its own, 8000,
# and the phone no longer takes the shared value; TALK_ and ROOM_VAD_SAMPLE_RATE have
# none, and those sessions take the shared 16000. Before this the one process-wide value
# had no correct setting on a box serving all three, and it was left at 16000
# everywhere. A replay of 11 phone calls' VAD input (pipecat 1.12's Silero through its
# own analyze_audio path, its state reset as a live call resets it) put 8000 modestly
# ahead on detection: over the bot the onset test fired on 49 of 59 utterances against
# 46 (p90 0.34 s against 0.42 s), the VAD started on 137 of 145 quiet-bot utterances
# against 132 (p90 0.70 s against 0.84 s), and false detections in the bot's audible
# windows were unchanged (0.07 per bot-minute). The VAD stop medians were unchanged
# (0.58 s); mid-speech splits, pooled, went 12 -> 15 at a 0.5 s stop with the model's
# 5 s resets at phase 0, which is inside the 11-18 the same audio gives across reset
# phases, so not a difference the replay can tell from its noise -- the live check
# watches turn fragmentation for it. Smart Turn is a waveform model fed the same
# upsampled audio and is not touched by this knob.


@dataclass(frozen=True)
class TurnSettings:
    endpoint_stop_secs: float = ENDPOINT_STOP_SECS
    smartturn_stop_secs: float = SMARTTURN_STOP_SECS
    smartturn_complete_threshold: float = SMARTTURN_COMPLETE_THRESHOLD
    interrupt_min_words: int = INTERRUPT_MIN_WORDS
    vad_sample_rate: int = VAD_SAMPLE_RATE


def _hz(rate: str | None) -> int | None:
    # The rate rows are enums of the two rates Silero runs at, so they read as text.
    return None if rate is None else int(rate)


# Each front-end's own value of each knob, in TurnSettings' field order: None where it
# has none (unset, unreadable, and no schema default), and then the shared value
# applies. One literal setting("NAME") per knob, so a search for a name finds its read.
FRONT_ENDS = {
    "sip": lambda: (setting("SIP_ENDPOINT_STOP_SECS"), setting("SIP_SMARTTURN_STOP_SECS"),
                    setting("SIP_SMARTTURN_COMPLETE_THRESHOLD"),
                    setting("SIP_INTERRUPT_MIN_WORDS"),
                    _hz(setting("SIP_VAD_SAMPLE_RATE"))),
    "talk": lambda: (setting("TALK_ENDPOINT_STOP_SECS"), setting("TALK_SMARTTURN_STOP_SECS"),
                     setting("TALK_SMARTTURN_COMPLETE_THRESHOLD"),
                     setting("TALK_INTERRUPT_MIN_WORDS"),
                     _hz(setting("TALK_VAD_SAMPLE_RATE"))),
    "room": lambda: (setting("ROOM_ENDPOINT_STOP_SECS"), setting("ROOM_SMARTTURN_STOP_SECS"),
                     setting("ROOM_SMARTTURN_COMPLETE_THRESHOLD"),
                     setting("ROOM_INTERRUPT_MIN_WORDS"),
                     _hz(setting("ROOM_VAD_SAMPLE_RATE"))),
}


def turn_settings(front_end: str | None) -> TurnSettings:
    """The turn-taking knobs for a session of `front_end` (a FRONT_ENDS key, or None
    for the shared values alone): its own value where it has one, else the shared
    value. An unknown name raises: a typo here would otherwise build a session on the
    shared values with nothing to say so."""
    shared = TurnSettings()
    if front_end is None:
        return shared
    own = FRONT_ENDS[front_end]()
    return TurnSettings(*(mine if mine is not None else base
                          for mine, base in zip(own, astuple(shared))))


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


# The stamp of a close that answers no VAD stop: the engine's own close of words no
# stop has claimed yet (stt.py, _handle_message). Stops count from 1, so it pays no
# debt and is no receipt. Not None, which is another STT's unstamped final, taken as
# stock.
NO_STOP = 0


@dataclass
class TurnVerdictFrame(SystemFrame):
    """Smart Turn's answer to a VAD stop, pushed UPSTREAM by the stop strategy so the
    STT service can close the engine-side segment on the VERDICT rather than on the
    raw VAD stop (stt.py's process_frame).

    One frame per verdict: at the VAD stop with the model's answer (`source`
    "verdict"); again when an INCOMPLETE answer falls through to the analyzer's
    silence ceiling (`source` "ceiling", always complete); and once more if the turn
    is closed under an INCOMPLETE answer by something other than that ceiling --
    keep_barge_in_reachable re-applying a refused stop, the aggregator's stop
    watchdog -- because the ceiling stops counting at the turn's end and its verdict
    would otherwise never come (`source` "turn-stopped", always complete). The STT
    records `source` as the commit's reason on its segment line; one that answers no
    held segment (the bot was speaking, the caller resumed first, the STT's no-verdict
    fallback is committing at the stop) it ignores.

    A SystemFrame like the VAD frames it answers: it rides the input task ahead of the
    queued audio, so the commit it triggers is not held behind a burst of frames.

    `stop_n` names the VAD stop the verdict answers, as the strategy's count of
    VADUserStoppedSpeakingFrames -- the count the STT stamps its finals with, and keeps
    of the same frames (stt.py, `_vad_stops`). The STT acts only on a verdict for the
    stop it is on. The VAD's edges reach the two by different queues (the class note
    of LateStartTurnStopStrategy), and the verdict goes up behind every edge the VAD
    found while the strategy was getting to the stop: with the strategy's queue a
    whole utterance behind, stop 1's COMPLETE reached the STT after stop 2 and, naming
    no stop, closed stop 2's held segment -- which the model then called INCOMPLETE,
    and the caller's resumption split the sentence (SttCommit.tla, sc_verdict_stale).
    The default, NO_STOP, answers no stop and is never acted on.
    """

    complete: bool = False
    source: str = ""
    stop_n: int = NO_STOP


@dataclass
class _UninterruptibleData(DataFrame):
    """The STT's bookkeeping frames below: DATA frames, so each arrives after the
    interims and finals it follows (a system frame would overtake the queue), and
    uninterruptible, so the turn start a MinWords interim triggers does not flush them
    with every interruptible frame queued behind it. Declared once here (pipecat 1.11
    retired the UninterruptibleFrame mixin for this field) so a new one cannot be added
    without it."""

    interruptible: bool = field(default=False, init=False)


@dataclass
class SegmentDoneFrame(_UninterruptibleData):
    """The STT's segment closed with NO words in it: pushed DOWNSTREAM by the STT
    service in place of the FinalTranscriptionFrame a done with words produces (a
    final is its own segment's close).

    `stop_n` is the VAD stop the close answers, counted as the final's stamp is (see
    FinalTranscriptionFrame in stt.py). The stop strategy waits for the close of the
    stop it is on before it ends the turn (LateStartTurnStopStrategy): this frame is
    how it learns that close came with nothing in it, so the words it already holds
    -- an earlier utterance's final that landed late -- are all the turn will get.

    A data frame, not a system frame, because it must arrive AFTER the interims and
    finals it follows: a system frame overtakes the queue. Uninterruptible for the
    same reason the final is: the turn start that a MinWords interim triggers flushes
    every interruptible frame queued behind it. Consumed by SegmentDoneSink right
    below the aggregator; nothing downstream of the turn strategies reads it.
    """

    stop_n: int


@dataclass
class SegmentResetFrame(_UninterruptibleData):
    """The STT dropped its open segment without closing it -- the engine session went
    (a reconnect, a hold) and the words streamed so far with it: no final and no
    SegmentDoneFrame will ever come for them. Pushed DOWNSTREAM by the STT service for
    the stop strategy, whose settled-interim trigger would otherwise keep the dead
    segment's interim and, at the next INCOMPLETE stop, find it "settled" long ago and
    speculate on words the turn will never carry. Data and uninterruptible for the
    reasons SegmentDoneFrame is: it must arrive after the interims it voids, and a turn
    start must not flush it. Dropped by SegmentDoneSink.

    `stop_n` is the count of VAD stops the STT had seen at the reset (counted as the
    final's stamp is). No close will come for any of them -- the held one, the ones
    whose commits were out -- so the strategy takes the reset as their close: without
    it, a stop owed its close stayed owed, and a turn with its transcript in hand and
    the verdict COMPLETE waited for the aggregator's 5 s watchdog (SttCommit.tla,
    sc_reset_owed)."""

    stop_n: int


@dataclass
class SttProgressFrame(_UninterruptibleData):
    """Whether the STT's engine session reports how far it has transcribed
    (teagram-engine's transcription.progress, offered in its session.created): pushed
    DOWNSTREAM by the STT service at the start of every engine session, for the stop
    strategy, which then asks the speculative reply on SpeechDecodedFrame instead of a
    settle timer (or, `reported` False, on the timer). Data and uninterruptible like
    the frames below, and dropped by SegmentDoneSink.

    `peeks`: the session's turn text is sliced and previewed at the VAD stop (stt.py
    TURN TEXT, THE PEEK): the speculative reply is asked on the stop's preview
    (SpeechPreviewFrame) and on nothing else, since that is the text the turn will
    commit."""

    reported: bool
    peeks: bool = False


@dataclass
class SpeechPreviewFrame(_UninterruptibleData):
    """The provisional text of VAD stop `stop_n`'s turn, from the engine's preview at the
    stop (stt.py TURN TEXT, THE PEEK), before any verdict has closed the turn on it: the
    words the turn will commit if Smart Turn calls it done with the caller quiet. Pushed
    DOWNSTREAM by the STT for the stop strategy, which asks the speculative reply on it;
    data and uninterruptible like the frames around it, dropped by SegmentDoneSink."""

    stop_n: int
    text: str


@dataclass
class SpeechDecodedFrame(_UninterruptibleData):
    """The engine has transcribed past the caller's end of speech for VAD stop
    `stop_n` (counted as the final's stamp is, FinalTranscriptionFrame in stt.py):
    every word it will ever return for that speech is in the interims already pushed.
    Pushed DOWNSTREAM by the STT service, once per stop, when the engine's
    decoded-through position passes the speech end it placed for that stop. The stop
    strategy's trigger for the speculative reply under an INCOMPLETE verdict
    (LateStartTurnStopStrategy). A data frame so that it arrives after those interims,
    uninterruptible so a turn start cannot flush it; dropped by SegmentDoneSink."""

    stop_n: int


@dataclass
class NoSpeechReportFrame(_UninterruptibleData):
    """No SpeechDecodedFrame will come for VAD stop `stop_n`, though the engine
    session gives its report: the STT could not place that stop's speech end on the
    engine's clock (audio passed it unsent -- a reconnect's buffer, a dropped send).
    The stop strategy decides that stop with the settle window instead. Pushed
    DOWNSTREAM by the STT at the stop; data, uninterruptible and dropped by
    SegmentDoneSink like the frames above."""

    stop_n: int


@dataclass
class TurnTextFinalFrame(_UninterruptibleData):
    """A sliced turn's final words differ from those pushed for it (stt.py, TURN TEXT).
    Pushed DOWNSTREAM by the STT behind the final it corrects; one of STT_FRAMES, so
    every processor that holds the STT's frames holds it behind that final, and the
    corrector below the aggregator (turn_text.TurnTextCorrector) gets it after the
    aggregator has handled the final. Data and uninterruptible like the frames above.

    `asked`: the text pushed as the turn's final (after the echo guard). `final`: the
    final words, the same way. `kind`: "adds", "drops" or "changes"
    (stt.compare_turn_text), with the words added and dropped. `pushed_at`: when
    `asked` was pushed (time.monotonic()): the commit that carries it came after."""

    stop_n: int
    asked: str
    final: str
    kind: str
    added: tuple = ()
    dropped: tuple = ()
    pushed_at: float = 0.0


# The STT's bookkeeping frames, for the processors below it: data, uninterruptible, each
# behind the words it follows. Dropped by SegmentDoneSink once the aggregator (and the
# turn-text corrector above the sink) has had them.
STT_BOOKKEEPING_FRAMES = (SegmentDoneFrame, SegmentResetFrame, SttProgressFrame,
                          SpeechDecodedFrame, NoSpeechReportFrame, SpeechPreviewFrame,
                          TurnTextFinalFrame)

# Everything the STT pushes for the turn strategies, in its order. A processor above the
# aggregator that holds the STT's words -- RunControlClaims while the relay decides, the
# barge-in judge while it judges -- holds every one of these behind them, so no close, no
# report and no correction overtakes the words it follows. One tuple, so a frame the STT
# gains is held by all of them at once.
STT_FRAMES = (TranscriptionFrame, InterimTranscriptionFrame) + STT_BOOKKEEPING_FRAMES


class SegmentDoneSink(FrameProcessor):
    """Drops the STT's bookkeeping frames (STT_BOOKKEEPING_FRAMES) once the stop
    strategy and the turn-text corrector have had them. Sits
    right after the user aggregator (behind only CutReplyFence's head, which passes
    every frame on inline), whose stock process_frame forwards every data frame it does
    not itself consume: the frames are for the stop strategy (which the aggregator hands
    them to) and would otherwise ride the queue of every processor down to the
    transport, one per wordless close."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, STT_BOOKKEEPING_FRAMES):
            return
        await self.push_frame(frame, direction)


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
    commit. Measured live 2026-09-10: 6 of 58 turn commits sat 0.64-0.71 s after the
    VAD stop instead of ~0.24 s, and every one was an "Okay, stop." / "Stop." spoken
    over the bot -- the turns where the caller is already waiting on us.

    The rule: if the user is silent and the VAD stop for this utterance has already
    been seen, the pending verdict is about this utterance, so a turn opening now
    keeps it and the final commits at once. A VAD start in between clears the verdict
    on its own (_discard_pending_end_of_turn), so a stale one cannot survive into a
    new utterance. An INCOMPLETE verdict is kept just the same, and the analyzer's
    silence ceiling (SMARTTURN_STOP_SECS) ends the turn as it always did.

    It is also where the speculative reply is triggered (speculate.py, off unless
    TEAPORT_SPECULATIVE_REPLY is set): this strategy is the one place that sees the
    caller's words AND knows whether the turn concluded on them. The main trigger is the
    SETTLED INTERIM: a VAD stop answered INCOMPLETE, the caller quiet, the ceiling
    running -- and since #43 the STT holding its segment open through that wait, so no
    final comes before the ceiling's commit. The engine's interim now equals the final it
    is followed by (48 of 48 phone turns, 2026-10-09), so once it has stopped changing
    for the speculator's settle window, it is text the LLM could already be working on:
    a speculation is opened on the turn's pending finals plus that interim, built as the
    aggregator will build the commit (Speculator.candidate). A changed interim cancels it
    and re-arms the window; a VAD start cancels it; a COMPLETE verdict never arms it (the
    commit is immediate and the final trails the last interim by ~0.01 s). The window is
    armed at an interim that lands under the wait, and at the INCOMPLETE stop itself for
    an interim already in hand, counted from that interim's arrival.

    "Stopped changing" is the engine's to say where it can (SttProgressFrame): a timer
    cannot tell an interim that is finished from a decoder between two words. Live
    2026-10-10 with the 160 ms window: 10 hits, 29 misses, 17 of them "superseded" --
    after the VAD stop the engine is still working through its 480 ms delay window, and
    its deltas pause 0.23-0.56 s between words while it does, so the window fired
    mid-sentence; a longer one eats the head start. An engine with transcription.progress
    says how far it has transcribed on the audio clock, and the STT, which placed the
    caller's speech end for the VAD stop on that clock, pushes SpeechDecodedFrame once
    the engine is past it: every word of that speech is in the interim by then. So with
    the report the speculation is asked on that frame, for the stop the INCOMPLETE
    verdict answered, and the window is not armed; without it (an engine without the
    report, TEAPORT_SPECULATE_ON_DECODE off, or a stop whose speech end the STT could
    not place: NoSpeechReportFrame), the window as above. Words that still
    arrive after the frame -- the speech end is Silero's, and noise after it can decode
    to a word -- are none the report vouched for: they supersede what was asked, and the
    window decides them.

    Finals still speculate where they land on a turn the stop did not fire on with the
    caller quiet, because under the verdict hold three shapes still deliver one before
    the commit: a VAD stop over the bot commits at once (the flush is the barge-in, so
    its final lands under an INCOMPLETE verdict); the engine's segmenter closes a segment
    on its own (it finalizes on shorter silences than the 0.5 s VAD floor, so its final
    can land before the stop -- then the question is asked at the stop, the verdict only
    being known there -- or under the hold after it); and a final with no VAD stop in
    sight (pipecat's fallback timer). Each is the candidate with no interim in it, and a
    final that confirms the interim a live speculation was asked on keeps that stream
    (Speculator.start). A final the stop DID fire on is followed by the real request
    within the same call, so nothing is speculated.

    A barge-in's final asks like any other, the one that OPENS the turn over the bot
    included: its interruption has not crossed the pipeline yet, so the cut reply is not
    in the context, and Speculator.start waits for it (CutReplyFence) before taking the
    snapshot. The settled-interim trigger does not fire while the bot is still speaking:
    that is the over-the-bot stop, which the STT commits at once, and the barge-in's own
    final, on its way, asks instead.

    It also reports every end-of-turn verdict UPSTREAM as a TurnVerdictFrame, numbered
    with the VAD stop it answers, for the STT service: this is the only object that sees the model's answer at the VAD stop
    and the analyzer's silence ceiling when an INCOMPLETE answer falls through to it,
    and the STT's commit -- the thing that closes the engine's segment -- wants to
    follow those rather than the raw VAD stop (issue #43; see stt.py's process_frame).
    The verdicts are read off what the turn DID, not off `_turn_complete` alone: a
    COMPLETE answer with a finalized transcript already in hand stops the turn inside
    the stock handler, and the controller resets this strategy on the way, so the flag
    reads False again by the time the handler returns -- the inference count is what
    says the turn was called done. And a turn closed under an INCOMPLETE answer by
    anything but the ceiling is reported as well (handle_user_turn_stopped), since the
    reset clears the analyzer and the ceiling's verdict would never come.

    And it will not end a turn on a final that belongs to an EARLIER utterance. The
    stock strategy takes every finalized TranscriptionFrame as the transcript of the
    utterance it is waiting on; on this brain the final arrives ~0.7 s after the commit
    (the engine's finish), and a caller who speaks in bursts starts the next utterance
    inside that window. Live 2026-09-19 13:31:46 (SIP): A's VAD stop, COMPLETE, commit;
    0.35 s later the caller starts B; A's final lands at +0.9 s -- inside the turn that
    now holds B -- and sets `_text`/`_transcript_finalized`. At B's VAD stop the
    COMPLETE verdict then ends the turn on A's text: at once when no interim of B's
    has arrived yet (the engine's first delta on a fresh segment trails the reset by
    ~0.6 s, so a short B has none), otherwise through the STT p99 safety net, which
    with TEAPORT_TTFS_P99=0.8 and a 0.5 s stop sits 0.3 s after the VAD stop -- before
    any done can exist. B's final then lands as a new turn, interrupts the reply to A
    before a word of it plays (LEDGER "CUT heard~0%"), and the LLM runs again on A+B,
    1.2-1.5 s later than it needed to. Since 2026-09-16: 38 of 431 replies cut at 0%,
    ~10 of the SIP path's 24 this shape. The same shape without the second VAD start
    -- the engine's own endpointer cutting a breath shorter than ENDPOINT_STOP_SECS,
    its final for the first half landing, the safety net firing on it while the
    second half's commit is still out -- ends the turn on half a sentence.

    The rule: the turn does not end while the latest VAD stop counted here is still
    OWED its close. The STT stamps every final and every wordless close
    (SegmentDoneFrame) with the VAD stop it answers, as a count of
    VADUserStoppedSpeakingFrames (stt.py: FinalTranscriptionFrame); this strategy
    counts the same frames, notes at each stop that its close is owed, and takes the
    debt as paid by the close stamped with it -- whenever that lands, the caller
    quiet or not. Every final's words still go where they always went -- the
    aggregator has them, and `_text` takes them, as stock -- but neither the verdict,
    the ceiling, the p99 safety net nor pipecat's fallback for a transcript with no
    stop in sight can end the turn while a close is owed
    (_maybe_trigger_user_turn_stopped). A late final of the previous utterance
    therefore pays that utterance's debt and ends nothing; the turn ends on this
    utterance's own final, or on its wordless close, which then finalizes whatever
    `_text` holds -- the earlier final, when the caller's resumption decoded to
    nothing. The debt survives turn boundaries on purpose: it is a fact about the
    STT's segment, not the turn, and the turn the controller closes under a running
    ceiling (its watchdog, keep_barge_in_reachable) leaves the STT holding exactly
    such a segment -- the half of a sentence the backstop or the engine's own cut
    had already returned would otherwise open a new turn and end it through the
    fallback while the other half is still on its way (SttCommit.tla found both).
    The two sides count in no fixed order, though: the VAD runs in the user
    aggregator, downstream of the STT, and each of its edges reaches this strategy on
    the aggregator's input queue and the STT four queues upstream. So a close can land
    here stamped with a stop not counted yet -- the STT saw the stop first and its
    commit came back quickly (the silent tail behind the engine's own cut, a stop over
    the bot) -- and is kept as a RECEIPT: at the count, that stop's debt is found
    already paid. Without it the count owed a close that had gone by, and the turn
    waited for the aggregator's 5 s watchdog. The STT's half of the same rule is that
    a close nobody asked for stamps no stop at all (NO_STOP) when it carries words: it
    used to be stamped as the next stop's, which the STT could only know while it
    counted first.
    Finals without the stamp (another STT service) pay whatever is owed, as stock.
    What the STT cannot stamp right, and what the wait costs, is in
    brain/formal/SttCommit.tla and its README section.
    """

    def __init__(self, *, speculator=None, **kwargs):
        super().__init__(**kwargs)
        self.speculator = speculator
        self._inferences = 0
        self._bot_speaking = False
        # VADUserStoppedSpeakingFrames seen: the count the STT stamps its finals with
        # (see the class note), the latest of them whose close has not landed -- None
        # once it has -- and the receipts: stops not counted yet whose close has. NOT
        # per turn: see the class note. The debt is only
        # collected once the session's STT has shown it stamps its closes (the first
        # stamped one latches this): another STT's finals are taken as stock, a
        # final in hand before the stop included.
        self._stops = 0
        self._owed = None
        self._receipts = set()
        self._stamped = False
        # The settled-interim trigger (see the class note): the latest interim's words
        # since the STT last closed a segment -- its buffer resets at every close, final
        # or wordless, so this does too -- when they last changed, and the settle
        # window's timer.
        self._interim = ""
        self._interim_at = 0.0
        self._settle_task = None
        # The engine's report (see the class note): whether this engine session gives
        # it, the latest VAD stop whose speech it has transcribed past
        # (SpeechDecodedFrame), and the latest stop whose verdict was INCOMPLETE --
        # known only once the model has answered, which a frame handled while it runs
        # must not take for granted.
        self._decode_reported = False
        self._decoded_n = None
        self._incomplete_n = None
        # A stop no report will come for (NoSpeechReportFrame): the window decides it.
        self._unreported_n = None
        # When the latest VAD stop reached this strategy, for the [SPEC] start line.
        self._stop_at = 0.0
        # The STT's turn text is previewed at the VAD stop (SttProgressFrame.peeks), and
        # the latest stop's preview, (stop_n, text) (SpeechPreviewFrame): then the
        # speculation is asked on that and on nothing else (see _speculate_on_preview).
        self._peeks = False
        self._preview = None

    async def trigger_user_turn_inference_triggered(self, *, speculation=None):
        # Every path that commits the turn passes here (trigger_user_turn_stopped is
        # inference + finalize), so a count taken around a final says whether it fired.
        # `speculation` is pipecat's own speculative inference (1.9+, its eager stop
        # strategy): it runs on a provisional copy and commits nothing, so it is not
        # counted. Nothing here asks for one; it passes through as given.
        if speculation is None:
            self._inferences += 1
        await super().trigger_user_turn_inference_triggered(speculation=speculation)

    async def process_frame(self, frame):
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, SegmentDoneFrame):
            self._interim = ""
            await self._handle_segment_done(frame)
        elif isinstance(frame, SegmentResetFrame):
            await self._handle_segment_reset(frame)
        elif isinstance(frame, SttProgressFrame):
            self._decode_reported = frame.reported
            self._peeks = frame.peeks
        elif isinstance(frame, SpeechPreviewFrame):
            self._preview = (frame.stop_n, frame.text)
            await self._speculate_on_preview()
        elif isinstance(frame, SpeechDecodedFrame):
            self._decoded_n = frame.stop_n
            await self._speculate_when_decoded()
        elif isinstance(frame, NoSpeechReportFrame):
            self._unreported_n = frame.stop_n
            await self._arm_settle_for_unreported()
        result = await super().process_frame(frame)
        if isinstance(frame, InterimTranscriptionFrame):
            await self._handle_interim(frame)
        return result

    def _note_close(self, stop_n: int) -> bool:
        """The STT closed a segment stamped as answering VAD stop `stop_n`. If that is
        the stop whose close is owed, the debt is paid -- whether the caller is quiet
        (this utterance's own close) or has started again (the previous utterance's
        final, landing late). A stop not counted here yet is the STT's, seen first: the
        close is kept as a receipt for the count. Any other stamp -- NO_STOP, or a stop
        already paid -- changes nothing. Returns whether it paid."""
        self._stamped = True
        if stop_n == self._owed:
            self._owed = None
            return True
        if stop_n > self._stops:
            self._receipts.add(stop_n)
            logger.debug(f"{self}: segment close for VAD stop {stop_n} before this "
                         f"strategy has counted it (the latest counted: {self._stops}); "
                         "kept as a receipt")
            return False
        if self._owed is None:
            # Nothing owed: the engine's own close of a segment it opened on noise
            # (hundreds a day, stamped with the last commit's stop, already paid), or
            # a close for a stop already answered. Not worth a line each.
            logger.trace(f"{self}: segment close for VAD stop {stop_n} with no stop owed "
                         f"(the latest counted: {self._stops})")
            return False
        logger.debug(f"{self}: segment close for VAD stop {stop_n} does not close "
                     f"stop {self._owed} (the latest counted: {self._stops}); its "
                     "words are kept, the turn waits for that close")
        return False

    def _note_reset(self, stop_n: int) -> bool:
        """The STT's segment is gone (SegmentResetFrame), and with it the close of every
        VAD stop it had seen, `stop_n` of them: none will come now. So the reset is
        their close -- it pays the owed one if it is among them, and keeps a receipt
        for each the count here has not reached. Returns whether it paid."""
        self._receipts.update(range(self._stops + 1, stop_n + 1))
        if self._owed is not None and self._owed <= stop_n:
            logger.debug(f"{self}: the STT's segment was reset -- VAD stop {self._owed}'s "
                         "close will not come; taking the reset as it")
            self._owed = None
            return True
        return False

    async def _handle_segment_done(self, frame: SegmentDoneFrame):
        """The segment closed with no words. If it is the owed close, whatever `_text`
        holds -- the previous utterance's late final, or nothing -- is all the turn
        will get: finalize it and let the verdict (or the ceiling) end the turn."""
        if self._note_close(frame.stop_n):
            await self._finalize_on_wordless_close("closed with no words")

    async def _finalize_on_wordless_close(self, how: str):
        if self._text:
            logger.debug(f"{self}: the utterance's segment {how} -- "
                         f"{self._text[:40]!r} is the turn's transcript")
        self._transcript_finalized = True
        await self._maybe_trigger_user_turn_stopped()

    async def _maybe_trigger_user_turn_stopped(self):
        # The stock trigger, gated: not while the latest VAD stop counted here is
        # owed its close. Every path that ends the turn passes here -- the verdict at
        # the stop (inline, with a final in hand), the ceiling, the p99 safety net's
        # timer, the final's own arrival and the no-stop fallback's timer -- so this
        # is the one place the rule in the class note is enforced. The safety net
        # thereby never fires on a VAD-stop path: this STT always closes the segment
        # (a commit for every stop, the backstop for a missed one, the hold's expiry
        # for a lost verdict), and the net exists for STTs that do not.
        if self.close_owed():
            return
        await super()._maybe_trigger_user_turn_stopped()

    def close_owed(self) -> bool:
        """The latest VAD stop counted here is still owed its close: the words that
        stop flushed are on their way from the engine, not in the turn yet. What this
        strategy's own trigger waits on (above). A commit made meanwhile by something
        that does not ask -- keep_barge_in_reachable's re-applied stop, in that very
        stop's handling; the watchdog's -- carries none of them (CutReplyFence tells
        FollowupGate nothing of it: see hold_commits)."""
        return self._wait_for_transcript and self._stamped and self._owed is not None

    async def _maybe_speculate(self, before: int, why: str):
        """Ask for a speculation if the frame just handled left a finalized transcript
        in hand with the caller quiet and the turn not concluded. `before` is the
        inference count taken ahead of the frame: unchanged means the stop did not fire
        on it (a commit refused by the controller still counts as fired, and is not
        ours to pre-empt)."""
        if self.speculator is None or self._inferences != before:
            return
        if not self._transcript_finalized or self._vad_user_speaking:
            return
        # A finalized transcript in hand means no interim since (the stock strategy
        # clears the flag on one): the candidate is the aggregation alone.
        await self.speculator.start(self.speculator.candidate(self._interim), why)

    def _incomplete_and_quiet(self) -> bool:
        """A VAD stop answered INCOMPLETE and the caller quiet since -- the state in
        which the analyzer's silence ceiling counts and the STT holds the segment. It
        says nothing of the turn: the Speculator checks that one is open."""
        return self._vad_stopped and not self._vad_user_speaking and not self._turn_complete

    async def _handle_interim(self, frame):
        """The open segment's words, as the STT has them so far. A change cancels a
        speculation asked on other words -- live, or a start still waiting at the cut
        fence, which has nothing live to compare -- and, under the ceiling, re-arms the
        window."""
        text = (frame.text or "").strip()
        if text == self._interim:
            return
        self._interim = text
        self._interim_at = time.monotonic()
        if self.speculator is None or self._peeks:
            # Previewed at the stop: the speculation is the preview's, and a turn's words
            # still arriving after the stop are what its preview already holds.
            return
        if self.speculator.text != self.speculator.candidate(text):
            await self.speculator.cancel("superseded")
        # With the engine's report, words still on their way are its to announce
        # (_speculate_when_decoded); words after it are not, and the window decides them.
        if self._incomplete_and_quiet() and (not self._on_decode()
                                             or self._decoded_n == self._stops):
            await self._arm_settle(self.speculator.settle_secs)

    def _on_decode(self) -> bool:
        """Whether the speculation waits for the engine's report rather than the settle
        window: the engine session gives it, the speculator is set to use it, and the
        STT has not said the latest stop will get none."""
        return (self._decode_reported and self.speculator.on_decode
                and self._unreported_n != self._stops)

    def _trigger(self, kind: str) -> str:
        """What opened a speculation, and how long after the VAD stop: the [SPEC] start
        line's evidence for comparing the report against the window live."""
        return f"trigger={kind} +{time.monotonic() - self._stop_at:.2f}s after the VAD stop"

    async def _arm_settle_for_unreported(self):
        """The STT says no report will come for the latest stop: if the model has
        already called it INCOMPLETE with the caller quiet, its window starts now,
        counted from the interim's arrival (as the stop itself would have armed it)."""
        if (self.speculator is None or self._peeks or not self._interim
                or not (self._unreported_n == self._stops == self._incomplete_n)
                or not self._incomplete_and_quiet()):
            return
        settled = time.monotonic() - self._interim_at
        await self._arm_settle(max(0.0, self.speculator.settle_secs - settled))

    def _previewed_stop(self) -> bool:
        """The latest VAD stop's preview is in hand (stt.py THE PEEK)."""
        return (self._peeks and self._preview is not None
                and self._preview[0] == self._stops)

    async def _speculate_on_preview(self):
        """Ask on the latest stop's preview (stt.py THE PEEK): the text the turn commits
        if the model calls it done with the caller quiet -- with no user turn open yet,
        too (a short answer whose words all come after the stop). Only for the stop
        this strategy has counted, the caller quiet, the bot silent (a stop over the bot
        commits at once, and its final asks)."""
        if self.speculator is None or not self._previewed_stop():
            return
        if self._vad_user_speaking or self._bot_speaking:
            return
        await self.speculator.start(self.speculator.candidate(self._preview[1]),
                                    "the stop's preview", trigger=self._trigger("preview"),
                                    ahead_of_turn=True)

    async def _speculate_when_decoded(self):
        """Ask on the interim if the engine has transcribed past the end of the speech
        whose VAD stop the model called INCOMPLETE, with the caller quiet since -- from
        whichever of the two arrives second: the report (SpeechDecodedFrame) or the
        verdict."""
        if self.speculator is None or self._peeks or not self._on_decode():
            return
        if not (self._decoded_n == self._stops == self._incomplete_n):
            return
        if not self._interim or not self._incomplete_and_quiet():
            return
        if self._bot_speaking:
            # As for the window: the over-the-bot shape, whose final asks instead.
            logger.debug(f"{self}: interim transcribed through with the bot still "
                         "speaking -- leaving the speculation to the barge-in's final")
            return
        await self.speculator.start(self.speculator.candidate(self._interim),
                                    "interim transcribed through, verdict INCOMPLETE",
                                    trigger=self._trigger("report"))

    async def _handle_segment_reset(self, frame: SegmentResetFrame):
        """The STT's open segment is gone without a close (SegmentResetFrame): its
        interim is no one's words now. Forget it, stop its window, and drop a
        speculation asked on it (or still waiting at the cut fence to be). And the
        stops it was to answer get no other close: if one was owed, whatever `_text`
        holds is all the turn will get, as at a wordless close."""
        if self._interim:
            self._interim = ""
            await self._cancel_settle()
            if (self.speculator is not None
                    and self.speculator.text != self.speculator.candidate("")):
                await self.speculator.cancel("segment-reset")
        if self._note_reset(frame.stop_n):
            await self._finalize_on_wordless_close("was reset")

    async def _arm_settle(self, delay: float):
        await self._cancel_settle()
        self._settle_task = self.task_manager.create_task(
            self._speculate_when_settled(delay), f"{self}::_speculate_when_settled")

    async def _cancel_settle(self):
        # Awaited: cancel_task waits for the task to finish, so a timer whose sleep has
        # already ended cannot run its start() after the state it read has moved on.
        task, self._settle_task = self._settle_task, None
        if task is not None:
            await self.task_manager.cancel_task(task)

    async def _speculate_when_settled(self, delay: float):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._settle_task = None
        if not self._interim or not self._incomplete_and_quiet():
            return
        if self._bot_speaking:
            # The over-the-bot shape (see the class note): the STT committed at that
            # stop; its final is on the way and asks then.
            logger.debug(f"{self}: interim settled with the bot still speaking -- "
                         "leaving the speculation to the barge-in's final")
            return
        await self.speculator.start(self.speculator.candidate(self._interim),
                                    "interim settled, verdict INCOMPLETE",
                                    trigger=self._trigger("window"))

    async def _handle_transcription(self, frame):
        if frame.finalized:
            stop_n = getattr(frame, "stop_n", None)
            if stop_n is not None:
                self._note_close(stop_n)
            # The segment closed (the STT's buffer resets with it): the final carries
            # its words now, and asks at once below if it is to ask at all.
            self._interim = ""
            await self._cancel_settle()
        before = self._inferences
        await super()._handle_transcription(frame)
        if frame.finalized:
            await self._maybe_speculate(
                before, "verdict INCOMPLETE, ceiling running" if self._vad_stopped
                else "no VAD stop yet")

    async def _handle_vad_user_stopped_speaking(self, frame):
        # Counted before the stock handler runs the model, and owed its close from
        # here: the STT commits for every stop it sees (or holds and then commits),
        # and the turn's end waits for what that returns -- unless it has come already,
        # the STT having seen this stop first (a receipt: see the class note).
        self._stops += 1
        self._owed = None if self._stops in self._receipts else self._stops
        self._receipts = {n for n in self._receipts if n > self._stops}
        self._stop_at = time.monotonic()
        before = self._inferences
        await super()._handle_vad_user_stopped_speaking(frame)
        # The model has answered (super() awaits the inference): tell the STT first,
        # since its commit is on the turn's critical path and nothing below waits on it.
        #
        # Its answer is `_turn_complete` -- unless the stop has just ended the turn
        # INLINE. With a finalized transcript already in hand (the engine's segmenter
        # closed the final before the VAD stop: the common order on the appliance) a
        # COMPLETE verdict fires _maybe_trigger_user_turn_stopped inside super(), the
        # controller calls handle_user_turn_stopped -> _reset() on the way, and the
        # flag reads False here for a turn the model has just called done. Nothing
        # else fires an inference in there, so an inference that did is that COMPLETE;
        # a stop the controller refused leaves the flag standing. Reading the flag
        # alone pushed INCOMPLETE for exactly those stops, and the STT held a segment
        # nothing was ever going to close.
        complete = self._turn_complete or self._inferences != before
        await self._push_verdict(complete, "verdict")
        if not complete:
            self._incomplete_n = self._stops
        # The final came first and the verdict has just been reached; if it was
        # INCOMPLETE the ceiling is now running on text the stop did not fire on.
        await self._maybe_speculate(before, "final before the VAD stop, verdict INCOMPLETE")
        # Or the interim is what is in hand (the verdict hold: the final waits for the
        # ceiling's commit). With the engine's report it is asked on once the engine is
        # past this stop's speech end -- now, if the report beat the verdict here.
        # Without it, the window counts from the interim's own arrival: words that
        # stopped changing before the stop are settled already.
        if (self.speculator is not None and self._interim and self._inferences == before
                and self._incomplete_and_quiet() and not self._peeks):
            if self._on_decode():
                await self._speculate_when_decoded()
            else:
                settled = time.monotonic() - self._interim_at
                await self._arm_settle(max(0.0, self.speculator.settle_secs - settled))
        # Previewed at the stop: a preview that came before this strategy counted the
        # stop is asked on now.
        if self._peeks and self._inferences == before:
            await self._speculate_on_preview()

    async def _handle_input_audio(self, frame):
        before = self._inferences
        complete_before = self._turn_complete
        await super()._handle_input_audio(frame)
        # The analyzer's silence ceiling (SMARTTURN_STOP_SECS) just overrode an
        # INCOMPLETE verdict: the stock handler sets _turn_complete on nothing else
        # here, and a verdict already COMPLETE has been reported at the VAD stop. The
        # same blind spot as the VAD stop above: with text and a final in hand the
        # ceiling stops the turn inline and the flag is reset before this line runs,
        # so the inference it fired is the evidence instead.
        if not complete_before and (self._turn_complete or self._inferences != before):
            await self._push_verdict(True, "ceiling")

    async def _push_verdict(self, complete: bool, source: str):
        # Every verdict answers the latest stop counted here: the model's at that stop,
        # the ceiling's and the turn-stop's under it (a VAD start since would have
        # stopped the ceiling and released the STT's hold). See TurnVerdictFrame.
        await self.push_frame(TurnVerdictFrame(complete=complete, source=source,
                                               stop_n=self._stops),
                              FrameDirection.UPSTREAM)

    async def _handle_vad_user_started_speaking(self, frame):
        await super()._handle_vad_user_started_speaking(frame)
        await self._cancel_settle()
        # The stop's preview is no one's now: the STT drops its peek on the same start.
        self._preview = None
        if self.speculator is not None:
            # The caller resumed: whatever they add, the committed text will not be
            # what was asked. Stop the stream now rather than at the mismatch.
            await self.speculator.cancel("resumed")

    async def cleanup(self):
        # The session is over: a speculation still streaming would keep reading (and
        # billing) a completion nothing can adopt. This is the only teardown that
        # awaits the HTTP close, since the loop goes with the session.
        await self._cancel_settle()
        if self.speculator is not None:
            await self.speculator.close()
        await super().cleanup()

    async def handle_user_turn_started(self):
        await self._cancel_settle()
        if self.speculator is not None and not self._previewed_stop():
            # Not the speculation on the latest stop's preview: the turn opening now is
            # the one it was asked for (a short answer, opened by its own words at the
            # verdict), and the commit that follows adopts it or misses.
            await self.speculator.cancel("new-turn")
        if self._vad_stopped and not self._vad_user_speaking:
            logger.debug(
                f"{self}: turn opened after its own VAD stop -- keeping the end-of-turn "
                f"verdict (complete={self._turn_complete}) instead of resetting"
            )
            # What _reset() would have cleared besides the verdict -- and it is
            # load-bearing, not belt-and-braces: the stock strategy sets _text from
            # EVERY final TranscriptionFrame, turn or no turn, so a sub-threshold final
            # between turns (a one-word "Stop." under the 2-word barge-in guard) leaves
            # it populated, and on this path nothing else clears it before the p99 timer
            # or the analyzer's silence ceiling can read it and commit the new turn on
            # its interim alone, ahead of the final.
            self._text = ""
            return
        await super().handle_user_turn_started()

    async def handle_user_turn_stopped(self):
        # The turn is over -- on this strategy's own verdict, or on something else's
        # say-so: keep_barge_in_reachable re-applying a stop the controller refused
        # earlier, or the aggregator's stop watchdog. The stock handler resets the
        # strategy AND clears the analyzer, so an INCOMPLETE verdict's silence ceiling
        # stops counting here and its verdict is never pushed. The STT holding the
        # segment for that verdict would then wait out its expiry for nothing -- the
        # words it holds belong to a turn that has just been closed under them -- and
        # log the wait as a lost frame. So tell it the turn is done: the segment closes
        # with the turn. Read BEFORE the reset, which is what the condition needs: the
        # ceiling is armed exactly when a VAD stop was judged INCOMPLETE and the caller
        # has not resumed. A turn this strategy ends on its own verdict has
        # _turn_complete set and pushes nothing here -- that verdict was, or is about
        # to be, reported by the handler that reached it.
        if self._vad_stopped and not self._vad_user_speaking and not self._turn_complete:
            logger.debug(f"{self}: turn closed under an INCOMPLETE verdict -- the ceiling "
                         "will not fire; closing the STT's held segment with the turn")
            await self._push_verdict(True, "turn-stopped")
        # The turn's words are committed; an interim still held belongs to a segment the
        # turn's close has closed (its final clears it in the ordinary order) or to one
        # whose close never reached here -- a final the follow-mute dropped, say. The
        # next segment's first interim brings its own words either way.
        self._interim = ""
        await self._cancel_settle()
        await super().handle_user_turn_stopped()


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
    _trigger_user_turn_stop or process_frame, this raises AttributeError at session
    build — loudly, on the first call, in CI — which is the outcome to want; and
    RuntimeError there if the inference hook's (strategy, speculation) signature
    moved, the one argument it reads rather than passes on. The two flags are read
    only at run time: _user_turn by every wrapper, inside pipecat's event handlers (a
    rename is an "uncaught exception in event handler" ERROR on every turn), and
    _user_speaking only in _process_frame while a finalization is owed -- the stuck
    case itself, which test_barge_in_survives_an_open_turn drives, so a rename fails
    there in CI. Silently doing nothing would restore a bug whose whole signature is
    that nothing is logged when it happens.

    Known limit: the re-attempt rides the frame that reports the user quiet, so it
    needs VAD to report a stop at all. It does NOT rescue a VAD frozen in SPEAKING —
    SIP_HALF_DUPLEX left on, or a desynced gateway AEC feeding the bot's own audio
    back in. Those kill barge-in on their own, upstream of any of this, and papering
    over them here would only hide them.
    """
    trigger_start = controller._trigger_user_turn_start
    trigger_inference = controller._trigger_user_turn_inference_triggered
    # _inference below decides on the speculation argument, so its place is checked
    # here, at session build, rather than trusted: a pipecat that moved it would leave
    # `speculation is None` never true and every refused finalization unowed -- the
    # dead barge-in this function exists for, with nothing logged.
    # Names AND kinds: _inference passes speculation positionally, so a pipecat that
    # made it keyword-only would turn every inference into a swallowed TypeError.
    params = [(p.name, p.kind) for p in inspect.signature(trigger_inference).parameters.values()]
    positional = inspect.Parameter.POSITIONAL_OR_KEYWORD
    if params != [("strategy", positional), ("speculation", positional)]:
        raise RuntimeError(
            f"pipecat's UserTurnController._trigger_user_turn_inference_triggered takes "
            f"{[f'{n} ({k.name})' for n, k in params]}, not (strategy, speculation): "
            f"keep_barge_in_reachable needs a new look")
    trigger_stop = controller._trigger_user_turn_stop
    process_frame = controller.process_frame

    # inference: this turn's words have already been pushed to the context and the LLM
    # asked. owed: the arguments of a finalization the controller refused after
    # that, to be re-applied at the next quiet moment.
    turn = {"inference": False, "owed": None}

    # _start and _stop pass their arguments through as given: they are pipecat's private
    # signatures, and 1.9 grew _trigger_user_turn_inference_triggered a `speculation`
    # argument that a fixed (strategy) wrapper turned into a TypeError on every turn,
    # swallowed by the event handler. _inference reads its own, checked above.
    async def _start(strategy, *args, **kwargs):
        was_open = controller._user_turn
        await trigger_start(strategy, *args, **kwargs)
        # Only on a turn that actually STARTED. The controller drops a start while one
        # is open, and clearing here on that no-op would forget the owed finalization
        # of the very turn that is stuck.
        if not was_open and controller._user_turn:
            turn["inference"], turn["owed"] = False, None

    async def _inference(strategy, speculation=None):
        was_open = controller._user_turn
        await trigger_inference(strategy, speculation)
        # A speculative inference (1.9+) runs on a provisional copy and commits none of
        # the turn's words: a stop refused after one owes nothing.
        if was_open and speculation is None:
            turn["inference"] = True

    async def _stop(strategy, *args, **kwargs):
        was_open = controller._user_turn
        await trigger_stop(strategy, *args, **kwargs)
        # Still open after a stop that had a turn to close = refused. `_user_speaking`
        # is the only reason the controller has for that.
        refused = was_open and controller._user_turn
        if refused and turn["inference"]:
            turn["owed"] = (strategy, args, kwargs)
        elif not controller._user_turn:
            turn["owed"] = None

    async def _process_frame(frame):
        await process_frame(frame)
        # AFTER pipecat's own handling: this frame may be the VADUserStoppedSpeaking
        # that cleared _user_speaking, and the stop strategies have already seen it.
        owed = turn["owed"]
        if owed and controller._user_turn and not controller._user_speaking:
            # Re-applied at the first quiet frame, AFTER the stop strategies ran on it.
            # If Smart Turn just returned a FRESH incomplete (the user resumed
            # mid-thought and paused again), this closes the turn anyway and the
            # aggregator runs the resumed words as their own turn -- a continuation
            # split off (LateStartTurnStopStrategy.handle_user_turn_stopped reports
            # this close to the STT, so the segment it was holding for the ceiling's
            # verdict closes with the turn instead of waiting out its expiry). With the
            # reply hold, the split is undone wherever the first reply has not played:
            # words already in hand are committed by this stop with the gate armed
            # (CutReplyFence.hold_commits drops the reply and they merge), and words
            # that land after it start a turn whose interruption does the same.
            # Accepted rather than waiting for the SMARTTURN_STOP_SECS
            # ceiling, because Smart Turn is near-inert on this audio (bimodal and
            # confidently wrong; see endpointing.py's threshold note), so the case is
            # rare; revisit here if Smart Turn ever carries real signal.
            turn["owed"] = None
            logger.debug(f"{controller}: re-applying the turn finalization refused "
                         "while the user was speaking — an open turn takes no barge-in")
            strategy, args, kwargs = owed
            await trigger_stop(strategy, *args, **kwargs)

    controller._trigger_user_turn_start = _start
    controller._trigger_user_turn_inference_triggered = _inference
    controller._trigger_user_turn_stop = _stop
    controller.process_frame = _process_frame


# --- The cut reply is committed before the turn that cut it --------------------------
# A barge-in puts two messages into the context from two ends of the pipeline: the reply
# it cut, committed by the ASSISTANT aggregator (last) when the InterruptionFrame reaches
# it, and the caller's turn, committed by the USER aggregator. HeardContextCorrector
# finds the cut reply by position, just before the turn, so the reply has to land first.
# pipecat 1.8.1's broadcast_interruption() only queues the frame, and a turn that opens
# on its final after its own VAD stop (LateStartTurnStopStrategy, d78396d) broadcasts
# and commits in one controller call. Live 2026-10-13 the commit beat the interruption
# by ~60 ms: the corrector found nothing to correct, the count the caller had stopped
# was committed after the stop at full length, and the model counted again. The same
# order armed the reply hold (reply_hold.py) ahead of the turn's own interruption, which
# it then read as a dropped reply: a false TurnMerge for the next turn.
#
# CutReplyFence restores the order. A commit (_push_aggregation) and a speculative reply's
# snapshot (Speculator.start, whose corrector pass consumes the ledger's cut) wait until
# every InterruptionFrame sent down ahead of them has passed the assistant aggregator.
# Only an interruption in flight is waited on: ~70 ms over a playing reply, a few ms
# with the bot silent, nothing otherwise. The wait is bounded (CUT_COMMIT_WAIT_SECS);
# a timeout is logged, the frames are forgotten so they tax nothing later, the commit
# goes ahead in the old order and the speculation is not started.
#
# No deadlock: what the wait needs is the frame crossing the processors below the user
# aggregator, each on its own input task, and none of them waits on it. The wait blocks
# the task it runs on. For a final's commit that is the aggregator's frame task, and its
# system frames run on meanwhile. A commit or snapshot at a VAD stop or the silence
# ceiling runs on the INPUT task, so its audio, VAD and CancelFrame queue behind the
# wait -- only when an interim opened the turn less than ~70 ms before, and never past
# the bound. The controller's stop watchdog commits from its own task, and the session's
# end (EndFrame, a hangup's CancelFrame) commits whatever words are left: with an
# interruption in flight those wait too, up to the same bound -- a hangup can end
# 0.5 s later, never hang. A VAD start handled inside a wait is the refused-finalize
# shape keep_barge_in_reachable re-applies. Like keep_barge_in_reachable, it fails loud
# rather than guards what it wraps: a pipecat that renames _push_aggregation raises at
# session build; one that renames _aggregation errors at the first commit (logged as
# an uncaught event-handler exception where pipecat calls it from a handler).

# The longest a commit (or a snapshot) waits. ~70 ms is the live crossing; this is a
# ceiling for a loop under load, well short of the 0.45 s every such turn paid before
# d78396d.
CUT_COMMIT_WAIT_SECS = 0.5

# InterruptionFrames remembered at most: each is forgotten when it lands, so only frames
# something swallowed accumulate, and a timed-out wait forgets those too.
_MAX_IN_FLIGHT = 64


class CutReplyFence:
    """Holds the user turn's commit, and the speculative reply's snapshot, until the
    reply an interruption cut is in the context. See the note above. Three parts, built
    and placed by agent_session:

      * `head`: DIRECTLY below the user aggregator. It is called inside the aggregator's
        own push of the InterruptionFrame, so the frame is on record before the
        controller call that broadcast it can reach the commit. Any processor between
        them would take the frame on its own task, later.
      * `tail`: directly below the assistant aggregator, the last processor. A frame
        reaching it has been handled there: the cut reply is in the context.
      * wait_for_cuts(): the wait. hold_commits() puts it in front of a commit.

    Both processors run in direct mode (as pipecat's own pipeline source and sink do):
    they pass every frame on inline, adding no task and no queue to the path."""

    def __init__(self, *, timeout: float = CUT_COMMIT_WAIT_SECS):
        self._timeout = timeout
        # InterruptionFrame id -> a future resolved when it reaches the tail: True, or
        # False when it is forgotten (a wait timed out on it, or evicted) -- so a
        # concurrent wait holding it gives up too, and says so.
        self._in_flight: dict = {}
        self.head = _CutFenceHead(self)
        self.tail = _CutFenceTail(self)
        # () -> bool: none of the reply to the last commit has played yet. Set by
        # agent_session where there is a reply hold; see hold_commits.
        self.unplayed_reply = None
        # (marker) -> None: the caller's words are being committed now. `marker` is a
        # number off pipecat's frame counter, taken here in the aggregator's own task
        # just before the commit's frame is made: a frame numbered below it was made
        # before the commit. FollowupGate.words_committed; see hold_commits.
        self.on_words_committed = None
        # () -> bool: the latest VAD stop's close is still owed (the stop strategy's
        # receipts, LateStartTurnStopStrategy.close_owed). Set by agent_session.
        self.close_owed = None
        # Tallies, for the tests.
        self.waits = 0
        self.timeouts = 0

    def hold_commits(self, aggregator) -> None:
        """Wrap ONE user aggregator's _push_aggregation -- every path that commits the
        caller's words passes there: the stop strategy's inference, the turn's stop,
        the controller's watchdog, the session's end. Private, because since pipecat
        1.12 the public push_aggregation is only one of its callers: the turn's stop
        (_maybe_emit_user_turn_stopped, behind the watchdog and the session's end too)
        calls _push_aggregation(run_llm=...) itself, and a fence on push_aggregation
        let those commits through unwaited. AttributeError at session build if it moves,
        like keep_barge_in_reachable. An empty aggregation commits nothing (pipecat
        returns at once), so it does not wait: a hangup's CancelFrame is not held for a
        commit that will not happen.

        `self.unplayed_reply` (reply_hold.ReplyHoldGate's `armed`, where the session has
        the gate): True while none of the reply to the LAST commit has played. Asked once
        the wait is over, when every turn start's interruption has reached the gate --
        so True there means no turn has started since that commit: these words continue
        it. That is the refused finalize's second commit (keep_barge_in_reachable): the
        caller resumed inside the first commit's await, their words joined the turn
        still open, and a final of them in hand by their VAD stop is committed there with
        no turn start and so no interruption. Committed as it is, it leaves two user
        messages, and the first one's reply, held, is released and answers the fragment
        before the second answers the rest (formal/UserTurn.tla,
        ut_retryOnQuiet_early_nosplitturn). So the unplayed reply is dropped first: an
        InterruptionFrame pushed DOWN only (no turn starts, and nothing above the
        aggregator -- the STT holding the caller's words, the input transport -- has
        anything to drop). The gate reads it as it reads a turn start's: it drops the
        held reply and asks for the TurnMerge; the LLM cancels a reply still being
        written. Once it has landed (waited for like any other), the commit's words are
        folded into the unanswered message. Past the bound, nothing is asked: the commit
        goes ahead in the old order, as for any cut. If the wait for the continuation's
        own frame runs out, the commit goes ahead unmerged and the frame, landing late,
        asks the gate for a merge the next commit then gets: the fence's accepted
        residual, for this commit as for any (formal/UserTurn.tla, ContinueLate).

        Never at the session's end: pipecat commits an open turn's leftover words after
        pushing the EndFrame on (_maybe_emit_user_turn_stopped(on_session_end=True)), and
        an interruption there would overtake the End and drop the reply queued ahead of
        it -- a goodbye -- for a commit nothing will answer (UserTurn.tla, NoLostGoodbye).
        That wrapper passes the rest of its arguments through (1.12 added run_llm), and
        the session's end is read from its on_session_end, so where that sits is checked
        here, at session build, as keep_barge_in_reachable checks its hook. (pipecat's
        realtime mode commits at the End through push_aggregation directly, past this
        wrapper; the brain's LLM is no realtime service, so that mode never turns on.)

        Nor when the commit asks for no answer (run_llm False: pipecat 1.12 confirming
        a speculative reply already generated for these words): the interruption would
        drop that reply, and nothing would ask for another. The cost, logged when it is
        paid: the continuation stays its own message, the split turn the merge exists to
        prevent. Unreachable while the brain uses no speculating pipecat stop strategy,
        and formal/UserTurn.tla has no such path -- model it before adopting one. Where
        run_llm sits (keyword-only) is checked at session build with the stop hook.

        `self.on_words_committed`, where set (agent_session: FollowupGate.words_committed),
        is told of every commit of words, after the waits, with a marker taken off pipecat's
        frame counter just before the commit's frame is made -- except while the latest VAD
        stop's close is owed (`self.close_owed`): those words are not in the commit."""
        push_aggregation = aggregator._push_aggregation
        emit_stopped = aggregator._maybe_emit_user_turn_stopped
        params = [(p.name, p.kind) for p in inspect.signature(emit_stopped).parameters.values()]
        positional = inspect.Parameter.POSITIONAL_OR_KEYWORD
        if params[:2] != [("strategy", positional), ("on_session_end", positional)]:
            raise RuntimeError(
                f"pipecat's LLMUserAggregator._maybe_emit_user_turn_stopped takes "
                f"{[f'{n} ({k.name})' for n, k in params]}, not (strategy, on_session_end, "
                f"...): CutReplyFence.hold_commits needs a new look")
        run_llm = inspect.signature(push_aggregation).parameters.get("run_llm")
        if run_llm is None or run_llm.kind is not inspect.Parameter.KEYWORD_ONLY:
            raise RuntimeError(
                "pipecat's LLMUserAggregator._push_aggregation no longer takes a "
                "keyword-only run_llm: CutReplyFence.hold_commits reads it from kwargs")
        ending = False

        async def _maybe_emit_user_turn_stopped(strategy=None, on_session_end=False,
                                                *args, **kwargs):
            nonlocal ending
            ending = ending or on_session_end
            return await emit_stopped(strategy, on_session_end, *args, **kwargs)

        async def _push_aggregation(*args, **kwargs):
            if aggregator._aggregation:
                continues = (await self.wait_for_cuts() and not ending
                             and self.unplayed_reply is not None and self.unplayed_reply())
                if continues and not kwargs.get("run_llm", True):
                    logger.info("CUT-FENCE: words committed while the reply to the turn "
                                "they continue has not played, confirming a speculative "
                                "reply -- not merged, so they stay their own message")
                elif continues:
                    logger.info("CUT-FENCE: words committed while the reply to the turn "
                                "they continue has not played -- dropping that reply; "
                                "the model gets the turn as one message")
                    await aggregator.push_frame(InterruptionFrame())
                    await self.wait_for_cuts()
            if (aggregator._aggregation and self.on_words_committed is not None
                    and not (self.close_owed is not None and self.close_owed())):
                # After every wait: a VAD frame made during one is newer than this. Not
                # while the latest stop's close is owed: this commit (the re-applied
                # stop, in that stop's own handling) has what was final before it, not
                # what that stop flushed (formal/UserTurn.tla, gateOwedBlind).
                self.on_words_committed(obj_id())
            return await push_aggregation(*args, **kwargs)

        aggregator._push_aggregation = _push_aggregation
        aggregator._maybe_emit_user_turn_stopped = _maybe_emit_user_turn_stopped

    def _sent(self, frame) -> None:
        self._in_flight[frame.id] = asyncio.get_running_loop().create_future()
        while len(self._in_flight) > _MAX_IN_FLIGHT:
            self._forget(next(iter(self._in_flight)))

    def _landed(self, frame) -> None:
        fut = self._in_flight.pop(frame.id, None)
        if fut is not None and not fut.done():
            fut.set_result(True)

    def _forget(self, key) -> None:
        fut = self._in_flight.pop(key)
        if not fut.done():
            fut.set_result(False)

    async def wait_for_cuts(self) -> bool:
        """Wait until every interruption sent down so far has passed the assistant
        aggregator. False if the bound ran out first (or another wait's did): the cut
        reply may still land later."""
        ahead = list(self._in_flight.values())
        if not ahead:
            return True
        self.waits += 1
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        landed, late = await asyncio.wait(ahead, timeout=self._timeout)
        waited = loop.time() - t0
        lost = len(late) + sum(1 for f in landed if f.result() is False)
        if lost:
            # Forgotten, not waited on again: a frame something swallowed must not tax
            # every commit after this one.
            self.timeouts += 1
            for key in [k for k, f in self._in_flight.items() if f in late]:
                self._forget(key)
            logger.warning(f"CUT-FENCE: {lost} interruption(s) had not reached the "
                           f"assistant aggregator after {waited:.2f}s -- going ahead; a "
                           "reply they cut may land after the caller's turn")
            return False
        logger.debug(f"CUT-FENCE: waited {waited * 1000:.0f} ms for the interruption "
                     "ahead to commit the reply it cut")
        return True


class _CutFenceHead(FrameProcessor):
    """CutReplyFence's record of the interruptions going down. See CutReplyFence."""

    def __init__(self, fence: CutReplyFence):
        super().__init__(enable_direct_mode=True)
        self._fence = fence

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame) and direction == FrameDirection.DOWNSTREAM:
            self._fence._sent(frame)
        await self.push_frame(frame, direction)


class _CutFenceTail(FrameProcessor):
    """CutReplyFence's record of the interruptions the assistant aggregator has handled."""

    def __init__(self, fence: CutReplyFence):
        super().__init__(enable_direct_mode=True)
        self._fence = fence

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame) and direction == FrameDirection.DOWNSTREAM:
            self._fence._landed(frame)
        await self.push_frame(frame, direction)


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
# A rate of 8000 hands Silero the TRUE narrowband signal instead. Decimating back to
# 8 kHz discards no information when the source was 8 kHz to begin with — it undoes the
# gateway's upsampling rather than degrading anything — and 512 samples at 16 kHz
# decimate to exactly the 256 Silero wants at 8 kHz, so pipecat's buffering is untouched.
#
# It is chosen per front-end (TurnSettings.vad_sample_rate: SIP_/TALK_/ROOM_VAD_SAMPLE_RATE
# over the shared VAD_SAMPLE_RATE, issue #215), and it is ON for the phone alone:
# SIP_VAD_SAMPLE_RATE defaults to 8000, while /talk and the room mic take the shared
# 16000. Until #215 it was one process-wide value and off by default, because one brain
# process serves all three (#58) and the other two are genuinely wideband: there it
# would throw away the top half of the band, and the pair-averaging below (a crude
# half-band filter, kept cheap because it runs per frame in the VAD executor) would not
# fully prevent aliasing. The phone's default is only correct while the carrier is
# narrowband: revisit SIP_VAD_SAMPLE_RATE together with the SDP offer if a wideband
# carrier ever lands.
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
