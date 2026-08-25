#
# llm_text_guard.py — fold degenerate unicode out of the model's streamed text and
# cut a runaway collapse before it floods the turn.
#
# gpt-oss-120b intermittently contaminates replies that echo tool-result strings
# (voice ids, language names, quoted phrases) with no-break unicode — U+00A0/U+202F
# between word parts, U+2011 inside hyphenated words — and sometimes cascades from
# there into runs of ellipses and whitespace: up to 2340 chars of unspeakable
# punctuation in ONE completion (live captures, 2026-08-12). Replayed offline, the
# exact live context reproduces the contamination in most samples, on every
# provider, at every reasoning effort; 97% of the exotic characters land inside
# text echoed from the tool result. It is a model-level failure mode the pipeline
# has to contain, not one it can avoid upstream (prompt wording ablations moved
# markdown-vs-unicode style, not the rate).
#
# Three harms follow if the raw deltas flow on: the TTS is handed junk it cannot
# synthesize, the junk is committed to the conversation as the assistant's own turn
# (pipecat appends the TTS-spoken text — see heard_context.py), and a degenerate
# turn in history makes the next reply degenerate more often (measured 6/12 vs
# 0/12 with clean history). This processor sits immediately downstream of the LLM
# service — AFTER RawLLMCapture, which must keep seeing the model's raw bytes —
# and does two things to LLMTextFrame deltas:
#
#   FOLD  no-break/zero-width characters to plain equivalents and collapse
#         ellipsis runs. Healthy text passes unchanged. Folding here cleans both
#         what is spoken and what enters history. The character table itself lives
#         in tts_text.fold_unspeakable so the synth-side normalizer and this guard
#         cannot drift apart. Not stateless: a trailing run is held back one delta
#         so a run the stream split across frames still folds (see _HOLDBACK).
#
#   CUT   once the cumulative completion crosses a degeneracy threshold that no
#         healthy one-or-two-sentence spoken reply reaches, swallow every
#         remaining delta of the response and speak one short recovery line in
#         its place. A single "..." or a few dramatic pauses never trip it.
#
# The recovery line matters: swallowing alone turned a degenerate turn into total
# silence. The captures collapse within their first ~50 characters, and a prefix
# that short is usually punctuation, which split_clauses_ramp then drops as
# unsynthesizable — so "keep the healthy prefix" could keep nothing, and the user
# got dead air with no signal and nothing to respond to. One spoken sentence ends
# the turn honestly and gives the context a clean assistant message instead of a
# fragment.
#
# The upstream generation is not aborted — this is containment, not cancellation.
# A true drop-and-retry would need the whole response buffered before TTS starts,
# which the first-audio latency budget rules out.
#
# Disable with TEAPORT_LLM_TEXT_GUARD=0.
#
import os
import re

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.env import env_flag
from teaport_brain.tts_text import MIN_DOT_RUN, fold_unspeakable

ENABLED = env_flag("TEAPORT_LLM_TEXT_GUARD", True)

# Guard-specific: a run of ellipses (optionally spaced) collapses to one. The
# no-break/zero-width table is tts_text's — see fold_unspeakable.
#
# \s, not [ \t]: the live captures separate ellipses with NEWLINES as often as spaces
# ("\u2026\n\u2026\n\u2026"), and a class that stopped at tab left those runs entirely unfolded.
_ELLIPSIS_RUN = re.compile(r"(?:\u2026\s*){2,}")
_DOT_RUN_LONG = re.compile(r"\.{4,}")

# The folds above are per-delta, and the stream splits wherever it likes: "**" arriving as
# "*" + "*", or "\u2026" + "\u2026", or ".." + "..", passed through untouched while the same text in
# ONE delta folded cleanly — guard output that depended on provider chunking, and markdown
# the header calls "always a defect" reaching the committed context. A trailing run that
# could be the start of a longer one is held back and prepended to the next delta. Capped,
# because a long run inside one delta already folds on its own and holding it back would
# only delay audio. The counter is still fed the RAW delta, so counting stays exactly as
# split-invariant as it was.
# `\.{2,}`, not `\.+`: a single trailing "." is how almost every sentence ends, and
# holding it back emitted the period as its own frame at the end of the response — a
# one-character slot that synthesizes nothing, which is the very caption failure the
# leading-punct strip exists to prevent. Two dots is already anomalous. "*" and
# U+2026 are held from the first character because neither belongs in speech alone.
_HOLDBACK = re.compile(r"(?:\*+|\.{2,}|(?:\u2026\s*)+)$")
_MAX_HOLDBACK = 4

