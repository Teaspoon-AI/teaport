#
# engine_tts.py — Engine TTS with WORD-LEVEL timestamps (engine text-in).
#
# Why this exists: pipecat otherwise emits ONE TTSTextFrame for the whole reply at
# end-of-synthesis — the ledger can only ESTIMATE the heard boundary from played-audio
# fraction. the engine gives per-word timing; we feed it to pipecat via add_word_timestamps()
# (push_text_frames=False), so the base TTSService schedules a TTSTextFrame PER WORD on the
# playout clock, and the ledger knows EXACTLY which words were heard before a barge-in.
#
# Synthesis runs entirely through the engine's embedded TTS over the text-in
# vLLM-Omni stream (ws://…/v1/audio/speech/stream): the brain sends TEXT and the ENGINE does
# G2P + number normalization + word timing. The brain-side phoneme path and espeak/misaki
# G2P were removed 2026-07 once the engine owned G2P (docs/G2P_ENGINE_MIGRATION.md Stage 2/3)
# — no torch/CUDA/onnxruntime/espeak in this process. ENGINE_TTS_URL sets the engine
# host/port (the stream path is derived), or ENGINE_TTS_STREAM_URL sets the stream URL directly.

import asyncio
import math
import os
import time
from contextlib import aclosing
from typing import AsyncGenerator

import numpy as np
from loguru import logger

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    TTSAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.tts_service import TTSService

from teaport_brain import tts_text as tts_text_lead  # noqa: E402  (shared caption-lead constant)
from teaport_brain.env import env_choice, env_flag, env_num
from teaport_brain.tts_text import split_clauses_ramp

# TEAPORT_TRACE=1 keeps the [WTS] word-timestamp traces (debug aid for the
# caption/heard-ledger pipeline) without spamming normal logs.
_TRACE = env_flag("TEAPORT_TRACE", False)

_SAMPLE_RATE = 24000  # the engine outputs 24 kHz

# Engine text-in stream endpoint + per-request recv timeout. The engine synthesizes a clause
# in ~0.2-0.5s (RTF ~0.23); 20s covers a stuck engine without hanging a reply. The stream URL
# derives from the engine ws base (ENGINE_TTS_URL, box-configured) unless set explicitly.
_ENGINE_WS = os.getenv("ENGINE_TTS_URL", "ws://127.0.0.1:8000/v1/tts").rsplit("/v1/", 1)[0]
_STREAM_URL = os.getenv("ENGINE_TTS_STREAM_URL", _ENGINE_WS + "/v1/audio/speech/stream")
_STREAM_TIMEOUT = env_num("TTS_REMOTE_TIMEOUT", "20", float)
# These two knobs live in brain.env, which installer repairs preserve verbatim — a bare
# int()/float() here turns one typo ("", "off", "2.5") into an import-time ValueError that
# crash-loops the whole brain service, and re-running the installer cannot clear it.
# env.env_num IS that defensive cast, shared with services.py's LLM_MAX_TOKENS so the
# warn-and-fall-back behaviour is defined once.
_env_num = env_num


# The engine caps this endpoint at OMNI_MAX_SESSIONS (2) and holds a slot until it finishes
# synthesizing — including work a barge-in abandoned, measured at ~1.5s on an Orin Nano. Two
# overlapping barge-ins therefore refuse every new connect until they drain. Retry the connect
# across that window: measured 0/3 clauses survive with a single attempt, 3/3 with three.
# >= 1: the retry count is also the ATTEMPT count, so 0 would skip the connect entirely
# and hand the caller a None websocket (AttributeError on .send) instead of a clean failure.
# Setting it to 0/1 is how an operator disables the retry, and both must still connect once.
_STREAM_CONNECT_RETRIES = max(1, _env_num("TTS_CONNECT_RETRIES", "3", int))
_STREAM_CONNECT_BACKOFF = _env_num("TTS_CONNECT_BACKOFF", "0.6", float)

# Caption lead: the transport releases each word's caption frame at its presentation
# timestamp (== when it SENDS that word's audio), but the client buffers and plays audio
# slightly ahead, so an unshifted caption reads BEHIND the voice. Releasing the caption a
# touch early compensates that client buffer, and also puts the caption ahead of the
# barge-in flush point (the transport discards not-yet-released word frames on interruption),
# so less of the just-spoken tail is dropped from the bubble. Tune to the client's buffer;
# too large shows words a beat before they're heard. Applies to the caption only (the pts of
# TTSTextFrames), not the audio. transcript_ledger.py backs this shift OUT of its heard-word
# accounting (same env var — keep the default in sync there) so "which words were heard"
# stays exact despite the UX lead.
_CAPTION_LEAD_SECS = tts_text_lead.CAPTION_LEAD_SECS

# pipecat's audio-context watchdog closes a TTS context after this many seconds without a
# new frame, resetting the word-timestamp baseline MID-REPLY and pushing a premature
# LLMFullResponseEndFrame (splits the assistant context; the ledger ignores that fresh
# frame -- it takes a response's End at the TTS's own sighting and recognises only a
# re-push of that same frame -- but its turn then closes on the next context's start
# rather than on the drain). Its 3s default is fine on GPU (synth ≤ ~1.2s/chunk) but on the
# CPU backends a ramped chunk of 110+ chars synthesizes >3s, tripping it in normal operation.
# Streaming (TTS_STREAM_AUDIO) does not change that: a clause's eager first block queues at
# ~1/3 of its synthesis, but the rest of the sentence still arrives only at its end, so the
# gap is ~2/3 of it -- and with streaming off it is all of it. 15s covers the worst
# cap/hard_max-sized chunk at CPU RTF ~0.6 with margin.
_STOP_FRAME_TIMEOUT_S = env_num("TTS_STOP_FRAME_TIMEOUT_S", "15", float)

