# Configuration

Non-secret configuration lives in `/etc/teaport/{engine,brain}.env`. Per-user
secrets live in `~/.config/teaport/` — `llm_key` · `openclaw_token` ·
`persona.md` · `discord_bot_token` · `teaport-sip.conf`. The installer does not
write secrets into units or images, and neither should you: a secret set as an
env var shadows its file, and the tables below say which file each one belongs
in.

Boolean settings accept `0`/`false`/`no`/`off` and `1`/`true`/`yes`/`on`. An
empty value means *unset* — the default applies. A flag that is off says so in
the journal at startup. (`LEDGER_TRACE` is the one exception; see its row.)

There is a page for all of this: **`http://<box>:7861/config`** on the brain,
opened with the same `GATEWAY_TOKEN` the Talk plugin uses (`?token=…` once, then
it is remembered by the browser). It edits the env files and the secret files,
never shows a secret back, and says which service is still running on old
values with a button to restart it. Everything on it is also in the tables
below, for editing by hand over ssh.

The tables below are generated from `brain/teaport_brain/config_schema.toml`,
which is also what the config UI reads — a setting that is not in the schema is
not a setting. Each knob is explained at length where it is read; the **source**
column of the schema names the file and line.

<!-- generated from brain/teaport_brain/config_schema.toml — edit that, then: python -m teaport_brain.config_schema -->

## Where settings live

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

In `/etc/teaport/brain.env`.

| Setting | Default | Description |
|---|---|---|
| `LLM_BASE_URL` | **required** | Your OpenAI-compatible endpoint: https://api.groq.com/openai/v1, https://api.cerebras.ai/v1, https://openrouter.ai/api/v1, or http://127.0.0.1:8182/v1 for a local server. A value set here survives a re-run of install.sh unless that run is given one explicitly (the prompt, or TEAPORT_LLM_BASE_URL). |
| `LLM_API_KEY` | file `~/.config/teaport/llm_key` | The key for that endpoint. A local server that ignores auth still needs a placeholder such as sk-local. Prefer the file; the env var, if set, wins over it. |
| `LLM_MODEL` | `gpt-oss-120b` | The served model name. A value set here survives a re-run of install.sh unless that run is given one explicitly (the prompt, or TEAPORT_LLM_MODEL). |
| `LLM_REASONING_EFFORT` | `low` | Reasoning effort for models that support it. Set "" to disable for models that do not. One of `low`, `medium`, `high`, `""`. low cuts gpt-oss's hidden chain-of-thought ~10x before the first spoken token. Also sets the LLM_MAX_TOKENS default. |
| `LLM_MAX_TOKENS` | by `LLM_REASONING_EFFORT`: 1024 at `low`, 3072 at `medium`, 8192 at `high`, 4096 at `""` | Completion cap. A credit-metered gateway reserves against the model's ceiling and refuses the request when the balance cannot cover it, however short the answer. Reasoning tokens bill as completion tokens. 0 = no cap, correct for a local or un-metered endpoint. |
| `LLM_TIMEOUT_SECS` | **20** s (≥ 1) | How long one completion attempt may take before it is abandoned. One retry follows, so the worst case before the failure is spoken is roughly twice this. Raise it for a slow local llama.cpp box. Without it the OpenAI SDK waits 600 s with no error, no log line and no audio. |
| `LLM_EXTRA_BODY` | — | Optional JSON object merged into the request extra_body. OpenRouter routing example: {"provider":{"order":["Groq"],"allow_fallbacks":true}}. |
| `TEAPORT_LLM_TEXT_GUARD` | **on** | Fold degenerate unicode out of the model's replies and cut a runaway ellipsis collapse. |
| `TEAPORT_LLM_GUARD_RECOVERY` | `""` | The one line spoken when that guard trips. Empty = the built-in English line; override it for a non-English deployment. |
| `TEAPORT_LLM_ERROR_SPEECH` | **on** | Say a short line out loud when the model call fails, instead of leaving the room silent. |
| `TEAPORT_LLM_ERROR_SPEECH_DEBOUNCE` | **30** s (≥ 0) | Seconds between spoken failure notices, so a failing endpoint is reported once per window rather than once per turn. |
| `TEAPORT_RAW_LLM_CAPTURE` | **on** | Log a completion verbatim when it degenerates. Healthy turns log nothing. |

## Speech engine connection

In `/etc/teaport/brain.env`.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_URL` | `ws://127.0.0.1:8000/v1/realtime` | The engine realtime speech-to-text WebSocket. The installer derives it from ENGINE_PORT. A wrong value is the bot saying "speech recognition isn't available". *Set by the installer.* |
| `ENGINE_TTS_URL` | `ws://127.0.0.1:8000/v1/tts` | The engine text-to-speech base; the stream URL derives from it. *Set by the installer.* |
| `ENGINE_TTS_STREAM_URL` | `<ENGINE_TTS_URL base>/v1/audio/speech/stream` | Only set to override the derivation from ENGINE_TTS_URL. *Set by the installer.* |
| `TTS_VOICE` | `af_heart` | Kokoro voice id. A per-session choice from OpenClaw wins over this. |
| `TTS_LANGUAGE` | — | Kokoro language. A per-session choice from OpenClaw wins over this; unset = the engine default. |
| `TTS_REMOTE_TIMEOUT` | **20** s (≥ 1) | Per-request receive timeout on the TTS stream. A clause synthesizes in ~0.2–0.5 s; 20 s covers a stuck engine without hanging a reply. |
| `TTS_CONNECT_RETRIES` | **3** (≥ 1) | Connect attempts while the engine drains synth a barge-in abandoned (~1.5 s on an Orin Nano). Measured: 0/3 clauses survive with one attempt, 3/3 with three. The code clamps to at least 1; 0 or 1 both mean a single attempt. |
| `TTS_CONNECT_BACKOFF` | **0.6** s (≥ 0) | Gap between those connect attempts. |
| `TTS_STOP_FRAME_TIMEOUT_S` | **15** s (≥ 1) | Pipecat's stop-frame timeout. Its 3 s default trips in normal CPU-backend operation; 15 s covers the worst cap-sized chunk at CPU RTF ~0.6. |
| `TEAPORT_STT_BACKEND` | `""` | Experimental: streaming points the STT service at vLLM serving Voxtral Realtime. The incumbent engine is the default. One of `""`, `streaming`. |
| `TEAPORT_STT_URL` | `ws://127.0.0.1:8100/v1/realtime` | Only with TEAPORT_STT_BACKEND=streaming. |
| `TEAPORT_STT_MODEL` | `voxtral-realtime` | Only with TEAPORT_STT_BACKEND=streaming. |
| `TEAPORT_STREAM_TAIL_SECS` | **0.7** s (≥ 0) | Only with the streaming backend. |

## TTS clause chunking

In `/etc/teaport/brain.env`. First-audio latency versus seam quality. Each is explained at length in engine_tts.py.