# Punctuation that opens a reply. It has no phonemes, and it costs the user the whole
# caption: pipecat's TTS aggregator gives a leading "..." its own text frames, each one
# opens a slot in the word-timestamp sequencer, and each synthesizes nothing. Every real
# word that follows then fails to match a slot ("not recognised by any slot, emitting as
# passthrough") and the assistant's bubble never assembles. Live 2026-08-20: the model
# answered "Continue from where you left off." with "... it was the worst of times",
# which became slots '..' and '.', and all thirty spoken words missed. The audio is
# untouched by any of this, which is why it reads as "I heard it but saw nothing".
#
# Only at the START of a response, where there is nothing for the punctuation to attach
# to. Mid-reply a "." delta is the end of a sentence and dropping it would merge two.
_LEADING_PUNCT = re.compile(r"^[\s.,;:!?\u2026]+")

# Spoken when the guard trips. Overridable because the pipeline serves Japanese,
# Mandarin and Hindi too, and a hardcoded English apology is wrong in those rooms.
# The leading space is deliberate: this is appended to whatever healthy prefix was
# already forwarded, and reads as its own sentence either way.
# `or`, not getenv's default: TEAPORT_LLM_GUARD_RECOVERY= (key present, value empty) is a
# plausible hand-edit for an operator who wants the guard quiet, and it used to push an
# EMPTY LLMTextFrame — which both restored the total silence this line exists to prevent
# and opened a phantom word-timestamp slot. env.py already treats an empty value as "not
# set" everywhere else; this was the one read in the module that did not.
RECOVERY_TEXT = (
    os.getenv("TEAPORT_LLM_GUARD_RECOVERY", "").strip()
    or " Sorry, I lost my train of thought there. Could you say that again?"
)

# Trip thresholds, calibrated against the nine live captures and healthy speech:
# every capture >= 37 chars trips within its first ~50 characters; a reply using
# "..." twice for dramatic effect, or an ellipsis after a few sentences, does not.
#
# UNITS: both counters count "collapse markers", where one marker is either a run
# of 3+ dots (however long — "...." is one marker, not two) or one U+2026. That is
# what makes _TRIP_COMBINED a coherent sum. The two are equal at 5 and the combined
# bar is 6: an earlier comment here claimed they differed and explained why, which the
# values contradicted — the boundary the suite actually pins (tests FOLDED_NOT_CUT vs
# DEGENERATE) is four markers folded, five markers cut, for both kinds.
_TRIP_DOT_RUNS = 5
_TRIP_ELLIPSES = 5
_TRIP_COMBINED = 6

# VOLUME, the term the marker counts cannot express. A marker is counted once per RUN, so
# an unbroken run of dots scores exactly 1 no matter how long it is — and the single worst
# shape on record, the 2340-character punctuation collapse in this module's own header,
# arrives as ONE run. It scored dot_runs=1, ellipses=0, and tripped nothing: the guard was
# blind to precisely the completion it was written for, and every delta was then swallowed
# by the leading-punct strip, so the turn went out as dead air with no recovery line and no
# log. Counting the characters that belong to a collapse marker catches it by mass instead.
# 40 sits above any punctuation a healthy spoken reply carries (the pinned FOLDED_NOT_CUT
# cases peak at 16) and below the ~50 characters within which every live capture collapses.
_TRIP_PUNCT_CHARS = 40


