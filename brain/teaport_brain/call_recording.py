#
# call_recording.py — the owner's phone-call recording (#114).
#
# One owner setting, TEAPORT_CALL_RECORDING = off | audio | transcript | both, separate
# from the diagnostic switches and off by default. Recording-consent law differs by
# place (one party's consent in most US states, everyone's in California and about a
# dozen others, other rules elsewhere); the owner chooses and is responsible. Phone
# calls only: a Talk session or the room mic is the owner's own conversation, and is
# never recorded by this setting.
#
# Per call, named by its start time and call id in TEAPORT_CALL_RECORDING_DIR:
#
#   <YYYYmmdd-HHMMSS>-<callid>.wav   both sides, aligned: the caller on the left
#                                    channel, the bot on the right (audio, both)
#   <YYYYmmdd-HHMMSS>-<callid>.txt   the caller's and the bot's lines, timed from the
#                                    start of the call (transcript, both)
#
# A call a restarted brain picks up again (the gateway replays it) continues the
# recording it had: its rest goes to <stem>.r1.wav/.txt beside the first part, one group
# that retention keeps or deletes whole (recordings.py).
#
# NOTHING IS RECORDED UNTIL THE CALLER IS TOLD. AgentSession.greet arms the recording
# (arm), and it starts when the notice's first word PLAYS -- with the notice off, the
# greeting's: _PlayedLines, below the output transport, sees the word as it is played
# and pushes pipecat's own AudioBufferStartRecordingFrame straight into the recorder.
# Started there and not queued from the top, no interruption can delete the start on its
# way, and a notice that never plays starts nothing (greet says so in the log). A call
# that cannot be heard is never armed (greet speaks the "I can't hear you" line instead).
# Before the start no file exists: a call that never connects leaves none. Once the
# notice has started playing, the recording runs on: the caller is unheard while it
# plays and the brain does not cut it, so a notice cut after its first word is not a
# case greet() has.
#
# AUDIO is pipecat's AudioBufferProcessor, below the output transport, where it sees
# the caller's audio on its way down and the bot's as it is played: what the caller
# heard, the typing bed and any cut reply included, merged into one stereo track at the
# call's rate (16 kHz). It is flushed to the wav every few seconds (_FLUSH_SECS) rather
# than held for the whole call, so a long call costs no memory and a crash keeps all but
# the last seconds. While the half-duplex gate (SIP_HALF_DUPLEX), the notice or a
# session's last line mutes the caller, their side is silence: the brain did not take
# that audio.
#
# The TRANSCRIPT has two sources. The transcript ledger (transcript_ledger.py) gives the
# caller's finals and each reply as far as the caller heard it -- a reply they cut is
# written as what they heard, marked cut off. Not the LLM context, which keeps a cut
# reply's unheard words until the corrector trims them. _PlayedLines, below the
# transport, gives what the ledger does not chart: the lines the pipeline says itself
# (the notice, a tool's "one moment", a failure notice: append_to_context=False), each
# once it has played, and whatever was still playing when the call ended. It keeps a
# reply's words only while it plays: its stop or its cut is the ledger's to chart.
#
# The NOTICE (TEAPORT_CALL_RECORDING_NOTICE, on by default): a recorded call starts with
# "This call is being recorded." in the call's language (i18n.py), before the greeting,
# outside the conversation's context, with the caller unheard while it plays so they
# cannot talk it away (AgentSession.greet). A resumed call hears it again: the brain that
# was cut off may not have finished saying it. The model is also told the call is
# recorded, so a caller who asks gets a straight answer.
#
# RETENTION: recordings older than TEAPORT_CALL_RECORDING_KEEP_DAYS go, then the oldest
# until the directory is under TEAPORT_CALL_RECORDING_MAX_MB (recordings.prune), checked
# as each recording starts.
#
import glob
import os
import re
import time
import wave

from loguru import logger

