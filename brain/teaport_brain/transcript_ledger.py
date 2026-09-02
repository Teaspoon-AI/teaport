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
#   intended text  <- LLMTextFrame stream. NOT TTSTextFrame: engines without word
#                     timestamps emit that as a single frame at the END of
#                     synthesis, so an early barge-in leaves it empty (verified
#                     against the live frame trace). With streaming TTS the first
#                     audio can arrive BEFORE the LLM text finishes, so a bot turn
#                     snapshotted mid-generation is completed on the response end
#                     (see _new_bot / LLMFullResponseEndFrame).
#   playout window <- BotStartedSpeaking -> (InterruptionFrame | BotStopped).
#   heard fraction <- played duration / intended duration; the unheard tail is
#                     flagged so the LLM can later be told what didn't land.
#
# STAGE 1 — observe-only: logs the merged timeline + heard/generated gap and
# drives nothing.
#

import os
from collections import deque
from dataclasses import dataclass
from typing import List, Optional

from loguru import logger

from teaport_brain.tts_text import CAPTION_LEAD_SECS

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FrameProcessed

# LEDGER_TRACE=1 logs the real per-frame sequence (deduped) for diagnosis.
_TRACE = os.getenv("LEDGER_TRACE") == "1"
_TRACE_TYPES = (
    TTSStartedFrame, TTSStoppedFrame, TTSTextFrame, BotStartedSpeakingFrame,
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
        """The part of the reply the user did NOT hear. Word-level heard text
        (engine TTS) isn't a char-prefix of the raw LLM text, so fall back gracefully
        for display."""
        return _unheard(self.text, self.heard_text)


def _prefix_words(text: str, fraction: float) -> str:
    words = text.split()
    if not words:
        return ""
    n = max(0, min(len(words), round(len(words) * fraction)))
    return " ".join(words[:n])


def _unheard(text: str, heard: str) -> str:
    # The part of the reply the user did NOT hear. Word-level heard text (engine TTS)
    # isn't a char-prefix of the raw LLM text, so fall back gracefully for display.
    return text[len(heard):].strip() if text.startswith(heard) else "(the rest)"


class TranscriptLedger(BaseObserver):
    """Single-writer merged transcript with heard-vs-generated bot tracking."""

    def __init__(self):
        super().__init__()
        self.events: List[Utterance] = []
        self._user_start: Optional[float] = None
        self._gen_acc: Optional[List[str]] = None  # current LLM response text
        # Whether a turn has already taken the in-flight response's text (a live
        # turn). Its End frame must then NOT queue the text as unspoken: a reply
        # whose playout ends before its End arrives (a generation stall longer than
        # the transport's silence timeout) is charted from the partial, and the full
        # text queued afterwards was claimed by the NEXT reply's context.
        self._gen_claimed = False
        # Completed replies not yet spoken, oldest first, as (seq, text). The TTS
        # service synthesizes responses in the order it received them, so the turn
        # that opens next is the OLDEST unspoken reply's; a turn takes its text off
        # the front when it opens. A single slot here lost the older text whenever
        # two completions finished before the first's TTS began (text plus a tool
        # call, a fast tool, a slow first chunk) and charted that reply under the
        # newer one's words.
        self._pending: deque = deque()
        self._pending_seq: int = 0  # response counter; a turn records the seq it took
        self._bot: Optional[dict] = None  # active bot (TTS) utterance
        # TTS contexts opened by filler speak frames (append_to_context=False —
        # the consult narrator / tool-ack lines, see tools.py). Pure audio UX:
        # kept out of the LLM context at the source, and charted by nothing here.
        self._filler_ctxs: dict = {}  # insertion-ordered set: oldest first
        # The output transport's playout window, as far as the ledger can see it.
        # BotStarted/BotStopped frames are anonymous, so what identifies a window is
        # the FIRST audio pushed since the previous one closed: the transport plays
        # in push order, and the ledger sees every push before the transport can
        # open a window for it. That head decides whether a window is a filler's
        # (no bot turn may open on it, however anonymous the frames inside it are)
        # and, with the seconds of audio queued ahead, where a reply that chains
        # into someone else's window actually begins playing. Replaces a
        # last-started-context boolean, which named whichever context's started
        # frame came LAST — not the one whose audio the window was opened for.
        self._window_open = False
        self._window_t0: Optional[float] = None
        self._window_head_seen = False
        self._window_head_filler = False
        self._window_dur = 0.0  # seconds of tagged audio pushed into this window
        self._seen = set()
        self._traced = set()

    async def on_process_frame(self, data: FrameProcessed):
        f = data.frame
        t = data.timestamp / 1e9  # pipeline clock ns -> s

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
            if len(self._traced) > 8192:
                self._traced.clear()

        # --- user side ---
        if isinstance(f, VADUserStartedSpeakingFrame):
            if self._user_start is None:
                self._user_start = t
            return

        # --- interruption: end an in-flight bot utterance as cut-off ---
        if isinstance(f, InterruptionFrame):
            if self._bot is not None:
                self._finish_bot(t, interrupted=True)
            # The interruption cancels the in-flight completion (its End frame still
            # arrives, from a finally, with the partial text) and flushes every queued
            # TTS context. Neither text will be spoken now, so neither may be left
            # for the next turn to claim: the partial was charted above as the cut
            # turn's intended, and a completed reply whose TTS had not started is
            # gone with the flush. Leaving them armed let the next filler's untagged
            # transport copy open a turn on the cut reply's text and chart it a
            # second time, complete — and hand that text to the NEXT reply's turn.
            self._gen_acc = None
            self._pending.clear()
            self._reset_window()
            return

        if f.id in self._seen:
            return

        if isinstance(f, TranscriptionFrame):
            if not (f.text or "").strip():
                return
            self._seen.add(f.id)
            self._add(Utterance("user", f.text.strip(),
                                self._user_start if self._user_start is not None else t, t))
            self._user_start = None

        # --- generated (intended) bot text from the LLM stream ---
        elif isinstance(f, LLMFullResponseStartFrame):
            self._seen.add(f.id)
            self._gen_acc = []
            self._gen_claimed = False
        elif isinstance(f, LLMTextFrame):
            self._seen.add(f.id)
            if self._gen_acc is not None:
                self._gen_acc.append(f.text or "")
        elif isinstance(f, LLMFullResponseEndFrame):
            self._seen.add(f.id)
            txt = "".join(self._gen_acc or []).strip()
            if txt:  # tool-call responses have no text; nothing to queue
                self._pending_seq += 1
                if self._bot is not None and self._bot.get("intended_live"):
                    # This response's playout started before its text finished
                    # streaming; complete the mid-generation snapshot with the
                    # full reply so heard_fraction has the right denominator.
                    # The live turn IS this reply's, so it is not queued.
                    self._bot["intended"] = txt
                    self._bot["intended_live"] = False
                    self._bot["gen_seq"] = self._pending_seq
                elif not self._gen_claimed:
                    self._pending.append((self._pending_seq, txt))
                # else: a live turn already spoke (and charted) this response.
            self._gen_acc = None

        # --- bot playout: associate the generated text with the audio ---
        elif isinstance(f, TTSStartedFrame):
            self._seen.add(f.id)
            fctx = getattr(f, "context_id", None)
            if getattr(f, "append_to_context", True) is False:
                # A FILLER context: the narrator / tool-ack speak frames are pushed
                # with append_to_context=False (tools.py), and tts_service stamps
                # that onto the context's TTSStartedFrame. A filler is never part
                # of a reply turn — it must neither open one (a phantom turn in the
                # silence before a reply swallowed that reply's text) nor adopt an
                # open one's identity (the reply's own frames then read as foreign
                # and a barged reply was recorded fully heard). Remember the ctx so
                # the filler's word/audio frames are dropped too — and so its audio
                # is known to be a filler's when it becomes a window's head.
                if fctx is not None:
                    self._filler_ctxs[fctx] = None
                    if len(self._filler_ctxs) > 512:
                        # A filler whose stop frame was lost (an interruption
                        # discards it) leaks its entry. Evict the OLDEST, never
                        # clear: clearing dropped the context that had just been
                        # added — the live filler — and its frames then folded
                        # into the open reply.
                        del self._filler_ctxs[next(iter(self._filler_ctxs))]
            elif self._bot is None:
                self._bot = self._new_bot(t)
                # The opening started frame names this turn's context in the normal
                # path (tts_service creates TTSStartedFrame(context_id=...)).
                self._bot["ctx"] = fctx
            elif fctx is None or self._bot["ctx"] is None:
                # A mid-turn TTSStarted for the SAME turn (an audio context
                # re-created after a stop-frame timeout keeps its context_id) or a
                # ctx-less path: keep the open turn — clobbering it would discard
                # the samples/words already played. Adopt the ctx if none yet.
                if fctx is not None:
                    self._bot["ctx"] = fctx
            elif fctx != self._bot["ctx"]:
                # A NEW spoken context chained straight onto the open turn with no
                # BotStopped between (fast tool flow: the ack's context and the
                # answer's context play back-to-back inside one BotStarted window).
                # The open turn is over: chart it and start a turn for the new
                # context, which claims the newer response's text. Rejecting these
                # frames as foreign made the whole second reply vanish. (Any speak
                # frame the brain plays INSIDE a reply must carry
                # append_to_context=False, or it would close that reply early here.)
                events_before = len(self.events)
                self._finish_bot(t, interrupted=False)
                self._bot = self._new_bot(t)
                self._bot["ctx"] = fctx
                if len(self.events) > events_before:
                    # Gapless chain: the new context's playout begins where the
                    # previous turn's ended (its charted t_end) — no second
                    # BotStartedSpeaking comes mid-window, and this started frame's
                    # own arrival is synthesis time, while the previous turn may
                    # still be playing out downstream. Without this the chained
                    # turn had no audio_start at all and a barge-in into it read
                    # as heard_fraction 0.
                    prev_end = self.events[-1].t_end
                    self._bot["t_start"] = prev_end
                    self._bot["audio_start"] = prev_end
        elif isinstance(f, TTSTextFrame):
            self._seen.add(f.id)
            # Per-word TTSTextFrames (engine TTS) are scheduled on the playout clock,
            # so they arrive at the ledger as each word is spoken. Collecting the
            # ones that arrive before an interruption gives EXACTLY what the user
            # heard — no estimate. (sherpa pushes one whole-reply frame instead.)
            if not self._is_filler(f):
                self._ensure_bot(t)
                if self._bot is not None and self._ctx_ok(f):
                    # Keep each word's SCHEDULED playout time (frame.pts), not its
                    # arrival order: our TTS pushes the whole clip at once, so the
                    # word frames arrive clustered, but their pts is each word's
                    # real playout instant — which is what tells us what was heard.
                    self._bot["spoken"].append((f.text or "", getattr(f, "pts", None)))
        elif isinstance(f, TTSAudioRawFrame):
            self._seen.add(f.id)
            fctx = getattr(f, "context_id", None)
            filler = self._is_filler(f)
            dur = ((getattr(f, "num_frames", 0) or 0) / f.sample_rate) if f.sample_rate else 0.0
            if not self._window_head_seen:
                # The first audio since the window closed: what the next window
                # opens FOR. Filler-ness is decided now, while the filler's context
                # is still in _filler_ctxs (its stop frame drains it before playout).
                self._window_head_seen = True
                self._window_head_filler = filler
            if not filler:
                # Only audio that NAMES a context may open a turn. The transport's
                # untagged rebuild of a chunk names nothing, and in a filler's window
                # it is the filler's — the frame that used to open a phantom turn on
                # whatever text was pending and chart it off the filler's playout.
                # (A reply's own audio is tagged on every path — engine_tts.py yields
                # it with context_id — so nothing real is refused.)
                if fctx is not None or not self._window_head_filler:
                    self._ensure_bot(t)
                if self._bot is not None and self._audio_ok(f):
                    if self._bot.get("queue_ahead") is None:
                        # This turn's first accepted audio (tagged, or untagged on the
                        # ctx-less legacy path): it plays after whatever this window
                        # already holds. A reply chained behind a filler
                        # gets no BotStarted of its own, so this offset is the only
                        # way to know when its playout begins — without it a barge-in
                        # into such a reply read as heard_fraction 0.
                        self._bot["queue_ahead"] = self._window_dur
                        if self._window_open and self._bot["audio_start"] is None:
                            self._bot["audio_start"] = self._window_t0 + self._window_dur
                    # Per PUSHING PROCESSOR, and take the max at the cut — never a
                    # running total. In a ctx-less pipeline the same audio is pushed
                    # twice: once by the TTS service at 24 kHz and again by the output
                    # transport, which resamples to the pipeline's 16 kHz. Those are
                    # different frame ids, so id-dedup cannot catch them, and bucketing
                    # per SAMPLE RATE does not either — the resampled frames still
                    # carry sample_rate=24000 while num_frames counts 16 kHz samples,
                    # so both land in one bucket and inflate it by (24000+16000)/24000
                    # = 1.67x. Every processor sees the whole reply exactly once, so
                    # the max across processors IS the reply's duration, whatever any
                    # one of them labels its rate. (A ctx-tagged turn counts only the
                    # TTS service's tagged copy — see _audio_ok — so it has one
                    # bucket; the max is then just that bucket.)
                    #
                    # Live 2026-08-25: a 9.5s count measured as audio_dur=15.8, frac
                    # 0.39 instead of 0.65, so a barge-in at "thirteen" was credited
                    # as "eight". The reply was then truncated to that in the context
                    # and the agent argued the point with the user.
                    src = self._bot["audio_by_src"]
                    proc = getattr(data, "processor", None)
                    key = getattr(proc, "name", None) or type(proc).__name__
                    acc = src.setdefault(key, [0, f.sample_rate])
                    acc[0] += getattr(f, "num_frames", 0) or 0
                    acc[1] = f.sample_rate
            if fctx is not None:
                self._window_dur += dur  # tagged pushes only: the copies are duplicates
        elif isinstance(f, BotStartedSpeakingFrame):
            self._seen.add(f.id)
            self._window_open = True
            self._window_t0 = t
            # A playout window opened by a FILLER must not start a bot turn:
            # BotStarted is anonymous, and with a reply's text pending (its TTS
            # delayed), _ensure_bot here would open a turn that claims the text,
            # gets charted "fully heard" off the filler's playout, and consumes
            # the pending — the delayed reply then vanishes (the phantom-turn
            # swallow, via the side door). A reply that CHAINS into the filler's
            # window still opens its turn on its own TTS frames, and its playout
            # begins after the filler's audio, not at this frame: audio_start is
            # set from its queue-ahead offset, never from a window that is not its
            # own. (Stamping it here credited a barged reply with the seconds the
            # FILLER had been playing.)
            if not self._window_head_filler:
                self._ensure_bot(t)
            if (self._bot is not None and self._bot["audio_start"] is None
                    and self._bot.get("queue_ahead") is not None):
                self._bot["audio_start"] = t + self._bot["queue_ahead"]
        elif isinstance(f, TTSStoppedFrame):
            self._seen.add(f.id)
            fctx = getattr(f, "context_id", None)
            if fctx is not None and fctx in self._filler_ctxs:
                # The filler's synthesis is over; its context won't be seen again.
                self._filler_ctxs.pop(fctx, None)
            elif self._bot is not None and (
                    fctx is None or self._bot["ctx"] is None
                    or fctx == self._bot["ctx"]):
                # Only THIS turn's stop frame means its full audio length is known.
                # A foreign context's stop (a filler chained inside the turn) said
                # nothing about the reply, yet used to set synth_done and collapse
                # full_dur to the samples synthesized so far — a barge-in a third
                # of the way in then read as fully heard.
                self._bot["synth_done"] = True
        elif isinstance(f, BotStoppedSpeakingFrame):
            self._seen.add(f.id)
            # Only a turn whose audio was IN this window ends with it. A reply's
            # TTSStarted is pushed at synthesis start; when its first chunk is slow
            # (Kokoro shares the GPU with STT) a filler queued just before it plays
            # out and closes its own window first. Closing the reply's turn on that
            # BotStopped charted it complete with no audio, and its audio then
            # opened a second turn on the same text -- charted again at the next
            # cut. A turn with no accepted audio keeps waiting for its window.
            if self._bot is not None and self._bot.get("queue_ahead") is not None:
                self._finish_bot(t, interrupted=False)
            self._reset_window()

        if len(self._seen) > 8192:
            self._seen.clear()

    def _new_bot(self, t: float) -> dict:
        # Which response does a turn that opens now belong to? The TTS service
        # synthesizes responses in the order it received them, so if completed
        # replies are still unspoken (_pending — a turn takes the front when it
        # opens; an interruption drops them all) the oldest is older than anything
        # still streaming and this turn is its. Only with nothing queued is the
        # turn the in-flight generation's — the streaming-TTS shape, where the
        # first audio arrives before LLMFullResponseEndFrame; that bot is marked
        # live and its intended is completed on the response end, or from the
        # partial text if barged first. (Preferring the in-flight text
        # unconditionally charted a reply whose TTS began after the NEXT
        # completion started streaming — text plus a tool call in one completion —
        # under the next completion's text.)
        if self._pending:
            gen_seq, intended = self._pending.popleft()
            live = False
        else:
            live = self._gen_acc is not None
            intended = "".join(self._gen_acc).strip() if live else ""
            gen_seq = None
            if live:
                self._gen_claimed = True
        return {"t_start": t, "intended": intended, "intended_live": live,
                # The response seq this turn took from the queue (None when the
                # text came from the live accumulator; filled in on the response
                # end for a live turn). Informational: the queue entry is gone.
                "gen_seq": gen_seq,
                "audio_start": None,
                # Seconds of audio queued in the playout window ahead of this
                # turn's first audio; None until that audio is seen. See the
                # TTSAudioRawFrame and BotStarted branches.
                "queue_ahead": None,
                # The TTS context this turn belongs to, adopted from the frame that
                # opens it (see _ctx_ok). A later frame from a DIFFERENT context is
                # foreign and is not folded in.
                "ctx": None,
                # samples the reply is worth, bucketed by the processor that pushed
                # them: {name: [samples, rate]}. See the TTSAudioRawFrame branch.
                "audio_by_src": {}, "sr": None, "synth_done": False,
                "spoken": []}

    def _reset_window(self):
        # The transport's window closed (BotStopped) or was flushed (interruption):
        # the next audio pushed is the head of the next window.
        self._window_open = False
        self._window_t0 = None
        self._window_head_seen = False
        self._window_head_filler = False
        self._window_dur = 0.0

    def _ensure_bot(self, t: float):
        # Some TTS paths don't emit a TTSStartedFrame the ledger sees — notably
        # the engine's per-word path (push_text_frames=False), which routes frames
        # through pipecat's audio context. Start the bot turn on whatever TTS
        # frame arrives first so the turn is still recorded.
        if self._bot is None and (self._gen_acc is not None or self._pending):
            self._bot = self._new_bot(t)

    def _is_filler(self, frame) -> bool:
        """True for a frame from a FILLER TTS context — a speak frame the brain
        plays as pure audio UX (the consult narrator, the tool-ack lines), pushed
        with append_to_context=False. Fillers are kept out of the LLM context at
        the source, and the ledger charts nothing for them: they are not part of
        anything the assistant *said* as a reply. Identified by the flag where the
        frame carries one (TTSStartedFrame, TTSTextFrame) and by the context
        remembered from the filler's started frame otherwise (audio)."""
        fctx = getattr(frame, "context_id", None)
        if fctx is not None and fctx in self._filler_ctxs:
            return True
        return getattr(frame, "append_to_context", True) is False

    def _ctx_ok(self, frame) -> bool:
        """True if a WORD frame belongs to the open bot turn's TTS context.

        A bot turn is opened by an LLM response but CARRIED by a TTS context, and it
        adopts the context_id of the first TTS frame that names one (the opening
        TTSStartedFrame in the normal path). A frame from a DIFFERENT context is
        foreign — a filler ("Still working on it.") pushed as its own TTSSpeakFrame
        while a reply is mid-playout, or the next turn's audio arriving early — and
        its words and samples must NOT be counted as part of this turn. Confirmed
        live: brain/formal (LEDGER_TRACE, 2026-08-29 23:21:42) shows a narrator
        filler's four words folded into a 29-word reply's utterance.

        Word frames with no context_id are accepted, not rejected: sherpa pushes
        one whole-reply TTSTextFrame with none. Only a frame that NAMES a different
        context is turned away. (Audio is stricter — see _audio_ok.)
        """
        if self._bot is None:
            return False
        fctx = getattr(frame, "context_id", None)
        if fctx is None:
            return True
        if self._bot["ctx"] is None:
            self._bot["ctx"] = fctx
            return True
        return fctx == self._bot["ctx"]

    def _audio_ok(self, frame) -> bool:
        """True if an AUDIO frame's samples count toward the open turn's duration.

        Stricter than _ctx_ok: once the turn is ctx-tagged, only audio that NAMES
        that context counts. Unlike words, audio is REBUILT by the output transport
        without its context_id, so an untagged frame is unattributable — it is just
        as likely the transport's copy of a foreign filler's playout (verified: a
        fully-heard 1.5s reply measured heard 0.53 because the filler's TAGGED copy
        was rejected while its untagged transport copy landed in the reply's
        denominator) or the thinking-sound loop as this reply's own resampled copy.
        The TTS service's tagged copy alone already measures the reply in full, so
        dropping untagged copies loses nothing. A turn with NO context accepts
        everything, same as before (the ctx-less test/legacy shape).
        """
        if self._bot is None:
            return False
        fctx = getattr(frame, "context_id", None)
        if self._bot["ctx"] is None:
            if fctx is not None:
                self._bot["ctx"] = fctx
            return True
        return fctx == self._bot["ctx"]

    def _finish_bot(self, t: float, interrupted: bool):
        b = self._bot
        self._bot = None
        if not b:
            return
        intended = (b["intended"] or "").strip()
        if b.get("intended_live") and self._gen_acc is not None:
            # Barged in while the reply was still streaming from the LLM: the text
            # generated so far is the best available intended for this utterance.
            intended = "".join(self._gen_acc).strip() or intended
        if not intended:
            # Nothing was ever attributed to this turn: there is nothing to chart.
            # (A turn takes its text off the queue when it OPENS, so an empty turn
            # never held anything — the next reply's text is still queued for the
            # turn that will speak it. Consuming here used to swallow it whenever
            # an unmarked speak context opened a phantom turn in the silence
            # before it; pre-existing, reproduced on main.)
            return
        # Every processor saw the WHOLE reply, so each one's samples/rate is the
        # reply's duration on its own. Take the longest, never the sum: a resampled
        # copy must not add length, and the longest is the source that saw the most.
        audio_dur = max((n / rate for n, rate in b["audio_by_src"].values() if rate),
                        default=0.0)
        # If synthesis was cut short, audio_dur underestimates the intended
        # length; fall back to a word-count estimate so heard_fraction isn't
        # inflated. (Also with NO counted audio at all — a turn whose audio was
        # never ctx-tagged — where trusting synth_done would make full_dur 0.)
        text_dur = len(intended.split()) * _SECONDS_PER_WORD
        full_dur = (audio_dur if b["synth_done"] and audio_dur > 0
                    else max(audio_dur, text_dur))
        if interrupted:
            # audio_start is None when playout never began (cut during synthesis)
            # — heard is genuinely zero. A turn chained mid-window never sees a
            # BotStarted of its own; it gets audio_start preset from the previous
            # turn's charted end instead (see the chained-context branch).
            heard_dur = (t - b["audio_start"]) if b["audio_start"] else 0.0
            frac = min(1.0, heard_dur / full_dur) if full_dur > 0 else 0.0
        else:
            frac = 1.0
        # Heard text from per-word TTSTextFrames (engine TTS): keep each word whose
        # SCHEDULED playout time (frame.pts) is at/before the cut. The frames
        # arrive clustered (our TTS pushes the whole clip at once), so we filter
        # by pts, not arrival order. Fall back to arrival order if frames carry
        # no pts, and to the played-fraction estimate with no per-word frames
        # (sherpa).
        spoken = b.get("spoken", [])
        est = _prefix_words(intended, frac)  # reliable played-audio-fraction estimate
        if any(p is not None for _, p in spoken):
            # pts are shifted EARLY by the caption lead (see _PTS_LEAD_SECS): a word
            # was truly heard only if pts + lead <= cut, i.e. pts <= cut - lead.
            cut_ns = (t - _PTS_LEAD_SECS) * 1e9
            # Space-JOIN the matched word texts. The engine's per-word frames do not
            # reliably carry inter-word spaces -- number/counting output arrives as
            # "One,","two,",... with none -- so a bare "".join collapses them to a
            # single whitespace-less token and len(...split()) reads 1 however many
            # matched. That made the ">= est" guard below always reject the exact
            # timing and fall back to the coarse fraction, so the EXACT heard boundary
            # this path exists for went unused. Live 2026-09-02: a barge-in during
            # "One..twenty" matched ~ten words, was counted as 1, and committed est=8
            # while the user heard ~ten. Rejoin on single spaces so the count -- and
            # the text -- reflect the words actually played.
            pts_heard = " ".join(
                (txt or "").strip()
                for txt, p in spoken
                if p is not None and p <= cut_ns and (txt or "").strip())
            # Prefer exact word-timing, but never report FEWER words than the
            # played-audio fraction implies — guards against a misaligned pts
            # baseline (which would silently drop words the user did hear).
            heard_text = pts_heard if len(pts_heard.split()) >= len(est.split()) else est
            if _TRACE:
                logger.info(
                    f"TRACE cut t={t:.2f} audio_start={b['audio_start'] or 0:.2f} "
                    f"heard_dur={(t - (b['audio_start'] or t)):.2f} "
                    f"audio_by_src={b['audio_by_src']} audio_dur={audio_dur:.2f} "
                    f"text_dur={text_dur:.2f} synth_done={b['synth_done']} "
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
        # The closing frame — the chain-end BotStopped, or the interruption — can
        # arrive only after a FOREIGN filler chained onto this turn has played out.
        # The turn's own end is when ITS audio finished, never later: without the
        # clamp the utterance window stretched across the filler's playout, and a
        # user remark made during the filler was marked OVERLAP against a reply it
        # never touched. (Synthesis runs ahead of playout, so for a genuine mid-turn
        # cut t is the earlier bound and the clamp is a no-op.)
        t_end = t
        if b["audio_start"] is not None and audio_dur > 0:
            t_end = min(t, b["audio_start"] + audio_dur)
        self._add(Utterance("assistant", intended, b["t_start"], t_end,
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
