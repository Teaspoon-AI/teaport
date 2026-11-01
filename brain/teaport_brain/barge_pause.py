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
#     pause from its start; a barge-in the STT still holds there (stt.py TURN TEXT)
#     closes on the words in hand first, and the reply resumes _FINAL_GRACE_S after
#     that close, so its words are decided paused.
#
# THE JUDGE (#224, TEAPORT_BARGE_JUDGE). Words that would start the turn over a paused
# reply -- "Okay.", "Mm-hmm.", as well as "Wait, what?" -- do not start it at once:
# BargeInJudgeGate, in front of the user aggregator, holds the STT's frames and asks a
# one-word judge (the cut judge's request path, owed.py) whether the reply should carry
# on, stop, or answer them. CONTINUE drops the words -- a final becomes the wordless
# close a cough gives -- and the reply resumes from the sample it paused on, as it does
# for a cough. STOP or ANSWER, or no verdict within _JUDGE_WAIT_S of being asked,
# lets the frames on as they came, and the turn starts exactly as it would have without
# the judge, that much later; so does a stop word, or the reply's would-be end, arriving
# while it decides. Stop words (below), a pause whose reply would be over by now and an
# open user turn's own words are not judged. formal/UserTurn.tla's JUDGE rows check the
# gate around the verdict (formal/README.md). Replayed on gpt-oss-120b (2026-10-25, 7
# contexts, 1,722 trials): backchannels 459/462 CONTINUE, stops and hold-ons 374/378 and
# questions and corrections 331/336 not; its first token p50 0.21 s, p90 0.40 s from the
# request. The in-context alternative (a continue_speaking tool in the turn's own
# context) continued over 61 of 96 questions. A pause with no words in it is no
# different with the judge: an empty transcript that was really a "stop" still resumes
# (the STT's to fix, not a decision's).
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
#   * The thinking sound's typing: plain audio, not bot speech (thinking_sound.py), so
#     no window to pause. Pausing it only delayed the consult answer queued behind it.
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
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy

from teaport_brain.endpointing import (
    NO_STOP,
    STT_FRAMES,
    SegmentDoneFrame,
    SegmentResetFrame,
)
from teaport_brain.settings import setting
from teaport_brain.word_tokens import word_tokens

# On for a transport that can pause (SIP); there is none to pause on Talk.
ENABLED = setting("TEAPORT_BARGE_PAUSE")
# The LONGEST a pause with no words waits after the caller's speech (both the VAD and
# the onset test) before it resumes; it resumes sooner once the STT's close of that
# speech is back with no turn in it. The caller's words come with the VAD stop's commit
# at the latest (the STT commits at once while the bot is speaking), ~0.1-0.5 s after it.
RESUME_SECS = min(8.0, max(0.0, setting("TEAPORT_BARGE_PAUSE_RESUME_S")))
# The longest a pause lasts, from its start: at most MAX_CAP_S, below the transport's
# own end to a pause (PAUSE_GUARD_S).
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
# The transport's own end to a pause (sip_transport._wait_if_paused, which takes it from
# here): a write blocked this long resumes whatever the pauser asks, since pipecat's
# MediaSender bounds a write at audio_out_write_timeout_secs (10 s) and treats running
# past it as the transport dead for the rest of the call.
PAUSE_GUARD_S = 8.0
# The longest cap, TEAPORT_BARGE_PAUSE_MAX_S's maximum (a test holds the schema to it):
# the cap's own resume, up to _FINAL_GRACE_S after its close of a held barge-in (_cap),
# comes before the transport's guard -- with _CAP_SLACK_S for that close itself and a
# stalled event loop -- so the close is decided paused whatever the cap.
_CAP_SLACK_S = 0.1
MAX_CAP_S = PAUSE_GUARD_S - _FINAL_GRACE_S - _CAP_SLACK_S
# Echo-like pauses (resumed on a blip) per bot-speaking window before the onset test
# alone stops pausing.
_MAX_NOWORD_PAUSES = 2