| Setting | Default | Description |
|---|---|---|
| `TTS_SENTENCE_SOFT_MAX` | **80** chars (≥ 0) | A sentence longer than this is split at clause boundaries before synthesis; first audio scales with the chunk's length, so one long sentence is the whole first-audio wait (a 236-char sentence began 4.6 s after the model finished it, on the 2026-09 engine and pre-#14 brain). 0 disables the split. |
| `TTS_FIRST_CLAUSE_CHARS` | **32** chars (≥ 8) | Size of the first chunk — the one the caller waits on. |
| `TTS_CLAUSE_GROWTH` | **1.5** (1.0–1.67) | Each chunk may grow this many times the previous. Must stay below 1/RTF (~1.67 at the measured CPU RTF 0.6) or a chunk's synth outruns the previous chunk's playout and playback stalls at the seam. |
| `TTS_CLAUSE_CAP` | **200** chars (≥ 8) | The largest ramped chunk. |
| `TTS_CLAUSE_HARD_MAX` | **350** chars (8–450) | Last-resort mid-sentence word break so a run-on cannot overflow the engine's ~512-token utterance limit and crash the synth. |
| `TTS_SEAM_KEEP_LEAD` | **0.05** s (≥ 0) | Near-silence kept before a chunk's first sound. |
| `TTS_SEAM_KEEP_TRAIL` | **0.25** s (≥ 0) | Near-silence kept after a chunk's last sound. With the lead this lands a seam near a sentence's natural ~320 ms comma pause instead of the ~793 ms a naive concat gives. |
| `TTS_STREAM_AUDIO` | **on** | Play each engine audio chunk as it arrives (first audio sooner). Off buffers each sentence to its end, as before #14, and tells the engine to skip its eager prefix. Turn it off on a CPU-only engine or wherever synthesis runs slower than playout, where the early first block plays out before the rest of the sentence arrives and leaves a gap mid-sentence; the journal logs each such gap as "TTS stream underrun". |
| `TTS_USER_SPEECH_HOLD_MAX_S` | **3.0** s (≥ 0) | While VAD says the user is speaking, the next clause is held back to free the GPU for barge-in transcription. This caps the hold so sustained noise cannot stall the reply. |
| `TTS_HOLD_ON_USER_SPEECH` | **on** | Hold the next clause while VAD hears the user, so STT gets the GPU (capped by TTS_USER_SPEECH_HOLD_MAX_S). |
| `TTS_PACING` | `greedy` | greedy submits each clause as soon as the previous one is synthesized. lead holds it until less than TTS_LEAD_S of synthesized audio is still unplayed, so the GPU idles for STT through most of the playout and a barge-in discards less synthesis. One of `greedy`, `lead`. Measured 2026-10-04 on the appliance and an Orin NX (80 barge-ins each): no change in barge-in latency, since synthesis now runs ~30x real time; an interrupted reply discards ~3-5 s of synthesis instead of ~15-45 s. |
| `TTS_LEAD_S` | `auto` | With TTS_PACING=lead: seconds of unplayed audio to keep queued, or auto for max(1.5 s, 2x the next clause's synth time learned this session). |
| `TTS_EAGER` | `always` | Which clauses ask the engine for its eager prefix (an earlier first block, ~10% more synthesis): always, each reply's first clause, any clause submitted with less audio queued ahead of it than its own synth time (low_lead), or never. Ignored with TTS_STREAM_AUDIO off, which never asks. One of `always`, `first`, `low_lead`, `never`. |
| `TTS_CAPTION_LEAD_SECS` | **0.2** s (≥ 0) | Caption lead shared by the TTS word timestamps and the heard-word ledger. One knob so the two cannot drift. |

## Turn-taking

In `/etc/teaport/brain.env`. Each is explained at length where it is read, in endpointing.py.

| Setting | Default | Description |
|---|---|---|
| `ENDPOINT_STOP_SECS` | **0.5** s (≥ 0.1) | Silence before the VAD reports the caller stopped. The dominant fixed latency on every turn; lower is snappier and cuts more mid-sentence pauses. The transcript segment survives a pause Smart Turn calls unfinished (the STT commits on its verdict, not on this stop), so this can come down toward pipecat's 0.2 where the verdicts are trustworthy (wideband audio); on telephony they are not, and the floor still does the work. |
| `SMARTTURN_STOP_SECS` | **1.0** s (≥ 0) | How long a Smart Turn "not done" verdict holds the turn open, counted from the VAD stop. The transcript segment is held open for the same wait, so a caller who resumes keeps one utterance. With TEAPORT_SPECULATIVE_REPLY on, the reply is already being generated on the settled words while this runs, so it can be raised (more patience for mid-sentence pauses) by up to the LLM's own latency at no cost to the turns that fall through. |
| `TEAPORT_SPECULATIVE_REPLY` | **off** | Ask the LLM as soon as the caller's words stop changing on a turn Smart Turn has not concluded on (an INCOMPLETE verdict waiting out SMARTTURN_STOP_SECS) — the settled interim transcript, which on the current engine equals the final — and use that reply if the turn then commits with exactly those words and nothing else touched the context. Off: the request waits for the commit. On: those turns answer up to the LLM's latency sooner (~0.5 s measured), and a turn the caller resumes has spent one wasted request — every outcome logs a [SPEC] line with the running hit/miss tally. |
| `TEAPORT_SPECULATE_SETTLE_MS` | **160** ms (≥ 0) | How long the interim transcript must stop changing, under a Smart Turn "not done" with the caller quiet, before TEAPORT_SPECULATIVE_REPLY asks the LLM on it. Two of the engine's 80 ms decoder tokens: one empty token between words cannot trip it. Lower starts the request sooner and opens more on words that then grow ([SPEC] miss reason=superseded); higher wastes fewer and gains less. |
| `SMARTTURN_COMPLETE_THRESHOLD` | **0.5** (0–1) | The end-of-turn probability that counts as done. Near-inert on telephony audio. |
| `VAD_CONFIDENCE` | **0.7** (0–1) | Silero's speech-probability gate. |
| `VAD_MIN_VOLUME` | **0.6** (0–1) | Silero's volume gate. |
| `VAD_SAMPLE_RATE` | `16000` | 8000 runs Silero on the true narrowband signal when the trunk is G.711; 16000 is the gateway's upsample. One of `16000`, `8000`. |
| `TEAPORT_INTERRUPT_MIN_WORDS` | **2** (≥ 1) | Transcribed words needed to interrupt the bot. 1 makes every word a barge-in, backchannels included. On SIP, while the barge-in pause has the bot paused, an utterance made only of TEAPORT_STOP_WORDS interrupts at any length, and once the paused reply would have finished anyway one word is enough. |
| `SIP_ENDPOINT_STOP_SECS` | — | ENDPOINT_STOP_SECS for phone calls (the SIP front-end) only. Empty: the shared ENDPOINT_STOP_SECS. One brain process serves both, and they were tuned apart. |
| `SIP_SMARTTURN_STOP_SECS` | — | SMARTTURN_STOP_SECS for phone calls (the SIP front-end) only. Empty: the shared SMARTTURN_STOP_SECS. One brain process serves both, and they were tuned apart. |
| `SIP_SMARTTURN_COMPLETE_THRESHOLD` | — | SMARTTURN_COMPLETE_THRESHOLD for phone calls (the SIP front-end) only. Empty: the shared SMARTTURN_COMPLETE_THRESHOLD. One brain process serves both, and they were tuned apart. |
| `SIP_INTERRUPT_MIN_WORDS` | — | TEAPORT_INTERRUPT_MIN_WORDS for phone calls (the SIP front-end) only. Empty: the shared TEAPORT_INTERRUPT_MIN_WORDS. One brain process serves both, and they were tuned apart. |
| `TALK_ENDPOINT_STOP_SECS` | — | ENDPOINT_STOP_SECS for Talk sessions (/talk: the app, the dashboard, Discord, the box's mic) only. Empty: the shared ENDPOINT_STOP_SECS. One brain process serves both, and they were tuned apart. Blind A/B 2026-09-18 picked 0.2 / 0.6 for Talk against the phone's 0.5 / 1.0. |
| `TALK_SMARTTURN_STOP_SECS` | — | SMARTTURN_STOP_SECS for Talk sessions (/talk: the app, the dashboard, Discord, the box's mic) only. Empty: the shared SMARTTURN_STOP_SECS. One brain process serves both, and they were tuned apart. Blind A/B 2026-09-18 picked 0.2 / 0.6 for Talk against the phone's 0.5 / 1.0. |
| `TALK_SMARTTURN_COMPLETE_THRESHOLD` | — | SMARTTURN_COMPLETE_THRESHOLD for Talk sessions (/talk: the app, the dashboard, Discord, the box's mic) only. Empty: the shared SMARTTURN_COMPLETE_THRESHOLD. One brain process serves both, and they were tuned apart. |
| `TALK_INTERRUPT_MIN_WORDS` | — | TEAPORT_INTERRUPT_MIN_WORDS for Talk sessions (/talk: the app, the dashboard, Discord, the box's mic) only. Empty: the shared TEAPORT_INTERRUPT_MIN_WORDS. One brain process serves both, and they were tuned apart. |
| `TEAPORT_REPLY_HOLD` | — | Retired, never shipped in a release: the reply hold is set per front-end by TEAPORT_REPLY_HOLD_SIP and TEAPORT_REPLY_HOLD_TALK. If it is still set, the brain ignores it and logs a warning saying so. |
| `TEAPORT_REPLY_HOLD_SIP` | **on** | Phone calls: before a reply to the caller's turn starts playing, check the line. If they have started talking again since the turn was committed, hold the reply; if their new words arrive, drop it and answer the whole turn as one message; if none come (a cough), play it TEAPORT_REPLY_HOLD_RELEASE_S after they go quiet. A caller who stays quiet is never delayed. |
| `TEAPORT_REPLY_HOLD_TALK` | **off** | The same reply hold for the OpenClaw Talk path. Off until a Talk session has been measured with it: the only measurement so far is a telephony call. |
| `TEAPORT_REPLY_HOLD_RELEASE_S` | **1.0** s (≥ 0) | How long a held reply waits for the caller's words once the line is quiet -- the VAD's stop and the fast onset test's offset, whichever is later -- before it plays anyway. So it is about ENDPOINT_STOP_SECS more than this after the speech ends. |
| `TEAPORT_REPLY_HOLD_MAX_S` | **6.0** s (≥ 0) | The longest a reply is ever held, from the start of the hold, whatever the VAD says. Raised to TEAPORT_REPLY_HOLD_RELEASE_S if set lower. |
| `TEAPORT_REPLY_HOLD_ONSET_MAX_S` | **2.5** s (≥ 0) | The cap for a hold only the fast onset test asked for (no VAD start in it). That test has no volume gate, so steady room noise above its threshold would otherwise hold every reply to TEAPORT_REPLY_HOLD_MAX_S. Never more than that, never less than TEAPORT_REPLY_HOLD_RELEASE_S. |
| `TEAPORT_ONSET_CONFIDENCE` | **0.6** (0–1) | Silero confidence that counts toward the fast speech-onset signal the reply hold (and, on SIP, the barge-in pause) uses. Below VAD_CONFIDENCE on purpose, and with no volume gate: it only has to say speech has begun, soon. It changes nothing the VAD decides. |
| `TEAPORT_ONSET_MIN_MS` | **128** ms (≥ 32) | Consecutive speech at TEAPORT_ONSET_CONFIDENCE that makes an onset (32 ms per Silero chunk, rounded up). |
| `TEAPORT_BARGE_PAUSE` | **on** | SIP only: the moment the caller starts talking over the bot (the fast speech-onset test, or the VAD), pause playout. If their words follow, the barge-in cancels the reply as before; if none come within TEAPORT_BARGE_PAUSE_RESUME_S of them stopping, the reply resumes from where it paused. No effect on Talk, whose relay can only clear its client's buffer. |
| `TEAPORT_BARGE_PAUSE_RESUME_S` | **0.8** s (0–8) | The longest a paused reply waits, after the caller goes quiet, for their words before it resumes (sooner once the STT has closed that speech with no turn in it). With the VAD's own stop delay a cough stalls the bot about ENDPOINT_STOP_SECS + this. At most 8. |
| `TEAPORT_BARGE_PAUSE_MAX_S` | **6.0** s (0–8) | The longest one pause lasts before the reply resumes anyway. Capped at 8 s: a write held past pipecat's 10 s deadline loses the call its voice. |
| `TEAPORT_STOP_WORDS` | `stop,wait,hold on,hold,pause,enough,quiet,shh,shush` | Comma-separated words or phrases that end a reply on their own while it is paused for the caller's speech, whatever TEAPORT_INTERRUPT_MIN_WORDS says (with the bot silent there is no echo to garble into one). Only an utterance made entirely of them counts, so "don't stop" does not; any "shh" spelling is shh. "No" is left out on purpose: over the bot it is as often an answer as an objection. |
| `TEAPORT_STRANDED_INTERIM_SECS` | **1.5** s (≥ 0.2) | Commit a segment ourselves after this much interim quiet with no VAD stop, so a missed stop loses a second rather than the turn. |
| `TEAPORT_STT_COMMIT_ON` | — | Retired 2026-10-09: the STT always commits on Smart Turn's verdict (a VAD stop while the bot is speaking still commits at once). The vad-stop choice it offered was for a final sooner on a fallthrough, which the silence hint has since made 0.1-0.2 s, and a final for TEAPORT_SPECULATIVE_REPLY, which now asks on the settled interim. If it is still set, the brain ignores it and logs a warning saying so. |
| `TEAPORT_STT_SILENCE_HINT` | **on** | Tell the engine, on each commit, how much trailing silence the VAD has already established (the stop's silence window plus everything sent since), so it can shorten the fixed ~1 s padding it decodes before answering. The engine takes the larger of this and its own count, clamped to what it has; one without the field's support ignores it. Off: the engine credits only what its own VAD counted, which on a phone line is often nothing. |
| `TEAPORT_STT_SILENCE_HINT_MARGIN_MS` | **100** ms (≥ 0) | Held back from the silence hint, for the tail the VAD's confidence gate drops before the decoder is done with it. Over-claiming by up to 240 ms cost nothing on the engine's measured corpus; 400 ms truncated the last word on 7 of 30 utterances. |
| `HEARD_MODE` | `truncate` | How the context records a reply the caller only partly heard: truncate it to what was heard, or keep it whole with a note. One of `truncate`, `note`. |
| `TEAPORT_SILENT_TURN_SECS` | **12** s (≥ 1) | How long a committed turn may produce no audio before it is reported in the journal. A turn waiting on an agent consult is not counted. |

## Agent consult and follow-ups

In `/etc/teaport/brain.env`. TEAPORT_AGENT says whether the box has a co-resident OpenClaw gateway; every other row here applies only when it does.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_AGENT` | `openclaw` | Which agent, if any, is co-resident. `openclaw`: a gateway (host OpenClaw or a NemoClaw sandbox) provides web_search, web_fetch, search_memory, remember, ask_openclaw, memory recall and the workspace persona. `none`: a voice-only box — those tools are not advertised, recall is not run, and the persona comes from TEAPORT_PERSONA_FILE. Asymmetric on repair: a detected gateway is positive evidence, so the installer writes `openclaw` unconditionally (installing a gateway later and re-running is all it takes to turn the tools on) — but NOT finding one is not evidence of anything, so the installer only seeds `none` on a first voice-only install and an existing value survives a repair. To disable agent tooling on a box that still has a working gateway, use the config page, not a hand edit of this file — a repair that still finds the gateway overwrites a hand edit here (and now logs that it did). One of `openclaw`, `none`. |
| `OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:18789` | The co-resident gateway for shared persona and memory recall. The installer sets it from the gateway port. *Set by the installer.* |
| `OPENCLAW_GATEWAY_TOKEN` | file `~/.config/teaport/openclaw_token` | Bearer token for that gateway. Prefer the file; the env var, if set, wins over it. |
| `OPENCLAW_AGENT_ID` | `main` | The agent consulted. |
| `OPENCLAW_BIN` | — | The openclaw CLI used as the consult fallback. Auto-detected; the host may have none when OpenClaw runs only inside the sandbox. *Set by the installer.* |
| `TEAPORT_AGENT_FIRST` | **off** | Agent-first routing: install the strict router directive and drop the "I'll work on that" ack. |
| `TEAPORT_RECALL_TIMEOUT` | **1.5** s (≥ 0.1) | Memory-recall budget. Never awaited on the frame path, so a generous value adds no turn latency; 1.5 s clears a cold embedder (~1.14 s measured on-box). |
| `TEAPORT_CONSULT_TIMEOUT` | **45** s (≥ 5) | Gateway/CLI consult ceiling on the synchronous path (TEAPORT_AGENT_FIRST, or a client with no follow-up injector). Otherwise the async path, SIP included, uses TEAPORT_ASYNC_CONSULT_TIMEOUT instead. The sandbox-exec shim adds ~14 s of spawn tax before the agent turn even starts. |
| `TEAPORT_NATIVE_CONSULT_ACK_TIMEOUT` | **1.5** s (≥ 0.2) | No ack within this means no native runner: fall back fast instead of burning the full window. A live relay acks within moments; the Discord bridge never acks. |
| `TEAPORT_NATIVE_CONSULT_TIMEOUT` | **45** s (≥ 5) | How long to wait on an acked native consult. Useful consults return in ~15–30 s; past ~45 s the voice wait degrades faster than the answer improves. |
| `TEAPORT_ASK_OPENCLAW_TIMEOUT` | **55** s (≥ 5) | The function-call timeout for ask_openclaw. Must exceed ack + native consult or pipecat abandons the call and drops the late answer. Bounds the sync path only. |
| `TEAPORT_ASYNC_CONSULT_TIMEOUT` | **130** s (≥ 5) | Async consult ceiling, for every lane: the native Talk consult and the gateway/CLI fallback (SIP). The consult runs off the turn; the answer is spoken as an unprompted follow-up when it lands, or a "that took too long" past this. Sized just past the Control UI's own hard 120 s consult wait, after which no Talk result can arrive. |
| `TEAPORT_FOLLOWUP_QUIET_S` | **0.7** s (≥ 0) | How long the conversation must stay quiet before a follow-up window counts; rejects mid-thought pauses and between-turn gaps. |
| `TEAPORT_FOLLOWUP_MAX_WAIT_S` | **60** s (≥ 1) | Ceiling on holding a follow-up for a gap; past this, speak anyway. |
| `TEAPORT_FOLLOWUP_MIN_HEARD` | **0.3** (0–1) | Fraction of a delegated answer the caller must have heard before it counts as delivered; below it, the answer is said again at the next quiet moment. |
| `TEAPORT_THINKING_SOUND` | **on** | The typing bed during a long agent consult. |
| `TEAPORT_THINKING_GRACE_S` | **1.5** s (≥ 0) | Silence before the bed starts. |
| `TEAPORT_THINKING_GAIN` | **0.8** (0–1) | Bed level; 1.0 is the synthesized peak. |
| `TEAPORT_THINKING_MAX_S` | **60** s (≥ 1) | Hard cap on the bed. Keep it above TEAPORT_ASK_OPENCLAW_TIMEOUT. |
| `TEAPORT_PERSONA_FILE` | `~/.config/teaport/persona.md` | The persona file: the source with TEAPORT_AGENT=none, the fallback when the gateway's workspace has nothing. The installer points it at the secrets dir. *Set by the installer.* |
| `OPENCLAW_WORKSPACE` | `~/.openclaw/workspace` | The OpenClaw workspace whose persona files the voice brain shares. *Set by the installer.* |
| `TEAPORT_WORKSPACE_FILES` | `SOUL.md,IDENTITY.md,USER.md,MEMORY.md` | Comma-separated workspace files injected as the shared persona, in order. IDENTITY.md is agent-writable by design. |
| `OPENCLAW_MEMORY_DIR` | `~/.openclaw/workspace/memory` | The daily-note store a voice-saved memory is appended to, shared with the text agent so both recall it. *Set by the installer.* |
| `TEAPORT_THINKING_WAV` | `<package>/assets/typing.wav` | Your own 24 kHz mono wav for the thinking bed; the default is synthesized and cached next to the code. |

## Tools

In `/etc/teaport/brain.env`. One switch per tool the voice model can call (tools.py, THE TOOL CONTRACT). A tool also needs what it works with: the OpenClaw gateway (TEAPORT_AGENT=openclaw) for web, memory and ask_openclaw; the voice for the voice tools; and, for set_volume, restart_session and end_conversation, a client that can do it itself (the local audio bridge announces them; end_conversation only with wake words set); answer_phone_call is offered to Talk sessions and the room mic, never to a call. A tool that is off, or missing what it needs, is not offered to the model and not named in its instructions.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_TOOL_GET_HOST_STATUS` | **on** | The machine's live free memory, CPU load and speech-engine decode speed. |
| `TEAPORT_TOOL_GET_CURRENT_TIME` | **on** | The local date and time. |
| `TEAPORT_TOOL_WEB_SEARCH` | **on** | Web search through the OpenClaw gateway. Needs TEAPORT_AGENT=openclaw. |
| `TEAPORT_TOOL_WEB_FETCH` | **on** | Read a web page through the OpenClaw gateway. Needs TEAPORT_AGENT=openclaw. |
| `TEAPORT_TOOL_SEARCH_MEMORY` | **on** | Recall from the shared long-term memory. Needs TEAPORT_AGENT=openclaw. |
| `TEAPORT_TOOL_REMEMBER` | **on** | Save a fact to the shared long-term memory. Needs TEAPORT_AGENT=openclaw. |
| `TEAPORT_TOOL_ASK_OPENCLAW` | **on** | Hand a request to the full OpenClaw agent. Needs TEAPORT_AGENT=openclaw. Agent-first mode (TEAPORT_AGENT_FIRST) routes every turn through it, so with this off agent-first is ignored (with a warning in the journal). |
| `TEAPORT_TOOL_LIST_VOICES` | **on** | List the speaking voices. |
| `TEAPORT_TOOL_SWITCH_VOICE` | **on** | Change the speaking voice (and with it the reply language). |
| `TEAPORT_TOOL_SET_VOLUME` | **on** | Speaker louder, quieter or to a level. Only offered to a client that can do it itself (the local audio bridge); the level is kept across sessions. |
| `TEAPORT_TOOL_RESTART_SESSION` | **off** | End the conversation and start a fresh one (empty context, a new greeting) when the user asks. A testing aid, off by default: on, a misheard request can wipe a conversation. Only offered to a client that can reconnect itself (the local audio bridge). |
| `TEAPORT_TOOL_END_CONVERSATION` | **on** | Go back to sleep when the user says they are done ("that's all", "goodnight", "go to sleep", in any language): the agent says a short goodbye and the conversation sleeps at once instead of waiting out LOCAL_AUDIO_KEEPALIVE_SECS. Only offered to the local audio bridge, and only when it has wake words (there is nothing to go back to without them). |
| `TEAPORT_TOOL_ANSWER_PHONE_CALL` | **on** | How the user answers "should I step away?" when a phone call comes in during a Talk session or a room conversation, in any language: the model reads the reply and calls it. Off, a conversation is not asked: a call ends a Talk session with a spoken line, and is refused (the caller hears busy) while the room mic is awake. |
| `TEAPORT_TOOL_WIFI_SETUP` | **on** | Wi-Fi setup by voice: the spoken phrase ("set up Wi-Fi", no LLM needed; it starts setup only while the box has no internet) and the tool the model can hand over to. Only for a client at the box (the local audio bridge) and where install.sh laid down teaport-wifi-setup. See Wi-Fi setup. |

## Wi-Fi setup

In `/etc/teaport/wifi-setup.env`. Say "set up Wi-Fi" at the box (the local audio bridge) and it opens a temporary setup network, teaport-ab12 (the end of its Wi-Fi MAC), with a page where a phone picks the box's network and types the password; the box speaks the network, the password and the page's address. No LLM involved, so it works offline; and only offline does the phrase start it by itself — online, the model hears it like any other words and decides, through the wifi_setup tool (Tools), which is also its switch. These two are flash-time settings for the paper insert.

| Setting | Default | Description |
|---|---|---|
| `WIFI_SETUP_PASSWORD` | — | The setup network's WPA2 password, written at flash time so the paper insert can print it. 8 to 63 characters, each an ASCII letter, a digit, a space or one of - _ . @ ! # & * (the symbols the voice can name when it spells the password). Digits are easiest to say and type. Unset (or invalid): fresh random digits each setup, spoken aloud. *Set by the installer.* |
| `WIFI_SETUP_SSID` | — | The setup network's name, for an insert printed before the MAC is known. Unset: teaport- and the last four hex digits of the Wi-Fi MAC. A name made only of hex digits (0-9, a-f) is ignored with a warning, and the MAC-based name used: some phones' QR scanners read such a name as raw bytes. *Set by the installer.* |

## Talk client context notes

In `/etc/teaport/brain.env`. Limits on the notes a Talk client adds to the voice LLM's context through the plugin's `teaport.talk.context` method (client_notes.py). Read only by teaport-brain.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_CONTEXT_MAX_CHARS` | **1000** chars (≥ 1) | Longest context note a Talk client may send, in characters; a longer one is refused, not cut. The teaport-realtime plugin announces the smaller of this and its own 16000. |
| `TEAPORT_CONTEXT_MAX_NOTES` | **20** (≥ 1) | How many context notes stay in the LLM context; past it the oldest are removed. |
| `TEAPORT_CONTEXT_RESPOND_INTERVAL_S` | **15** s (≥ 0) | Minimum time between context notes that ask for a spoken reaction (respond:true); one sent sooner is refused as rate-limited. |

## Busy lamp

In `/etc/teaport/brain.env`. The ReSpeaker XVF3800's LED ring as a busy lamp (BLF) for phone calls (xvf_led.py): breathing red while a call rings, solid red while it is live, then back to what it showed before, normally the firmware's own listening effect (the direction of the voice it hears). It follows the session arbiter's call claim, so it also goes back to normal when the call is torn down with the SIP front-end or the brain restarts or dies (the look to restore is kept in /run/teaport-busy-lamp until it is put back). The ring is driven by USB control transfers beside the audio streams; install.sh lays down a udev rule (/etc/udev/rules.d/60-teaport-xvf3800.rules) giving the device to the teaport-hw group, and puts the run user in it. A box without an XVF3800 does nothing. Read only by teaport-brain.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_BUSY_LAMP` | `auto` | auto lights the XVF3800's ring during a phone call when one is plugged in; off never touches the ring. One of `auto`, `off`. |
| `TEAPORT_BUSY_LAMP_COLOR` | `ff0000` | The lamp's colour, as RRGGBB hex (ff0000 is red). Anything else warns and falls back to red. |

## SIP front-end

In `/etc/teaport/brain.env`. Read by teaport-brain's SIP front-end (phone calls): how a call is answered, and how it asks a live conversation (a Talk session, the room mic awake) to make way for it.

| Setting | Default | Description |
|---|---|---|
| `SIP_HALF_DUPLEX` | **off** | Drop the caller's mic while the bot speaks. On means no barge-in. |
| `SIP_HALF_DUPLEX_TAIL_S` | **0.8** s (≥ 0) | The tail after the bot stops, when half-duplex is on. |
| `SIP_STT_MAKEUP_DB` | **0** dB (0–20) | Makeup gain added to the caller signal the transcriber sees, to recover the quiet speech a caller produces over the bot. 0 = off; 6 recovered the quiet barge-in "stop"s with no regressions on 205 clips. VAD and endpointing are upstream of it and unaffected. |
| `SIP_ANSWER_AFTER_SECS` | **5** s (≥ 0) | How long a call rings before the brain answers it: a couple of rings, time it builds the call's pipeline and has the greeting worded in, so the greeting plays as soon as the caller is connected. 0 answers as soon as the pipeline is up. Needs the gateway's auto_answer off (the brain answers). |
| `TEAPORT_CALL_PROMPT_SECS` | **12** s (≥ 0) | When a call comes in during a conversation (a Talk session, or the room mic awake), the agent asks the people in it first ("Someone's calling me — … Should I step away for a moment?"). This is how long they have to answer, from the end of the question; the caller hears it ring meanwhile. |
| `TEAPORT_CALL_PROMPT_DEFAULT` | `take` | What happens when nobody answers that question in time: take (the conversation goes on hold and the call is answered) or ring (the call is never answered and rings until the caller gives up). One of `take`, `ring`. |
| `TEAPORT_SIP_SOCKET` | `/run/teaport/teaport-sip.sock` | Gateway-to-brain Unix socket. teaport-brain looks for it every 2 s and connects whenever the gateway is up; while there is none (telephony off) the SIP front-end does nothing. Empty turns the SIP front-end off. *Set by the installer.* |

## Diagnostics

In `/etc/teaport/brain.env`. All off by default; log-only.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_ENDPOINT_DEBUG` | **off** | VAD state transitions, Smart Turn verdicts and the turn-commit / first-audio timing bubbles in the journal. |
| `TEAPORT_ENDPOINT_DIST_EVERY` | **500** frames (≥ 1) | The per-frame confidence census interval (500 frames ≈ 16 s). |
| `LEDGER_TRACE` | **off** | Trace every frame the transcript ledger sees. Parsed as == "1", not through env_flag: true/yes/on are silently off. Write 1. |
| `TEAPORT_TRACE` | **off** | Keep the [CAP] caption-pipeline and [WTS] word-timestamp traces in the journal. |
| `TEAPORT_AUDIO_DUMP` | — | A directory; every phone call and Talk session writes the caller PCM the brain received plus a sidecar of bot-playout offsets (Talk sessions as caller-talk-<UTC time>). Empty = off. |
| `TEAPORT_AUDIO_DUMP_MAX_SECS` | **600** s (≥ 1) | Caps the recording. |
| `TEAPORT_CAPTION_USER_HOLD_S` | **1.2** s (≥ 0) | Gap after the user's last interim before assistant partials may render again; prevents the doubled assistant bubble in the Talk UI. |
| `ENGINE_LOG` | `~/teaport-engine.log` | The engine's serve log, where the tools read decode ms/step. *Set by the installer.* |

## Installer-owned

In `/etc/teaport/brain.env`. Written by install.sh. Shown read-only.

| Setting | Default | Description |
|---|---|---|
| `BRAIN_PORT` | **7861** port (1–65535) | Passed as --port by the unit. Changing it means re-rendering the plugin config, bridge.env and Caddy. *Set by the installer.* |
| `GATEWAY_PORT` | **7861** port (1–65535) | Code fallback for the listen port when --port is not given. On an installed box BRAIN_PORT is the effective knob. *Set by the installer.* |
| `GATEWAY_TOKEN` | — | Shared secret for /talk. Empty means anyone who can reach the port gets a full agent session and can replace a client's session by claiming its id; the brain warns loudly at startup. Mirrored into the plugin config by install.sh. Never change one side alone. *Set by the installer.* |
| `MALLOC_ARENA_MAX` | **2** (≥ 1) | glibc arena cap; part of the memory budget. *Set by the installer.* |
| `HF_HUB_OFFLINE` | `1` | Never fetch from the Hub on the appliance. Also set by the package at import. One of `1`. *Set by the installer.* |

## Speech engine service

In `/etc/teaport/engine.env`.

| Setting | Default | Description |
|---|---|---|
| `KOKORO_RESERVE_FPT` | — | The engine memory reserve. 6 with any agent resident (a NemoClaw sandbox or a host OpenClaw gateway share the RAM), 12 for a voice-only device. One of `6`, `12`. The engine's own default (50) OOMs an 8 GB box. The installer derives this from whether an agent is present and rewrites it on every run, so it is not editable here: re-run install.sh to change what is resident. *Set by the installer.* |
| `ENGINE_PORT` | **8000** port (1–65535) | --serve. TEAPORT_URL and ENGINE_TTS_URL in brain.env must follow it. *Set by the installer.* |
| `ENGINE_DELAY` | **240** | --delay to the engine binary. *Set by the installer.* |
| `TTS_CTX` | **192** | --tts-ctx to the engine binary. *Set by the installer.* |
| `VOX_REQUIRE_DICT_G2P` | `1` | Refuse to serve if the en-us G2P dict failed to load, rather than degrade silently to espeak-only prosody. Engine ≤ 1.3 ignores it. One of `1`. *Set by the installer.* |

## SIP gateway

In `~/.config/teaport/teaport-sip.conf`. Written by `teaport sip configure`; see *SIP telephony (opt-in)* below for turning the line on.

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
| `auto_answer` | **on** | Whether the gateway answers inbound calls itself. `teaport sip configure` writes false: the brain answers, so the caller hears a couple of rings first (SIP_ANSWER_AFTER_SECS), and a call the people in a live conversation say not to pick up rings on. true (the gateway's own default, and older confs): answered at once, with neither. |
| `aec` | **on** | The gateway's echo canceller. Keep it on; the SIP front-end's makeup gain assumes it. |
| `aec_tail_ms` | **256** ms (≥ 0) | Echo canceller tail. |
| `log_level` | **3** (0–6) | pjsua log level. |
| `app_log_level` | **3** (0–6) | Gateway app log level. |

## Discord bridge

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

In `/etc/teaport/local-audio.env`. Talk to the agent through a sound card on the box (a USB mic array with a speaker on its jack). Opt-in: the unit stays inert until /etc/teaport/local-audio.env exists (`TEAPORT_ENABLE_LOCAL_AUDIO=1 ./install.sh`). It is a /talk client, and the brain holds one conversation at a time (docs/CONFIG.md, One engine, one conversation): it dials the brain when it starts, and after a session ends it redials only when someone speaks near it (LOCAL_AUDIO_WAKE_DB) — or, with wake words (LOCAL_AUDIO_WAKE_WORDS), it holds a session in which nothing the room says goes anywhere until a wake word is said. A dashboard (or other) Talk session takes the box only from a sleeping mic; while any other conversation is live the brain refuses the bridge (out loud if someone in the room asked to talk), and the bridge backs off while that conversation lasts (LOCAL_AUDIO_BACKOFF_SECS).

| Setting | Default | Description |
|---|---|---|
| `LOCAL_AUDIO_DEVICE` | `hw:CARD=Array,DEV=0` | The ALSA device for both the mic and the speaker. It must do 16 kHz stereo S16_LE both ways. The default names a ReSpeaker XVF3800 by its card id; `arecord -l` lists the others. |
| `LOCAL_AUDIO_CAPTURE_CHANNEL` | **0** (0–1) | Which capture channel is the mic. On the XVF3800, 0 is the echo-cancelled conversation beam and 1 the ASR-tuned beam. |
| `LOCAL_AUDIO_WAKE_DB` | **-40.0** dB (-70.0–0.0) | Without wake words: how loud (dBFS RMS on the capture channel) a voice in the room must be to reconnect after a session ended. The bridge dials the brain at start, then waits for ~0.26 s of sound above this before dialling again, so it never dials into a busy box on a timer. The XVF3800's room measured about -50 dBFS idle. Raise it if noise reconnects, lower it if speech does not. |
| `LOCAL_AUDIO_WAKE_WORDS` | — | Wake words for the box's own mic, comma-separated, in any language the speech engine hears (e.g. `hey teaport, tea port, привет чайник`). Set, the box's speech recognition hears the room but every word is dropped, inside the brain's STT, before it can reach the model, the agent, a caption or a log — until a phrase from this list is said. Then what was said after it is the first turn ("hey teaport, what's the weather"), and the conversation needs no wake word until it sleeps again (LOCAL_AUDIO_KEEPALIVE_SECS, LOCAL_AUDIO_AWAKE_MAX_SECS, or "go to sleep"). Matching ignores case, punctuation and spacing but is otherwise exact: list the spellings you want ("teaport, tea port"); "teapot" wakes it only if listed. Empty: no wake words, the voice wake (LOCAL_AUDIO_WAKE_DB). SIP calls and dashboard/browser Talk sessions never need them. Any value at all means wake mode, and it fails closed: if the brain or its speech recognition cannot be reached the bridge stays deaf and retries; it never falls back to listening without the wake words. While asleep the engine's speech recognition runs on the room (the GPU stays busy, one STT session held); a phone call takes it over (the bridge stays off until the call ends), but a call that lands DURING a conversation at the box still hears the busy line (until issue #58's take-the-call prompt). |
| `LOCAL_AUDIO_KEEPALIVE_SECS` | **45.0** s (5.0–3600.0) | With wake words: the conversation sleeps once nobody (you or the agent) has spoken for this long. Its session ends — the pipeline, the speech engine and the mic all go — and the box waits for a wake word again; the conversation itself is kept for LOCAL_AUDIO_CONVERSATION_SECS. Without wake words the session stays up, but after this long with nobody speaking (and until the first time someone does) it counts as a sleeping mic: a Talk client that connects takes the box from it quietly instead of being told the agent is busy. |
| `LOCAL_AUDIO_AWAKE_MAX_SECS` | **300.0** s (30.0–86400.0) | With wake words: the conversation also sleeps this long after the last wake word, once the agent is not speaking — so a TV or a chat in the room that the agent keeps answering cannot hold it awake for good. Say the wake word again to keep going. |
| `LOCAL_AUDIO_CONVERSATION_SECS` | **7200.0** s (0.0–604800.0) | With wake words: one conversation at the box lasts until it has been asleep this long. A wake within it continues where it stopped ("what about tomorrow?" three minutes later still knows the question) and is not greeted; after it, a new conversation starts with a greeting. The brain keeps the last conversation's messages in memory (at most 40 messages / 24k characters), for the box's own mic only; a restart of teaport-brain forgets it. |
| `LOCAL_AUDIO_BACKOFF_SECS` | **60.0** s (0.0–3600.0) | When another client (the dashboard, grokani, a browser) has the box -- it took the session from the sleeping bridge, or the brain refused the bridge because that client's conversation is live -- the box is theirs: the bridge stops listening for a voice until the brain has had no live session for this long, so the room (or the dashboard user's own voice) does not keep asking. A bridge that starts (or restarts) while such a session is live does the same instead of dialling. An unused Talk session ends on the brain's ~5 min idle timeout, which bounds the wait. After it, the bridge waits for a voice as after any session; 0 turns the back-off off (with wake words a refused bridge then retries on a doubling wait, never at once). |
| `LOCAL_AUDIO_URL` | `ws://127.0.0.1:$BRAIN_PORT/talk` | The brain's /talk WebSocket. GATEWAY_TOKEN, read from brain.env, is appended as ?token= when set. |
| `LOCAL_AUDIO_FACE_SOCK` | `/run/oled-avatar/face.sock` | The OLED avatar daemon's socket (teaport-oled-avatar, `oled_face.py --serve`). The bridge sends it listening/thinking/speaking, the jaw opening from the loudness of the audio as it plays, and the reply text for its mood. No daemon there means no face; nothing else changes. |
| `LOCAL_AUDIO_FACE_ADVANCE_MS` | **50** ms (0–300) | How early the avatar's mouth is told about the audio it is about to play, to cover its own drawing delay. Raise it if the mouth lags the voice, lower it if it leads. |

## Settings that constrain each other

- TEAPORT_ASK_OPENCLAW_TIMEOUT must exceed the ack + native consult timeouts, or pipecat drops the late answer.
- TEAPORT_THINKING_MAX_S should stay above TEAPORT_ASK_OPENCLAW_TIMEOUT so the bed outlasts the consult.
- First clause ≤ clause cap ≤ hard max.
- Changing ENGINE_PORT means rewriting TEAPORT_URL and ENGINE_TTS_URL in brain.env.
- Changing BRAIN_PORT means rewriting BRAIN_URL in bridge.env, the plugin config and Caddy.
- GATEWAY_TOKEN is mirrored into the plugin config by install.sh; change both or neither.

<!-- end generated -->

## SIP telephony (opt-in)

A teaport box is a local voice assistant by default; **SIP telephony is off
until you turn it on**, the same way the Discord bridge is. The installer lays
down the gateway unit, `teaport-sip`, gated on a config file that does not exist
yet, so nothing starts. The calls are answered by `teaport-brain` itself: its SIP
front-end connects to the gateway's socket whenever the gateway is up, and sits
idle when it is not.

Turn it on with the wizard:

```
teaport sip configure          # prompts for registrar/SBC host, domain, user, password
teaport sip configure --host sbc.example.net --domain voip.example.net \
                      --user 100 --password '…' --yes   # headless
```

It test-registers against your trunk (briefly, on a throwaway port — never
`:5060`, so a running gateway is untouched) and only on a `200 OK` writes
`~/.config/teaport/teaport-sip.conf` (mode `600`, holds the SIP password) and
enables the gateway. On a failed register it writes nothing and leaves telephony
off. `teaport sip status` shows the gateway, the brain's SIP front-end, the config,
and this run's registration; `teaport sip disable [--purge]` turns it back off.

Already have a gateway `.conf` — from a box that ran `teaport-sip` by hand, or
carried over from another one? Adopt it instead of retyping it:

```
teaport sip configure --conf ~/my-trunk.conf
```

It goes through the same test-register, then is installed as-is except for the
four keys the units own (`sip_port` → 5060, `uds_path` → the socket the brain
looks for, `register` → true — the gateway's default and the sample's value
are `false` — and `auto_answer` → false: the brain answers, after a couple of
rings; see *One engine, one conversation*). If a hand-launched gateway or SIP front-end is still running, the
command refuses and tells you what to stop: the unit it is about to enable needs
`:5060` and the socket.

Once configured, the line **survives reboots and crashes on its own**: the
gateway is `enabled` and restarts with a backoff that will not hammer the
registrar, and the brain reconnects to it whenever it comes back. The two
restart independently: a brain restart (a brain update, a config change) keeps
the call at the gateway, which hands it to the restarted brain — the caller hears
a short "sorry, I lost you for a moment" — and a gateway restart is picked up by
the brain within a couple of seconds. So the day-2 commands work on the gateway:

```
teaport sip restart            # the gateway, then waits for the 200 OK (the brain reconnects)
teaport sip aec off            # A/B the echo canceller: flips aec= in the conf, restarts the gateway
teaport sip aec on
teaport sip aec                # show the current setting
teaport logs sip -f            # the gateway's journal (the calls' own lines are in `teaport logs brain`)
teaport doctor                 # the gateway, the brain's SIP front-end, and whether the trunk is registered
```

The `.conf` is the gateway's own `key=value` format (`registrar_uri`, `id_uri`,
`username`, `password`, `uds_path`, `aec`, `auto_answer`, …). Editing it by hand
is fine; `teaport sip restart` afterwards. Nothing about the line lives in
`/tmp`: the conf is in `~/.config/teaport`, the socket in `/run/teaport` (created
by the unit), the logs in journald.

### Testing the phone path without a line

`brain/tests/fake_sip_gateway.py` stands in for `teaport-sip` on the brain's side
of the socket: it binds the socket, says hello, rings and answers a call from a
caller ID you choose, plays a WAV as the caller at real time, records what the
caller hears, and hangs up when the bot has answered, partway through the answer,
or after N seconds. A brain that drops mid-call is replayed the call when it
reconnects, as the real gateway does. Nothing registers and no SIP port is bound,
so you can test on a box whose trunk account is registered somewhere else. It only
needs the system `python3`. Run it from a checkout that contains it.

The brain needs nothing: `teaport-brain`'s SIP front-end looks for the gateway's
socket (`TEAPORT_SIP_SOCKET`, by default `/run/teaport/teaport-sip.sock`) every 2 s,
and takes calls from whatever serves it, through the same session arbiter as Talk.
So the fake serves that path.

**Never start `teaport-sip` for this:** it registers. Guard it for the session
first. `tools/sip-line-guard on` adds a runtime drop-in to `teaport-sip` whose
condition can never hold, so every start of it is skipped: a direct start, a
restart, `teaport sip restart`, or a pull from another unit. The drop-in is gone at
the next boot. `systemctl mask --runtime` does not work here: the unit file in
`/etc/systemd/system` outranks a mask in `/run`.

`tools/sip-line-guard status` proves the guard holds without starting anything. It
checks three things, and exits 0 only when all three hold:
- the drop-in is loaded;
- `systemd-analyze condition` reports the guard condition failed;
- the gateway is not running.

```
cd <checkout>                                   # one that contains brain/tests/fake_sip_gateway.py
sudo tools/sip-line-guard on                    # ends with "GUARDED: teaport-sip.service cannot start"
tools/sip-line-guard status                     # re-check any time; exit 0 = guarded
sudo install -d -o teaspoon -g teaspoon -m 0750 /run/teaport   # teaport-sip's RuntimeDirectory, absent while it is off
install -d -m 0700 ~/fake-sip

# The fake gateway. teaport-brain connects within ~2 s; the fake waits up to 120 s.
python3 brain/tests/fake_sip_gateway.py --socket /run/teaport/teaport-sip.sock --allow-live-path \
    --from +15551234567 --wav brain/test/question.wav --record ~/fake-sip/bot.wav \
    --record-stereo ~/fake-sip/both.wav --summary-json ~/fake-sip/summary.json

# Meanwhile, in another shell: the call's own lines
journalctl -u teaport-brain -f                  # "SIP front-end: connected to the gateway at ..."

# Done: the fake removes its socket when it exits. Then:
sudo rmdir /run/teaport
sudo tools/sip-line-guard off
```

If `brain.env` sets `TEAPORT_SIP_SOCKET` to another path, serve that path instead.
It must not be empty, because empty turns the SIP front-end off. The fake refuses
to bind a path something is already listening on. It compares real paths, so a
symlinked alias like `/var/run` does not get past that check. Without
`--allow-live-path` it also refuses the default path.

The fake prints a JSON summary per call: whether it was answered, when the
greeting and the answer started and ended (seconds from answer), the answer's
latency from the end of the WAV, who hung up, and the control messages the brain
sent. It exits 1 in four cases:
- the call was not answered;
- there was no greeting;
- there was no answer to the WAV;
- there was a protocol violation: an audio frame that is not exactly 640 bytes, a
  datagram over the gateway's 2048-byte read, or unparseable control.

`both.wav` has the caller on the left and the bot on the right. The test WAVs:
- `brain/test/question.wav` asks "What is the capital of France, and is it a large
  city?"
- `brain/test/question_host_status.wav` asks for the host status, which is a tool
  call.

Any 16-bit WAV works: other rates and stereo are converted to the 16 kHz mono the
gateway sends.

Other calls:

```
--hangup-mid-reply 1.5             # hang up 1.5 s into the bot's answer
--hangup-after 60                  # hang up 60 s after answer at the latest
--wait-brain-hangup --hangup-after 60   # leave hanging up to the brain (capped)
--calls 3 --between 2              # three calls in a row on one connection
--no-auto-answer                   # ring until the brain sends call.answer
```

To test a brain restart mid-call:
1. Start a call that stays up on its own, with `--wait-brain-hangup --hangup-after 60`. Without a WAV, the default hangs up as soon as the greeting is over.
2. While it runs, `sudo systemctl restart teaport-brain`, or `sudo systemctl kill -s KILL teaport-brain`, which `Restart=` brings back 5 s later. Either one also ends any Talk session on the box.
3. The fake keeps the call up. Its socket survives too, because `/run/teaport` is not `teaport-brain`'s.
4. The fake replays the call to the brain when it reconnects. Its log shows `brain 2 connected: hello + replay of call ...`.
5. The brain's journal shows it `RESUMING a call already in progress`, and the summary has `"brain_connects": 2`.

`python -m teaport_brain.sip_server --socket PATH` runs the SIP front-end on its
own. It is for test rigs off the box only. It has its own arbiter and cannot see
the box's Talk sessions. So on a box whose room mic holds the engine (wake words
set), its calls get the busy line rather than the handoff.

Off the box, `brain/tests/fake_engine.py` stands in for the engine and the LLM
(`python tests/fake_engine.py` prints the env to point a brain at it).
`tests/test_sip_fake_gateway.py` runs whole calls through `teaport-brain` against
both fakes.

## Turn-taking per front-end

Phone calls and Talk run in one brain process, but they were tuned apart (blind A/Bs:
Talk stops at 0.2 s with a 0.6 s Smart Turn ceiling; the phone keeps 0.5 s / 1.0 s).
So `ENDPOINT_STOP_SECS`, `SMARTTURN_STOP_SECS`, `SMARTTURN_COMPLETE_THRESHOLD` and
`TEAPORT_INTERRUPT_MIN_WORDS` are the shared values, and each front-end can override
them in `brain.env`: `SIP_ENDPOINT_STOP_SECS`, `TALK_ENDPOINT_STOP_SECS`, and so on
(the interrupt count drops its `TEAPORT_` prefix: `SIP_INTERRUPT_MIN_WORDS`,
`TALK_INTERRUPT_MIN_WORDS`). An unset or unreadable override falls back to the shared
value. Each session logs the values it was built with (`turn-taking (sip): …`).

## One engine, one conversation

The engine serves **a single speech-to-text session at a time**, and the box
holds **one conversation at a time**. A session arbiter in `teaport-brain`
(`brain/teaport_brain/session_arbiter.py`) decides who gets the engine, with one
policy for every front-end: Talk clients (the OpenClaw app or dashboard, the
Discord bridge), the box's own microphone (the local audio bridge) and phone
calls. Nobody is ever cut off without a word:

| Someone new, while … | What happens |
|---|---|
| nothing is live | They get the agent. |
| the session holding the agent has lost its client (its socket is closed, or it has sent nothing — not even microphone silence — for 20 s) | That session is ended quietly and the newcomer gets the agent. |
| the same client's session is live (a reconnect) | The new connection replaces it. A client is "the same" by the id it sends (`?client=`): the OpenClaw plugin sends one per paired device, the Discord bridge `discord`, the mic bridge `local-audio`. The OpenClaw Control UI served over plain HTTP has no device identity, so its reload counts as a new client: the old session is ended once its socket closes (a reload closes it) or goes silent. |
| another Talk session, or a conversation at the box, is live | They are refused: the agent says *"Sorry, I'm in another conversation right now…"* and the connection closes (code 4004). The conversation in progress goes on. |
| the box's mic is asleep (waiting for a wake word; without wake words, until someone in the room has spoken, and again after `LOCAL_AUDIO_KEEPALIVE_SECS` with nobody speaking) | A Talk session or a phone call takes the engine; the mic bridge stays off while they last. A sleeping mic is not a conversation. |
| a phone call comes in during a conversation (a remote Talk session, or the box's mic awake) | The caller hears it ring while the agent asks in that conversation, in its language: *"Someone's calling me — +1 346 234 8500. Should I step away for a moment?"* (*"Someone's calling me. Should I step away…"* when there is no name or number; a withheld display name shows the number when the call carries one). **Yes:** it says *"I'll take the call — back in a moment."*, the conversation goes on hold (its connection and what was said are kept; it hears nothing meanwhile), and the call is answered. When the call is over it comes back: *"Sorry about that — where were we?"*. **No:** the call is never answered; the caller hears it ring until they give up. **No answer** within `TEAPORT_CALL_PROMPT_SECS`: `TEAPORT_CALL_PROMPT_DEFAULT` (by default it takes the call). A call already connected (a gateway conf with `auto_answer=true`, or a call that was up when the brain restarted) is not asked about: the caller would wait on a silent line; it is taken, and the conversation held. With `TEAPORT_TOOL_ANSWER_PHONE_CALL=0` nobody is asked: a Talk session hears *"Sorry, a phone call is coming in and I have to take it…"* and closes (4005), and a conversation at the box keeps it (the caller hears the busy line). |
| a phone call is live | A Talk client is refused: *"Sorry, I'm on a phone call right now…"*. A second call is turned away by the gateway (486 Busy Here: the caller's side takes it, voicemail or a busy tone). |

Phone calls and Talk run in the same brain process, so the arbiter sees both;
anything else that holds the engine (a standalone test rig, say) is met first come,
first served, and the loser hears *"Sorry, the voice assistant is busy with another
session right now — please try again in a moment."*

For a box you want to dedicate to the phone, turn off the other front-ends rather
than the brain (the brain is what answers calls): the box's microphone
(`sudo systemctl disable --now teaport-local-audio`), the Discord bridge
(`teaport-discord-bridge`), and Talk in OpenClaw. A call already asks a live
conversation to make way (and takes the engine if nobody answers); only a "no"
keeps it out.

Who is calling is said aloud in that question, as at most 40 characters of the caller
ID: letters (any script), digits and a name's punctuation. It is the far end's text,
and it becomes the agent's own words.

A call **rings before it is answered**: the brain, not the gateway, answers it
(`auto_answer=false` in the gateway's conf, which `teaport sip configure` writes;
the gateway needs teaport-sip 0.6.0 or later for the caller to hear it ring),
`SIP_ANSWER_AFTER_SECS` after it came in (a couple of rings), with the call's pipeline
already built and its greeting already worded, so the greeting plays as soon as the
caller is connected. A gateway conf from before this (`auto_answer=true`) still
works: calls are answered at once as before, and a call someone said not to pick up
hears the busy line instead of ringing on (it is already answered); the brain logs
a warning at the first such call. To switch an existing line over, set
`auto_answer=false` in `~/.config/teaport/teaport-sip.conf` and `teaport sip restart`
(it re-registers).
