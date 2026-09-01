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
# (not through the LLM), so they don't touch the caption/heard-ledger machinery.
# Turn the whole thing off by unsetting the env; the live pipeline is unchanged.
#
# A bubble due while the bot is audibly speaking is WITHHELD — dropped, not
# delayed. On the wire these are indistinguishable from reply text
# (role=assistant, final=true — the only transcript shape the OpenClaw relay
# accepts), and the official OpenClaw Talk UI keeps ONE open assistant entry:
# an assistant final landing while reply captions are streaming is APPENDED to
# the open reply bubble and closes it early (ui realtime-talk-conversation.ts,
# mergeAssistantTranscriptText). A chip for a backchannel spoken over the bot
# ("mm-hm" that never barges) therefore ended up INSIDE the reply bubble.
# Outside bot playout no assistant entry is open, so a chip opens and closes
# its own bubble — the intended rendering. Not delayed-and-flushed on
# BotStopped, because that flush races the reply's caption final to the
# transport and can still merge; dropping is race-free, and the journal log
# lines are never withheld, so the measurement itself stays complete — only
# the chip is display-best-effort. (The client would be the cleaner seam, but
# teaport runs against the official OpenClaw build, not a fork.)
#
import time

from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
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

from teaport_brain.env import env_flag

ENABLED = env_flag("TEAPORT_ENDPOINT_DEBUG", False)


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
        # Whether the bot is audibly speaking, tracked from the output transport's
        # Bot{Started,Stopped}Speaking frames (they pass both taps traveling
        # upstream). While True, chips are withheld — see the module docstring:
        # an assistant-final transcript arriving mid-reply is folded into the
        # open reply bubble by the OpenClaw Talk UI.
        self._bot_speaking = False

    async def _chip(self, text: str) -> bool:
        """Emit the bubble unless the bot is mid-reply. Returns whether it went
        out, so the (never-withheld) journal line can say when it didn't."""
        if self._bot_speaking:
            return False
        await _bubble(self, text)
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        m = self._m
        t = time.monotonic()
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        # Smart Turn's verdict rides a MetricsFrame that the user aggregator queues
        # DOWNSTREAM of itself, so the "in" tap can never see it and TURN-COMMIT below
        # has never been able to print it.
        #
        # "out" ONLY, and deliberately NOT written into the shared marks dict. Both taps
        # share one dict, so recording it from whichever tap happened to see the frame
        # logged the same verdict twice; and because the frame has to traverse the LLM and
        # the TTS before reaching the "out" tap, it routinely arrives after the NEXT turn's
        # VAD-start has already cleared the dict — at which point the value would be read
        # back as the next turn's verdict. A number that is sometimes a lie is worse than
        # no number in a probe whose whole purpose is attribution.
        #
        # No "Nms after VAD-stop" either: that interval is dominated by LLM + TTS transit
        # to this tap, so it measured pipeline latency and called it endpointing latency.
        if self._stage == "out" and isinstance(frame, MetricsFrame):
            for d in (frame.data or []):
                if isinstance(d, TurnMetricsData):
                    logger.info(f"[EP] SmartTurn verdict "
                                f"{'COMPLETE' if d.is_complete else 'INCOMPLETE'} "
                                f"p={d.probability:.3f} (seen at the output tap; the "
                                f"verdict is made upstream, before turn-commit)")
        if self._stage == "in":
            if isinstance(frame, VADUserStartedSpeakingFrame):
                m.clear()
                m["speech_start"] = t
                logger.info("[EP] VAD speech STARTED")
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                m["vad_stop"] = t
                ss = m.get("speech_start")
                dur = f" (utterance {(t - ss) * 1000:.0f}ms)" if ss else ""
                shown = await self._chip("🎙️ VAD: speech stopped")
                logger.info(f"[EP] VAD-STOP{dur}"
                            + ("" if shown else " (chip withheld: bot speaking)"))
            elif isinstance(frame, UserStoppedSpeakingFrame):
                m["commit"] = t
                vs = m.get("vad_stop")
                tail = f"+{(t - vs) * 1000:.0f}ms after VAD-stop" if vs else "(no VAD-stop seen)"
                # No verdict field. It cannot be here: the MetricsFrame carrying it is
                # queued downstream of the aggregator and reaches the "out" tap a whole
                # LLM+TTS traversal later, so any value present at this instant would
                # belong to a previous turn. The verdict has its own line above.
                shown = await self._chip(f"⏱️ turn committed {tail}")
                logger.info(f"[EP] TURN-COMMIT {tail}"
                            + ("" if shown else " (chip withheld: bot speaking)"))
        else:  # "out"
            if isinstance(frame, TTSAudioRawFrame) and not m.get("audio_done"):
                m["audio_done"] = True
                c = m.get("commit")
                if c:
                    # This chip is normally safe (synthesis precedes playout, so the
                    # bot isn't speaking yet), but a turn CHAINED onto a still-playing
                    # reply pushes its first audio mid-playout — withheld then too.
                    shown = await self._chip(
                        f"🔊 first audio +{(t - c) * 1000:.0f}ms after turn commit")
                    logger.info(f"[EP] FIRST-AUDIO +{(t - c) * 1000:.0f}ms after commit"
                                + ("" if shown else " (chip withheld: bot speaking)"))
        await self.push_frame(frame, direction)
