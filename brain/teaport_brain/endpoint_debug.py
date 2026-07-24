#
# endpoint_debug.py — LIVE endpointing instrumentation (opt-in, TEAPORT_ENDPOINT_DEBUG=1).
#
# Answers "where does the post-speech lag actually go?" on the REAL mic path, in
# real time, without recordings:
#   - InstrumentedSileroVAD logs every VAD state transition with the loudness
#     (vs min_volume) and speech-confidence (vs the confidence gate) to the
#     journal — so you can see whether real speech hugs/flickers the volume gate.
#   - EndpointDebug (two taps sharing one per-session dict, like TurnTimer) emits
#     a chat bubble at each stage so the cascade is visible in the Talk UI while
#     you talk: VAD-stop → turn-commit (with the VAD-stop→commit delay + Smart
#     Turn verdict/probability) → first audio (reply-pipeline delay).
#
# Debug-only: the bubbles are pushed as plain assistant transcript messages
# (not through the LLM), so they render as separate ⏱️ bubbles and don't touch
# the caption/heard-ledger machinery. Turn the whole thing off by unsetting the
# env; the live pipeline is unchanged.
#
import os
import time

from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import (
    Frame,
    MetricsFrame,
    OutputTransportMessageUrgentFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TurnMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

ENABLED = os.getenv("TEAPORT_ENDPOINT_DEBUG", "").strip().lower() in ("1", "true")


class InstrumentedSileroVAD(SileroVADAnalyzer):
    """Silero VAD that logs each state transition with the loudness/confidence
    that drove it — reveals whether real speech sits on top of the min_volume /
    confidence gates (the flicker that can delay endpointing)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dbg_prev = VADState.QUIET
        self._dbg_conf = 0.0

    def voice_confidence(self, buffer: bytes) -> float:
        c = super().voice_confidence(buffer)
        # c can be a numpy scalar/array — keep a plain float for logging so a
        # format error can never propagate out of the audio path.
        try:
            self._dbg_conf = float(c)
        except (TypeError, ValueError):
            self._dbg_conf = 0.0
        return c

    async def analyze_audio(self, buffer: bytes) -> VADState:
        state = await super().analyze_audio(buffer)
        # Logging is best-effort: a formatting slip must NEVER break the VAD
        # (that kills the transport audio task -> no transcription).
        if state != self._dbg_prev:
            try:
                vol = float(getattr(self, "_prev_volume", 0) or 0)
                logger.info(
                    f"[VAD] {self._dbg_prev.name}->{state.name}  "
                    f"vol={vol:.2f}/{self._params.min_volume} "
                    f"conf={self._dbg_conf:.2f}/{self._params.confidence}"
                )
            except Exception:  # noqa: BLE001
                pass
            self._dbg_prev = state
        return state


async def _bubble(proc: FrameProcessor, text: str) -> None:
    await proc.push_frame(
        OutputTransportMessageUrgentFrame(message={
            "type": "transcript", "role": "assistant", "final": True, "text": text}),
        FrameDirection.DOWNSTREAM,
    )


class EndpointDebug(FrameProcessor):
    """Per-session timing taps. Instantiate two, sharing one dict:
    stage="in" (after transport.input) watches VAD/turn/metrics + emits the
    endpointing bubbles; stage="out" (after tts) emits the first-audio bubble."""

    def __init__(self, marks: dict, stage: str):
        super().__init__()
        self._m = marks
        self._stage = stage

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        m = self._m
        t = time.monotonic()
        if self._stage == "in":
            if isinstance(frame, VADUserStartedSpeakingFrame):
                m.clear()
                m["speech_start"] = t
            elif isinstance(frame, VADUserStartedSpeakingFrame):
                logger.info("[EP] VAD speech STARTED")
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                m["vad_stop"] = t
                ss = m.get("speech_start")
                dur = f" (utterance {(t - ss) * 1000:.0f}ms)" if ss else ""
                logger.info(f"[EP] VAD-STOP{dur}")
                await _bubble(self, "🎙️ VAD: speech stopped")
            elif isinstance(frame, MetricsFrame):
                for d in (frame.data or []):
                    if isinstance(d, TurnMetricsData):
                        m["st_prob"] = d.probability
                        m["st_complete"] = d.is_complete
                        vs = m.get("vad_stop")
                        since = f"{(t - vs) * 1000:.0f}ms after VAD-stop" if vs else "?"
                        logger.info(f"[EP] SmartTurn verdict "
                                    f"{'COMPLETE' if d.is_complete else 'INCOMPLETE'} "
                                    f"p={d.probability:.3f} ({since})")
            elif isinstance(frame, UserStoppedSpeakingFrame):
                m["commit"] = t
                vs = m.get("vad_stop")
                tail = f"+{(t - vs) * 1000:.0f}ms after VAD-stop" if vs else "(no VAD-stop seen)"
                p = m.get("st_prob")
                if p is not None:
                    verdict = "✓COMPLETE" if m.get("st_complete") else "✗INCOMPLETE"
                    ver = f" · SmartTurn {verdict} p={p:.2f}"
                else:
                    ver = " · SmartTurn (no verdict — silence path)"
                logger.info(f"[EP] TURN-COMMIT {tail}{ver}")
                await _bubble(self, f"⏱️ turn committed {tail}{ver}")
        else:  # "out"
            if isinstance(frame, TTSAudioRawFrame) and not m.get("audio_done"):
                m["audio_done"] = True
                c = m.get("commit")
                if c:
                    logger.info(f"[EP] FIRST-AUDIO +{(t - c) * 1000:.0f}ms after commit")
                    await _bubble(self, f"🔊 first audio +{(t - c) * 1000:.0f}ms after turn commit")
        await self.push_frame(frame, direction)
