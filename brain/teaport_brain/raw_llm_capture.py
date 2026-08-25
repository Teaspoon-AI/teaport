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
# evenly, it is not. Nothing else in the pipeline records that association — which is
# exactly why it has to be right. See _pending_after_tool for the two ways a naive
# implementation gets it wrong in OPPOSITE directions.
#
# Disable with TEAPORT_RAW_LLM_CAPTURE=0.
#
import re

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    FunctionCallResultFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.env import env_flag
from teaport_brain.tts_text import DOT_RUN, MIN_DOT_RUN

ENABLED = env_flag("TEAPORT_RAW_LLM_CAPTURE", True)

# What counts as degenerate. All three were present in the observed failures and none
# belongs in speech output: a run of 3+ dots, two ellipsis characters near each other,
# or markdown bold (the system prompt says "no markdown", so ** is always a defect).
# A single "..." inside otherwise healthy prose is deliberately NOT matched — it is
# ordinary punctuation, and matching it would make this log noisy enough to ignore.
# Built from MIN_DOT_RUN rather than repeating the "3" that DOT_RUN already encodes: the
# dot-run length was previously spelled out in three files under two different values, so
# widening it in one place made this log and the guard's trip line report different counts
# for the same completion.
_DEGENERATE = re.compile(r"\.{%d,}|\u2026\s*\u2026|\*\*" % MIN_DOT_RUN)

# Per-code-point counters. Explicit \uXXXX escapes, never the literal character: a
# literal U+00A0 here is two indistinguishable-from-a-space bytes in the source, and
# any formatter or paste through a normalizing tool silently turns it into 0x20 — at
# which point this counter reports every ordinary space and the diagnostic is worse
# than useless. U+202F/U+200B/U+2011 are counted separately because all three were
# present in the live captures and a single "nbsp" number hid them.
_EXOTIC = (
    ("nbsp", "\u00a0"),          # no-break space
    ("nnbsp", "\u202f"),         # narrow no-break space
    ("zwsp", "\u200b"),          # zero-width space
    ("wj", "\u2060"),            # word joiner
    ("bom", "\ufeff"),           # zero-width no-break space
    ("nbhyphen", "\u2011"),      # non-breaking hyphen
)

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
        # `_after_tool` describes the completion currently being buffered; it is
        # latched from `_pending_after_tool` at the Start frame that opens it.
        #
        # Setting `_after_tool` directly on the result frame and clearing it on End
        # was wrong in both directions. Too early: run_function_calls() only SCHEDULES
        # the handler task, and base_llm pushes LLMFullResponseEndFrame immediately
        # afterwards, so the usual order is End -> FunctionCallResultFrame and the flag
        # survived to the next completion only by luck of scheduling — a tool that
        # returned before End cleared it and its completion logged after_tool=False.
        # Too late: with run_llm=False (the async ask_openclaw placeholders) NO
        # completion follows at all, so the flag was cleared by some unrelated later
        # turn, which was then logged after_tool=True. Latching at Start fixes the
        # first; skipping results that suppress inference fixes the second.
        self._after_tool = False
        self._pending_after_tool = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, FunctionCallResultFrame):
            # Only if a completion is actually coming. A result carrying run_llm=False
            # is a placeholder the model never sees (tools.no_inference).
            # getattr for BOTH. run_llm is the legacy duplicate of properties.run_llm and
            # may not survive a pipecat bump; a bare attribute access would raise inside
            # process_frame, and since the single push_frame is the LAST statement of this
            # method the exception would skip it entirely — silently DROPPING the tool
            # result, so no follow-up inference runs and the turn goes quiet. Instrumentation
            # must not be able to do that (cf. the try/except around _report below).
            props = getattr(frame, "properties", None)
            suppressed = (getattr(frame, "run_llm", None) is False
                          or (props is not None and props.run_llm is False))
            if not suppressed:
                self._pending_after_tool = True
        elif isinstance(frame, UserStartedSpeakingFrame):
            # A new user turn: any tool result still waiting for a completion that
            # never came is stale, and must not colour this turn's reply.
            self._pending_after_tool = False
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._buf = []
            self._after_tool = self._pending_after_tool
            self._pending_after_tool = False
        elif isinstance(frame, LLMTextFrame):
            self._buf.append(frame.text or "")
        elif isinstance(frame, LLMFullResponseEndFrame):
            # Never let instrumentation break a live call.
            try:
                self._report()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"RawLLMCapture: report failed (ignored): {e!r}")
            self._buf = []
        await self.push_frame(frame, direction)

    def _report(self):
        raw = "".join(self._buf)
        if not raw or not _DEGENERATE.search(raw):
            return
        # Counts describe the WHOLE completion even though the text is capped.
        dot_runs = len(DOT_RUN.findall(raw))
        ellipses = raw.count("\u2026")
        bold = raw.count("**")
        exotic = " ".join(f"{name}={raw.count(ch)}" for name, ch in _EXOTIC
                          if ch in raw) or "none"
        # repr() so ellipses, non-breaking spaces and newline runs are all visible as
        # escapes rather than collapsing into invisible whitespace in the journal.
        logger.warning(
            f"RawLLMCapture: DEGENERATE completion chars={len(raw)} "
            f"after_tool={self._after_tool} dot_runs={dot_runs} ellipsis={ellipses} "
            f"bold={bold} exotic=[{exotic}] raw={raw[:_MAX_LOG]!r}"
        )
