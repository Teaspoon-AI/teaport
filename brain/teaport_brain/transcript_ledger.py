#
# teaport demo — single-writer merged transcript ledger
#
# A pipeline observer (sees every frame non-intrusively) that builds ONE
# time-ordered transcript: each utterance carries (speaker, t_start, t_end),
# stamped on the shared pipeline clock, so user and bot speech merge into a
# single linear stream and overlaps are explicit rather than competing
# branches.
#
# For the bot it distinguishes what the LLM *generated* (intended to say) from
# what the user actually *heard* as audio. This matters on barge-in: the agent
# otherwise believes it said something the user never heard.
#
# HOW A BOT TURN IS CHARTED
#
#   which text     <- the LLM stream, read where the TTS reads it: the sighting at
#                     the TTS service (when the ledger is given it, see __init__), so
#                     the text is what the TTS was handed -- after LLMTextGuard's
#                     folding, cuts and recovery line, not the model's raw deltas. A
#                     spoken notice (a TTSSpeakFrame that is not a filler) is text
#                     too. Each is an EXPECTED CONTEXT, queued in the order the TTS
#                     receives them; a TTS context claims the oldest when it starts,
#                     because the TTS opens contexts in exactly that order. The claim
#                     is then CONFIRMED when the context drains: pipecat holds a
#                     response's LLMFullResponseEndFrame and re-pushes the SAME frame
#                     after the context's last audio (tts_service.py,
#                     _maybe_reset_word_timestamps), so its second sighting names the
#                     response the context belonged to and says its synthesis is over.
#   playout        <- the output transport plays what it is pushed, in push order,
#                     from BotStartedSpeaking at real time until BotStoppedSpeaking
#                     (the TTSStoppedFrame queued behind the last chunk -- engine_tts
#                     push_stop_frames; without one, pipecat's 3 s idle fallback).
#                     The ledger keeps that FIFO:
#                     every audio push -- tagged with its context; fillers included,
#                     since they take playout time -- with its length, laid out back
#                     to back from the window's start. (The thinking sound's typing is
#                     plain audio, not bot speech: no window, so no part of the layout;
#                     a reply behind it is dated by the transport's BotStarted at the
#                     reply's own first chunk.)
#                     A turn's playout start is where ITS first chunk lands in that
#                     layout, never a frame that could be someone else's (BotStarted
#                     is anonymous); heard time at a cut is the laid-out portion of
#                     its chunks. The transport's rebuilt copy of every chunk it
#                     played (untagged) is recognised and ignored.
#   heard fraction <- heard / (audio length once synthesis is known complete, else
#                     the longer of audio so far and a word-count estimate); the
#                     per-word TTSTextFrames give the exact heard boundary by pts.
#
# Several turns can be open at once -- a reply chained behind another that is still
# queued at the transport. Each closes when ITS audio has played out and its
# synthesis is over, or at the barge-in with its own heard portion; a window
# closing mid-reply (synthesis stalled behind STT on the GPU) leaves the turn open
# and the next window continues it. A context starting proves every earlier
# context ended, since the TTS runs them one at a time.
#
# STAGE 1 — observe-only: logs the merged timeline + heard/generated gap and
# drives nothing.
#

import re
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import List, Optional

from loguru import logger

from teaport_brain.barge_pause import PlayoutPauseFrame
from teaport_brain.settings import setting
from teaport_brain.tts_text import CAPTION_LEAD_SECS, fold_unspeakable, has_speech, unsaid_markers

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed, FrameProcessed

# LEDGER_TRACE=1 logs the real per-frame sequence (deduped) for diagnosis.
_TRACE = setting("LEDGER_TRACE")
_TRACE_TYPES = (
    TTSStartedFrame, TTSStoppedFrame, TTSTextFrame, TTSSpeakFrame, BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame, InterruptionFrame, LLMFullResponseStartFrame,
    LLMFullResponseEndFrame, LLMTextFrame, TranscriptionFrame,
    # Interims too, for the double-talk investigation (2026-09-09). The finals were
    # already traced and told us they are 18x rarer while the bot speaks; only the
    # interims say WHERE that loss is. An interim flowing with no final behind it means
    # the engine is receiving audio and never closing the segment -- a brain-side fix
    # (commit during double-talk). No interim at all means the audio is not reaching the
    # engine despite the tap proving it reached the pipeline, which is a different bug.
    # Cumulative and frequent, so this is deliberately behind LEDGER_TRACE=1 and the
    # formatter truncates to 40 chars like every other traced text frame.
    InterimTranscriptionFrame,
)

# Fallback speaking rate when synthesized audio length isn't fully known yet
# (interruption before TTS finished synthesizing).
_SECONDS_PER_WORD = 0.36

# The engine TTS schedules every word's caption pts EARLY by the shared caption lead
# (tts_text.CAPTION_LEAD_SECS). A word's TRUE playout instant is therefore
# pts + lead, so heard-word accounting backs the lead out of the interruption cut
# before comparing — otherwise every barge-in over-counts "heard" by the lead.
_PTS_LEAD_SECS = CAPTION_LEAD_SECS

# Bounds. Every structure here is fed by a stream that may never present the frame
# that would drain it (a stop frame lost to an interruption, a response the TTS
# never opens a context for), so each evicts its OLDEST entry on overflow -- never
# clears, which used to drop the live entry along with the stale ones.
_MAX_QUEUED = 32       # expected contexts the TTS has not opened yet
_MAX_END_IDS = 64      # response End frames awaiting their post-drain re-push
_MAX_FILLER_CTXS = 512
_MAX_CLOSED_CTXS = 64
_MAX_FIFO = 4096
_MAX_SEEN = 8192

_EPS = 1e-6
# How far before a chunk's modelled end a stop-frame BotStopped may arrive and still
# mean the chunk played out: the transport dequeues the stop frame as soon as the
# last chunk is written, one write period (~20 ms live) before that chunk finishes.
_STOP_WRITE_TOL = 0.05

# heard_fraction at/above this counts as "the listener heard it all" — shared by
# the ledger's own rendering/logging and heard_context's barge-in reconciliation.
HEARD_ALL = 0.99


