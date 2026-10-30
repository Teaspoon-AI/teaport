#
# teaport — Pipecat STT service
#
# A Pipecat streaming STT service that uses a teaport engine
# (the engine's realtime STT) as the transcriber, over the engine's
# vLLM /v1/realtime-compatible WebSocket.
#
# Structure mirrors pipecat's GladiaSTTService (the canonical WebsocketSTTService
# subclass that uses raw `websockets`); frame/metric conventions reconciled
# against DeepgramSTTService.
#
# Protocol (verified against the engine websocket implementation):
#   endpoint:  ws://<host>:<port>/v1/realtime   (no auth)
#   we send:   {"type":"session.update","model":"...",
#               "transcription_progress":true}   (handshake; the field opts in to the
#                                                  progress report -- see SPEECH_DECODED)
#              {"type":"input_audio_buffer.append","audio":"<base64 PCM16>"} (mono, 16 kHz)
#              {"type":"input_audio_buffer.commit","final":true,
#               "trailing_silence_ms":N}   (forces a final; N optional -- see STT_SILENCE_HINT)
#   we recv:   {"type":"session.created","id":"sess_...","created":N,
#               "capabilities":["transcription.progress"]}   (capabilities: engines with
#                                                              the report)
#              {"type":"transcription.progress","decoded_through_ms":N}   (opted in)
#              {"type":"transcription.delta","delta":"<piece>","timestamp":1.2}  (append-only)
#              {"type":"transcription.done","text":"<full utterance>","usage":{...},
#               "reason":"commit"|"vad","credited_ms":N}
#                     (reason: engines with the trailing-silence credit, teagram-engine#36;
#                      credited_ms: engines that take the hint, teagram-engine#37)
#              {"type":"error","error":"...","code":"..."}
#
# Key semantics:
#   - Deltas are APPEND-ONLY token pieces (not revisable). We keep a running
#     buffer and emit it as the cumulative InterimTranscriptionFrame each time.
#   - A final (transcription.done) is gated on an explicit commit. We send the
#     commit on Smart Turn's VERDICT: a COMPLETE answer to the VAD stop commits at
#     once, an INCOMPLETE one holds the segment open until the turn is judged
#     complete. See the commit note above _VAD_STOP_COMMITS, and process_frame.
#     The engine resets its text after each done, so done.text is per-utterance
#     and we reset our buffer on it.
#   - streaming_backend=True (services.make_stt, TEAPORT_STT_BACKEND=streaming) speaks
#     the same protocol to vLLM serving Voxtral Realtime, where `final` on a commit
#     means the opposite thing: see the constructor. Experimental; the incumbent
#     engine is the default.
#

import asyncio
import base64
import contextlib
import difflib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

import numpy as np
from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import WebsocketSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from teaport_brain import echo_guard
from teaport_brain.endpointing import (
    NO_STOP,
    SMARTTURN_STOP_SECS,
    SegmentDoneFrame,
    NoSpeechReportFrame,
    SegmentResetFrame,
    SpeechDecodedFrame,
    SpeechPreviewFrame,
    SttProgressFrame,
    TurnTextFinalFrame,
    TurnVerdictFrame,
)
from teaport_brain.settings import setting
from teaport_brain.wake_words import find_wake

# P99 latency from speech end to final transcript (broadcast to downstream turn
# strategies). The engine's realtime STT targets <500 ms delta delay; ~0.8 s end-to-end p99
# is a conservative default — override per deployment.
TEAPORT_TTFS_P99 = 0.8

# How many consecutive word-less finals before the brain says it cannot hear. Low enough
# to catch a dead microphone inside one exchange, high enough that ordinary VAD triggers
# on room noise never reach it.
_EMPTY_FINAL_RUN = 5

# What closes the engine-side segment (forces the final): Smart Turn's VERDICT on the
# VAD stop, not the raw stop.
#
#   verdict   The stop strategy answers every VAD stop with a TurnVerdictFrame
#             (endpointing.py). COMPLETE commits at once (unless the caller's voice
#             is back: see THE CALLER'S VOICE below); INCOMPLETE holds the segment
#             OPEN -- if the caller resumes, their next words land in the same segment,
#             and if they do not, the analyzer's silence ceiling (SMARTTURN_STOP_SECS)
#             sends a second, complete verdict and that commits.
#   vad-stop  Commit on VADUserStoppedSpeakingFrame itself, before the verdict exists.
#             What this service did until issue #43, and now only the session-wide
#             fallback below, for a session in which no verdict ever arrives.
#
# Why the verdict: a commit is irreversible on the engine side -- finish() right-pads
# and REDECODES the buffer, then resets the stream -- so a commit at a mid-sentence
# breath does not just split the turn's text, it hands the rest of the sentence to a
# fresh decoder with no context ("Hey, what's" + "Time is here." / "Time is his.", the
# second half decoded with no idea what came before; measured 2026-09-18 on real caller
# audio with a 350 ms pause at a natural dip, 4 of 4 runs at ENDPOINT_STOP_SECS=0.2,
# one fragment returned EMPTY) and pays the engine's fixed final-commit cost twice.
# Smart Turn called every one of those fragments INCOMPLETE and held the pipecat turn
# open; the segment was already closed by then. ENDPOINT_STOP_SECS sits at 0.5 -- 0.3 s
# above pipecat's recommended 0.2 on every clean turn -- to keep the raw stop from asking
# at a breath. With the commit following the verdict, the floor can come down: a
# COMPLETE verdict at 0.2 s is the same commit 0.3 s sooner, and an INCOMPLETE one costs
# nothing the ceiling was not already costing.
#
# What it trades. A COMPLETE verdict commits when the MODEL has answered, so its
# inference is now on the commit path instead of hidden under the engine's decode:
# ~90 ms for Smart Turn v3 on a desktop CPU (measured 2026-09-18), more on the
# appliance's ARM cores. That is paid back with interest wherever ENDPOINT_STOP_SECS can
# come down (the Talk client), and not at all where it cannot (telephony, where the
# model's COMPLETE is unreliable and the 0.5 floor stays). An INCOMPLETE verdict that
# FALLS THROUGH to the ceiling (the caller really was done) pays the engine's commit
# AFTER the ceiling instead of under it -- ~0.8 s to the final when this was written,
# 0.1-0.2 s since the silence hint (STT_SILENCE_HINT). What is bought: every INCOMPLETE
# verdict the caller proves right (12 of 20 on the SIP calls measured 2026-09-10) keeps
# one utterance and one decode instead of two context-free fragments.
#
# Until 2026-10-09 the raw stop was also an operator setting (TEAPORT_STT_COMMIT_ON),
# for two things the hold took away: that final ~0.8 s sooner on a fallthrough, which
# the silence hint has since made 0.1-0.2 s; and a final under the ceiling for the
# speculative reply (speculate.py) to ask the LLM on, which it now does on the settled
# interim instead -- the interim equals the final (48 of 48 phone turns measured that
# day). With nothing left to trade for, the setting went.
#
# Two things do not wait for a verdict. While the BOT is speaking the VAD stop commits
# as before: the engine returns nothing for words spoken over the bot until the
# segment is flushed (teagram-engine#7), so that flush IS the barge-in -- holding it
# for a verdict would hold "Okay, stop." for up to the ceiling -- and someone talking
# over the bot is not composing a sentence with breaths in it. And a segment held on an
# INCOMPLETE verdict is not held forever: if neither the ceiling's verdict nor a VAD
# start arrives within SMARTTURN_STOP_SECS plus slack of the VAD stop, the hold expires
# and commits with a warning, because a segment that never closes is a turn that is
# never answered (the same failure the stranded backstop below exists for). And if NO
# verdict of any kind has reached the STT by the time its first hold expires, nothing
# upstream is answering VAD stops -- a stop strategy that is not the brain's, a model
# that raises before the push, or simply a first inference slower than the wait (a
# cold ONNX session on a GPU the TTS is using) -- and the service commits at the VAD
# stop from there UNTIL a verdict does arrive, which puts it back on the verdict. A
# wiring that never answers costs no turn more than one wait; one that was merely
# slow costs the turns in between the split-at-the-stop the old design paid on every
# turn, and its first verdict undoes it.
#
# Nor is a hold kept over a segment the engine has closed on its own. Its endpointer
# finalizes on shorter silences than the VAD's floor, so on the appliance the final
# commonly lands BEFORE the VAD stop: what is open at the stop is the tail after the
# engine's cut, and a hold over it keeps nothing whole (the engine has split there
# already) while the commit that eventually closes it hands the engine a second of
# silence to decode under the reply. Such a stop commits at once instead (`tail`).
# (A done that arrives unasked while a segment is held does NOT release the hold: it
# may be the previous utterance's -- see _handle_message.)
#
# _commit_on holds which rule is in force -- "verdict", or "vad-stop" while the
# fallback is -- and the retired setting is only warned about:
if os.getenv("TEAPORT_STT_COMMIT_ON") is not None:
    logger.warning("TEAPORT_STT_COMMIT_ON is no longer read and is ignored: the STT always "
                   "commits on Smart Turn's verdict (a VAD stop over the bot still commits "
                   "at once)")

# The commit reasons that followed a VAD stop -- the raw stop, or one of the verdicts
# that answer it -- as opposed to the backstops. _log_segment's wordless warning is
# gated on these; see it. "tail" is left out on purpose: it closes what is open after
# the engine's own close of the utterance (the VAD stop in process_frame), which on
# the ordinary turn is silence, so an empty final there is the expected result and
# not a miss.
_VAD_STOP_COMMITS = frozenset({"vad-stop", "bot-speaking", "verdict", "ceiling",
                               "turn-stopped", "hold-expired"})

# Slack past the analyzer's ceiling before a held segment is committed anyway. The
# ceiling is clocked on the audio frames the aggregator processes and its verdict is a
# frame that crosses four processors' queues to get here: milliseconds on an idle box,
# but the aggregator's queue backs up behind the model and the TTS under STT-vs-TTS
# contention, and the expiry is wall-clock from the VAD stop. So the slack is what
# keeps a LATE verdict from being taken for a lost one -- an expiry that beats it
# commits correctly and blames the wiring in the journal. 1.0 s is sized to that as a
# guess, not a measurement: _handle_verdict logs how late a verdict that missed its
# expiry actually was, and that line is the number to size this from. It has to stay
# inside the aggregator's 5 s user_turn_stop_timeout (SMARTTURN_STOP_SECS plus this),
# so that an expiry still answers late rather than never.
_HOLD_SLACK_SECS = 1.0

# The trailing-silence hint on the commit (teagram-engine#37, teaport#53).
#
# A final commit costs the engine a fixed pad: it right-pads the audio with 12 tokens
# (960 ms) of zeros so the delayed decoder can flush the last words, and decodes them.
# Since teagram-engine#36 it credits trailing silence it has already fed against that
# pad -- but it counts that silence with its own VAD (fvad, mode 1), which on a phone
# line hears the noise floor as speech and credited under 80 ms on 37 of 58 commits of
# the 2026-09-20 18:25 call. Silero, upstream, knows better: the VAD stop this service
# commits on IS the finding that the last ENDPOINT_STOP_SECS of audio were silence, and
# every sample sent between that stop and the commit (the verdict's ~170 ms, or the
# ceiling's second) is silence too. So the commit says how much: `trailing_silence_ms`,
# the audio sent since Silero's speech end, less a margin. The engine takes the larger
# of its count and ours, clamped to what it has actually fed; an engine without #37
# ignores the field. Measured on jetson06 (2026-09-20, teagram-engine PR #39): identical
# transcripts, the commit-to-done latency of a full-pad commit 662 -> 232 ms.
#
# The count is a SAMPLE position, never a wall-clock difference: the VAD stop names
# the audio (stop_secs of silence, ending at the frame that tripped it), and the
# service counts what it has sent, so the two are on one clock. Which side of the stop
# the count errs on is decided by the pipeline's shape, and it is the safe side. The
# VAD lives in the user aggregator, DOWNSTREAM of this service (pipecat >= 1.0), and
# run_stt sends a chunk before process_frame passes it on -- so every sample the VAD
# had heard when it fired the stop was already on the wire, and by the time the stop
# reaches here (a SystemFrame, upstream, four queues) more have followed. The service
# therefore places the speech end no earlier than it was, and the hint UNDER-claims by
# whatever was in flight (a frame or two on an idle box, to be read off
# endpoint_debug's skew probe on a real call -- unclaimed silence, not a risk). Where
# the session's VAD analyzer keeps its own speech end on the count of audio passed down
# to it (THE CALLER'S VOICE), that is used instead: the same point, exact. The one
# way to over-claim is audio that passed the service unsent -- a
# reconnect buffering it, a dead socket dropping it -- so any such gap since the
# caller last started speaking withholds the hint for that stop (_hint_gap).
#
# The margin covers what Silero's speech end is not: the decoder's. Its confidence gate
# drops an unvoiced tail ("s", "f") before the audio does. On the engine an over-claim
# of K tokens is a (K-1)-token cut into its 8-token buffer (the #28 knee): 80/160/240
# ms over cost nothing on 30 utterances, 400 ms truncated the last word on 7. 100 ms
# sits well inside that, on top of the engine's own one-token margin.
#
# With the carry on (TEAPORT_STT_CARRY_MS), the margin gives way at one floor: it never
# takes a claim below _REPRIME_MS, the pause the engine must be told of before it
# re-primes the next segment, the one the carry opens (CARRY THE TAIL). Talk's 0.2 s
# stop confirms 192 ms of quiet (six 32 ms Silero chunks), so the barge-in commit at
# that stop, with nothing in transit, would claim 92 ms and lose the re-prime the carry
# is there for. Where the window is short the margin shrinks instead, as far as Silero's
# speech end itself: an over-claim of the decoder's unvoiced tail at most, inside the
# 240 ms that cost nothing above. A window under _REPRIME_MS keeps the whole margin; it
# cannot vouch for the pause either way. With the carry off there is no re-prime to
# serve, and the margin is held back whole, as before #154.
#
# Off (0/false): the commit carries no field and the engine credits its own count, as
# before -- the A/B switch, not a fallback the service takes on its own.
STT_SILENCE_HINT = setting("TEAPORT_STT_SILENCE_HINT")
_HINT_MARGIN_MS = setting("TEAPORT_STT_SILENCE_HINT_MARGIN_MS")
# Mirrors the engine's REPRIME_MIN_PAUSE (teagram-engine --reprime, issue #154): the
# engine gates re-prime eligibility on the raw hint this service sends, so a hint of at
# least this much makes the commit eligible. Nothing of the engine's VAD is modelled here.
_REPRIME_MS = 100

# THE CALLER'S VOICE: no segment boundary over the caller's resumed words (issue #140).
#
# Everything above treats the audio after a VAD stop as silence until a VAD start says
# otherwise. The start is slow, though. Silero needs start_secs of speech at its
# confidence AND its volume gate, and the gate integrates over 400 ms, so after a long
# pause the start trails the caller's first word by 0.2-0.4 s. A commit that goes out
# inside that window is drawn over words the VAD has not yet admitted to, and it loses
# them in two ways (2026-10-10 Talk session, 4 of ~24 utterances cut, every one spoken
# in the clear):
#
#   * The hint over-claims. It counts everything since the stop's speech end as
#     silence, so the commit tells the engine to skip decoding the tail that holds the
#     onset of the next words. Those words were sent before the commit, so the next
#     segment does not have them either. Worst on the ceiling's commit, which claims
#     the whole hold: "vad 0, client 787" took "fusion" out of "then a fusion
#     reaction".
#   * The boundary falls mid-word. Even with no hint, a commit inside a word loses or
#     garbles it in both segments ("There's" -> "" / "Essa phone call"). On Talk the
#     0.2 s stop with a COMPLETE verdict commits at short in-sentence pauses, and on
#     each drop the VAD start reached the STT within ~0.3 s of the commit.
#
# The fix reads the session's VAD analyzer through the speech-onset test
# (speech_onset.py): Silero's confidence with no volume gate to lag behind, the test
# the reply hold already trusts for "speech has begun". The analyzer keeps that test's
# state as VoiceMarks, at positions on the count of audio this service passes down to
# it (_passed_samples), and where the VAD's own last stop tripped. Voice after that
# stop decides two things:
#
#   * The hint counts from the later of the stop's speech end and the last voiced
#     chunk since (_claim). Any single chunk counts: the hint never claims silence
#     the caller has broken, unless the next segment gets that voice again (CARRY THE
#     TAIL).
#   * A complete verdict does not draw the boundary while the caller is speaking
#     again. Speaking is an onset since the stop that the onset test has not yet seen
#     go quiet (its own offset point, the stop's window of quiet), or a run of voice
#     since the stop still too short to tell. A single chunk is not: the onset test
#     false-alarms on those, and a cough must not hold a turn. The segment stays held,
#     as it is under an INCOMPLETE verdict, and is committed at that pause. If the VAD
#     start comes first, the existing resume path keeps the segment open and the next
#     stop asks again. Only the boundary moves. The verdict has already reached the
#     turn, and the stop strategy waits for this close (LateStartTurnStopStrategy's
#     owed close), so the turn still ends on the words it was waiting for.
#
# The analyzer has to have heard the audio before it can vouch for it. It runs
# downstream, in the user aggregator, and the aggregator does not analyze anything
# while Smart Turn's inference runs on the VAD stop. So when the model's verdict
# arrives, the ~0.1-0.3 s of audio that came in during the inference is still
# unanalyzed, along with the frames in transit between the two, and that is the
# window the Talk drops fell in. The commit therefore waits until the analyzer has
# been handed all the audio this service had passed down when the verdict came
# (_passed_samples against VoiceMarks.received). The analyzer wakes the wait each time
# it has run more audio (voice_heard), so the cost every COMPLETE commit pays is the
# backlog's own VAD passes: ~5 ms for a 300 ms backlog on the desktop, an estimated
# 15-40 ms on the appliance's ARM cores. Three bounds keep a wait from becoming a
# stall:
#
#   * _VOICE_CATCHUP_SECS for the analyzer to catch up. Past it, the boundary is
#     decided on what has been heard (logged at most once a _SLOW_LOG_SECS). A miss is
#     only slowness if the analyzer gets there later, so the next verdict checks that
#     it has by then. If it never has, frames are going somewhere it does not hear,
#     and the marks go off for the session, as they do for counts that disagree
#     outright.
#   * smartturn_stop_secs for the voice to pause. That is the session's own bound on
#     honouring a "not done", and it is the worst case a reply waits here. A caller
#     who never pauses gets the commit at that bound with a warning, and the hint then
#     claims nothing, since the voice is current.
#   * _VOICE_STRIKES verdict commits running that hit that bound stand the wait down
#     for the session, logged once. Any other verdict commit breaks the run, and so
#     does a VAD start during a wait. Commits that answer no verdict (a stop over the
#     bot's reply, the backstop, a tail, an expired hold) neither count nor break it,
#     and nor does a VAD start between turns: every verdict follows a stop, and every
#     stop a start, so a run broken by any start could never reach two. Such a run is
#     steady noise at speech confidence (a TV in the room, a phone line's floor),
#     which would otherwise cost every turn the whole ceiling.
#
# What it cannot see: voice in the last chunk or two before the commit, which Silero
# has not yet scored at onset confidence. The hint's margin covers that much of an
# over-claim. A boundary there can still clip the first phoneme of a word; the engine
# holding back its last ~200 ms when its own VAD hears speech would close that, and
# that change is the engine's to make.
#
# brain/formal/SttCommit.tla (VOICE, the sc_voice rows) checks the wait against the
# commit protocol: its ways out, the hold it shares a slot with, and the final's rules
# for ending the turn.
#
# Without an analyzer that keeps the marks (a session built without the onset mixin,
# or the unit tests that drive this service alone) none of this applies: the verdict
# commits at once and the hint counts from the stop, as before.
_VOICE_CATCHUP_SECS = 0.1
_VOICE_STRIKES = 3
_SLOW_LOG_SECS = 60.0

