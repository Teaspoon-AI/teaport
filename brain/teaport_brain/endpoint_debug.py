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

import numpy as np
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

from teaport_brain.env import env_flag, env_num

ENABLED = env_flag("TEAPORT_ENDPOINT_DEBUG", False)

# Per-frame confidence census (see _dbg_sample). 20 buckets = 0.05 resolution, which is
# finer than any threshold anyone would actually set. Silero frames are 512 samples at
# 16 kHz = 32 ms, so 500 frames is ~16 s of audio — frequent enough to get several dumps
# from a short call, rare enough that the census costs one log line per sixteen seconds.
_DIST_BUCKETS = 20
_DIST_EVERY = env_num("TEAPORT_ENDPOINT_DIST_EVERY", "500", int)


class InstrumentedSileroVAD(SileroVADAnalyzer):
    """Silero VAD that logs each state transition with the loudness/confidence
    that drove it — reveals whether real speech sits on top of the min_volume /
    confidence gates (the flicker that can delay endpointing)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dbg_prev = VADState.QUIET
        self._dbg_conf = 0.0
        # Per-frame confidence census — see _dbg_sample. Two histograms of 20 x 0.05
        # buckets, split on the volume gate, plus a frame counter driving the dump.
        self._dbg_loud = [0] * _DIST_BUCKETS
        self._dbg_quiet = [0] * _DIST_BUCKETS
        self._dbg_frames = 0
        # Frames the census could not place — a NaN from a failing probe, say. Counted
        # rather than dropped: silently shrinking the sample is how the last broken
        # instrument hid, and a census with a hole in it is worse than none.
        self._dbg_dropped = 0

    def _dbg_sample(self, conf: float, volume: float) -> None:
        """Record ONE frame, and dump the census every _DIST_EVERY frames.

        Why per-frame and not per-transition: a transition is only logged when the gate
        is crossed, so the sample is truncated BY the gate being measured. Three rounds
        of tuning read the dips as median 0.59 (through a 0.70 gate), then 0.26 (through
        0.35), then still-below-0.15 — each number an artifact of where the gate sat, and
        each one motivating the next move on evidence that could not see past itself.
        A census of every frame is the only thing that shows the real distribution, and
        therefore the only thing that can set this gate once instead of iteratively.

        Split on the volume gate because that is the population that matters: the flicker
        is confidence dropping WHILE the audio is loud enough to be speech (216/216 and
        138/138 of the SPEAKING->STOPPING transitions on 2026-09-09), so `loud` is the
        distribution the confidence threshold is actually deciding on, and `quiet` is the
        control showing what real silence looks like.

        O(1) per frame: this runs in the VAD executor on the hot audio path, so it is one
        bucket increment, and the dump is amortised over _DIST_EVERY frames.
        """
        buckets = self._dbg_loud if volume >= self._params.min_volume else self._dbg_quiet
        try:
            i = int(conf * _DIST_BUCKETS)
        except (TypeError, ValueError):   # NaN, or not a number at all
            self._dbg_dropped += 1
            return
        buckets[min(max(i, 0), _DIST_BUCKETS - 1)] += 1
        self._dbg_frames += 1
        if self._dbg_frames % _DIST_EVERY == 0:
            self._dbg_dump()

    def _dbg_dump(self) -> None:
        def pct(hist, q):
            n = sum(hist)
            if not n:
                return float("nan")
            target, run = n * q, 0
            for i, c in enumerate(hist):
                run += c
                if run >= target:
                    return (i + 0.5) / _DIST_BUCKETS
            return 1.0
        try:
            loud, quiet = self._dbg_loud, self._dbg_quiet
            nl, nq = sum(loud), sum(quiet)
            # The gate's cost, stated directly: how much of LOUD audio — i.e. audio the
            # volume gate already calls speech — this confidence threshold is rejecting.
            below = sum(c for i, c in enumerate(loud)
                        if (i + 0.5) / _DIST_BUCKETS < self._params.confidence)
            logger.info(
                f"[VAD-DIST] loud n={nl} p10={pct(loud, .10):.2f} p50={pct(loud, .50):.2f} "
                f"p90={pct(loud, .90):.2f} | quiet n={nq} p50={pct(quiet, .50):.2f} "
                f"p90={pct(quiet, .90):.2f} | conf gate={self._params.confidence} "
                f"rejects {100 * below / nl if nl else 0:.0f}% of loud frames"
                + (f" | DROPPED {self._dbg_dropped} unplaceable frames"
                   if self._dbg_dropped else "")
            )
            logger.info("[VAD-DIST] loud histogram " + " ".join(
                f"{(i / _DIST_BUCKETS):.2f}:{c}" for i, c in enumerate(loud) if c))
        except Exception:  # noqa: BLE001
            pass  # a census must never break the audio path

    def voice_confidence(self, buffer: bytes) -> float:
        c = super().voice_confidence(buffer)
        # c can be a numpy scalar/array — keep a plain float for logging so a format
        # error can never propagate out of the audio path.
        #
        # .item(), not float(). SileroVADAnalyzer returns `self._model(...)[0]`, which is
        # a shape-(1,) array, and numpy 1.25 deprecated then 2.x REMOVED float() on any
        # array with ndim > 0: "only 0-dimensional arrays can be converted to Python
        # scalars". So on numpy 2.5 (both the box and the lockfile) the old float() raised
        # TypeError on EVERY frame and the except wrote 0.0 — this instrument reported a
        # constant conf=0.00 through the whole call of 2026-09-09 17:33, while the VAD was
        # plainly reaching SPEAKING, which requires confidence >= 0.7. The one field the
        # tool exists to show was the one field it never showed. .item() accepts a 0-d
        # array, a shape-(1,) array and a plain scalar alike.
        #
        # NaN, not 0.0, when it still fails: 0.0 is a VALID-looking confidence, so a broken
        # probe read as "Silero says no voice" and hid itself for as long as anyone cared
        # to look. "nan" in the log is unmistakably an instrument fault, not a measurement.
        # Broad, like the sibling catch in analyze_audio: this runs inside the VAD
        # executor, so anything escaping here kills the transport audio task and with it
        # ALL transcription. A diagnostic must never be able to do that, and np.asarray
        # will run __array__ on whatever it is handed.
        try:
            self._dbg_conf = np.asarray(c).item()
        except Exception:  # noqa: BLE001
            self._dbg_conf = float("nan")
        return c

    def _get_smoothed_volume(self, audio) -> float:
        # _run_analyzer calls voice_confidence() then this, on the SAME frame, so
        # _dbg_conf is this frame's confidence and the pair is coherent. Hooking here
        # rather than overriding _run_analyzer keeps the census out of the VAD's own
        # state machine — nothing about the decision changes, only what is recorded.
        v = super()._get_smoothed_volume(audio)
        self._dbg_sample(self._dbg_conf, v)
        return v

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


def _withheld(shown: bool) -> str:
    """The journal line's note when the chip it accompanies was withheld."""
    return "" if shown else " (chip withheld: bot speaking)"


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
                            + _withheld(shown))
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
                            + _withheld(shown))
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
                                + _withheld(shown))
        await self.push_frame(frame, direction)