# Words over a paused reply are judged (BargeInJudgeGate) rather than starting the turn
# on their own. Only where the pauser is.
JUDGE = setting("TEAPORT_BARGE_JUDGE")
# The longest the gate waits for a verdict before the words go on as they would have; a
# later verdict is dropped. 0.8 s timed out 2 of 25 on a live call (an "Okay." that then
# cut the reply) though the box's own judge requests, off a call, were p99 0.62 s, max
# 0.74 s (100 sequential, 2026-10-25): the tail is the call's load. Only a timeout waits
# this long -- a verdict is acted on when it comes. Each request gets its own wait: a
# caller still talking is not a slow judge (one deadline for a whole hold cut "Yeah, now
# you found it." after three quick CONTINUEs on its growing words, 2026-10-25), and a
# stop word does not wait on it at all.
_JUDGE_WAIT_S = 1.2

CONTINUE, STOP, ANSWER = "continue", "stop", "answer"

# The replay's prompt (2026-10-25; see the module header), word for word.
JUDGE_PROMPT = (
    "A voice assistant was speaking on a phone call and paused because the caller started to "
    "speak. You see what the caller said before the assistant started, what the assistant was "
    "saying, how far the caller had heard, and what the caller said over it. Decide what the "
    "assistant should do now. CONTINUE: the caller's words need no response at all -- they "
    "were only showing they are listening, or it was a cough or a noise -- so it carries on "
    "from where it paused. STOP: they want it to stop or wait. ANSWER: they asked something, "
    "corrected it, or said anything else that needs a reply. Reply with exactly one word: "
    "CONTINUE, STOP or ANSWER.")
# The heard part, as the judge sees it: its last words.
_HEARD_WORDS = 12


def judge_messages(asked: str, reply: str, heard: str, said: str) -> list:
    """The judge's request: the caller's words framed as the request the reply answers.
    Framed only as "said" (the replay's first form), a "Yeah." a few words into the
    answer to "Okay. Actually, can you explain how ... works?" was an ANSWER to the
    can-you -- 30/60 on the live case, 42 of 600 on synthetic ones of its shape; framed
    as answered, 0/60 and 25 of 1,500, most of them "Got it." read as STOP (a cut, the
    safe side), with questions and stops talked over 12/700 against 13/700 (the
    re-measure, 2026-10-25)."""
    words = heard.split()
    tail = ("…" if len(words) > _HEARD_WORDS else "") + " ".join(words[-_HEARD_WORDS:])
    return [{"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": f"The caller had asked: \"{asked}\"\n\nThe assistant "
                                        f"was answering it: \"{reply}\"\n\nThe caller had "
                                        f"heard up to: \"{tail}\"\n\nThen, while the "
                                        f"assistant was still talking, the caller said: "
                                        f"\"{said}\""}]


