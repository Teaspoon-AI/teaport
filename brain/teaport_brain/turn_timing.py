#
# teaport — live per-turn latency taps, and the watchdog that says when a turn
# produced no reply at all.
#
import asyncio
import time

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.env import env_num

# How long a committed turn may stay silent before it is reported. Short enough that the
# line lands while the session is still live.
#
# env_num, not a bare float(): this value lives in /etc/teaport/brain.env, which installer
# repairs preserve verbatim, so `TEAPORT_SILENT_TURN_SECS=` or `=off` from a bare cast
# would raise at IMPORT time — and gateway_server imports this module at module scope, so
# the brain would crash-loop with no way to clear it short of hand-editing the file. See
# env.py, which exists for exactly this.
_SILENT_TURN_SECS = env_num("TEAPORT_SILENT_TURN_SECS", "12", float)

# A tool call legitimately owns the turn for far longer than the watchdog window:
# _ASK_OPENCLAW_TIMEOUT is 55s and _NATIVE_CONSULT_TIMEOUT 45s (tools.py), and in
# agent-first mode _fallback_line returns None so nothing is spoken until the consult
# answers. Barking at 12s into a working consult made the one diagnostic this module adds
# fire on the pipeline's slowest NORMAL path, which is how a warning becomes noise. So the
# watchdog re-arms while a tool is in flight instead of reporting, and only gives up after
# this many consecutive windows — long enough to outlast any tool in the repo, while a
# turn that is silent for a non-tool reason is still reported at the first window.
_MAX_TOOL_EXTENSIONS = 6

# A final transcript that arrives just BEFORE the turn commits is the normal Smart Turn
# ordering, and the commit handler clears the marks. Remembering that we saw one, for this
# long, is what lets the watchdog distinguish "STT produced nothing" from "STT worked and
# the model was never asked" — see _bark.
_FINAL_CARRY_SECS = 5.0