@dataclass
class Utterance:
    speaker: str  # "user" | "assistant"
    text: str  # user: transcript; assistant: full intended (generated) text
    t_start: float  # seconds on the pipeline clock
    t_end: float
    overlap: bool = False
    # assistant-only — what was actually played out to the user:
    interrupted: bool = False
    heard_fraction: float = 1.0  # 1.0 fully heard; <1 tail cut; 0 not heard
    heard_text: str = ""
    # For HeardContextCorrector's positional anchor. turn_seq: the order its TTS context
    # opened in, which is the order pipecat committed the replies' messages (each at its
    # context's end) -- not always the order they are charted in: a reply the engine
    # failed on is charted at its stop frame, while the reply ahead of it still plays.
    # committed: pipecat committed a message for it -- words of it played, or its
    # context ended and the TTS force-completed its text (a failed reply's included).
    # Only a reply a barge-in cut before its context ended, with nothing played, has none.
    turn_seq: Optional[int] = None
    committed: bool = True

    @property
    def cut_short(self) -> bool:
        """Assistant speech the listener did NOT hear to the end."""
        return self.speaker == "assistant" and self.heard_fraction < HEARD_ALL

    def unheard_tail(self) -> str:
        """The part of the reply the user did NOT hear."""
        rest = _unheard(self.text, self.heard_text)
        return "(the rest)" if rest is None else rest


# A script that writes without spaces -- kana, the CJK ideographs, Thai -- arrives as ONE
# "word" however long the reply, so its heard prefix is taken by characters. Any other
# single token is one word, heard or not: "Understood." cut at 40% is "" (the word
# rounding), not "Under", which HeardContextCorrector would otherwise write into the
# context as what the assistant said. (Length alone was the test once, and every
# Latin word past eight letters took the character path.)
_SPACELESS_SCRIPT = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\u0e00-\u0e7f]")


def _prefix(text: str, fraction: float) -> str:
    """The first `fraction` of `text`: by words, or by characters for a script
    that writes without spaces (a Mandarin or Japanese reply is one "word")."""
    words = text.split()
    if not words:
        return ""
    if len(words) == 1 and _SPACELESS_SCRIPT.search(text):
        return text[:max(0, min(len(text), round(len(text) * fraction)))]
    n = max(0, min(len(words), round(len(words) * fraction)))
    return " ".join(words[:n])


def _nospace(s: str) -> str:
    return "".join((s or "").split())


def _unheard(text: str, heard: str) -> Optional[str]:
    """The part of `text` after the heard prefix, matched by its LETTERS AND DIGITS; None
    when `heard` is not a prefix of it by those.

    The heard text is rebuilt from the TTS's per-word frames, whose spacing is the
    frames' (engine word frames carry none; a CJK voice's tokens are characters), and
    whose text is what the engine was handed: tts_text._normalize_for_tts folds the
    unspeakable characters ("B0‑22‑7A" with U+2011 is "B0-22-7A"), drops the markdown
    and a list's markers ("1. ", "- ") and turns line breaks and ellipses into commas. So
    it is a prefix of the raw LLM text only by its letters and digits. Compared up to
    spacing alone, a typographic hyphen left a telling heard to 89% unjudged and owed
    (2026-10-24 05:38): 17 of that session's 57 cuts aligned, 56 by this (the 57th a
    fragment). A list's numbering may or may not be said -- the normalizer sees it at a
    line's start only when the sentence splitter left it there ("2." alone is said) --
    so its digits are skipped when the heard text does not have them. The heard text's
    own trailing marks ("zero-", "two,") are matched after it, so the rest starts where
    they end. The tail is the raw text's, for both its readers -- the transcript's
    unheard tail (Utterance.unheard_tail) and the rest the cut judge is shown (the
    settle feed).
    """
    text = text or ""
    want = [c.casefold() for c in heard or "" if c.isalnum()]
    if not want:
        return text.strip()
    unsaid = unsaid_markers(text)
    i = j = 0
    while i < len(text) and j < len(want):
        c = text[i]
        i += 1
        if not c.isalnum():
            continue
        if c.casefold() != want[j]:
            if i - 1 in unsaid:
                continue
            return None
        j += 1
    if j < len(want):
        return None
    last = max(k for k, c in enumerate(heard) if c.isalnum())
    for mark in heard[last + 1:]:
        if mark.isspace():
            continue
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text) or fold_unspeakable(text[i]) != mark:
            break
        i += 1
    return text[i:].strip()