from pipecat.frames.frames import (
    AudioBufferStartRecordingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain import i18n, recordings
from teaport_brain.settings import setting
from teaport_brain.sip_serializer import PIPELINE_SAMPLE_RATE

MODE = setting("TEAPORT_CALL_RECORDING")
NOTICE = setting("TEAPORT_CALL_RECORDING_NOTICE")
DIR = setting("TEAPORT_CALL_RECORDING_DIR")
KEEP_DAYS = setting("TEAPORT_CALL_RECORDING_KEEP_DAYS")
MAX_MB = setting("TEAPORT_CALL_RECORDING_MAX_MB")

ENABLED = MODE != "off"

# How much audio the processor holds before it hands it to the wav, per side.
_FLUSH_SECS = 5
_FLUSH_BYTES = PIPELINE_SAMPLE_RATE * 2 * _FLUSH_SECS

# Our files' names: <YYYYmmdd-HHMMSS>-<callid>, the only ones pruning touches.
_NAMES = re.compile(r"\d{8}-\d{6}-")


def notice_line(espeak_language: str | None) -> str:
    """The recording notice, in the language the call's voice speaks."""
    return i18n.for_espeak(espeak_language)._("This call is being recorded.")


def start_frame() -> AudioBufferStartRecordingFrame:
    """pipecat's start for the recorder, kept through an interruption (Frame.interruptible,
    set per frame): it is made only once the caller has been told, and an interruption
    landing in the recorder's queue must not lose it."""
    frame = AudioBufferStartRecordingFrame()
    frame.interruptible = False
    return frame


def _clock(secs: float) -> str:
    """mm:ss.s, rounded to the tenth before it is split (59.96 s is 01:00.0)."""
    m, tenths = divmod(round(max(secs, 0.0) * 10), 600)
    return f"{m:02d}:{tenths // 10:02d}.{tenths % 10}"


def _resumed_stem(directory: str, safe: str) -> str | None:
    """The newest recording of call `safe` in `directory`: the one a resumed call
    continues."""
    found = [p for p in glob.glob(os.path.join(glob.escape(directory), f"*-{safe}*"))
             if _NAMES.match(os.path.basename(p))
             and os.path.basename(p).split(".", 1)[0].endswith(f"-{safe}")]
    if not found:
        return None
    return os.path.join(directory, os.path.basename(max(found, key=os.path.getmtime))
                        .split(".", 1)[0])


class _PlayedLines(FrameProcessor):
    """Below the output transport: the words of each TTS context as they play. Starts an
    armed recording at the first word it is armed for (CallRecording.arm), and writes the
    lines the ledger does not chart -- the pipeline's own (append_to_context=False), each
    when it has played or is cut -- and, at the call's end, whatever was still playing.
    A reply's words are kept only while it plays: its stop or its cut is the ledger's."""

    def __init__(self, recording: "CallRecording"):
        super().__init__()
        self._rec = recording
        self._own: set = set()          # context ids of the pipeline's own lines
        self._armed_own: set = set()    # ... of those, the ones started while armed
        self._words: dict = {}          # context id -> [start secs, [words]]

    def _now(self) -> float:
        return self.get_clock().get_time() / 1e9

    def _flush(self, ctx, how: str = "") -> None:
        start, words = self._words.pop(ctx, (None, None))
        self._own.discard(ctx)
        self._armed_own.discard(ctx)
        if words:
            self._rec.line(start, f"Assistant{how}", "".join(words).strip())

    async def _start_on(self, ctx) -> None:
        """An armed recording starts at the first word of what it was armed for."""
        armed = self._rec.armed
        # "notice": a pipeline line that STARTED while armed -- the notice, queued after
        # the arming; not one already playing when greet() armed it.
        if armed == "any" or (armed == "notice" and ctx in self._armed_own):
            self._rec.begin()
            # Straight into the recorder below, and kept through an interruption.
            await self.push_frame(start_frame())

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        try:
            if isinstance(frame, AudioBufferStartRecordingFrame):
                self._rec.begin()       # queued from the top (greet's fallback)
            elif isinstance(frame, TTSStartedFrame):
                if getattr(frame, "append_to_context", True) is False:
                    self._own.add(frame.context_id)
                    if self._rec.armed == "notice":
                        self._armed_own.add(frame.context_id)
            elif isinstance(frame, TTSTextFrame) and frame.text:
                ctx = getattr(frame, "context_id", None)
                if not self._rec.started:
                    await self._start_on(ctx)
                if self._rec.started and self._rec.transcript:
                    entry = self._words.setdefault(ctx, [self._now(), []])
                    spaced = getattr(frame, "includes_inter_frame_spaces", False)
                    entry[1].append(frame.text if spaced else frame.text + " ")
            elif isinstance(frame, TTSStoppedFrame):
                if frame.context_id in self._own:
                    self._flush(frame.context_id)
                else:
                    self._words.pop(frame.context_id, None)   # the ledger charts it
            elif isinstance(frame, InterruptionFrame):
                for ctx in list(self._words):
                    if ctx in self._own:
                        self._flush(ctx, " (cut off)")
                    else:
                        del self._words[ctx]                   # the ledger charts the cut
            elif isinstance(frame, (EndFrame, CancelFrame)):
                for ctx in list(self._words):
                    self._flush(ctx, " (call ended)")
        except Exception as e:  # noqa: BLE001 -- a transcript must never stop a call
            logger.warning(f"call recording: played-lines tap failed ({e!r})")
        await self.push_frame(frame, direction)


class CallRecording:
    """One call's recording: what build_agent_session puts in its pipeline (processors),
    the ledger it listens to (attach), what the model is told (prompt_line), and the
    word that starts it (arm, by AgentSession.greet)."""

    def __init__(self, call_id: str, caller: str | None = None, *, mode: str | None = None,
                 directory: str | None = None, resumed: bool = False):
        mode = mode or MODE
        self.directory = directory or DIR
        self.mode = mode
        self.audio = mode in ("audio", "both")
        self.transcript = mode in ("transcript", "both")
        self.started = False
        # What starts it (arm): "notice" -- the notice's first word; "any" -- any word.
        self.armed = None
        self._call_id, self._caller, self._resumed = call_id, caller, resumed
        safe = "".join(c for c in (call_id or "call") if c.isalnum() or c in "-_")[:64]
        base = (_resumed_stem(self.directory, safe) if resumed else None) or os.path.join(
            self.directory, f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}")
        self.stem = recordings.free_stem(base)
        self.wav_path = self.stem + ".wav"
        self.txt_path = self.stem + ".txt"
        self._wav = None
        self._txt = None
        self._buffer = None
        # The tap also opens the recording when the start frame passes (begin), so it is
        # there in every mode.
        self._played = _PlayedLines(self)
        if self.audio:
            # Started by the start frame (greet), not at the pipeline's start: nothing
            # is recorded before the caller is told.
            self._buffer = AudioBufferProcessor(sample_rate=PIPELINE_SAMPLE_RATE,
                                                num_channels=2, buffer_size=_FLUSH_BYTES)
            # Plain functions, not coroutines: pipecat runs each handler in a task of its
            # own, started in order, and a handler with no await runs whole when its
            # task starts -- so the chunks reach the file in order, and the close after
            # the last of them. An await in either would break that.
            self._buffer.add_event_handler("on_audio_data", self._on_audio)
            self._buffer.add_event_handler("on_recording_stopped", self._on_stopped)

    # ---------------------------------------------------------------- pipeline

    @property
    def processors(self) -> list:
        """What goes directly below the output transport."""
        return [p for p in (self._played, self._buffer) if p is not None]

    @property
    def active(self) -> bool:
        """Something of this call is to be kept (False once it could not be written)."""
        return self.audio or self.transcript

    @property
    def prompt_line(self) -> str | None:
        """The system line telling the model the call is recorded; None when it is not."""
        if not self.active:
            return None
        what = ("its audio and a transcript" if self.audio and self.transcript
                else "its audio" if self.audio else "a transcript of it")
        return (f"This phone call is being recorded ({what} is kept). If the caller asks "
                "whether the call is recorded, tell them it is.")

    def arm(self, on: str | None) -> None:
        """Start at the first word that plays of the notice ("notice") or of anything
        ("any": the notice is off, so the greeting's); None disarms."""
        self.armed = on if self.active else None

    def attach(self, ledger) -> None:
        """Write the ledger's lines to the transcript as it charts them."""
        if self.transcript:
            ledger.on_utterance = self.on_utterance

    def begin(self) -> None:
        """The start frame reached the recorder: open the transcript, prune old ones."""
        if self.started:
            return
        self.started = True
        try:
            os.makedirs(self.directory, exist_ok=True)
            if self.transcript:
                self._txt = open(self.txt_path, "w", encoding="utf-8")
        except OSError as e:
            # A recording that cannot be written must not stop the call.
            logger.warning(f"call recording: off for this call -- cannot write to "
                           f"{self.directory} ({e})")
            self.audio = self.transcript = False
            return
        logger.info(f"call recording ({self.mode}): {self.stem}.*")
        recordings.prune_soon(self.directory, KEEP_DAYS, MAX_MB, _NAMES,
                              keep=frozenset({os.path.basename(self.stem).split(".", 1)[0]}))
        if self.transcript:
            started = time.strftime("%Y-%m-%d %H:%M:%S %z")
            self._write(f"Call {self._call_id} from {self._caller or 'an unknown caller'}, "
                        f"{'resumed' if self._resumed else 'started'} {started}. Times are "
                        "from the start of this part of the call.\n\n")

    # ---------------------------------------------------------------- transcript

    def _write(self, text: str) -> None:
        if self._txt is None:
            return
        try:
            self._txt.write(text)
            self._txt.flush()   # per line: a call that ends badly keeps what it had
        except OSError as e:
            logger.warning(f"call recording: transcript stopped ({e})")
            self._close_txt()

    def line(self, secs: float | None, who: str, text: str) -> None:
        """One transcript line, at `secs` on the pipeline clock."""
        at = "--:--.-" if secs is None else _clock(secs)
        self._write(f"[{at}] {who}: {text}\n")

    def on_utterance(self, u) -> None:
        """One ledger line: the caller's final, or a reply as far as it was heard."""
        if not self.started:
            return
        if u.speaker == "user":
            self.line(u.t_start, "Caller", u.text)
        elif u.cut_short:
            self.line(u.t_start, "Assistant (cut off)", (u.heard_text or "").strip() or "...")
        else:
            self.line(u.t_start, "Assistant", u.text)

    def _close_txt(self) -> None:
        if self._txt is not None:
            try:
                self._txt.close()
            except OSError:
                pass
            self._txt = None

    # ---------------------------------------------------------------- audio

    def _on_audio(self, _buffer, audio: bytes, sample_rate: int, num_channels: int) -> None:
        if not self.audio:
            return  # stopped by a write failure: never reopen (that would truncate it)
        try:
            if self._wav is None:
                self._wav = wave.open(self.wav_path, "wb")
                self._wav.setnchannels(num_channels)
                self._wav.setsampwidth(2)
                self._wav.setframerate(sample_rate)
            self._wav.writeframes(audio)
        except (OSError, wave.Error) as e:
            # A recording that cannot be written must not stop the call.
            logger.warning(f"call recording: audio stopped ({e})")
            self._close_wav()
            self.audio = False

    def _on_stopped(self, _buffer) -> None:
        self._close_wav()

    def _close_wav(self) -> None:
        if self._wav is not None:
            try:
                self._wav.close()
            except (OSError, wave.Error):
                pass
            self._wav = None

    # ---------------------------------------------------------------- end

    def close(self) -> None:
        """The call is over. The wav closes with the pipeline (its EndFrame or CancelFrame
        stops the processor, and the tap writes what was still playing); this closes the
        transcript."""
        self._close_txt()
