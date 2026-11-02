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
# A call a restarted brain picks up again (the gateway replays it) is a new bring-up,
# so its rest goes to a file of its own start time; the transcript's header says it was
# resumed.
#
# AUDIO is pipecat's AudioBufferProcessor, below the output transport, where it sees
# the caller's audio on its way down and the bot's as it is played: what the caller
# heard, the typing bed and any cut reply included, merged into one stereo track. It is
# flushed to the wav every few seconds (_FLUSH_SECS) rather than held for the whole
# call, so a long call costs no memory and a crash keeps all but the last seconds. While
# the half-duplex gate (SIP_HALF_DUPLEX) or a session's last line mutes the caller,
# their side is silence: the brain did not take that audio.
#
# The TRANSCRIPT is the transcript ledger's (transcript_ledger.py): the caller's
# finals, and each reply as far as the caller heard it -- a reply they cut is written
# as what they heard, marked cut off. Not the LLM context, which keeps a cut reply's
# unheard words until the corrector trims them, and never sees a line spoken outside it
# (the notice below is written by this module).
#
# The NOTICE (TEAPORT_CALL_RECORDING_NOTICE, on by default): a recorded call starts with
# "This call is being recorded." in the call's language (i18n.py), before the greeting,
# outside the conversation's context. The model is also told the call is recorded, so a
# caller who asks gets a straight answer.
#
# RETENTION: recordings older than TEAPORT_CALL_RECORDING_KEEP_DAYS go, then the oldest
# until the directory is under TEAPORT_CALL_RECORDING_MAX_MB (recordings.prune), checked
# as each call starts.
#
import os
import re
import time
import wave

from loguru import logger

from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

from teaport_brain import i18n, recordings
from teaport_brain.settings import setting

MODE = setting("TEAPORT_CALL_RECORDING")
NOTICE = setting("TEAPORT_CALL_RECORDING_NOTICE")
DIR = setting("TEAPORT_CALL_RECORDING_DIR")
KEEP_DAYS = setting("TEAPORT_CALL_RECORDING_KEEP_DAYS")
MAX_MB = setting("TEAPORT_CALL_RECORDING_MAX_MB")

ENABLED = MODE != "off"

# How much audio the processor holds before it hands it to the wav, per side.
_FLUSH_SECS = 5
# What the processor buffers per track, in bytes, at the highest rate a call runs at
# (16 kHz s16): it is sized in bytes, before the call's own rate is known.
_FLUSH_BYTES = 16000 * 2 * _FLUSH_SECS

# Our files' names: <YYYYmmdd-HHMMSS>-<callid>, the only ones pruning touches.
_NAMES = re.compile(r"\d{8}-\d{6}-")


def notice_line(espeak_language: str | None) -> str:
    """The recording notice, in the language the call's voice speaks."""
    return i18n.for_espeak(espeak_language)._("This call is being recorded.")


def _clock(secs: float) -> str:
    m, s = divmod(max(secs, 0.0), 60)
    return f"{int(m):02d}:{s:04.1f}"


class CallRecording:
    """One call's recording: what build_agent_session puts in its pipeline (processors),
    the ledger it listens to (attach), and what the model is told (prompt_line)."""

    def __init__(self, call_id: str, caller: str | None = None, *, mode: str | None = None,
                 directory: str | None = None, resumed: bool = False):
        mode = mode or MODE
        directory = directory or DIR
        self.mode = mode
        self.audio = mode in ("audio", "both")
        self.transcript = mode in ("transcript", "both")
        safe = "".join(c for c in (call_id or "call") if c.isalnum() or c in "-_")[:64]
        self.stem = recordings.free_stem(
            os.path.join(directory, f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}"))
        self.wav_path = self.stem + ".wav"
        self.txt_path = self.stem + ".txt"
        self._wav = None
        self._txt = None
        self._buffer = None
        try:
            os.makedirs(directory, exist_ok=True)
            if self.transcript:
                self._txt = open(self.txt_path, "w", encoding="utf-8")
        except OSError as e:
            # A recording that cannot be written must not stop the call.
            logger.warning(f"call recording: off for this call -- cannot write to "
                           f"{directory} ({e})")
            self.audio = self.transcript = False
            return
        recordings.prune_soon(directory, KEEP_DAYS, MAX_MB, _NAMES,
                              keep=frozenset({os.path.basename(self.stem).split(".", 1)[0]}))
        if self.transcript:
            started = time.strftime("%Y-%m-%d %H:%M:%S %z")
            self._write(f"Call {call_id} from {caller or 'an unknown caller'}, "
                        f"{'resumed' if resumed else 'started'} {started}. Times are from "
                        "the start of this recording.\n\n")
        if self.audio:
            self._buffer = AudioBufferProcessor(num_channels=2, buffer_size=_FLUSH_BYTES,
                                                auto_start_recording=True)
            # Plain functions, not coroutines: pipecat runs each handler in a task of its
            # own, started in order, and a handler with no await runs whole when its
            # task starts -- so the chunks reach the file in order, and the close after
            # the last of them.
            self._buffer.add_event_handler("on_audio_data", self._on_audio)
            self._buffer.add_event_handler("on_recording_stopped", self._on_stopped)
        logger.info(f"call recording ({mode}): {self.stem}.*")

    # ---------------------------------------------------------------- pipeline

    @property
    def processors(self) -> list:
        """What goes directly below the output transport."""
        return [self._buffer] if self._buffer is not None else []

    @property
    def active(self) -> bool:
        """Something of this call is being kept (False once it could not be written)."""
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

    def attach(self, ledger) -> None:
        """Write the ledger's lines to the transcript as it charts them."""
        if self.transcript:
            ledger.on_utterance = self.on_utterance

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

    def note(self, text: str) -> None:
        """A line the bot said outside the conversation (the recording notice)."""
        self._write(f"[notice] Assistant: {text}\n")

    def on_utterance(self, u) -> None:
        """One ledger line: the caller's final, or a reply as far as it was heard."""
        if u.speaker == "user":
            self._write(f"[{_clock(u.t_start)}] Caller: {u.text}\n")
        elif u.cut_short:
            heard = (u.heard_text or "").strip()
            self._write(f"[{_clock(u.t_start)}] Assistant (cut off): {heard or '...'}\n")
        else:
            self._write(f"[{_clock(u.t_start)}] Assistant: {u.text}\n")

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
        stops the processor); this closes the transcript."""
        self._close_txt()