def judge_verdict(reply_text: str):
    """The judge's answer: CONTINUE, STOP, ANSWER, or None for anything else."""
    t = (reply_text or "").strip().upper()
    for verdict in (CONTINUE, STOP, ANSWER):
        if t.startswith(verdict.upper()):
            return verdict
    return None


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
        self._max_secs = min(MAX_CAP_S, max(self._resume_secs, max_secs))
        self._near_end_secs = near_end_secs
        self._max_noword = max_noword_pauses
        self._bot_speaking = False
        # The audio going to the transport is a reply's (it has a TTS context).
        self._reply_audio = False
        self._sources = {"vad": False, "onset": False}
        self._speech_t0 = None
        self._paused = False
        self._pause_t0 = 0.0
        self._resume_task = None
        self._cap_task = None
        # Who is still deciding the caller's words over the pause -- the judge
        # (BargeInJudgeGate), the STT waiting on a barge-in's final (stt.py TURN TEXT): no
        # resume on the quiet window meanwhile. The cap still ends the pause.
        self._holds: set = set()
        # Called (async) at the cap, before it resumes, while anyone holds: the STT's
        # close of a held barge-in on the words in hand (set by build_agent_session).
        self.before_cap = None
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

    @property
    def _held(self) -> bool:
        return bool(self._holds)

    def hold_resume(self, on: bool, who: str = "the judge"):
        """Words over the pause are being decided (`on`), or no longer are, by `who`: the
        judge (BargeInJudgeGate), or the STT, whose barge-in turn waits on its final
        (stt.py TURN TEXT). Each holds and lets go its own. While any holds, nothing but
        the cap resumes; after, a quiet line resumes on the usual window if nothing sooner
        does (the close that follows)."""
        if on:
            self._holds.add(who)
            self._cancel(self._resume_task)
            self._resume_task = None
            return
        self._holds.discard(who)
        if (not self._holds and self._paused and not self._speaking
                and self._resume_task is None):
            self._schedule_resume(self._resume_secs, f"{who} let the reply carry on",
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
        self._holds.clear()
        for task in (self._resume_task, self._cap_task):
            self._cancel(task)
        self._resume_task = self._cap_task = None

    def _schedule_resume(self, delay: float, why: str, *, echo: bool):
        if self._held:
            return
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
        why = f"pause cap {self._max_secs:.1f}s"
        if self._holds and self.before_cap is not None:
            # Whoever still holds it decides first, while the reply is paused: the STT
            # closes a barge-in still waiting on its final on the words in hand
            # (stt.close_held, wired by build_agent_session).
            closed = False
            try:
                closed = await self.before_cap()
            except Exception as e:  # noqa: BLE001 -- the cap must still resume
                logger.warning("BARGE-PAUSE: the cap's close of the held words failed "
                               f"({e!r})")
            if closed and self._paused:
                # The close goes down the pipeline as data, through each processor's
                # queue; the resume would go up from the transport as a system frame,
                # past every queue, and could reach the start strategy first -- the
                # words then judged as over a reply already playing. So the grace a
                # close with no turn in it gets: the barge-in's interruption, or that
                # close itself (on_final_without_turn), ends the pause sooner -- and this
                # wait with it (_end).
                self._cap_task = asyncio.current_task()
                await asyncio.sleep(_FINAL_GRACE_S)
                self._cap_task = None
                why += f", {_FINAL_GRACE_S:.2f}s after the held words' close"
        await self._resume(why)

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


_SHH = re.compile(r"s*h{2,}")


def _tokens(text: str) -> list:
    """The words of `text` (word_tokens, the rule sliced turn texts share), every "shhh"
    as "shh"."""
    return ["shh" if _SHH.fullmatch(w) else w for w in word_tokens(text)]


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
        # taken_turn.TakenWords, where the session keeps one (agent_session).
        self.taken_words = None
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

    def judges(self, text: str) -> bool:
        """BargeInJudgeGate: would `text` start a turn now as a barge-in into a paused
        reply -- one the judge decides instead? Not a stop word, nor once the reply would
        have been over anyway: those start it here as they always did."""
        return (self.paused_for_judging() and len(text.split()) >= self._min_words
                and not self.stops(text))

    def paused_for_judging(self) -> bool:
        """The bot is paused mid-reply, and its held-back audio would not be over yet."""
        return (self._bot_speaking and self._playout_paused
                and not (self._would_end_at is not None
                         and time.monotonic() >= self._would_end_at))

    def stops(self, text: str) -> bool:
        return is_stop_utterance(text, self._stop_words)

    async def _notify(self):
        if self._on_final_without_turn is not None:
            await self._on_final_without_turn()

    async def _handle_transcription(self, frame):
        result = await self._count(frame)
        if (result != ProcessFrameResult.STOP and isinstance(frame, TranscriptionFrame)
                and (frame.text or "").strip() and self.taken_words is not None):
            # A final that starts no turn: the aggregator drops its words (the guard's
            # reset), and they go on record as dropped (taken_turn.TakenWords).
            await self.taken_words.took(frame.text, "dropped by the barge-in guard")
        return result

    async def _count(self, frame):
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


def _key(text: str) -> tuple:
    return tuple(_tokens(text))


def _close_of(final) -> SegmentDoneFrame:
    """A final's segment close without its words, stamped as the final was."""
    stop_n = getattr(final, "stop_n", None)
    return SegmentDoneFrame(stop_n=NO_STOP if stop_n is None else stop_n)


def _open_words(held: list) -> str:
    """The latest interim after the last segment end in `held`: the words of a segment
    still open (an interim repeats its segment's words so far); "" once it has ended --
    its final, or a close or reset without one (a final the echo guard emptied)."""
    words = ""
    for f in held:
        if isinstance(f, (TranscriptionFrame, SegmentDoneFrame, SegmentResetFrame)):
            words = ""
        elif isinstance(f, InterimTranscriptionFrame) and f.text.strip():
            words = f.text.strip()
    return words


def _held_words(held: list) -> str:
    """Every word in `held`, as the judge is asked on them: each closed segment's final,
    then the open one's latest interim."""
    finals = [f.text.strip() for f in held
              if isinstance(f, TranscriptionFrame) and f.text.strip()]
    return " ".join(finals + [w for w in (_open_words(held),) if w])


class BargeInJudgeGate(FrameProcessor):
    """Holds the caller's words over a paused reply while the judge decides them (see
    the module header). Place it directly above the user aggregator.

    `strategy` is the session's PauseAwareMinWordsStrategy (which words it judges),
    `judge` an async (said) -> CONTINUE | STOP | ANSWER | None, and `hold_resume` the
    pauser's (BargeInPauser.hold_resume), called True while a verdict is awaited."""

    def __init__(self, strategy, judge, *, hold_resume=None, wait_secs: float = _JUDGE_WAIT_S):
        super().__init__()
        self._strategy = strategy
        self._judge = judge
        self._hold_resume = hold_resume
        self._wait_secs = wait_secs
        # The STT's frames held since the first judged words, in order; empty when not
        # judging. A lock keeps a release in one piece against frames still arriving.
        self._held: list = []
        self._lock = asyncio.Lock()
        # All the held words, and the judge asked now (on which words).
        self._said = ""
        self._task = None
        # A user turn is open (the aggregator below broadcasts its start and stop): its
        # words are its own, never judged.
        self._turn_open = False
        # Words judged CONTINUE, until their segment closes: the final that repeats an
        # interim's verdict is not asked again, nor does it cut the resumed reply.
        self._continued = None
        # Tallies, for the log and the tests.
        self.verdicts = {CONTINUE: 0, STOP: 0, ANSWER: 0, None: 0}
        # taken_turn.TakenWords, where the session keeps one (agent_session).
        self.taken_words = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # The STT's frames (endpointing.STT_FRAMES), held together: no close, report or
        # turn-text correction overtakes the words it follows.
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, STT_FRAMES):
            async with self._lock:
                if not await self._gate(frame):
                    await self.push_frame(frame, direction)
            return
        if direction == FrameDirection.UPSTREAM and isinstance(
                frame, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame)):
            self._turn_open = isinstance(frame, UserStartedSpeakingFrame)
        new_turn = isinstance(frame, (InterruptionFrame, EndFrame, CancelFrame))
        if new_turn or isinstance(frame, BotStoppedSpeakingFrame) \
                or (isinstance(frame, PlayoutPauseFrame) and not frame.paused):
            # The turn opened some other way, the reply is over, or the pause ended (a
            # resume the judge did not ask for: its cap): what is held goes on as it
            # came, to be counted as it always was. A verdict to carry on outlives the
            # resume it asked for and the reply's end -- its final may still be on its
            # way, and its segment's close ends it -- but not a new turn.
            async with self._lock:
                await self._release("the pause is over")
                if new_turn:
                    self._continued = None
        await self.push_frame(frame, direction)

    async def _gate(self, frame) -> bool:
        """Hold, drop or rewrite one of the STT's frames; False lets it on unchanged."""
        if self._held:
            self._held.append(frame)
            if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)) \
                    and frame.text.strip():
                if self._strategy.stops(frame.text) \
                        or not self._strategy.paused_for_judging():
                    # "Stop." while it decides, or the reply would be over by now: no
                    # verdict to wait for, the words take the turn as they would have.
                    await self._release("a stop word" if self._strategy.stops(frame.text)
                                        else "the reply would be over")
                else:
                    self._said = _held_words(self._held)
            return True
        if not isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if isinstance(frame, (SegmentDoneFrame, SegmentResetFrame)):
                self._continued = None
            return False
        text = frame.text or ""
        if not text.strip():
            return False
        if self._continued is not None and _key(text) == self._continued:
            # The words the judge let the reply carry on over, again: an interim is
            # dropped; their final is the close of a segment with no turn in it.
            if isinstance(frame, TranscriptionFrame):
                self._continued = None
                await self.push_frame(await self._carried_over(frame))
            return True
        self._continued = None
        if self._turn_open or not self._strategy.judges(text):
            return False
        self._held = [frame]
        self._said = text
        if self._hold_resume is not None:
            self._hold_resume(True)
        self._ask(text)
        return True

    def _ask(self, said: str):
        self._task = self.create_task(self._decide(said), name="barge-judge")

    async def _decide(self, said: str):
        t0 = time.monotonic()
        try:
            verdict = await asyncio.wait_for(self._judge(said), self._wait_secs)
            why = ""
        except asyncio.TimeoutError:
            verdict, why = None, f" (none within {self._wait_secs:.1f}s)"
        except Exception as e:  # noqa: BLE001 -- a judge failure is no verdict
            verdict, why = None, f" ({type(e).__name__}: {e})"
        self.verdicts[verdict] += 1
        logger.info(f"BARGE-JUDGE: {(verdict or 'no verdict').upper()}{why} on {said!r} "
                    f"in {time.monotonic() - t0:.2f}s")
        async with self._lock:
            if self._task is asyncio.current_task():
                self._task = None
            if not self._held:
                return  # released meanwhile: the pause ended
            if verdict == CONTINUE and _key(self._said) != _key(said):
                # They said more while it was deciding: ask on all of it.
                self._ask(self._said)
                return
            if verdict == CONTINUE:
                await self._carry_on(said)
            else:
                await self._release(f"verdict {verdict or 'none'}")

    async def _carry_on(self, said: str):
        """CONTINUE: the held words are dropped, and the reply resumes as for a cough."""
        held, self._held = self._held, []
        # The open segment's words, if the held ones end in one: its final is still to
        # come, and is that segment's close when it does.
        self._continued = _key(_open_words(held)) or None
        for f in held:
            if isinstance(f, TranscriptionFrame):
                if not f.text.strip():
                    continue
                f = await self._carried_over(f)
            elif isinstance(f, InterimTranscriptionFrame):
                continue
            await self.push_frame(f)
        if self._hold_resume is not None:
            self._hold_resume(False)

    async def _carried_over(self, final) -> SegmentDoneFrame:
        """A final the reply carries on over: its segment's close, with no words for the
        turn strategies, and its words on record as judged (taken_turn.TakenWords)."""
        if self.taken_words is not None:
            await self.taken_words.took(final.text, "judged a backchannel")
        return _close_of(final)

    async def _release(self, why: str):
        """The held frames go on as they came: the turn starts on them as it would have."""
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if not self._held:
            return
        held, self._held = self._held, []
        logger.debug(f"BARGE-JUDGE: letting {len(held)} frame(s) on ({why})")
        for f in held:
            await self.push_frame(f)
        if self._hold_resume is not None:
            self._hold_resume(False)

    async def cleanup(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if any(self.verdicts.values()):
            logger.info("BARGE-JUDGE summary: " + ", ".join(
                f"{(k or 'none').upper()} {v}" for k, v in self.verdicts.items()))
        await super().cleanup()
