# SPDX-License-Identifier: MIT
#
# speech_onset.py — a faster "the caller has started talking" than the VAD's.
#
# The VAD (pipecat's SileroVADAnalyzer, with endpointing.py's thresholds) answers a
# different question -- where an utterance starts and stops, for the STT commit and
# the turn -- and it answers it slowly: confidence >= 0.7 AND volume >= 0.6 for
# start_secs (0.2 s), with the volume integrated over a rolling 400 ms window that
# trails every onset. Replayed offline over the caller audio of a 2026-10-05 test call
# (it reproduces 282 of the 284 live VAD transitions), the SPEAKING edge came a median
# 384 ms after the first chunk of the caller's speech at confidence >= 0.5, and for 5
# of 16 barge-ins it never came at all: this caller's speech under the bot's voice sat
# around confidence 0.75, often dipping under 0.7, and the volume gate lagged it.
#
# What reply_hold.py needs is only "speech has begun", soon
# and with few false alarms. Measured on the same call: N consecutive 32 ms chunks at
# confidence >= 0.6 with no volume gate, N = 4 (128 ms), fired within 250 ms of the
# onset for 13 of the 16 barge-ins (median 144 ms) and on no non-speech in the bot's
# audible windows. The call cannot say much about false alarms, though: the gateway's
# echo canceller left the line near digital silence whenever the caller was quiet.
#
# SpeechOnsetMixin rides the VAD analyzer the session already runs (agent_session's
# _vad_cls composes it like NarrowbandSileroMixin), so it costs no second model: it
# reads each chunk's confidence on its way out of voice_confidence() -- the analyzer's
# executor thread -- and tells its listeners, back on the event loop, when speech
# starts (onset) and when the line has been below the threshold for the VAD's own
# stop_secs (offset). It changes nothing the VAD decides.
#
# The same test serves the STT's commit (stt.py, THE CALLER'S VOICE, issue #140). A
# commit that lands after a VAD stop but over the caller's resumed voice cuts their
# words, and this test sees that voice before the VAD's start does. It uses the onset's
# confidence rather than the VAD's speech test because the VAD's volume gate
# integrates over 400 ms: after a pause as long as the smart-turn ceiling, the first
# ~0.2 s of resumed speech reads as silence to it, and that is exactly the audio the
# ceiling's commit claimed as silence and cut. So the mixin keeps, as VoiceMarks, the
# state its own onset and offset are decided on, at sample positions counted from the
# session's first audio frame. The STT counts the same audio on its side, as the
# frames it passes down to this analyzer, so the two can compare positions. One test
# for both: the reply hold and the commit cannot disagree about whether the caller is
# talking, or about when they paused.
#
import asyncio
import math
from typing import NamedTuple, Optional

from loguru import logger
from pipecat.audio.vad.vad_analyzer import VADState

from teaport_brain.settings import setting

# A chunk counts as speech at this Silero confidence or above (the VAD's is 0.7).
ONSET_CONFIDENCE = setting("TEAPORT_ONSET_CONFIDENCE")
# This much consecutive speech is an onset. 128 ms = 4 of Silero's 32 ms chunks.
ONSET_MIN_MS = setting("TEAPORT_ONSET_MIN_MS")


class VoiceMarks(NamedTuple):
    """The onset test's state, read on the event loop by the STT. Positions are in
    samples from the session's first audio frame (see the module note)."""
    received: int                # handed to the analyzer and run through it
    analyzed: int                # through the last chunk whose confidence is known
    voice_end: Optional[int]     # the end of the last chunk at ONSET_CONFIDENCE
    onset_end: Optional[int]     # the end of the last chunk of a run long enough to be
                                 # an onset (ONSET_MIN_MS): speech, by this test
    in_run: bool                 # a run of voiced chunks is going on, still too short
                                 # to be an onset: it may yet become one
    paused: bool                 # quiet for the VAD's stop_secs since voice_end: the
                                 # point the onset's offset fires at
    stop_at: Optional[int]       # where the VAD's latest stop tripped
    speech_end: Optional[int]    # where that stop's speech ended, by the VAD's own test