class TranscriptLedger(BaseObserver):
    """Single-writer merged transcript with heard-vs-generated bot tracking.

    `tts` is the pipeline's TTS service and `output` its output transport: naming
    either says the pipeline tags its TTS frames with context ids. With `tts` the
    ledger tells a frame's sightings apart by the processor handling it:
    the LLM stream and spoken notices are read at the TTS (the text it will
    actually speak), a response's End frame sighted again downstream is pipecat's
    post-drain re-push (see the module header).
    Without them -- the hermetic tests, and any pipeline whose observer carries no
    processor -- every frame counts on its first sighting, the drain signal is
    absent, and a turn is assumed fully synthesized once its response has ended.
    """

    def __init__(self, tts=None, output=None):
        super().__init__()
        self.events: List[Utterance] = []
        # The settle feed (owed.py, #203): called as on_reply_end(reply, heard, cut=,
        # audio=, text=, rest=) at each LLM reply's OWN end -- the interruption that cut it, or once its
        # synthesis is done and all of its audio has played -- with `reply` the owed.Reply its
        # LLMFullResponseStartFrame carried, `heard` its pts-exact heard text (all of
        # it when it played through; None when cut with no per-word pts) and `text` all
        # of it once its stream ended (None before: _reply_end). Never the
        # heard_text charted below, which on a cut is the LONGER of that prefix and an
        # audio-fraction estimate: right for the corrector, which must not delete words
        # that were heard, and unsafe for closing a record (formal/Owed.tla,
        # ow_ledgerMax). A reply that never reaches the TTS as text (a tool call, one
        # the guard emptied) or never plays is settled as heard nothing.
        self.on_reply_end = None
        # Called as on_reply_text(reply, text) when a reply's stream ends with text, before
        # any of it is heard: owed.py asks its gist judge then (#210), so the verdict is in
        # by the time the reply has played.
        self.on_reply_text = None
        # What has been PUSHED to the TTS of each response (on_push_frame): the guard's
        # output, by its LLMFullResponseStartFrame's id, as {"reply", "parts", "text"} --
        # "text" set once its End was pushed. The TTS reads its input in order, and its
        # synthesis holds that up: a response can have ended at the TTS's door for a
        # second or more before the TTS reads its End (2026-10-24 00:24:26: a one-sentence
        # answer cut at its first word 0.5 s after its End was pushed was read as cut
        # mid-stream, no record, the answer lost -- #211).
        self._pushed: OrderedDict = OrderedDict()
        self._pushing: Optional[dict] = None
        self._turn_seq = 0
        self._tts = tts
        self._output = output
        self._user_start: Optional[float] = None
        # The response streaming now (an entry, see _new_entry), None between.
        self._gen: Optional[dict] = None
        self._seq = 0
        # Expected contexts the TTS has not opened yet, oldest first.
        self._queue: deque = deque()
        # LLMFullResponseEnd frame id -> its response, for the post-drain re-push.
        self._end_ids: OrderedDict = OrderedDict()
        # Open bot turns, in playout order (see _open_turn for the record).
        self._turns: List[dict] = []
        # TTS contexts opened by filler speak frames (append_to_context=False —
        # the consult narrator and tool-ack lines in tools.py, the failure notice
        # in llm_error_speaker.py, the STT-busy line in agent_session.py). Pure
        # audio UX: kept out of the LLM context at the source, and charted by
        # nothing here.
        self._filler_ctxs: OrderedDict = OrderedDict()  # insertion-ordered set
        # Contexts whose turn has been charted; a late frame naming one belongs to
        # no turn (a context re-created after pipecat's stop-frame timeout).
        self._closed_ctxs: OrderedDict = OrderedDict()
        # The pipeline tags its TTS frames with context ids (the engine TTS; any
        # pipeline that names its TTS or transport here). Until the first tagged
        # frame an unnamed pipeline is on the legacy ctx-less path, where untagged
        # audio is the TTS's own push rather than the transport's copy.
        self._tagged = tts is not None or output is not None
        self._legacy_src_set = False
        self._legacy_src = None
        # The output transport's playout FIFO: audio pushed and not yet played
        # out, as [ctx, seconds, push time]; ctx is None on the legacy path.
        # _win_t0 is when the open window started
        # playing the head, None while no window is open.
        self._fifo: List[list] = []
        self._win_t0: Optional[float] = None
        # A playout pause (barge_pause.py; the transport stopped writing and will go on
        # from the same sample): when it began, None while playing. Nothing plays during
        # it, so the layout is read at that instant, and every queued chunk plays that
        # much later.
        self._paused_at: Optional[float] = None
        self._seen: OrderedDict = OrderedDict()
        self._traced = set()

    async def on_push_frame(self, data: FramePushed):
        """A response as it is handed to the TTS (self._pushed). Its End pushed, its whole
        text is known -- the settle feed's `text` for a cut that lands before the TTS has
        read that far (_reply_end), and the moment owed.py's gist judge is asked."""
        if self._tts is None or data.destination is not self._tts:
            return
        f = data.frame
        if isinstance(f, LLMFullResponseStartFrame):
            self._pushing = {"reply": getattr(f, "owed_reply", None), "parts": [], "text": None}
            self._remember(self._pushed, f.id, _MAX_SEEN, self._pushing)
        elif isinstance(f, InterruptionFrame):
            self._pushing = None          # a response cut while it was being pushed
        elif self._pushing is None:
            return
        elif isinstance(f, LLMTextFrame):
            self._pushing["parts"].append(f.text or "")
        elif isinstance(f, LLMFullResponseEndFrame):
            pushed, self._pushing = self._pushing, None
            pushed["text"] = "".join(pushed["parts"]).strip()
            if pushed["text"] and pushed["reply"] is not None and self.on_reply_text is not None:
                try:
                    self.on_reply_text(pushed["reply"], pushed["text"])
                except Exception as e:  # noqa: BLE001 -- an observer must never raise
                    logger.warning(f"LEDGER: the reply-text feed raised {e!r}")

    # ------------------------------------------------------------------ frames

    async def on_process_frame(self, data: FrameProcessed):
        f = data.frame
        t = data.timestamp / 1e9  # pipeline clock ns -> s
        proc = getattr(data, "processor", None)

        if _TRACE and isinstance(f, _TRACE_TYPES) and f.id not in self._traced:
            self._traced.add(f.id)
            info = getattr(f, "text", "")
            info = f" {info[:40]!r}" if info else ""
            # pts, not just arrival: engine-TTS word frames arrive CLUSTERED (the whole
            # clip is pushed at once), so arrival order says nothing about what played.
            # The heard-word cut is made on pts, so pts is what has to be inspectable.
            pts = getattr(f, "pts", None)
            pts_s = f" pts={pts / 1e9:.2f}" if pts else ""
            logger.info(f"TRACE {type(f).__name__} t={t:.2f}{pts_s}{info}")
            if len(self._traced) > _MAX_SEEN:
                self._traced.clear()

        # --- user side ---
        if isinstance(f, VADUserStartedSpeakingFrame):
            if self._user_start is None:
                self._user_start = t
            return

        # --- interruption: every open bot turn ends as cut-off ---
        if isinstance(f, InterruptionFrame):
            self._interrupt(t)
            return

        # --- the LLM stream and spoken notices: read where the TTS reads them ---
        if isinstance(f, (LLMFullResponseStartFrame, LLMTextFrame,
                          LLMFullResponseEndFrame, TTSSpeakFrame)):
            if self._tts is not None and proc is not self._tts:
                if isinstance(f, LLMFullResponseEndFrame):
                    # Any sighting of a response's End AFTER the TTS's own is the
                    # post-drain re-push (the frame keeps its id; upstream sightings
                    # precede the TTS's and are not yet registered).
                    self._end_drained(f, t)
                return
            if f.id in self._seen:
                return
            self._mark_seen(f)
            self._llm_side(f, t)
            return

        if f.id in self._seen:
            return
        self._mark_seen(f)

        if isinstance(f, TranscriptionFrame):
            if not (f.text or "").strip():
                return
            self._add(Utterance("user", f.text.strip(),
                                self._user_start if self._user_start is not None else t, t))
            self._user_start = None

        elif isinstance(f, TTSStartedFrame):
            fctx = getattr(f, "context_id", None)
            if fctx is not None:
                self._tagged = True
            if getattr(f, "append_to_context", True) is False:
                # A FILLER context: the narrator / tool-ack / notice speak frames
                # are pushed with append_to_context=False, and tts_service stamps
                # that onto the context's TTSStartedFrame. A filler is never part
                # of a reply turn: it neither opens one nor is folded into one.
                # Remember the ctx so its word/audio frames are known as a filler's.
                if fctx is not None:
                    self._remember(self._filler_ctxs, fctx, _MAX_FILLER_CTXS)
                self._context_started(t, fctx)
                return
            self._context_started(t, fctx)
            if self._turn_for(fctx) is None:
                self._open_turn(t, fctx)

        elif isinstance(f, TTSTextFrame):
            if self._is_filler(f):
                return
            # Per-word TTSTextFrames (engine TTS) are scheduled on the playout clock
            # and carry that schedule as pts; collecting the ones at/before an
            # interruption's cut gives EXACTLY what the user heard — no estimate.
            # (A whole-reply frame, the legacy ctx-less shape, is not; see _finish.)
            fctx = getattr(f, "context_id", None)
            turn = self._turn_for(fctx)
            if turn is None:
                if fctx is None and self._tagged:
                    return  # a stray untagged word frame on a tagged pipeline
                turn = self._open_turn(t, fctx)
            # Keep each word's SCHEDULED playout time (frame.pts), not its arrival
            # order: our TTS pushes the whole clip at once, so the word frames
            # arrive clustered, but their pts is each word's real playout instant.
            turn["spoken"].append((f.text or "", getattr(f, "pts", None)))
            if getattr(f, "includes_inter_frame_spaces", False):
                turn["ifs"] = True

        elif isinstance(f, TTSAudioRawFrame):
            fctx = getattr(f, "context_id", None)
            dur = ((getattr(f, "num_frames", 0) or 0) / f.sample_rate) if f.sample_rate else 0.0
            if fctx is None:
                if self._tagged:
                    # The transport's rebuild of a chunk it played: no context_id
                    # (base_output re-chunks the bytes into new frames). Not a push.
                    return
                # Legacy ctx-less path: the TTS's own push -- but only from the
                # first processor seen pushing audio. The transport's resampled
                # copy comes from another and used to inflate the reply's length.
                if not self._legacy_src_set:
                    self._legacy_src_set = True
                    self._legacy_src = proc
                elif proc is not self._legacy_src:
                    return
            else:
                self._tagged = True
            if self._is_filler(f):
                self._push(fctx, dur, t)
                return
            turn = self._turn_for(fctx)
            if turn is None:
                # No TTSStartedFrame was seen for this context (a path that routes
                # audio straight through pipecat's audio context): open the turn on
                # its first audio so the reply is still recorded.
                turn = self._open_turn(t, fctx)
            self._push(fctx, dur, t)
            turn["pushed"] += dur
            if turn["audio_start"] is None and self._win_t0 is not None:
                # This turn's first chunk, pushed into a window already playing:
                # it plays after whatever the window holds ahead of it.
                turn["audio_start"] = self._layout()[-1][1]

        elif isinstance(f, BotStartedSpeakingFrame):
            # The transport began playing the head of its queue. Anonymous — it
            # says nothing about WHOSE audio — so it opens no turn; it only dates
            # the window, from which every queued chunk's playout start follows.
            if self._win_t0 is None:
                self._win_t0 = t
                for item, start, _end in self._layout():
                    turn = self._turn_for_item(item)
                    if turn is not None and turn["audio_start"] is None:
                        turn["audio_start"] = start

        elif isinstance(f, TTSStoppedFrame):
            fctx = getattr(f, "context_id", None)
            if fctx is not None and fctx in self._filler_ctxs:
                # The filler's synthesis is over; its context won't be seen again.
                self._filler_ctxs.pop(fctx, None)
                return
            turn = self._turn_for(fctx)
            if turn is None or (fctx is None and self._tagged and turn["ctx"] is not None):
                return  # a foreign context's stop says nothing about the open turns
            # Only THIS turn's stop frame means its full audio length is known.
            turn["synth_done"] = True
            if turn["pushed"] <= 0:
                self._finish(turn, t, heard=0.0, interrupted=True, never=True)

        elif isinstance(f, BotStoppedSpeakingFrame):
            self._window_closed(t)

        elif isinstance(f, PlayoutPauseFrame):
            if f.paused:
                self._pause(t + getattr(f, "lag_secs", 0.0))
            else:
                self._resume(t)

    def _llm_side(self, f, t: float):
        """The LLM stream and spoken notices, once each, at the TTS's sighting."""
        if isinstance(f, LLMFullResponseStartFrame):
            self._seq += 1
            self._gen = self._new_entry("llm")
            self._gen["pushed"] = self._pushed.get(f.id)
            reply = getattr(f, "owed_reply", None)
            if reply is not None:
                reply.claimed = True
                self._gen["reply"] = reply
        elif isinstance(f, LLMTextFrame):
            if self._gen is not None:
                self._gen["parts"].append(f.text or "")
        elif isinstance(f, LLMFullResponseEndFrame):
            gen, self._gen = self._gen, None
            if gen is None:
                # A cancelled completion's End: pipecat pushes it from a finally
                # after the InterruptionFrame that dropped the stream. Nothing of
                # it will be spoken.
                return
            gen["text"] = "".join(gen["parts"]).strip()
            gen["ended"] = True
            if gen["text"] and gen["reply"] is not None and self.on_reply_text is not None:
                try:
                    self.on_reply_text(gen["reply"], gen["text"])
                except Exception as e:  # noqa: BLE001 -- an observer must never raise
                    logger.warning(f"LEDGER: the reply-text feed raised {e!r}")
            if not gen["text"]:
                # A tool-call response, or one the guard emptied: the TTS opens
                # no context for it, so nothing will claim it.
                self._reply_end(gen, None, cut=False, audio=False)
                return
            # Any other text opens a context, synthesizable or not:
            # _push_tts_frames queues the TTSStartedFrame before run_tts, whose
            # "nothing synthesizable" (an emoji, a bare "...") yields no audio
            # and leaves the context to the stop-frame timeout. Left out of the
            # queue, that context claimed the NEXT reply's text when it opened
            # late, charted it never-played, and the next reply then played in
            # full under an anonymous turn (LedgerPlayout.tla,
            # lp_unheard_unspeakable_opens). Expected, it claims its own entry and
            # closes charting nothing (_finish).
            self._remember(self._end_ids, f.id, _MAX_END_IDS, gen)
            if gen["turn"] is None:
                self._enqueue(gen)
            # else a live turn holds it and reads the completed text from it.
        elif isinstance(f, TTSSpeakFrame):
            if getattr(f, "append_to_context", True) is False:
                return  # a filler; its TTSStartedFrame carries the flag
            text = (f.text or "").strip()
            if text:
                # A spoken notice (the STT-busy line, an error read-out): its
                # context will open in turn, and it IS an assistant utterance.
                # (Synthesizable or not, as for a response.)
                self._seq += 1
                entry = self._new_entry("speak")
                entry["text"] = text
                entry["ended"] = True
                self._enqueue(entry)

    def _end_drained(self, f, t: float):
        """A response's End frame sighted again downstream of the TTS: the TTS
        just drained the context it belonged to (the frame keeps its id, see
        tts_service._maybe_reset_word_timestamps). Confirms which response that
        context spoke, and that its synthesis is over."""
        entry = self._end_ids.get(f.id)
        if entry is None or entry["drained"]:
            return
        entry["drained"] = True
        if _TRACE:
            logger.info(f"TRACE LLMFullResponseEndFrame t={t:.2f} drained seq={entry['seq']}")
        turn = entry["turn"]
        if turn is not None and turn not in self._turns:
            return  # its turn is charted already (closed on other evidence)
        if turn is None:
            # Nothing claimed this response, yet its context just drained: the
            # context opened a turn claiming something else -- an older response
            # the TTS never opened a context for -- or nothing. The re-push is
            # the authority: the newest open turn is this response's, and
            # whatever it held was never spoken.
            cand = self._turns[-1] if self._turns else None
            if cand is None or cand["entry"] is entry:
                if entry in self._queue:
                    self._queue.remove(entry)
                if cand is None:
                    logger.warning(
                        f"LEDGER response {entry['seq']} drained with no turn open; "
                        f"dropped {entry['text'][:40]!r}")
                    self._reply_end(entry, None, cut=False, audio=False)
                return
            old = cand["entry"]
            if old is not None and old is not entry:
                logger.warning(
                    f"LEDGER response {old['seq']} was never synthesized; its context "
                    f"was response {entry['seq']}'s. Dropped {old['text'][:40]!r}")
                old["turn"] = None
                self._reply_end(old, None, cut=False, audio=False)
            if entry in self._queue:
                self._queue.remove(entry)
            cand["entry"] = entry
            entry["turn"] = cand
            turn = cand
        turn["synth_done"] = True
        if turn["pushed"] <= 0:
            # Drained without a chunk of audio: nothing of it was ever played.
            self._finish(turn, t, heard=0.0, interrupted=True, never=True)
        else:
            # Its window may have closed already (playout outran a stalled
            # synthesis): then it is over now.
            self._close_ready(t)

    # ------------------------------------------------------------ bookkeeping

    def _new_entry(self, kind: str) -> dict:
        return {"seq": self._seq, "kind": kind, "parts": [], "text": "",
                "ended": False, "drained": False, "turn": None, "reply": None,
                "pushed": None}

    # The settle feed's call sites -- every distinct end an LLM reply can have, each
    # settling it once (_reply_end drops the entry's reply after the first):
    #   _llm_side      its End with no text at the TTS: a tool call, or emptied
    #   _end_drained   its context drained with no turn open, or another response's
    #                  context took its place: never synthesized
    #   _interrupt     cut while still streaming, or queued for a context: heard nothing
    #   _finish        its turn charted with no text, or nothing synthesizable in it
    #   _finish        charted: played through, cut (the pts-exact prefix), or never played
    def _reply_end(self, entry, heard: Optional[str], *, cut: bool, audio: bool):
        """The settle feed (on_reply_end), once per entry, with the reply's whole text
        when its stream ended (at the TTS's door: on_push_frame) -- None when it was cut
        before that: the text as far as it got is a fragment, not the answer a cut
        telling is owed as
        (owed.Owed._cut_answer; formal/Owed.tla, partialRecord) -- and, for a cut with
        a heard prefix, the rest of that text after it (_unheard; None if the prefix is
        not one of it): what the cut judge is shown (owed.Owed._judge)."""
        reply = entry.get("reply") if entry is not None else None
        if reply is None:
            return
        entry["reply"] = None
        text = entry["text"] if entry["ended"] else None
        if text is None and entry.get("pushed") and entry["pushed"]["text"] is not None:
            # Its End was pushed to the TTS before the cut, which the TTS had not read
            # yet: the whole text is known, and the reply is no fragment.
            text = entry["pushed"]["text"] or None
        rest = _unheard(text, heard) if cut and text and heard else None
        if self.on_reply_end is not None:
            try:
                self.on_reply_end(reply, heard, cut=cut, audio=audio, text=text, rest=rest)
            except Exception as e:  # noqa: BLE001 -- an observer must never raise
                logger.warning(f"LEDGER: the settle feed raised {e!r}")

    def _enqueue(self, entry: dict):
        self._queue.append(entry)
        while len(self._queue) > _MAX_QUEUED:
            old = self._queue.popleft()
            logger.warning(f"LEDGER dropped unspoken response {old['seq']}: "
                           f"{old['text'][:40]!r} (queue overflow)")

    @staticmethod
    def _remember(od: OrderedDict, key, cap: int, value=None):
        od[key] = value
        while len(od) > cap:
            od.popitem(last=False)

    def _mark_seen(self, f):
        self._remember(self._seen, f.id, _MAX_SEEN)
        sib = getattr(f, "broadcast_sibling_id", None)
        if sib is not None:
            # BotStarted/BotStopped are broadcast as a downstream/upstream pair
            # with distinct ids; the ledger sees both.
            self._remember(self._seen, sib, _MAX_SEEN)

    def _is_filler(self, frame) -> bool:
        """True for a frame from a FILLER TTS context — a speak frame the brain
        plays as pure audio UX (the consult narrator, the tool-ack lines, the
        pipeline's own notices), pushed with append_to_context=False. Fillers are kept out of the LLM context at
        the source, and the ledger charts nothing for them: they are not part of
        anything the assistant *said* as a reply. Identified by the flag where the
        frame carries one (TTSStartedFrame, TTSTextFrame) and by the context
        remembered from the filler's started frame otherwise (audio)."""
        fctx = getattr(frame, "context_id", None)
        if fctx is not None and fctx in self._filler_ctxs:
            return True
        return getattr(frame, "append_to_context", True) is False

    def _turn_for(self, fctx) -> Optional[dict]:
        """The open turn a frame naming context `fctx` belongs to. A frame naming
        NO context (the legacy ctx-less path) goes to the newest open turn; a frame naming a DIFFERENT context than every open turn
        is foreign and belongs to none."""
        if fctx is None:
            return self._turns[-1] if self._turns else None
        for turn in reversed(self._turns):
            if turn["ctx"] == fctx:
                return turn
        return None

    def _turn_for_item(self, item) -> Optional[dict]:
        """The open turn a FIFO item's audio belongs to (None for a filler's or a
        charted turn's)."""
        ctx = item[0]
        if ctx is None:
            for turn in reversed(self._turns):
                if turn["ctx"] is None:
                    return turn
            return None
        for turn in reversed(self._turns):
            if turn["ctx"] == ctx:
                return turn
        return None

    def _open_turn(self, t: float, fctx) -> dict:
        """Open a bot turn for TTS context `fctx` (None on the legacy path).

        Which text is it speaking? The TTS opens contexts in the order it received
        their text, so the oldest expected context not yet opened (the queue's
        head) is this one's. With nothing queued it is the response streaming
        now -- the streaming-TTS shape, where the first audio lands before the
        LLMFullResponseEndFrame: the turn is LIVE and completes its text on the
        response end, or charts the partial if barged first. A response with no
        text yet cannot have audio, so an audio-only context opens ANONYMOUS
        (entry None) and charts nothing rather than taking the next reply's words.
        """
        entry = None
        if fctx is not None and fctx in self._closed_ctxs:
            # A context re-created after pipecat's stop-frame timeout, whose turn
            # is already charted: the tail belongs to no expected context.
            pass
        elif self._queue:
            entry = self._queue.popleft()
        elif (self._gen is not None and self._gen["turn"] is None
              and "".join(self._gen["parts"]).strip()):
            entry = self._gen  # live: the entry completes on the response end
        self._turn_seq += 1
        turn = {"ctx": fctx, "entry": entry,  # entry None: anonymous, charts nothing
                "seq": self._turn_seq,  # opening order (Utterance.turn_seq)
                "t_open": t,          # when the ledger saw the turn begin
                "audio_start": None,  # when its first chunk started playing
                "last_end": None,     # when its last played-out chunk ended
                "pushed": 0.0,        # seconds of its audio pushed to the transport
                "played": 0.0,        # seconds played out in windows already closed
                "synth_done": False,  # all of its audio has been pushed
                "spoken": [],         # (word, pts) from its TTSTextFrames
                # Playout pauses since it opened: added to the cut before comparing
                # pts. A turn that opens after a pause starts at 0 -- the TTS moved
                # the anchor of a reply queued behind the paused one by the pause
                # (engine_tts._freeze_playout), so its pts already account for it.
                "pts_shift": 0.0,
                "ifs": False}         # its word frames carry their own spacing
        if entry is not None:
            entry["turn"] = turn
        self._turns.append(turn)
        return turn

    def _context_started(self, t: float, fctx):
        """A TTS context began (filler or not). The TTS drains contexts one at a
        time, so every open turn of ANOTHER context is fully synthesized -- and one
        that never got a chunk never will."""
        for turn in list(self._turns):
            if turn["ctx"] == fctx:
                continue  # a context re-created under its own id: the same turn
            if turn["ctx"] is None and fctx is None:
                continue  # legacy: one ctx-less stream
            turn["synth_done"] = True
            if turn["pushed"] <= 0:
                self._finish(turn, t, heard=0.0, interrupted=True, never=True)

    # ---------------------------------------------------------------- playout

    def _push(self, ctx, dur: float, t: float) -> list:
        item = [ctx, dur, t]
        self._fifo.append(item)
        if len(self._fifo) > _MAX_FIFO:
            del self._fifo[0]
        return item

    def _layout(self):
        """Where each queued chunk plays: back to back from the window's start,
        in push order, and never before it was pushed (a chunk that arrived while
        the queue had already run dry starts when it arrived). Empty while no
        window is open -- nothing is playing."""
        out = []
        if self._win_t0 is None:
            return out
        cur = self._win_t0
        for item in self._fifo:
            start = max(cur, item[2])
            end = start + item[1]
            out.append((item, start, end))
            cur = end
        return out

    def _window_closed(self, t: float):
        """BotStoppedSpeaking. The transport says this on the TTSStoppedFrame
        queued behind a context's last chunk (engine_tts push_stop_frames) -- at
        the end of EACH context, so when another context's chunks are already
        queued behind, the next one starts 1 ms later and "the transport played
        everything it held" is not what this means (live 2026-09-04 21:26: a
        consult delivery queued behind a 44 s reply was credited in full at the
        reply's end, charted [98.3-98.3] before a word of it had played, and
        would have been uncuttable). Credit each open turn with the portion of
        its chunks that had played by `t`; whatever is still ahead stays queued,
        re-timed to play from `t`, and the window stays open for it. Then chart
        the turns that are over -- oldest first, stopping at one that is not
        (still queued, its synthesis stalled, or its first chunk has not come;
        the next window continues it)."""
        self._credit(self._play_t(t))
        self._close_ready(t)

    def _play_t(self, t: float) -> float:
        """Playout time at `t`: `t`, or the instant a pause still in force began."""
        return min(t, self._paused_at) if self._paused_at is not None else t

    def _credit(self, t: float):
        """Credit each open turn with what of its queued audio had played by `t`, and
        re-time the rest to play from `t` (see _window_closed)."""
        if self._win_t0 is not None:
            remaining = []
            for item, start, end in self._layout():
                # The stop frame is dequeued once the last chunk has been WRITTEN,
                # a write period before the layout says it has finished playing
                # (live: 20 ms). A chunk that is over, or that close to over, is
                # played in full; only what is genuinely still ahead stays queued
                # -- otherwise the turn waited for the next event to close, and a
                # barge-in into the reply behind it charted THIS one as a cut.
                if end <= t + _STOP_WRITE_TOL:
                    played = item[1]
                else:
                    played = min(item[1], max(0.0, t - start))
                turn = self._turn_for_item(item)
                if turn is not None and played > 0:
                    turn["played"] += played
                    turn["last_end"] = min(end, t)
                if item[1] - played > _EPS:
                    remaining.append([item[0], item[1] - played, t])
            self._fifo = remaining
            self._win_t0 = t if remaining else None
        else:
            self._fifo.clear()

    def _pause(self, t: float):
        """The transport stopped writing: credit what played up to now and hold the
        layout there. Turns stay open -- the rest of their audio is still to play --
        and so does the window: the transport is still speaking, and no
        BotStartedSpeaking will reopen it."""
        if self._paused_at is not None:
            return
        was_open = self._win_t0 is not None
        self._credit(t)
        if was_open and self._win_t0 is None:
            self._win_t0 = t
        self._paused_at = t
        self._close_ready(t)

    def _resume(self, t: float):
        """Writing again: everything still queued plays from now, and every open turn's
        words play that much later than they were scheduled."""
        if self._paused_at is None:
            return
        shift = max(0.0, t - self._paused_at)
        self._paused_at = None
        if self._win_t0 is not None:
            self._win_t0 = t
        for turn in self._turns:
            # Only a turn with audio already pushed was scheduled on the pre-pause clock.
            # One whose first audio comes after the resume is anchored by the TTS on its
            # moved clock (engine_tts._freeze_playout moves _prev_audio_end_ns), so
            # shifting it here would count the pause twice.
            if turn["pushed"] > 0:
                turn["pts_shift"] += shift

    def _close_ready(self, t: float):
        """Chart every open turn that is over -- all of its audio played out and
        its synthesis known complete -- oldest first, stopping at the first that
        is not, so turns are charted in playout order."""
        for turn in list(self._turns):
            if turn["pushed"] <= 0 or turn["played"] < turn["pushed"] - _EPS:
                break
            entry = turn["entry"]
            done = (entry is None or turn["synth_done"]
                    or (self._tts is None and entry["ended"]))
            if not done:
                break
            self._finish(turn, t, heard=turn["played"], interrupted=False)

    def _interrupt(self, t: float):
        """The user barged in: every open turn ends now, with the portion of its
        audio the layout says had played. The completion is cancelled (its End
        frame still comes, empty now) and the TTS flushes every queued context,
        so nothing expected will be spoken -- neither text may wait for a later
        turn to claim it."""
        portions: dict = {}
        tp = self._play_t(t)
        for item, start, end in self._layout():
            turn = self._turn_for_item(item)
            if turn is None:
                continue
            played = min(item[1], max(0.0, tp - start))
            portions[id(turn)] = portions.get(id(turn), 0.0) + played
            if played > 0:
                turn["last_end"] = min(end, tp)
        for turn in list(self._turns):
            self._finish(turn, t, heard=turn["played"] + portions.get(id(turn), 0.0),
                         interrupted=True)
        # Replies cut before their TTS context opened -- still streaming, or queued for
        # one: heard nothing (Owed.tla CutGot "gen").
        for entry in [self._gen, *self._queue]:
            self._reply_end(entry, None, cut=True, audio=False)
        self._turns.clear()
        self._gen = None
        self._queue.clear()
        self._fifo.clear()
        self._win_t0 = None
        self._paused_at = None

    # ------------------------------------------------------------------ chart

    def _finish(self, turn: dict, t: float, heard: float, interrupted: bool,
                never: bool = False):
        if turn in self._turns:
            self._turns.remove(turn)
        if turn["ctx"] is not None:
            self._remember(self._closed_ctxs, turn["ctx"], _MAX_CLOSED_CTXS)
        entry = turn["entry"]
        if entry is not None:
            # The entry keeps pointing at this (now charted) turn: a live turn's
            # End frame arriving after its playout must find it spoken, not queue
            # the text for the next context to claim.
            intended = (entry["text"] if entry["ended"]
                        else "".join(entry["parts"]).strip())
        else:
            intended = ""
        if not intended:
            # Nothing was ever attributed to this turn: there is nothing to chart.
            self._reply_end(entry, None, cut=interrupted, audio=False)
            return
        if turn["pushed"] <= 0 and not has_speech(intended) and (never or entry["ended"]):
            # No audio, and nothing synthesizable in its text (tts_text.has_speech,
            # the engine's own predicate): a whole response (an emoji, a bare
            # "..."), or the part of one that reached the TTS before its context
            # timed out. Nothing of it was heard, so a heard-0 chart would hand
            # HeardContextCorrector a cut -- and it anchors by position: it deleted
            # the reply that played after this one (LedgerPlayout.tla,
            # lp_unspeakable_charted). (pipecat does commit its text: the TTS
            # force-completes it at the stop. An emoji left in the context is harmless.) A barge-in into a response still streaming is
            # charted as any cut: the rest of it may have had words, and the
            # follow-up loop reads that cut.
            logger.warning(f"LEDGER response {entry['seq']} had nothing synthesizable; "
                           f"not charted: {intended[:40]!r}")
            self._reply_end(entry, None, cut=interrupted, audio=False)
            return
        audio_dur = turn["pushed"]
        # If synthesis was cut short, audio_dur underestimates the intended
        # length; fall back to a word-count estimate so heard_fraction isn't
        # inflated. (Also with NO audio at all, where trusting synth_done would
        # make full_dur 0.)
        text_dur = len(intended.split()) * _SECONDS_PER_WORD
        full_dur = (audio_dur if turn["synth_done"] and audio_dur > 0
                    else max(audio_dur, text_dur))
        heard = max(0.0, heard)
        if interrupted and not never and turn["synth_done"] and heard >= audio_dur - _EPS > 0:
            # Every chunk of it had played when the cut landed (a reply queued
            # ahead of the one the user actually cut): heard in full.
            interrupted = False
        if interrupted:
            frac = min(1.0, heard / full_dur) if full_dur > 0 else 0.0
        else:
            frac = 1.0
        # Heard text from per-word TTSTextFrames (engine TTS): keep each word whose
        # SCHEDULED playout time (frame.pts) is at/before the cut. The frames
        # arrive clustered (our TTS pushes the whole clip at once), so we filter
        # by pts, not arrival order. Fall back to arrival order if frames carry
        # no pts, and to the played-fraction estimate with no per-word frames
        # (a single whole-reply frame, the legacy ctx-less shape).
        spoken = turn["spoken"]
        est = _prefix(intended, frac)  # reliable played-audio-fraction estimate
        # What the settle feed reads: the whole reply when it played through; on a cut,
        # the pts prefix alone, or nothing at all without per-word pts (_reply_end).
        exact = intended if not interrupted else None
        if any(p is not None for _, p in spoken):
            # pts are shifted EARLY by the caption lead (see _PTS_LEAD_SECS): a word
            # was truly heard only if pts + lead <= cut, i.e. pts <= cut - lead.
            # A playout pause stops the clock the words were scheduled on: the cut is
            # read where playout stood (the pause's start, if one is in force), on the
            # schedule's own time (less the pausing it has had since).
            cut_ns = (self._play_t(t) - turn["pts_shift"] - _PTS_LEAD_SECS) * 1e9
            # Rejoin the matched words with the spacing the frames declare: a
            # frame set that includes its inter-frame spaces (a CJK voice's
            # character tokens) is concatenated; one that does not (the engine's
            # per-word frames, which carry no spaces at all -- "One,","two,") is
            # space-joined, or the count read 1 however many words matched.
            sep = "" if turn["ifs"] else " "
            pts_heard = sep.join(
                (txt or "").strip()
                for txt, p in spoken
                if p is not None and p <= cut_ns and (txt or "").strip())
            # Prefer exact word-timing, but never report LESS than the played-audio
            # fraction implies — guards against a misaligned pts baseline (which
            # would silently drop words the user did hear). Compared by characters,
            # not words, so a voice whose tokens are characters compares too.
            heard_text = pts_heard if len(_nospace(pts_heard)) >= len(_nospace(est)) else est
            if interrupted:
                exact = pts_heard
            if _TRACE:
                logger.info(
                    f"TRACE cut t={t:.2f} audio_start={turn['audio_start']} "
                    f"heard={heard:.2f} pushed={audio_dur:.2f} played_before={turn['played']:.2f} "
                    f"text_dur={text_dur:.2f} synth_done={turn['synth_done']} "
                    f"full_dur={full_dur:.2f} frac={frac:.2f} "
                    f"cut={cut_ns / 1e9:.2f} spoken={len(spoken)} "
                    f"pts={[p / 1e9 if p is not None else None for _, p in spoken]} "
                    f"pts_heard={len(pts_heard.split())} est={len(est.split())} "
                    f"-> {len(heard_text.split())} words"
                )
        elif len(spoken) > 1:
            heard_text = "".join(txt for txt, _ in spoken).strip()
        else:
            heard_text = est
        # The utterance's window is its PLAYOUT: from where its first chunk started
        # to where its last played-out chunk ended (a foreign filler chained after
        # it may still be playing when the closing frame arrives; the turn's own
        # end is when ITS audio finished). A turn cut before any of it played, or
        # never played at all, is a point at the time the ledger saw it begin.
        t_start = turn["audio_start"] if turn["audio_start"] is not None else turn["t_open"]
        if never or turn["audio_start"] is None:
            t_end = t_start if never else t
        elif interrupted:
            t_end = self._play_t(t)
        else:
            t_end = turn["last_end"] if turn["last_end"] is not None else t
        t_end = max(t_start, min(t, t_end))
        if never:
            logger.warning(f"LEDGER assistant reply was never played: {intended[:60]!r}")
        self._add(Utterance("assistant", intended, t_start, t_end,
                            interrupted=interrupted, heard_fraction=frac,
                            heard_text=heard_text, turn_seq=turn["seq"],
                            committed=(not interrupted or never or turn["synth_done"]
                                       or heard > 0)))
        # The settle feed (on_reply_end), here and nowhere earlier: a reply that played
        # through is charted only once its synthesis is done and all of its audio has
        # played (_close_ready) -- not at a BotStoppedSpeaking that came mid-reply, which
        # the transport's 3 s idle fallback sends on a TTS stall.
        played_any = (not never and audio_dur > 0 and (not interrupted or heard > 0))
        # Cut after its last word: every word of it was heard, which is the reply heard
        # to its end (formal/Owed.tla, RUNCLOSE = "about": g = "all" for a cut too).
        whole = (interrupted and played_any and exact is not None
                 and len(_nospace(exact)) >= len(_nospace(intended)))
        self._reply_end(entry, exact if played_any else None,
                        cut=interrupted and not whole, audio=played_any)

    def _add(self, u: Utterance):
        for e in self.events:
            if e.speaker != u.speaker and u.t_start < e.t_end and e.t_start < u.t_end:
                u.overlap = True
                e.overlap = True
        self.events.append(u)
        self._log(u)

    def _log(self, u: Utterance):
        if u.cut_short:
            unheard = u.unheard_tail()
            logger.info(
                f"LEDGER +assistant CUT heard~{u.heard_fraction*100:.0f}%: "
                f"heard {u.heard_text!r} | NOT heard {unheard!r}"
            )
        else:
            logger.info(
                f"LEDGER +{u.speaker}{' OVERLAP' if u.overlap else ''}: "
                f"[{u.t_start:.1f}-{u.t_end:.1f}] {u.text!r}"
            )