# CARRY THE TAIL: the next segment opens at the speech end, not at the commit (#154).
#
# THE CALLER'S VOICE keeps a verdict's boundary off voice the analyzer has heard. It
# cannot keep it off voice that starts in the last chunk or two before the commit, or
# just after it, and it does not cover the commit of a stop over the bot's reply (the
# barge-in flush, sent at once). A room session replayed through the box's engine
# (2026-10-11, 58 live cuts) lost "You're" from "You're not supposed to know" to a
# barge-in commit drawn 0.14 s into the word ("He led the post to know"), and "No little
# opening" came back "No, they'll opening" from a verdict committed 0.1 s before the
# caller's next word, with no audio ahead of that word in its segment.
#
# So every commit, whatever path sends it, sends the audio from where its claim starts
# again, as the start of the next segment: the commit goes out, then that audio, then
# the live stream goes on. The committed segment decodes up to where the hint says the
# speech ended, and the next one opens there, behind the margin.
#
# ONE RULE ties the two together: the carry starts where the hint's claim starts (or at
# the bound, if that is later), and the committed segment decodes through the margin
# past that point. So only the margin (by Silero, silence) is decoded by both segments,
# and what neither decodes is silence the claim skipped, as with no carry at all. Where
# the claim starts is the choice:
#
#   * The carry reaches back to the speech end (the bound -- _CARRY_MS back, the
#     previous commit, the session's start -- is no later than the speech end plus the
#     margin): the claim counts from the speech end. Voice heard since the stop does not
#     hold it back (THE CALLER'S VOICE), since that voice is in the carry: it goes to
#     the next segment whole, with the silence before it, and the committed segment does
#     not decode its onset a second time. The barge-in commit (on the phone's 0.5 s
#     stop, while the stop reaches the STT within 0.1 s), and a verdict on Talk's 0.2 s
#     stop that came within ~0.4 s of the stop.
#   * It does not (a ceiling's commit; any verdict on the phone's 0.5 s stop): #140's
#     rule stands. The claim counts from the later of the speech end and the last voice
#     since the stop, and the carry starts there too. Voice since the stop stays whole in
#     the committed segment, and the carry holds only the silence after it.
#   * A commit THE CALLER'S VOICE held for the caller's pause (for_voice) takes the
#     second rule whatever the bound. The turn waited for those words, and the stop
#     strategy ends it on this close; claimed and carried, they would be the next
#     segment's, missing from the turn's final, and that segment, opened on words no
#     stop is waiting for, would close only on the backstop.
#   * A commit that claims nothing -- no speech end placed (the backstop, audio passed
#     unsent), the hint off, or nothing past the margin (the caller's voice current at
#     the wait's bound) -- carries nothing: its segment decodes its whole tail, and a
#     carry would hand the next one the same audio again.
#
# THE WIRE. The carry must follow its commit with nothing between them, and the claim is
# made at the commit's position on the wire, which _sent_samples must name exactly. One
# lock (_wire, through _on_wire) is held around every append run_stt sends and around a
# commit with its carry, and every commit is DECIDED under it too: the VAD's stop and
# start and the verdict are handled holding it (_turn_edge), and a timer (the wait for
# the caller's pause, the hold's expiry, the backstop) takes it before the check that
# decides its commit and gives up its slot only then (_fired). A decision taken outside
# could queue behind a timer's commit and go out after it, a second commit for one stop
# (SttCommit.tla takes deciding and committing as one step). The timers are the only
# commits beside the input task, and they meet run_stt only while a send waits on the
# socket's back-pressure, when either would wait for the same drain anyway. The commit
# and its carry go on the socket the commit went out on: a session closed in between
# takes its segment and the carry along.
#
# The clocks. _sent_samples stays the count of the caller's audio sent once -- the clock
# the speech end, the hint, the VAD's counts and the gap check are on -- and the carry
# does not move it. The engine's clock (its transcription report) counts every sample
# appended, the carried ones too, so each carry moves _session_origin back by what it
# sends, before that send is awaited: a report inside the carried stretch then names
# exactly the audio it holds, and one before it reads early, the safe side
# (SPEECH_DECODED).
#
# Measured on that replay (the box's engine and model, the live cut schedule, scored
# against Deepgram): 26 word edits without the carry, 23 with it (21 vs 28 decoding each
# segment on its own), and the two phrases above right in all four runs that carried
# from the speech end (or from it plus the margin). That run predates the rule above for
# a bound-cut claim (it carried from the bound) and for a commit held for the caller's
# pause; the two phrases are first-rule commits, which it leaves as they were.
# Starting the carry at the speech end plus the margin instead scored 30 and 24: the
# silence ahead of the word helps the next segment, and the shifts of a segment's start
# alone move the score by 3-4 edits either way, so the totals are noise around the two
# fixes. What the carry does not fix: a segment that opens on a short function word
# ("of the T-Port", "to make sure", "I mean") still gets it wrong with the word whole,
# because the decoder starts each segment with no context. Only the previous words give
# it that -- not cutting there (a guard on every commit, which costs every turn its
# length), or an engine that keeps its context across a commit. No commit or done is
# added or moved; what changes for brain/formal/SttCommit.tla is that the segment after
# a quiet commit is fed (the carry), so an interim, and the backstop behind it, can
# follow it (CommitAt, CARRY).
#
# 0 turns it off (the A/B switch): the next segment opens at the commit, as before.
_CARRY_MS = setting("TEAPORT_STT_CARRY_MS")

# TURN TEXT: what the turn a commit closes is made of (TEAPORT_STT_TURN_TEXT).
#
#   segment  The commit closes an engine segment: the engine finishes it (right-pads and
#            redecodes the buffer), answers with transcription.done, and decodes what
#            follows from nothing. The done is the turn's text. Everything above this
#            note -- the hold for the verdict, the wait for the caller's pause, the hint's
#            re-prime floor, the carry -- exists because that close is irreversible and a
#            misplaced one damages the words on both sides of it.
#   slice    The engine's preview stream (teagram-engine, ~/rbf/PROTOCOL3.md; opt-in
#            "preview": true, offered as "transcription.preview" in session.created): ONE
#            continuous decode per session, which commits never touch. A commit at P (the
#            audio sent so far) is a question: the engine answers it with one
#            transcription.preview -- the text a finish at P would add now, decoded on a
#            snapshot it throws away -- and the main transcript goes on as if it had not
#            been asked. Every main delta carries end_ms, where its word ends, and the
#            progress report end_through_ms: no delta after it ends before it. The turn a
#            commit at P closes is the main words ending in [P', P), P' the commit before:
#              provisional  those received by the preview, then the preview's text. Pushed
#                           at once as the turn's final, so the reply starts when it does
#                           today: commit -> preview is the done's ~0.15 s.
#              final        all of them, once end_through_ms >= P (~0.5 s later): the
#                           authoritative text. It equalled the provisional on 97 % of the
#                           288 turns measured on jetson05 (~/rbf/RESULTS2.md, "Spike
#                           2b"), and the provisional was, overall, MORE accurate than
#                           today's done (calls WER 14.8 vs 15.6 %), most of all at a
#                           turn's first words: no decoder starts a turn from nothing.
#            A word ending at or after P is the next turn's, even when it arrives before
#            P's preview: the commit often goes out after the caller resumed. A word of a
#            committed turn arriving after the commit is never an interim -- it would start
#            a turn and cut the reply to its own words -- it is that turn's final's.
#
# THE CUT'S MARGIN. P is the audio sent when the commit went out, and end_ms is the
# engine's estimate: a turn's last word is often stamped as ending just past P (the
# peek's commit goes out at Silero's stop), and a commit with no pause behind it (the
# backstop, a stop over the bot) can fall inside a word. The preview, decoded on the
# audio up to P, has the word; sliced by end_ms alone the final lacks it (a "drops"), and
# the open turn has it, as an interim that the backstop later commits as a turn of its
# own. So a WORD past P is P's turn's when P's preview VOUCHED for it -- it is a word of
# the preview's text (its first piece a prefix of one; the rest of its pieces, which
# share its positions, follow it) that no word that came in has claimed -- and it ends
# less than _VOUCH_MS past P or STARTS before P (audio_ms). A turn whose preview still
# vouches for a word is final once end_through_ms >= P + _VOUCH_MS and audio_through_ms
# >= P (no word starting before P can still come), not at end_through_ms >= P. A word the
# preview did not have is the next turn's however close to P it ends: the caller resumed.
# Picked by re-slicing the replays offline, the brain's slicing simulated in log order
# (it reproduces the live settles exactly): on the 9305a0d engine (jetson05 2026-10-30,
# peek arm, calls1-4 + FLEURS, 290 turns) end_ms < P alone agreed 94.8 %; vouched per
# PIECE, end + 200 (as first written: a word's later pieces fell to the next turn)
# 95.5 %; by word, end + 200, 95.5 % (the slice arm: 94.4 -> 95.5 %); by word, start or
# end + 200, 95.9 %; +300 96.2 % for +0.08 s on the final's p90. On 03e4223 (288 turns)
# every vouched rule gave 97.9 % against 96.5 % for end_ms alone. Slicing by audio_ms for
# every word is worse (PROTOCOL3: 81.6 %; the caller's next word often starts before a
# commit sent after they resumed): only a word the preview vouched for may.
#
# Where the final differs from what was pushed, the STT says so (TurnTextFinalFrame) and
# turn_text.TurnTextCorrector applies it, without a restart: see there. A provisional
# with no words is the STT's own business (_settle_turn), since nothing went on for the
# corrector to correct:
#   * over the bot's reply (the commit is the barge-in) a provisional with none of the
#     caller's words -- an empty preview, or only the bot's own heard back -- does not
#     close the turn: it closes on its final, stamped with its stop, so its words are
#     decided as the barge-in they are, with the reply paused -- not later, unpaused;
#   * otherwise the empty provisional closed the turn wordless, and a final with words
#     goes on as new words, stamped NO_STOP, through the close every final goes through
#     (_close): the echo guard and the wake gate decide them as they would any.
# The words are the caller's; a preview that missed them must not lose them. A preview
# the engine could not take whole ("complete": false, PROTOCOL3 difference 7: its text is
# a held word's pieces) is no preview: nothing is speculated on it, and the turn closes
# on its final, as the one over the bot does -- the final decides.
#
# Nothing waits on a final: the provisional text is what the reply, the owed records and
# the speculation run on, and the next turn is sliced whether the last is final or not.
# Only the order of the finals does -- each settles after the one before -- and a final
# can fail to come: an engine whose end_through_ms freezes through the pause after the
# caller stops (teagram-engine 03e4223: until the next utterance starts) never passes a
# commit sent more than ~1.3 s after the stop, and a session's last commit perhaps never.
# So a turn's final has _FINAL_TIMEOUT_MS past its position, by the engine's
# decoded_through_ms or by the wall clock since its commit went out, whichever comes
# first; then the turn settles as its provisional text ("[TURN] ... final timeout") --
# a turn that closes on its final (over the bot, above) closes on the words in hand --
# and a word of it that comes later goes to the open turn, as one the bound let through.
#
# In slice mode a commit damages nothing, so what repaired its damage is off: the carry
# (no segment opens behind a commit), with it the hint's re-prime floor, and the wait for
# the caller's pause (THE CALLER'S VOICE: words they resume with end after P and go to the
# next turn whole). What stays: WHEN commits go out (the verdict, the hold, its expiry,
# the stop over the bot, the backstop) -- the measured design kept the brain's commit
# schedule -- and the trailing-silence hint, which still shortens the preview's pad
# exactly as it shortens a finish's (PROTOCOL3: credited_ms; it never reaches the main
# stream). Without it the engine credits its own fvad count, ~0 on a phone line, and the
# preview pays the full ~1 s pad: the one remaining dependency on the old machinery.
#
# The mode is the setting's ask and the engine session's answer: sent in every
# session.update, taken only where the session's capabilities offer it. An engine without
# the preview ignores the field and the session runs on segments, as before.
TURN_TEXT = setting("TEAPORT_STT_TURN_TEXT")

# THE CUT'S MARGIN, in ms past a turn's position: how far past it a word the turn's
# preview vouched for may end and still be the turn's.
_VOUCH_MS = 200

# How long past its position a turn's final may take (TURN TEXT: nothing waits on a
# final), in audio ms: the final comes ~0.7 s after the preview at the 90th percentile
# (jetson05, 288 turns).
_FINAL_TIMEOUT_MS = 2000

# THE PEEK: in slice mode, ask the preview at the VAD stop, while Smart Turn decides
# (TEAPORT_STT_PREVIEW_AT_STOP).
#
# A commit at the verdict puts Smart Turn's inference and then the commit's preview in
# series: on the box (875 COMPLETE phone turns) the stop-to-commit time was p50 0.47 s,
# 0.17 s of model then 0.26 s to the final. In slice mode a commit is only a question,
# so it can be asked at the stop: the preview (~0.15 s) lands about when the verdict
# does. The commit at the stop is a PEEK. It takes no words and closes nothing: the
# turn's words keep streaming as interims, since the turn is not closed. Then:
#
#   * a VAD start (the caller resumed) drops it. Its preview, when it lands, is
#     ignored, and the speculation asked on it is cancelled by the stop strategy on that
#     start. The next stop peeks again, and that turn is every word since the last turn,
#     whatever the dropped peek saw: an earlier preview never splits a turn;
#   * anything that would commit for that stop -- a COMPLETE verdict, the ceiling's, the
#     hold's expiry, the turn closed under the ceiling -- ADOPTS it instead of
#     committing: the turn is the open words that end before the peek's position, its
#     provisional text the peek's (the words in hand at its preview, then the preview's
#     text), pushed at once, or when the preview lands if the verdict came first. No
#     second commit; the final waits for end_through_ms to pass the stop's position, not
#     the verdict's;
#   * its provisional goes downstream when the preview lands (SpeechPreviewFrame), and
#     the stop strategy asks the speculative reply on it -- even with no user turn open
#     yet, for the short answer whose words all come after the stop -- which the turn
#     then adopts through the Speculator's own whole-context check.
#
# One commit in flight: a stop while an answer is outstanding waits for it, then peeks
# if no VAD start came meanwhile. A stop over the bot's reply still commits at once:
# that commit is the barge-in, and it closes the turn as in segment mode.
PREVIEW_AT_STOP = setting("TEAPORT_STT_PREVIEW_AT_STOP")

# SPEECH_DECODED: the engine's transcription report (teagram-engine's
# transcription.progress), turned into SpeechDecodedFrame for the stop strategy.
#
# The speculative reply asks the LLM on the interim once it is the speech's whole text
# (speculate.py). A timer cannot know that: after the VAD stop the engine is still
# working through its 480 ms delay window, and its deltas pause 0.23-0.56 s between
# words while it does (live 2026-10-10: 17 of 29 misses "superseded" with a 160 ms
# window). An engine that offers the report (session.created's "capabilities") sends,
# once opted in, how far its transcription has got -- decoded_through_ms, on the clock
# of the audio this session has appended, the hint's clock: nothing more will come for
# any sample before it. So the position goes onto the same clock as the speech end above
# (_sent_samples at the connect, plus the report), and once it passes the speech end of
# the VAD stop the segment is waiting on, the strategy is told, once per stop.
#
# The speech end is Silero's, placed no earlier than it was (the hint's argument), and
# counts as transcribed only once the engine is _REPORT_MARGIN_MS past it, for the
# hint's margin's reason: the decoder's speech end can trail Silero's by an unvoiced
# tail. Its own setting, not the hint's: the two are the same physical gap, but tuning
# the commit's claim must not move the speculation's trigger. Measured on jetson05
# (2026-10-10, 32 replayed phone calls, 92 fallthrough turns with words): the report
# passed the speech end a median 0.66 s after the VAD stop, +100 ms at 0.75 s -- 91
# hits either way, 1 wasted request, against the 160 ms window's 92 hits for 242
# requests. No speech end placed (audio left unsent, see _hint_gap): no
# report can speak for that stop, and the strategy is told so (NoSpeechReportFrame) and
# decides it with the window.
#
# Asked for only when the session will use it: build_agent_session sets
# transcription_progress when the speculative reply is on and set to wait for the
# report, so with either off the handshake is what it was.
_REPORT_MARGIN_MS = setting("TEAPORT_STT_TRANSCRIBED_MARGIN_MS")

# The stranded-segment backstop. This service has exactly ONE path to a final: a commit
# sent when VADUserStoppedSpeakingFrame arrives, or the verdict that answers it (see
# process_frame). That makes a missed VAD stop unrecoverable, and silently so -- the
# turn started from an interim, owns no
# text, and TurnAnalyzerUserTurnStopStrategy cannot fire without text, so it hangs until
# the aggregator's 5 s user_turn_stop_timeout force-stops it with an empty aggregation and
# the model is never asked. The engine's own buffer auto-commit would close the segment
# eventually, but at ~15 s it is far outside that 5 s window, so the words are always lost.
#
# Live 2026-09-09 17:17:43, mid-Hamlet: the caller said "Stop there", the interim started
# the turn and cut the bot correctly, no final ever arrived, and the turn force-stopped at
# 5.006 s ("User stopped speaking (strategy: None)") -> SILENT TURN reached=['nothing'].
# The barge-in worked perfectly and the reply was still lost.
#
# So: if a segment has streamed interims and then gone quiet for this long without a final,
# commit it ourselves. Armed off INTERIM ACTIVITY and nothing else -- deliberately not off
# a turn-stop frame, which is the coupling process_frame's comment rejects as a deadlock
# (that frame only fires after the turn is judged complete, which itself waits on this
# final). A still-talking user keeps interims coming and keeps re-arming it, so this can
# only fire on a segment that really has stalled.
#
# 1.5 s: comfortably past the widest interim gap observed on the wire (~0.6 s) and past the
# VAD's own ENDPOINT_STOP_SECS commit, so the normal path always wins the race and this
# costs nothing when VAD is healthy; and far enough inside the 5 s turn timeout that the
# forced final still lands in time to be answered. The failure it converts is "no answer,
# no explanation" into "an answer about a second late".
# setting(), not a bare float(): this is read at import out of /etc/teaport/brain.env, which
# installer repairs preserve verbatim, so one operator typo would otherwise be an
# import-time ValueError and a Restart=always crash-loop nothing but a hand-edit clears.
_STRANDED_INTERIM_SECS = setting("TEAPORT_STRANDED_INTERIM_SECS")