class DegeneracyCounter:
    """Counts collapse markers in a completion, incrementally.

    Fed delta-by-delta from the hot path, so a completion is scanned ONCE in total
    rather than re-joined and re-scanned per delta. The re-scan was O(n^2): a 1024
    token reply arrives as ~800 deltas, and rebuilding a string that grows to ~4KB
    on each one copied ~1.6M characters and ran ~800 full regex passes per turn, on
    an Orin Nano, inside the coroutine that carries first-audio latency — and every
    healthy turn paid all of it for nothing.

    `_run` carries a dot run across a frame boundary, so "..." split by the stream
    into ".." + "." still counts as one marker and not zero.

    `punct_chars` is the same evidence measured by mass rather than by occurrence, so a
    single unbroken run — which is one marker however long — still registers. See
    _TRIP_PUNCT_CHARS.
    """

    __slots__ = ("dot_runs", "ellipses", "punct_chars", "_run")

    def __init__(self):
        self.dot_runs = 0
        self.ellipses = 0
        self.punct_chars = 0
        self._run = 0

    def feed(self, text: str) -> None:
        run = self._run
        for ch in text:
            if ch == ".":
                run += 1
                if run == MIN_DOT_RUN:  # count the run once, at its third dot
                    self.dot_runs += 1
                    self.punct_chars += MIN_DOT_RUN
                elif run > MIN_DOT_RUN:
                    self.punct_chars += 1  # ... and every further dot by mass only
            else:
                run = 0
        self._run = run
        n = text.count("\u2026")
        self.ellipses += n
        self.punct_chars += n

    @property
    def tripped(self) -> bool:
        return (self.dot_runs >= _TRIP_DOT_RUNS
                or self.ellipses >= _TRIP_ELLIPSES
                or self.dot_runs + self.ellipses >= _TRIP_COMBINED
                or self.punct_chars >= _TRIP_PUNCT_CHARS)


def fold_degenerate_chars(text: str) -> str:
    """Fold the unspeakable family to plain equivalents; collapse ellipsis runs."""
    text = fold_unspeakable(text)
    text = _ELLIPSIS_RUN.sub("\u2026 ", text)
    text = _DOT_RUN_LONG.sub("...", text)
    return text


def is_degenerate(cumulative: str) -> bool:
    """Has this completion collapsed into the ellipsis/whitespace attractor?

    One-shot form of DegeneracyCounter. There is no "batch path" in the pipeline — the
    guard only ever feeds deltas — so this exists for the tests, which use it as the
    whole-string oracle that the incremental counter is checked against
    (test_incremental_matches_oneshot). Kept in the module, not the test file, so the
    oracle and the implementation cannot drift apart."""
    counter = DegeneracyCounter()
    counter.feed(cumulative)
    return counter.tripped


