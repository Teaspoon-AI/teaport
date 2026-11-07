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
#     before -- or a single stop word, paused or not, PauseAwareMinWordsStrategy), the
#     interruption cancels the reply and the caller has the turn.
#   * No words: a cough, a breath, a backchannel too short to count. Playout RESUMES
#     from the very sample it stopped on -- the audio waited in the transport's own
#     queue -- so nothing is lost or repeated. It resumes a short grace after the STT
#     says the speech had no turn in it: where the engine reports how far it has
#     transcribed -- the session asks for the report with the judge on, or slices --
#     once that report has passed the speech (HEARD OUT, below); else once its close
#     came back so (a final under the word guard, or a wordless close).
#     Failing that, it resumes TEAPORT_BARGE_PAUSE_RESUME_S after the line went quiet,
#     or, while the report is awaited, the longer of that and _HEARD_WAIT_S. With the
#     VAD's own stop delay, a cough stalls the bot about ENDPOINT_STOP_SECS + 0.9 s (the
#     report's median, plus the grace). A blip under 250 ms resumes after 0.3 s where
#     there is no report to wait for.
#     TEAPORT_BARGE_PAUSE_MAX_S caps a pause from its start; a barge-in the STT still
#     holds there (stt.py TURN TEXT) closes on the words in hand first, and the reply
#     resumes _FINAL_GRACE_S after that close, so its words are decided paused.
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
# HEARD OUT (#275). The quiet window alone cannot tell a cough from a word the engine has
# not returned yet. Over the bot a short word often trips only the onset test -- the
# VAD's volume gate never opens on it, so no VAD stop commits it -- and its first interim
# then comes when the decoder gets to it: 20 ms AFTER the 0.8 s window resumed the reply,
# on the box (2026-11-02, a "Right." over a story). A word that lands on a reply playing
# again is no longer over a paused reply: the judge never sees it, the word count cuts
# the reply, and the bot starts afresh. So where the engine reports its progress
# (stt.py SPEECH_DECODED), the pauser asks the STT, as the line goes quiet, to say when
# the report has passed the caller's speech (stt.watch_heard, the onset test's own speech
# end): every word of it is then in the interims ahead of that notice
# (endpointing.SpeechHeardFrame), which comes down behind them, through the judge's
# gate, to the start strategy. "Passed" is a sliced turn's own rule for its final (stt.py
# THE CUT'S MARGIN): both the start and the end bound past the voice end plus 200 ms,
# since a word is returned once complete and its stamps can trail its voice. Words that
# come first are judged as any over the pause are; with none, the reply resumes
# _FINAL_GRACE_S after the notice. A close of the speech no longer resumes it while the
# notice is awaited (a close may be of an earlier segment than the latest speech), and
# a hold's release (the judge's, the STT's) never resumes on the grace: what it decided
# is still on its way down. The quiet window is then only the bound on a report that
# does not come: the longer of it and _HEARD_WAIT_S. On the box the same bounds passed a
# turn's commit a median 0.73 s after it, at most 1.98 s (1,494 turns), so a cough waits
# a little longer than the window made it wait. A blip waits too -- on Talk's 0.2 s
# stop a short "yeah" is one. The session asks for the report with the judge on
# (agent_session; before audit A2 only the speculative reply asked, and a session with
# the judge and segment turn text resumed on the window) -- without the judge the wait
# buys nothing (a late word cuts the reply paused or not) and only holds a cough
# longer. An engine without the report, or a session that has none yet, resumes on the
# window as before. formal/UserTurn.tla's NoUnjudgedWords checks it
# (JUDGE = "quietWindow" is the window alone).
#
# Four things it does NOT pause, each because pausing there cost more than it saved:
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
#   * A line that keeps pausing on sound that is not speech: after two pauses that
#     ended with no words in them (_MAX_NOWORD_PAUSES) within _NOWORD_WINDOW_S -- resumed,
#     or ended by the cap with nothing holding them (a sound that outlasts it) -- nothing
#     pauses, the onset test nor the VAD's start, until words come (over the pause or
#     over the playing reply), the reply ends, or _NOWORD_WINDOW_S pass with no sound to
#     skip. Meanwhile words over the reply are not judged either: they barge in on the
#     word count, as without the pause. A ringtone near the box, music, a TV: each read
#     as a speech onset, and a reply was paused over and over, the user hearing almost
#     nothing of it (on 2026-11-06 the incoming-call question, now never paused: below).
#     Echo residue that trips the onset test the same. A backchannel (words, judged or under
#     the word guard) re-arms it -- with a word guard over one word, so can the bot's own
#     echo transcribed as one word the echo guard let through; a pause the cap ended
#     while the judge or the STT held it does not count.
#   * The box's own urgent line: the incoming-call question (call_prompt.py), said
#     unpausable (AgentSession.own_line, hold_off) -- the ringtone it is about must not
#     stop it. The user is still heard, and words over it still barge in.
#
# The echo guard is the onset test itself: 128 ms of Silero confidence >= 0.6, which
# typically fires within ~100-250 ms of the onset of real speech -- well ahead of the
# VAD's edge -- while a short click or a burst of echo residue does not last long
# enough. How well it rejects echo depends on the line: behind the SIP gateway's echo
# canceller, the XVF3800's in the room, or a browser's, the caller's audio is near
# silent while they are quiet, and a line with real echo residue, a poor handset or
# background speech is where it would trip. The limit on pauses with no words below
# bounds that; raise TEAPORT_ONSET_MIN_MS / TEAPORT_ONSET_CONFIDENCE if the bot stutters on its
# own echo.
#
# WHERE THE PAUSE HAPPENS. In the output transport (playout_pause.PausablePlayout, on
# SIP and on /talk alike), which simply stops writing until resumed: everything after
# the pause point stays in pipecat's MediaSender queue, in order. Both transports send
# at real time, one frame ahead of playout at most, so what is downstream of the brain
# is only what was just written: about one 20 ms frame in the SIP gateway's playout
# queue (audible within ~40 ms; protocol v0 has no pause or flush, and needs none), and
# on /talk up to two 40 ms blocks plus the client's own jitter margin -- the room bridge's
# 120 ms card lead, a browser's playout buffer. No client is told: it plays what it
# has, then what it plays when no audio comes (the bridge pads with silence), and goes
# on when the audio does. A send clock with a longer lead would put that much more
# audio past the brain's reach, to play through every pause; that is why they keep
# none beyond the one frame (playout_pause.py).
#
# BOOKKEEPING. The transport announces each pause and resume (PlayoutPauseFrame, both
# directions): the TranscriptLedger freezes its playout layout through it and moves
# the word times of the replies it delays (heard accounting at a later cut stays
# exact), the TTS freezes its playout model (as for a reply hold), and the start
# strategy learns the bot is silent.
#
# Known limits:
#   * A pause is ~150-250 ms behind the caller's onset, plus the gateway's frame or
#     the client's jitter margin, and the network; what played before it is heard.
#   * That margin is measured for the room bridge (its 120 ms card lead) and for SIP,
#     not for the OpenClaw Talk view: a browser that buffers more plays on that much
#     longer after the pause, and the ledger's heard cut (lag_secs) and the
#     captions are short of what was heard by the same amount -- the same assumption
#     the ledger already made on Talk for every cut, pause or not.
#   * A client that reads a quiet speaker as the reply's end must be told of a pause:
#     the room bridge asks to be (?features=pause, talk_transport's pause_notice), so its
#     goodbye "heard out" and its idle timer wait one out; its mouth follows the audio
#     stream, not the card. The reply's words wait out a pause on the transport's voice
#     clock (playout_pause.py). Another client that ends on silence would need the same.
#   * A caller who talks without pause for longer than the cap hears the bot resume
#     over them until their words arrive.
#
import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    SystemFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy

from teaport_brain.conversation_log import quoted
from teaport_brain.endpointing import (
    NO_STOP,
    STT_FRAMES,
    SegmentDoneFrame,
    SegmentResetFrame,
    SpeechHeardFrame,
)
from teaport_brain.settings import setting
from teaport_brain.taken_turn import is_taken
from teaport_brain.tts_text import has_speech
from teaport_brain.word_tokens import word_tokens

# On for every transport that can pause (playout_pause.PausablePlayout: SIP and /talk)
# -- on /talk only with a switch of its own as well (talk_pauses), off by default. The
# room mic has its own, off by default too: there the echo and the face are the box's own
# to get right (#255; Known limits).
ENABLED = setting("TEAPORT_BARGE_PAUSE")
TALK_ENABLED = setting("TEAPORT_BARGE_PAUSE_TALK")
ROOM_ENABLED = setting("TEAPORT_BARGE_PAUSE_ROOM")


def talk_pauses(room: bool) -> bool:
    """Whether a /talk session's playout pauses on a barge-in: the room mic's (the
    local audio bridge, ?features=local) by ROOM_ENABLED, any other client's by
    TALK_ENABLED."""
    return ENABLED and (ROOM_ENABLED if room else TALK_ENABLED)
# The LONGEST a pause with no words waits after the caller's speech (both the VAD and
# the onset test) before it resumes; it resumes sooner once the STT's close of that
# speech is back with no turn in it. The caller's words come with the VAD stop's commit
# at the latest (the STT commits at once while the bot is speaking), ~0.1-0.5 s after it.
RESUME_SECS = min(8.0, max(0.0, setting("TEAPORT_BARGE_PAUSE_RESUME_S")))
# The longest a pause lasts, from its start: at most MAX_CAP_S, below the transport's
# own end to a pause (PAUSE_GUARD_S).
MAX_SECS = setting("TEAPORT_BARGE_PAUSE_MAX_S")
# What ends a reply on its own, paused or still playing, whatever
# TEAPORT_INTERRUPT_MIN_WORDS says: a lone "Stop." is the most natural thing to say, and
# the short unvoiced word the pause is likeliest to miss (#216). An utterance counts only
# if EVERY word of it is one of these (so "don't stop" is not a stop), entries may be
# phrases ("hold on"), and any "shh" ("shhh", "sshh") is "shh". "No" is deliberately not
# one: over the bot it is as often an answer to what is being said as an objection to
# it. The bot's own "wait" coming back as echo is the room echo guard's to take
# (echo_guard._taken), and the line's echo canceller's elsewhere; a cough the STT hears
# as "stop" costs one cut reply, as a 2-word garble always has.
STOP_WORDS = tuple(
    w.strip().lower() for w in setting("TEAPORT_STOP_WORDS").split(",")
    if w.strip())
# The longest the reply's wait lasts after a commit (WORDS NOBODY SPOKE, THE WAIT). The
# engine's own close and the backstops (stt.py: 1.5 s stranded, the slice final's 6 s)
# bring a tail well inside it; under the silent-turn watchdog's default 12 s
# (TEAPORT_SILENT_TURN_SECS, a diagnostic), which it does not read.
REPLY_WAIT_S = 10.0