# Clause-chunking. The engine streams a sentence's PCM as it synthesizes it, but only for
# the FIRST sentence of a stream session (voxtral_websocket.c omni_worker, eager-prefix
# synthesis: a first block after ~1/3 of the sentence's synthesis time); later sentences
# of a session arrive whole. With "chunk_timestamps" (teagram-engine#77) each audio chunk
# carries the words that start in it. _synth_text forwards every audio.chunk as it lands,
# and its words just ahead of it (issue #14: it used to buffer to audio.done, which
# deferred first audio to the LAST chunk). Each clause is its own session, so every
# clause's opening streams. First audio still scales
# with the first clause's length, so synthesize a SHORT opening clause and the rest in
# larger pieces — later pieces play behind the first, so at RTF<1 their synth time is
# hidden. Measured 2026-10-03 on the box (engine dac50fb, GPU idle): a 27-char clause's
# first block at 45-73 ms (all of it 80-109 ms), a 98-char sentence's at 73 ms (all of it
# 171 ms) — on that GPU engine the ramp buys tens of ms; it earns its keep on the CPU
# backends (RTF ~0.6), where a long first clause costs seconds. Each clause's
# leading/trailing near-silence is trimmed (_SeamTrim) so the per-synth padding doesn't
# stack into an over-long seam: naive concat gives a ~793 ms gap vs a whole sentence's
# natural ~320 ms comma pause; trimming to ~lead+trail lands it near the natural pause, and
# the terminal pitch + register are already continuous across the seam (measured). Per-word
# timestamps are shifted for the leading trim, so the heard-ledger stays exact.
_FIRST_CLAUSE_MAX_CHARS = env_num("TTS_FIRST_CLAUSE_CHARS", "32", int)
# Ramp-up chunking: each chunk may grow up to GROWTH x the previous. GROWTH must stay below
# 1/RTF (~1.67 at the measured CPU RTF 0.6) so a chunk's synth never outruns the previous
# chunk's playout — otherwise playback stalls at the seam even when RTF is healthy. 1.5 leaves
# margin for light load; CAP bounds the largest chunk.
_CLAUSE_GROWTH = env_num("TTS_CLAUSE_GROWTH", "1.5", float)
_CLAUSE_CAP = env_num("TTS_CLAUSE_CAP", "200", int)
# Last-resort word-break: any chunk longer than this (chars) is split mid-sentence so a
# long run-on (e.g. the Tale of Two Cities opening) can't overflow the engine's ~512-token
# utterance limit and crash the synth. Kept well under that limit with margin.
_CLAUSE_HARD_MAX = env_num("TTS_CLAUSE_HARD_MAX", "350", int)
# A sentence longer than this is split at its clause boundaries before synthesis
# (see split_clauses_ramp): first audio scales with the chunk's length, so one long
# sentence is the whole first-audio wait. Live 2026-09-04 (pre-#14 brain, which
# waited for a chunk's LAST block, on a several-times-slower engine build): a 236-char
# sentence began 4.6 s after the model finished it, a ~30 s one 6.6 s, and at a 120
# cap a 112-char one still waited 1.7 s. Sentences up to this length keep their
# prosody untouched. 0 disables the split.
_SENTENCE_SOFT_MAX = env_num("TTS_SENTENCE_SOFT_MAX", "80", int)
_SEAM_KEEP_LEAD = env_num("TTS_SEAM_KEEP_LEAD", "0.05", float)   # s kept before first sound
_SEAM_KEEP_TRAIL = env_num("TTS_SEAM_KEEP_TRAIL", "0.25", float)  # s kept after last sound
# Play each engine audio.chunk as it arrives (issue #14). Off = buffer each sentence to its
# audio.done and play it whole, as before #14, and ask the engine to skip its eager prefix
# (session.config "eager": false, ~10% less synthesis per clause, measured on the box; it
# only buys early audio).
# Turn it off on a CPU-only engine, or wherever synthesis runs slower than playout: there a
# clause's eager first block plays out before the rest of its sentence arrives, leaving a hole
# mid-sentence (PR #81 review; word times follow such a hole, see _note_playout, but the
# listener still hears it). A "TTS stream underrun" line in the journal is that hole. The
# brain cannot see the engine's backend, so the default follows the GPU appliance.
_STREAM_AUDIO = env_flag("TTS_STREAM_AUDIO", True)
# A playout gap shorter than this is timing noise: _note_playout neither shifts word
# times by it nor reports it.
_PLAYOUT_GAP_MS = 20.0
# Per-context audio tallies kept at most (see EngineTTSService._ctx_audio_secs).
_MAX_CTX_TALLIES = 8

# GPU-yield hold: Kokoro synthesis and Voxtral STT share ONE CUDA context in the
# engine, and synthesis starves transcription (measured 2026-07-21: decode
# 28 ms/step quiet -> 105 ms/step under one synth loop, 2-33 s/step in live
# sessions) — exactly when a barge-in needs the user's words transcribed FAST.
# So while VAD says the user is speaking, run_tts holds before submitting the
# NEXT clause, freeing the GPU for STT; the barge either fires (interruption
# cancels this task) or VAD-stop resumes synthesis. The cap bounds the hold so
# sustained non-barge speech/noise can't stall the reply; the clause-ramp's
# synthesized lead over playout absorbs a capped hold without an audible gap.
_USER_SPEECH_HOLD_MAX_S = env_num("TTS_USER_SPEECH_HOLD_MAX_S", "3.0", float)
_HOLD_ON_USER_SPEECH = env_flag("TTS_HOLD_ON_USER_SPEECH", True)


def _parse_lead_s(raw: str):
    """TTS_LEAD_S: seconds (>= 0), or None for auto. A bad value falls back to the
    documented default (auto), not to some fixed lead; inf would hold every clause
    for the wait cap."""
    raw = (raw or "").strip().lower()
    if raw in ("", "auto"):
        return None
    try:
        secs = float(raw)
    except ValueError:
        secs = math.nan
    if not math.isfinite(secs):
        logger.warning(f"TTS_LEAD_S={raw!r} is not auto or a number of seconds; using auto")
        return None
    return max(0.0, secs)


# Synthesis pacing. "greedy" submits each clause as soon as the previous one is
# synthesized, so a reply is on the GPU in one burst right after it starts playing,
# and a barge-in throws away everything synthesized past the cut. "lead" holds the
# next clause until the audio emitted but not yet played (_play_end, see
# _note_playout) drops below TTS_LEAD_S, so a barge-in discards at most about one
# lead of synthesis. "auto" sizes the lead from the next clause:
# _LEAD_AUTO_SYNTH_MULT x its estimated synth time (learned per session), never under
# _LEAD_AUTO_FLOOR_S, so a slow engine gets a longer lead before it starves playout.
# _PACE_MAX_WAIT_S caps one clause's wait. Measured 2026-10-04 (appliance with the
# live LLM, and an Orin NX with a fixed reply; 80 barge-ins each, 2x2 with
# VOX_STT_STREAM_PRIORITY): barge-in latency did not move (onset -> cut ~1.9 s in
# every arm), because synthesis now runs ~30x real time and greedy's burst barely
# overlaps STT; an interrupted reply discarded ~3-5 s of synthesis instead of
# ~15-45 s. Hence off by default: it saves GPU work, not latency, on this hardware.
_PACING = env_choice("TTS_PACING", "greedy", ("lead", "greedy"))
_LEAD_S = _parse_lead_s(os.getenv("TTS_LEAD_S"))
_LEAD_AUTO_FLOOR_S = 1.5
_LEAD_AUTO_SYNTH_MULT = 2.0
_PACE_MAX_WAIT_S = 30.0

# Which clauses ask the engine for its eager prefix (session.config "eager"), which
# only buys early audio, at ~10% more synthesis (measured on the box). With
# TTS_STREAM_AUDIO off no clause does.
#   always   every clause (the brain's behaviour before this knob)
#   first    each reply's first clause
#   low_lead a clause submitted with less audio queued ahead of it than its own
#            estimated synth time: a reply's opening after silence, or after an
#            underrun; not one queued behind audio still playing
#   never    no clause
_EAGER = env_choice("TTS_EAGER", "always", ("always", "first", "low_lead", "never"))

