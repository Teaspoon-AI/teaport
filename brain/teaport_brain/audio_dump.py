#
# audio_dump.py — capture the caller PCM the brain actually receives (opt-in).
#
# Why this exists. Measured live 2026-09-09 on the SIP path, per second of call:
#
#     VAD sees speech start      bot-speaking 0.087   bot-quiet 0.112   (1.3x)
#     VAD confirms speech        bot-speaking 0.069   bot-quiet 0.110   (1.6x)
#     STT produced an interim    bot-speaking 0.009   bot-quiet 0.165   (18.6x)
#
# The caller's audio ARRIVES while the bot is talking — Silero detects speech at very
# nearly the same rate either way, which is about the difference you would expect from
# people simply talking less while listening. The engine's STT then transcribes almost
# none of it. A 1.3x difference going in becomes 18.6x coming out, so the loss is
# entirely between the VAD and the transcript, on audio that is present and
# speech-shaped enough to trip a speech detector but not to feed an ASR.
#
# It is NOT the echo canceller. That was tested directly: caller finals per second while
# the bot spoke measured 0.012 at pjmedia's default aggressiveness, 0.011 at
# conservative, and 0.009 with `aec = false` — invariant across the whole range. With the
# canceller fully off the bot's own voice never came back as caller text either, so on
# this line there is no meaningful acoustic echo path to begin with.
#
# What is left is a claim about BYTES, and three theories were wrong before this one, so
# this module stops inferring and records them. Point TEAPORT_AUDIO_DUMP at a directory
# and every call writes:
#
#   caller-<callid>.wav    the exact PCM handed to the VAD/STT, 16 kHz mono s16
#   caller-<callid>.marks  bot playout spans as SAMPLE OFFSETS into that wav
#
# The marks are what make it usable: double-talk is the region of interest and it is a
# few seconds inside a ten-minute recording. Offsets are in samples of the wav itself, so
# a region can be cut with no clock arithmetic and no dependence on log timestamps.
#
# Off unless TEAPORT_AUDIO_DUMP is set. It records the CALLER side only — that is what
# the STT consumes, and it is the side in question.
#
import os
import wave

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.env import env_num

# A directory, or empty for off. Deliberately not a bool: a capture needs somewhere to
# go, and naming the destination is the same act as asking for one.
DUMP_DIR = (os.getenv("TEAPORT_AUDIO_DUMP") or "").strip()
ENABLED = bool(DUMP_DIR)

# Ceiling per call, in seconds of audio. A debug tap must not be the reason a box runs
# out of disk during a long call: 16 kHz mono s16 is 32 kB/s, so 600 s is ~19 MB and the
# tap simply stops writing past it (the wav stays valid and the marks keep accruing).
MAX_SECS = env_num("TEAPORT_AUDIO_DUMP_MAX_SECS", "600", float)


class CallerAudioTap(FrameProcessor):
    """Write caller PCM to a wav, and bot-playout spans to a sidecar, then pass through.

    Inserted via build_agent_session(input_processors=...), so it sits right after
    transport.input() and sees exactly the frames the VAD and STT go on to see — not a
    reconstruction of them, which is the whole point.
    """

    def __init__(self, call_id: str, sample_rate: int = 16000):
        super().__init__()
        self._sr = sample_rate
        safe = "".join(c for c in (call_id or "call") if c.isalnum() or c in "-_")[:64]
        self._wav_path = os.path.join(DUMP_DIR, f"caller-{safe}.wav")
        self._marks_path = os.path.join(DUMP_DIR, f"caller-{safe}.marks")
        self._wav = None
        self._marks = None
        self._samples = 0
        self._capped = False
        try:
            os.makedirs(DUMP_DIR, exist_ok=True)
            self._wav = wave.open(self._wav_path, "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(sample_rate)
            self._marks = open(self._marks_path, "w", encoding="utf-8")
            self._marks.write(f"# sample offsets into {os.path.basename(self._wav_path)}"
                              f" @ {sample_rate} Hz mono s16\n")
            self._marks.flush()
            logger.info(f"audio dump: capturing caller PCM to {self._wav_path}")
        except OSError as e:
            # A tap that cannot open its files must not stop the call.
            logger.warning(f"audio dump: disabled — cannot write to {DUMP_DIR!r} ({e})")
            self._close()

    def _close(self):
        for f in (self._wav, self._marks):
            try:
                if f is not None:
                    f.close()
            except OSError:
                pass
        self._wav = self._marks = None

    def _mark(self, what: str):
        if self._marks is None:
            return
        try:
            self._marks.write(f"{self._samples} {what}\n")
            self._marks.flush()   # flushed per mark: a call that ends badly still leaves
        except OSError:           # a usable sidecar, and marks are a few per minute
            pass

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # Record first, forward second — but NEVER let recording block the audio path.
        try:
            if isinstance(frame, InputAudioRawFrame) and self._wav is not None:
                if self._samples < MAX_SECS * self._sr:
                    self._wav.writeframes(frame.audio)
                    self._samples += len(frame.audio) // 2
                elif not self._capped:
                    self._capped = True
                    self._mark("CAP reached; no further audio recorded")
                    logger.warning(f"audio dump: {MAX_SECS}s cap reached for "
                                   f"{self._wav_path}; still marking, not recording")
            elif isinstance(frame, BotStartedSpeakingFrame):
                self._mark("BOT_START")
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._mark("BOT_STOP")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"audio dump: stopping capture after {e!r}")
            self._close()
        await self.push_frame(frame, direction)

    async def cleanup(self):
        secs = self._samples / self._sr if self._sr else 0
        if self._wav is not None:
            logger.info(f"audio dump: wrote {secs:.1f}s to {self._wav_path}")
        self._close()
        await super().cleanup()
