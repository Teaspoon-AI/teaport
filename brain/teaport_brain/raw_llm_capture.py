#
# raw_llm_capture.py — record the RAW model completion, but only when it degenerates.
#
# The "ellipsis problem" (the bot emitting runs of "...", "…" and "**" that the TTS
# normalizer then strips into near-silent 0.2s fragments) has only ever been visible
# AFTER the fact, in TTS chunk logs. That is too late to diagnose: by then pipecat has
# aggregated the stream into sentences and _normalize_for_tts has rewritten the
# punctuation, so the model's actual bytes are gone. The brain logs the context it
# SENDS on every turn and the completion it GETS on none of them.
#
# Reproduction attempts failed — 40 completions across 7 conditions (no tools, tools
# available, immediately after an empty tool result, with a truncated assistant turn in
# history, with the real template-laden system prompt, with a clean one, and streaming)
# produced zero degenerate replies (2026-08-12). So the trigger is something only a live
# session has, and the only way forward is to catch one in the act.
#
# This taps LLMTextFrame deltas between LLMFullResponseStart/End — the model's own text,
# upstream of both the sentence aggregator and the TTS normalizer — and logs the whole
# completion verbatim ONLY when it matches a degeneracy pattern. Healthy turns log
# nothing, which is why this is safe to leave on by default.
#
# It also records `after_tool`, because the standing hypothesis is that tool-call
# formatting is what confuses the model. That flag is the discriminator: if degenerate
# completions cluster on after_tool=True, the hypothesis is supported; if they are spread
# evenly, it is not. Nothing else in the pipeline records that association.
#
# Disable with TEAPORT_RAW_LLM_CAPTURE=0.
#
import os
import re

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    FunctionCallResultFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

ENABLED = os.getenv("TEAPORT_RAW_LLM_CAPTURE", "1").strip().lower() not in ("0", "false", "")

# What counts as degenerate. All three were present in the observed failures and none
# belongs in speech output: a run of 3+ dots, two ellipsis characters near each other,
# or markdown bold (the system prompt says "no markdown", so ** is always a defect).
# A single "..." inside otherwise healthy prose is deliberately NOT matched — it is
# ordinary punctuation, and matching it would make this log noisy enough to ignore.
_DEGENERATE = re.compile(r"\.{3,}|…\s*…|\*\*")
_DOT_RUN = re.compile(r"\.{3,}")
_ELLIPSIS = "…"

# Cap the logged text. Degenerate completions have run to thousands of characters of
# pure punctuation; the first 1200 is more than enough to characterize one, and the
# counts below describe the whole thing regardless of the cap.
_MAX_LOG = 1200


class RawLLMCapture(FrameProcessor):
    """Logs a full model completion whenever it looks degenerate. Silent otherwise.

    Place immediately after the LLM service so the frames seen here are the model's
    own deltas — downstream of the TTS aggregator the evidence is already normalized
    away. Passes every frame through untouched; this is a tap, not a filter.
    """

    def __init__(self):
        super().__init__()
        self._buf: list[str] = []
        self._after_tool = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, FunctionCallResultFrame):
            # The next completion is the model reacting to a tool result.
            self._after_tool = True
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._buf = []
        elif isinstance(frame, LLMTextFrame):
            self._buf.append(frame.text or "")
        elif isinstance(frame, LLMFullResponseEndFrame):
            # Never let instrumentation break a live call.
            try:
                self._report()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"RawLLMCapture: report failed (ignored): {e!r}")
            self._buf = []
            self._after_tool = False
        await self.push_frame(frame, direction)

    def _report(self):
        raw = "".join(self._buf)
        if not raw or not _DEGENERATE.search(raw):
            return
        # Counts describe the WHOLE completion even though the text is capped.
        dot_runs = len(_DOT_RUN.findall(raw))
        ellipses = raw.count(_ELLIPSIS)
        bold = raw.count("**")
        nbsp = raw.count(" ")
        # repr() so ellipses, non-breaking spaces and newline runs are all visible as
        # escapes rather than collapsing into invisible whitespace in the journal.
        logger.warning(
            f"RawLLMCapture: DEGENERATE completion chars={len(raw)} "
            f"after_tool={self._after_tool} dot_runs={dot_runs} ellipsis={ellipses} "
            f"bold={bold} nbsp={nbsp} raw={raw[:_MAX_LOG]!r}"
        )