# Starting estimates for the per-session EWMAs behind "auto" lead and "low_lead"
# eager: speech runs ~15 chars/s, and the engine synthesizes at RTF ~0.03-0.04
# (measured 2026-10-04: a 120-char clause, 9.1 s of audio, in 0.30 s on the
# appliance). 0.1 leaves margin for in-call load, so the first clauses err toward a
# long lead / eager on; a slower engine raises the estimate within a few clauses.
_SECS_PER_CHAR_SEED = 0.065
_RTF_SEED = 0.1
_EWMA_ALPHA = 0.3


# "Sound", for the seam trim, is above max(1% of the peak heard so far, this floor). The
# floor (-54 dBFS) keeps a quiet opening block (noise padding, a soft onset) from setting
# the threshold below the padding's noise floor and keeping all of the lead padding (PR #81
# review). It sits under every real threshold: engine sentences peak at 0.25-0.77, so 1% is
# 0.0025-0.0077, and the padding is digital silence (measured 2026-10-03: 8 sentences x 3
# voices; streamed onsets within 0.2 ms of whole-sentence ones).
_SEAM_FLOOR = 0.002


class _EngineError(Exception):
    """The engine's synthesis stream failed: connect, I/O, a malformed message, or an
    error the engine reported. run_tts skips the clause on this and only this."""


class _SeamTrim:
    """Trim one engine sentence's leading/trailing near-silence WHILE it streams, so chunked
    clauses don't stack the engine's per-synth silence padding into an over-long seam.

    feed() takes each audio block as it arrives and returns what can play now: nothing
    before the first sound bar _SEAM_KEEP_LEAD of it, then everything, except that quiet
    after the latest sound plays only up to _SEAM_KEEP_TRAIL; the excess is held until
    more sound follows it (a pause: it plays in full) or the sentence ends (finish():
    trailing padding, dropped). "Sound" is above max(1% of the peak so far, _SEAM_FLOOR).
    That threshold only ever rises, so a sample once quiet stays quiet and each block is
    scanned once. lead_cut is the seconds dropped from the front — shift the sentence's
    word times back by it so they stay aligned with the audio."""

    def __init__(self, sr):
        self._sr = sr
        self._held = np.zeros(0, dtype=np.float32)  # lead padding, or quiet past the trail
        self._peak = 0.0
        self._started = False
        self._quiet = 0  # samples of quiet since the last sound (once started)
        self.lead_cut = 0.0

    @property
    def started(self):
        return self._started

    def feed(self, block):
        mag = np.abs(block)
        if mag.size:
            self._peak = max(self._peak, float(mag.max()))
        loud = np.nonzero(mag > max(0.01 * self._peak, _SEAM_FLOOR))[0]
        if loud.size == 0:
            if not self._started:
                self._held = np.concatenate([self._held, block])
                return block[:0]
            # More quiet: it plays up to the trail; the rest waits for sound or the end.
            room = max(0, int(_SEAM_KEEP_TRAIL * self._sr) - self._quiet)
            self._quiet += block.shape[0]
            self._held = np.concatenate([self._held, block[room:]])
            return block[:room]
        if not self._started:
            # Nothing has been dropped or emitted yet: `held` (all quiet) starts at sample 0.
            h = self._held.shape[0]
            start = max(0, h + int(loud[0]) - int(_SEAM_KEEP_LEAD * self._sr))
            self.lead_cut = start / self._sr
            if start < h:
                self._held = self._held[start:]
            else:
                self._held = block[:0]
                block, loud = block[start - h:], loud - (start - h)
            self._started = True
        last = int(loud[-1]) + 1
        tail = block[last:]
        room = int(_SEAM_KEEP_TRAIL * self._sr)
        out = np.concatenate([self._held, block[:last], tail[:room]])
        self._held, self._quiet = tail[room:], tail.shape[0]
        return out

    def finish(self):
        held, self._held = self._held, np.zeros(0, dtype=np.float32)
        if self._started:
            return held[:0]  # trailing padding past the trail: dropped
        return held  # no sound at all: pass it through untrimmed

# Voice names encode their language in the FIRST letter (af_heart/am_* = American
# English, ef_*/em_* = Spanish, ...). Map that letter to the language the engine's G2P uses
# (which doubles as the engine lang_code letter). This is what makes the engine multilingual:
# pick a voice and the language follows. The brain sends the voice; the engine derives G2P.
_PREFIX_ESPEAK = {
    "a": "en-us",  # American English
    "b": "en-gb",  # British English
    "e": "es",     # Spanish
    "f": "fr-fr",  # French
    "h": "hi",     # Hindi
    "i": "it",     # Italian
    "p": "pt-br",  # Brazilian Portuguese
    "j": "ja",     # Japanese  (espeak g2p — lower quality than misaki)
    "z": "cmn",    # Mandarin  (espeak g2p — lower quality than misaki)
}
_ESPEAK_PREFIX = {v: k for k, v in _PREFIX_ESPEAK.items()}  # "es" -> "e", etc.

# Voice inventory of the engine GGUF (50 of 54 upstream voices — the four
# male-Mandarin zm_* packs are absent from TTS.cpp's converter default list; see
# docs/SINGLE_CUDA_CONTEXT_SCOPE.md). Grouped by the language the prefix letter implies.
# Source of truth for the list_voices / switch_voice tools.
ENGINE_VOICES = {
    "en-us": ["af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica",
              "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
              "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
              "am_michael", "am_onyx", "am_puck", "am_santa"],
    "en-gb": ["bf_alice", "bf_emma", "bf_isabella", "bf_lily",
              "bm_daniel", "bm_fable", "bm_george", "bm_lewis"],
    "es": ["ef_dora", "em_alex", "em_santa"],
    "fr-fr": ["ff_siwis"],
    "hi": ["hf_alpha", "hf_beta", "hm_omega", "hm_psi"],
    "it": ["if_sara", "im_nicola"],
    "ja": ["jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo"],
    "pt-br": ["pf_dora", "pm_alex", "pm_santa"],
    "cmn": ["zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi"],
}

LANG_NAMES = {
    "en-us": "English (US)", "en-gb": "English (UK)", "es": "Spanish",
    "fr-fr": "French", "hi": "Hindi", "it": "Italian", "ja": "Japanese",
    "pt-br": "Portuguese", "cmn": "Chinese (Mandarin)",
}


def _resolve_lang(language, voice):
    """Resolve (engine_lang_code_letter, espeak_lang) for a voice / language request.

    `language` may be an engine lang_code letter ("a"), an espeak/BCP-47-ish code
    ("es", "fr-fr", "pt-BR"), or None — in which case the language is inferred from the
    voice's first letter. An explicit `language` is what OpenClaw drives for multilingual.
    """
    if language:
        code = language.strip().lower()
        if len(code) == 1 and code in _PREFIX_ESPEAK:
            return code, _PREFIX_ESPEAK[code]
        letter = _ESPEAK_PREFIX.get(code) or _ESPEAK_PREFIX.get(code.split("-")[0]) \
            or (voice or "a")[0]
        return letter, code
    letter = (voice or "a")[0]
    return letter, _PREFIX_ESPEAK.get(letter, "en-us")


