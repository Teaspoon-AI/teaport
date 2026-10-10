# SPDX-License-Identifier: MIT
#
# barge_pause.py — stop talking the moment the caller does, and pick up again if it
# was nothing (teaport#34).
#
# Barge-in on this brain is a user turn START, and a turn starts from a TRANSCRIPT
# (MinWordsUserTurnStartStrategy). While the bot is speaking the engine STT returns
# nothing for the caller's words until the VAD stop flushes the segment
# (teagram-engine#7), so the bot talks on until the caller has finished talking over
# it: from the caller's first syllable to the bot going quiet is typically most of a
# second, and longer the longer they talk. The VAD does not close that gap on its own:
# it decides where an utterance starts and stops for the STT and the turn, never
# interrupts, and its SPEAKING edge lags the onset of speech by a few hundred ms (its
# volume is integrated over a rolling 400 ms window). A caller who talks over a bot
# wants it to stop at once; a cough, a breath or an "mm" should not cost them the
# reply.
#
# BargeInPauser takes the decision apart. The moment speech starts while the bot is
# audible -- the faster onset test in speech_onset.py, or the VAD's start -- it PAUSES
# playout at the output transport: no more audio is written, and none is thrown away.
# A caller already talking when the bot starts is paused at that start.
# Then:
#
#   * Words arrive: the normal barge-in runs (TEAPORT_INTERRUPT_MIN_WORDS words, as
#     before -- or, while paused, a single stop word, PauseAwareMinWordsStrategy), the
#     interruption cancels the reply and the caller has the turn.
#   * No words: a cough, a breath, a backchannel too short to count. Playout RESUMES
#     from the very sample it stopped on -- the audio waited in the transport's own
#     queue -- so nothing is lost or repeated. It resumes as soon as the STT's close of
#     that speech has come back with no turn in it (a final under the word guard, or a
#     wordless close) plus a short grace, and at the latest TEAPORT_BARGE_PAUSE_RESUME_S
#     after the line went quiet -- which, with the VAD's own stop delay, is a stall of
#     about ENDPOINT_STOP_SECS + TEAPORT_BARGE_PAUSE_RESUME_S (~1.0 s at 0.2 + 0.8) for
#     a cough. A blip under 250 ms resumes after 0.3 s. TEAPORT_BARGE_PAUSE_MAX_S caps a
#     pause from its start.
#
# Three things it does NOT pause, each because pausing there cost more than it saved:
#
#   * The end of a reply. A pause holds back the reply's TTSStoppedFrame with its audio,
#     so the bot counts as speaking until the resume -- and a one-word answer ("Yes.")
#     spoken over the last syllable of "...shall I book it?" landed under the 2-word
#     barge-in guard and was dropped. So no pause when the reply is fully synthesized
#     and at most 0.5 s of it is left to play; and while paused, the start strategy
#     treats the bot as silent once the audio it held back would have finished playing
#     anyway (PlayoutPauseFrame.tail_secs): a single word then starts the turn, as it
#     would have without the pause.
#   * The thinking-sound bed (audio with no TTS context). Pausing it only delayed the
#     consult answer queued behind it.
#   * More than two ECHO-LIKE pauses in one bot-speaking window, on the onset test alone:
#     after two pauses that resumed on a blip (speech under 250 ms, no words), only the
#     VAD's start pauses until the bot stops. Bounds the stutter if echo residue trips
#     the onset test, which has no volume gate; a backchannel, a cough or a pause the cap
#     ended does not count against it.
#
# The echo guard is the onset test itself: 128 ms of Silero confidence >= 0.6, which
# typically fires within ~100-250 ms of the onset of real speech -- well ahead of the
# VAD's edge -- while a short click or a burst of echo residue does not last long
# enough. How well it rejects echo depends on the line: behind the SIP gateway's echo
# canceller the caller's audio is near silent while they are quiet, and a line with
# real echo residue, a poor handset or background speech is where it would trip. The
# limit on echo-like pauses below bounds that; raise TEAPORT_ONSET_MIN_MS /
# TEAPORT_ONSET_CONFIDENCE if the bot stutters on its own echo.
#
# WHERE THE PAUSE HAPPENS. In the output transport (SipGatewayOutputTransport), which
# simply stops writing until resumed: everything after the pause point stays in
# pipecat's MediaSender queue, in order. The SIP gateway's playout queue holds only
# what the brain's real-time send clock has put there -- about one 20 ms frame, because
# that clock (SipGatewayOutputTransport._write_audio_sleep) sends with ZERO lead: a
# burst-lead send clock would leave that much more in the gateway's queue to play
# through a pause -- so a brain-side pause is audible within ~40 ms and the gateway
# needs no pause control of its own (it has none; protocol v0 has no flush either). The OpenClaw Talk relay
# is different: the client buffers audio we cannot see, and its only control is
# "clear", which throws the buffer away -- a pause there would lose audio, and a
# resume would have to know how much. So the pauser is only built for a transport
# that can pause (supports_playout_pause); on Talk it is absent and barge-in is as
# before.
#
# BOOKKEEPING. The transport announces each pause and resume (PlayoutPauseFrame, both
# directions): the TranscriptLedger freezes its playout layout through it and moves
# the word times of the replies it delays (heard accounting at a later cut stays
# exact), the TTS freezes its playout model (as for a reply hold), and the start
# strategy learns the bot is silent.
#
# Known limits:
#   * A pause is ~150-250 ms behind the caller's onset, plus the gateway's frame and
#     the network; what played before it is heard.
#   * While paused, the transport keeps releasing word captions on the wall clock (the
#     assistant aggregator's running text runs ahead of the voice; the heard
#     correction at a cut trims it back to what was heard, as on any cut).
#   * A caller who talks without pause for longer than the cap hears the bot resume
#     over them until their words arrive.
#
import asyncio
import re
import time
from dataclasses import dataclass

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    SystemFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy

from teaport_brain.endpointing import SegmentDoneFrame
from teaport_brain.settings import setting

# On for a transport that can pause (SIP); there is none to pause on Talk.
ENABLED = setting("TEAPORT_BARGE_PAUSE")
# The LONGEST a pause with no words waits after the caller's speech (both the VAD and
# the onset test) before it resumes; it resumes sooner once the STT's close of that
# speech is back with no turn in it. The caller's words come with the VAD stop's commit
# at the latest (the STT commits at once while the bot is speaking), ~0.1-0.5 s after it.
RESUME_SECS = min(8.0, max(0.0, setting("TEAPORT_BARGE_PAUSE_RESUME_S")))
# The longest a pause lasts, from its start. Kept well under pipecat's
# audio_out_write_timeout_secs (10 s), past which a stalled write writes the transport
# off for the rest of the call; the transport resumes on its own at 8 s regardless.
MAX_SECS = setting("TEAPORT_BARGE_PAUSE_MAX_S")
# What ends a paused reply on its own, whatever TEAPORT_INTERRUPT_MIN_WORDS says: with
# the bot silent there is no echo to garble into one, and a lone "Stop." is the most
# natural thing to say. An utterance counts only if EVERY word of it is one of these
# (so "don't stop" is not a stop), entries may be phrases ("hold on"), and any "shh"
# ("shhh", "sshh") is "shh". "No" is deliberately not one: over the bot it is as often
# an answer to what is being said as an objection to it.
STOP_WORDS = tuple(
    w.strip().lower() for w in setting("TEAPORT_STOP_WORDS").split(",")
    if w.strip())

# No pause when the reply is fully synthesized and at most this much of it is left.
_NEAR_END_S = 0.5
# Speech shorter than this is a blip: its pause resumes after _BLIP_RESUME_S.
_BLIP_S = 0.25
_BLIP_RESUME_S = 0.3
# After the STT's close of the speech came back with no turn, resume this much later.
_FINAL_GRACE_S = 0.15
# Echo-like pauses (resumed on a blip) per bot-speaking window before the onset test
# alone stops pausing.
_MAX_NOWORD_PAUSES = 2


@dataclass
class PlayoutPauseFrame(SystemFrame):
    """The output transport stopped writing the bot's audio (`paused`) or started
    again. Pushed both ways by the transport itself, as it does Bot{Started,Stopped}
    SpeakingFrame, with sibling ids."""

    paused: bool = True
    # How long after this frame the audio really stops: the frame already written plays
    # out its 20 ms slot first. The ledger credits playout up to then.
    lag_secs: float = 0.0
    # On a pause: the audio it held back, when the reply is fully synthesized (so this is
    # all that is left to play), else -1. The start strategy treats the bot as silent
    # once this much time has passed since the pause began: it would have finished by
    # then without the pause. A reply whose synthesis completes DURING the pause gets
    # the pause announced again with its tail (sip_transport._track_tail).
    tail_secs: float = -1.0


