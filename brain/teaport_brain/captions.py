#
# teaport — live transcript/caption emitters for the OpenClaw Talk UI.
#
# Two processors: user transcript streaming (UserTranscriptEmitter, before the user
# aggregator) and assistant captions (CaptionTap, after transport.output()).
#
# ── Why CaptionTap is UTTERANCE-scoped, not LLM-response-scoped ──────────────
# The previous design (three processors sharing CaptionState) finalized one bubble
# per LLM response: partials paced from TTSTextFrames, a final from the captured
# LLM text on BotStoppedSpeaking, per-reply state reset on LLMFullResponseStart.
# That assumed LLM-response boundaries align with audio-segment boundaries. They
# don't: tool-call fillers (TTSSpeakFrame) have no LLM response at all, chained
# tool turns synthesize several utterances that play as ONE continuous audio
# segment (a single BotStopped for four utterances — observed live 2026-07-05),
# and a barged reply's in-flight synthesis can land after the interruption clear
# and still play. Result: state resets mid-segment, finals carrying only the
# NEWEST reply's text replacing bubbles that showed everything spoken (the
# "heard it but it's not in chat" ghost — 'Sure thing—where should I check…'
# vanished), premature finals for not-yet-played replies, and partial buffers
# concatenating utterances ("Opening that page. He won NBA championships…" next
# to a reply-only final = the same text charted twice).
#
# The fix: pipecat's TTS gives every utterance its own audio context — one per
# LLM turn (all its sentences share it), one per standalone TTSSpeakFrame filler
# — and every word's AggregatedTextProgressFrame carries that context_id,
# released by the transport clock at the word's playout time. So playout-paced
# utterance segmentation needs NO synthesis-time state at all:
#
#   context_id changes  → previous utterance ended seamlessly: finalize it
#   BotStoppedSpeaking  → real playout silence: finalize the current utterance
#   interruption        → the client commits the active bubble itself; mark the
#                         context DEAD and drop its stragglers (no final — one
#                         would land as a second bubble after the user's turn)
#
# The final text is the utterance's OWN played segments — what was actually
# spoken, in playout order, exactly once. The invariant this enforces: every
# spoken utterance is charted in exactly one bubble, and bubbles appear in the
# order the words were heard.
#
import os

from loguru import logger
from teaport_brain.env import env_flag

import time