def _require_context_end_hook():
    """EngineTTSService does its per-context end bookkeeping (where a context's audio
    ends, dropping its tally) in an override of pipecat's PRIVATE
    TTSService._maybe_reset_word_timestamps(context_id), called from
    _handle_audio_context once a context's queued audio has all been handed downstream
    (synthesis done, its end frame through; not when that audio finishes playing, which
    is why _prev_audio_end_ns exists). If a pipecat bump renames,
    reshapes or stops calling it, the override would silently never run: queued replies
    anchored at the previous reply's last word again (issue #19) and tallies never
    dropped. So, like endpointing.keep_barge_in_reachable, fail loudly at session build
    instead."""
    import inspect
    hook = getattr(TTSService, "_maybe_reset_word_timestamps", None)
    if hook is None or "context_id" not in inspect.signature(hook).parameters:
        raise AttributeError("pipecat TTSService._maybe_reset_word_timestamps(context_id) "
                             "is gone: EngineTTSService's per-context reset needs a new hook")
    try:
        source = inspect.getsource(TTSService._handle_audio_context)
    except (OSError, TypeError):
        # An install without .py sources (a stripped package): the call check can't
        # run, and failing every session over that would be worse than skipping it.
        logger.warning("pipecat source unavailable: cannot confirm _handle_audio_context "
                       "still calls _maybe_reset_word_timestamps")
        return
    if "_maybe_reset_word_timestamps(" not in source:
        raise AttributeError("pipecat no longer calls _maybe_reset_word_timestamps from "
                             "_handle_audio_context: EngineTTSService's per-context reset "
                             "needs a new hook")