class BargeInPauser(FrameProcessor):
    """Pause playout on the caller's speech while the bot is audible; resume if no
    words come. Place it directly above the output transport (it reads the transport's
    upstream Bot{Started,Stopped}SpeakingFrames). See the module header."""

    def __init__(self, output, *, resume_secs: float = RESUME_SECS,
                 max_secs: float = MAX_SECS, near_end_secs: float = _NEAR_END_S,
                 max_noword_pauses: int = _MAX_NOWORD_PAUSES):
        super().__init__()
        self._output = output
        self._resume_secs = min(8.0, max(0.0, resume_secs))
        self._max_secs = max(self._resume_secs, min(max_secs, 8.0))
        self._near_end_secs = near_end_secs
        self._max_noword = max_noword_pauses
        self._bot_speaking = False
        # The audio going to the transport is a reply's (it has a TTS context), not the
        # thinking-sound bed's.
        self._reply_audio = False
        self._sources = {"vad": False, "onset": False}
        self._speech_t0 = None
        self._paused = False
        self._pause_t0 = 0.0
        self._resume_task = None
        self._cap_task = None
        # Echo-like pauses (resumed on a blip, no words) in this bot-speaking window.
        self._noword = 0
        # Tallies, for the log and the tests.
        self.pauses = 0
        self.resumes = 0
        self.cancels = 0
        self.skipped_near_end = 0
        self.skipped_onset = 0

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def _speaking(self) -> bool:
        return any(self._sources.values())

    async def on_onset(self, started: bool):
        """speech_onset.SpeechOnsetMixin listener (the user aggregator's task)."""
        await self._speech("onset", started)

    async def on_final_without_turn(self):
        """PauseAwareMinWordsStrategy: the STT closed the caller's speech and it started
        no turn (a final under the word guard, or a close with no words). If the line is
        quiet, resume after a short grace rather than the full window."""
        if self._paused and not self._speaking:
            self._schedule_resume(_FINAL_GRACE_S, "the STT's close of it had no turn in it",
                                  echo=False)

    async def _speech(self, source: str, started: bool):
        was_speaking = self._speaking
        self._sources[source] = started
        if started:
            if not was_speaking:
                self._speech_t0 = time.monotonic()
            self._cancel(self._resume_task)
            self._resume_task = None
            if self._may_pause(source):
                await self._pause(source)
        elif self._paused and not self._speaking:
            blip = (self._speech_t0 is not None
                    and time.monotonic() - self._speech_t0 < _BLIP_S)
            self._schedule_resume(
                min(self._resume_secs, _BLIP_RESUME_S) if blip else self._resume_secs,
                "a blip" if blip else
                f"no words within {self._resume_secs:.1f}s of the caller stopping",
                echo=blip)

    def _may_pause(self, source: str) -> bool:
        if self._paused or not self._bot_speaking or not self._reply_audio:
            return False
        if source == "onset" and self._noword >= self._max_noword:
            # Two echo-like pauses in this window already: echo, or a noisy line. The
            # VAD's start (with its volume gate) still pauses.
            self.skipped_onset += 1
            return False
        tail = getattr(self._output, "playout_tail_secs", lambda: None)()
        if tail is not None and tail <= self._near_end_secs:
            self.skipped_near_end += 1
            logger.debug(f"BARGE-PAUSE: not pausing -- the reply ends in {tail:.2f}s")
            return False
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            if self._speaking:
                # The caller is already talking as the bot starts: a rising edge would
                # never come.
                src = "vad" if self._sources["vad"] else "onset"
                if self._may_pause(src):
                    await self._pause(src, already=True)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._noword = 0
            self._reply_audio = False
        elif isinstance(frame, TTSAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            self._reply_audio = getattr(frame, "context_id", None) is not None
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            await self._speech("vad", True)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._speech("vad", False)
        elif isinstance(frame, InterruptionFrame):
            # The words came: the barge-in cancels the reply. The transport drops its
            # pause with the queued audio when the interruption reaches it.
            if self._paused:
                self.cancels += 1
                logger.info(f"BARGE-PAUSE: cancelled after "
                            f"{asyncio.get_running_loop().time() - self._pause_t0:.2f}s -- "
                            "the caller's words take the turn")
            self._end()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._end()
        await self.push_frame(frame, direction)

    async def _pause(self, source: str, already: bool = False):
        self._paused = True
        self._pause_t0 = asyncio.get_running_loop().time()
        self.pauses += 1
        logger.info(f"BARGE-PAUSE: caller speech "
                    f"{'already under way as the bot starts' if already else 'while the bot talks'}"
                    f" ({'speech onset' if source == 'onset' else 'VAD'}) -- playout paused")
        await self._output.set_playout_paused(True)
        self._cap_task = self.create_task(self._cap(), name="barge-pause-cap")

    async def _resume(self, why: str, echo: bool = False):
        if not self._paused:
            return
        held = asyncio.get_running_loop().time() - self._pause_t0
        self._end()
        self.resumes += 1
        if echo:
            self._noword += 1
        logger.info(f"BARGE-PAUSE: resumed after {held:.2f}s ({why}); "
                    f"echo-like pauses this window: {self._noword}")
        await self._output.set_playout_paused(False)

    def _end(self):
        self._paused = False
        for task in (self._resume_task, self._cap_task):
            self._cancel(task)
        self._resume_task = self._cap_task = None

    def _schedule_resume(self, delay: float, why: str, *, echo: bool):
        self._cancel(self._resume_task)
        self._resume_task = self.create_task(self._resume_after(delay, why, echo),
                                             name="barge-resume")

    async def _resume_after(self, delay: float, why: str, echo: bool):
        await asyncio.sleep(delay)
        self._resume_task = None
        await self._resume(why, echo)

    async def _cap(self):
        await asyncio.sleep(self._max_secs)
        self._cap_task = None
        await self._resume(f"pause cap {self._max_secs:.1f}s")

    @staticmethod
    def _cancel(task):
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def cleanup(self):
        self._end()
        if self.pauses:
            logger.info(f"BARGE-PAUSE summary: {self.pauses} pause(s): {self.cancels} "
                        f"cancelled by words, {self.resumes} resumed with none; skipped "
                        f"{self.skipped_near_end} near a reply's end, "
                        f"{self.skipped_onset} on the onset test past the echo-like limit")
        await super().cleanup()


_WORD = re.compile(r"[^\w']+")
_SHH = re.compile(r"s*h{2,}")


def _tokens(text: str) -> list:
    out = []
    for w in _WORD.split((text or "").lower()):
        w = w.strip("'")
        if w:
            out.append("shh" if _SHH.fullmatch(w) else w)
    return out


def is_stop_utterance(text: str, stop_words=STOP_WORDS) -> bool:
    """True when EVERY word of `text` belongs to a stop entry ("Stop.", "Wait, wait!",
    "Hold on.", "Shhh"), so "don't stop" or "stop by the shop" are not stops."""
    toks = _tokens(text)
    if not toks:
        return False
    phrases = sorted({tuple(_tokens(e)) for e in stop_words if _tokens(e)}, key=len,
                     reverse=True)
    i = 0
    while i < len(toks):
        for ph in phrases:
            if tuple(toks[i:i + len(ph)]) == ph:
                i += len(ph)
                break
        else:
            return False
    return True


class PauseAwareMinWordsStrategy(MinWordsUserTurnStartStrategy):
    """The brain's barge-in rule, plus, while playout is paused (PlayoutPauseFrame):

      * an utterance made only of stop words starts the turn whatever its length;
      * once the audio the pause held back would have finished playing anyway (the
        pause frame's tail_secs), the bot counts as silent and one word is a turn --
        as it would have been without the pause;
      * a final (or a wordless close) that starts no turn tells the pauser, which then
        resumes sooner (on_final_without_turn).

    Anything else counts words exactly as MinWordsUserTurnStartStrategy does, so a
    backchannel ("yeah") still does not cut the bot, and the pause it caused resumes."""

    def __init__(self, *, stop_words=STOP_WORDS, on_final_without_turn=None, **kwargs):
        super().__init__(**kwargs)
        self._stop_words = tuple(stop_words)
        self._on_final_without_turn = on_final_without_turn
        self._playout_paused = False
        self._pause_t0 = None
        self._would_end_at = None

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, PlayoutPauseFrame):
            if frame.paused:
                if not self._playout_paused:
                    self._pause_t0 = time.monotonic()
                # Anchored at the pause's START: nothing played since, so a tail the
                # transport learns later (the reply's synthesis completed during the
                # pause; it re-announces the pause) is measured from the same instant.
                if frame.tail_secs >= 0:
                    self._would_end_at = self._pause_t0 + frame.tail_secs
            else:
                self._pause_t0 = self._would_end_at = None
            self._playout_paused = frame.paused
        elif isinstance(frame, (BotStoppedSpeakingFrame, InterruptionFrame)):
            self._playout_paused = False
            self._pause_t0 = self._would_end_at = None
        elif isinstance(frame, SegmentDoneFrame) and self._playout_paused:
            await self._notify()
        return await super().process_frame(frame)

    async def _notify(self):
        if self._on_final_without_turn is not None:
            await self._on_final_without_turn()

    async def _handle_transcription(self, frame):
        if not (self._bot_speaking and self._playout_paused):
            return await super()._handle_transcription(frame)
        if is_stop_utterance(frame.text, self._stop_words):
            logger.debug(f"{self}: stop word while playout is paused "
                         f"({frame.text!r}) -- starting the turn")
            await self.trigger_user_turn_started()
            return ProcessFrameResult.STOP
        if self._would_end_at is not None and time.monotonic() >= self._would_end_at:
            # Without the pause the reply would be over by now and this would be an
            # answer to it, not a barge-in: count it as one.
            saved, self._bot_speaking = self._bot_speaking, False
            result = await super()._handle_transcription(frame)
            if result != ProcessFrameResult.STOP:
                self._bot_speaking = saved
            return result
        result = await super()._handle_transcription(frame)
        if result != ProcessFrameResult.STOP and isinstance(frame, TranscriptionFrame):
            await self._notify()
        return result