# STREAMING BACKEND ONLY: how long to wait after the VAD stop before drawing the segment
# boundary, so the model's trailing deltas are in.
#
# Voxtral Realtime transcribes on a deliberate delay -- transcription_delay_ms in
# tekken.json, 480 ms as shipped -- plus the time to generate those last tokens. Cutting
# at the VAD stop takes the end off every utterance and gives it to the next segment.
#
# This is real latency added to the RESPONSE (the final gates the LLM turn); it is NOT
# added to barge-in, which triggers on interims and those arrive in ~0.03 s. 0.7 is the
# 480 ms delay with slack for generation; too low resurrects the split, too high just
# makes replies later.
_STREAM_TAIL_SECS = setting("TEAPORT_STREAM_TAIL_SECS")

# Caller-path makeup gain, in dB, applied to the audio sent to the TRANSCRIBER only
# (not to VAD or endpointing, which read the frame upstream of this and are unaffected).
# Caller speech spoken over the bot is quiet: measured 2026-09-11, ~4 dB quieter within a
# call than the same caller in the clear, and the segments the engine returns EMPTY are
# the quietest of all (-34 dBFS). A flat makeup gain recovers the quiet barge-in "stop"s
# the engine was dropping: batch-decoding the corpus at +6 dB recovered 4 of 12 dropped
# double-talk clips and 3 of 4 dropped clean clips and lost NONE already read (n=205). It
# does not reach the residual misses that survive +12 dB with headroom to spare -- those
# are the model's, not level (see teagram-engine#7).
#
# The quiet is NOT mainly the bridge's echo canceller, contrary to the first read of this:
# an offline test through the exact pjmedia WebRTC AEC (2026-09-11) showed the bridge's
# conservative setting pulls near-end down only ~1 dB and never silences it -- only the
# aggressive setting, which the bridge does not use, both attenuates hard and drops words.
# So the ~4 dB is mostly genuine soft over-talk (people speak quietly while listening),
# plus double-talk gating a clean offline mix cannot model. This gain compensates the
# quiet whatever its cause; lowering aec_aggressiveness is not the lever.
#
# Off (0.0) by default and threaded from the FRONT-END, not read from the shared brain.env:
# the OpenClaw Talk path does its own client-side echo cancellation and must not get this,
# and both brains read the same env file, so only sip_server passes a non-zero value.
# A fixed gain with a hard clip, deliberately simple; a level-targeting AGC is the v2 if a
# flat boost proves too blunt (it pushes already-loud audio toward the clip guard).

# Retry budget for a connect the engine REJECTS because its single STT slot is still
# held (see _connect_websocket). Sized to cover the hand-off window both front-ends
# currently paper over with `await asyncio.sleep(0.3)`, while staying well inside
# greet()'s 12s tri-state resolution window so a genuinely busy engine still gets the
# spoken warning promptly.
_CONNECT_ATTEMPTS = 4
_CONNECT_RETRY_S = 0.4

# What ONE attempt costs against a host that accepts the connection (or blackholes the
# SYN) and then never finishes the handshake: the websockets library's default
# open_timeout. Not a setting — recorded so the budget below is derived rather than
# guessed, and so a future `websockets.connect(open_timeout=...)` has one place to move.
_CONNECT_OPEN_TIMEOUT_S = 10.0

# The worst-case wall time start() can spend inside _connect_websocket, and therefore
# how long a StartFrame can sit in this service before it reaches the next processor.
# pipecat 1.8.0 put a 20 s ceiling on that trip (PipelineTask.start_timeout_secs) and
# TEARS THE PIPELINE DOWN when it is passed, which would turn the deliberate
# "let the pipeline start so the brain can speak a warning" path below into a dead
# session. agent_session.py sizes the task's start budget from this. 41.2 s at the
# current constants — every attempt timing out, which needs a host that is reachable,
# rejecting in a way _is_slot_busy recognises, and slow about it.
CONNECT_BUDGET_S = (_CONNECT_ATTEMPTS * _CONNECT_OPEN_TIMEOUT_S
                    + (_CONNECT_ATTEMPTS - 1) * _CONNECT_RETRY_S)


try:
    import websockets
    from websockets.protocol import State
except ModuleNotFoundError as e:
    logger.error(f"{e}\nInstall with: pip install websockets")
    raise


