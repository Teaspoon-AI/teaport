#
# teaport — utterances OpenClaw's spoken run control takes (#230).
#
# On a Talk session OpenClaw's relay reads every final user transcript the brain sends
# it (runControl.handleSpoken, relay/session-create.ts, 2026.9.7). While a consult it
# runs is in flight, an utterance its classifier calls run control -- "cancel that",
# "how's it going?", "actually make it tomorrow", "also check the weather" -- is acted on
# there: the run is cancelled, reported on, steered or given the follow-up, and the
# relay answers the user itself, through the provider (a host note, client_notes.py).
# The brain's own LLM must not answer the same words as well: a second "okay, cancelled"
# at best, a second consult for a steer at worst.
#
# Which utterances: the ones the relay acts on, decided by the plugin, which sees the
# relay's side. It runs OpenClaw's own classifier (the SDK's
# shouldAutoControlRealtimeVoiceAgentText, not a copy of its regexes) while it has a
# consult open (one the relay acknowledged and has not answered yet; supportsToolCalls is
# declared, so the relay never controls idle), skips what the relay drops as the
# assistant's own echo (isLikelyRealtimeVoiceAssistantEchoTranscript, its 12 s lookback),
# and then waits for the relay to ACT -- its reply (sendUserMessage) or a cancelled consult
# result -- before it says "claimed" (plugin/provider.js, answerRunControl). The relay
# counts its run a moment after the consult is acknowledged (only once the Talk client's
# talk.client.toolCall and chat.send are acknowledged) and forgets it a moment before the
# result is submitted; in those windows, and for an echo the classifier alone would call
# control, it acts on nothing, and an utterance claimed on the classifier alone was
# answered by no one. Confirmed, it cannot be. Not acted on within the plugin's window
# (CONFIRM_MS, 2 s), the answer is "not claimed" and the model answers the words.
#
# Why the plugin waits and not the brain: the plugin sees both signs first hand, as they
# happen, and on the same socket the host note then reaches the brain by, after the
# answer. Here, a fallback would mean taking a turn back after it was closed with
# nothing to answer (TakenTurnCloser drops the turn's earlier words with it) and building
# it again. Held a little longer instead, the turn simply goes on unchanged when no one
# acts. What it costs: a claimed utterance ends its turn when the relay's reply comes
# (it says nothing before that anyway); one the relay ignores waits up to the window.
#
# THE ORDER. The STT's final is both what starts the brain's LLM turn (it ends the user
# turn in the aggregator below) and what the brain sends the plugin as the transcript
# (UserTranscriptEmitter, above this). So this processor sits directly below the
# emitter: the transcript has gone out, and the turn has not ended yet. While a consult
# is running (FollowupGate.consults_running) it holds a non-empty final, and every STT
# frame after it in order, and asks the plugin over the socket:
#
#   brain -> plugin   {"type":"run_control","id":"r1","text":"cancel that"}
#   plugin -> brain   {"type":"run_control_result","id":"r1","claimed":true|false}
#
# The transcript and the question can cross on the way (UserTranscriptEmitter pushes the
# final down before its transcript message); the plugin waits for the relay's act from
# whichever reached it first. Claimed, the final goes on taken (taken_turn.taken: emptied
# and marked) and TakenTurnCloser ends the turn with nothing to answer, the way Wi-Fi
# setup takes words; and the words go into the context as a notice at the next turn
# boundary (ClientNotes.add_notice), so the model knows what the user said that the
# relay's reply answers. Not claimed, or no answer within CLAIM_WAIT_S (past the plugin's
# own window, so that means a wedged plugin), the held frames go on unchanged: the turn
# ends exactly as it would have, that much later. With no consult running nothing is held.
#
# A SECOND FINAL while one is held (the user went on talking) is asked about in its turn,
# not let through with the first's answer: the relay reads every final, so a second
# "cancel that" or "how much longer?" is the relay's as much as the first, and let through
# unasked the model would answer it too, or call openclaw_agent_control on words the
# relay already acted on. When the first is answered, the frames held behind it go on up
# to the next final that asks, and that final is asked about, the rest still held.
#
# "Running" is async and agent-first consults alike (tools._count_consult): an agent-first
# one counts for as long as its turn waits on it. Not covered: a barge-in usually ends
# that wait before its final comes (an interim starts the turn and cancels the call), and
# the relay's run goes on -- it is not cancelled there, as an async consult survives a
# barge-in too -- so a spoken "cancel that" may still be acted on by the relay and
# answered by the model as well. Agent-first only (TEAPORT_AGENT_FIRST, off by default
# and off on the appliance).
#
# A CONSULT THAT TIMED OUT. The brain stops asking once its consult is over, and the plugin
# once the relay has answered it, but the relay acts as long as the consult's RUN is still
# going. When the brain's own wait (tools._ASYNC_CONSULT_TIMEOUT) runs out first, it has the
# relay cancel the call (tools._cancel_at_relay: the relay aborts the run), so nothing is
# left to act on. Not covered: the Control UI's own 120 s wait (shared.ts,
# waitForChatResult) submits "OpenClaw tool call timed out" as the consult's result without
# aborting the run, and once a result is in, the relay no longer cancels the call; the run
# goes on until it ends by itself, and a "cancel that" said meanwhile is acted on by the
# relay (it stops that run, and says so) and answered by the model as well, from what it
# knows (that the request ran out of time). Rare: a consult past two minutes, then words
# about it. Closing it would take the run's own end, which nothing on this side sees.
#
# Only on a Talk session whose plugin announced it (?run_control=1, plugin/provider.js):
# the room mic and the phone line have no relay, so no run control. On a phone call the
# barge-in judge (barge_pause.BargeInJudgeGate, #224) holds the STT's frames above the
# aggregator the same way; the two never share a session, and if they did this one,
# higher up, would decide first and hand the judge a taken final, which it lets pass.
#
import asyncio
import json
import time
from dataclasses import dataclass

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    OutputTransportMessageUrgentFrame,
    SystemFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.endpointing import (
    NoSpeechReportFrame,
    SegmentDoneFrame,
    SegmentResetFrame,
    SpeechDecodedFrame,
    SttProgressFrame,
)
from teaport_brain.taken_turn import is_taken, taken

