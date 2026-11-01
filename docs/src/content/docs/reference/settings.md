---
title: Settings reference
description: Every setting teaport reads, the file it lives in, its default, and what it does.
---

This page lists every setting teaport reads, the file it lives in, and what it does. See [Configure teaport](/guides/configuration/) for the configuration page, the secret files, and how to apply a change.

<!-- source: brain/teaport_brain/config_schema.toml:1-2522#25d1d90f -->

<!-- generated from brain/teaport_brain/config_schema.toml — edit that, then: python -m teaport_brain.config_schema -->

## Where settings live

Every setting lives in one of the files in this table; the second column names the service that reads it, and the last says how a change takes effect.

| File | Read by | To apply a change |
|---|---|---|
| `/etc/teaport/engine.env` | `teaport-engine` | systemctl restart teaport-engine — reloads Voxtral + Kokoro onto the GPU; the brain loses its engine session (Talk and a live call alike). |
| `/etc/teaport/brain.env` | `teaport-brain` | systemctl restart teaport-brain — drops the live Talk session; a live phone call stays up at the gateway and is picked up again by the restarted brain (it says sorry, it lost you for a moment). |
| `~/.config/teaport/teaport-sip.conf` | `teaport-sip` | Edit it and `teaport sip restart` (or re-run `teaport sip configure`, which test-registers before it writes). `teaport sip aec on\|off` flips the echo canceller and restarts the gateway for you. |
| `/etc/teaport/bridge.env` | `teaport-discord-bridge` | systemctl restart teaport-discord-bridge. |
| `/etc/teaport/local-audio.env` | `teaport-local-audio` | systemctl restart teaport-local-audio (ends the local session; it redials the brain at once). |
| `/etc/teaport/wifi-setup.env` | `teaport-wifi-setup` | Read at each Wi-Fi setup (the unit runs only on request): nothing to restart. Written when the drive is flashed, so the paper insert can print the password; install.sh never writes it. |

Nothing hot-reloads: every value is fixed when its service starts, so a change
is a restart of the service(s) in the second column.

## Language model

These settings choose the large language model (LLM) endpoint and model, cap the completion, and add guards and spoken notices around the replies.

In `/etc/teaport/brain.env`.

| Setting | Default | Description |
|---|---|---|
| `LLM_BASE_URL` | **required** | Your OpenAI-compatible endpoint: https://api.groq.com/openai/v1, https://api.cerebras.ai/v1, https://openrouter.ai/api/v1, or http://127.0.0.1:8182/v1 for a local server. A value set here survives a re-run of install.sh unless that run is given one explicitly (the prompt, or TEAPORT_LLM_BASE_URL). |
| `LLM_API_KEY` | file `~/.config/teaport/llm_key` | The key for that endpoint. A local server that ignores auth still needs a placeholder such as sk-local. Prefer the file; the env var, if set, wins over it. |
| `LLM_MODEL` | `gpt-oss-120b` | The served model name. A value set here survives a re-run of install.sh unless that run is given one explicitly (the prompt, or TEAPORT_LLM_MODEL). |
| `LLM_REASONING_EFFORT` | `low` | Reasoning effort for models that support it. Set "" to disable for models that do not. One of `low`, `medium`, `high`, `""`. low cuts gpt-oss's hidden chain-of-thought ~10x before the first spoken token. Also sets the LLM_MAX_TOKENS default. |
| `LLM_MAX_TOKENS` | by `LLM_REASONING_EFFORT`: 1024 at `low`, 3072 at `medium`, 8192 at `high`, 4096 at `""` | Completion cap. A credit-metered gateway reserves against the model's ceiling and refuses the request when the balance cannot cover it, however short the answer. Reasoning tokens bill as completion tokens. 0 = no cap, correct for a local or un-metered endpoint. |
| `LLM_TIMEOUT_SECS` | **20** s (≥ 1) | How long one completion attempt can take before it is abandoned. One retry follows, so the worst case before the failure is spoken is roughly twice this. Raise it for a slow local llama.cpp box. Without it the OpenAI SDK waits 600 s with no error, no log line and no audio. |
| `LLM_EXTRA_BODY` | — | Optional JSON object merged into the request extra_body. OpenRouter routing example: {"provider":{"order":["Groq"],"allow_fallbacks":true}}. |
| `TEAPORT_LLM_TEXT_GUARD` | **on** | Fold degenerate unicode out of the model's replies and cut a runaway ellipsis collapse. |
| `TEAPORT_FACE_MOOD` | `tag` | Where the OLED face's mood comes from. tag: the model begins a reply with a mood tag ([mood: sad], never spoken) when it has a clear feeling, and the reply's words are scored (VADER) when it gave none. text: the words only, no tag. off: the face is given no mood. One of `tag`, `text`, `off`. |
| `TEAPORT_LLM_GUARD_RECOVERY` | `""` | The one line spoken when that guard trips. Empty = the built-in English line; override it for a non-English deployment. |
| `TEAPORT_LLM_ERROR_SPEECH` | **on** | Say a short line out loud when the model call fails, instead of leaving the room silent. |
| `TEAPORT_LLM_ERROR_SPEECH_DEBOUNCE` | **30** s (≥ 0) | Seconds between spoken failure notices, so a failing endpoint is reported once per window rather than once per turn. |
| `TEAPORT_RAW_LLM_CAPTURE` | **on** | Log a completion verbatim when it degenerates. Healthy turns log nothing. |

## Speech engine connection

These settings point the brain at the engine's speech-to-text (STT) and text-to-speech (TTS) endpoints and keep a slow engine from hanging a reply.

In `/etc/teaport/brain.env`.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_URL` | `ws://127.0.0.1:8000/v1/realtime` | The engine realtime speech-to-text WebSocket. The installer derives it from ENGINE_PORT. A wrong value is the bot saying "speech recognition isn't available". *Set by the installer.* |
| `ENGINE_TTS_URL` | `ws://127.0.0.1:8000/v1/tts` | The engine text-to-speech base; the stream URL derives from it. *Set by the installer.* |
| `ENGINE_TTS_STREAM_URL` | `<ENGINE_TTS_URL base>/v1/audio/speech/stream` | Only set to override the derivation from ENGINE_TTS_URL. *Set by the installer.* |
| `TTS_VOICE` | `af_heart` | Kokoro voice id. A per-session choice from OpenClaw wins over this. |
| `TTS_LANGUAGE` | — | Kokoro language. A per-session choice from OpenClaw wins over this; unset = the engine default. |
| `TTS_REMOTE_TIMEOUT` | **20** s (≥ 1) | Per-request receive timeout on the TTS stream. A clause synthesizes in ~0.2–0.5 s; 20 s covers a stuck engine without hanging a reply. |
| `TTS_CONNECT_RETRIES` | **3** (≥ 1) | Connect attempts while the engine drains synth a barge-in abandoned (~1.5 s on an Orin Nano). One attempt is not enough; three is. The code clamps to at least 1; 0 or 1 both mean a single attempt. |
| `TTS_CONNECT_BACKOFF` | **0.6** s (≥ 0) | Gap between those connect attempts. |
| `TTS_STOP_FRAME_TIMEOUT_S` | **15** s (≥ 1) | Pipecat's stop-frame timeout. Its 3 s default trips in normal CPU-backend operation; 15 s covers the worst cap-sized chunk at CPU RTF ~0.6. |
| `TEAPORT_STT_BACKEND` | `""` | Experimental: streaming points the STT service at vLLM serving Voxtral Realtime. The incumbent engine is the default. One of `""`, `streaming`. |
| `TEAPORT_STT_URL` | `ws://127.0.0.1:8100/v1/realtime` | Only with TEAPORT_STT_BACKEND=streaming. |
| `TEAPORT_STT_MODEL` | `voxtral-realtime` | Only with TEAPORT_STT_BACKEND=streaming. |
| `TEAPORT_STREAM_TAIL_SECS` | **0.7** s (≥ 0) | Only with the streaming backend. |

## TTS clause chunking

These settings cut each reply into clauses for the synthesizer and limit how far ahead of playback it works.

