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
import os
import time
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
from teaport_brain.env import env_flag, env_num
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
# rather than on the drain). Its 3s default is fine on GPU (synth ≤ ~1.2s/chunk) but on the CPU backends a
# ramped chunk of 110+ chars synthesizes >3s with nothing queued, tripping it in normal
# operation. 15s covers the worst cap/hard_max-sized chunk at CPU RTF ~0.6 with margin.
_STOP_FRAME_TIMEOUT_S = env_num("TTS_STOP_FRAME_TIMEOUT_S", "15", float)

# Clause-chunking: the engine TTS encodes the WHOLE input before emitting any audio, so first-audio
# latency scales with input length. Synthesize a SHORT opening clause (fast first-audio) and
# the rest in larger pieces — later pieces play behind the first, so at RTF<1 their synth time
# is hidden. Each clause's leading/trailing near-silence is trimmed so the per-synth padding
# doesn't stack into an over-long seam: naive concat gives a ~793 ms gap vs a whole sentence's
# natural ~320 ms comma pause; trimming to ~lead+trail lands it near the natural pause, and the
# terminal pitch + register are already continuous across the seam (measured). Per-word
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
# (see split_clauses_ramp): the engine synthesizes a chunk whole before any of it
# plays, so one long sentence is the whole first-audio wait. Live 2026-09-04: a
# 236-char sentence began 4.6 s after the model finished it, a ~30 s one 6.6 s, and
# at a 120 cap a 112-char one still waited 1.7 s. Sentences up to this length keep
# their prosody untouched. 0 disables the split.
_SENTENCE_SOFT_MAX = env_num("TTS_SENTENCE_SOFT_MAX", "80", int)
_SEAM_KEEP_LEAD = env_num("TTS_SEAM_KEEP_LEAD", "0.05", float)   # s kept before first sound
_SEAM_KEEP_TRAIL = env_num("TTS_SEAM_KEEP_TRAIL", "0.25", float)  # s kept after last sound

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


def _trim_seam_silence(audio, words, sr):
    """Trim leading/trailing near-silence from a clause's audio so chunked clauses don't stack
    the engine's per-synth silence padding into an over-long seam. Shifts each word's start time by
    the leading trim so timestamps stay aligned. Returns (trimmed_audio, adjusted_words)."""
    n = audio.shape[0]
    if n == 0:
        return audio, words
    peak = float(np.max(np.abs(audio)))
    if peak <= 0.0:
        return audio, words
    nz = np.nonzero(np.abs(audio) > 0.01 * peak)[0]
    if nz.size == 0:
        return audio, words
    start = max(0, int(nz[0] - _SEAM_KEEP_LEAD * sr))
    end = min(n, int(nz[-1] + 1 + _SEAM_KEEP_TRAIL * sr))
    lead_cut = start / sr
    trimmed = audio[start:end]
    if lead_cut > 0.0 and words:
        words = [(w, max(0.0, s - lead_cut)) for (w, s) in words]
    return trimmed, words

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