# The ?run_control= value a plugin that answers run_control questions sends.
PROTOCOL = "1"
# The longest a final waits for the plugin's answer. The plugin answers at once for words
# the relay will not act on, and within its own window (CONFIRM_MS in plugin/provider.js,
# 2 s) for words it might, so this only bounds a plugin that is wedged or gone.
CLAIM_WAIT_S = 2.5

# What the STT pushes for the turn strategies, in its order: held together behind a final,
# so a segment close or a decode report never overtakes the words it follows.
_STT_FRAMES = (TranscriptionFrame, InterimTranscriptionFrame, SegmentDoneFrame,
               SegmentResetFrame, SpeechDecodedFrame, NoSpeechReportFrame, SttProgressFrame)

# The notice a claimed utterance leaves in the context (ClientNotes.add_notice).
_NOTICE = ("The user said {} about the request running in the background, and OpenClaw "
           "took it: it acts on it and answers them itself.")


@dataclass
class RunControlResultFrame(SystemFrame):
    """The plugin's answer to a run_control question (gateway_serializer,
    {"type":"run_control_result"}). A SystemFrame, so it reaches RunControlClaims at once,
    past whatever the STT has queued."""

    request_id: str = ""
    claimed: bool = False


class RunControlClaims(FrameProcessor):
    """Holds a final while a consult runs and asks the plugin whether OpenClaw's run
    control took it (see the module comment). Place it directly below
    UserTranscriptEmitter, above everything else that reads the user's words.

    `consults_running`: () -> how many consults this session has in flight
    (FollowupGate.consults_running). `notes`: the session's ClientNotes, for the notice a
    claimed utterance leaves; None leaves none."""

    def __init__(self, consults_running, *, notes=None, wait_secs: float = CLAIM_WAIT_S):
        super().__init__()
        self._consults = consults_running
        self.notes = notes
        self._wait_secs = wait_secs
        # The STT's frames held since the final asked about, in order; empty when not
        # asking. A lock keeps a release in one piece against frames still arriving.
        self._held: list = []
        self._lock = asyncio.Lock()
        self._id = None
        self._seq = 0
        self._t0 = 0.0
        self._timer = None
        # Tallies, for the log and the tests.
        self.claimed = 0
        self.passed = 0
        self.unanswered = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, RunControlResultFrame):
            async with self._lock:
                await self._decided(frame.request_id, frame.claimed)
            return  # consumed
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, _STT_FRAMES):
            async with self._lock:
                if self._held:
                    self._held.append(frame)  # a final among them is asked in its turn
                elif self._asks(frame):
                    await self._ask(frame)
                else:
                    await self.push_frame(frame, direction)
            return
        if isinstance(frame, (EndFrame, CancelFrame)):
            async with self._lock:
                await self._decided(self._id, False, ending=True)
        await self.push_frame(frame, direction)

    def _asks(self, frame) -> bool:
        if not isinstance(frame, TranscriptionFrame) or is_taken(frame):
            return False
        if not (frame.text or "").strip():
            return False
        try:
            return self._consults() > 0
        except Exception:  # noqa: BLE001 -- a broken count asks nothing
            return False

    async def _ask(self, final: TranscriptionFrame, behind=()):
        """Hold `final` (and the frames `behind` it) and ask the plugin about it."""
        self._seq += 1
        self._id = f"r{self._seq}"
        self._held = [final, *behind]
        self._t0 = time.monotonic()
        # The display copy's text: what UserTranscriptEmitter sent, which the relay read.
        await self.push_frame(OutputTransportMessageUrgentFrame(message={
            "type": "run_control", "id": self._id, "text": final.text.strip()}))
        self._timer = self.create_task(self._expire(self._id), name="run-control-wait")

    async def _expire(self, request_id: str):
        await asyncio.sleep(self._wait_secs)
        async with self._lock:
            await self._decided(request_id, False,
                                why=f"no answer within {self._wait_secs:.1f}s")

    async def _decided(self, request_id, claimed: bool, *, why: str = "",
                       ending: bool = False):
        """The answer to `request_id` (a stale one is ignored): the held frames go on, the
        final taken when it was claimed. `why`: no answer came; `ending`: the session is
        ending, and what is held goes on ahead of its EndFrame."""
        if request_id is None or request_id != self._id or not self._held:
            return
        timer, self._timer = self._timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        held, self._held, self._id = self._held, [], None
        final = held[0]
        text = final.text.strip()
        waited = time.monotonic() - self._t0
        if claimed:
            self.claimed += 1
            held[0] = taken(final)
            logger.info(f"RUN-CONTROL: OpenClaw took {text!r} (asked {waited:.2f}s); the "
                        "model does not answer it")
            if self.notes is not None:
                self.notes.add_notice(_NOTICE.format(json.dumps(text, ensure_ascii=False)))
        elif not ending:
            if why:
                self.unanswered += 1
                logger.warning(f"RUN-CONTROL: {why} for {text!r}; the turn goes on")
            else:
                self.passed += 1
                logger.debug(f"RUN-CONTROL: {text!r} is the model's ({waited:.2f}s)")
        # What was held behind it goes on, up to the next final that asks (module
        # comment, A SECOND FINAL): that one is asked about, the rest held behind it. At
        # the session's end everything goes, unasked.
        for i, f in enumerate(held):
            if i and not ending and self._asks(f):
                await self._ask(f, held[i + 1:])
                return
            await self.push_frame(f)

    async def cleanup(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self.claimed or self.unanswered:
            logger.info(f"RUN-CONTROL summary: {self.claimed} taken by OpenClaw, "
                        f"{self.passed} the model's, {self.unanswered} unanswered")
        await super().cleanup()
