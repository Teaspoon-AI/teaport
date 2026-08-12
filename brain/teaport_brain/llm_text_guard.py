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
#
#   CUT   once the cumulative completion crosses a degeneracy threshold that no
#         healthy one-or-two-sentence spoken reply reaches, swallow every
#         remaining delta of the response. The TTS never receives the tail and
#         the context never commits it; the turn ends at the last mostly-healthy
#         prefix. A single "..." or a few dramatic pauses never trip it.
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

ENABLED = os.getenv("TEAPORT_LLM_TEXT_GUARD", "1").strip().lower() not in ("0", "false", "")

# Explicit \uXXXX escapes only (raw strings, parsed by `re`) — a literal class once
# silently folded an ordinary space into itself and broke a detector.
_NOBREAK_SPACES = re.compile(r"[\u00a0\u202f]")
_ZERO_WIDTH = re.compile(r"[\u200b\u2060\ufeff]")
_ELLIPSIS_RUN = re.compile(r"(?:\u2026[ \t]*){2,}")
_DOT_RUN_LONG = re.compile(r"\.{4,}")
_DOT_RUN = re.compile(r"\.{3,}")

# Trip thresholds, calibrated against the nine live captures and healthy speech:
# every capture >= 37 chars trips within its first ~50 characters; a reply using
# "..." twice for dramatic effect, or an ellipsis after a few sentences, does not.
_TRIP_DOT_RUNS = 3
_TRIP_ELLIPSES = 5
_TRIP_COMBINED = 6


def fold_degenerate_chars(text: str) -> str:
    """Fold the no-break/zero-width family to plain equivalents; collapse runs."""
    text = _NOBREAK_SPACES.sub(" ", text)
    text = _ZERO_WIDTH.sub("", text)
    text = text.replace("\u2011", "-")
    text = _ELLIPSIS_RUN.sub("\u2026 ", text)
    text = _DOT_RUN_LONG.sub("...", text)
    return text


def is_degenerate(cumulative: str) -> bool:
    """Has this completion collapsed into the ellipsis/whitespace attractor?"""
    dot_runs = len(_DOT_RUN.findall(cumulative))
    ellipses = cumulative.count("\u2026")
    return (dot_runs >= _TRIP_DOT_RUNS or ellipses >= _TRIP_ELLIPSES
            or dot_runs + ellipses >= _TRIP_COMBINED)


class LLMTextGuard(FrameProcessor):
    """Folds degenerate unicode in LLMTextFrames; swallows the tail of a collapse.

    Start/End frames always pass through — downstream aggregators need them to
    open and close the turn even when every text frame between them is swallowed.
    """

    def __init__(self):
        super().__init__()
        self._cum: list[str] = []
        self._tripped = False
        self._swallowed = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._cum = []
            self._tripped = False
            self._swallowed = 0
        elif isinstance(frame, LLMTextFrame) and frame.text:
            self._cum.append(frame.text)
            if self._tripped:
                self._swallowed += len(frame.text)
                return  # swallow: no push
            if is_degenerate("".join(self._cum)):
                self._tripped = True
                self._swallowed += len(frame.text)
                kept = sum(map(len, self._cum)) - self._swallowed
                logger.warning(
                    f"LLMTextGuard: degenerate completion — tripped after {kept} "
                    f"forwarded chars; swallowing the rest of this response"
                )
                return  # the frame that crossed the threshold is junk too
            frame.text = fold_degenerate_chars(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._tripped:
                logger.warning(
                    f"LLMTextGuard: response ended; swallowed {self._swallowed} of "
                    f"{sum(map(len, self._cum))} chars"
                )
            self._cum = []
            self._tripped = False
        await self.push_frame(frame, direction)