class EngineTTSService(TTSService):
    """Pipecat TTS service for the engine TTS (text-in) with per-word timestamps."""

    def __init__(self, *, voice: str = "af_heart", language: str = None,
                 lang_code: str = None, speed: float = 1.0, **kwargs):
        self._voice = voice
        self._speed = speed
        # Cross-run_tts word-time base. pipecat calls run_tts once PER SENTENCE but keeps
        # ONE word-timestamp baseline for the whole reply, so a per-call offset that
        # restarts at 0 makes every sentence's words collide on the reply's opening
        # ("To die, to sleep" landing right after "To be"). This accumulator carries the
        # base across calls; reset_word_timestamps() zeroes it when the base clears its
        # baseline (reply end / interruption).
        self._reply_audio_offset = 0.0
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
            # Push a TTSStoppedFrame once a context's audio is fully enqueued (pipecat
            # 1.7.0 on_turn_context_completed). The output transport ends bot-speaking on
            # that frame, which it queues behind the last chunk; with no stop frame it
            # falls back to its BOT_VAD_STOP_FALLBACK_SECS (3 s) idle timeout, so every
            # reply used to be followed by 3 s of "bot speaking" nobody could hear
            # (journal 2026-09-04: BotStoppedSpeaking = audio end + 2.9..3.0 s on 8/8
            # replies; `based on TTSStoppedFrame` never once). That tail is what the
            # assistant aggregator waits out before running the post-tool-call
            # completion (3 s of dead air after every tool filler), what keeps the
            # 2-word barge-in guard armed past the audio, and what the follow-up gate
            # reads as "still talking". stop_frame_timeout_s below remains the sweep
            # for a context that never yielded audio (nothing synthesizable): pipecat
            # appends no stop frame for it, so only the timeout reclaims it, and the
            # transport ignores a stop frame that follows no audio.
            push_stop_frames=True,
            stop_frame_timeout_s=_STOP_FRAME_TIMEOUT_S,
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
        # seam silence is trimmed (see _trim_seam_silence) so the joins sound like natural
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
        # NB: use self._reply_audio_offset (carries across run_tts calls), NOT a local
        # per-call offset — see __init__ / reset_word_timestamps.
        emitted_audio = False
        emitted_secs = 0.0
        failed_clauses = 0
        # No interrupted-context bookkeeping here: a barge-in cancels the process
        # task this generator runs on (pipecat 1.5.0 InterruptionFrame handling),
        # so an in-flight run_tts dies at its next await — measured 255 live
        # interruptions with zero survivors before the old guard was removed.
        for clause in clauses:
            # User speaking → hold this clause so the GPU serves STT (a maybe-barge
            # needs its words transcribed NOW); resume on VAD-stop or the cap.
            await self._hold_for_user_speech()
            try:
                segments = await self._synth_text(clause)
            except Exception as e:  # noqa: BLE001
                # Skip this clause but keep going — one bad chunk shouldn't abort the
                # whole reply mid-sentence and leave the user hanging ("why did you
                # stop?"). hard_max should prevent the over-length case upstream.
                # (If EVERY clause fails we surface an ErrorFrame below — a
                # persistently broken engine must not degrade to silent dead air.)
                logger.error(f"engine synth error on clause (skipping): {e!r} clause={clause[:60]!r}")
                failed_clauses += 1
                continue
            for audio, words in segments:
                audio, words = _trim_seam_silence(audio, words, self.sample_rate)
                if audio.shape[0] == 0:
                    continue
                if words:
                    base = self._reply_audio_offset
                    if _TRACE:
                        _g = [base + s for (_w, s) in words]
                        logger.info(f"[WTS] off={base:.2f} n={len(words)} span=[{_g[0]:.2f},{_g[-1]:.2f}] first={words[0][0]!r} last={words[-1][0]!r}")
                    # Release captions _CAPTION_LEAD_SECS early to compensate the client's
                    # audio buffer (fixes the "caption lags the voice" feel) and to keep
                    # words ahead of the barge-in flush (fixes the dropped spoken tail).
                    await self.add_word_timestamps(
                        [(w, max(0.0, base + s - _CAPTION_LEAD_SECS)) for (w, s) in words], context_id)
                pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
                yield TTSAudioRawFrame(pcm, self.sample_rate, 1, context_id=context_id)
                emitted_audio = True
                emitted_secs += audio.shape[0] / self.sample_rate
                self._reply_audio_offset += audio.shape[0] / self.sample_rate
        # Playout forensics: which utterance emitted how much audio, into which
        # context, and when relative to barge-ins (the stale-speech bug class).
        logger.debug(f"{self}: run_tts done — {emitted_secs:.1f}s audio "
                     f"ctx={str(context_id)[:8]} [{text[:36]}…]")
        if failed_clauses and not emitted_audio:
            # EVERY clause failed (CUDA OOM, unsupported language, corrupted engine):
            # dead air with no signal is the worst outcome — surface it so the
            # pipeline/user knows the voice is down.
            yield ErrorFrame(error=f"tts: all {failed_clauses} clause(s) failed to synthesize")

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
        # The base zeroes its per-reply word-timestamp baseline here (reply end or
        # interruption); zero our cross-call audio-offset accumulator in lockstep so the
        # next reply's first sentence starts at 0 again — otherwise it would inherit the
        # previous reply's tail offset and schedule every word in the past (dropped).
        # First, remember where this context's audio ends (baseline + everything it
        # emitted) for the next one -- unless a barge-in just flushed it.
        # (A reset with no baseline set is a no-op reset -- pipecat resets more than
        # once around a context end -- and must not forget a real end.)
        base = self._initial_word_timestamp
        if self._interrupted:
            self._prev_audio_end_ns = 0
            self._interrupted = False
        elif base != -1:
            self._prev_audio_end_ns = base + int(self._reply_audio_offset * 1e9)
        await super().reset_word_timestamps()
        self._reply_audio_offset = 0.0

    async def _handle_interruption(self, frame, direction):
        # Forensics for the stale-speech bug class: prove the barge-in actually
        # reached the TTS (fresh context dicts, word-timestamp reset) and when.
        logger.debug(f"{self}: interruption reached TTS "
                     f"(turn_ctx={str(self._turn_context_id)[:8]})")
        self._interrupted = True  # the base resets word timestamps below: no carry
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

    async def _synth_text(self, text: str):
        """Synthesize via the engine's vLLM-Omni text-in stream (/v1/audio/speech/stream).
        The ENGINE does G2P + number normalization + word timing — the brain sends raw text,
        no phonemize. One WS per call (the stream is one-shot per connection:
        config -> input.text -> input.done -> results -> close). Returns
        [(audio float32 [-1,1], [(word, start_sec)])], one segment per engine sentence
        (the engine splits input.text on . ! ? and newline; the brain's clause-ramp already
        gates first-audio by sending one clause per call). run_tts applies the seam-trim /
        offset / caption logic. Number expansions fold their word_timestamps back to the
        source token engine-side, so captions show "2026", not "twenty twenty six"."""
        import base64
        import json
        ws = await self._connect_stream()
        try:
            await ws.send(json.dumps({"type": "session.config", "voice": self._voice,
                                      "response_format": "pcm", "stream_audio": True,
                                      "word_timestamps": True}))
            await ws.send(json.dumps({"type": "input.text", "text": text},
                                     ensure_ascii=False))
            await ws.send(json.dumps({"type": "input.done"}))
            segments, pcm, words = [], b"", []
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=_STREAM_TIMEOUT)
                if isinstance(msg, (bytes, bytearray)):
                    continue  # word_timestamps=True => audio rides base64 JSON, not binary
                data = json.loads(msg)
                mtype = data.get("type")
                if mtype == "audio.start":
                    pcm, words = b"", []
                elif mtype == "audio.chunk":
                    b64 = data.get("audio_b64") or ""
                    if b64:
                        pcm += base64.b64decode(b64)
                    ts = data.get("timestamps")
                    if ts:  # [{word,start_ms,end_ms}]; null (aligner failed) / [] => no words
                        # Bare word tokens (no trailing space): pipecat 1.5.0's word tracker
                        # matches them against the LLM's leading-space tokens (a trailing space
                        # mismatches and the word is discarded); captions.py rejoins with spaces.
                        words = [(w.get("word", ""),
                                  float(w.get("start_ms", 0)) / 1000.0) for w in ts]
                elif mtype == "audio.done":
                    if data.get("error"):
                        raise RuntimeError(f"tts engine (stream): sentence "
                                           f"{data.get('sentence_index')} failed")
                    if pcm:
                        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                        if audio.shape[0] > 0:
                            segments.append((audio, words))
                    pcm, words = b"", []
                elif mtype == "session.done":
                    break
                elif mtype == "error":
                    raise RuntimeError(f"tts engine (stream): "
                                       f"{data.get('message') or data.get('error')}")
            return segments
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