def _is_slot_busy(exc: Exception) -> bool:
    """True for an engine REJECTION (HTTP 503 / 4xx-5xx handshake failure), as opposed
    to an unreachable host.

    The distinction decides whether retrying is worth anything: a rejection means the
    engine is up and its slot is momentarily taken, an unreachable host means each
    further attempt burns the websockets open timeout for nothing.

    Duck-typed on purpose. websockets is lock-pinned but bounded per-major and bumped
    deliberately (see brain/pyproject.toml — the range is OURS to own; pipecat's base
    dep is unbounded), and the rejection carries its status as `.response.status_code` on
    >=14 but `.status_code` on older releases. Matching the shape rather than the class
    keeps this working across that range; an unrecognised exception is treated as NOT
    retryable, so the worst case is the behaviour we had before.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    return isinstance(status, int)


@dataclass
class FinalTranscriptionFrame(TranscriptionFrame):
    """A final transcript that an interruption cannot flush, stamped with the VAD stop
    it answers.

    TranscriptionFrame is a plain data frame. On InterruptionFrame every processor
    flushes its queued data frames (FrameProcessor._start_interruption ->
    FrameQueue.reset), and the LLM user aggregator flushes its own queue the moment
    it broadcasts one (FrameProcessor.broadcast_interruption). The aggregator
    broadcasts exactly that when MinWordsUserTurnStartStrategy starts a user turn
    from an INTERIM — so when the engine closes a segment on its own, the final
    rides a few frames behind the interim that starts the turn, and the turn's own
    start flushes the turn's own words out of the pipeline. The turn is then empty:
    TurnAnalyzerUserTurnStopStrategy cannot fire without transcript text, the 5 s
    user_turn_stop_timeout force-stops the turn, push_aggregation() has nothing to
    push, and the user gets silence. Live 2026-08-25 19:05 on jetson01: "Can you
    recite Hamlet's soliloquy for me?" — charted by the ledger at the STT's push,
    never seen by the model (SILENT TURN reached=['nothing']).

    Frame.interruptible False is pipecat's designed escape hatch for exactly this:
    FrameQueue.reset() keeps such frames, and a task processing one is not
    cancelled (same pattern as pipecat's own FunctionCallResultFrame; pipecat 1.11
    moved it from the UninterruptibleFrame mixin to this per-class field). Interims
    stay interruptible on purpose — they are disposable hypotheses.

    `stop_n` names the VAD stop this final answers, as a COUNT of
    VADUserStoppedSpeakingFrames: the number this service had seen when it sent the
    commit the final answers (see _send_commit), and the stop strategy's own count of
    the same frames is what it compares against -- the same frames, so the same
    numbers, with no clock read in two places; but not at the same moment (the VAD
    reaches the two by different queues), so a stamp can name a stop the strategy has
    not counted yet, and it keeps that close as a receipt. A final for
    a stop the strategy has moved past (a later stop, or the caller started again) is
    an EARLIER utterance's, landing late -- the engine's finish takes ~0.7 s, and a
    caller who speaks in bursts starts the next utterance inside it -- and must not
    end the turn: that ended turns on the previous utterance's words and the real
    final then cut the reply at 0%. A done the engine sent on its own is stamped by
    _handle_message's rules, NO_STOP among them. The final IS its segment's close; a
    wordless close is a SegmentDoneFrame instead. See
    endpointing.LateStartTurnStopStrategy, and brain/formal/SttCommit.tla for the
    protocol and its properties.

    None (another producer's final) is taken as the stock strategy would.
    """

    interruptible: bool = field(default=False, init=False)
    stop_n: Optional[int] = None


@dataclass
class _Commit:
    """A commit on the wire and not yet answered by a done (see _send_commit)."""
    n: int        # VAD stops seen at the send: the stamp its final carries
    at: float     # monotonic time of the send, for the segment line's latency
    why: str      # what triggered it: vad-stop | verdict | backstop | ...
    hint: Optional[int] = None   # trailing_silence_ms it carried, if any (STT_SILENCE_HINT)
    # How long a complete verdict's commit was held, in s, and whether for the caller's
    # pause rather than only for the VAD analyzer to catch up (THE CALLER'S VOICE).
    waited: Optional[float] = None
    for_voice: bool = False
    # Audio re-sent behind it as the next segment's start, in ms (CARRY THE TAIL).
    carry_ms: int = 0
    # The turn it closes, in slice mode (TURN TEXT); None on segments.
    turn: Optional["_Turn"] = None
    # Or the peek it is, at a VAD stop (THE PEEK).
    peek: Optional["_Peek"] = None


@dataclass
class _Peek:
    """A commit at a VAD stop that closes nothing until it is adopted (THE PEEK)."""
    at_ms: int                 # its position on the engine session's clock
    n: int                     # the VAD stop it was asked at
    commit: Optional[_Commit] = None    # the commit it went out as
    previewed: bool = False
    asked: str = ""            # the provisional text: words in hand at its preview + its text
    asked_n: int = 0
    msg: Optional[dict] = None    # the preview (an empty one, if the engine cut it short)
    seen: int = 0              # open words at its preview: later ones may be vouched for
    dropped: bool = False      # the caller resumed: it is no one's
    turn: Optional["_Turn"] = None    # the turn that adopted it


@dataclass
class _Turn:
    """A commit's turn in slice mode (TURN TEXT), from the commit until its final."""
    at_ms: int                 # P: the commit's position on the engine session's clock
    stop_n: int = 0            # the commit's stamp (_Commit.n), for the lines
    # (piece, end_ms, audio_ms) of its main words, in arrival order: those in hand at the
    # commit, then those that arrive after it with end_ms < P, or vouched for.
    words: list = field(default_factory=list)
    since: Optional[float] = None    # the echo guard's clock: when its first words came
    previewed: bool = False
    asked_n: int = 0           # words in hand when the preview came
    asked: str = ""            # the provisional text
    # What went downstream as the turn's final: "" for a wordless close; None while the
    # wake gate slept through it, which a correction leaves be. And whether its words
    # woke the gate: then what went down is what followed the wake phrase.
    pushed: Optional[str] = None
    woke: bool = False
    preview_at: float = 0.0
    # The words of the preview's text (normalized, _word_key) no main word has claimed
    # yet: they may still come, past at_ms (THE CUT'S MARGIN).
    expect: list = field(default_factory=list)
    # Why nothing went on at its preview and it closes on its final, with this preview
    # and commit (over the bot's reply with an empty provisional, a preview cut short),
    # or "": it went on at its preview.
    deferred: str = ""
    msg: Optional[dict] = None
    commit: Optional[_Commit] = None
    # When its commit went out (the wall clock its final's timeout counts from).
    sent_at: float = 0.0


def _words_of(text: str) -> list:
    """The words a turn's texts are compared by: case and punctuation out, as the
    measurement compared them (~/rbf/harness/textnorm.py)."""
    return re.findall(r"[0-9a-z']+", (text or "").lower().replace("’", "'"))


def _word_key(text: str) -> str:
    """A word as the preview's and the main stream's are matched: its letters and digits,
    lower case ("A.D.," -> "ad"); a main word's first piece is a prefix of it."""
    return "".join(_words_of(text))


def compare_turn_text(asked: str, final: str) -> tuple:
    """How the final differs from what was asked: ("same", (), ()), or a kind -- "adds"
    (the final only has words the provisional lacked, the trailing "stop" a preview
    stopped short of), "drops" (only lacks some), "changes" (anything else) -- with the
    words the final adds and the words it drops."""
    a, b = _words_of(asked), _words_of(final)
    if a == b:
        return "same", (), ()
    added, dropped, changed = [], [], False
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "insert":
            added += b[j1:j2]
        elif op == "delete":
            dropped += a[i1:i2]
        elif op == "replace":
            changed = True
            added += b[j1:j2]
            dropped += a[i1:i2]
    kind = "changes" if changed or (added and dropped) else ("adds" if added else "drops")
    return kind, tuple(added), tuple(dropped)


class TeaportSTTService(WebsocketSTTService):
    """Streams audio to a teaport engine and yields interim + final transcripts.

    Args:
        url: engine /v1/realtime WebSocket URL (e.g. ws://jetson.local:8000/v1/realtime).
        model: model name sent in the session.update handshake. the engine is a
            single-model server and only acknowledges this, so any value works.
        sample_rate: audio sample rate fed to the engine. the engine expects 16 kHz
            mono PCM16; Pipecat resamples to this for us.
        language: language tag attached to emitted frames.
    """

    def __init__(
        self,
        *,
        url: str = "ws://127.0.0.1:8000/v1/realtime",
        model: str = "voxtral-mini-realtime",
        sample_rate: int = 16000,
        language: Language = Language.EN,
        ttfs_p99_latency: float = TEAPORT_TTFS_P99,
        streaming_backend: bool = False,
        makeup_db: float = 0.0,
        **kwargs,
    ):
        super().__init__(
            sample_rate=sample_rate,
            settings=STTSettings(model=model, language=language),
            ttfs_p99_latency=ttfs_p99_latency,
            **kwargs,
        )
        self._url = url
        self._model = model
        self._language = language
        # Smart Turn's silence ceiling for this session's front-end (the commit hold
        # below waits it out); build_agent_session sets it from endpointing.turn_settings.
        self.smartturn_stop_secs = SMARTTURN_STOP_SECS
        # Only the linear scale is kept, precomputed once; 0 dB -> 1.0 -> the fast no-op
        # path in _apply_makeup.
        self._makeup_scale = float(10.0 ** (makeup_db / 20.0)) if makeup_db else 1.0
        # What forces the final (the commit note above _VAD_STOP_COMMITS): the verdict,
        # until the session-wide fallback finds nothing answering the VAD stops.
        self._commit_on = "verdict"
        # verdict mode: a VAD stop has been seen for the open segment and the commit that
        # answers it is deferred to the verdict, and after a complete verdict to the
        # caller's pause (THE CALLER'S VOICE). Cleared by the commit, whichever path
        # sends it, and by a VAD start (the caller resumed: the next stop asks again).
        self._commit_pending = False
        # The hold's timer: its expiry until the verdict comes (see _HOLD_SLACK_SECS),
        # then the wait for the caller's pause if one is needed. One slot, so every path
        # that ends a hold ends either. And what the expiry needs to say: whether THIS
        # stop got the model's verdict (then it is the ceiling's that is missing), and
        # whether ANY verdict has ever arrived (then the wiring is known to work).
        self._hold_task = None
        self._hold_kind = "expiry"
        self._verdict_seen = False
        self._verdicts = 0
        # The session-wide fallback (the commit note above _VAD_STOP_COMMITS): set when
        # an expiry with no verdict ever seen switched commit_on to vad-stop, so the
        # first verdict to arrive can switch it back.
        self._commit_on_downgraded = False
        # When the last expiry committed, until a verdict arrives for that stop: the
        # verdict was late rather than lost, and the journal should say which.
        self._expired_at = None
        # The engine closed a segment on its own (a done with no commit of ours in
        # flight) since the caller last started speaking: the next VAD stop commits the
        # tail rather than hold it. Cleared at the VAD start.
        self._closed_unasked = False
        # Tracked from the transport output's broadcast; a VAD stop over the bot's own
        # voice does not wait for a verdict (the commit note above _VAD_STOP_COMMITS).
        self._bot_speaking = False
        # The mic path's wake words (wake_gate.WakeGate), set by build_agent_session for
        # a session that opens asleep: asked before any transcript is pushed or logged,
        # so the room's words never become a frame while it sleeps. None: every word goes.
        self.wake_gate = None
        # The room mic's echo guard (echo_guard.EchoGuard), set by build_agent_session:
        # the bot's own words, heard back through the speaker, are taken off the front of
        # every transcript before it is pushed. None: nothing is taken off.
        self.echo_guard = None
        # When the open segment's first words arrived (the guard's clock): it
        # decides by when the speech began, not when its final lands. None: no words yet.
        self._seg_since = None

        # STREAMING BACKEND (vLLM serving Voxtral-Mini-4B-Realtime).
        #
        # Same events and the same field names as our engine, but `final` on a commit
        # means the OPPOSITE thing, so the two are not interchangeable by URL:
        #
        #   our engine   commit(final=True)  -> "transcribe the buffer now", returns a
        #                                       transcription.done per segment
        #   vLLM         commit(final=False) -> START generating; deltas then stream for
        #                                       as long as audio keeps arriving
        #                commit(final=True)  -> end-of-stream sentinel; the session is
        #                                       over and nothing more is transcribed
        #
        # So there is no per-segment commit to send. Generation is started once, deltas
        # flow continuously, and a "final" is something we synthesise at the VAD stop by
        # snapshotting the interim buffer -- which _handle_message already falls back to
        # when a done carries no text, and already clears. Downstream sees the identical
        # frame sequence either way.
        self._streaming = streaming_backend
        self._gen_started = False
        self._boundary_task = None

        # TURN TEXT: whether the setting asks for slices (never of the streaming backend),
        # and whether this engine session gives them -- asked at the connect, settled by
        # its session.created, which the engine sends before anything else.
        self._slice_asked = TURN_TEXT == "slice" and not streaming_backend
        self._slicing = False
        # The open turn's words: (piece, end_ms) of the main deltas no commit has taken,
        # in arrival order (their text is _interim_buffer). The committed turns not yet
        # final, oldest first. The session's end_through_ms, and the end of the last turn
        # made final (no delta may end before it: the engine's bound).
        self._open_words: list = []
        self._turns: deque = deque()
        # The latest main piece: (audio_ms, end_ms, the turn it went to or None for the
        # open turn) -- where the rest of its word's pieces go (THE CUT'S MARGIN).
        self._last_piece: Optional[tuple] = None
        self._end_through = -1
        self._audio_through = -1
        self._final_through = 0
        self._warned: set = set()
        # Turns whose final equalled what was pushed, and those whose did not. And the
        # newest turn's final, where it dropped words the provisional had, until the next
        # turn closes: a backstop commit before then is counted (THE CUT'S MARGIN).
        self._turn_same = 0
        self._turn_differs = 0
        self._last_drop: Optional[tuple] = None
        self._drop_backstops = 0
        # The engine's decoded_through_ms, the timer for the oldest turn's final and the
        # turn it is for, the finals that timed out, and the lock that settles turns one
        # at a time in order (the receive task and the timer both settle).
        self._decoded_ms = -1
        self._final_task = None
        self._final_for: Optional[_Turn] = None
        self._final_timeouts = 0
        self._finals_lock = asyncio.Lock()
        # THE PEEK: the one outstanding (None: none, or adopted), and a VAD stop whose
        # peek waits for the commit in flight to be answered.
        self._peeks = self._slice_asked and PREVIEW_AT_STOP
        self._peek: Optional[_Peek] = None
        self._peek_wanted: Optional[int] = None

        # Running transcript for the current utterance (deltas are append-only).
        self._interim_buffer: str = ""
        # Consecutive finals that carried no words at all — see _handle_message.
        self._empty_finals = 0
        # Backstop for a segment whose VAD stop never came — see _STRANDED_INTERIM_SECS.
        self._stranded_task = None
        # Per-segment accounting for the commit/final boundary — see _log_segment.
        self._seg_bytes = 0        # audio appended since the last final
        self._seg_interims = 0     # interims the engine sent back for it
        # The trailing-silence hint (STT_SILENCE_HINT). The audio clock: samples this
        # service has put on the wire, ever -- positions, so only differences matter,
        # and a reconnect does not reset it. Where Silero's speech end fell on that
        # clock for the VAD stop the open segment is waiting on (None: no stop, or a
        # stop whose count cannot be trusted).
        self._sent_samples = 0
        self._speech_end = None
        # The audio this service has passed down to the VAD analyzer, ever: the
        # analyzer's own count (THE CALLER'S VOICE). Against the sent count it says
        # whether audio went down unsent (_hint_gap): the difference of the two at the
        # caller's last start is the baseline, and where the passed count stood then.
        self._passed_samples = 0
        self._unsent_at_start = 0
        self._passed_at_start = 0
        # CARRY THE TAIL: the last _CARRY_MS of audio sent (as sent, after the makeup
        # gain), ending at _sent_samples; the sent position no carry reaches back past
        # (the last commit, or the session's start); and the wire's lock, with the task
        # holding it (THE WIRE, _on_wire).
        self._tail = bytearray()
        self._carry_floor = 0
        self._wire = asyncio.Lock()
        self._wire_owner: Optional[asyncio.Task] = None
        # THE CALLER'S VOICE. The session's VAD analyzer, read for where it last heard
        # the caller (speech_onset.VoiceMarks; set by build_agent_session, None: the
        # verdict commits at once). The stop's silence window. Whether the marks have
        # been turned off for the session (counts that do not line up, or marks that
        # cannot be read). The position a catch-up last fell short of, for the next
        # verdict to check the analyzer has reached since (None: nothing to check),
        # and the slow-catch-up log's rate limit. And the run of commits that hit the
        # pause's bound, which stands the wait down for the session (_VOICE_STRIKES).
        self.vad_analyzer = None
        self._stop_secs = 0.0
        self._marks_unaligned = False
        self._behind = None
        self._slow_logged_at = None
        self._slow_since_log = 0
        self._pause_capouts = 0
        self._pause_off = False
        # The engine's transcription report (SPEECH_DECODED): _sent_samples when the
        # engine session opened (the report counts from there), how far it has
        # transcribed on our clock (None: no report yet, or none offered), and the VAD
        # stop the strategy was last told about, with when that stop came (for the line).
        self._session_origin = 0
        self._decoded_through = None
        self._decoded_stop = 0
        self._stop_at = 0.0
        # Whether to ask the engine for the report (SPEECH_DECODED; set by
        # build_agent_session), and whether this engine session gives it.
        self.transcription_progress = False
        self._reports = False
        # Commits sent and not yet answered, oldest first. The engine answers every
        # final commit with exactly one done, in order (voxtral_websocket.c: one
        # worker, commits are a counter it drains in turn), so the done at the head of
        # the wire answers the commit at the head of this queue -- UNLESS the engine
        # closed a segment on its own in between, whose done is indistinguishable from
        # a commit's answer on an engine that does not mark its dones, and takes the
        # commit's place here. That is the one pairing the queue alone cannot get
        # right; SttCommit.tla's unmarked rows are its counterexample, and the
        # "reason" field the engine sends since the trailing-silence credit is the
        # marker that closes it (_handle_message). The marker is trusted: a done
        # marked "commit" with nothing queued is logged (it cannot happen on a
        # correct engine), but an engine that labelled a commit's answer "vad"
        # would leave that commit's stamp queued for the NEXT done to inherit, and
        # nothing on this side can tell -- that direction is the engine's to keep.
        self._commits: deque = deque()
        # The stamp of the last commit sent, for a wordless done nothing asked for --
        # see _handle_message.
        self._last_commit_n = 0
        # VADUserStoppedSpeakingFrames seen this session: what a done's stamp counts
        # (see FinalTranscriptionFrame). The stop strategy counts the same frames, in
        # its own time.
        self._vad_stops = 0
        # Latches the one debug line if the backstop cannot arm at all (no TaskManager).
        self._stranded_unavailable = False
        # The session is ending (stop/cancel): a disconnect then tells nothing downstream
        # that a segment was dropped -- there is no next turn to mislead.
        self._ending = False
        # _receive_task is created only on a SUCCESSFUL connect; initialize it here
        # so a failed/503 connect doesn't AttributeError during _disconnect teardown.
        self._receive_task = None
        # STT engine reachability: None = not yet attempted, True = session open,
        # False = unavailable (e.g. the single-session engine returned 503). The
        # brain reads stt_available to warn the user it cannot hear.
        self._stt_available = None
        # WHY the last connect failed, when it did: True = the engine REJECTED us (its
        # one session is taken), False = we could not reach it at all. None until a
        # failure resolves it. The brain speaks a different line for each — telling a
        # caller the assistant is "busy with another session" when the engine is simply
        # down sends them to the wrong remedy entirely.
        self._slot_busy = None

    @property
    def commit_on(self) -> str:
        """What forces the final: "verdict", or "vad-stop" while the session-wide
        no-verdict fallback is in force (the commit note above _VAD_STOP_COMMITS)."""
        return self._commit_on

    @property
    def slices_asked(self) -> bool:
        """Whether this service asks the engine for sliced turn text (TURN TEXT); each
        engine session's created says whether it gets it."""
        return self._slice_asked

    @property
    def stt_available(self):
        """None until a connect is attempted; True if the engine session is open;
        False if the engine was unavailable (no free session / 503 / refused)."""
        return self._stt_available

    @property
    def slot_busy(self):
        """True if the last failure was the engine REJECTING us (503 — its single
        session is held), False if the engine was unreachable, None if no connect has
        failed. Only meaningful when stt_available is False."""
        return self._slot_busy

    def can_generate_metrics(self) -> bool:
        """True: we start processing metrics in run_stt and stop them on the
        final transcript (mirrors GladiaSTTService)."""
        return True

    # ---- lifecycle ---------------------------------------------------------

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        # Before super(): pipecat's WebsocketSTTService.stop() disconnects on its own,
        # and that disconnect must already know the session is ending (_ending).
        self._ending = True
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        self._ending = True   # before super(), for the same reason as stop()
        await super().cancel(frame)
        await self._disconnect()

    async def hold(self):
        """Let the engine's one session go with the pipeline still running: a
        conversation put on hold for a phone call (AgentSession.hold). Nothing reconnects
        it (the disconnect flag stops the base class's reconnect) until resume()."""
        logger.info(f"{self}: on hold — closing the engine session")
        await self._disconnect()

    async def resume(self):
        """Take an engine session again after hold(). stt_available goes back to None
        and resolves as at the start, so the session can say whether it hears again."""
        self._stt_available = None
        self._slot_busy = None
        logger.info(f"{self}: back from hold — reconnecting to the engine")
        await self._connect()

    # ---- audio in ----------------------------------------------------------

    def _apply_makeup(self, audio: bytes) -> bytes:
        """Scale PCM16 by the makeup gain, hard-clipping to int16 range. No-op at 0 dB.

        Int32 intermediate so the multiply cannot wrap before the clip. An odd trailing
        byte (never seen from a 20 ms frame, but the contract is bytes) is left as-is."""
        if self._makeup_scale == 1.0 or len(audio) < 2:
            return audio
        n = len(audio) // 2
        x = np.frombuffer(audio, dtype="<i2", count=n).astype(np.int32)
        x = np.clip(np.rint(x * self._makeup_scale), -32768, 32767).astype("<i2")
        tail = audio[2 * n:]
        return x.tobytes() + tail

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Optional[Frame], None]:
        """Forward one chunk of PCM16 audio to the engine. Transcripts arrive
        asynchronously via _receive_messages, so we just yield None here.

        Metrics: processing metrics are started here per chunk (each call
        restarts the clock, so the reported span is last audio chunk -> final
        transcript) and stopped on the final (mirrors GladiaSTTService)."""
        await self.start_processing_metrics()
        audio = self._apply_makeup(audio)
        self._seg_bytes += len(audio)
        # Under the wire's lock (CARRY THE TAIL: THE WIRE): a commit's carry follows it
        # with no append between, and a commit is placed with this chunk on the send
        # clock or not, never half-way. Free unless a timer's commit is draining.
        async with self._on_wire():
            if self._websocket:
                payload = base64.b64encode(audio).decode("ascii")
                try:
                    await self._websocket.send(
                        json.dumps({"type": "input_audio_buffer.append", "audio": payload})
                    )
                    # On the wire: the hint's clock advances (see _hint_gap for the paths
                    # that leave audio behind), and the tail a commit carries with it.
                    self._sent_samples += len(audio) // 2
                    self._keep_tail(audio)
                    if self._streaming and not self._gen_started:
                        # One commit for the whole call, once there is audio to work on:
                        # this STARTS generation. Everything after it streams.
                        self._gen_started = True
                        await self._websocket.send(json.dumps(
                            {"type": "input_audio_buffer.commit", "final": False}))
                        logger.info(f"{self}: streaming backend — generation started")
                except (websockets.exceptions.ConnectionClosed, OSError) as e:
                    # Connection died under us — drop the chunk. The receive task
                    # owns reconnection; raising here would kill the pipeline task.
                    logger.warning(f"{self}: audio chunk dropped, send failed: {e}")
        yield None

    @contextlib.asynccontextmanager
    async def _on_wire(self):
        """Hold the wire's lock (CARRY THE TAIL: THE WIRE). Re-entered by the task that
        holds it: a timer takes it before the check that decides its commit, and
        _send_commit takes it again."""
        task = asyncio.current_task()
        if self._wire_owner is task:
            yield
            return
        async with self._wire:
            self._wire_owner = task
            try:
                yield
            finally:
                self._wire_owner = None

    def _fired(self, attr: str) -> bool:
        """For a timer that has just taken the wire: whether its slot (`attr`) still
        holds it, emptied now if so. Until then the slot is the timer's, so a VAD start
        or a commit that comes first cancels it there and a second verdict sees the
        wait still running; one that has replaced it has cancelled it already."""
        if getattr(self, attr) is not asyncio.current_task():
            return False
        setattr(self, attr, None)
        return True

    def _keep_tail(self, audio: bytes):
        """Keep the last _CARRY_MS of audio sent, for the next commit to carry. Not in
        slice mode: no segment opens behind a commit (TURN TEXT)."""
        if not _CARRY_MS or self._streaming or self._slicing:
            return
        self._tail += audio
        keep = 2 * (_CARRY_MS * (self.sample_rate or 16000) // 1000)
        if len(self._tail) > keep:
            del self._tail[:len(self._tail) - keep]

    @property
    def _hint_gap(self) -> bool:
        """Whether audio has gone down to the VAD without reaching the engine since the
        caller last started speaking.

        The base class can buffer a frame for a reconnect, drop it muted or unusable,
        or hand it to run_stt, which finds no socket or a dead one. In every case the
        frame still goes on downstream to the VAD. Audio the VAD heard and the engine
        never got is the one thing that puts Silero's speech end EARLIER on the send
        clock than it really was, i.e. the hint's over-claim direction (see
        STT_SILENCE_HINT). The two counts say it directly: what was passed down less
        what was sent, against the same difference at the caller's last start."""
        return self._passed_samples - self._sent_samples != self._unsent_at_start

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if (isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM
                and self._audio_passthrough):
            # The base class pushes this frame on down to the VAD analyzer, sent or not:
            # the passed count is exactly what the analyzer receives (THE CALLER'S
            # VOICE). Counted here, once per frame and before the push, and not in
            # process_audio_frame, which a reconnect's replay runs a second time for
            # the frames it buffered.
            self._passed_samples += len(frame.audio) // 2
        await super().process_frame(frame, direction)
        # Commit (force a final) off the VAD's end-of-speech, or off the verdict that
        # answers it — NOT on UserStoppedSpeakingFrame. With the Smart Turn turn
        # strategy that turn-stop frame only fires AFTER the turn is judged complete,
        # which itself waits for the final transcript — a deadlock that left
        # finalization to the engine's ~15s buffer auto-commit (multi-second
        # endpointing lag). The VERDICT does not wait on the final: the strategy runs
        # the model at the VAD stop and answers with a TurnVerdictFrame either way, and
        # its silence ceiling answers again when an INCOMPLETE one falls through. So
        # the chain is VAD stop -> verdict -> commit -> final -> turn stop, with no
        # cycle in it.
        if isinstance(frame, (VADUserStoppedSpeakingFrame, VADUserStartedSpeakingFrame,
                              TurnVerdictFrame)):
            # Every commit is decided under the wire's lock, the timers' as well as
            # these (CARRY THE TAIL: THE WIRE): a decision taken outside it could queue
            # behind a timer's and go out after it, a second commit for one stop.
            async with self._on_wire():
                await self._turn_edge(frame)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False

    async def _turn_edge(self, frame: Frame):
        """The VAD's stop and start and the verdict, as they reach the STT: what each
        does to the open segment. Called under the wire's lock."""
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            # Counted before anything here can commit, so a commit sent at this stop
            # is stamped with it.
            self._vad_stops += 1
            # Where the caller's speech ended, on the clock of what has been sent.
            # Before anything here can commit, so a commit sent at this stop can carry
            # it. Not placed if audio passed this service unsent: then the commit
            # carries no hint.
            self._stop_secs = frame.stop_secs
            self._speech_end = None if self._hint_gap else self._place_speech_end()
            # The engine may be past it already (SPEECH_DECODED) -- or, with no speech
            # end to pass, no report can speak for this stop: the window decides it.
            self._stop_at = time.monotonic()
            if self._reports and self._speech_end is None:
                await self.push_frame(NoSpeechReportFrame(stop_n=self._vad_stops))
            await self._push_if_speech_decoded()
            # The VAD stop came, so the backstop's premise (that it never would) is
            # gone whichever path commits.
            await self._cancel_stranded_commit()
            # A verdict arriving from here on answers this stop, not the last expiry.
            self._expired_at = None
            if self._commit_on == "vad-stop":
                # The no-verdict fallback (the commit note above _VAD_STOP_COMMITS).
                await self._send_commit(final=True, why="vad-stop")
            elif self._bot_speaking:
                # The flush is the barge-in; see the commit note above _VAD_STOP_COMMITS.
                await self._send_commit(final=True, why="bot-speaking")
            elif self._closed_unasked and not self._seg_interims:
                # The engine closed this utterance's segment on its own before the VAD
                # stop, and nothing has streamed since: what is open is the tail after
                # its cut. A hold keeps nothing whole here (the engine has split
                # already), and holding means the ceiling's verdict commits a second
                # of silence into the reply's first second -- a finish() decode on the
                # GPU the TTS is using, logged as a wordless final. Close it now, as
                # the commit-at-the-stop design did; a tail with words in it (the
                # engine's deltas lag) comes back as its own final either way. Not
                # skipped: nothing here can tell a silent tail from one whose words
                # have not streamed yet, and a commit loses neither.
                await self._send_commit(final=True, why="tail")
            else:
                self._commit_pending = True
                self._verdict_seen = False
                await self._arm_hold_expiry()
                if self._slicing and self._peeks:
                    await self._peek_at_stop()
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            # A new utterance, or the rest of one: whatever the engine closed before it
            # is not this one's.
            self._closed_unasked = False
            # Nor is the last stop's speech end: the caller is talking again, and the
            # next stop places its own. A gap before this start is behind the speech
            # end that stop will name, so it is forgiven here; and the analyzer's stop
            # from before it is not the next stop's (_place_speech_end).
            self._speech_end = None
            self._unsent_at_start = self._passed_samples - self._sent_samples
            self._passed_at_start = self._passed_samples
            # The caller resumed: the stop's peek is no one's (THE PEEK).
            self._drop_peek()
            if self._hold_task is not None and self._hold_kind == "pause":
                # The wait for the caller's pause ends the way it should: they did
                # resume. It sends no commit, so it breaks the run of capped commits
                # here (_count_capout).
                self._pause_capouts = 0
            # The caller resumed inside a held segment (or before its verdict landed):
            # their words join it, and the next VAD stop asks the model again. Nothing
            # is owed for this stop any more.
            if self._commit_pending:
                logger.debug(f"{self}: caller resumed -- the held segment stays open")
                self._commit_pending = False
                await self._cancel_hold_expiry()
                # The hold stood the backstop down; the segment's words are back in its
                # care. Their own deltas would re-arm it -- unless the resumed speech
                # decodes to nothing (over the bot, teagram-engine#7) AND its stop is
                # missed, which is the one shape that would strand words the old
                # commit-at-the-stop had already banked. brain/formal/SttCommit.tla
                # (NoStrandedSegment) is where that shape was found.
                if self._interim_buffer.strip():
                    await self._arm_stranded_commit()
        elif isinstance(frame, TurnVerdictFrame):
            await self._handle_verdict(frame)

    async def _handle_verdict(self, frame: TurnVerdictFrame):
        """Act on Smart Turn's answer to the VAD stop this segment is waiting on."""
        self._verdicts += 1
        if self._commit_on_downgraded:
            # The wiring answers after all: the expiry that gave up on it had merely
            # beaten a slow first verdict. Back onto the verdict from here.
            self._commit_on_downgraded = False
            self._commit_on = "verdict"
            logger.info(f"{self}: a verdict ({frame.source}) has reached the STT after "
                        "all -- the commit follows the verdict from here on")
        if frame.stop_n != self._vad_stops:
            # Another stop's: the stop before, whose verdict the VAD overtook with this
            # one (the strategy's queue a whole utterance behind -- see
            # TurnVerdictFrame). Its COMPLETE is no answer for the segment held now,
            # which the model has yet to judge; that verdict is still on its way.
            logger.debug(f"{self}: verdict {frame.source} complete={frame.complete} for "
                         f"VAD stop {frame.stop_n}, not {self._vad_stops}; ignored")
            return
        if not self._commit_pending:
            if self._expired_at is not None:
                # It answers the stop whose hold has just expired: late, not lost. The
                # commit went out on time; only the expiry's blame was misplaced.
                late = time.monotonic() - self._expired_at
                self._expired_at = None
                logger.warning(
                    f"{self}: verdict {frame.source} arrived {late:.2f}s after the hold "
                    f"expired -- late, not lost: the expiry's slack ({_HOLD_SLACK_SECS}s "
                    "past the ceiling) did not cover the aggregator's queue here")
                return
            # Nothing owed: the no-verdict fallback, a stop the bot's speech flushed, a
            # caller who resumed before the verdict landed, or a verdict for a stop
            # this segment never saw (a reconnect in between).
            logger.trace(f"{self}: verdict {frame.source} complete={frame.complete} "
                         "with no commit pending; ignored")
            return
        if self._hold_task is not None and self._hold_kind == "pause":
            # A complete verdict is already waiting for the caller's pause, and a
            # second one answers the same stop: the wait it started stands, bound
            # included.
            logger.trace(f"{self}: verdict {frame.source} while the boundary waits for "
                         "the caller's pause; ignored")
            return
        self._verdict_seen = True
        if frame.complete:
            await self._commit_at_pause(frame.source)
        else:
            logger.debug(f"{self}: verdict INCOMPLETE -- holding the segment open for "
                         f"the ceiling ({self.smartturn_stop_secs}s) or the caller")

    # ---- the caller's voice ----------------------------------------------------
    # See THE CALLER'S VOICE (issue #140): a complete verdict draws the segment
    # boundary only once the analyzer has heard the audio sent so far and found no
    # voice since the stop, or found the caller paused again. Held until then in the
    # hold's own slot, so a VAD start, a disconnect or another path's commit ends the
    # wait exactly as it ends a hold.

    def _voice_marks(self):
        """The analyzer's VoiceMarks, or None when there are none to go on: no
        analyzer that keeps them, marks turned off for the session, or marks that
        cannot be read. The one place they are read, so every reader -- the hint, the
        speech end, the wait -- falls back the same way when they cannot be."""
        marks = getattr(self.vad_analyzer, "voice_marks", None)
        if marks is None or self._marks_unaligned:
            return None
        try:
            w = marks()
        except Exception as e:  # noqa: BLE001 -- a broken side channel must not hold a commit
            self._marks_off(f"reading the VAD analyzer's marks failed ({e!r})")
            return None
        if w.received > self._passed_samples:
            # The analyzer has been handed more audio than this service passed it:
            # something else feeds it, or its count started elsewhere.
            self._marks_off(f"the VAD analyzer has heard {w.received} samples and this "
                            f"service passed it {self._passed_samples}: the counts "
                            "disagree")
            return None
        return w

    def _marks_off(self, why: str):
        """Stop reading the marks for the session, and say so once."""
        self._marks_unaligned = True
        logger.warning(f"{self}: {why} -- commits stop reading the caller's voice for "
                       "this session (THE CALLER'S VOICE)")

    @staticmethod
    def _voice_since_stop(w, onset: bool = False) -> Optional[int]:
        """Where the voice heard since the VAD's latest stop ended, on the passed count,
        or None if the analyzer has heard none since, or has not seen a stop at all.
        `onset`: only voice long enough to be an onset (the wait's test); otherwise
        any voiced chunk (the hint's).

        The stop is where the analyzer saw it trip, which is the stop this service is
        answering: a later one could only follow a VAD start, which ends the hold.
        The one stop the analyzer never sees is the one its controller forces when
        audio stops arriving mid-speech. After an earlier stop, the voice before the
        forced one reads as voice since the analyzer's last stop, and the commit waits
        out the bound, which is a fair answer to a client that went quiet mid-word.
        On a session's first utterance there is no earlier stop: nothing reads as
        voice since, and the verdict commits at once, as it would without the marks."""
        end = w.onset_end if onset else w.voice_end
        if end is None or w.stop_at is None or end <= w.stop_at:
            return None
        return end

    def _place_speech_end(self) -> int:
        """Where the speech this VAD stop ends stopped, on the send clock. The
        analyzer's own speech end for its stop, carried across from the passed count,
        when it has one for this utterance: exact, where the stop's window back from
        here lands late by whatever was in transit between the two. Not when its last
        stop came before the caller's latest start: then the stop in hand is one the
        analyzer never saw (forced when the audio stopped arriving), and its speech end
        is the previous utterance's, which would claim this whole utterance as silence.
        Otherwise that window back from here, which errs the safe way
        (STT_SILENCE_HINT). Called with no gap since the caller's start, so the two
        counts have moved together."""
        w = self._voice_marks()
        if (w is not None and w.speech_end is not None and w.stop_at is not None
                and w.stop_at > self._passed_at_start):
            return self._sent_samples - (self._passed_samples - w.speech_end)
        rate = self.sample_rate or 16000
        return self._sent_samples - round(self._stop_secs * rate)

    def _boundary_wait(self, w, heard_through: int) -> Optional[str]:
        """Why the boundary must not be drawn yet: "unheard" while the analyzer has not
        been handed the audio up to `heard_through`; "voice" while an onset since the
        stop has not paused (the onset test's own offset point), or while a run of
        voice since the stop is still too short to say whether it is one. None: draw
        it."""
        if w.received < heard_through:
            return "unheard"
        if self._voice_since_stop(w, onset=True) is not None and not w.paused:
            return "voice"
        if w.in_run and self._voice_since_stop(w) is not None:
            return "voice"
        return None

    def _check_caught_up(self, w):
        """The last catch-up fell short of `_behind`: by this verdict, a turn later, a
        slow analyzer has got there. One that has not is not hearing the frames this
        service passes it, and the marks go off. Returns the marks still to use."""
        if w is None or self._behind is None:
            return w
        if w.received >= self._behind:
            self._behind = None
            return w
        self._marks_off(f"the VAD analyzer has still not heard the audio sent at an "
                        f"earlier verdict ({w.received} of {self._behind} samples): the "
                        "counts disagree")
        return None

    def _note_slow(self, short_ms: int, waited: float):
        """A catch-up that missed its bound: logged at most once a _SLOW_LOG_SECS, with
        how many there were since, since a busy box can miss it on many commits."""
        self._slow_since_log += 1
        now = time.monotonic()
        if self._slow_logged_at is not None and now - self._slow_logged_at < _SLOW_LOG_SECS:
            return
        logger.warning(f"{self}: the VAD analyzer is still {short_ms}ms short of the audio "
                       f"sent at the verdict after {waited:.2f}s -- drawing the boundary "
                       f"on what it has heard ({self._slow_since_log} such commit(s) since "
                       "the last such line)")
        self._slow_logged_at = now
        self._slow_since_log = 0

    def _count_capout(self, capped: bool):
        """The run of verdict commits that hit the pause's bound (_VOICE_STRIKES): any
        other commit answering a verdict breaks it; the third running stands the wait
        down for the session. Only the commits that answer a complete verdict count,
        either way: with a TV in the room every one of those caps, and a commit of
        another kind between them -- the flush of a stop over the bot's reply, the
        backstop, a tail, an expired hold -- says nothing about the noise."""
        if not capped:
            self._pause_capouts = 0
            return
        self._pause_capouts += 1
        if self._pause_capouts >= _VOICE_STRIKES and not self._pause_off:
            self._pause_off = True
            logger.warning(
                f"{self}: the caller's voice has not paused within "
                f"{self.smartturn_stop_secs}s of the verdict on {self._pause_capouts} "
                "commits running -- steady noise at speech confidence, most likely; "
                "commits stop waiting for the caller's pause for this session")

    async def _commit_at_pause(self, why: str):
        """Commit a complete verdict's segment now if nothing says the caller is
        speaking, otherwise hold it for _wait_for_pause. In slice mode, now: a commit
        damages nothing there, and words the caller resumed with end after it, in the
        next turn (TURN TEXT)."""
        if self._slicing:
            await self._send_commit(final=True, why=why)
            return
        w = self._check_caught_up(self._voice_marks())
        if self._pause_off:
            w = None
        wait = None if w is None else self._boundary_wait(w, self._passed_samples)
        if wait is None:
            self._count_capout(False)
            await self._send_commit(final=True, why=why)
            return
        self._hold_kind = "pause"
        err = await self._restart_timer("_hold_task",
                                        self._wait_for_pause(why, self._passed_samples))
        if err is not None:
            logger.warning(f"{self}: cannot wait for the caller's pause ({err}); "
                           "committing now")
            self._count_capout(False)
            await self._send_commit(final=True, why=why)

    async def _wait_for_pause(self, why: str, heard_through: int):
        start = time.monotonic()
        voiced = capped = False
        try:
            voiced, capped = await self._until_pause(why, heard_through, start)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 -- the segment must still close
            logger.warning(f"{self}: the wait for the caller's pause failed ({e!r}); "
                           "committing the held segment")
        # A timer beside run_stt and the input task: from the check to the commit under
        # the wire's lock (CARRY THE TAIL: THE WIRE), and its slot given up only there.
        async with self._on_wire():
            if not self._fired("_hold_task") or not self._commit_pending:
                return
            self._count_capout(capped)
            await self._send_commit(final=True, why=why, waited=time.monotonic() - start,
                                    for_voice=voiced)

    async def _until_pause(self, why: str, heard_through: int, start: float):
        """Wait, woken by the analyzer, until the boundary may be drawn or a bound is
        hit. Returns whether it waited on the caller's voice, and whether it ended at
        the pause's bound."""
        heard = self.vad_analyzer.voice_heard
        voiced = False
        caught_up = False
        while True:
            heard.clear()
            w = self._voice_marks()
            if w is None:
                return voiced, False
            waited = time.monotonic() - start
            wait = self._boundary_wait(w, heard_through)
            if wait == "unheard" and waited >= _VOICE_CATCHUP_SECS:
                # Missed its bound: decide on what has been heard, and have the next
                # verdict check the analyzer gets there (_check_caught_up).
                self._behind = heard_through
                self._note_slow((heard_through - w.received) * 1000
                                // (self.sample_rate or 16000), waited)
                heard_through = w.received
                caught_up = True
                wait = self._boundary_wait(w, heard_through)
            elif wait != "unheard":
                caught_up = True
            if wait is None:
                return voiced, False
            if wait == "voice" and not voiced:
                voiced = True
                logger.debug(f"{self}: verdict {why} with the caller's voice heard since "
                             "the stop -- the segment boundary waits for their pause")
            bound = self.smartturn_stop_secs if caught_up else _VOICE_CATCHUP_SECS
            if waited >= self.smartturn_stop_secs:
                # The hint claims nothing here: the voice is current (_claim).
                logger.warning(f"{self}: the caller's voice has not paused "
                               f"{self.smartturn_stop_secs}s after the verdict -- "
                               "committing over it")
                return voiced, True
            try:
                await asyncio.wait_for(heard.wait(), timeout=max(0.0, bound - waited))
            except asyncio.TimeoutError:
                pass

    # ---- the hold's expiry ---------------------------------------------------
    # See the commit note above _VAD_STOP_COMMITS: a segment held on an INCOMPLETE
    # verdict must still close if the ceiling's verdict never reaches us. Armed at the
    # VAD stop (not at the verdict, so a model verdict that never arrives is covered
    # too), cancelled by the commit, by a VAD start and by a disconnect -- not by a
    # done the engine sends on its own, which may be the previous utterance's (see
    # _handle_message). Same timer discipline as the backstop below.

    async def _arm_hold_expiry(self):
        self._hold_kind = "expiry"
        err = await self._restart_timer("_hold_task", self._commit_when_hold_expires())
        if err is not None:
            logger.warning(f"{self}: cannot arm the hold's expiry ({err}); committing at "
                           "the VAD stop instead")
            await self._send_commit(final=True, why="vad-stop")

    async def _cancel_hold_expiry(self):
        # Whichever the slot holds: the expiry, or the wait for the caller's pause.
        await self._cancel_timer("_hold_task")

    async def _commit_when_hold_expires(self):
        await asyncio.sleep(self.smartturn_stop_secs + _HOLD_SLACK_SECS)
        # A timer (CARRY THE TAIL: THE WIRE, as in _wait_for_pause).
        async with self._on_wire():
            if self._fired("_hold_task"):
                await self._commit_expired_hold()

    async def _commit_expired_hold(self):
        """The expiry's check and commit, under the wire's lock."""
        if not self._commit_pending:
            return
        wait = f"{self.smartturn_stop_secs + _HOLD_SLACK_SECS:.1f}s"
        now = "committing the held segment so the turn is answered late rather than never"
        # "late or lost": from here the two look the same -- a verdict still queued
        # behind the aggregator's audio, or one that will never come. If it was late,
        # its arrival is logged against this expiry (_handle_verdict), and that line is
        # the one to size _HOLD_SLACK_SECS from.
        if self._verdict_seen:
            logger.warning(f"{self}: hold expired -- the ceiling's verdict has not reached "
                           f"the STT {wait} after a VAD stop the model called INCOMPLETE "
                           f"(late or lost); {now}")
        elif self._verdicts:
            logger.warning(f"{self}: hold expired -- no verdict reached the STT {wait} "
                           f"after the VAD stop, though earlier ones did (late or lost); "
                           f"{now}")
        else:
            # Not one verdict all session: either nothing upstream answers VAD stops
            # (not the brain's LateStartTurnStopStrategy, or its model raises before
            # the push), or the first inference was slower than this wait. Holding
            # every turn for it would be the worst of both modes, so stop holding --
            # until a verdict arrives, which is what tells the two apart
            # (_handle_verdict switches back).
            logger.warning(f"{self}: hold expired -- no verdict has reached the STT this "
                           f"session, {wait} after its first held VAD stop; committing at "
                           f"the VAD stop until one does, and {now}")
            self._commit_on = "vad-stop"
            self._commit_on_downgraded = True
        self._expired_at = time.monotonic()
        await self._send_commit(final=True, why="hold-expired")

    # ---- one-shot timers ---------------------------------------------------
    # The stranded-segment backstop and the streaming segment boundary are both
    # resettable one-shot timers, and both go through the two helpers here so the rules
    # each one learned the hard way are written once:
    #
    #   * cancel_task is `async def` on BaseObject and is AWAITED. Called bare it returns
    #     an un-awaited coroutine that cancels NOTHING: every re-arm left the previous
    #     timer running, so a segment that streamed N deltas accumulated N live timers
    #     and they all fired at once (live 2026-09-09 20:50:57.958 — six identical
    #     commits in the same millisecond). Each one forces a final on the engine, and a
    #     stale commit landing inside the NEXT utterance closes it early, which is a
    #     fragment. It also inflated every backstop count measured that day. The only
    #     visible symptom was one RuntimeWarning line: "coroutine
    #     'BaseObject.cancel_task' was never awaited".
    #   * create_task needs the TaskManager that setup() installs, so it can raise when
    #     the caller runs outside a started pipeline. A timer that cannot arm must never
    #     take the transcript path down with it — the backstop is armed inside
    #     _handle_message on every delta, and raising there kills the receive loop and
    #     with it ALL transcription, trading a rare lost turn for a call that hears
    #     nothing. So the failure is handed back to the caller to decide, and the
    #     un-awaited coroutine is closed rather than left to warn on every delta.

    async def _restart_timer(self, attr: str, coro) -> Optional[Exception]:
        """Cancel the timer task held in `self.<attr>`, if any, and start `coro` in its
        place. Returns the exception when no task could be created (coro closed)."""
        await self._cancel_timer(attr)
        try:
            setattr(self, attr, self.create_task(coro))
        except Exception as e:
            coro.close()
            return e
        return None

    async def _cancel_timer(self, attr: str) -> None:
        task = getattr(self, attr)
        setattr(self, attr, None)
        if task is not None:
            await self.cancel_task(task)

    # ---- stranded-segment backstop -----------------------------------------
    # See _STRANDED_INTERIM_SECS for why this exists and why it is armed off interim
    # activity rather than off any turn frame.

    async def _arm_stranded_commit(self):
        """(Re)start the quiet timer for the segment currently streaming interims."""
        if self._commit_pending:
            # The VAD stop this guards against missing HAS come; the segment is held
            # for its verdict on purpose. Re-arming off the engine's lagging deltas
            # would fire a second commit into the ceiling's, closing the next
            # utterance early -- the exact fragment the hold exists to prevent.
            return
        err = await self._restart_timer("_stranded_task",
                                        self._stranded_commit_after_quiet())
        if err is not None and not self._stranded_unavailable:
            self._stranded_unavailable = True
            logger.debug(f"{self}: stranded-segment backstop unavailable ({err}); "
                         "a missed VAD stop will lose its turn")

    async def _cancel_stranded_commit(self):
        await self._cancel_timer("_stranded_task")

    async def _stranded_commit_after_quiet(self):
        await asyncio.sleep(_STRANDED_INTERIM_SECS)
        # A timer (CARRY THE TAIL: THE WIRE, as in _wait_for_pause).
        async with self._on_wire():
            if self._fired("_stranded_task"):
                await self._commit_stranded()

    async def _commit_stranded(self):
        """The backstop's check and commit, under the wire's lock."""
        if not self._interim_buffer.strip():
            return  # the segment closed on its own; nothing stranded
        if self._commit_pending:
            return  # the VAD stop came after all; the verdict path owns the close
        # KNOWN EDGE, left as-is deliberately: this fires 1.5 s after the last interim,
        # so an engine that STALLS its interims mid-utterance (STT-vs-TTS GPU contention
        # has lagged them past 1.5 s during long replies) gets a forced commit while the
        # user is still talking, cutting the phrase. The obvious guard -- don't fire
        # while VAD reports SPEAKING -- would disable this backstop in the very case it
        # exists for: a MISSED VAD stop leaves VAD stuck in SPEAKING. The two failure
        # modes route through the same timer, so the guard trades one for the other. The
        # 1.5 s wait is the compromise; revisit with the GPU-contention work, not alone.
        # WARNING, not debug: reaching here means the VAD stop that should have closed
        # this segment never arrived, which is a fault in the audio/VAD path that this
        # only papers over. Silence here would hide it exactly as it was hidden before.
        logger.warning(
            f"{self}: no final after {_STRANDED_INTERIM_SECS}s of interim quiet — the "
            f"VAD stop never came; committing its {len(self._interim_buffer.strip())} "
            "chars myself so the turn is answered late rather than lost"
        )
        if self._last_drop is not None:
            # The countable shape of a cut too close to the speech end (THE CUT'S
            # MARGIN): the last turn's final dropped words, and the open words they may
            # be are now committed as a turn of their own.
            n, dropped = self._last_drop
            self._drop_backstops += 1
            logger.info(f"[TURN] VAD stop {n}'s final dropped {list(dropped)}, and the "
                        f"backstop now commits the open words as a turn of their own "
                        f"(dropped-then-backstop={self._drop_backstops})")
        await self._send_commit(final=True, why="backstop")

    def _log_segment(self, msg: dict, commit: Optional[_Commit]) -> None:
        """One line per segment, at the seam between what we sent and what came back.
        `commit` is the one this done answers, or None when the engine closed the
        segment on its own.

        This is the BOUNDARY line: everything left of it is the brain's doing, everything
        right of it is the engine's. Live 2026-09-10 05:57:44 the caller spoke for 1.2 s
        with Silero at conf 0.89 against a 0.15 gate and volume 0.75 -- a full
        QUIET->STARTING->SPEAKING->STOPPING->QUIET cycle -- and the pipeline produced no
        transcript at all; they waited ~2.5 s, tried again, and the retry transcribed in
        197 ms. Nothing in the log said whether the brain failed to commit that audio or
        the engine returned nothing for it, so the fault could not be placed.

        Now it can: bytes appended, interims received, what triggered the commit and how
        long the engine took to answer it. `EMPTY` means the engine was handed audio and
        returned no words -- that is an ENGINE-side result, and this line is the evidence
        to hand over with it. `no-commit` means the done arrived without us asking, i.e.
        the engine closed the segment on its own.
        """
        # `or 16000`, not `max(..., 1)`: sample_rate is 0 until the StartFrame sets it,
        # and dividing by 1 turns a one-second segment into "16000.00s audio" — a line
        # that is worse than no line, because it reads as a fault in itself.
        secs = self._seg_bytes / 2 / (self.sample_rate or 16000)
        engine_text = (msg.get("text") or "").strip()
        lat = f"{time.monotonic() - commit.at:.2f}s" if commit else "no-commit"
        why = commit.why if commit else ""
        verdict = f"{len(engine_text)} chars" if engine_text else "EMPTY"
        # What the commit claimed as trailing silence and what the engine credited
        # against its pad (an engine with teagram-engine#37 reports it): the two numbers
        # that say whether the hint is doing its work, and the second is the engine's
        # to explain when it is short of the first (its clamp to the audio it fed).
        credit = ""
        if commit and commit.hint is not None:
            credit = f", hint {commit.hint}ms"
        if msg.get("credited_ms") is not None:
            credit += f", credited {msg['credited_ms']}ms"
        # How long the boundary was held after a complete verdict, and what for: the
        # live measure of what THE CALLER'S VOICE costs, and of how often it is the
        # caller who resumed rather than the analyzer lagging.
        if commit and commit.waited is not None:
            credit += (f", held {commit.waited * 1000:.0f}ms "
                       f"{'for the caller to pause' if commit.for_voice else 'for the VAD'}")
        if commit and commit.carry_ms:
            credit += f", carried {commit.carry_ms}ms"
        # In slice mode the answer is the commit's preview, and what it carries is the
        # turn's provisional text (TURN TEXT).
        answer = "preview" if msg.get("type") == "transcription.preview" else "done"
        line = (f"{self}: segment {secs:.2f}s audio, {self._seg_interims} interims, "
                f"commit={why} -> {answer} in {lat}: {verdict}{credit}")
        # An empty final on a segment the engine DID stream interims for is the shape
        # worth noticing: it had words a moment ago and returned none.
        #
        # `secs` counts ALL audio since the last final -- run_stt streams every chunk,
        # silence included -- so it is > 0.3 on essentially every segment and cannot be
        # the warn condition on its own: it would fire "audio in, no words out" at
        # WARNING for a cough, a door, or the engine's ~15 s buffer auto-commit during
        # any quiet stretch, drowning the once-per-run empty-final counter just below
        # (which exists for exactly those). Gate the wordless warning on a commit that
        # FOLLOWED A VAD STOP instead (_VAD_STOP_COMMITS: the raw stop, or the verdict
        # that answered it): VAD reporting speech-then-silence is the only evidence
        # there was an utterance to transcribe, so an empty final there is a real miss
        # (the double-talk drop this whole surface is about); a backstop or auto-commit
        # empty is ordinary and logs at debug.
        # In slice mode a turn with words in hand is never wordless, so the interims
        # since the last line were the open turn's, which this commit does not close.
        if not engine_text and self._seg_interims and answer == "done":
            logger.warning(line + " — engine streamed interims then returned no text")
        elif not engine_text and why in _VAD_STOP_COMMITS and secs > 0.3:
            logger.warning(line + " — audio in, no words out")
        else:
            logger.debug(line)
        self._seg_bytes = 0
        self._seg_interims = 0

    async def _push_if_speech_decoded(self):
        """Tell the stop strategy, once per VAD stop, that the engine has transcribed
        past the speech end placed for it (SPEECH_DECODED)."""
        if (self._decoded_through is None or self._speech_end is None
                or self._decoded_stop == self._vad_stops):
            return
        rate = self.sample_rate or 16000
        past = self._decoded_through - self._speech_end - _REPORT_MARGIN_MS * rate // 1000
        if past < 0:
            return
        self._decoded_stop = self._vad_stops
        # INFO: with the [SPEC] start line, the live measure of the report's timing.
        logger.info(f"{self}: speech of VAD stop {self._vad_stops} transcribed through "
                    f"(+{_REPORT_MARGIN_MS} ms) {time.monotonic() - self._stop_at:.2f}s "
                    "after the stop")
        await self.push_frame(SpeechDecodedFrame(stop_n=self._vad_stops))

    def _claim(self, for_voice: bool = False) -> tuple[Optional[int], Optional[int]]:
        """What the commit going out now claims as trailing silence, in ms, and where on
        the send clock its carry starts, the next segment's start (CARRY THE TAIL).

        The claim: the audio sent since Silero's speech end, or since the caller's voice
        heard after the stop where there was some (THE CALLER'S VOICE), less the margin
        -- with the carry on, the margin giving way where it would take the claim below
        _REPRIME_MS -- or
        None when there is no placed speech end to count from, the count is not
        positive, or the hint is off (see STT_SILENCE_HINT). That voice does not hold it
        back when the carry reaches the speech end, unless the commit was held for that
        voice (`for_voice`).

        The carry: from where the claim starts, or from the bound (_CARRY_MS back, the
        previous commit, the session's start) if that is later. None when the claim is
        None, the carry is off or the backend streams, or nothing has been sent since
        the bound."""
        if not STT_SILENCE_HINT or self._speech_end is None:
            return None, None
        rate = self.sample_rate or 16000
        sent = self._sent_samples
        bound = None
        if _CARRY_MS and not self._streaming and not self._slicing:
            bound = max(sent - len(self._tail) // 2, self._carry_floor)
        end = self._speech_end
        if (bound is None or bound > end + _HINT_MARGIN_MS * rate // 1000
                or for_voice):
            w = self._voice_marks()
            voice = None if w is None else self._voice_since_stop(w)
            if voice is not None:
                # Any voiced chunk counts here, not only an onset: the hint must never
                # claim silence that has voice in it. From the passed count onto the send
                # clock: nothing has passed unsent since the caller last started
                # speaking, or the speech end would not be placed, so the two have moved
                # together since.
                end = max(end, sent - (self._passed_samples - voice))
        quiet = (sent - end) * 1000 // rate
        ms = quiet - _HINT_MARGIN_MS
        if bound is not None and quiet >= _REPRIME_MS:
            # With the carry on, the margin gives way rather than take the claim below
            # the re-prime floor (STT_SILENCE_HINT); off, it is held back whole.
            ms = max(ms, _REPRIME_MS)
        if ms <= 0:
            return None, None
        carry = None if bound is None else max(bound, end)
        return int(ms), (carry if carry is not None and carry < sent else None)

    async def _send_commit(self, final: bool = True, why: str = "other",
                           waited: Optional[float] = None, for_voice: bool = False,
                           peek: bool = False):
        """Force a final. `why` is recorded so the segment line says which path fired,
        and `waited` how long a complete verdict's commit was held for the VAD analyzer
        or, `for_voice`, for the caller's pause (THE CALLER'S VOICE). `peek`: the commit
        at a VAD stop that only asks (THE PEEK); and a commit for a stop whose peek is out
        adopts that instead of going out."""
        # The wire's lock (CARRY THE TAIL: THE WIRE): every caller holds it already,
        # from the check that decided this commit, and it is taken again here so no
        # commit can go out without it. Then the hold's timer: cancelling a live task
        # is the one await here that can actually suspend, and the bookkeeping below
        # must be atomic with the send -- a VAD stop landing between "pending = False"
        # and the commit going out would arm a hold this commit then never clears.
        # Whatever sent it, the stop this segment was waiting on is answered.
        async with self._on_wire():
            if not peek and self._peek is not None and self._peek.n == self._vad_stops:
                # The stop's peek is out: the turn closes on it, with no second commit
                # (THE PEEK).
                await self._adopt_peek(why)
                return
            if not peek:
                await self._cancel_hold_expiry()
            # The claim and where the next segment opens, at the commit's position on
            # the wire, before the speech end goes below.
            hint, carry_from = self._claim(for_voice)
            commit = _Commit(n=self._vad_stops, at=time.monotonic(), why=why, hint=hint,
                             waited=waited, for_voice=for_voice)
            # Whatever this commit claims, the next one starts from a fresh segment and
            # must place its own speech end. The backstop never has one to take: it
            # fires on a segment whose stop never came, and a stop that did come has
            # already committed (clearing this) or is holding (so the backstop stands
            # down). A peek answers nothing yet: the stop stays held for its verdict.
            if not peek:
                self._stop_answered(commit.n)
            if self._streaming:
                # Nothing to ask for: generation is already running, and sending
                # final=True here would close the stream for the rest of the CALL rather
                # than end a segment. So the boundary is drawn locally -- but NOT yet.
                #
                # The model transcribes with a deliberate delay (transcription_delay_ms
                # in tekken.json, 480 ms as shipped), so at the VAD stop the deltas for
                # the end of the utterance have not arrived. Snapshotting immediately
                # cuts the tail off every segment and hands it to the NEXT one. Live
                # 2026-09-10 22:14:
                #
                #   'Hey, can you'  /  'hear me? Hey, can you hear'  /  'me?'
                #
                # -- one question split across three turns, each carrying the previous
                # one's ending. So wait out the delay before drawing the line.
                #
                # One boundary at a time: a later commit supersedes the earlier one's
                # timer, so it takes the earlier one's place here too, or the
                # synthesized done that answers it would pop the wrong stamp for ever
                # after.
                self._commits.clear()
                self._commits.append(commit)
                await self._schedule_stream_boundary()
                return
            ws = self._websocket
            if not ws:
                return
            msg = {"type": "input_audio_buffer.commit", "final": final}
            if commit.hint is not None:
                msg["trailing_silence_ms"] = commit.hint
            # The carry is the tail as it stands at the commit's position, and no later
            # carry reaches back past this commit.
            carry = b""
            if carry_from is not None:
                carry = bytes(self._tail[len(self._tail)
                                         - 2 * (self._sent_samples - carry_from):])
            self._carry_floor = self._sent_samples
            try:
                await ws.send(json.dumps(msg))
            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                # Lost commit = lost turn (no final transcript), but raising here
                # would kill the whole pipeline. The receive task owns reconnection.
                # Not queued: no done is coming for it, and a stamp left waiting here
                # would be handed to the next done the engine sends on its own.
                logger.warning(f"{self}: commit dropped, send failed: {e}")
                return
            if self._websocket is not ws:
                # The session closed while the commit went out: the reconnect has
                # cleared the stamps the old one was owed, no done will answer this
                # one, and its carry (old audio, for an engine that no longer has the
                # segment it opens) goes with it.
                logger.warning(f"{self}: the engine session closed under a commit -- no "
                               "final will come for it, and it carries nothing")
                return
            self._commits.append(commit)
            if peek:
                self._peek = commit.peek = _Peek(at_ms=self._session_ms(), n=commit.n,
                                                 commit=commit)
            elif self._slicing:
                self._take_turn(commit)
            if carry:
                await self._send_carry(ws, commit, carry)

    def _session_ms(self) -> int:
        """The audio sent on this engine session so far, in ms: the engine's clock, which
        its positions (end_ms, through_ms, end_through_ms) are on."""
        return ((self._sent_samples - self._session_origin) * 1000
                // (self.sample_rate or 16000))

    def _stop_answered(self, n: int):
        """VAD stop `n` is answered -- its commit went out, or its peek was adopted (THE
        PEEK): the next commit places its own speech end, and nothing waits for it."""
        self._speech_end = None
        self._last_commit_n = n
        self._commit_pending = False
        self._peek_wanted = None

    def _take_turn(self, commit: _Commit):
        """The commit just sent closes a turn (TURN TEXT) at its position, on the engine's
        session clock -- the audio sent on this socket, which the commit followed with
        nothing between (THE WIRE)."""
        commit.turn = self._turn_at(self._session_ms(), commit.n, commit.at)

    def _turn_at(self, at: int, n: int, sent_at: float) -> _Turn:
        """A turn closed at `at`: it takes the open words that end before it. Words
        ending at or after it stay open: the caller had resumed by then -- unless its
        preview vouches for them (THE CUT'S MARGIN)."""
        mine = [w for w in self._open_words if w[1] < at]
        turn = _Turn(at_ms=at, stop_n=n, words=mine, since=self._seg_since if mine else None,
                     sent_at=sent_at)
        self._turns.append(turn)
        self._keep_open([w for w in self._open_words if w[1] >= at])
        if (self._last_piece is not None and self._last_piece[2] is None
                and self._last_piece[1] < at):
            self._last_piece = (*self._last_piece[:2], turn)
        # A turn has closed since the last one's final dropped words: a backstop now is
        # not that shape.
        self._last_drop = None
        return turn

    def _keep_open(self, words: list):
        """The open turn's words are now `words`."""
        self._open_words = words
        self._interim_buffer = "".join(w[0] for w in words)
        if not words:
            self._seg_since = None

    async def _peek_at_stop(self):
        """The VAD stop the hold waits on: ask its preview now (THE PEEK) -- or, with an
        answer still outstanding, once that lands."""
        if self._commits:
            self._peek_wanted = self._vad_stops
            return
        await self._send_commit(final=True, why="peek", peek=True)

    def _drop_peek(self):
        """The caller resumed: the peek is no one's, and a peek waiting to go is not
        wanted (THE PEEK)."""
        if self._peek is not None:
            self._peek.dropped = True
            self._peek = None
        self._peek_wanted = None

    async def _adopt_peek(self, why: str):
        """The turn closes on the stop's peek (THE PEEK): what a commit at `why` would do,
        with the peek's position and text. Called under the wire's lock."""
        peek, self._peek = self._peek, None
        await self._cancel_hold_expiry()
        self._stop_answered(peek.n)
        # The words that came in after its preview: the preview may vouch for them.
        late = {id(w) for w in self._open_words[peek.seen:]} if peek.previewed else set()
        peek.turn = self._turn_at(peek.at_ms, peek.n, peek.commit.at)
        peek.commit.why = f"peek+{why}"
        logger.debug(f"{self}: VAD stop {peek.n}'s peek adopted ({why}), "
                     f"{'its preview in hand' if peek.previewed else 'its preview to come'}")
        if peek.previewed:
            await self._push_turn(peek.turn, peek.asked, peek.asked_n, peek.msg,
                                  peek.commit, late=late)

    async def _push_turn(self, turn: _Turn, asked: str, asked_n: int, msg: dict,
                         commit: _Commit, late: set = frozenset()):
        """A turn goes downstream on its provisional text (TURN TEXT), stamped with its
        commit's VAD stop, and waits for its final. `late`: open words (by id) that came
        in after the preview, which it may vouch for (an adopted peek's)."""
        turn.previewed = True
        turn.asked_n = asked_n
        turn.asked = asked
        turn.preview_at = time.monotonic()
        # What the preview vouches for (THE CUT'S MARGIN): its text's words, less those
        # of the words that have come in since, which are the turn's already.
        turn.expect = [k for k in map(_word_key, (msg.get("delta") or "").split()) if k]
        follow = None
        for w in turn.words[asked_n:]:
            if not self._goes_on(w, follow):
                self._claim_expected(turn, w[0])
            follow = (w[2], w[1], True)
        if late:
            keep, follow = [], None
            for w in self._open_words:
                took = (follow[2] if self._goes_on(w, follow)
                        else id(w) in late and self._vouch(turn, *w))
                follow = (w[2], w[1], took)
                (turn.words if took else keep).append(w)
            if len(keep) < len(self._open_words):
                self._keep_open(keep)
                last = self._last_piece
                if last is not None and last[2] is None and follow[2] and follow[:2] == last[:2]:
                    self._last_piece = (*last[:2], turn)
        if msg.get("complete") is False:
            turn.deferred = "its preview cut short"
        elif (commit.why == "bot-speaking"
              and not _words_of(self._callers(asked, turn.since))):
            # Nothing of the caller's: no words, or only the bot's own heard back.
            turn.deferred = "its preview over the bot's reply having had none of the caller's"
        if turn.deferred:
            # Nothing goes on now: the turn closes on its final -- over the bot still the
            # barge-in its words are -- or, if that never comes, on the words in hand
            # (TURN TEXT).
            turn.msg, turn.commit = msg, commit
            logger.debug(f"{self}: VAD stop {turn.stop_n}: {turn.deferred} -- the turn "
                         "closes on its final")
        else:
            turn.pushed, turn.woke = await self._close(msg, commit, asked, asked,
                                                       turn.since)
        # The backstop guards the open turn's words, and a close stands it down (the
        # done's rule); words already open that no delta will re-arm it for keep it.
        await self._cancel_stranded_commit()
        if self._interim_buffer.strip():
            await self._arm_stranded_commit()
        await self._finalize_turns()

    @staticmethod
    def _goes_on(w: tuple, last: Optional[tuple]) -> bool:
        """Main piece `w` is the rest of the word `last` (audio_ms, end_ms, ...) began: no
        leading space, and the word's positions (every piece of a word carries them,
        PROTOCOL3 section 2)."""
        return (last is not None and not w[0].startswith(" ")
                and (w[2], w[1]) == tuple(last[:2]))

    @staticmethod
    def _expected(turn: _Turn, piece: str) -> Optional[str]:
        """The word of the preview's this word (its first piece) is, if any."""
        key = _word_key(piece)
        return next((k for k in turn.expect if key and k.startswith(key)), None)

    @classmethod
    def _claim_expected(cls, turn: _Turn, piece: str):
        """A word of the turn came in: the preview's vouching for it is spent."""
        k = cls._expected(turn, piece)
        if k is not None:
            turn.expect.remove(k)

    @classmethod
    def _vouch(cls, turn: _Turn, piece: str, end, start) -> bool:
        """A word past the turn's position (its first piece) is the turn's if the preview
        vouched for it, which it spends, and it ends less than _VOUCH_MS past the position
        or starts before it (THE CUT'S MARGIN)."""
        if not turn.expect or not (end < turn.at_ms + _VOUCH_MS or start < turn.at_ms):
            return False
        k = cls._expected(turn, piece)
        if k is None:
            return False
        turn.expect.remove(k)
        return True

    def _warn_once(self, key: str, line: str):
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(f"{self}: {line}")

    def _slice_delta(self, piece: str, end, start=None) -> bool:
        """File a main delta by where its word ends (TURN TEXT): into the committed turn
        it ends before -- the oldest such, the turns being in position order -- or whose
        preview vouched for it (THE CUT'S MARGIN), and say so (True), or into the open
        turn (False). The rest of a word's pieces go where its first went."""
        if not isinstance(end, (int, float)):
            # No position (not the preview stream's delta): it ends no later than the
            # audio sent so far, so the next commit takes it.
            self._warn_once("no-end", "a delta with no end_ms in a sliced session -- "
                            "placed at the audio sent so far")
            end = self._session_ms()
        if not isinstance(start, (int, float)):
            start = end
        w = (piece, end, start)
        last, self._last_piece = self._last_piece, None
        if self._goes_on(w, last) and (last[2] is None
                                       or any(t is last[2] for t in self._turns)):
            self._last_piece = last
            if last[2] is not None:
                last[2].words.append(w)
                return True
            self._open_words.append(w)
            return False
        for turn in self._turns:
            if end < turn.at_ms:
                turn.words.append(w)
                self._claim_expected(turn, piece)
                self._last_piece = (start, end, turn)
                return True
            if self._vouch(turn, piece, end, start):
                turn.words.append(w)
                self._last_piece = (start, end, turn)
                return True
        if end < self._final_through:
            # The engine's end_through_ms promised this cannot come. Kept, not lost: the
            # open turn has it.
            self._warn_once("bound", f"a word ending at {end} ms arrived after the turn "
                            f"through {self._final_through} ms was settled -- the engine's "
                            "end_through_ms did not hold, or that turn's final timed out; "
                            "the open turn takes it")
        self._open_words.append(w)
        self._last_piece = (start, end, None)
        return False

    def _forget_turn(self, turn: _Turn):
        with contextlib.suppress(ValueError):
            self._turns.remove(turn)

    async def _handle_preview(self, msg: dict):
        """The engine's answer to a commit in slice mode (TURN TEXT): a turn's goes
        downstream now, as the words in hand and the preview's text; a peek's is held for
        the verdict that adopts it, and goes to the speculative reply (THE PEEK)."""
        commit = self._commits.popleft() if self._commits else None
        turn = commit.turn if commit is not None else None
        peek = commit.peek if commit is not None else None
        if turn is None and peek is None:
            self._warn_once("preview", "a transcription.preview with no sliced commit "
                            "outstanding -- the engine and this service disagree about "
                            "what was sent; ignored")
            return
        at = turn.at_ms if turn is not None else peek.at_ms
        through = msg.get("through_ms")
        if isinstance(through, (int, float)) and int(through) != at:
            self._warn_once("through", f"the engine placed a commit at {through} ms, this "
                            f"service at {at} ms -- the turns are sliced at ours")
        cut = msg.get("complete") is False
        if cut:
            # The engine could not take the preview whole: its text is no one's. The turn
            # closes on its final, and nothing is speculated on it (TURN TEXT).
            n = turn.stop_n if turn is not None else peek.n
            logger.info(f"{self}: VAD stop {n}'s preview came back cut short (complete: "
                        "false) -- no preview: the turn closes on its final")
            msg = {**msg, "delta": ""}
        # The provisional text: the words in hand that end before the position, then the
        # preview's.
        owner = turn if turn is not None else peek.turn
        in_hand = (owner.words if owner is not None
                   else [w for w in self._open_words if w[1] < at])
        asked = ("".join(w[0] for w in in_hand) + (msg.get("delta") or "")).strip()
        self._log_segment({**msg, "text": asked}, commit)
        if turn is not None:
            await self._push_turn(turn, asked, len(in_hand), msg, commit)
        else:
            peek.previewed, peek.msg, peek.asked, peek.asked_n = True, msg, asked, len(in_hand)
            peek.seen = len(self._open_words)
            if peek.dropped:
                logger.debug(f"{self}: VAD stop {peek.n}'s peek answered after the caller "
                             "resumed -- no one's")
            elif peek.turn is not None:
                await self._push_turn(peek.turn, peek.asked, peek.asked_n, msg, commit)
            elif not cut:
                await self._offer_preview(peek)
        if self._peek_wanted is not None:
            # A stop waited for this answer to peek (one commit in flight).
            async with self._on_wire():
                if (self._peek_wanted == self._vad_stops and self._commit_pending
                        and self._peek is None and not self._commits):
                    await self._send_commit(final=True, why="peek", peek=True)
                self._peek_wanted = None

    def _callers(self, text: str, since: Optional[float]) -> str:
        """`text` less the bot's own words heard back off its front (the echo guard's
        strip, as _close applies it to a final)."""
        if text and self.echo_guard is not None:
            return self.echo_guard.strip(text, final=True, since=since)
        return text

    async def _offer_preview(self, peek: _Peek):
        """A peek's provisional text, before any verdict has closed the turn on it, for
        the speculative reply (SpeechPreviewFrame): as the turn will push it -- the bot's
        own words off its front -- and never while the wake gate sleeps."""
        if self.wake_gate is not None and self.wake_gate.asleep:
            return
        text = self._callers(peek.asked, self._seg_since)
        if text:
            await self.push_frame(SpeechPreviewFrame(stop_n=peek.n, text=text))

    async def _finalize_turns(self):
        """Settle every turn, oldest first, whose preview has come and whose position
        end_through_ms has passed -- no word of it can come any more -- or whose final
        has timed out (TURN TEXT); then time the oldest still waiting."""
        async with self._finals_lock:
            while self._turns and self._turns[0].previewed:
                turn = self._turns[0]
                final = (turn.at_ms <= self._end_through if not turn.expect
                         else (turn.at_ms + _VOUCH_MS <= self._end_through
                               and turn.at_ms <= self._audio_through))
                if not final and not self._final_overdue(turn):
                    break
                self._turns.popleft()
                self._final_through = turn.at_ms
                await self._settle_turn(turn, timed_out=not final)
            await self._time_final()

    def _final_overdue(self, turn: _Turn) -> bool:
        """The turn's final has had _FINAL_TIMEOUT_MS past its position: by the engine's
        decoded_through_ms, or by the wall clock since its commit went out."""
        return (self._decoded_ms >= turn.at_ms + _FINAL_TIMEOUT_MS
                or time.monotonic() >= turn.sent_at + _FINAL_TIMEOUT_MS / 1000)

    async def _time_final(self):
        """(Re)arm the timer for the oldest turn waiting for its final, if it changed."""
        oldest = self._turns[0] if self._turns and self._turns[0].previewed else None
        if oldest is self._final_for:
            return
        self._final_for = oldest
        if oldest is None:
            await self._cancel_timer("_final_task")
        else:
            await self._restart_timer("_final_task", self._final_after(oldest))

    async def _final_after(self, turn: _Turn):
        await asyncio.sleep(max(0.0, turn.sent_at + _FINAL_TIMEOUT_MS / 1000
                                - time.monotonic()))
        if self._fired("_final_task"):
            self._final_for = None
            await self._finalize_turns()

    async def _settle_turn(self, turn: _Turn, timed_out: bool = False):
        """A turn's final is in: all its words. Compared with the provisional text it was
        pushed as, by words (case and punctuation aside, as measured); a difference goes
        downstream for turn_text.TurnTextCorrector to apply, and every outcome is one
        [TURN] line. `timed_out`: the final never came (_finalize_turns), and the turn
        settles as its provisional text."""
        final = "".join(w[0] for w in turn.words).strip()
        if timed_out:
            self._final_timeouts += 1
            head = (f"[TURN] VAD stop {turn.stop_n}: final timeout, "
                    f"{time.monotonic() - turn.preview_at:.2f}s after the preview (position "
                    f"{turn.at_ms} ms; end_through_ms {self._end_through}, "
                    f"decoded_through_ms {self._decoded_ms}; "
                    f"{len(turn.words) - turn.asked_n} word(s) since; "
                    f"timeouts={self._final_timeouts})")
            if turn.deferred:
                logger.warning(f"{head} -- closed on the {len(_words_of(final))} word(s) in "
                               f"hand, {turn.deferred}")
                await self._close(turn.msg, turn.commit, final, final, turn.since)
            else:
                logger.warning(f"{head} -- settled as its provisional text")
            return
        kind, added, dropped = compare_turn_text(turn.asked, final)
        late = len(turn.words) - turn.asked_n
        wait = time.monotonic() - turn.preview_at
        if kind == "same":
            self._turn_same += 1
        else:
            self._turn_differs += 1
        tally = f"same={self._turn_same} differs={self._turn_differs}"
        head = (f"[TURN] VAD stop {turn.stop_n}: final +{wait:.2f}s after the preview, "
                f"{late} word(s) since")
        if turn.deferred:
            # Nothing went on at its preview (_push_turn): the turn closes now, on its
            # final, stamped with its stop -- over the bot, the barge-in its words are.
            logger.info(f"{head}: closed on its final ({len(_words_of(final))} word(s)), "
                        f"{turn.deferred} ({tally})")
            await self._close(turn.msg, turn.commit, final, final, turn.since)
            return
        if kind == "same":
            logger.info(f"{head}: as asked ({tally})")
            return
        if dropped and not self._turns:
            # The newest turn's final drops words: if the backstop commits the open words
            # before another turn closes, that is counted (_commit_stranded).
            self._last_drop = (turn.stop_n, dropped)
        if turn.pushed is None and _words_of(turn.asked):
            # The wake gate slept through words: nothing went on, and a wake phrase only
            # the final has is not re-offered (the gate's carry has moved on). Counts,
            # never words.
            logger.info(f"{head}: final {kind} ({len(added)} added, {len(dropped)} "
                        f"dropped) while the wake gate slept -- left as it went ({tally})")
            return
        # As the provisional was: the bot's own words off its front.
        corrected = self._callers(final, turn.since)
        if turn.woke and corrected:
            # The words that woke the gate went on as what followed the phrase: the
            # final's are measured the same way, if the final has the phrase too.
            found = find_wake(corrected, self.wake_gate.phrases)
            if found is None:
                logger.info(f"{head}: final {kind}, and the wake phrase the provisional "
                            f"woke on is not in it -- left as it went ({tally})")
                return
            corrected = found[1]
        if not turn.pushed:
            # Nothing with words went on for this turn -- an empty preview, the bot's own
            # words, a wake phrase alone -- so there is nothing downstream to correct, and
            # the final's words must not be lost: they go on as new words, through the
            # close every final goes through (the echo guard, the wake gate), stamped with
            # no stop (as the engine's own close of words is), and the turn they open is
            # asked on them (TURN TEXT).
            if corrected:
                logger.info(f"{head}: final {kind} {list(added)} where nothing was asked "
                            f"-- they go on as new words ({tally})")
                await self._close({"type": "transcription.final", "text": final}, None,
                                  final, corrected if turn.woke else final, turn.since)
            else:
                logger.info(f"{head}: final {kind}, the bot's own words -- nothing goes "
                            f"on ({tally})")
            return
        if corrected == turn.pushed:
            # Only the bot's own words differ, and the echo guard took them off both.
            logger.info(f"{head}: final {kind}, the same once the echo guard has run ({tally})")
            return
        logger.info(f"{head}: final {kind} added={list(added)} dropped={list(dropped)}: "
                    f"asked {turn.pushed!r}, final {corrected!r} ({tally})")
        await self.push_frame(TurnTextFinalFrame(stop_n=turn.stop_n, asked=turn.pushed,
                                                 final=corrected, kind=kind, added=added,
                                                 dropped=dropped, pushed_at=turn.preview_at))

    async def _send_carry(self, ws, commit: _Commit, carry: bytes):
        """Send the audio from where the commit's claim starts again, right behind the
        commit on its socket, as the next segment's start (CARRY THE TAIL). Called under
        the wire's lock, so no live append comes between the two."""
        n = len(carry) // 2
        # The engine counts it; the send clock does not (CARRY THE TAIL: the clocks).
        # Before the send is awaited, so a report handled while it drains reads early,
        # never late. A send that fails leaves a dead session, whose clock the reconnect
        # starts afresh.
        self._session_origin -= n
        commit.carry_ms = n * 1000 // (self.sample_rate or 16000)
        payload = base64.b64encode(carry).decode("ascii")
        try:
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": payload}))
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            logger.warning(f"{self}: the commit's carry ({commit.carry_ms}ms) was dropped, "
                           f"send failed: {e}")
            commit.carry_ms = 0

    async def _schedule_stream_boundary(self):
        """Draw the segment boundary once the model's trailing deltas have landed.

        Scheduled rather than awaited inline: _send_commit runs from process_frame, and
        sleeping there would stall the frame pipeline for the whole grace period.

        A later commit supersedes an earlier one -- if the caller pauses and resumes,
        the boundary belongs where they actually stopped, not where they first did.
        """
        err = await self._restart_timer("_boundary_task", self._boundary_after_delay())
        if err is not None:
            # A boundary drawn now, tail lost, beats a segment that never closes.
            logger.warning(f"{self}: could not schedule the segment boundary ({err}); "
                           "drawing it now")
            await self._synthesize_final()

    async def _cancel_stream_boundary(self):
        await self._cancel_timer("_boundary_task")

    async def _boundary_after_delay(self):
        await asyncio.sleep(_STREAM_TAIL_SECS)
        self._boundary_task = None
        await self._synthesize_final()

    async def _synthesize_final(self):
        """Draw a segment boundary from what has streamed in, and feed it through the
        ordinary final path so nothing downstream can tell the difference.

        text is passed explicitly rather than left empty: _handle_message would fall
        back to the interim buffer anyway, but _log_segment reads the message, and an
        empty one would log every healthy streaming segment as "EMPTY -- audio in, no
        words out" and trip the empty-final run detector.
        """
        text = self._interim_buffer.strip()
        await self._handle_message({"type": "transcription.done", "text": text})

    # ---- connection (WebsocketSTTService contract) -------------------------

    async def _connect(self):
        await super()._connect()  # base resets the _disconnecting flag
        await self._connect_websocket()
        # Only run the receiver if the websocket actually connected (a 503/refused
        # connect leaves _websocket None — see _connect_websocket).
        if self._websocket:
            # Base class wraps _receive_messages with error reporting + reconnect.
            self._receive_task = self.create_task(
                self._receive_task_handler(self._report_error)
            )

    async def _disconnect(self):
        await super()._disconnect()  # base sets _disconnecting (suppresses reconnect)
        # A boundary scheduled but not yet fired would push a transcript into a session
        # that has ended.
        await self._cancel_stream_boundary()
        try:
            if self._receive_task:
                await self.cancel_task(self._receive_task)
                self._receive_task = None
        finally:
            # The engine is single-session: its slot is only freed by our close
            # reaching it, so the close must run even if task-cancel raises.
            await self._disconnect_websocket()

    async def _connect_websocket(self):
        if self._websocket:
            return
        # A reconnect is a new session, so generation has to be started again on the
        # next audio chunk — otherwise the stream is silently dead for the rest of the
        # call and every segment finalises as empty.
        self._gen_started = False
        logger.debug(f"{self}: connecting to the engine at {self._url}")
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                self._websocket = await websockets.connect(self._url)
                # The engine counts the audio of this session from here; so does its
                # transcription report (SPEECH_DECODED). No await in between: run_stt
                # sends nothing on the new socket before this is taken.
                self._session_origin = self._sent_samples
                self._decoded_through = None
                # Nor does a carry reach back into the last session's audio.
                self._carry_floor = self._sent_samples
                # Slices are asked for, and taken until the session's created says
                # otherwise (TURN TEXT); its clocks start here.
                self._slicing = self._slice_asked
                self._end_through = -1
                self._audio_through = -1
                self._decoded_ms = -1
                self._final_through = 0
                self._last_piece = None
                # Handshake: the engine is single-model and just acknowledges this --
                # and an engine with the report starts sending it (one without ignores
                # the field). Not to the streaming backend: vLLM validates its session.
                update = {"type": "session.update", "model": self._model}
                if (self.transcription_progress or self._slice_asked) and not self._streaming:
                    update["transcription_progress"] = True
                if self._slice_asked:
                    # Before any audio, as the engine requires. "continuous" is what the
                    # spike engine (rbf3) also needed; the production engine ignores it.
                    # Its bounds ride on the progress report, so that is asked for too.
                    update["preview"] = True
                    update["continuous"] = True
                await self._websocket.send(json.dumps(update))
                self._stt_available = True
                if attempt:
                    logger.info(f"{self}: STT slot acquired on attempt {attempt + 1} "
                                "(the previous session's close was still settling)")
                return
            except Exception as e:  # 503 (engine session busy), refused, timeout, ...
                # If connect succeeded but the handshake send failed, an OPEN socket
                # holds the engine's single session slot — abandoning the reference
                # leaks the slot until TCP notices ("talks but can't hear" for every
                # later session). Close it before dropping it.
                ws, self._websocket = self._websocket, None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:  # noqa: BLE001 — best-effort cleanup
                        pass
                # A 503 means the engine is up and its single slot is taken — which,
                # right after an eviction, usually means "taken by the close we are
                # still waiting on". Both front-ends hand that over to a bare
                # `await asyncio.sleep(0.3)` and then connect once; if the engine has
                # not finished processing the close by then, this session declares
                # itself deaf and the user is told the assistant is busy with another
                # session when nothing is using it at all (SIP also hangs the caller
                # up). TLC finds the interleaving with one OpenClaw session and one
                # SIP call — brain/formal/SttSlot.tla, MODE = "fixedSettle",
                # invariant NoFalseBusy — because nothing anywhere observes that the
                # engine has actually freed the slot.
                #
                # So retry a REJECTION briefly. Only a rejection: a refused or
                # blackholed host is not going to fix itself in a second, and each
                # such attempt costs the websockets open timeout (~10s), which would
                # blow through greet()'s resolution window and leave the caller in
                # silence instead of hearing the warning.
                if attempt + 1 < _CONNECT_ATTEMPTS and _is_slot_busy(e):
                    logger.debug(f"{self}: engine slot busy ({type(e).__name__}) — "
                                 f"retrying in {_CONNECT_RETRY_S}s "
                                 f"({attempt + 2}/{_CONNECT_ATTEMPTS})")
                    await asyncio.sleep(_CONNECT_RETRY_S)
                    continue
                self._stt_available = False
                self._slot_busy = _is_slot_busy(e)
                logger.error(
                    f"{self}: STT engine unavailable ({type(e).__name__}: {e}) — the "
                    f"agent cannot hear; the brain will warn the user "
                    f"({'engine slot held' if self._slot_busy else 'engine unreachable'})."
                )
                # Do NOT re-raise: let the pipeline start so the brain can speak a
                # pre-computed warning instead of silently 'talking but not hearing'.
                return

    async def _disconnect_websocket(self):
        try:
            if self._websocket and self._websocket.state is not State.CLOSED:
                # Close in ANY live state, not just OPEN: a CONNECTING/CLOSING or
                # half-open socket dropped by reference never finishes the close
                # handshake from our side, and the single-session engine's slot
                # stays held until TCP times it out. close() is idempotent.
                logger.debug(f"{self}: disconnecting from the engine")
                await self._websocket.close()
        finally:
            self._websocket = None
            # The open segment's words go with the session, and no final or wordless
            # close will ever come for them: say so, or the stop strategy keeps the last
            # interim as words that settled long ago (SegmentResetFrame). Not at the
            # session's end, and never at the cost of the teardown below.
            if not self._ending:
                try:
                    await self.push_frame(SegmentResetFrame(stop_n=self._vad_stops))
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"{self}: could not push the segment reset ({e})")
            self._interim_buffer = ""
            self._seg_since = None
            # TURN TEXT: the open words go with the session, and so do the finals the
            # committed turns were waiting for -- what was pushed at their previews
            # stands.
            if self._turns:
                logger.info(f"{self}: the engine session closed with "
                            f"{len(self._turns)} turn(s) not yet final -- their "
                            "provisional text stands")
            self._open_words = []
            self._last_piece = None
            self._turns.clear()
            self._final_for = None
            await self._cancel_timer("_final_task")
            self._peek = None
            self._peek_wanted = None
            # Per-session, like the buffer beside it: a run of 4 empty finals before a
            # reconnect plus 1 after is not 5 finals of the same microphone.
            self._empty_finals = 0
            # The buffer it would have committed is gone, and the socket with it --
            # and so are the dones its commits were owed.
            await self._cancel_stranded_commit()
            self._commits.clear()
            self._commit_pending = False
            self._closed_unasked = False
            # The stop this placed was on audio the new session will never hold, and the
            # report was on the old session's clock.
            self._speech_end = None
            self._decoded_through = None
            await self._cancel_hold_expiry()

    # ---- transcripts out ---------------------------------------------------

    async def _close(self, msg: dict, commit: Optional[_Commit], engine_text: str,
                     text: str, since: Optional[float]) -> tuple:
        """Push a segment's close downstream: its final, or a SegmentDoneFrame when it has
        no words. `commit` is the commit it answers (None: the engine closed it on its
        own), `engine_text` the engine's own words for it, `text` what goes on (the
        done's text, or the interim it fell back to; in slice mode the provisional
        text), `since` the echo guard's clock for it. Returns the text pushed as the
        final ("" for a wordless close; None while the wake gate sleeps through it) and
        whether these words woke the gate (what was pushed is what followed the wake
        phrase): what a slice's correction is measured against."""
        unasked = commit is None
        # The stamp the final (or the SegmentDoneFrame) below carries: the VAD stop
        # this close answers, for the stop strategy to hold against its own count
        # (see FinalTranscriptionFrame). A commit's answer is stamped with the
        # commit's. The engine's own close names no stop, so it is placed by what
        # this service knows:
        #   * it carries no words -- the engine's own close is on speech it heard,
        #     so this is the silent tail a commit of ours closed behind a close
        #     of the engine's that took the commit's place in the queue (the
        #     pairing _commits documents): it answers that commit's stop after
        #     all, and never the stop of an utterance since;
        #   * otherwise it is the caller's words up to some point before now --
        #     inside the utterance under way, or an earlier one's whose finish
        #     ran long: whichever, no stop of ours has been answered by it, and it
        #     is stamped NO_STOP: it pays no stop's debt. The strategy waits for
        #     the close the stop itself produces: the rest of the words, or the
        #     silent tail the stop's commit returns at once. It was stamped as the
        #     NEXT stop's until SttCommit.tla gave the VAD its real place: the
        #     strategy can count that stop before this service has seen it, and
        #     then the first half paid the stop with the rest still in the
        #     segment -- the turn ended on half a sentence (sc_final_stop_marked).
        # It does NOT release a segment held for its verdict, though the first
        # cut did: this close may be the PREVIOUS utterance's, landing under the
        # hold on the next one (the engine's finish outlasting the caller's next
        # utterance), and a released hold would leave those words to the engine's
        # own close, unstamped. Keeping the hold costs the ceiling's commit one
        # wordless done, which the engine answers without a decode (no audio fed
        # since its own close: voxtral_websocket.c's "empty commit").
        if not unasked:
            stop_n = commit.n
        elif not text:
            stop_n = self._last_commit_n
        else:
            stop_n = NO_STOP
        if unasked and not self._slicing:
            # See the VAD stop in process_frame: the next stop, if none has come
            # yet, closes the tail rather than hold it. Not in slice mode, where the
            # only unasked close is a turn's words going on after its empty provisional
            # (_settle_turn), and no segment was split.
            self._closed_unasked = True
        # A run of finals with nothing in them means the engine is being handed audio
        # it can find no words in. Both are silent from the pipeline's side — no
        # transcript, so no turn, so no reply — and that is indistinguishable from the
        # pipeline bugs that produce the same silence, which is why it has to say so.
        #
        # Live 2026-08-21: 77 seconds of it. The engine ran 11 segments and returned 0
        # characters for every one while the brain's own VAD stayed QUIET, so the room
        # was making noise and the user's voice was not reaching the microphone. The
        # engine was healthy throughout — fed a synthesized sentence it transcribed it
        # perfectly — and nothing anywhere said "I cannot hear you".
        #
        # One line per run, not per final: an isolated empty final is ordinary (the VAD
        # fires on a cough or a door), and warning on each would bury the real case.
        #
        # Not while a wake gate is asleep (#116): the room mic's session is open on a
        # quiet room, waiting for a wake word, and wordless finals are all it should
        # hear. The run counts from the wake, so a muted mic in a conversation still
        # says so.
        if self.wake_gate is not None and self.wake_gate.asleep:
            self._empty_finals = 0
        elif not engine_text:
            self._empty_finals += 1
            if self._empty_finals == _EMPTY_FINAL_RUN:
                logger.warning(
                    f"{self}: {self._empty_finals} finals in a row with no words — "
                    "the engine is hearing audio but no speech (microphone muted or "
                    "routed away, or the room is louder than the speaker)"
                )
        else:
            if self._empty_finals >= _EMPTY_FINAL_RUN:
                logger.info(f"{self}: hearing speech again after "
                            f"{self._empty_finals} empty finals")
            self._empty_finals = 0
        result = msg
        if text and self.echo_guard is not None:
            # Before the wake gate: "hey teaport" in the bot's own voice wakes nothing.
            kept = self.echo_guard.strip(text, final=True, since=since)
            if kept != text and (self.wake_gate is None or not self.wake_gate.asleep):
                # Never the room's words while it sleeps (wake_gate.py).
                echo_guard.log_strip("final", text, kept)
                result = {**msg, "text": kept}
            text = kept
        asleep = self.wake_gate is not None and self.wake_gate.asleep
        woke = False
        if text and self.wake_gate is not None:
            # Asleep: no wake phrase -> nothing; one -> what follows it (wake_gate.py).
            gated = await self.wake_gate.final(text, self.push_frame)
            if gated != text:
                # The engine's message carries the whole utterance: not what is kept.
                result = {**msg, "text": gated or ""}
            woke = asleep and not self.wake_gate.asleep
            text = gated
        pushed = None if asleep and not woke else (text or "")
        if text:
            await self.push_frame(
                FinalTranscriptionFrame(
                    text,
                    self._user_id,
                    time_now_iso8601(),
                    self._language,
                    result=result,
                    finalized=True,
                    stop_n=stop_n,
                )
            )
        else:
            # The segment closed with nothing in it. The stop strategy is waiting
            # on this stop's close (endpointing.py): this is how it learns the
            # close came, and that the words it already holds are all the turn
            # will get. A data frame, so it queues behind the interims it follows.
            await self.push_frame(SegmentDoneFrame(stop_n=stop_n))
        await self.stop_processing_metrics()
        return pushed, woke

    async def _receive_messages(self):
        # Iterate the base class's websocket directly (WebsocketSTTService owns it).
        # Returning normally on graceful close is expected; the base
        # _receive_task_handler handles reconnect/cleanup.
        if not self._websocket:
            return
        async for message in self._websocket:
            try:
                msg = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"{self}: non-JSON message ({len(message)} chars)")
                continue
            await self._handle_message(msg)

    async def _handle_message(self, msg: dict):
        mtype = msg.get("type")

        if mtype == "transcription.delta":
            # Append-only token piece → grow the running utterance buffer and
            # emit it as the cumulative interim hypothesis.
            piece = msg.get("delta", "")
            if self._slicing and self._slice_delta(piece, msg.get("end_ms"),
                                                   msg.get("audio_ms")):
                # A committed turn's word: its final's, never an interim (TURN TEXT).
                return
            self._interim_buffer += piece
            # The streaming backend emits a delta per AUDIO FRAME -- 12.5 a second,
            # blank ones through silence (210 of them across one 16.9 s segment). Each
            # pushed frame re-arms the aggregator's user_turn_stop_timeout, so the turn
            # could never stop and the model was never asked: live 2026-09-10 22:20,
            # "SILENT TURN: 12s after user-stopped and no audio. reached=['nothing']"
            # while the caller said "Hello?" four times into a bot that had gone deaf.
            #
            # The text still accumulates above -- spacing between words depends on it --
            # but a delta that carries no word is not a hypothesis and must not be
            # announced as one.
            if self._streaming and not piece.strip():
                return
            # Asleep, a hypothesis of the room goes nowhere (wake_gate.py) -- but it is
            # still a segment streaming, which the backstop below must keep watching.
            # The bot's own words are held back (echo_guard.py): only what the guard
            # emptied is skipped, an interim that was empty is announced as before.
            heard, echo = self._interim_buffer, False
            if self.echo_guard is not None and heard:
                if self._seg_since is None and heard.strip():
                    self._seg_since = self.echo_guard.bot.clock()
                heard = self.echo_guard.strip(heard, final=False, since=self._seg_since)
                echo = not heard
            if not echo and (self.wake_gate is None or not self.wake_gate.asleep):
                await self.push_frame(
                    InterimTranscriptionFrame(
                        heard,
                        self._user_id,
                        time_now_iso8601(),
                        self._language,
                        result=msg,
                    )
                )
            self._seg_interims += 1
            # Every delta re-arms: while the user is still talking this can never fire.
            await self._arm_stranded_commit()

        elif mtype == "transcription.done":
            # Authoritative per-utterance text; engine resets after this. Mark it
            # finalized=True: this IS the definitive final, so the turn-stop
            # strategy triggers the turn the instant it arrives (once Smart Turn
            # says complete) instead of waiting out its STT p99-latency fallback
            # timeout — that fallback is for STTs that never signal a final, and
            # eating it added ~(ttfs_p99 - stop_secs) of dead hang after every
            # finished utterance.
            # `or`, not get()'s default: the engine sends "text": "" (key PRESENT,
            # value empty) for an utterance it could not finalize, so the default is
            # never reached and the interim fallback was dead code for the one case it
            # was written for. Verified against the engine on 2026-08-20 -- 1.5s of
            # silence returns {"type": "transcription.done", "text": "", ...}.
            #
            # Pushing nothing wedges the turn. MinWordsUserTurnStartStrategy starts the
            # user turn from an INTERIM, but TurnAnalyzerUserTurnStopStrategy only ever
            # sets its _text from a final TranscriptionFrame, and
            # _maybe_trigger_user_turn_stopped() returns early while that is empty. So a
            # turn that started on interims and got an empty final can never stop: it
            # waits out the aggregator's 5s user_turn_stop_timeout, and since any VAD or
            # transcription frame re-arms that timeout, a noisy mic starves it and the
            # turn never ends at all. Falling back to the hypothesis we already streamed
            # both recovers the words and lets the turn close.
            # The ENGINE's own verdict, kept separate from what we go on to push. The
            # empty-final run below has to be counted on this and not on the fallback:
            # `or self._interim_buffer` made `text` truthy whenever the engine had
            # streamed so much as one delta before deciding the audio held no speech, so
            # _empty_finals was reset instead of incremented and the "microphone muted"
            # warning could never reach its run — the two halves of this hunk cancelled
            # out. .strip() because a final of " " is truthy but wordless, and pushing it
            # commits a user turn whose content is whitespace.
            # The segment closed, however it was committed — the backstop is done.
            await self._cancel_stranded_commit()
            # The commit this done answers, oldest first (see _commits). None is the
            # engine closing the segment on its own (its endpointer; its ~15 s buffer
            # sends no done), not the answer to one we sent. An engine that says which
            # ("reason": "vad" for its own close, "commit" for ours -- teagram-engine
            # since the trailing-silence credit) is believed over the queue: its own
            # close then never takes a commit's place, which is the one pairing the
            # queue alone cannot get right (SttCommit.tla's unmarked rows). An older
            # engine sends no reason, and the queue decides as before.
            if msg.get("reason") == "vad":
                commit = None
            else:
                commit = self._commits.popleft() if self._commits else None
                if commit is None and msg.get("reason") == "commit":
                    # Cannot happen on an engine that answers every final commit
                    # once (teagram-engine#36 does): either a commit this service
                    # never recorded, or an engine labelling its own closes as
                    # answers. Taken as the engine's own close, which is what an
                    # unmatched done is; the drift is named so it is not silent.
                    logger.warning(f"{self}: a done marked as a commit's answer arrived "
                                   "with no commit outstanding -- the engine and this "
                                   "service disagree about what was sent; taken as the "
                                   "engine's own close")
            if commit is not None and commit.turn is not None:
                # A commit taken for a slice, answered as a segment: the engine is not
                # slicing after all. Its turn waits for no final.
                self._forget_turn(commit.turn)
            self._log_segment(msg, commit)
            engine_text = (msg.get("text") or "").strip()
            text = engine_text or self._interim_buffer.strip()
            # Counts, never words: this line runs before the wake gate does.
            if text and not engine_text:
                logger.debug(f"{self}: empty final — falling back to the interim "
                             f"hypothesis ({len(text)} chars) to close the turn")
            self._interim_buffer = ""
            since, self._seg_since = self._seg_since, None
            await self._close(msg, commit, engine_text, text, since)

        elif mtype == "transcription.preview":
            await self._handle_preview(msg)

        elif mtype == "transcription.progress":
            # How far the engine has transcribed, on this session's audio clock
            # (SPEECH_DECODED); whole ms, so the sample it names is never past it.
            ms = msg.get("decoded_through_ms")
            if self._slicing:
                # The bound the turns' finals wait for (TURN TEXT). It is also the
                # report's position here: decoded_through_ms is no bound once words are
                # held until complete, and every word of the speech that ends before
                # end_through_ms is in hand. Asked for by the slices, so pushed on only
                # where the stop strategy asked for the report too.
                ms = msg.get("end_through_ms")
                decoded = msg.get("decoded_through_ms")
                if isinstance(decoded, (int, float)):
                    self._decoded_ms = max(self._decoded_ms, int(decoded))
                started = msg.get("audio_through_ms")
                if isinstance(started, (int, float)):
                    self._audio_through = max(self._audio_through, int(started))
                if isinstance(ms, (int, float)):
                    self._end_through = max(self._end_through, int(ms))
                await self._finalize_turns()
                if not self.transcription_progress:
                    ms = None
            if isinstance(ms, (int, float)):
                rate = self.sample_rate or 16000
                self._decoded_through = self._session_origin + int(ms) * rate // 1000
                await self._push_if_speech_decoded()

        elif mtype == "session.created":
            # Whether this engine session reports its transcription's progress (it
            # offers it and we asked): the stop strategy then asks the speculation on
            # the report rather than on a timer. Said at every session's start, so a
            # reconnect to an engine without it puts the strategy back on the timer.
            capabilities = msg.get("capabilities") or []
            reported = (self.transcription_progress and not self._streaming
                        and "transcription.progress" in capabilities)
            self._reports = reported
            # Slices where asked and offered (TURN TEXT). The engine sends this first,
            # so no delta or answer has come yet; a commit already sent for a slice is
            # answered as a segment, and the done path forgets its turn.
            if self._slice_asked:
                # Both: the preview answers the commits, and the progress report's
                # end_through_ms is what makes a turn's final known. Without it no turn
                # would ever be final.
                self._slicing = ("transcription.preview" in capabilities
                                 and "transcription.progress" in capabilities)
                if not self._slicing:
                    logger.warning(f"{self}: TEAPORT_STT_TURN_TEXT=slice, but this engine "
                                   "offers no preview stream with a progress report -- "
                                   "this session's turns are its segments, as before")
                    self._open_words = []
            logger.debug(f"{self}: session created {msg.get('id')}"
                         f"{' (transcription.progress)' if reported else ''}"
                         f"{' (turn text sliced)' if self._slicing else ''}")
            await self.push_frame(SttProgressFrame(reported=reported,
                                                   peeks=self._slicing and self._peeks))

        elif mtype == "error":
            logger.error(
                f"{self}: engine error: {msg.get('error')} ({msg.get('code')})"
            )

        else:
            logger.trace(f"{self}: unhandled message type {mtype!r}")