class LLMTextGuard(FrameProcessor):
    """Folds degenerate unicode in LLMTextFrames; swallows the tail of a collapse.

    Start/End frames always pass through — downstream aggregators need them to
    open and close the turn even when every text frame between them is swallowed.
    """

    def __init__(self):
        super().__init__()
        self._pending = ""
        self._pending_frame = None
        self._reset_response()

    def _reset_response(self):
        """Full reset. ONLY on Start — see _reset_log for why not on End."""
        self._counter = DegeneracyCounter()
        self._tripped = False
        self._forwarded = 0
        self._swallowed = 0
        self._pending = ""
        self._pending_frame = None

    def _reset_log(self):
        """Reset the accounting a trip line reports, keeping the trip itself latched.

        pipecat's audio-context watchdog can push a premature LLMFullResponseEndFrame
        mid-reply (see engine_tts), so an End is NOT proof the completion is over. A full
        reset here cleared _tripped and re-armed the guard against the remaining deltas of
        the SAME collapse: they flowed through until they re-crossed the threshold, and the
        recovery line was then spoken a SECOND time — breaking the one guarantee this class
        makes ("exactly one recovery line, however long the tail runs"). Clearing
        _forwarded had its own edge: _LEADING_PUNCT fired again on the first delta of the
        next segment, so a mid-reply ", and then we left." lost the comma that joined the
        clauses, which is exactly the sentence merge the _LEADING_PUNCT comment forbids.

        What DOES belong here is the per-segment accounting, which is what the original
        End-reset was for: without it _swallowed carried across segments and the trip log
        reported more swallowed characters than the response contained.
        """
        self._counter = DegeneracyCounter()
        self._swallowed = 0

    def _fold_streaming(self, text: str) -> str:
        """fold_degenerate_chars with one-delta lookahead — see _HOLDBACK."""
        text = self._pending + text
        self._pending = ""
        m = _HOLDBACK.search(text)
        if m and len(m.group(0)) <= _MAX_HOLDBACK:
            self._pending = m.group(0)
            text = text[:m.start()]
        return fold_degenerate_chars(text) if text else ""

    async def _emit(self, text: str, direction: FrameDirection,
                    frame: "LLMTextFrame | None" = None):
        """Push one text frame, applying the leading-punct strip. Empty -> nothing.

        `frame` REUSES the inbound frame instead of building a new one, and that is
        load-bearing rather than tidiness. TranscriptLedger is a BaseObserver: it sees
        every push of every frame and de-duplicates on frame IDENTITY (`if f.id in
        self._seen: return`). Each LLMTextFrame is pushed twice on the way down — once
        by the LLM service, once by this guard — and the ledger charted it once only
        because both pushes carried the same object. A fresh LLMTextFrame gives the
        second push an id the ledger has never seen, so every delta lands in the
        intended-text accumulator TWICE, interleaved by arrival order: "I like teal."
        was charted as "I like tealI. like teal.".
 
        That is the denominator of the heard fraction, so replies the user heard in
        full scored 0-96%, HeardContextCorrector truncated or dropped them from history,
        and the model — unable to see its own turns — re-answered questions it had
        already answered. Live 2026-08-25: 11 of 12 assistant turns doubled, against
        clean single TTS calls.

        Only genuinely NEW text (the recovery line) may build a new frame. Text that
        merely passed through keeps the identity it arrived with.

        Every emission goes through here, so the "an empty frame still opens a slot"
        rule cannot be enforced on one path and forgotten on another — which is how a
        mid-reply delta of "**" (folding to "") and an LLMTextFrame that ARRIVED empty
        both used to reach the TTS aggregator and break the caption for the rest of the
        reply.
        """
        if not self._forwarded:
            text = _LEADING_PUNCT.sub("", text)
        if not text:
            return
        self._forwarded += len(text)
        if frame is None:
            await self.push_frame(LLMTextFrame(text=text), direction)
            return
        frame.text = text
        await self.push_frame(frame, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._reset_response()
        elif isinstance(frame, LLMTextFrame):
            # No `and frame.text` guard: an LLMTextFrame that arrives already empty used
            # to match no branch at all and fall through to the unconditional push at the
            # bottom, forwarding an empty frame even while tripped.
            if self._tripped:
                self._swallowed += len(frame.text or "")
                return  # swallow: no push
            if not frame.text:
                return
            self._counter.feed(frame.text)
            if self._counter.tripped:
                self._tripped = True
                self._swallowed += len(frame.text)
                logger.warning(
                    f"LLMTextGuard: degenerate completion — tripped after "
                    f"{self._forwarded} forwarded chars "
                    f"(dot_runs={self._counter.dot_runs} "
                    f"ellipses={self._counter.ellipses} "
                    f"punct_chars={self._counter.punct_chars}); "
                    f"swallowing the rest and speaking the recovery line"
                )
                # The frame that crossed the threshold is junk too, so it is not
                # forwarded; this takes its place and ends the turn with something
                # speakable in both the audio and the committed context. Anything held
                # back for folding is junk by the same argument and is dropped with it.
                self._pending = ""
                self._pending_frame = None
                # Through _emit so the recovery line obeys the leading-punct rule as
                # well: its deliberate leading space reads as a sentence break after a
                # healthy prefix, but with nothing forwarded it is a whitespace-only
                # frame, which opens the phantom caption slot the strip exists to stop.
                await self._emit(RECOVERY_TEXT, direction)
                return
            emitted = self._fold_streaming(frame.text)
            # If the folder held a tail back, remember which frame it came from: the
            # ledger charted that delta's whole text on the LLM service's push, so the
            # eventual flush must ride the same frame or the tail is charted twice.
            self._pending_frame = frame if self._pending else None
            await self._emit(emitted, direction, frame)
            return  # _emit already pushed; never fall through to the push below
        elif isinstance(frame, LLMFullResponseEndFrame):
            # Flush whatever the folder was holding, or it is lost with the turn.
            if self._pending and not self._tripped:
                tail, self._pending = self._pending, ""
                held, self._pending_frame = self._pending_frame, None
                await self._emit(fold_degenerate_chars(tail), direction, held)
            if self._tripped:
                logger.warning(
                    f"LLMTextGuard: response ended; swallowed {self._swallowed} "
                    f"chars after {self._forwarded} forwarded"
                )
            self._reset_log()
        await self.push_frame(frame, direction)