from pipecat.frames.frames import (
    AggregatedTextProgressFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# TEAPORT_TRACE=1 keeps the [CAP] caption-pipeline traces around (they've caught
# every bubble-rendering regression so far) without spamming normal logs.
_TRACE = env_flag("TEAPORT_TRACE", False)

# While the user's interims are flowing, assistant partials are HELD (see
# CaptionTap): the Talk UI commits the active assistant bubble the moment the
# user's first interim renders, so any partial we emit during their utterance
# REOPENS a second assistant bubble ("seen twice"; a following tool card then
# renders into that stray bubble). A gap longer than this since the last user
# interim counts as quiet again — inter-interim gaps run ~0.3-0.7s, and a fresh
# reply's first audio lands well past this after the user's final.
_USER_HOLD_S = float(os.getenv("TEAPORT_CAPTION_USER_HOLD_S", "1.2"))

# Caption text rides pipecat 1.5.0's AggregatedTextProgressFrame, NOT the bare
# TTSTextFrame word tokens: the sequencer emits one progress frame per word with
# accumulated_text SLICED from the source sentence — exact spacing, punctuation,
# and apostrophes ("Whether 'tis", not "Whether'tis") with no rejoin heuristic.
# Verified live on the appliance (2026-07-21): each progress frame is released
# by the transport clock in lockstep with its sibling word frame (same pts,
# <=1 ms apart), so pacing, the caption lead, and barge-in flush behavior are
# identical to the word frames. accumulated_text restarts at each sentence
# SEGMENT within a turn's shared audio context; CaptionTap stitches completed
# segments (their full source text) plus the live segment's accumulated text.


class VoiceActivity:
    """One shared fact: when did the USER's transcript last reach the client?
    UserTranscriptEmitter stamps it as it sends; CaptionTap reads it to decide
    whether the client has committed the active assistant bubble."""

    def __init__(self):
        self.user_ts = 0.0  # time.monotonic() of the last user interim/final sent

    def stamp(self):
        self.user_ts = time.monotonic()

    def user_active(self) -> bool:
        return (time.monotonic() - self.user_ts) <= _USER_HOLD_S


class UserTranscriptEmitter(FrameProcessor):
    """Stream the user transcript to the relay: each interim pushes the FULL
    partial text; the final pushes the FULL final and marks it. This is what
    OpenClaw's onTranscript(role, text, isFinal) expects — full text on every
    event, NOT deltas (a delta final reads as a 'trailing-fragment' and gets
    dropped, which breaks the turn and stalls the LLM). Placed BEFORE the user
    aggregator, which consumes TranscriptionFrame and does not forward it.

    Both interims AND the final are sent as OutputTransportMessageUrgentFrame so
    they go out IN PIPELINE ORDER. This is load-bearing for correct rendering:
    the base output transport sends urgent messages inline (base_output
    process_frame → send_message), but routes a NORMAL OutputTransportMessageFrame
    through the paced media sink queue. If interims were normal and only the final
    urgent, the urgent final would jump AHEAD of interims still draining from the
    sink queue — so on the wire a couple of that turn's own interims arrive AFTER
    its final. The final already closed the turn's bubble in the Talk UI
    (userEntryId → null), so each such trailing interim (a prefix of / equal to
    the final) opens a SECOND bubble seeded with the just-finished turn's text;
    the NEXT utterance's interims then merge into that stray bubble, so its text
    gets prepended to the next turn ("same text repeated"). Sending interims
    urgent keeps the final last for its turn, so no straggler is ever emitted."""

    def __init__(self, activity: "VoiceActivity" = None):
        super().__init__()
        self._activity = activity

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, InterimTranscriptionFrame):
            # Full-text partial: live transcript as the user speaks (~the engine
            # --delay behind). The Talk UI REPLACES the active turn's text with this.
            # Urgent so it is sent inline and stays ordered ahead of this turn's final.
            # Strip the STT's leading-space (SentencePiece ▁) artifact from the DISPLAY
            # copy only — the forwarded frame stays verbatim for the LLM ctx / ledger /
            # heard-grounding. A whitespace-leading transcript makes OpenClaw's Talk
            # reducer APPEND instead of REPLACE, stacking partials into one bubble.
            # Mirrors CaptionTap's assistant-side .strip().
            if self._activity:
                self._activity.stamp()
            await self.push_frame(
                OutputTransportMessageUrgentFrame(message={
                    "type": "transcript", "role": "user",
                    "text": (frame.text or "").strip(), "final": False,
                }),
                FrameDirection.DOWNSTREAM,
            )
        elif isinstance(frame, TranscriptionFrame):
            # The final also triggers a turn interruption that flushes the paced
            # sink queue; urgent keeps it from being dropped and keeps it ordered
            # after this turn's interims.
            if self._activity:
                self._activity.stamp()
            await self.push_frame(
                OutputTransportMessageUrgentFrame(message={
                    "type": "transcript", "role": "user",
                    "text": (frame.text or "").strip(), "final": True,   # display copy only (see interim)
                }),
                FrameDirection.DOWNSTREAM,
            )