In `/etc/teaport/brain.env`. First-audio latency versus seam quality. Each is explained at length in engine_tts.py.

| Setting | Default | Description |
|---|---|---|
| `TTS_SENTENCE_SOFT_MAX` | **80** chars (≥ 0) | A sentence longer than this is split at clause boundaries before synthesis; first audio scales with the chunk's length, so one long sentence is the whole first-audio wait (a 236-char sentence began 4.6 s after the model finished it). 0 disables the split. |
| `TTS_FIRST_CLAUSE_CHARS` | **32** chars (≥ 8) | Size of the first chunk — the one the caller waits on. |
| `TTS_CLAUSE_GROWTH` | **1.5** (1.0–1.67) | Each chunk can grow this many times the previous. Must stay below 1/RTF (~1.67 at a CPU RTF of 0.6) or a chunk's synth outruns the previous chunk's playout and playback stalls at the seam. |
| `TTS_CLAUSE_CAP` | **200** chars (≥ 8) | The largest ramped chunk. |
| `TTS_CLAUSE_HARD_MAX` | **350** chars (8–450) | Last-resort mid-sentence word break so a run-on cannot overflow the engine's ~512-token utterance limit and crash the synth. |
| `TTS_SEAM_KEEP_LEAD` | **0.05** s (≥ 0) | Near-silence kept before a chunk's first sound. |
| `TTS_SEAM_KEEP_TRAIL` | **0.25** s (≥ 0) | Near-silence kept after a chunk's last sound. With the lead this lands a seam near a sentence's natural ~320 ms comma pause instead of the ~793 ms a naive concat gives. |
| `TTS_STREAM_AUDIO` | **on** | Play each engine audio chunk as it arrives (first audio sooner). Off buffers each sentence to its end and tells the engine to skip its eager prefix. Turn it off on a CPU-only engine or wherever synthesis runs slower than playout, where the early first block plays out before the rest of the sentence arrives and leaves a gap mid-sentence; the journal logs each such gap as "TTS stream underrun". |
| `TTS_USER_SPEECH_HOLD_MAX_S` | **3.0** s (≥ 0) | While VAD says the user is speaking, the next clause is held back to free the GPU for barge-in transcription. This caps the hold so sustained noise cannot stall the reply. |
| `TTS_HOLD_ON_USER_SPEECH` | **on** | Hold the next clause while VAD hears the user, so STT gets the GPU (capped by TTS_USER_SPEECH_HOLD_MAX_S). |
| `TTS_PACING` | `greedy` | greedy submits each clause as soon as the previous one is synthesized. lead holds it until less than TTS_LEAD_S of synthesized audio is still unplayed, so the GPU idles for STT through most of the playout and a barge-in discards less synthesis. One of `greedy`, `lead`. Barge-in latency is the same in both modes, because synthesis runs ~30x real time; an interrupted reply discards ~3-5 s of synthesis instead of ~15-45 s. |
| `TTS_LEAD_S` | `auto` | With TTS_PACING=lead: seconds of unplayed audio to keep queued, or auto for max(1.5 s, 2x the next clause's synth time learned this session). |
| `TTS_EAGER` | `always` | Which clauses ask the engine for its eager prefix (an earlier first block, ~10% more synthesis): always, each reply's first clause, any clause submitted with less audio queued ahead of it than its own synth time (low_lead), or never. Ignored with TTS_STREAM_AUDIO off, which never asks. One of `always`, `first`, `low_lead`, `never`. |
| `TTS_CAPTION_LEAD_SECS` | **0.2** s (≥ 0) | Caption lead shared by the TTS word timestamps and the heard-word ledger. One setting so the two cannot drift. |

## Turn-taking

These settings decide when the caller's turn ends and when the bot hears an interruption, through voice activity detection (VAD), endpointing, and the Smart Turn verdicts.

In `/etc/teaport/brain.env`. Each is explained at length where it is read, in endpointing.py.

| Setting | Default | Description |
|---|---|---|
| `ENDPOINT_STOP_SECS` | **0.5** s (≥ 0.1) | Silence before the VAD reports the caller stopped. The dominant fixed latency on every turn; lower is snappier and cuts more mid-sentence pauses. The transcript segment survives a pause Smart Turn calls unfinished (the STT commits on its verdict, not on this stop), so this can come down toward pipecat's 0.2 where the verdicts are trustworthy (wideband audio); on telephony they are not, and the floor still does the work. |
| `SMARTTURN_STOP_SECS` | **1.0** s (≥ 0) | How long a Smart Turn "not done" verdict holds the turn open, counted from the VAD stop. The transcript segment is held open for the same wait, so a caller who resumes keeps one utterance. With TEAPORT_SPECULATIVE_REPLY on, the reply is already being generated on the settled words while this runs, so it can be raised (more patience for mid-sentence pauses) by up to the LLM's own latency at no cost to the turns that fall through. |
| `TEAPORT_SPECULATIVE_REPLY` | **off** | Ask the LLM as soon as the caller's words stop changing on a turn Smart Turn has not concluded on (an INCOMPLETE verdict waiting out SMARTTURN_STOP_SECS) — the settled interim transcript, which on the current engine equals the final — and use that reply if the turn then commits with exactly those words and nothing else touched the context. Off: the request waits for the commit. On: those turns answer up to the LLM's latency sooner (~0.5 s in tests), and a turn the caller resumes has spent one wasted request — every outcome logs a [SPEC] line with the running hit/miss tally. |
| `TEAPORT_SPECULATE_SETTLE_MS` | **160** ms (≥ 0) | How long the interim transcript must stop changing, under a Smart Turn "not done" with the caller quiet, before TEAPORT_SPECULATIVE_REPLY asks the LLM on it -- when the engine does not report how far it has transcribed (see TEAPORT_SPECULATE_ON_DECODE), or for words that arrive after its report. Two of the engine's 80 ms decoder tokens: one empty token between words cannot trip it. Lower starts the request sooner and opens more on words that then grow ([SPEC] miss reason=superseded); higher wastes fewer and gains less. |
| `TEAPORT_STT_TRANSCRIBED_MARGIN_MS` | **100** ms (≥ 0) | With TEAPORT_SPECULATE_ON_DECODE, how far past the caller's end of speech (Silero's) the engine must report it has transcribed before the speech counts as done and the speculative reply is asked. Covers the decoder hearing an unvoiced tail Silero has already called silence. Lower starts the request sooner (each 80 ms is one decoder step) at the risk of a last word arriving after it ([SPEC] miss reason=superseded). |
| `TEAPORT_SPECULATE_ON_DECODE` | **on** | With TEAPORT_SPECULATIVE_REPLY on, ask the LLM once the engine reports it has transcribed past the end of the caller's speech -- the interim is then all the words that speech will get -- rather than once the interim has been unchanged for TEAPORT_SPECULATE_SETTLE_MS, which cannot tell a finished sentence from the engine pausing between two words while it catches up. Needs an engine that offers the report (the engine's transcription.progress message); with any other the settle window is used either way, as it is for a turn whose speech end could not be placed. The engine is asked for the report only when this and TEAPORT_SPECULATIVE_REPLY are both on. Off: always the settle window (the A/B switch). The margin past the speech end is TEAPORT_STT_TRANSCRIBED_MARGIN_MS. |
| `SMARTTURN_COMPLETE_THRESHOLD` | **0.5** (0–1) | The end-of-turn probability that counts as done. Near-inert on telephony audio. |
| `VAD_CONFIDENCE` | **0.7** (0–1) | Silero's speech-probability gate. |
| `VAD_MIN_VOLUME` | **0.6** (0–1) | Silero's volume gate. |
| `VAD_SAMPLE_RATE` | `16000` | The rate Silero runs at, shared: /talk and the room mic take it unless TALK_VAD_SAMPLE_RATE / ROOM_VAD_SAMPLE_RATE is set; phone calls have SIP_VAD_SAMPLE_RATE (8000 by default) and do not take it. 8000 decimates to 8 kHz before the model, which is right only for narrowband audio (the phone's 8 kHz G.711, upsampled by the gateway); on real wideband it throws away the top half of the band. One of `16000`, `8000`. |
| `TEAPORT_INTERRUPT_MIN_WORDS` | **2** (≥ 1) | Transcribed words needed to interrupt the bot. 1 makes every word a barge-in, backchannels included. On SIP, while the barge-in pause has the bot paused, an utterance made only of TEAPORT_STOP_WORDS interrupts at any length, and once the paused reply would have finished anyway one word is enough. |
| `SIP_ENDPOINT_STOP_SECS` | — | ENDPOINT_STOP_SECS for phone calls (the SIP front-end) only. Empty: the shared ENDPOINT_STOP_SECS. One brain process serves both, and they were tuned apart. |
| `SIP_SMARTTURN_STOP_SECS` | — | SMARTTURN_STOP_SECS for phone calls (the SIP front-end) only. Empty: the shared SMARTTURN_STOP_SECS. One brain process serves both, and they were tuned apart. |
| `SIP_SMARTTURN_COMPLETE_THRESHOLD` | — | SMARTTURN_COMPLETE_THRESHOLD for phone calls (the SIP front-end) only. Empty: the shared SMARTTURN_COMPLETE_THRESHOLD. One brain process serves both, and they were tuned apart. |
| `SIP_INTERRUPT_MIN_WORDS` | — | TEAPORT_INTERRUPT_MIN_WORDS for phone calls (the SIP front-end) only. Empty: the shared TEAPORT_INTERRUPT_MIN_WORDS. One brain process serves both, and they were tuned apart. |
| `SIP_VAD_SAMPLE_RATE` | `8000` | VAD_SAMPLE_RATE for phone calls (the SIP front-end) only; it does not fall back to the shared value. The trunk offers only PCMU and G.729, so a call is 8 kHz audio the gateway upsampled to 16 kHz, and Silero, a waveform model, does better on the true narrowband signal: on 11 recorded calls, 8000 found a few more caller utterances and a shorter slow tail, with no more false detections while the bot talks. 16000 runs it on the upsample. One of `16000`, `8000`. |
| `TALK_ENDPOINT_STOP_SECS` | — | ENDPOINT_STOP_SECS for Talk sessions (/talk: the app, the dashboard, Discord; not the box's own mic, which has ROOM_*) only. Empty: the shared ENDPOINT_STOP_SECS. One brain process serves both, and they were tuned apart. Testing on Talk chose 0.2 / 0.6, against the phone's 0.5 / 1.0. |
| `TALK_SMARTTURN_STOP_SECS` | — | SMARTTURN_STOP_SECS for Talk sessions (/talk: the app, the dashboard, Discord; not the box's own mic, which has ROOM_*) only. Empty: the shared SMARTTURN_STOP_SECS. One brain process serves both, and they were tuned apart. Testing on Talk chose 0.2 / 0.6, against the phone's 0.5 / 1.0. |
| `TALK_SMARTTURN_COMPLETE_THRESHOLD` | — | SMARTTURN_COMPLETE_THRESHOLD for Talk sessions (/talk: the app, the dashboard, Discord; not the box's own mic, which has ROOM_*) only. Empty: the shared SMARTTURN_COMPLETE_THRESHOLD. One brain process serves both, and they were tuned apart. |
| `TALK_INTERRUPT_MIN_WORDS` | — | TEAPORT_INTERRUPT_MIN_WORDS for Talk sessions (/talk: the app, the dashboard, Discord; not the box's own mic, which has ROOM_*) only. Empty: the shared TEAPORT_INTERRUPT_MIN_WORDS. One brain process serves both, and they were tuned apart. |
| `TALK_VAD_SAMPLE_RATE` | — | VAD_SAMPLE_RATE for Talk sessions (/talk: the app, the dashboard, Discord; not the box's own mic, which has ROOM_*) only. Empty: the shared VAD_SAMPLE_RATE. Talk audio is real wideband, so 8000 would throw away the top half of the band. One of `16000`, `8000`. |
| `ROOM_ENDPOINT_STOP_SECS` | **0.3** s (≥ 0.1) | ENDPOINT_STOP_SECS for the box's own microphone (the room mic) only; it does not fall back to the shared value or to Talk's. People talking across a room pause mid-sentence: tests on room recordings picked 0.3 / 1.5 against the 0.2 / 0.6 the room had from Talk, cutting the mid-sentence splits from about 18 of 29 to about 6, for +0.1 s on a typical turn end. |
| `ROOM_SMARTTURN_STOP_SECS` | **1.5** s (≥ 0) | SMARTTURN_STOP_SECS for the box's own microphone (the room mic) only; it does not fall back to the shared value or to Talk's. The ceiling is the room's main lever against mid-sentence splits (raising it from 0.6 to 1.5 at a 0.3 s stop took them from about 14 of 29 to about 6). Its cost falls on the turn ends Smart Turn calls unfinished, about a quarter in testing, which wait ROOM_ENDPOINT_STOP_SECS + this (1.8 s) instead of 0.8 s; with TEAPORT_SPECULATIVE_REPLY on, the reply is already being generated under it. |
| `ROOM_SMARTTURN_COMPLETE_THRESHOLD` | — | SMARTTURN_COMPLETE_THRESHOLD for the box's own microphone (the room mic) only. Empty: the shared SMARTTURN_COMPLETE_THRESHOLD. |
| `ROOM_INTERRUPT_MIN_WORDS` | — | TEAPORT_INTERRUPT_MIN_WORDS for the box's own microphone (the room mic) only. Empty: the shared TEAPORT_INTERRUPT_MIN_WORDS. |
| `ROOM_VAD_SAMPLE_RATE` | — | VAD_SAMPLE_RATE for the box's own microphone (the room mic) only. Empty: the shared VAD_SAMPLE_RATE. The room mic is real wideband, so 8000 would throw away the top half of the band. One of `16000`, `8000`. |
| `TEAPORT_REPLY_HOLD_SIP` | **on** | Phone calls: before a reply to the caller's turn starts playing, check the line. If they have started talking again since the turn was committed, hold the reply; if their new words arrive, drop it and answer the whole turn as one message; if none come (a cough), play it TEAPORT_REPLY_HOLD_RELEASE_S after they go quiet. A caller who stays quiet is never delayed. |
| `TEAPORT_REPLY_HOLD_TALK` | **off** | The same reply hold for the OpenClaw Talk path. Off until it is proven on a Talk session; so far it is proven on phone calls only. |
| `TEAPORT_REPLY_HOLD_RELEASE_S` | **1.0** s (≥ 0) | How long a held reply waits for the caller's words once the line is quiet -- the VAD's stop and the fast onset test's offset, whichever is later -- before it plays anyway. So it is about ENDPOINT_STOP_SECS more than this after the speech ends. The answer to a tool's result, asked while the caller was talking, waits the same time after their VAD stop (FollowupGate), on every front-end. |
| `TEAPORT_REPLY_HOLD_MAX_S` | **6.0** s (≥ 0) | The longest a reply is ever held, from the start of the hold, whatever the VAD says. Raised to TEAPORT_REPLY_HOLD_RELEASE_S if set lower. Also caps the hold of a tool's answer under the caller's speech. |
| `TEAPORT_REPLY_HOLD_ONSET_MAX_S` | **2.5** s (≥ 0) | The cap for a hold only the fast onset test asked for (no VAD start in it). That test has no volume gate, so steady room noise above its threshold would otherwise hold every reply to TEAPORT_REPLY_HOLD_MAX_S. Never more than that, never less than TEAPORT_REPLY_HOLD_RELEASE_S. |
| `TEAPORT_ONSET_CONFIDENCE` | **0.6** (0–1) | Silero confidence that counts toward the fast speech-onset signal the reply hold (and, on SIP, the barge-in pause) uses. Below VAD_CONFIDENCE on purpose, and with no volume gate: it only has to say speech has begun, soon. It changes nothing the VAD decides. The STT reads the same test after a VAD stop: an onset since the stop holds a complete verdict's commit until the test's own offset (the caller paused again), and the silence hint does not count past any chunk at this confidence. |
| `TEAPORT_ONSET_MIN_MS` | **128** ms (≥ 32) | Consecutive speech at TEAPORT_ONSET_CONFIDENCE that makes an onset (32 ms per Silero chunk, rounded up). |
| `TEAPORT_BARGE_PAUSE` | **on** | SIP only: the moment the caller starts talking over the bot (the fast speech-onset test, or the VAD), pause playout. If their words follow, the barge-in cancels the reply as before; if none come within TEAPORT_BARGE_PAUSE_RESUME_S of them stopping, the reply resumes from where it paused. No effect on Talk, whose relay can only clear its client's buffer. |
| `TEAPORT_BARGE_PAUSE_RESUME_S` | **0.8** s (0–8) | The longest a paused reply waits, after the caller goes quiet, for their words before it resumes (sooner once the STT has closed that speech with no turn in it). With the VAD's own stop delay a cough stalls the bot about ENDPOINT_STOP_SECS + this. At most 8. |
| `TEAPORT_BARGE_PAUSE_MAX_S` | **6.0** s (0–7.75) | The longest one pause lasts before the reply resumes anyway. At most 7.75 s: the call's audio resumes on its own at 8 s (a write held past pipecat's 10 s deadline loses the call its voice), and the reply's own resume, up to 0.15 s after the cap, comes first. |
| `TEAPORT_BARGE_JUDGE` | **off** | SIP only, with TEAPORT_BARGE_PAUSE: words spoken over a paused reply (other than TEAPORT_STOP_WORDS) are first shown to a one-word LLM judge. If they need no answer ("okay", "mm-hmm") the reply resumes from where it paused, as for a cough; if they ask it to stop, ask something or correct it, or no verdict comes within 1.2 s, they interrupt as before, about a judge's time later. One extra short LLM request per such pause. |
| `TEAPORT_STOP_WORDS` | `stop,wait,hold on,hold,pause,enough,quiet,shh,shush` | Comma-separated words or phrases that end a reply on their own while it is paused for the caller's speech, whatever TEAPORT_INTERRUPT_MIN_WORDS says (with the bot silent there is no echo to garble into one). Only an utterance made entirely of them counts, so "don't stop" does not; any "shh" spelling is shh. "No" is left out on purpose: over the bot it is as often an answer as an objection. |
| `TEAPORT_ECHO_GUARD` | **on** | On the room mic, drop the bot's own words when the mic hears them back from the speaker, so the bot does not interrupt itself or answer its own sentence. Words that follow what the bot just said are taken off the front of a transcript; one made only of them is ignored. Off: every transcript goes through as heard. |
| `TEAPORT_STRANDED_INTERIM_SECS` | **1.5** s (≥ 0.2) | Commit a segment ourselves after this much interim quiet with no VAD stop, so a missed stop loses a second rather than the turn. |
| `TEAPORT_STT_SILENCE_HINT` | **on** | Tell the engine, on each commit, how much trailing silence the VAD has already established (the stop's silence window plus everything sent since, or only what was sent after the caller's voice where it came back after the stop and TEAPORT_STT_CARRY_MS does not send it again), so it can shorten the fixed ~1 s padding it decodes before answering. The engine takes the larger of this and its own count, clamped to what it has; one without the field's support ignores it. Off: the engine credits only what its own VAD counted, which on a phone line is often nothing. |
| `TEAPORT_STT_SILENCE_HINT_MARGIN_MS` | **100** ms (≥ 0) | Held back from the silence hint, for the tail the VAD's confidence gate drops before the decoder is done with it. Over-claiming by up to 240 ms cost nothing in engine testing; 400 ms truncated the last word on 7 of 30 utterances. With TEAPORT_STT_CARRY_MS on, it never takes a hint below 100 ms, the pause the engine needs to hear of before it re-primes the segment the carry opens: where the stop's window is that short (Talk's 0.2 s stop confirms 192 ms), the margin shrinks instead, at most to nothing. With the carry off it is held back whole. |
| `TEAPORT_STT_CARRY_MS` | **500** ms (0–2000) | After each commit, send the audio from where the silence hint's claim starts again, as the start of the next segment: at most this much, and never back past the previous commit. A word the caller begins just as a segment is closed then reaches the engine whole, with the silence before it, instead of split between two segments; only the hint's margin is decoded by both. Where this much does not reach back to the end of the caller's speech, or the commit was held for the caller's pause, voice heard since the stop stays in the closed segment and the carry starts after it. Needs TEAPORT_STT_SILENCE_HINT: a commit that claims no silence carries nothing. 0: the next segment opens at the commit, as before. |
| `TEAPORT_STT_TURN_TEXT` | `segment` | What the words of a turn are. segment: each commit closes an engine segment, and the engine's final for it is the turn's text. slice: the engine keeps one continuous transcript that commits never change (its preview stream; an engine without it runs on segments as before); a turn is the words that end before its commit, answered at once on the words in hand plus the engine's preview of the rest, then checked against the transcript's own words a moment later (about 0.7 s on an Orin NX, up to 2 s on an Orin Nano), which correct the conversation's record where they differ. In slice mode the carry and the wait for the caller's pause are off; the silence hint stays. One of `segment`, `slice`. Prototype: needs a teaport-engine with the preview stream ("transcription.preview" in its capabilities; engine 1.7 or later). |
| `TEAPORT_STT_PREVIEW_AT_STOP` | **on** | With TEAPORT_STT_TURN_TEXT=slice: ask the engine's preview of the turn at the end of the caller's speech, while the turn-end model is still deciding, instead of after it. The turn closes on that preview if the model calls the turn done, the speculative reply is asked on it as soon as it lands, and its final words come sooner; if the caller goes on talking, it is dropped and the next pause asks again. Off: the preview is asked when the turn closes. |
| `HEARD_MODE` | `truncate` | How the context records a reply the caller only partly heard: truncate it to what was heard, or keep it whole with a note. One of `truncate`, `note`. |
| `TEAPORT_SILENT_TURN_SECS` | **12** s (≥ 1) | How long a committed turn can produce no audio before it is reported in the journal. A turn waiting on an agent consult is not counted. |

## Agent consult and follow-ups

These settings choose the agent, time its consults, fill the wait with thinking sounds, and name the persona file a voice-only device uses.

In `/etc/teaport/brain.env`. TEAPORT_AGENT says whether the box has a co-resident OpenClaw gateway; every other row here applies only when it does.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_AGENT` | `openclaw` | Which agent, if any, is co-resident. `openclaw`: a gateway (host OpenClaw or a NemoClaw sandbox) provides web_search, web_fetch, search_memory, remember, openclaw_agent_consult, memory recall and OpenClaw's voice context (identity and profile). `none`: a voice-only box — those tools are not advertised, recall is not run, and the persona comes from TEAPORT_PERSONA_FILE. Asymmetric on repair: a detected gateway is positive evidence, so the installer writes `openclaw` unconditionally (installing a gateway later and re-running is all it takes to turn the tools on) — but NOT finding one is not evidence of anything, so the installer only seeds `none` on a first voice-only install and an existing value survives a repair. To disable agent tooling on a box that still has a working gateway, use the configuration page, not a hand edit of this file — a repair that still finds the gateway overwrites a hand edit here (and now logs that it did). One of `openclaw`, `none`. |
| `OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:18789` | The co-resident gateway for shared persona and memory recall. The installer sets it from the gateway port. *Set by the installer.* |
| `OPENCLAW_GATEWAY_TOKEN` | file `~/.config/teaport/openclaw_token` | Bearer token for that gateway. Prefer the file; the env var, if set, wins over it. |
| `OPENCLAW_AGENT_ID` | — | The OpenClaw agent this box's consults reach (model openclaw/<id> on /v1/chat/completions, --agent on the CLI fallback) and whose voice context (agent paragraph and profile) the room mic, the phone and the Discord bridge get from the teaport plugin. Must be a configured agent. Unset: the gateway's default agent, with the CLI fallback and the memory reindex run as --agent main. Memory and the web tools (remember, search_memory, web_search, web_fetch) stay with the gateway's default agent either way: remember writes OPENCLAW_MEMORY_DIR, and /tools/invoke names no agent. |
| `OPENCLAW_BIN` | — | The openclaw CLI used as the consult fallback. Auto-detected; the host might have none when OpenClaw runs only inside the sandbox. *Set by the installer.* |
| `TEAPORT_FRONTDOOR_HOST` | — | The name browsers reach the box's front door (Caddy, HTTPS) by. Pairing a laptop or phone shows a QR code of https://<this>/p/<code> and points the new browser's dashboard at wss://<this>; the installer writes the name it gave the front door (teaport.local unless TEAPORT_HOST says otherwise). Unset: no front door is known, and the pair_device tool is not offered. The QR code holds at most 53 bytes, so a name longer than 36 characters (ASCII) cannot be paired from the screen. *Set by the installer.* |
| `TEAPORT_AGENT_FIRST` | **off** | Agent-first routing: install the strict router directive and drop the "I'll work on that" ack. |
| `TEAPORT_RECALL_TIMEOUT` | **1.5** s (≥ 0.1) | Memory-recall budget. Never awaited on the frame path, so a generous value adds no turn latency; 1.5 s clears a cold embedder (~1.14 s on the device). |
| `TEAPORT_CONSULT_TIMEOUT` | **45** s (≥ 5) | Gateway/CLI consult ceiling on the synchronous path (TEAPORT_AGENT_FIRST, or a client with no follow-up injector). Otherwise the async path, SIP included, uses TEAPORT_ASYNC_CONSULT_TIMEOUT instead. The sandbox-exec shim adds ~14 s of spawn tax before the agent turn even starts. |
| `TEAPORT_NATIVE_CONSULT_ACK_TIMEOUT` | **1.5** s (≥ 0.2) | No ack within this means no native runner: fall back fast instead of burning the full window. A live relay acks within moments; the Discord bridge never acks. |
| `TEAPORT_NATIVE_CONSULT_TIMEOUT` | **45** s (≥ 5) | How long to wait on an acked native consult. Useful consults return in ~15–30 s; past ~45 s the voice wait degrades faster than the answer improves. |
| `TEAPORT_OPENCLAW_AGENT_CONSULT_TIMEOUT` | **55** s (≥ 5) | The function-call timeout for openclaw_agent_consult. Must exceed ack + native consult or pipecat abandons the call and drops the late answer. Bounds the sync path only. |
| `TEAPORT_ASYNC_CONSULT_TIMEOUT` | **130** s (≥ 5) | Async consult ceiling, for every lane: the native Talk consult and the gateway/CLI fallback (SIP). The consult runs off the turn; the answer is spoken as an unprompted follow-up when it lands, or a "that took too long" past this. Sized just past the Control UI's own hard 120 s consult wait, after which no Talk result can arrive. |
| `TEAPORT_FOLLOWUP_QUIET_S` | **0.7** s (≥ 0) | How long the conversation must stay quiet before a window counts for something the brain says or adds on its own (a delegated answer as it lands, a client note); rejects mid-thought pauses and between-turn gaps. |
| `TEAPORT_FOLLOWUP_MAX_WAIT_S` | **60** s (≥ 1) | Ceiling on one wait for such a window by a client note that asked for no reaction; past it the note waits again (it never goes in mid-turn). |
| `TEAPORT_REOFFER_AFTER_QUIET_S` | **10** s (≥ 1) | How long the line must stay quiet before a delegated answer the user has not heard yet (they cut it off, or spoke when it arrived) is mentioned once more, in passing. Once per answer; after that it is only given when asked. |
| `TEAPORT_THINKING_SOUND` | **on** | Typing during a long agent consult, in Talk and the room (on a phone call only in agent-first mode, where nothing else covers the wait); the OLED face types along. |
| `TEAPORT_THINKING_GRACE_S` | **1.5** s (≥ 0) | Quiet on the line before the typing is heard. |
| `TEAPORT_THINKING_GAIN` | **0.8** (0–1) | Bed level; 1.0 is the typing's limited peak (-6 dBFS). |
| `TEAPORT_THINKING_MAX_S` | **60** s (≥ 1) | Most typing heard per consult; the face types for the whole consult. |
| `TEAPORT_PERSONA_FILE` | `~/.config/teaport/persona.md` | The persona file: the source with TEAPORT_AGENT=none, and the fallback when the gateway cannot give the voice context OpenClaw composes (a Talk session's instructions, or the room/phone context from the teaport plugin; OpenClaw 2026.9.7+). The installer points it at the secrets dir. *Set by the installer.* |
| `OPENCLAW_MEMORY_DIR` | `~/.openclaw/workspace/memory` | The daily-note store a voice-saved memory is appended to, shared with the text agent so both recall it. *Set by the installer.* |
| `TEAPORT_THINKING_WAV` | `""` | Your own 24 kHz mono 16-bit wav to loop as the thinking bed (the face does not type along to it). Empty = typing generated live. |

## Tools

These settings turn each of the voice model's tools on or off.

In `/etc/teaport/brain.env`. One switch per tool the voice model can call (tools.py, THE TOOL CONTRACT). A tool also needs what it works with: the OpenClaw gateway (TEAPORT_AGENT=openclaw) for web, memory and openclaw_agent_consult; the voice for the voice tools; and, for set_volume, restart_session and end_conversation, a client that can do it itself (the local audio bridge announces them; end_conversation only with wake words set); answer_phone_call is offered to Talk sessions and the room mic, never to a call. A tool that is off, or missing what it needs, is not offered to the model and not named in its instructions.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_TOOL_GET_HOST_STATUS` | **on** | The machine's live free memory, CPU load and speech-engine decode speed. |
| `TEAPORT_TOOL_GET_CURRENT_TIME` | **on** | The local date and time. |
| `TEAPORT_TOOL_WEB_SEARCH` | **on** | Web search through the OpenClaw gateway. Needs TEAPORT_AGENT=openclaw. |
| `TEAPORT_TOOL_WEB_FETCH` | **on** | Read a web page through the OpenClaw gateway. Needs TEAPORT_AGENT=openclaw. |
| `TEAPORT_TOOL_SEARCH_MEMORY` | **on** | Recall from the shared long-term memory. Needs TEAPORT_AGENT=openclaw. |
| `TEAPORT_TOOL_REMEMBER` | **on** | Save a fact to the shared long-term memory. Needs TEAPORT_AGENT=openclaw. |
| `TEAPORT_TOOL_OPENCLAW_AGENT_CONSULT` | **on** | Hand a request to the full OpenClaw agent. Needs TEAPORT_AGENT=openclaw. Agent-first mode (TEAPORT_AGENT_FIRST) routes every turn through it, so with this off agent-first is ignored (with a warning in the journal). |
| `TEAPORT_TOOL_OPENCLAW_AGENT_CONTROL` | **on** | Let the model ask about, cancel, change or add to a request the OpenClaw agent is still working on, in any phrasing or language (OpenClaw's openclaw_agent_control; the Talk client runs it). Talk only, and only with TEAPORT_TOOL_OPENCLAW_AGENT_CONSULT on. Off, only what OpenClaw's own spoken run control recognizes ("cancel that", "how's it going?") reaches a request in progress. |
| `TEAPORT_TOOL_LIST_VOICES` | **on** | List the speaking voices. |
| `TEAPORT_TOOL_SWITCH_VOICE` | **on** | Change the speaking voice (and with it the reply language). |
| `TEAPORT_TOOL_SET_VOLUME` | **on** | Speaker louder, quieter or to a level. Only offered to a client that can do it itself (the local audio bridge); the level is kept across sessions. |
| `TEAPORT_TOOL_RESTART_SESSION` | **off** | End the conversation and start a fresh one (empty context, a new greeting) when the user asks. A testing aid, off by default: on, a misheard request can wipe a conversation. Only offered to a client that can reconnect itself (the local audio bridge). |
| `TEAPORT_TOOL_END_CONVERSATION` | **on** | Go back to sleep when the user says they are done ("that's all", "goodnight", "go to sleep", in any language): the agent says a short goodbye and the conversation sleeps at once instead of waiting out LOCAL_AUDIO_KEEPALIVE_SECS. Only offered to the local audio bridge, and only when it has wake words (there is nothing to go back to without them). |
| `TEAPORT_TOOL_ANSWER_PHONE_CALL` | **on** | How the user answers "should I step away?" when a phone call comes in during a Talk session or a room conversation, in any language: the model reads the reply and calls it. Off, a conversation is not asked: a call ends a Talk session with a spoken line, and is refused (the caller hears busy) while the room mic is awake. |
| `TEAPORT_TOOL_PAIR_DEVICE` | **on** | Connect a laptop, phone or tablet to the box by voice ("add my laptop"), its browser or the OpenClaw app: the box shows a QR code on its screen for the device's camera, opening a page with both choices, never reads it out, and says when the device is connected. Only in a room conversation or a Talk session (someone at the box scans it), never on a phone call or through the Discord bridge; needs TEAPORT_AGENT=openclaw, the front door (TEAPORT_FRONTDOOR_HOST, written by the installer) and an OLED avatar that draws QR codes. |
| `TEAPORT_TOOL_WIFI_SETUP` | **on** | Wi-Fi setup by voice: the spoken phrase ("set up Wi-Fi", no LLM needed; it starts setup only while the box has no internet) and the tool the model can hand over to. Only for a client at the box (the local audio bridge) and where install.sh laid down teaport-wifi-setup. See Wi-Fi setup. |

## Wi-Fi setup

These settings name the Wi-Fi setup network and set its password.

In `/etc/teaport/wifi-setup.env`. Say "set up Wi-Fi" at the box (the local audio bridge) and it opens a temporary setup network, teaport-ab12 (the end of its Wi-Fi MAC), with a page where a phone picks the box's network and types the password; the box speaks the network, the password and the page's address. No LLM involved, so it works offline; and only offline does the phrase start it by itself — online, the model hears it like any other words and decides, through the wifi_setup tool (Tools), which is also its switch. These two are flash-time settings for the paper insert.

| Setting | Default | Description |
|---|---|---|
| `WIFI_SETUP_PASSWORD` | — | The setup network's WPA2 password, written at flash time so the paper insert can print it. 8 to 63 characters, each an ASCII letter, a digit, a space or one of - _ . @ ! # & * (the symbols the voice can name when it spells the password). Digits are easiest to say and type. Unset (or invalid): fresh random digits each setup, spoken aloud. *Set by the installer.* |
| `WIFI_SETUP_SSID` | — | The setup network's name, for an insert printed before the MAC is known. Unset: teaport- and the last four hex digits of the Wi-Fi MAC. A name made only of hex digits (0-9, a-f) is ignored with a warning, and the MAC-based name used: some phones' QR scanners read such a name as raw bytes. *Set by the installer.* |

## Talk client context notes

These settings cap the size, the count, and the rate of the context notes a Talk client sends.

In `/etc/teaport/brain.env`. Limits on the notes a Talk client adds to the voice LLM's context through the plugin's `teaport.talk.context` method (client_notes.py). Read only by teaport-brain.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_CONTEXT_MAX_CHARS` | **1000** chars (≥ 1) | Longest context note a Talk client can send, in characters; a longer one is refused, not cut. The teaport-realtime plugin announces the smaller of this and its own 16000. |
| `TEAPORT_CONTEXT_MAX_NOTES` | **20** (≥ 1) | How many context notes stay in the LLM context; past it the oldest are removed. |
| `TEAPORT_CONTEXT_RESPOND_INTERVAL_S` | **15** s (≥ 0) | Minimum time between context notes that ask for a spoken reaction (respond:true); one sent sooner is refused as rate-limited. |

## Busy lamp

These settings drive the ReSpeaker XVF3800's LED ring as a busy lamp during phone calls.

In `/etc/teaport/brain.env`. The ReSpeaker XVF3800's LED ring as a busy lamp (BLF) for phone calls (xvf_led.py): breathing red while a call rings, solid red while it is live, then back to what it showed before, normally the firmware's own listening effect (the direction of the voice it hears). It follows the session arbiter's call claim, so it also goes back to normal when the call is torn down with the SIP front-end or the brain restarts or dies (the look to restore is kept in /run/teaport-busy-lamp until it is put back). The ring is driven by USB control transfers beside the audio streams; install.sh lays down a udev rule (/etc/udev/rules.d/60-teaport-xvf3800.rules) giving the device to the teaport-hw group, and puts the run user in it. A box without an XVF3800 does nothing. Read only by teaport-brain.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_BUSY_LAMP` | `auto` | auto lights the XVF3800's ring during a phone call when one is plugged in; off never touches the ring. One of `auto`, `off`. |
| `TEAPORT_BUSY_LAMP_COLOR` | `ff0000` | The lamp's colour, as RRGGBB hex (ff0000 is red). Anything else warns and falls back to red. |

## SIP front-end

These settings time the answer, shape the caller's audio, and decide what a call does to a live conversation.

In `/etc/teaport/brain.env`. Read by teaport-brain's SIP front-end (phone calls): how a call is answered, and how it asks a live conversation (a Talk session, the room mic awake) to make way for it.

| Setting | Default | Description |
|---|---|---|
| `SIP_HALF_DUPLEX` | **off** | Drop the caller's mic while the bot speaks, and while the thinking sound types over a consult (agent-first mode). On means no barge-in. |
| `SIP_HALF_DUPLEX_TAIL_S` | **0.8** s (≥ 0) | The tail after the bot stops, when half-duplex is on. |
| `SIP_STT_MAKEUP_DB` | **0** dB (0–20) | Makeup gain added to the caller signal the transcriber sees, to recover the quiet speech a caller produces over the bot. 0 = off; 6 recovered the quiet barge-in "stop"s with no regressions on 205 clips. VAD and endpointing are upstream of it and unaffected. |
| `SIP_ANSWER_AFTER_SECS` | **5** s (≥ 0) | How long a call rings before the brain answers it: a couple of rings, time it builds the call's pipeline and has the greeting worded in, so the greeting plays as soon as the caller is connected. 0 answers as soon as the pipeline is up. Needs the gateway's auto_answer off (the brain answers). |
| `TEAPORT_CALL_PROMPT_SECS` | **12** s (≥ 0) | When a call comes in during a conversation (a Talk session, or the room mic awake), the agent asks the people in it first ("Someone's calling me — … Should I step away for a moment?"). This is how long they have to answer, from the end of the question; the caller hears it ring meanwhile. |
| `TEAPORT_CALL_PROMPT_DEFAULT` | `take` | What happens when nobody answers that question in time: take (the conversation goes on hold and the call is answered) or ring (the call is never answered and rings until the caller gives up). One of `take`, `ring`. |
| `TEAPORT_SIP_SOCKET` | `/run/teaport/teaport-sip.sock` (`""` = off) | Gateway-to-brain Unix socket. teaport-brain looks for it every 2 s and connects whenever the gateway is up; while there is none (telephony off) the SIP front-end does nothing. Empty turns the SIP front-end off. *Set by the installer.* |

## Diagnostics

These settings trace and record turns for the journal and name the engine's log file.

In `/etc/teaport/brain.env`. All off by default; log-only.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_ENDPOINT_DEBUG` | **off** | VAD state transitions, Smart Turn verdicts and the turn-commit / first-audio timing bubbles in the journal. |
| `TEAPORT_ENDPOINT_DIST_EVERY` | **500** frames (≥ 1) | The per-frame confidence census interval (500 frames ≈ 16 s). |
| `LEDGER_TRACE` | **off** | Trace every frame the transcript ledger sees. Only 1 turns it on: true/yes/on are not flag words here, and read as off (with a warning). Write 1, or 0 for off. |
| `TEAPORT_TRACE` | **off** | Keep the [CAP] caption-pipeline and [WTS] word-timestamp traces in the journal. |
| `TEAPORT_AUDIO_DUMP` | — | A directory; every phone call and Talk session writes the caller PCM the brain received plus a sidecar of bot-playout offsets (Talk sessions as caller-talk-<UTC time>). Empty = off. |
| `TEAPORT_AUDIO_DUMP_MAX_SECS` | **600** s (≥ 1) | Caps the recording. |
| `TEAPORT_CAPTION_USER_HOLD_S` | **1.2** s (≥ 0) | Gap after the user's last interim before assistant partials can render again; prevents the doubled assistant bubble in the Talk UI. |
| `ENGINE_LOG` | `~/teaport-engine.log` | The engine's serve log, where the tools read decode ms/step. *Set by the installer.* |

## Installer-owned

The installer writes these values on each run.

In `/etc/teaport/brain.env`. Written by install.sh. Shown read-only.

| Setting | Default | Description |
|---|---|---|
| `BRAIN_PORT` | **7861** port (1–65535) | Passed as --port by the unit, and read by the local audio bridge to find /talk. Changing it means re-rendering the plugin config, bridge.env and Caddy. *Set by the installer.* |
| `GATEWAY_PORT` | **7861** port (1–65535) | Code fallback for the listen port when --port is not given. On an installed box BRAIN_PORT is the effective setting. *Set by the installer.* |
| `GATEWAY_TOKEN` | — | Shared secret for /talk. Empty means anyone who can reach the port gets a full agent session and can replace a client's session by claiming its id; the brain warns loudly at startup. Mirrored into the plugin config by install.sh. Never change one side alone. *Set by the installer.* |
| `MALLOC_ARENA_MAX` | **2** (≥ 1) | glibc arena cap; part of the memory budget. *Set by the installer.* |
| `HF_HUB_OFFLINE` | `1` | Never fetch from the Hub on the appliance. Also set by the package at import. One of `1`. *Set by the installer.* |

## Speech engine service

These settings configure the engine service itself: its port, its startup flags, and its memory reserve.

In `/etc/teaport/engine.env`.

| Setting | Default | Description |
|---|---|---|
| `KOKORO_RESERVE_FPT` | — | The engine memory reserve. 6 with any agent resident (a NemoClaw sandbox or a host OpenClaw gateway share the RAM), 12 for a voice-only device. One of `6`, `12`. The engine's own default (50) OOMs an 8 GB box. The installer derives this from whether an agent is present and rewrites it on every run, so it is not editable here: re-run install.sh to change what is resident. *Set by the installer.* |
| `ENGINE_PORT` | **8000** port (1–65535) | --serve. TEAPORT_URL and ENGINE_TTS_URL in brain.env must follow it. *Set by the installer.* |
| `ENGINE_DELAY` | **480** | --delay to the engine binary. *Set by the installer.* |
| `TTS_CTX` | **192** | --tts-ctx to the engine binary. *Set by the installer.* |
| `VOX_REQUIRE_DICT_G2P` | `1` | Refuse to serve if the en-us G2P dict failed to load, rather than degrade silently to espeak-only prosody. One of `1`. *Set by the installer.* |

## SIP gateway

These settings are the gateway's own conf: registration, the echo canceller, and the addresses it accepts SIP requests from.

In `~/.config/teaport/teaport-sip.conf`. Written by `teaport sip configure`; see "Turn on telephony" in the phone calls guide for turning the line on. A re-run sets the trunk's keys (id_uri, registrar_uri, username, password, realm) and the units' (sip_port, uds_path, register, auto_answer) and keeps every other line of the conf as it was, so a setting made by hand, or by `teaport sip aec`, survives it; `teaport sip configure --fresh` keeps nothing and writes the template.

| Setting | Default | Description |
|---|---|---|
| `registrar_uri` | — | The registrar / SBC, as sip:host. |
| `id_uri` | — | sip:user@domain. |
| `username` | — | Auth username. |
| `password` | — | Auth password. Lives in the mode-600 conf itself. |
| `register` | **on** | *Set by the installer.* |
| `realm` | `*` | *Set by the installer.* |
| `reg_timeout` | **300** s (≥ 30) | Registration refresh interval. |
| `bind_addr` | `0.0.0.0` | *Set by the installer.* |
| `sip_port` | — | The wizard test-registers on a throwaway port, never :5060 while a gateway runs. *Set by the installer.* |
| `uds_path` | — | Must match the brain's TEAPORT_SIP_SOCKET. *Set by the installer.* |
| `auto_answer` | **on** | Whether the gateway answers inbound calls itself. `teaport sip configure` writes false: the brain answers, so the caller hears a couple of rings first (SIP_ANSWER_AFTER_SECS), and a call the people in a live conversation say not to pick up rings on. true (the gateway's own default): answered at once, with neither. |
| `aec` | **on** | The gateway's echo canceller. Keep it on; the SIP front-end's makeup gain assumes it. |
| `aec_tail_ms` | **256** ms (≥ 0) | Echo canceller tail. |
| `log_level` | **3** (0–6) | pjsua log level. |
| `app_log_level` | **3** (0–6) | Gateway app log level. |
| `sip_source_filter` | `log` | Which addresses can send the gateway SIP requests (gateway v0.6.6 and later): its allowlist is every address registrar_uri resolves to, all its DNS SRV targets included, plus sip_source_allow. enforce drops a request from any other address, with no answer and no state (responses to our REGISTERs are never filtered); log accepts it as before and logs an `[acl] ... NOT on the allowlist` line for each new address and method; off is no filter and no lines. Any case; another value is a config error, and the gateway exits 78 rather than start. The gateway logs the mode it runs on at startup (`[acl] source filter ...`); `teaport sip status` and doctor show the conf's mode as confirmed only when that line (this run) agrees and the gateway's `--check-config` passes the conf; a gateway older than v0.6.6 ignores the key, and they say so. One of `enforce`, `log`, `off`. Switch to enforce only once the trunk's call-delivery addresses are confirmed to be its registrar's or on sip_source_allow: run in log first and read the `[acl]` lines over a few real calls, since an address the filter does not know drops the call with nothing sent and nothing seen by the brain. It checks the packet's source address, which can be forged: it is not authentication. With register = false it needs at least one sip_source_allow entry. *Set by hand: `teaport sip configure` never writes it, and keeps it.* |
| `sip_source_allow` | — | More addresses the gateway takes SIP requests from, besides its registrar's (sip_source_filter): IPv4 or IPv6 addresses or CIDR networks, comma- or space-separated, and the key can be repeated, each line adding to the list. No names (nothing resolves them), no /0 (that is sip_source_filter = off), no address bits beyond the prefix: each is a config error. Unset: the registrar's addresses alone. Repeated lines add up, where every other key's last line wins: the configuration page shows the last line only, `teaport sip status` counts them all. *Set by hand: `teaport sip configure` never writes it, and keeps it.* |

## Discord bridge

These settings identify the guild and user the bridge follows and configure its voice and its path to the brain.

In `/etc/teaport/bridge.env`.

| Setting | Default | Description |
|---|---|---|
| `BRIDGE_GUILD_ID` | **required** | The Discord guild. The unit stays inert until this and BRIDGE_FOLLOW_USER_ID are set. |
| `BRIDGE_FOLLOW_USER_ID` | **required** | The user whose voice channel the bot follows. |
| `BRIDGE_JOIN_CHANNEL_ID` | — | Force a voice channel instead of following the user. |
| `BRAIN_URL` | `ws://127.0.0.1:7861/talk` | Derived from BRAIN_PORT. *Set by the installer.* |
| `TEAPORT_VOICE` | — | TTS voice for bridge sessions; unset = the brain's TTS_VOICE. |
| `BRIDGE_PRIME_MS` | **40** ms (≥ 0) | Downlink prime. |
| `DISCORD_BOT_TOKEN_FILE` | `~/.config/teaport/discord_bot_token` | Where the bot token is read from. *Set by the installer.* |
| `DISCORD_BOT_TOKEN` | file `~/.config/teaport/discord_bot_token` | The bot token. Prefer the file; the env var, if set, wins over it. Read once at login: a new token needs the bridge restarted. |

## Local audio bridge

These settings choose the sound card, tune its gain and echo handling, and set the wake words and session timers.

In `/etc/teaport/local-audio.env`. Talk to the agent through a sound card on the box (a USB mic array with a speaker on its jack). Opt-in: the unit stays inert until /etc/teaport/local-audio.env exists (`TEAPORT_ENABLE_LOCAL_AUDIO=1 ./install.sh`). It is a /talk client, and the brain holds one conversation at a time (see "One conversation at a time" in the architecture reference): it dials the brain when it starts, and after a session ends it redials only when someone speaks near it (LOCAL_AUDIO_WAKE_DB) — or, with wake words (LOCAL_AUDIO_WAKE_WORDS), it holds a session in which nothing the room says goes anywhere until a wake word is said. A dashboard (or other) Talk session takes the box only from a sleeping mic; while any other conversation is live the brain refuses the bridge (out loud if someone in the room asked to talk), and the bridge backs off while that conversation lasts (LOCAL_AUDIO_BACKOFF_SECS).

| Setting | Default | Description |
|---|---|---|
| `LOCAL_AUDIO_DEVICE` | `hw:CARD=Array,DEV=0` | The ALSA device for both the mic and the speaker. It must do 16 kHz stereo S16_LE both ways. The default names a ReSpeaker XVF3800 by its card id; `arecord -l` lists the others. |
| `LOCAL_AUDIO_CAPTURE_CHANNEL` | **0** (0–1) | Which capture channel is the mic. On the XVF3800, 0 is the echo-cancelled conversation beam and 1 the ASR-tuned beam. |
| `LOCAL_AUDIO_ECHO_CONTROL` | **on** | Let the bridge tune the XVF3800 so it hears you while the bot is talking: the chip's automatic gain is replaced by a fixed gain, and its echo suppressor stays off unless the bot's echo comes too close to your voice. Off: the bridge leaves the chip alone (one it already tuned keeps that until it is replugged). |
| `LOCAL_AUDIO_MIC_GAIN_DB` | **16.0** dB (0.0–36.0) | The fixed gain the XVF3800 applies to the mic in place of its automatic gain (which also boosted the bot's echo). Raise it if the speech engine misses quiet speech, lower it if loud speech clips. |
| `LOCAL_AUDIO_ECHO_MARGIN_DB` | **15.0** dB (0.0–40.0) | How far your voice must stay above the bot's echo, as the bridge measures them at the mic, for the chip's echo suppressor to stay off. Closer than that, the bot's words start leaking in as yours and the suppressor comes on (it also muffles you while the bot talks). |
| `LOCAL_AUDIO_WAKE_DB` | **-40.0** dB (-70.0–0.0) | Without wake words: how loud (dBFS RMS on the capture channel) a voice in the room must be to reconnect after a session ended. The bridge dials the brain at start, then waits for ~0.26 s of sound above this before dialling again, so it never dials into a busy box on a timer. Measured on the appliance with the XVF3800's automatic gain off at the default LOCAL_AUDIO_MIC_GAIN_DB (16 dB): the quiet room about -50 dBFS (the quietest fifth of 100 ms blocks), a voice 3-4 m from the box about -28 dBFS (its median; -21 at its loudest tenth). Raise it if noise reconnects, lower it if speech does not. |
| `LOCAL_AUDIO_WAKE_WORDS` | — | Wake words for the box's own mic, comma-separated, in any language the speech engine hears (for example `hey teaport, tea port, привет чайник`). Set, the box's speech recognition hears the room but every word is dropped, inside the brain's STT, before it can reach the model, the agent, a caption or a log — until a phrase from this list is said. Then what was said after it is the first turn ("hey teaport, what's the weather"), and the conversation needs no wake word until it sleeps again (LOCAL_AUDIO_KEEPALIVE_SECS, or "go to sleep"). Matching ignores case, punctuation and spacing but is otherwise exact: list the spellings you want ("teaport, tea port"); "teapot" wakes it only if listed. Empty: no wake words, the voice wake (LOCAL_AUDIO_WAKE_DB). SIP calls and dashboard/browser Talk sessions never need them. Any value at all means wake mode, and it fails closed: if the brain or its speech recognition cannot be reached the bridge stays deaf and retries; it never falls back to listening without the wake words. While asleep the engine's speech recognition runs on the room (the GPU stays busy, one STT session held); a phone call takes it over (the bridge stays off until the call ends), but a call that lands DURING a conversation at the box still hears the busy line (the take-the-call prompt asks the room in this case). |
| `LOCAL_AUDIO_KEEPALIVE_SECS` | **45.0** s (5.0–3600.0) | With wake words: the conversation sleeps once nobody (you or the agent) has spoken for this long. Its session ends — the pipeline, the speech engine and the mic all go — and the box waits for a wake word again; the conversation itself is kept for LOCAL_AUDIO_CONVERSATION_SECS. Without wake words the session stays up, but after this long with nobody speaking (and until the first time someone does) it counts as a sleeping mic: a Talk client that connects takes the box from it quietly instead of being told the agent is busy. |
| `LOCAL_AUDIO_CONVERSATION_SECS` | **7200.0** s (0.0–604800.0) | With wake words: one conversation at the box lasts until it has been asleep this long. A wake within it continues where it stopped ("what about tomorrow?" three minutes later still knows the question) and is not greeted; after it, a new conversation starts with a greeting. The brain keeps the last conversation's messages in memory (at most 40 messages / 24k characters), for the box's own mic only; a restart of teaport-brain forgets it. |
| `LOCAL_AUDIO_BACKOFF_SECS` | **60.0** s (0.0–3600.0) | When another client (the dashboard, grokani, a browser) has the box -- it took the session from the sleeping bridge, or the brain refused the bridge because that client's conversation is live -- the box is theirs: the bridge stops listening for a voice until the brain has had no live session for this long, so the room (or the dashboard user's own voice) does not keep asking. A bridge that starts (or restarts) while such a session is live does the same instead of dialling. An unused Talk session ends on the brain's ~5 min idle timeout, which bounds the wait. After it, the bridge waits for a voice as after any session; 0 turns the back-off off (with wake words a refused bridge then retries on a doubling wait, never at once). |
| `LOCAL_AUDIO_URL` | `ws://127.0.0.1:<BRAIN_PORT>/talk` | The brain's /talk WebSocket. GATEWAY_TOKEN, read from brain.env, is appended as ?token= when set. |
| `LOCAL_AUDIO_FACE_SOCK` | `/run/oled-avatar/face.sock` | The OLED avatar daemon's socket (teaport-oled-avatar, `oled_face.py --serve`). The bridge sends it listening/thinking/speaking, the jaw opening from the loudness of the audio as it plays, and the reply text for its mood. No daemon there means no face; nothing else changes. |
| `LOCAL_AUDIO_FACE_ADVANCE_MS` | **50** ms (0–300) | How early the avatar's mouth is told about the audio it is about to play, to cover its own drawing delay. Raise it if the mouth lags the voice, lower it if it leads. |
| `LOCAL_AUDIO_FACE_TYPIST` | `hands` | Who types on the OLED face while the agent works on a question: hands, or claws. Takes effect the next time it starts typing, on an avatar that has both; an older one types its own way. One of `hands`, `claws`. |

## Settings that constrain each other

The settings in this section depend on one another; each item names the others that must change with it.

- TEAPORT_OPENCLAW_AGENT_CONSULT_TIMEOUT must exceed the ack + native consult timeouts, or pipecat drops the late answer.
- First clause ≤ clause cap ≤ hard max.
- Changing ENGINE_PORT means rewriting TEAPORT_URL and ENGINE_TTS_URL in brain.env.
- Changing BRAIN_PORT means rewriting BRAIN_URL in bridge.env, the plugin config and Caddy.
- GATEWAY_TOKEN is mirrored into the plugin config by install.sh; change both or neither.

<!-- end generated -->
