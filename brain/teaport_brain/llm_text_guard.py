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
#         ellipsis runs, per frame, stateless. Healthy text passes unchanged.
#         Folding here cleans both what is spoken and what enters history.
#         The character table itself lives in tts_text.fold_unspeakable so the
#         synth-side normalizer and this guard cannot drift apart.
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
from teaport_brain.tts_text import fold_unspeakable

ENABLED = env_flag("TEAPORT_LLM_TEXT_GUARD", True)

# Guard-specific: a run of ellipses (optionally spaced) collapses to one. The
# no-break/zero-width table is tts_text's — see fold_unspeakable.
_ELLIPSIS_RUN = re.compile(r"(?:\u2026[ \t]*){2,}")
_DOT_RUN_LONG = re.compile(r"\.{4,}")

# Spoken when the guard trips. Overridable because the pipeline serves Japanese,
# Mandarin and Hindi too, and a hardcoded English apology is wrong in those rooms.
# The leading space is deliberate: this is appended to whatever healthy prefix was
# already forwarded, and reads as its own sentence either way.
RECOVERY_TEXT = os.getenv(
    "TEAPORT_LLM_GUARD_RECOVERY",
    " Sorry, I lost my train of thought there. Could you say that again?",
)

# Trip thresholds, calibrated against the nine live captures and healthy speech:
# every capture >= 37 chars trips within its first ~50 characters; a reply using
# "..." twice for dramatic effect, or an ellipsis after a few sentences, does not.
#
# UNITS: both counters count "collapse markers", where one marker is either a run
# of 3+ dots (however long — "...." is one marker, not two) or one U+2026. That is
# what makes _TRIP_COMBINED a coherent sum. The two individual thresholds still
# differ because the evidence differs: three "..." runs in a two-sentence spoken
# reply is already unusual, while U+2026 is ordinary typography that healthy prose
# reaches more often, so it needs more of them to mean the same thing.
_MIN_DOT_RUN = 3
_TRIP_DOT_RUNS = 5
_TRIP_ELLIPSES = 5
_TRIP_COMBINED = 6


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
    """

    __slots__ = ("dot_runs", "ellipses", "_run")

    def __init__(self):
        self.dot_runs = 0
        self.ellipses = 0
        self._run = 0

    def feed(self, text: str) -> None:
        run = self._run
        for ch in text:
            if ch == ".":
                run += 1
                if run == _MIN_DOT_RUN:  # count the run once, at its third dot
                    self.dot_runs += 1
            else:
                run = 0
        self._run = run
        self.ellipses += text.count("\u2026")

    @property
    def tripped(self) -> bool:
        return (self.dot_runs >= _TRIP_DOT_RUNS
                or self.ellipses >= _TRIP_ELLIPSES
                or self.dot_runs + self.ellipses >= _TRIP_COMBINED)


def fold_degenerate_chars(text: str) -> str:
    """Fold the unspeakable family to plain equivalents; collapse ellipsis runs."""
    text = fold_unspeakable(text)
    text = _ELLIPSIS_RUN.sub("\u2026 ", text)
    text = _DOT_RUN_LONG.sub("...", text)
    return text


def is_degenerate(cumulative: str) -> bool:
    """Has this completion collapsed into the ellipsis/whitespace attractor?

    One-shot form of DegeneracyCounter, so the batch and streaming paths cannot
    disagree about what "degenerate" means."""
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
        self._reset()

    def _reset(self):
        # Reset on BOTH Start and End. Resetting only on Start let _swallowed carry
        # into the next segment whenever a response was split without one — pipecat's
        # audio-context watchdog can push a premature LLMFullResponseEndFrame
        # mid-reply (see engine_tts) — and the trip log then reported more swallowed
        # characters than the response contained.
        self._counter = DegeneracyCounter()
        self._tripped = False
        self._forwarded = 0
        self._swallowed = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._reset()
        elif isinstance(frame, LLMTextFrame) and frame.text:
            if self._tripped:
                self._swallowed += len(frame.text)
                return  # swallow: no push
            self._counter.feed(frame.text)
            if self._counter.tripped:
                self._tripped = True
                self._swallowed += len(frame.text)
                logger.warning(
                    f"LLMTextGuard: degenerate completion — tripped after "
                    f"{self._forwarded} forwarded chars "
                    f"(dot_runs={self._counter.dot_runs} "
                    f"ellipses={self._counter.ellipses}); "
                    f"swallowing the rest and speaking the recovery line"
                )
                # The frame that crossed the threshold is junk too, so it is not
                # forwarded; this takes its place and ends the turn with something
                # speakable in both the audio and the committed context.
                await self.push_frame(LLMTextFrame(text=RECOVERY_TEXT), direction)
                return
            frame.text = fold_degenerate_chars(frame.text)
            self._forwarded += len(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._tripped:
                logger.warning(
                    f"LLMTextGuard: response ended; swallowed {self._swallowed} "
                    f"chars after {self._forwarded} forwarded"
                )
            self._reset()
        await self.push_frame(frame, direction)