class SpeechOnsetMixin:
    """Mixed in ahead of the VAD analyzer: tells `onset_listeners` (async callables
    taking `started: bool`) when the caller's speech starts and stops, on the faster
    test in the module header. Listeners run on the event loop, in the task that
    analyzes the audio (the user aggregator's), so they must not block. The same
    test's state is kept for the STT as voice_marks(), and `voice_heard` is set each
    time more audio has been run through it."""

    def _onset_state(self):
        st = getattr(self, "_onset", None)
        if st is None:
            st = self._onset = {"run": 0, "quiet": 0, "active": False, "events": [],
                                "listeners": [], "received": 0, "analyzed": 0,
                                "voice_end": None, "onset_end": None, "in_run": False,
                                "paused": True, "stop_at": None, "speech_end": None,
                                "vad_speech_end": None, "speaking": False,
                                "heard": None}
        return st

    @property
    def onset_listeners(self) -> list:
        return self._onset_state()["listeners"]

    @property
    def onset_active(self) -> bool:
        return self._onset_state()["active"]

    @property
    def voice_heard(self) -> asyncio.Event:
        """Set on the event loop each time analyze_audio has run more audio through
        the test: what the STT waits on instead of polling the marks. Whoever waits
        clears it first and then reads the marks, so an analysis that lands in
        between is not missed."""
        st = self._onset_state()
        if st["heard"] is None:
            st["heard"] = asyncio.Event()
        return st["heard"]

    def voice_marks(self) -> VoiceMarks:
        """The onset test's state for the STT (see VoiceMarks).

        `analyzed` is read first, and the executor thread writes every other mark of
        a chunk before it advances `analyzed` past that chunk. So a read that races a
        chunk sees marks at least as recent as the position it reports: voice it may
        place after `analyzed`, but never a quiet stretch longer than the real one."""
        st = self._onset_state()
        analyzed = st["analyzed"]
        return VoiceMarks(st["received"], analyzed, st["voice_end"], st["onset_end"],
                          st["in_run"], st["paused"], st["stop_at"], st["speech_end"])

    def voice_confidence(self, buffer) -> float:
        conf = super().voice_confidence(buffer)
        try:
            self._onset_feed(conf, len(buffer) // 2)
        except Exception as e:  # noqa: BLE001 -- never let the side channel break the VAD
            logger.warning(f"speech onset: {e!r}")
        return conf

    def _onset_feed(self, conf: float, samples: int):
        """Executor thread: one chunk's confidence. Everything about the chunk is
        decided first and written before `analyzed` moves past it (voice_marks)."""
        st = self._onset_state()
        if samples <= 0:
            return
        chunk_s = samples / self.sample_rate
        need = max(1, math.ceil(ONSET_MIN_MS / 1000.0 / chunk_s - 1e-9))
        stop = max(1, math.ceil(self.params.stop_secs / chunk_s - 1e-9))
        end = st["analyzed"] + samples
        voiced = conf >= ONSET_CONFIDENCE
        run = st["run"] + 1 if voiced else 0
        quiet = 0 if voiced else st["quiet"] + 1
        if voiced:
            st["voice_end"] = end
            if run >= need:
                st["onset_end"] = end
        st["run"] = run
        st["quiet"] = quiet
        st["in_run"] = 0 < run < need
        st["paused"] = quiet >= stop
        if voiced and not st["active"] and run >= need:
            st["active"] = True
            st["events"].append(True)
        elif st["active"] and st["paused"]:
            st["active"] = False
            st["events"].append(False)
        st["analyzed"] = end

    async def analyze_audio(self, buffer):
        state = await super().analyze_audio(buffer)
        # Back on the loop: the executor call above has returned, so whatever it
        # appended is visible here and nothing appends concurrently.
        st = self._onset_state()
        st["received"] += len(buffer) // 2
        # The VAD's own speech end and stop, read the way its controller reads them:
        # the state settling to QUIET after SPEAKING (STARTING and STOPPING are
        # transient on both edges). An input frame carries less than one Silero
        # chunk, so a call that returns SPEAKING ended on a speaking chunk.
        if state == VADState.SPEAKING:
            st["speaking"] = True
            st["vad_speech_end"] = st["analyzed"]
        elif state == VADState.QUIET and st["speaking"]:
            st["speaking"] = False
            st["speech_end"] = st["vad_speech_end"]
            st["stop_at"] = st["analyzed"]
        if st["heard"] is not None:
            st["heard"].set()
        if st["events"]:
            events, st["events"] = st["events"], []
            for started in events:
                for cb in list(st["listeners"]):
                    try:
                        await cb(started)
                    except Exception as e:  # noqa: BLE001 -- a listener's bug is its own
                        logger.warning(f"speech onset listener {cb!r}: {e!r}")
        return state
