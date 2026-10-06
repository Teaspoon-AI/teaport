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
# What reply_hold.py (and, on SIP, barge_pause.py) needs is only "speech has begun", soon
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
import math

from loguru import logger

from teaport_brain.env import env_num

# A chunk counts as speech at this Silero confidence or above (the VAD's is 0.7).
ONSET_CONFIDENCE = env_num("TEAPORT_ONSET_CONFIDENCE", "0.6", float)
# This much consecutive speech is an onset. 128 ms = 4 of Silero's 32 ms chunks.
ONSET_MIN_MS = env_num("TEAPORT_ONSET_MIN_MS", "128", float)


class SpeechOnsetMixin:
    """Mixed in ahead of the VAD analyzer: tells `onset_listeners` (async callables
    taking `started: bool`) when the caller's speech starts and stops, on the faster
    test in the module header. Listeners run on the event loop, in the task that
    analyzes the audio (the user aggregator's), so they must not block."""

    def _onset_state(self):
        st = getattr(self, "_onset", None)
        if st is None:
            st = self._onset = {"run": 0, "quiet": 0, "active": False, "events": [],
                                "listeners": []}
        return st

    @property
    def onset_listeners(self) -> list:
        return self._onset_state()["listeners"]

    @property
    def onset_active(self) -> bool:
        return self._onset_state()["active"]

    def voice_confidence(self, buffer) -> float:
        conf = super().voice_confidence(buffer)
        try:
            self._onset_feed(conf, len(buffer) / 2 / self.sample_rate)
        except Exception as e:  # noqa: BLE001 -- never let the side channel break the VAD
            logger.warning(f"speech onset: {e!r}")
        return conf

    def _onset_feed(self, conf: float, chunk_s: float):
        """Executor thread: one chunk's confidence."""
        st = self._onset_state()
        if chunk_s <= 0:
            return
        need = max(1, math.ceil(ONSET_MIN_MS / 1000.0 / chunk_s - 1e-9))
        stop = max(1, math.ceil(self.params.stop_secs / chunk_s - 1e-9))
        if conf >= ONSET_CONFIDENCE:
            st["run"] += 1
            st["quiet"] = 0
            if not st["active"] and st["run"] >= need:
                st["active"] = True
                st["events"].append(True)
        else:
            st["run"] = 0
            st["quiet"] += 1
            if st["active"] and st["quiet"] >= stop:
                st["active"] = False
                st["events"].append(False)

    async def analyze_audio(self, buffer):
        state = await super().analyze_audio(buffer)
        # Back on the loop: the executor call above has returned, so whatever it
        # appended is visible here and nothing appends concurrently.
        st = self._onset_state()
        if st["events"]:
            events, st["events"] = st["events"], []
            for started in events:
                for cb in list(st["listeners"]):
                    try:
                        await cb(started)
                    except Exception as e:  # noqa: BLE001 -- a listener's bug is its own
                        logger.warning(f"speech onset listener {cb!r}: {e!r}")
        return state