class TurnTimer(FrameProcessor):
    """Live per-turn latency taps (observation only). Three instances — after STT,
    after the LLM, after TTS — share one per-session `marks` dict and log one line
    per turn: time from user-stopped to stt-final / llm-start / llm-first-token /
    tts-first-audio. Lets us read the WARM pipeline latency the cold e2e harness
    can't see. The dict is per-session (not class-level) because single-slot
    eviction briefly overlaps two sessions — a dead session's t0 would otherwise
    corrupt the replacement's first TURN-TIMING line."""

    def __init__(self, marks: dict, watchdog: bool = False):
        super().__init__()
        self._marks = marks
        # Only one of the three taps arms the silent-turn watchdog. They all see
        # UserStoppedSpeakingFrame and share one dict, so without an owner every silent
        # turn would be reported three times.
        self._owns_watchdog = watchdog
        self._watchdog = None
        self._turn = 0
        # Last final transcript this tap saw, whether or not it survived the commit clear.
        self._recent_final = None
        # Tool calls dispatched but not yet resolved, for this turn.
        self._tools_in_flight = 0

    # A committed turn that never speaks is the single most common way this pipeline
    # fails, and until now it left NOTHING in the journal — TURN-TIMING only logs on
    # first audio, so a turn that produced none simply had no line. Five distinct causes
    # were found this way on 2026-08-20, each costing a live session and a long forensic
    # dig, and every one of them would have been named instantly by this warning:
    # whichever marks are missing say exactly how far the turn got.
    async def _bark(self, turn: int):
        # No `except CancelledError: return`. Swallowing it made a cancelled watchdog
        # complete NORMALLY, so pipecat's cancel_task could not tell it apart from one
        # that ran to term, and the TaskManager contract ("re-raise to ensure the task is
        # cancelled") was quietly broken. Letting it propagate is the whole protocol.
        for _ in range(_MAX_TOOL_EXTENSIONS):
            await asyncio.sleep(_SILENT_TURN_SECS)
            m = self._marks
            if turn != self._turn or m.get("done"):
                return
            if not self._tools_in_flight:
                break
            logger.debug(
                f"silent-turn watchdog: {self._tools_in_flight} tool call(s) still in "
                f"flight after {_SILENT_TURN_SECS:.0f}s — extending"
            )
        else:
            # Fell out of the loop still waiting on a tool: that is its own failure.
            logger.warning(
                f"SILENT TURN: tool call still unresolved after "
                f"{_SILENT_TURN_SECS * _MAX_TOOL_EXTENSIONS:.0f}s — the consult never "
                f"returned and nothing was spoken"
            )
            return
        m = self._marks
        reached = [k for k in ("stt_final", "llm_start", "llm_first_token") if k in m]
        # stt_final normally arrives just BEFORE the commit that clears the marks, so its
        # absence above says nothing about STT. Without this the warning blamed "the model
        # was never asked" on every turn including the ones where STT worked perfectly.
        if "stt_final" not in reached and m.get("had_final"):
            reached.insert(0, "stt_final(pre-commit)")
        logger.warning(
            f"SILENT TURN: {_SILENT_TURN_SECS:.0f}s after user-stopped and no audio. "
            f"reached={reached or ['nothing']} — "
            + ("the model was never asked (empty aggregation, or the turn was "
               "force-stopped without inference)" if "llm_start" not in m
               else "the model answered but nothing was spoken")
        )

    # pipecat's FrameProcessor.cleanup() cancels only its own internal input/process
    # tasks, never ones made with create_task. Without this a caller who hung up mid-turn
    # left _bark sleeping: the task showed up in "Dangling tasks detected", held this
    # processor and the session's marks for the rest of the window, and then warned about
    # a SILENT TURN for a session that had simply ended.
    async def cleanup(self):
        if self._watchdog:
            await self.cancel_task(self._watchdog)
            self._watchdog = None
        await super().cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        t = time.monotonic()
        if isinstance(frame, (EndFrame, CancelFrame)):
            if self._watchdog:
                await self.cancel_task(self._watchdog)
                self._watchdog = None
        elif isinstance(frame, UserStoppedSpeakingFrame):
            had_final = (self._recent_final is not None
                         and t - self._recent_final <= _FINAL_CARRY_SECS)
            self._marks.clear()
            self._marks["t0"] = t
            if had_final:
                self._marks["had_final"] = True
            if self._owns_watchdog:
                self._turn += 1
                self._tools_in_flight = 0
                if self._watchdog:
                    await self.cancel_task(self._watchdog)
                self._watchdog = self.create_task(self._bark(self._turn))
        elif isinstance(frame, FunctionCallInProgressFrame):
            self._tools_in_flight += 1
        elif isinstance(frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
            self._tools_in_flight = max(0, self._tools_in_flight - 1)
        elif self._marks and not self._marks.get("done"):
            m = self._marks
            if isinstance(frame, TranscriptionFrame):
                self._recent_final = t
                m.setdefault("stt_final", t)
            elif isinstance(frame, LLMFullResponseStartFrame):
                m.setdefault("llm_start", t)
            elif isinstance(frame, LLMTextFrame):
                m.setdefault("llm_first_token", t)
            elif isinstance(frame, TTSAudioRawFrame):
                m.setdefault("tts_first_audio", t)
                m["done"] = True
                t0 = m["t0"]
                seg = "  ".join(
                    f"{k}=+{m[k]-t0:.2f}s"
                    for k in ("stt_final", "llm_start", "llm_first_token", "tts_first_audio")
                    if k in m
                )
                logger.info(f"TURN-TIMING (after user-stopped)  {seg}")
        elif isinstance(frame, TranscriptionFrame):
            # Outside a live turn (e.g. the final that TRIGGERS the commit) — still worth
            # remembering, so the watchdog can tell STT worked. See _bark.
            self._recent_final = t
        await self.push_frame(frame, direction)