class EngineTTSService(TTSService):
    """Pipecat TTS service for the engine TTS (text-in) with per-word timestamps."""

    def __init__(self, *, voice: str = "af_heart", language: str = None,
                 lang_code: str = None, speed: float = 1.0, **kwargs):
        _require_context_end_hook()
        self._voice = voice
        self._speed = speed
        # Cross-run_tts word-time base, PER CONTEXT. pipecat calls run_tts once PER
        # SENTENCE but keeps ONE word-timestamp baseline for the whole reply, so a
        # per-call offset that restarts at 0 makes every sentence's words collide on the
        # reply's opening ("To die, to sleep" landing right after "To be"). This carries
        # the base across calls: seconds of audio each context has emitted so far.
        # Keyed by context, not one running total, because the base resets a context's
        # baseline once that context's audio has all been handed downstream, and by then
        # the reply queued behind it may already be synthesizing: a shared total zeroed
        # there put that reply's words a whole reply late (PR #81 review: 'Bravo' at
        # 2.3 s instead of 0.3 s). A context's entry goes at that same point
        # (_maybe_reset_word_timestamps), all of them on a barge-in.
        self._ctx_audio_secs: dict[str, float] = {}
        # When the audio emitted so far finishes playing (time.monotonic(); 0 = idle
        # since the last barge-in), for _note_playout.
        self._play_end = 0.0
        # Where the previous context's audio ENDS on the playout clock (its word
        # baseline + all the audio it emitted), so a reply queued behind it is
        # scheduled from there. pipecat anchors a new context at the previous one's
        # last WORD pts (tts_service.start_word_timestamps: "continuity across
        # overlapping audio contexts"), which is that word's start, shifted early by
        # the caption lead -- 0.8-1.3 s before the audio actually starts (live
        # 2026-09-04: a queued reply's captions ran 1.3 s ahead of its voice, the
        # ledger over-credited one heard word, issue #19). Zeroed on a barge-in: the
        # flushed audio ends now, not where it would have. See start_word_timestamps.
        self._prev_audio_end_ns = 0
        self._interrupted = False
        # Set = user silent (synthesize freely); cleared = user speaking (hold the
        # next clause so the GPU serves STT — see _USER_SPEECH_HOLD_MAX_S). Toggled
        # from process_frame by the VAD frames, which are SystemFrames handled on
        # the input task, so they land while run_tts occupies the process task.
        self._user_quiet = asyncio.Event()
        self._user_quiet.set()
        self._speech_hold_max_s = _USER_SPEECH_HOLD_MAX_S
        self._hold_on_user_speech = _HOLD_ON_USER_SPEECH
        self._pacing, self._lead_s, self._eager = _PACING, _LEAD_S, _EAGER
        # Per-session estimates of a clause's audio length and synth time (see
        # _est_synth_secs), learned from every clause this session synthesizes.
        self._secs_per_char = _SECS_PER_CHAR_SEED
        self._rtf = _RTF_SEED
        # Resolve synthesis language from the explicit request (OpenClaw-driven) or the
        # voice's language family. Populating TTSSettings here also satisfies pipecat's
        # validate_complete() — without it the service logs a NOT_GIVEN warning each start.
        self._lang_code, self._espeak_lang = _resolve_lang(language or lang_code, voice)
        from pipecat.services.settings import TTSSettings
        super().__init__(
            sample_rate=_SAMPLE_RATE, push_text_frames=False,
            # Create + arm the per-reply audio context at reply START (pipecat 1.5.0).
            # start_word_timestamps() only re-arms the word-timestamp baseline when the
            # context exists as audio begins draining; with the default (False), pipecat
            # instead lazily recreates the context via a _turn_context_id fallback that is
            # stale right after a barge-in — so the FOLLOWING reply's words buffer un-armed
            # and get force-completed (dumped unpaced) at reply end, and its live word
            # captions never appear. Creating the context up front makes every reply pace,
            # including the one after an interruption. (push_text_frames stays False: it is
            # what gives playout-paced per-word TTSTextFrames; True would emit one unpaced
            # lump at synthesis end.)
            push_start_frame=True,
            # Push a TTSStoppedFrame once a context's audio is fully enqueued
            # (on_turn_context_completed, pipecat >= 1.7.0). The output transport ends
            # bot-speaking on that frame, which it queues behind the last chunk; with
            # no stop frame it falls back to its BOT_VAD_STOP_FALLBACK_SECS (3 s) idle
            # timeout, so every reply used to be followed by 3 s of "bot speaking"
            # nobody could hear (journal 2026-09-04: BotStoppedSpeaking = audio
            # end + 2.9..3.0 s on 8/8 replies; `based on TTSStoppedFrame` never once).
            # That tail is what the assistant aggregator waits out before running
            # the post-tool-call completion (3 s of dead air after every tool
            # filler), what keeps the 2-word barge-in guard armed past the audio,
            # and what the follow-up gate reads as "still talking".
            # stop_frame_timeout_s below remains the sweep
            # for a context that never yielded audio (nothing synthesizable): pipecat
            # appends no stop frame for it, so only the timeout reclaims it, and the
            # transport ignores a stop frame that follows no audio.
            push_stop_frames=True,
            stop_frame_timeout_s=_STOP_FRAME_TIMEOUT_S,
            # Do NOT let a run of silent contexts write this service off. pipecat
            # 1.8.0 added a counter (default 3) that reports a PERMANENT error on the
            # third consecutive context that completes with no audio: is_usable goes
            # False, _synthesize_text then skips run_tts for good ("service is no
            # longer usable, not speaking"), and make_tts builds a fresh service only
            # per SESSION — so the bot would be mute for the REST OF THE CALL.
            #
            # That counter is aimed at a provider that accepts requests and stays
            # silent (an unknown voice ID), which is a config error and really is
            # permanent. Ours are not: BOTH zero-audio paths below — "nothing
            # synthesizable" and "every clause failed" — are per-utterance and
            # self-healing, and the one that matters (CUDA OOM / an engine restart)
            # clears on its own within seconds. Under 1.7.0 the identical outage
            # recovered on the next successful clause; 0 keeps that.
            #
            # 0 does not hide anything: pipecat still reports every silent context as a
            # (non-permanent) ErrorFrame, and both paths below already say so
            # themselves. What it opts out of is the VERDICT, not the reporting — and
            # that verdict now ends the session (agent_session's
            # _end_session_when_unusable), which is why a recoverable outage must not
            # reach it.
            max_consecutive_zero_audio_contexts=0,
            settings=TTSSettings(model="engine-tts", voice=voice,
                                 language=self._espeak_lang),
            **kwargs,
        )
        if self._speed != 1.0:
            logger.warning(f"engine TTS ignores speed={self._speed} (engine synthesizes at 1.0)")
        logger.info(f"EngineTTSService ready (engine text-in, voice={self._voice}, "
                    f"lang={self._espeak_lang}, 24kHz)")

    def can_generate_metrics(self) -> bool:
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        # VAD speech state rides SystemFrames broadcast by the user aggregator;
        # track it here so run_tts (busy on the process task) can yield the GPU.
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._user_quiet.clear()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_quiet.set()
        await super().process_frame(frame, direction)

    async def _hold_for_user_speech(self):
        """Between clauses: wait out user speech (capped) so STT gets the GPU."""
        if self._user_quiet.is_set():
            return
        held = time.monotonic()
        logger.debug(f"{self}: holding synthesis — user speaking (GPU → STT)")
        try:
            await asyncio.wait_for(self._user_quiet.wait(),
                                   timeout=self._speech_hold_max_s)
        except asyncio.TimeoutError:
            logger.debug(f"{self}: speech-hold cap reached — resuming synthesis")
        logger.debug(f"{self}: synthesis resumed after "
                     f"{time.monotonic() - held:.2f}s hold")

    def _lead_secs(self) -> float:
        """Audio emitted that has not played yet, across contexts (_note_playout's model)."""
        return max(0.0, self._play_end - time.monotonic()) if self._play_end else 0.0

    def _est_synth_secs(self, clause: str) -> float:
        return len(clause) * self._secs_per_char * self._rtf

    def _lead_target(self, clause: str) -> float:
        if self._lead_s is not None:
            return self._lead_s
        return max(_LEAD_AUTO_FLOOR_S, _LEAD_AUTO_SYNTH_MULT * self._est_synth_secs(clause))

    def _want_eager(self, clause: str, context_id: str) -> bool:
        if not _STREAM_AUDIO or self._eager == "never":
            return False
        if self._eager == "always":
            return True
        if self._eager == "first":
            return self._ctx_audio_secs.get(context_id, 0.0) == 0.0
        return self._lead_secs() < self._est_synth_secs(clause)

    async def _pace(self, clause: str, context_id: str) -> float:
        """"lead" pacing: wait until at most the lead target is queued ahead of
        playout (at most: a TTS_LEAD_S of 0 means "once playout has caught up", and
        with "less than" it could never be met). Returns the seconds waited. The
        reply's audio context sees no frame while we wait, so refresh it at least
        twice per stop_frame_timeout_s: its watchdog would otherwise close it mid-wait
        and end the reply early."""
        if self._pacing != "lead":
            return 0.0
        target = self._lead_target(clause)
        step = min(1.0, self._stop_frame_timeout_s / 2)
        t0 = time.monotonic()
        while True:
            lead = self._lead_secs()
            if lead <= target:
                break
            if time.monotonic() - t0 >= _PACE_MAX_WAIT_S:
                logger.warning(f"{self}: pacing wait cap ({_PACE_MAX_WAIT_S:.0f}s) reached "
                               f"with {lead:.1f}s queued — submitting anyway")
                break
            self._refresh_audio_context(context_id)
            await asyncio.sleep(min(lead - target + 0.01, step))
        return time.monotonic() - t0

    def _learn(self, clause: str, audio_secs: float, synth_secs: float):
        """Fold one clause's measured audio length and synth time into the estimates.
        A clause under 0.3 s of audio is too short to say anything about either."""
        if audio_secs < 0.3 or not clause:
            return
        a = _EWMA_ALPHA
        self._secs_per_char += a * (audio_secs / len(clause) - self._secs_per_char)
        self._rtf += a * (synth_secs / audio_secs - self._rtf)

    @property
    def espeak_language(self) -> str:
        """The language the current voice implies ('en-us', 'es', ...). Public accessor
        so callers don't reach into _espeak_lang."""
        return self._espeak_lang

    def set_voice(self, voice: str) -> dict:
        """Switch the speaking voice — and the language its prefix implies — mid-session.
        The engine holds every voice pack and takes the voice on each synthesize request,
        so this is pure brain-side state; the engine's G2P language follows the voice
        exactly like session-start selection."""
        self._lang_code, self._espeak_lang = _resolve_lang(None, voice)
        self._voice = voice
        logger.info(f"TTS voice switched: {voice} (lang={self._espeak_lang})")
        return {"ok": True, "voice": voice, "language": self._espeak_lang,
                "language_name": LANG_NAMES.get(self._espeak_lang,
                                                self._espeak_lang)}

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: engine TTS [{text}]")
        # Synthesize clause-by-clause so the (short) opening clause is the first-audio gate,
        # not the whole reply; later clauses synthesize while the first plays. Each clause's
        # seam silence is trimmed (see _SeamTrim) so the joins sound like natural
        # comma pauses, and per-word times are offset by the emitted audio for an exact ledger.
        # No `or [text]` fallback. split_clauses_ramp drops chunks with nothing
        # synthesizable, and reinstating the raw text when it dropped everything handed
        # the engine exactly the punctuation-only junk it had just removed, earning a
        # 500 per clause and 0.0s of "audio". The fallback existed only because the old
        # ASCII-only test dropped every Japanese/Mandarin/Hindi reply; [^\W_] fixed that
        # at the source, so an empty list here now genuinely means "nothing to speak".
        clauses = split_clauses_ramp(text, first_max=_FIRST_CLAUSE_MAX_CHARS,
                                     growth=_CLAUSE_GROWTH, cap=_CLAUSE_CAP,
                                     hard_max=_CLAUSE_HARD_MAX,
                                     soft_max=_SENTENCE_SOFT_MAX)
        if not clauses:
            # ErrorFrame, not a bare return. Returning here yielded neither audio nor a
            # signal, and by this point _push_tts_frames has already created the audio
            # context and pushed TTSStartedFrame — so tts_process_generator saw no
            # TTSAudioRawFrame, left _is_yielding_frames_synchronously False, and
            # on_turn_context_completed skipped closing the context. It was then only
            # reclaimed by the TTS_STOP_FRAME_TIMEOUT_S sweep: fifteen seconds of dead air
            # per occurrence, with the silent-turn watchdog firing in the middle of it and
            # nothing upstream told anything was wrong.
            logger.warning(f"{self}: nothing synthesizable in {text[:60]!r} — no audio")
            yield ErrorFrame(error=f"tts: nothing synthesizable in {text[:60]!r}")
            return
        await self.start_tts_usage_metrics(text)
        # NB: word times are based on self._ctx_audio_secs (carries across run_tts calls),
        # NOT a local per-call offset — see __init__ / reset_word_timestamps.
        emitted_audio = False
        emitted_secs = 0.0
        failed_clauses = 0
        paced_secs = synth_secs = 0.0
        # No interrupted-context bookkeeping here: a barge-in cancels the process
        # task this generator runs on (pipecat 1.5.0 InterruptionFrame handling),
        # so an in-flight run_tts dies at its next await — measured 255 live
        # interruptions with zero survivors before the old guard was removed.
        # That includes a pacing or speech-hold wait.
        for clause in clauses:
            paced_secs += await self._pace(clause, context_id)
            # User speaking → hold this clause so the GPU serves STT (a maybe-barge
            # needs its words transcribed NOW); resume on VAD-stop or the cap. After
            # the pacing wait, not before it: the user may start talking during that
            # wait, and that is when the clause must not go to the GPU.
            if self._hold_on_user_speech:
                await self._hold_for_user_speech()
            eager = self._want_eager(clause, context_id)
            clause_secs, t0 = 0.0, time.monotonic()
            try:
                # aclosing: any early exit closes the clause's stream now, not at GC. That
                # releases the brain's socket; the engine still finishes the sentence it
                # was synthesizing (~1.5 s) before its session slot frees, which is what
                # _connect_stream's retry rides out.
                async with aclosing(self._speak_clause(clause, context_id, eager)) as frames:
                    async for frame in frames:
                        yield frame
                        emitted_audio = True
                        clause_secs += frame.num_frames / self.sample_rate
                emitted_secs += clause_secs
                wall = time.monotonic() - t0
                synth_secs += wall
                self._learn(clause, clause_secs, wall)
                logger.debug(f"{self}: clause {len(clause)} chars → {clause_secs:.2f}s audio "
                             f"in {wall:.2f}s (eager={eager}, est rtf {self._rtf:.3f})")
            except _EngineError as e:
                emitted_secs += clause_secs
                # Skip the rest of this clause but keep going — one bad chunk shouldn't
                # abort the whole reply mid-sentence and leave the user hanging ("why did
                # you stop?"). hard_max should prevent the over-length case upstream.
                # (If EVERY clause fails we surface an ErrorFrame below — a
                # persistently broken engine must not degrade to silent dead air.)
                # Only the engine stream's own failures land here: a brain/pipecat bug
                # raised while placing words or yielding frames propagates, as it did
                # before #14, instead of being logged as the engine's.
                cause = f" ({e.__cause__!r})" if e.__cause__ else ""
                logger.error(f"engine synth error on clause (skipping): {e}{cause} "
                             f"clause={clause[:60]!r}")
                failed_clauses += 1
                continue
        # Playout forensics: which utterance emitted how much audio, into which
        # context, and when relative to barge-ins (the stale-speech bug class).
        logger.debug(f"{self}: run_tts done — {emitted_secs:.1f}s audio, "
                     f"{synth_secs:.2f}s synth, {paced_secs:.2f}s paced "
                     f"ctx={str(context_id)[:8]} [{text[:36]}…]")
        if failed_clauses and not emitted_audio:
            # EVERY clause failed (CUDA OOM, unsupported language, corrupted engine):
            # dead air with no signal is the worst outcome — surface it so the
            # pipeline/user knows the voice is down.
            yield ErrorFrame(error=f"tts: all {failed_clauses} clause(s) failed to synthesize")

    async def _speak_clause(self, clause: str, context_id: str, eager: bool | None = None):
        """One clause's audio frames, each yielded as the engine streams it (seam-trimmed),
        each preceded by the word timestamps of the words that start in it.

        Words are placed ahead of their audio, so the caption lead and the barge-in
        flush work as they did when whole sentences were buffered. A word's time is
        where its sentence's audio began in this context (`base`) plus its own offset,
        less the lead trim. `base` is taken when the words are placed: the context's
        tally less what this sentence has emitted so far, so it moves with any playout
        gap _note_playout adds to the tally. An engine without per-chunk lists (pre
        teagram-engine#77) gives the words only at the sentence's end; they are placed
        then, after its audio, as before #77.

        Known limit: if pipecat's stop-frame timeout (TTS_STOP_FRAME_TIMEOUT_S with no
        frame) ends the context mid-sentence, pipecat force-completes the sentence's
        remaining text as one caption and drops the next chunk's words, so that
        sentence loses its word timing. Main does the same for a stall between clauses;
        it takes an engine stalled for longer than the timeout (PR #81 review)."""
        def emitted():
            return self._ctx_audio_secs.get(context_id, 0.0)

        trim, sent, words, chunked = _SeamTrim(self.sample_rate), 0.0, [], False
        async with aclosing(self._synth_text(clause, eager=eager)) as stream:
            async for event in stream:
                if event[0] == "audio":
                    audio, chunk_words = trim.feed(event[1]), event[2]
                    if chunk_words is not None:
                        chunked = True
                        words += chunk_words
                else:  # "end": the sentence is complete
                    audio = trim.finish()
                    if not chunked:
                        words += event[1]
                if audio.shape[0]:
                    self._note_playout(context_id, audio.shape[0] / self.sample_rate,
                                       mid_sentence=sent > 0)
                # Lead padding carries no words; wait until the trim has fixed lead_cut.
                if words and (trim.started or event[0] == "end"):
                    await self._place_words(words, max(0.0, emitted() - sent),
                                            trim.lead_cut, context_id)
                    words = []
                if audio.shape[0]:
                    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
                    yield TTSAudioRawFrame(pcm, self.sample_rate, 1, context_id=context_id)
                    secs = audio.shape[0] / self.sample_rate
                    sent += secs
                    # Re-inserted, so the dict's order is "least recently updated first".
                    total = emitted() + secs
                    self._ctx_audio_secs.pop(context_id, None)
                    self._ctx_audio_secs[context_id] = total
                    # A tally is dropped when its context ends, or on a barge-in; one
                    # written after either (an uninterruptible frame's run_tts keeps
                    # going) would stay. Keep only the most recently updated few.
                    while len(self._ctx_audio_secs) > _MAX_CTX_TALLIES:
                        self._ctx_audio_secs.pop(next(iter(self._ctx_audio_secs)))
                if event[0] == "end":
                    trim, sent, words, chunked = _SeamTrim(self.sample_rate), 0.0, [], False

    async def _place_words(self, words, base, lead_cut, context_id):
        """Hand `words` (sentence-relative [(word, start_sec)]) to the base at
        base + start - lead_cut, released _CAPTION_LEAD_SECS early: that compensates the
        client's audio buffer (the "caption lags the voice" feel) and keeps words ahead of
        the barge-in flush (the dropped spoken tail)."""
        placed = [(w, max(0.0, base + max(0.0, s - lead_cut) - _CAPTION_LEAD_SECS))
                  for (w, s) in words]
        if _TRACE:
            logger.info(f"[WTS] off={base:.2f} n={len(words)} span=[{placed[0][1]:.2f},"
                        f"{placed[-1][1]:.2f}] first={words[0][0]!r} last={words[-1][0]!r}")
        await self.add_word_timestamps(placed, context_id)

    def _note_playout(self, context_id: str, secs: float, mid_sentence: bool):
        """Track playout across everything this service emits, and account for its gaps.

        The transport plays frames back to back in real time, so audio emitted after the
        previous audio has run out starts late by the difference. Word times count only
        emitted audio, so without this, every word after such a gap (a slow LLM between
        sentences, a speech hold, synthesis outrunning playout mid-sentence) was placed
        early by it — captions ahead of the voice, and the ledger crediting words not
        yet heard (PR #81 review). The gap joins the context's tally, which shifts all
        later words with the audio. Not on a context's first audio: its baseline is set
        when that audio starts, so there is nothing to shift. A gap inside a sentence is
        the hole TTS_STREAM_AUDIO=0 avoids, so it is also logged.

        Assumes contexts don't interleave: the model is one playout stream in emission
        order, while the transport plays contexts in queue order. A TTSSpeakFrame spoken
        while a reply is still being synthesized counts as playing at once although it
        plays after that reply, which hides the reply's next gap (PR #81 review). Today's
        call sites push them between replies."""
        now = time.monotonic()
        gap = now - self._play_end if self._play_end else 0.0
        self._play_end = max(self._play_end, now) + secs
        if gap * 1000.0 <= _PLAYOUT_GAP_MS or context_id not in self._ctx_audio_secs:
            return
        self._ctx_audio_secs[context_id] += gap
        if mid_sentence:
            logger.info(f"{self}: TTS stream underrun — {gap * 1000.0:.0f} ms of silence "
                        f"inside a sentence (ctx={str(context_id)[:8]}; "
                        f"TTS_STREAM_AUDIO=0 avoids this)")
        else:
            logger.debug(f"{self}: playout gap {gap * 1000.0:.0f} ms between sentences "
                         f"(ctx={str(context_id)[:8]})")

    async def start_word_timestamps(self):
        # The base sets the baseline to max(now, _word_last_pts) -- "the last emitted
        # timestamp if it's ahead of current time, to maintain continuity across
        # overlapping audio contexts" -- and flushes the words it cached before the
        # first audio frame with that baseline, all inside this call. When the
        # previous context's audio is still playing, this context plays when THAT
        # audio ends, later than its last word's start: hand the base that instant
        # through the hook it already reads, BEFORE it runs.
        if self._initial_word_timestamp == -1 and self._word_last_pts < self._prev_audio_end_ns:
            if _TRACE:
                logger.info(f"[WTS] baseline floor {self._word_last_pts / 1e9:.2f} -> "
                            f"{self._prev_audio_end_ns / 1e9:.2f} (queued behind audio)")
            self._word_last_pts = self._prev_audio_end_ns
        await super().start_word_timestamps()

    async def reset_word_timestamps(self):
        # The base zeroes its per-reply word-timestamp baseline here. A context's normal
        # end is handled first in _maybe_reset_word_timestamps, which knows the context;
        # this override only handles the barge-in: the flushed audio ends now, not where
        # it would have, so nothing is carried, and every queued context's tally goes.
        if self._interrupted:
            self._prev_audio_end_ns = 0
            self._interrupted = False
            self._ctx_audio_secs.clear()
        await super().reset_word_timestamps()

    async def _maybe_reset_word_timestamps(self, context_id: str):
        # pipecat's per-context end hook: `context_id`'s audio has all been handed
        # downstream (it may still be playing) and the base is about to reset the
        # baseline. Remember where that context's audio ends
        # (baseline + everything it emitted) for a reply queued behind it, and drop its
        # tally — so a reply that reuses its id starts at 0 — and no other context's:
        # one queued behind may already be placing words against its own. (With no
        # baseline set the reset is a no-op one -- pipecat resets more than once around
        # a context end -- and must not forget a real end.)
        base = self._initial_word_timestamp
        secs = self._ctx_audio_secs.pop(context_id, 0.0)
        if base != -1 and not self._interrupted:
            self._prev_audio_end_ns = base + int(secs * 1e9)
        await super()._maybe_reset_word_timestamps(context_id)

    async def _handle_interruption(self, frame, direction):
        # Forensics for the stale-speech bug class: prove the barge-in actually
        # reached the TTS (fresh context dicts, word-timestamp reset) and when.
        logger.debug(f"{self}: interruption reached TTS "
                     f"(turn_ctx={str(self._turn_context_id)[:8]})")
        # How much emitted audio the barge-in threw away — the synthesis pacing exists
        # to avoid. Read before _play_end is cleared below.
        if self._lead_secs() > 0.0:
            logger.info(f"{self}: barge-in discarded {self._lead_secs():.1f}s of "
                        f"synthesized audio (pacing={self._pacing})")
        self._interrupted = True  # the base resets word timestamps below: no carry
        self._play_end = 0.0      # the flushed audio is not going to play
        await super()._handle_interruption(frame, direction)
        self._prev_audio_end_ns = 0

    async def _connect_stream(self):
        """Open the one-shot synthesis stream, waiting out a full engine session pool.

        Only the CONNECT is retried, and only a fast refusal. Once the stream is up, an
        engine-side error is a real failure (unsupported language, OOM) and replaying it
        would just burn a slot; a recv timeout likewise means the engine accepted and then
        hung, which more attempts cannot fix — and the same goes for the open_timeout: a
        wedged engine that accepts TCP but never finishes the handshake would otherwise
        cost 3 x 5s of dead air PER CLAUSE before the caller sees the failure, versus the
        pool-full case this retry exists for, where the refusal is immediate.
        CancelledError is a BaseException, so a barge-in still cancels us here instead
        of looping — the caller's clause is meant to die when the user interrupts.
        """
        import websockets
        for attempt in range(_STREAM_CONNECT_RETRIES):
            try:
                return await websockets.connect(_STREAM_URL, max_size=None, open_timeout=5)
            except (OSError, websockets.exceptions.WebSocketException) as e:
                # TimeoutError is an OSError: it is the open_timeout expiring, not a refusal.
                if isinstance(e, TimeoutError) or attempt + 1 >= _STREAM_CONNECT_RETRIES:
                    raise
                logger.debug(f"{self}: TTS connect refused ({e!r}); engine session pool "
                             f"likely full — retry {attempt + 1}/{_STREAM_CONNECT_RETRIES - 1}")
                await asyncio.sleep(_STREAM_CONNECT_BACKOFF * (attempt + 1))

    async def _synth_text(self, text: str, eager: bool | None = None):
        """Synthesize via the engine's vLLM-Omni text-in stream (/v1/audio/speech/stream).
        The ENGINE does G2P + number normalization + word timing — the brain sends raw text,
        no phonemize. One WS per call (the stream is one-shot per connection:
        config -> input.text -> input.done -> results -> close).

        Yields, per engine sentence (the engine splits input.text on . ! ? and newline):
        ("audio", float32 [-1,1], words) for each audio.chunk AS IT ARRIVES, then
        ("end", words) at its audio.done. Words are [(word, start_sec)], sentence-relative.
        We ask for "chunk_timestamps" (teagram-engine#77), so each audio chunk carries the
        words that START in it ([] when none does); the "end" list is the engine's
        trailing full list. An engine without #77, or a sentence the engine could not
        align, gives None on every chunk, and then only the "end" list (or nothing) has
        the words. With TTS_STREAM_AUDIO off a sentence's audio comes as one block, with
        all its chunks' words, just before its "end". A sentence the engine leaves without
        an audio.done (a new audio.start, or session.done) is closed there with no
        trailing list. Number expansions fold their word_timestamps back to the source
        token engine-side, so captions show "2026", not "twenty twenty six".

        Every failure of the engine stream itself (connect, I/O, timeout, malformed engine
        messages or audio, an engine-reported error) raises _EngineError; a bug in this
        code surfaces as itself. Malformed word timestamps only lose the words (logged)."""
        import base64
        import binascii
        import json
        import websockets
        try:
            ws = await self._connect_stream()
        except (OSError, websockets.exceptions.WebSocketException) as e:
            # refused / timed out (TimeoutError is an OSError) / handshake failed
            raise _EngineError("connect failed") from e
        pending, chunk_words, carry, heard, trailing = [], None, b"", False, []

        def close_sentence():
            # What ends a sentence: its buffered audio (streaming off), then its words.
            nonlocal pending, chunk_words, carry, heard, trailing
            events = [("audio", np.concatenate(pending), chunk_words)] if pending else []
            if heard:  # a sentence that produced no audio has no words to place
                events.append(("end", trailing))
            pending, chunk_words, carry, heard, trailing = [], None, b"", False, []
            return events

        # Only the stream's own I/O and the engine's bytes become _EngineError; the rest of
        # this body is the brain's code, and a bug in it must surface as itself.
        async def send(obj):
            msg = json.dumps(obj, ensure_ascii=False)
            try:
                await ws.send(msg)
            except Exception as e:  # noqa: BLE001 — connection closed / socket error
                raise _EngineError("send failed") from e

        async def recv():
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=_STREAM_TIMEOUT)
            except Exception as e:  # noqa: BLE001 — timeout / connection closed
                raise _EngineError("recv failed") from e
            if isinstance(msg, (bytes, bytearray)):
                return None  # word_timestamps=True => audio rides base64 JSON, not binary
            try:
                data = json.loads(msg)
            except ValueError as e:
                raise _EngineError("malformed message") from e
            if not isinstance(data, dict):
                raise _EngineError(f"malformed message: {msg[:80]!r}")
            return data

        def parse_words(ts):
            # [{word,start_ms,end_ms}] -> [(word, start_sec)]; null stays None. Bare word
            # tokens (no trailing space): pipecat 1.5.0's word tracker matches them against
            # the LLM's leading-space tokens (a trailing space mismatches and the word is
            # discarded); captions.py rejoins with spaces. Malformed timing is treated as
            # null: the audio still plays, without its words (the ledger then estimates).
            if ts is None:
                return None
            try:
                if isinstance(ts, list) and all(isinstance(w, dict) for w in ts):
                    return [(str(w.get("word", "")), float(w.get("start_ms", 0)) / 1000.0)
                            for w in ts]
            except (TypeError, ValueError):
                pass
            logger.warning(f"{self}: malformed engine word timestamps, dropped: "
                           f"{str(ts)[:80]!r}")
            return None

        try:
            await send({"type": "session.config", "voice": self._voice,
                        "response_format": "pcm", "stream_audio": True,
                        "word_timestamps": True, "chunk_timestamps": True,
                        "eager": _STREAM_AUDIO if eager is None else eager})
            await send({"type": "input.text", "text": text})
            await send({"type": "input.done"})
            while True:
                data = await recv()
                if data is None:
                    continue
                mtype = data.get("type")
                if mtype == "audio.start":
                    for event in close_sentence():
                        yield event
                elif mtype == "audio.chunk":
                    b64 = data.get("audio_b64") or ""
                    if not isinstance(b64, str):
                        raise _EngineError(f"malformed audio: {type(b64).__name__}")
                    words = parse_words(data.get("timestamps"))
                    if not b64:
                        # The trailing empty chunk: the sentence's full word list.
                        trailing = words or []
                        continue
                    # Decode whole samples only; a chunk that ends mid-sample carries its
                    # odd byte into the next one.
                    try:
                        raw = carry + base64.b64decode(b64)
                    except (binascii.Error, ValueError) as e:
                        raise _EngineError("malformed audio") from e
                    cut = len(raw) - len(raw) % 2
                    carry = raw[cut:]
                    pcm = np.frombuffer(raw[:cut], dtype=np.int16)
                    if pcm.shape[0] == 0:
                        continue
                    heard = True
                    block = pcm.astype(np.float32) / 32768.0
                    if _STREAM_AUDIO:
                        yield "audio", block, words
                    else:
                        pending.append(block)
                        if words is not None:
                            chunk_words = (chunk_words or []) + words
                elif mtype == "audio.done":
                    if data.get("error"):
                        raise _EngineError(f"tts engine (stream): sentence "
                                           f"{data.get('sentence_index')} failed")
                    for event in close_sentence():
                        yield event
                elif mtype == "session.done":
                    for event in close_sentence():
                        yield event
                    break
                elif mtype == "error":
                    raise _EngineError(f"tts engine (stream): "
                                       f"{data.get('message') or data.get('error')}")
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