# No pause when the reply is fully synthesized and at most this much of it is left.
_NEAR_END_S = 0.5
# Speech shorter than this is a blip: its pause resumes after _BLIP_RESUME_S where the
# STT will not report (HEARD OUT).
_BLIP_S = 0.25
_BLIP_RESUME_S = 0.3
# After the STT's close of the speech came back with no turn, resume this much later.
_FINAL_GRACE_S = 0.15
# The longest a quiet line waits for the STT to say it has heard the speech out (HEARD
# OUT), past which the engine is further behind than any measured on the box and the
# reply resumes as on the window alone. The same bounds a sliced turn's final waits on
# passed its commit a median 0.73 s after it, p99 1.44 s, at most 1.98 s (1,494 turns,
# 2026-10-31..11-03).
_HEARD_WAIT_S = 2.5
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
# Pauses that resumed with no words in them, within _NOWORD_WINDOW_S, before nothing
# pauses until words come or the reply ends (the module header).
_MAX_NOWORD_PAUSES = 2
_NOWORD_WINDOW_S = 10.0

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
    # How long after this frame the audio really stops: what was already sent still
    # plays out -- the frame playing and the one queued behind it, up to two 20 ms
    # frames on SIP and two 40 ms blocks on /talk (playout_pause.PausablePlayout._wrote).
    # The ledger credits playout up to then.
    lag_secs: float = 0.0
    # On a pause: the audio it held back, when the reply is fully synthesized (so this is
    # all that is left to play), else -1. The start strategy treats the bot as silent
    # once this much time has passed since the pause began: it would have finished by
    # then without the pause. A reply whose synthesis completes DURING the pause gets
    # the pause announced again with its tail (playout_pause.PausablePlayout.process_frame).
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
        # Both under the transport's guard (PAUSE_GUARD_S): the pauser, not the guard,
        # ends a long pause -- its cap, and its resume window too.
        self._resume_secs = min(MAX_CAP_S, max(0.0, resume_secs))
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
        # Called (async) with a watch's number as the line goes quiet: the STT's watch
        # for its report passing the speech (stt.watch_heard, set by
        # build_agent_session), True if a notice will come (HEARD OUT). The latest
        # watch, and whether its notice came: the speech is heard out.
        self.watch_heard = None
        self._watch_seq = 0
        self._watch = None
        self._heard = False
        # The last speech over the pause was a blip (_BLIP_S): it resumes on a shorter
        # window.
        self._blip = False
        # Words came over this pause (judged, or a final under the word guard).
        self._worded = False
        # When the pauses that resumed with no words in them did, the latest
        # _NOWORD_WINDOW_S of them; and whether that tripped the limit: nothing pauses
        # until words come or the reply ends.
        self._noword_at: deque = deque()
        self._noisy = False
        # When the limit last skipped a sound (or tripped): it lifts _NOWORD_WINDOW_S on.
        self._noisy_at = 0.0
        # The cap is ending a pause someone held (the judge, the STT): no word-less end.
        self._cap_held = False
        # Held off (hold_off): the box's own urgent line plays -- nothing pauses it.
        self._held_off = 0
        # Tallies, for the log and the tests.
        self.pauses = 0
        self.resumes = 0
        self.cancels = 0
        self.skipped_near_end = 0
        self.skipped_noisy = 0
        self.skipped_held = 0

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def _speaking(self) -> bool:
        return any(self._sources.values())

    async def on_onset(self, started: bool):
        """speech_onset.SpeechOnsetMixin listener (the user aggregator's task)."""
        await self._speech("onset", started)

    async def on_final_without_turn(self, words: bool = False):
        """PauseAwareMinWordsStrategy: the STT closed the caller's speech and it started
        no turn (`words`: a final under the word guard; else a close with no words). If
        the line is quiet, resume after a short grace rather than the full window --
        unless the STT is still to hear out speech since (HEARD OUT): a close may be of an
        earlier segment, and the notice says it for all of it."""
        if words:
            self._words_came()
        if self._watch is not None and not self._heard:
            return
        if self._paused and not self._speaking:
            self._schedule_resume(_FINAL_GRACE_S, "the STT's close of it had no turn in it")

    async def on_speech_heard(self, watch: int):
        """PauseAwareMinWordsStrategy: the STT's notice for `watch` (SpeechHeardFrame),
        behind every word of the speech it was asked on, none of which started a turn.
        The latest watch's resumes a quiet line after a short grace (HEARD OUT)."""
        if watch != self._watch:
            return  # an earlier speech's: the caller has spoken since
        self._heard = True
        if self._paused and not self._speaking:
            self._schedule_resume(_FINAL_GRACE_S, "the STT heard it out with no turn in it")

    def _quiet_resume(self, why: str, released: bool = False):
        """The line is quiet over the pause with nothing holding it: resume after a
        grace if the STT has heard the speech out; while its notice is awaited, at the
        latest _HEARD_WAIT_S on (HEARD OUT); with no notice to come, on the window (a
        blip's, _BLIP_RESUME_S, for speech too short to be more than echo). A hold that
        lets go (`released`) never resumes on the grace: what it decided -- the judge's
        words, the STT's final -- is only now on its way down to the start strategy,
        and its own arrival resumes sooner if it starts no turn."""
        if self._heard and not released:
            self._schedule_resume(_FINAL_GRACE_S, f"{why}, the speech heard out")
        elif self._watch is not None and not self._heard:
            wait = max(self._resume_secs, _HEARD_WAIT_S)
            self._schedule_resume(wait, f"{why}, no report from the STT within {wait:.1f}s")
        else:
            wait = min(self._resume_secs, _BLIP_RESUME_S) if self._blip else self._resume_secs
            self._schedule_resume(wait, f"{why}, no words within {wait:.1f}s")

    @property
    def _held(self) -> bool:
        return bool(self._holds)

    def hold_resume(self, on: bool, who: str = "the judge", words: bool = False):
        """Words over the pause are being decided (`on`), or no longer are, by `who`: the
        judge (BargeInJudgeGate), or the STT, whose barge-in turn waits on its final
        (stt.py TURN TEXT). Each holds and lets go its own. While any holds, nothing but
        the cap resumes; after, a quiet line resumes as on going quiet (_quiet_resume) if
        nothing sooner does (the close that follows). `words`: the hold is on words said
        over the pause (the judge's): the line has speech, not only noise."""
        if on:
            self._holds.add(who)
            self._cancel(self._resume_task)
            self._resume_task = None
            if words:
                self._words_came()          # words over the pause, being judged
            return
        self._holds.discard(who)
        # Words were decided: whatever the speech was, it was not a blip.
        self._blip = False
        if (not self._holds and self._paused and not self._speaking
                and self._resume_task is None):
            self._quiet_resume(f"{who} let the reply carry on", released=True)

    async def _speech(self, source: str, started: bool):
        was_speaking = self._speaking
        self._sources[source] = started
        if started:
            if not was_speaking:
                self._speech_t0 = time.monotonic()
            self._cancel(self._resume_task)
            self._resume_task = None
            # More speech: what was heard out is not all of it.
            self._watch = None
            self._heard = False
            if self._may_pause(source):
                await self._pause(source)
        elif self._noisy and not self._speaking:
            # A sound skipped while the limit holds has ended: still sound to skip, so the
            # window counts from its end, not its start (one that outlasts the window).
            self._noisy_at = time.monotonic()
        elif self._paused and not self._speaking:
            # A blip resumes on a shorter window -- but waits for the STT like any speech
            # where it will report: a short word is short too.
            self._blip = (self._speech_t0 is not None
                          and time.monotonic() - self._speech_t0 < _BLIP_S)
            if self.watch_heard is not None:
                # Numbered before the ask: a notice the STT can push at once must match.
                self._watch_seq += 1
                self._watch = self._watch_seq
                try:
                    if not await self.watch_heard(self._watch):
                        self._watch = None
                except Exception as e:  # noqa: BLE001 -- the window still resumes
                    logger.warning(f"BARGE-PAUSE: asking the STT to report failed ({e!r})")
                    self._watch = None
            if self._paused and not self._speaking and self._resume_task is None:
                self._quiet_resume("a blip" if self._blip else "the caller stopped")

    def _words_came(self):
        """Words over a pause: the line has speech, not only noise -- the limit on
        word-less pauses starts over."""
        self._worded = True
        self._noword_at.clear()
        if self._noisy:
            self._noisy = False
            logger.info("BARGE-PAUSE: words came -- pausing on the caller's speech again")

    def _note_noword(self):
        """A pause resumed with no words in it: count it, and past the limit within the
        window, pause no more until words come or the reply ends."""
        now = time.monotonic()
        self._noword_at.append(now)
        while self._noword_at and now - self._noword_at[0] > _NOWORD_WINDOW_S:
            self._noword_at.popleft()
        if len(self._noword_at) >= self._max_noword and not self._noisy:
            self._noisy, self._noisy_at = True, now
            logger.info(f"BARGE-PAUSE: {len(self._noword_at)} pauses with no words in "
                        f"{_NOWORD_WINDOW_S:.0f}s -- not pausing again until words come or "
                        "the reply ends (sound that is not speech: a ringtone, music, echo)")

    def hold_off(self, on: bool):
        """The box's own urgent line is being said (AgentSession.own_line, unpausable:
        the incoming-call question) -- nothing pauses it, whatever is heard over it (a
        ringtone read as speech paused it over and over, 2026-11-06). Counted, so lines
        that overlap each lift only their own."""
        if not on and not self._held_off:
            logger.warning("BARGE-PAUSE: hold-off lifted with none held")
            return
        self._held_off += 1 if on else -1
        if on and self._held_off == 1:
            logger.info("BARGE-PAUSE: not pausing (the box is saying a line nothing may pause)")
        elif not on and not self._held_off:
            logger.info("BARGE-PAUSE: pausing again (done saying a line nothing may pause)")

    def _may_pause(self, source: str) -> bool:
        if self._paused or not self._bot_speaking or not self._reply_audio:
            return False
        if self._held_off:
            self.skipped_held += 1
            return False
        if self._noisy:
            now = time.monotonic()
            if now - self._noisy_at > _NOWORD_WINDOW_S:
                # Nothing to skip for the window: the sound has stopped.
                self._noisy = False
                logger.info(f"BARGE-PAUSE: no sound to skip for {_NOWORD_WINDOW_S:.0f}s -- "
                            "pausing on the caller's speech again")
            else:
                # The line keeps pausing on what is not speech (the module header).
                self._noisy_at = now
                self.skipped_noisy += 1
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
            self._noword_at.clear()
            self._noisy = False
            self._reply_audio = False
        elif isinstance(frame, TTSAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            self._reply_audio = getattr(frame, "context_id", None) is not None
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            await self._speech("vad", True)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._speech("vad", False)
        elif (isinstance(frame, PlayoutPauseFrame) and not frame.paused and self._paused
              and not getattr(self._output, "playout_paused", True)):
            # The transport resumed on its own (its write guard): the pause is over here
            # too, or this pauser would refuse the next one and count the bot paused.
            # Only if the transport is playing now: a notice of this pauser's own resume
            # that a newer pause overtook must not end that newer pause.
            logger.info("BARGE-PAUSE: the transport resumed playout on its own "
                        "(its write guard)")
            self._end()
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
        # Words over this pause, from now on: none reported between pauses counts here.
        self._worded = self._cap_held = False
        self._pause_t0 = asyncio.get_running_loop().time()
        self.pauses += 1
        logger.info(f"BARGE-PAUSE: caller speech "
                    f"{'already under way as the bot starts' if already else 'while the bot talks'}"
                    f" ({'speech onset' if source == 'onset' else 'VAD'}) -- playout paused")
        await self._output.set_playout_paused(True)
        self._cap_task = self.create_task(self._cap(), name="barge-pause-cap")

    async def _resume(self, why: str, counted: bool = True):
        """`counted`: a resume with no words in the pause counts against the limit on
        word-less pauses -- not one the cap ended while someone held it."""
        if not self._paused:
            return
        held = asyncio.get_running_loop().time() - self._pause_t0
        worded, cap_held = self._worded, self._cap_held
        self._end()
        self.resumes += 1
        if counted and not worded and not cap_held:
            self._note_noword()
        logger.info(f"BARGE-PAUSE: resumed after {held:.2f}s ({why}); pauses with no "
                    f"words in the last {_NOWORD_WINDOW_S:.0f}s: {len(self._noword_at)}")
        await self._output.set_playout_paused(False)

    def _end(self):
        self._paused = False
        self._holds.clear()
        self._watch = None
        self._heard = False
        self._blip = False
        self._worded = self._cap_held = False
        for task in (self._resume_task, self._cap_task):
            self._cancel(task)
        self._resume_task = self._cap_task = None

    def _schedule_resume(self, delay: float, why: str):
        if self._held:
            return
        self._cancel(self._resume_task)
        self._resume_task = self.create_task(self._resume_after(delay, why),
                                             name="barge-resume")

    async def _resume_after(self, delay: float, why: str):
        await asyncio.sleep(delay)
        self._resume_task = None
        await self._resume(why)

    async def _cap(self):
        await asyncio.sleep(self._max_secs)
        self._cap_task = None
        why = f"pause cap {self._max_secs:.1f}s"
        # Ended while the judge or the STT held it: words being decided, no word-less
        # end -- by this resume, or by the close's own grace racing it. With nothing
        # holding it, the sound outlasted the cap (a ringtone, music): it counts.
        self._cap_held = bool(self._holds)
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
        if self.pauses or self.skipped_held:
            logger.info(f"BARGE-PAUSE summary: {self.pauses} pause(s): {self.cancels} "
                        f"cancelled by words, {self.resumes} resumed with none; skipped "
                        f"{self.skipped_near_end} near a reply's end, "
                        f"{self.skipped_noisy} past the limit on pauses with no words, "
                        f"{self.skipped_held} over a line nothing may pause")
        await super().cleanup()


_SHH = re.compile(r"s*h{2,}")


def fold_shh(word: str) -> str:
    """Every "shhh" ("sshh", "shhhh") as "shh": the one fold the stop words and the echo
    guard's word match share."""
    return "shh" if _SHH.fullmatch(word) else word


def _tokens(text: str) -> list:
    """The words of `text` (word_tokens, the rule sliced turn texts share), every "shhh"
    as "shh"."""
    return [fold_shh(w) for w in word_tokens(text)]


def _stop_phrases(stop_words) -> list:
    """The stop entries as word tuples, longest first ("hold on" before "hold")."""
    return sorted({tuple(_tokens(e)) for e in stop_words if _tokens(e)}, key=len,
                  reverse=True)


def has_stop_word(text: str, stop_words=STOP_WORDS) -> bool:
    """True when ANY word of `text` belongs to a stop entry ("Okay, wait.", "No, stop.",
    "Hold on, what?") -- the test words in a reply's wait must pass to be a tail (WORDS
    NOBODY SPOKE, A STOP WORD). Over a playing bot the test is is_stop_utterance's."""
    toks = _tokens(text)
    return any(tuple(toks[i:i + len(ph)]) == ph
               for ph in _stop_phrases(stop_words) for i in range(len(toks)))


def is_stop_utterance(text: str, stop_words=STOP_WORDS) -> bool:
    """True when EVERY word of `text` belongs to a stop entry ("Stop.", "Wait, wait!",
    "Hold on.", "Shhh"), so "don't stop" or "stop by the shop" are not stops."""
    toks = _tokens(text)
    if not toks:
        return False
    phrases = _stop_phrases(stop_words)
    i = 0
    while i < len(toks):
        for ph in phrases:
            if tuple(toks[i:i + len(ph)]) == ph:
                i += len(ph)
                break
        else:
            return False
    return True


# WORDS NOBODY SPOKE (#22). After a turn is committed the STT can return words with no
# speech under them: the engine closing a segment of its own on the line's silence or
# noise, a tail it decoded twice, the backstop's commit of a segment no VAD stop
# closed. Logged live as zero-length lines ('Okay.' [23.3-23.3], 'Fake.' [11.0-11.0],
# the "fig" of "fiddle fig") -- and each was a new user turn: the bot not speaking yet,
# one word is a turn, and the turn start's interruption cut the reply in flight before a
# word of it played (LEDGER "CUT heard~0%"). The model was then asked again on the
# question plus the fragment, a second later: as one message on SIP (the reply hold's
# TurnMerge: the reply had not played), as two on Talk and the room.
#
# Such words are a TAIL: they start no turn and interrupt nothing, and a tail's final
# goes into the committed turn's own message (on_tail_words; agent_session:
# heard_context.append_to_last_turn), in place, the reply asked on it kept. That is what
# a sliced turn does with a word of a committed turn that arrives after the commit (stt.py
# TURN TEXT: "it is that turn's final's"; turn_text.py "corrected": the record rewritten,
# no restart). Not the reply hold's merge, which drops the unplayed reply and asks again:
# that is for words the caller went on to say, and nothing says these were. The cost, when
# they were said (a fragment's last word that no speech test caught): the reply answers
# the turn as it was asked, without them.
#
# A tail is a transcript that arrives
#
#   * with no user turn open (an open turn's words are its own, as ever),
#   * in the reply's WAIT: after a turn that committed words, before the bot is next
#     audible -- the only time one word could cut it at 0 % -- and while that reply is
#     still to come (THE WAIT, below),
#   * with no speech heard for it: no VAD start (nor, where the reply hold is, an onset:
#     THE ONSET, below) whose words have not been closed yet, and
#   * with no stop word in it (A STOP WORD, below).
#
# "Closed": a close stamped with a VAD stop after that speech, carrying words -- the
# final itself, or the wordless close of a stop whose words the engine's own close had
# already returned (the commonest order on the box). A wordless close alone answers
# nothing: in slice mode an empty provisional closes the stop and its words follow
# (stt.py _settle_turn), and they are a turn. An unstamped final (another STT, or words
# handed back) closes the speech heard before the latest VAD stop -- not speech the VAD
# still hears: a turn committed on the caller's first words while they talk on (the
# strategy concluding before their stop) has the rest of them still to come, and those
# start a turn of their own after its re-applied finalize.
#
# THE WAIT ends, and words are counted as ever from then on:
#
#   * when the bot is audible (BotStartedSpeakingFrame), a filler or an error line ("let
#     me check") included -- after one, a word can cut the real reply still to come, as
#     on main. The barge-in rules stand over it -- two words, or a stop word -- for every
#     word, a segment begun in the wait included: "Okay" in the wait and then "Okay, no,
#     that is not what I asked." over the reply is a barge-in, and words over the bot
#     often come with no onset (#215: 3 of 13 at 8 kHz). It stays over once the bot
#     stops again: the reply has played;
#   * when a turn starts;
#   * when the reply ends with nothing of it to play (UnspokenReplyTap, directly above
#     the TTS: an empty answer, one with no text the TTS would speak, a tool round that
#     asks for none). Nothing is in flight to cut, and words joined to the turn would go
#     unanswered;
#   * REPLY_WAIT_S (10 s) after the commit: a reply that never comes, one that ends
#     unspoken in a way the tap does not see (a synthesis that fails), and a consult
#     with nothing said for that long -- else a caller the VAD missed, minutes on, would
#     be folded into a message already answered. The engine's own close and the
#     backstops (stt.py: 1.5 s stranded, the slice final's 6 s) bring a tail well
#     inside it.
#
# A STOP WORD in it, anywhere, and words are never a tail: "Stop.", "Okay, wait.", "No,
# wait.", "Wait, no." said after the commit, before the bot is audible, start the turn
# and cut the reply in flight, as TEAPORT_STOP_WORDS promise ("end a reply on their
# own"; turn_text.py's trailing "stop"), and as main cut all of them ('Okay, wait.'
# [35.3-35.3] is one of #22's own zero-length lines). A stop word is the word likeliest
# to come with no speech heard: #216 measured real ones at 8 kHz below the onset test's
# threshold, so neither the VAD's start nor the onset fires on them. Any word, not every
# word (has_stop_word, not is_stop_utterance): the all-words rule guards a playing reply
# from being cut on "don't stop"; here the cost runs the other way -- words wrongly
# kept from the turn are the caller's "wait" lost and the reply going ahead, tool calls
# and all, where words wrongly not taken as a tail only start the turn main started.
#
# THE ONSET (onset_is_speech). Where the reply hold is (SIP; Talk only with
# TEAPORT_REPLY_HOLD_TALK), speech the onset test heard and the VAD's start did not (a
# short "Fig." after a pause) is speech heard, and its words are a turn: the hold holds
# the reply for the onset, and that turn's start drops it unplayed and merges the words
# into the committed turn (TurnMerge), asking once more on the whole -- the onset and
# the VAD start are one event to it, and to formal/UserTurn.tla. Without the hold (Talk
# and the room as shipped) nothing holds or merges: such a turn cut the reply in flight
# and left a second user message, so words the onset alone heard are a tail like any
# other, with the same exceptions (the owner's call, 2026-11-06).
#
# So none of these is a tail: words after a VAD start (a real new utterance), or after
# an onset where the reply hold is; the final of the speech whose VAD stop the turn's
# re-applied finalize ended on (keep_barge_in_reachable); words with a stop word in
# them; words once the wait is over; the session's first words. A tail left with
# nowhere to go (no on_tail_words, or no message of the caller's to take it) is
# dropped. Each is logged at info; in slice mode its final also goes on record
# (TakenWords, which only slice mode keeps: the turn-text corrector reads it).
class PauseAwareMinWordsStrategy(MinWordsUserTurnStartStrategy):
    """The brain's barge-in rule, plus:

      * over the speaking bot, paused or not, an utterance made only of stop words
        starts the turn whatever its length (#216);
      * words nobody was heard saying, after a committed turn and before its reply
        plays, start no turn: they are that turn's (WORDS NOBODY SPOKE, above; a final's
        words to on_tail_words) -- unless a stop word is among them.
        `onset_is_speech`: speech the onset test alone heard counts as heard (where the
        reply hold is);

    and while playout is paused (PlayoutPauseFrame):

      * once the audio the pause held back would have finished playing anyway (the
        pause frame's tail_secs), the bot counts as silent and one word is a turn --
        as it would have been without the pause;
      * a final (or a wordless close) that starts no turn tells the pauser, which then
        resumes sooner (on_final_without_turn), as does the STT's notice that it has
        heard the speech out (SpeechHeardFrame, on_speech_heard).

    Anything else counts words exactly as MinWordsUserTurnStartStrategy does, so a
    backchannel ("yeah") still does not cut the bot, and the pause it caused resumes.
    on_onset is for the VAD analyzer's onset listeners, reply_unspoken for
    UnspokenReplyTap (agent_session)."""

    def __init__(self, *, stop_words=STOP_WORDS, on_final_without_turn=None,
                 on_speech_heard=None, on_tail_words=None, onset_is_speech=True,
                 **kwargs):
        super().__init__(**kwargs)
        self._stop_words = tuple(stop_words)
        self._on_final_without_turn = on_final_without_turn
        self._on_speech_heard = on_speech_heard
        # (words) -> bool: a tail's final words into the committed turn's message, True
        # if placed (WORDS NOBODY SPOKE).
        self._on_tail_words = on_tail_words
        # Whether the onset test alone hearing speech makes its words a turn (WORDS
        # NOBODY SPOKE, THE ONSET): where the reply hold is.
        self._onset_is_speech = onset_is_speech
        # The longest the reply's wait lasts after the commit (THE WAIT).
        self.reply_wait_secs = REPLY_WAIT_S
        # taken_turn.TakenWords, where the session keeps one (agent_session).
        self.taken_words = None
        self._playout_paused = False
        self._pause_t0 = None
        self._would_end_at = None
        # WORDS NOBODY SPOKE. A user turn is open, and a final with words came in it
        # (cleared by a taken one: the closer drops the turn's words). The last turn
        # committed words, and its reply is awaited (THE WAIT: the bot has not been
        # audible since, nor has the reply ended unspoken), since `_due_at`. The bot
        # audible (BotStarted/StoppedSpeaking, kept across turns, unlike MinWords' own
        # flag). VAD stops counted, as the STT stamps them; the count at the latest
        # speech heard (VAD start, or onset) whose words no close has carried yet, None
        # when there is none; and whether a final with words has come since that
        # speech. A tail's segment is open: its first words came in the wait.
        self._turn_open = False
        self._turn_words = False
        self._reply_due = False
        self._due_at = 0.0
        self._audible = False
        self._stops = 0
        self._heard_at = None
        self._worded = False
        self._tail_open = False

    async def on_onset(self, started: bool):
        """speech_onset.SpeechOnsetMixin listener: speech began (or ended) on the onset
        test. Called in the user aggregator's task, between the frames it processes.
        Speech heard only where `onset_is_speech` (THE ONSET); elsewhere the VAD's start
        is what says so."""
        if started and self._onset_is_speech:
            self._heard()

    def reply_unspoken(self, why: str):
        """UnspokenReplyTap: the reply to the last turn ended with nothing of it to play
        (`why`). Its wait is over (THE WAIT): words with no speech heard for them are a
        turn again, and a tail's segment still open ends."""
        if self._reply_due:
            # info: beside the tails, how often a wait ends this way.
            logger.info(f"{self}: the reply to the last turn ended unspoken ({why}) -- "
                        "words heard after it are a turn again (#22)")
        self._reply_due = self._tail_open = False

    def _waiting(self) -> bool:
        """The reply to the last turn is still to come: THE WAIT."""
        return (self._reply_due and not self._audible
                and time.monotonic() - self._due_at < self.reply_wait_secs)

    def _heard(self):
        self._heard_at, self._worded, self._tail_open = self._stops, False, False

    def _closed(self, stop_n):
        """A close with words, stamped `stop_n`: the speech heard before that stop has
        had its words. Unstamped (None), it is taken as closing the stops counted so
        far -- never speech the VAD still hears, whose rest is still to come."""
        if stop_n is None:
            stop_n = self._stops
        if stop_n != NO_STOP and self._heard_at is not None and self._heard_at < stop_n:
            self._heard_at = None

    def _note(self, frame: Frame):
        """The state WORDS NOBODY SPOKE reads, from every frame but a transcript's words
        (_handle_transcription)."""
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._heard()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._stops += 1
        elif isinstance(frame, BotStartedSpeakingFrame):
            # The wait is over, a tail's segment with it: from here every word is
            # counted as one over the bot (THE WAIT).
            self._audible, self._reply_due, self._tail_open = True, False, False
        elif isinstance(frame, (BotStoppedSpeakingFrame, InterruptionFrame)):
            self._audible = False
        elif isinstance(frame, (SegmentDoneFrame, SegmentResetFrame)):
            self._tail_open = False
            if isinstance(frame, SegmentDoneFrame) and self._worded:
                self._closed(frame.stop_n)
        elif is_taken(frame):
            self._turn_words = False

    async def handle_user_turn_started(self):
        await super().handle_user_turn_started()
        self._turn_open, self._turn_words = True, False
        self._reply_due = self._tail_open = False

    async def handle_user_turn_stopped(self):
        await super().handle_user_turn_stopped()
        self._turn_open = False
        self._reply_due, self._due_at = self._turn_words, time.monotonic()

    def _tail(self, frame) -> bool:
        """WORDS NOBODY SPOKE: `frame`'s words are a tail. Words with a stop word among
        them never are (A STOP WORD), nor anything once the wait is over (THE WAIT), a
        segment begun in it included: they are counted as ever."""
        text = (frame.text or "").strip()
        if (not text or self._turn_open or self._heard_at is not None
                or has_stop_word(text, self._stop_words)):
            return False
        if not self._waiting():
            self._tail_open = False
            return False
        return True

    async def _take_tail(self, frame) -> ProcessFrameResult:
        text = frame.text.strip()
        final = isinstance(frame, TranscriptionFrame)
        if not self._tail_open:
            # info: the live measure of how often it happens, beside the cuts it spares.
            logger.info(f"{self}: {quoted(text)} with no speech heard for it, after a "
                        "committed turn and before its reply plays -- that turn's words, "
                        "not a new turn (#22)")
        self._tail_open = not final
        await self.trigger_reset_aggregation()      # a final's words leave the aggregation
        if final:
            placed = self._on_tail_words is not None and self._on_tail_words(text)
            logger.info(f"{self}: the tail {quoted(text)} "
                        + ("joins the committed turn's message" if placed
                           else "is dropped: no message of the caller's to join"))
            if self.taken_words is not None:      # slice mode's record (TakenWords)
                await self.taken_words.took(
                    text, "joined to the committed turn: no speech was heard for them"
                    if placed else "dropped: no speech was heard for them")
        return ProcessFrameResult.CONTINUE

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        self._note(frame)
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
            await self._notify()                       # a close with no words
        elif (isinstance(frame, SpeechHeardFrame) and self._playout_paused
              and self._on_speech_heard is not None):
            await self._on_speech_heard(frame.watch)
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

    async def _notify(self, words: bool = False):
        if self._on_final_without_turn is not None:
            await self._on_final_without_turn(words)

    async def _handle_transcription(self, frame):
        if self._tail(frame):
            return await self._take_tail(frame)
        result = await self._count(frame)
        worded = isinstance(frame, TranscriptionFrame) and (frame.text or "").strip()
        if (result != ProcessFrameResult.STOP and worded
                and self.taken_words is not None):
            # A final that starts no turn: the aggregator drops its words (the guard's
            # reset), and they go on record as dropped (taken_turn.TakenWords).
            await self.taken_words.took(frame.text, "dropped by the barge-in guard")
        if worded:
            # After the count: a turn this final opens is open now, and the speech it
            # carries was heard before it, not after (WORDS NOBODY SPOKE).
            self._worded, self._tail_open = True, False
            self._closed(getattr(frame, "stop_n", None))
            if self._turn_open:
                self._turn_words = True
        return result

    async def _count(self, frame):
        if self._bot_speaking and is_stop_utterance(frame.text, self._stop_words):
            # Paused or not (#216): the pause fails most on short unvoiced words, and
            # "stop" is one, so the word that must always work is the likeliest to land
            # on a reply still playing.
            # info: a cut on the bot's own echoed stop word (SIP and non-room Talk
            # have no echo guard) must show in the journal.
            logger.info(f"{self}: stop word over the bot "
                         f"({'paused' if self._playout_paused else 'playing'}, "
                         f"{quoted(frame.text)}) -- starting the turn")
            await self.trigger_user_turn_started()
            return ProcessFrameResult.STOP
        if not (self._bot_speaking and self._playout_paused):
            result = await super()._handle_transcription(frame)
            if (self._bot_speaking and result != ProcessFrameResult.STOP
                    and isinstance(frame, TranscriptionFrame) and (frame.text or "").strip()):
                # Words over the playing reply that start no turn: the line has speech,
                # and the pauser's limit on word-less pauses starts over.
                await self._notify(words=True)
            return result
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
            await self._notify(words=bool((frame.text or "").strip()))
        return result


class UnspokenReplyTap(FrameProcessor):
    """Tells `on_unspoken` (PauseAwareMinWordsStrategy.reply_unspoken) when a reply ends
    with nothing of it to play: a response that ended with no text the TTS would speak
    (tts_text.has_speech, the TTS's own test) and called no tool -- an empty answer, a
    mood tag alone -- or a tool round whose final result asks for no answer
    (tools.no_inference). The reply's wait is over then (WORDS NOBODY SPOKE, THE WAIT);
    the reply hold's own test for a reply with nothing to check (ReplyHoldGate,
    disarmed). Place it DIRECTLY above the TTS, below everything that edits the reply's
    text (MoodTag, LLMTextGuard): it reads the text the TTS is handed, and the End,
    which nothing below the TTS ever sees for a completion with no text -- the TTS
    holds it (test_pipecat_premises.py, the text-less completion's End). Direct mode:
    no queue between the model's words and the TTS. A tap: every frame goes on as it
    came.

    A tool call is read from the frames the LLM service broadcasts BEFORE the response's
    End: services.FunctionCallsDispatchedFrame (every call, the silent cancel_<tool>
    included) and pipecat's FunctionCallsStartedFrame. Never from
    FunctionCallInProgressFrame, which each call's own task broadcasts and which can
    trail the End (services.py): read there, the flag outlived its response, and the
    answer after the tool -- empty, say -- went unreported."""

    def __init__(self, on_unspoken):
        super().__init__(enable_direct_mode=True)
        # Here, not at the top: services imports this module.
        from teaport_brain.services import FunctionCallsDispatchedFrame
        self._called = (FunctionCallsStartedFrame, FunctionCallsDispatchedFrame)
        self._on_unspoken = on_unspoken
        # Since the response began: text the TTS would speak went by; a tool call was
        # made (its result runs the model again, and that answer is the reply).
        self._said = False
        self._tool_call = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            self._said = self._tool_call = False
        elif direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._said = False
            elif isinstance(frame, (LLMTextFrame, TTSSpeakFrame)):
                self._said = self._said or has_speech(frame.text)
            elif isinstance(frame, self._called):
                self._tool_call = True
            elif isinstance(frame, LLMFullResponseEndFrame):
                if self._tool_call:
                    self._tool_call = False
                elif not self._said:
                    self._on_unspoken("a response with nothing to say")
                self._said = False
            elif isinstance(frame, FunctionCallResultFrame) and _asks_no_answer(frame):
                self._on_unspoken(f"{frame.function_name} asked for no answer")
        await self.push_frame(frame, direction)


def _asks_no_answer(result: FunctionCallResultFrame) -> bool:
    """A tool's final result the model is not run on, as the assistant aggregator reads
    it: an empty result, or run_llm False (its properties' first)."""
    props = result.properties
    if props is not None and not props.is_final:
        return False                    # an update: the call goes on
    if not result.result:
        return True
    run = props.run_llm if props is not None and props.run_llm is not None else result.run_llm
    return run is False


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
        logger.info(f"BARGE-JUDGE: {(verdict or 'no verdict').upper()}{why} on {quoted(said)} "
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
