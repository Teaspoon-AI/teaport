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
#                     (0.35s with nothing left to play). The ledger keeps that FIFO:
#                     every audio push -- tagged with its context; fillers and the
#                     thinking-sound bed included, since they take playout time --
#                     with its length, laid out back to back from the window's start.
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

import os
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import List, Optional

from loguru import logger

from teaport_brain.tts_text import CAPTION_LEAD_SECS, has_speech

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
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
from pipecat.observers.base_observer import BaseObserver, FrameProcessed

# LEDGER_TRACE=1 logs the real per-frame sequence (deduped) for diagnosis.
_TRACE = os.getenv("LEDGER_TRACE") == "1"
_TRACE_TYPES = (
    TTSStartedFrame, TTSStoppedFrame, TTSTextFrame, TTSSpeakFrame, BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame, InterruptionFrame, LLMFullResponseStartFrame,
    LLMFullResponseEndFrame, LLMTextFrame, TranscriptionFrame,
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

# FIFO tag for the thinking-sound bed's audio: pushed into the transport by
# thinking_sound.py with no context, it takes playout time and belongs to no turn.
_BED = "<bed>"

_EPS = 1e-6

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

    @property
    def cut_short(self) -> bool:
        """Assistant speech the listener did NOT hear to the end."""
        return self.speaker == "assistant" and self.heard_fraction < HEARD_ALL

    def unheard_tail(self) -> str:
        """The part of the reply the user did NOT hear."""
        return _unheard(self.text, self.heard_text)


def _prefix(text: str, fraction: float) -> str:
    """The first `fraction` of `text`: by words, or by characters for a script
    that writes without spaces (a Mandarin or Japanese reply is one "word")."""
    words = text.split()
    if not words:
        return ""
    if len(words) == 1 and len(text) > 8:
        return text[:max(0, min(len(text), round(len(text) * fraction)))]
    n = max(0, min(len(words), round(len(words) * fraction)))
    return " ".join(words[:n])


def _nospace(s: str) -> str:
    return "".join((s or "").split())


def _unheard(text: str, heard: str) -> str:
    """The part of `text` after the heard prefix, matched IGNORING WHITESPACE.

    The heard text is rebuilt from the TTS's per-word frames, whose spacing is the
    frames' (engine word frames carry none; a CJK voice's tokens are characters), so
    it is a prefix of the raw LLM text only up to whitespace. Comparing the two with
    their spaces removed keeps the tail exact for both; a mismatch beyond spacing
    falls back to a placeholder for display.
    """
    want = _nospace(heard)
    if not want:
        return (text or "").strip()
    i = j = 0
    while i < len(text) and j < len(want):
        if text[i].isspace():
            i += 1
            continue
        if text[i] != want[j]:
            return "(the rest)"
        i += 1
        j += 1
    if j < len(want):
        return "(the rest)"
    return text[i:].strip()


class TranscriptLedger(BaseObserver):
    """Single-writer merged transcript with heard-vs-generated bot tracking.

    `tts` is the pipeline's TTS service and `output` its output transport. With
    them the ledger tells a frame's sightings apart by the processor handling it:
    the LLM stream and spoken notices are read at the TTS (the text it will
    actually speak), a response's End frame sighted again downstream is pipecat's
    post-drain re-push (see the module header), and untagged audio the transport
    is HANDED (the thinking-sound bed) is told from the untagged copies it emits.
    Without them -- the hermetic tests, and any pipeline whose observer carries no
    processor -- every frame counts on its first sighting, the drain signal is
    absent, and a turn is assumed fully synthesized once its response has ended.
    """

    def __init__(self, tts=None, output=None):
        super().__init__()
        self.events: List[Utterance] = []
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
        # out, as [ctx, seconds, push time]; ctx is None on the legacy path and
        # _BED for the thinking sound. _win_t0 is when the open window started
        # playing the head, None while no window is open.
        self._fifo: List[list] = []
        self._win_t0: Optional[float] = None
        self._seen: OrderedDict = OrderedDict()
        self._traced = set()

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
                if self._output is not None and proc is self._output:
                    # Handed TO the transport by something other than the TTS: the
                    # thinking-sound bed. It plays, so it takes playout time; it is
                    # nobody's reply, so it opens no turn.
                    self._push(_BED, dur, t)
                    return
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

    def _llm_side(self, f, t: float):
        """The LLM stream and spoken notices, once each, at the TTS's sighting."""
        if isinstance(f, LLMFullResponseStartFrame):
            self._seq += 1
            self._gen = self._new_entry("llm")
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
            if not gen["text"]:
                # A tool-call response, or one the guard emptied: the TTS opens
                # no context for it, so nothing will claim it.
                return
            if not has_speech(gen["text"]):
                # Text the engine will not synthesize (punctuation, symbols, a
                # bare ellipsis -- run_tts: "nothing synthesizable"): no context
                # opens for it either. Queued, it sat at the queue's head and the
                # NEXT reply's context claimed it; a barge-in before that reply's
                # End had drained then charted the wrong text, and the drain that
                # would have corrected the claim cannot come before the End.
                # The predicate is the engine's own (tts_text.has_speech).
                logger.warning(f"LEDGER response {gen['seq']} has nothing synthesizable; "
                               f"not expected: {gen['text'][:40]!r}")
                return
            self._remember(self._end_ids, f.id, _MAX_END_IDS, gen)
            if gen["turn"] is None:
                self._enqueue(gen)
            # else a live turn holds it and reads the completed text from it.
        elif isinstance(f, TTSSpeakFrame):
            if getattr(f, "append_to_context", True) is False:
                return  # a filler; its TTSStartedFrame carries the flag
            text = (f.text or "").strip()
            if text and has_speech(text):
                # A spoken notice (the STT-busy line, an error read-out): its
                # context will open in turn, and it IS an assistant utterance.
                # (has_speech: as for a response -- one the engine will not
                # synthesize opens no context and must not be expected.)
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
                return
            old = cand["entry"]
            if old is not None and old is not entry:
                logger.warning(
                    f"LEDGER response {old['seq']} was never synthesized; its context "
                    f"was response {entry['seq']}'s. Dropped {old['text'][:40]!r}")
                old["turn"] = None
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
                "ended": False, "drained": False, "turn": None}

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
        """The open turn a FIFO item's audio belongs to (None for a filler's, the
        bed's, or a charted turn's)."""
        ctx = item[0]
        if ctx == _BED:
            return None
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
        turn = {"ctx": fctx, "entry": entry,  # entry None: anonymous, charts nothing
                "t_open": t,          # when the ledger saw the turn begin
                "audio_start": None,  # when its first chunk started playing
                "last_end": None,     # when its last played-out chunk ended
                "pushed": 0.0,        # seconds of its audio pushed to the transport
                "played": 0.0,        # seconds played out in windows already closed
                "synth_done": False,  # all of its audio has been pushed
                "spoken": [],         # (word, pts) from its TTSTextFrames
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
        """BotStoppedSpeaking: the transport played everything it held, then
        0.35s of nothing. Credit each open turn with its chunks, drop the FIFO,
        and chart the turns that are over -- oldest first, stopping at one that
        is not (its synthesis stalled, or its first chunk has not come; the next
        window continues it)."""
        if self._win_t0 is not None:
            for item, _start, end in self._layout():
                turn = self._turn_for_item(item)
                if turn is not None:
                    turn["played"] += item[1]
                    turn["last_end"] = end
        self._fifo.clear()
        self._win_t0 = None
        self._close_ready(t)

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
        for item, start, end in self._layout():
            turn = self._turn_for_item(item)
            if turn is None:
                continue
            played = min(item[1], max(0.0, t - start))
            portions[id(turn)] = portions.get(id(turn), 0.0) + played
            if played > 0:
                turn["last_end"] = min(end, t)
        for turn in list(self._turns):
            self._finish(turn, t, heard=turn["played"] + portions.get(id(turn), 0.0),
                         interrupted=True)
        self._turns.clear()
        self._gen = None
        self._queue.clear()
        self._fifo.clear()
        self._win_t0 = None

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
        if any(p is not None for _, p in spoken):
            # pts are shifted EARLY by the caption lead (see _PTS_LEAD_SECS): a word
            # was truly heard only if pts + lead <= cut, i.e. pts <= cut - lead.
            cut_ns = (t - _PTS_LEAD_SECS) * 1e9
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
            t_end = t
        else:
            t_end = turn["last_end"] if turn["last_end"] is not None else t
        t_end = max(t_start, min(t, t_end))
        if never:
            logger.warning(f"LEDGER assistant reply was never played: {intended[:60]!r}")
        self._add(Utterance("assistant", intended, t_start, t_end,
                            interrupted=interrupted, heard_fraction=frac,
                            heard_text=heard_text))

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
