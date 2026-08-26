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
        self._pending_gen: str = ""  # last non-empty generated reply
        self._bot: Optional[dict] = None  # active bot (TTS) utterance
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
        elif isinstance(f, LLMTextFrame):
            self._seen.add(f.id)
            if self._gen_acc is not None:
                self._gen_acc.append(f.text or "")
        elif isinstance(f, LLMFullResponseEndFrame):
            self._seen.add(f.id)
            txt = "".join(self._gen_acc or []).strip()
            if txt:  # tool-call responses have no text; don't clobber
                self._pending_gen = txt
                if self._bot is not None and self._bot.get("intended_live"):
                    # This response's playout started before its text finished
                    # streaming; complete the mid-generation snapshot with the
                    # full reply so heard_fraction has the right denominator.
                    self._bot["intended"] = txt
                    self._bot["intended_live"] = False
            self._gen_acc = None

        # --- bot playout: associate the generated text with the audio ---
        elif isinstance(f, TTSStartedFrame):
            self._seen.add(f.id)
            # A mid-turn TTSStarted (audio context re-created after a stop-frame
            # timeout) must not clobber the in-flight bot dict — that would discard
            # the samples/words already played and report the reply as unheard.
            if self._bot is None:
                self._bot = self._new_bot(t)
        elif isinstance(f, TTSTextFrame):
            self._seen.add(f.id)
            # Per-word TTSTextFrames (engine TTS) are scheduled on the playout clock,
            # so they arrive at the ledger as each word is spoken. Collecting the
            # ones that arrive before an interruption gives EXACTLY what the user
            # heard — no estimate. (sherpa pushes one whole-reply frame instead.)
            self._ensure_bot(t)
            if self._bot is not None:
                # Keep each word's SCHEDULED playout time (frame.pts), not its
                # arrival order: our TTS pushes the whole clip at once, so the
                # word frames arrive clustered, but their pts is each word's
                # real playout instant — which is what tells us what was heard.
                self._bot["spoken"].append((f.text or "", getattr(f, "pts", None)))
        elif isinstance(f, TTSAudioRawFrame):
            self._seen.add(f.id)
            self._ensure_bot(t)
            if self._bot is not None:
                # Per PUSHING PROCESSOR, and take the max at the cut — never a running
                # total. The same audio is pushed twice: once by the TTS service at
                # 24 kHz and again by the output transport, which resamples to the
                # pipeline's 16 kHz. Those are different frame ids, so id-dedup cannot
                # catch them, and bucketing per SAMPLE RATE does not either — the
                # resampled frames still carry sample_rate=24000 while num_frames counts
                # 16 kHz samples, so both land in one bucket and inflate it by
                # (24000+16000)/24000 = 1.67x. Every processor sees the whole reply
                # exactly once, so the max across processors IS the reply's duration,
                # whatever any one of them labels its rate.
                #
                # Live 2026-08-25: a 9.5s count measured as audio_dur=15.8, frac 0.39
                # instead of 0.65, so a barge-in at "thirteen" was credited as "eight".
                # The reply was then truncated to that in the context and the agent
                # argued the point with the user.
                src = self._bot["audio_by_src"]
                proc = getattr(data, "processor", None)
                key = getattr(proc, "name", None) or type(proc).__name__
                acc = src.setdefault(key, [0, f.sample_rate])
                acc[0] += getattr(f, "num_frames", 0) or 0
                acc[1] = f.sample_rate
        elif isinstance(f, BotStartedSpeakingFrame):
            self._seen.add(f.id)
            self._ensure_bot(t)
            if self._bot is not None and self._bot["audio_start"] is None:
                self._bot["audio_start"] = t
        elif isinstance(f, TTSStoppedFrame):
            self._seen.add(f.id)
            if self._bot is not None:
                self._bot["synth_done"] = True  # full audio length now known
        elif isinstance(f, BotStoppedSpeakingFrame):
            self._seen.add(f.id)
            if self._bot is not None:
                self._finish_bot(t, interrupted=False)

        if len(self._seen) > 8192:
            self._seen.clear()

    def _new_bot(self, t: float) -> dict:
        # Prefer the IN-FLIGHT generation's text: with streaming TTS the first audio
        # arrives before LLMFullResponseEndFrame, so at that instant _pending_gen
        # still holds the PREVIOUS reply — snapshotting it gave a barge-in the wrong
        # intended text (wrong heard_fraction denominator, wrong heard-context
        # truncation). A bot created mid-generation is marked live; its intended is
        # completed on the response end, or from the partial text if barged first.
        live = self._gen_acc is not None
        intended = ("".join(self._gen_acc).strip() if live else "") or self._pending_gen
        return {"t_start": t, "intended": intended, "intended_live": live,
                "audio_start": None,
                # samples the reply is worth, bucketed by the processor that pushed
            # them: {name: [samples, rate]}. See the TTSAudioRawFrame branch.
            "audio_by_src": {}, "sr": None, "synth_done": False,
                "spoken": []}

    def _ensure_bot(self, t: float):
        # Some TTS paths don't emit a TTSStartedFrame the ledger sees — notably
        # the engine's per-word path (push_text_frames=False), which routes frames
        # through pipecat's audio context. Start the bot turn on whatever TTS
        # frame arrives first so the turn is still recorded.
        if self._bot is None and (
                self._gen_acc is not None or (self._pending_gen or "").strip()):
            self._bot = self._new_bot(t)

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
        # Consumed: the next bot turn must never inherit this reply's text as its
        # intended (the stale-snapshot bug this block exists to prevent).
        self._pending_gen = ""
        if not intended:
            return
        # Every processor saw the WHOLE reply, so each one's samples/rate is the
        # reply's duration on its own. Take the longest, never the sum: a resampled
        # copy must not add length, and the longest is the source that saw the most.
        audio_dur = max((n / rate for n, rate in b["audio_by_src"].values() if rate),
                        default=0.0)
        # If synthesis was cut short, audio_dur underestimates the intended
        # length; fall back to a word-count estimate so heard_fraction isn't
        # inflated.
        text_dur = len(intended.split()) * _SECONDS_PER_WORD
        full_dur = audio_dur if b["synth_done"] else max(audio_dur, text_dur)
        if interrupted:
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
            pts_heard = "".join(txt for txt, p in spoken
                                if p is not None and p <= cut_ns).strip()
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
                    f"pts_heard={len(pts_heard.split())} est={len(est.split())} "
                    f"-> {len(heard_text.split())} words"
                )
        elif len(spoken) > 1:
            heard_text = "".join(txt for txt, _ in spoken).strip()
        else:
            heard_text = est
        self._add(Utterance("assistant", intended, b["t_start"], t,
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