class CaptionTap(FrameProcessor):
    """Assistant captions: playout-paced partials AND per-utterance finals.

    Placed AFTER transport.output(): the output transport queues each word's
    AggregatedTextProgressFrame by its presentation timestamp and its clock task
    releases it DOWNSTREAM exactly when that word's audio plays (verified live:
    lockstep with the sibling TTSTextFrame, same pts), so both the partials and
    the utterance segmentation below are paced to the real voice BY CONSTRUCTION.
    accumulated_text is sliced from the source sentence, so spacing, punctuation,
    and apostrophes are exact — no token-rejoin heuristic (the "Whether'tis"
    class of bug cannot occur).

    Sending from here is the trick: outbound messages are serialized by
    transport.output(), which we sit *after*. But the transport sends an
    OutputTransportMessageUrgentFrame regardless of frame DIRECTION (its isinstance
    check precedes the direction check), so we push captions UPSTREAM and they
    reach the websocket immediately, in emit order.

    Utterance boundaries (see module docstring for why NOT LLM-response
    boundaries), in the order they can fire:
      * LLMFullResponseEndFrame carrying a pts — the TTS synthesizes one per LLM
        turn with pts = the last word's playout time, so the transport clock
        releases it right AT the last word: the earliest correct moment to
        finalize. Beating the user's next interim matters (see hold below);
        waiting for BotStopped (~0.4s+ of silence detection) loses that race.
        pts-guarded against stale Ends from wordless (pure tool-call) turns.
      * a change of the progress frame's context_id — seamless utterance switch.
      * BotStoppedSpeaking — real playout silence; the fallback that covers
        fillers (no LLM turn, no End frame) trailing a segment.

    SEGMENTS: within one audio context, pipecat tracks one sentence at a time
    (one AggregatedTextFrame slot); accumulated_text restarts at each new
    segment_id. The bubble text is completed segments' full source text plus the
    live segment's accumulated text, space-joined (sentence boundaries are
    whitespace in the source). A finalize only fires at a playout boundary, by
    which point the current segment has fully played, so finals use the full
    source text — which also covers a force-completed tail (words whose
    timestamps the engine dropped mid-segment still played as audio). A whole
    segment with NO word timestamps (engine aligner failure for a clause) has no
    progress frames at all and is absent from the bubble; the ledger and LLM
    context are unaffected — accepted trade for deleting the rejoin heuristic.

    USER-HOLD: the Talk UI commits the active assistant bubble the moment the
    user's first interim renders — 1-3 words BEFORE the turn/barge machinery
    decides anything. Any partial we emit in that window reopens a second bubble
    with near-identical text ("seen twice"), which a following tool card then
    renders into. So while user interims are active (shared VoiceActivity stamp)
    partials are HELD — the buffer keeps accumulating, and if the utterance
    survives (no barge), the next emit carries the full text so far. A final due
    during the hold is SKIPPED when it exactly matches the last shown partial
    (the client already committed that bubble; re-sending it would duplicate).

    On barge-in the interrupted utterance's context goes on the dead list: the
    committed bubble is the record of what was heard, no final is emitted, and
    straggler word frames already released by the transport clock are dropped."""

    # Barged context ids kept to reject stragglers; bounded, they're short uuids.
    _DEAD_MAX = 16

    def __init__(self, activity: "VoiceActivity" = None):
        super().__init__()
        self._activity = activity
        self._ctx = None        # context_id of the utterance currently playing
        self._seg = None        # segment_id of the sentence currently playing
        self._seg_text = ""     # that sentence's FULL source text (frame.text)
        self._done = []         # completed sentences' full source text, playout order
        self._acc = ""          # live sentence's played prefix (accumulated_text)
        self._last_sent = ""    # longest partial emitted (forward-only guard)
        self._first_pts = None  # pts of its first word (guards stale End frames)
        self._dead = set()      # barged context ids: drop their late progress frames

    def _user_active(self) -> bool:
        return bool(self._activity) and self._activity.user_active()

    async def _start_interruption(self):
        await super()._start_interruption()
        # Barge-in: the client commits the active bubble itself; the last paced
        # partial already shows what was heard (words played during the user's
        # first words are held, so the bubble ends where they started talking —
        # matching the heard-ledger's overlap model). Emit no final and reject
        # this utterance's stragglers. This override fires out-of-band, before
        # those stragglers.
        if self._ctx is not None:
            self._dead.add(self._ctx)
            while len(self._dead) > self._DEAD_MAX:
                self._dead.pop()
        if _TRACE:
            logger.info(f"[CAP] barge → ctx {self._ctx} dead; bubble is the client's")
        self._reset()

    def _reset(self):
        self._ctx = None
        self._seg = None
        self._seg_text = ""
        self._done = []
        self._acc = ""
        self._last_sent = ""
        self._first_pts = None

    def _snapshot(self) -> str:
        """Bubble text right now: completed sentences + the live played prefix."""
        return " ".join(p for p in [*self._done, self._acc] if p).strip()

    async def _finalize(self, reason: str):
        """Commit the current utterance's bubble.

        A finalize fires only at a playout boundary (context switch, reply-end
        pts, real silence), by which point the live sentence has fully played —
        so it contributes its FULL source text, not just the accumulated prefix
        (covers a force-completed tail whose timestamps the engine dropped)."""
        text = " ".join(p for p in [*self._done, self._seg_text] if p).strip()
        shown, user_active = self._last_sent, self._user_active()
        self._reset()
        if not text:
            return
        if user_active and text == shown:
            # The user is mid-utterance and their interim already committed the
            # bubble showing exactly this text — a final now would render as a
            # duplicate bubble after their message and add nothing.
            if _TRACE:
                logger.info(f"[CAP] FINAL skipped (client committed) tail={text[-40:]!r}")
            return
        if _TRACE:
            logger.info(f"[CAP] FINAL ({reason}) tail={text[-40:]!r}")
        await self.push_frame(
            OutputTransportMessageUrgentFrame(message={
                "type": "transcript", "role": "assistant",
                "text": text, "final": True,
            }),
            FrameDirection.UPSTREAM,
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

        if isinstance(frame, AggregatedTextProgressFrame) \
                and direction == FrameDirection.DOWNSTREAM:
            ctx = frame.context_id
            if ctx in self._dead:
                if _TRACE:
                    logger.info(f"[CAP] dropped straggler acc={frame.accumulated_text!r} ctx={ctx}")
                return
            if self._ctx is not None and ctx != self._ctx:
                # Seamless utterance switch (filler → reply, reply → reply with no
                # audio gap): the previous utterance is done — commit its bubble.
                await self._finalize("utterance switch")
            if self._ctx is None:
                self._first_pts = frame.pts
            self._ctx = ctx
            if self._seg is not None and frame.segment_id != self._seg:
                # New sentence slot: pipecat only advances slots after the
                # previous one completes, so its full source text is played.
                self._done.append(self._seg_text)
            self._seg = frame.segment_id
            self._seg_text = frame.text or ""
            self._acc = (frame.accumulated_text or "").strip()
            snapshot = self._snapshot()
            if len(snapshot) > len(self._last_sent):
                if self._user_active():
                    # The client committed the active bubble at the user's interim;
                    # a partial now would reopen a second one. Keep accumulating —
                    # if this utterance survives, the next emit carries it all.
                    if _TRACE:
                        logger.info(f"[CAP] partial held (user talking) tail={snapshot[-30:]!r}")
                    return
                self._last_sent = snapshot
                await self.push_frame(
                    OutputTransportMessageUrgentFrame(message={
                        "type": "transcript", "role": "assistant",
                        "text": snapshot, "final": False,
                    }),
                    FrameDirection.UPSTREAM,
                )
        elif isinstance(frame, LLMFullResponseEndFrame) and frame.pts is not None:
            # Playout-paced reply end (released at the last word). Ignore stale
            # Ends from wordless turns: their pts predates this utterance's words.
            if (self._acc or self._done) and \
                    (self._first_pts is None or frame.pts >= self._first_pts):
                await self._finalize("reply end")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Real playout silence — covers utterances with no End frame (fillers
            # trailing a segment). No-ops when a boundary already finalized.
            await self._finalize("bot stopped")
