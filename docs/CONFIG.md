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
| `/etc/teaport/engine.env` | `teaport-engine` | systemctl restart teaport-engine — reloads Voxtral + Kokoro onto the GPU; both brains lose their engine session. |
| `/etc/teaport/brain.env` | `teaport-brain`, `teaport-sip-brain` | Talk brain: systemctl restart teaport-brain (drops the live Talk session). SIP brain: `teaport sip restart` — the gateway and the SIP brain together; a brain-only relaunch desyncs the gateway echo canceller. |
| `~/.config/teaport/teaport-sip.conf` | `teaport-sip` | Edit it and `teaport sip restart` (or re-run `teaport sip configure`, which test-registers before it writes). `teaport sip aec on\|off` flips the echo canceller and restarts the pair for you. |
| `/etc/teaport/bridge.env` | `teaport-discord-bridge` | systemctl restart teaport-discord-bridge. |

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
| `TTS_SENTENCE_SOFT_MAX` | **80** chars (≥ 0) | A sentence longer than this is split at clause boundaries before synthesis; the engine synthesizes a chunk whole before any of it plays, so one long sentence is the whole first-audio wait (a 236-char sentence began 4.6 s after the model finished it). 0 disables the split. |
| `TTS_FIRST_CLAUSE_CHARS` | **32** chars (≥ 8) | Size of the first chunk — the one the caller waits on. |
| `TTS_CLAUSE_GROWTH` | **1.5** (1.0–1.67) | Each chunk may grow this many times the previous. Must stay below 1/RTF (~1.67 at the measured CPU RTF 0.6) or a chunk's synth outruns the previous chunk's playout and playback stalls at the seam. |
| `TTS_CLAUSE_CAP` | **200** chars (≥ 8) | The largest ramped chunk. |
| `TTS_CLAUSE_HARD_MAX` | **350** chars (8–450) | Last-resort mid-sentence word break so a run-on cannot overflow the engine's ~512-token utterance limit and crash the synth. |
| `TTS_SEAM_KEEP_LEAD` | **0.05** s (≥ 0) | Near-silence kept before a chunk's first sound. |
| `TTS_SEAM_KEEP_TRAIL` | **0.25** s (≥ 0) | Near-silence kept after a chunk's last sound. With the lead this lands a seam near a sentence's natural ~320 ms comma pause instead of the ~793 ms a naive concat gives. |
| `TTS_USER_SPEECH_HOLD_MAX_S` | **3.0** s (≥ 0) | While VAD says the user is speaking, the next clause is held back to free the GPU for barge-in transcription. This caps the hold so sustained noise cannot stall the reply. |
| `TTS_CAPTION_LEAD_SECS` | **0.2** s (≥ 0) | Caption lead shared by the TTS word timestamps and the heard-word ledger. One knob so the two cannot drift. |

## Turn-taking

In `/etc/teaport/brain.env`. Each is explained at length where it is read, in endpointing.py.

| Setting | Default | Description |
|---|---|---|
| `ENDPOINT_STOP_SECS` | **0.5** s (≥ 0.1) | Silence before the VAD reports the caller stopped. The dominant fixed latency on every turn; lower is snappier and cuts more mid-sentence pauses. With TEAPORT_STT_COMMIT_ON=verdict the transcript segment survives a pause Smart Turn calls unfinished, so this can come down toward pipecat's 0.2 where the verdicts are trustworthy (wideband audio); on telephony they are not, and the floor still does the work. |
| `SMARTTURN_STOP_SECS` | **1.0** s (≥ 0) | How long a Smart Turn "not done" verdict holds the turn open, counted from the VAD stop. With TEAPORT_STT_COMMIT_ON=verdict the transcript segment is held open for the same wait, so a caller who resumes keeps one utterance. With TEAPORT_STT_COMMIT_ON=vad-stop and TEAPORT_SPECULATIVE_REPLY on, the reply is already being generated while this runs, so it can be raised (more patience for mid-sentence pauses) by up to the LLM's own latency at no cost to the turns that fall through. |
| `TEAPORT_SPECULATIVE_REPLY` | **off** | Ask the LLM as soon as the final transcript lands on a turn Smart Turn has not concluded on (an INCOMPLETE verdict waiting out SMARTTURN_STOP_SECS), and use that reply if the turn then commits with exactly that text and nothing else touched the context. Off: the request waits for the commit. On: those turns answer up to the LLM's latency sooner, and a turn the caller resumes has spent one wasted request — every outcome logs a [SPEC] line with the running hit/miss tally. Needs TEAPORT_STT_COMMIT_ON=vad-stop to have a final to work on: under verdict the segment is held open through that wait and no final lands before the commit. |
| `SMARTTURN_COMPLETE_THRESHOLD` | **0.5** (0–1) | The end-of-turn probability that counts as done. Near-inert on telephony audio. |
| `VAD_CONFIDENCE` | **0.7** (0–1) | Silero's speech-probability gate. |
| `VAD_MIN_VOLUME` | **0.6** (0–1) | Silero's volume gate. |
| `VAD_SAMPLE_RATE` | `16000` | 8000 runs Silero on the true narrowband signal when the trunk is G.711; 16000 is the gateway's upsample. One of `16000`, `8000`. |
| `TEAPORT_INTERRUPT_MIN_WORDS` | **2** (≥ 1) | Transcribed words needed to interrupt the bot. 1 makes every word a barge-in, backchannels included. |
| `TEAPORT_STRANDED_INTERIM_SECS` | **1.5** s (≥ 0.2) | Commit a segment ourselves after this much interim quiet with no VAD stop, so a missed stop loses a second rather than the turn. |
| `TEAPORT_STT_COMMIT_ON` | `verdict` | What closes the transcript segment and forces the final. verdict: Smart Turn's answer to the VAD stop — done commits at once, not-done holds the segment open until the turn is judged complete, so a mid-sentence pause keeps one utterance instead of splitting it into two context-free decodes; the model's inference (~0.1 s) is then on the commit path, which lowering ENDPOINT_STOP_SECS more than pays for where the verdicts are trustworthy. vad-stop: the raw VAD stop, before the verdict exists (the behaviour before issue #43); a turn that falls through SMARTTURN_STOP_SECS gets its final ~0.8 s sooner, and TEAPORT_SPECULATIVE_REPLY needs this. Either way a VAD stop while the bot is speaking commits at once — that flush is the barge-in. One of `verdict`, `vad-stop`. |
| `HEARD_MODE` | `truncate` | How the context records a reply the caller only partly heard: truncate it to what was heard, or keep it whole with a note. One of `truncate`, `note`. |
| `TEAPORT_SILENT_TURN_SECS` | **12** s (≥ 1) | How long a committed turn may produce no audio before it is reported in the journal. A turn waiting on an agent consult is not counted. |

## Agent consult and follow-ups

In `/etc/teaport/brain.env`.

| Setting | Default | Description |
|---|---|---|
| `OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:18789` | The co-resident gateway for shared persona and memory recall. The installer sets it from the gateway port. *Set by the installer.* |
| `OPENCLAW_GATEWAY_TOKEN` | file `~/.config/teaport/openclaw_token` | Bearer token for that gateway. Prefer the file; the env var, if set, wins over it. |
| `OPENCLAW_AGENT_ID` | `main` | The agent consulted. |
| `OPENCLAW_BIN` | — | The openclaw CLI used as the consult fallback. Auto-detected; the host may have none when OpenClaw runs only inside the sandbox. *Set by the installer.* |
| `TEAPORT_AGENT_FIRST` | **off** | Agent-first routing: install the strict router directive and drop the "I'll work on that" ack. |
| `TEAPORT_RECALL_TIMEOUT` | **1.5** s (≥ 0.1) | Memory-recall budget. Never awaited on the frame path, so a generous value adds no turn latency; 1.5 s clears a cold embedder (~1.14 s measured on-box). |
| `TEAPORT_CONSULT_TIMEOUT` | **45** s (≥ 5) | CLI consult ceiling. The sandbox-exec shim adds ~14 s of spawn tax before the agent turn even starts. |
| `TEAPORT_NATIVE_CONSULT_ACK_TIMEOUT` | **1.5** s (≥ 0.2) | No ack within this means no native runner: fall back fast instead of burning the full window. A live relay acks within moments; the Discord bridge never acks. |
| `TEAPORT_NATIVE_CONSULT_TIMEOUT` | **45** s (≥ 5) | How long to wait on an acked native consult. Useful consults return in ~15–30 s; past ~45 s the voice wait degrades faster than the answer improves. |
| `TEAPORT_ASK_OPENCLAW_TIMEOUT` | **55** s (≥ 5) | The function-call timeout for ask_openclaw. Must exceed ack + native consult or pipecat abandons the call and drops the late answer. Bounds the sync path only. |
| `TEAPORT_ASYNC_CONSULT_TIMEOUT` | **180** s (≥ 5) | Async consult ceiling. The consult runs off the turn; the answer is spoken as an unprompted follow-up when it lands, or an honest "couldn't get it" past this. |
| `TEAPORT_FOLLOWUP_QUIET_S` | **0.7** s (≥ 0) | How long the conversation must stay quiet before a follow-up window counts; rejects mid-thought pauses and between-turn gaps. |
| `TEAPORT_FOLLOWUP_MAX_WAIT_S` | **60** s (≥ 1) | Ceiling on holding a follow-up for a gap; past this, speak anyway. |
| `TEAPORT_FOLLOWUP_MIN_HEARD` | **0.3** (0–1) | Fraction of a delegated answer the caller must have heard before it counts as delivered; below it, the answer is said again at the next quiet moment. |
| `TEAPORT_THINKING_SOUND` | **on** | The typing bed during a long agent consult. |
| `TEAPORT_THINKING_GRACE_S` | **1.5** s (≥ 0) | Silence before the bed starts. |
| `TEAPORT_THINKING_GAIN` | **0.8** (0–1) | Bed level; 1.0 is the synthesized peak. |
| `TEAPORT_THINKING_MAX_S` | **60** s (≥ 1) | Hard cap on the bed. Keep it above TEAPORT_ASK_OPENCLAW_TIMEOUT. |
| `TEAPORT_PERSONA_FILE` | `~/.config/teaport/persona.md` | Fallback persona when the gateway's is not reachable. The installer points it at the secrets dir. *Set by the installer.* |
| `OPENCLAW_WORKSPACE` | `~/.openclaw/workspace` | The OpenClaw workspace whose persona files the voice brain shares. *Set by the installer.* |
| `TEAPORT_WORKSPACE_FILES` | `SOUL.md,IDENTITY.md,USER.md,MEMORY.md` | Comma-separated workspace files injected as the shared persona, in order. IDENTITY.md is agent-writable by design. |
| `OPENCLAW_MEMORY_DIR` | `~/.openclaw/workspace/memory` | The daily-note store a voice-saved memory is appended to, shared with the text agent so both recall it. *Set by the installer.* |
| `TEAPORT_THINKING_WAV` | `<package>/assets/typing.wav` | Your own 24 kHz mono wav for the thinking bed; the default is synthesized and cached next to the code. |

## SIP brain

In `/etc/teaport/brain.env`. Read only by teaport-sip-brain; the Talk brain ignores them.

| Setting | Default | Description |
|---|---|---|
| `SIP_HALF_DUPLEX` | **off** | Drop the caller's mic while the bot speaks. On means no barge-in. The unit also sets Environment=SIP_HALF_DUPLEX=0, but EnvironmentFile= overrides Environment= in systemd, so a value in brain.env wins. |
| `SIP_HALF_DUPLEX_TAIL_S` | **0.8** s (≥ 0) | The tail after the bot stops, when half-duplex is on. |
| `SIP_STT_MAKEUP_DB` | **0** dB (0–20) | Makeup gain added to the caller signal the transcriber sees, to recover the quiet speech a caller produces over the bot. 0 = off; 6 recovered the quiet barge-in "stop"s with no regressions on 205 clips. VAD and endpointing are upstream of it and unaffected. |
| `TEAPORT_SIP_SOCKET` | — | Gateway-to-brain Unix socket. The unit passes --socket explicitly, so this is a fallback only. *Set by the installer.* |

## Diagnostics

In `/etc/teaport/brain.env`. All off by default; log-only.

| Setting | Default | Description |
|---|---|---|
| `TEAPORT_ENDPOINT_DEBUG` | **off** | VAD state transitions, Smart Turn verdicts and the turn-commit / first-audio timing bubbles in the journal. |
| `TEAPORT_ENDPOINT_DIST_EVERY` | **500** frames (≥ 1) | The per-frame confidence census interval (500 frames ≈ 16 s). |
| `LEDGER_TRACE` | **off** | Trace every frame the transcript ledger sees. Parsed as == "1", not through env_flag: true/yes/on are silently off. Write 1. |
| `TEAPORT_TRACE` | **off** | Keep the [CAP] caption-pipeline and [WTS] word-timestamp traces in the journal. |
| `TEAPORT_AUDIO_DUMP` | — | A directory; every call writes the caller PCM the brain received plus a sidecar of bot-playout offsets. Empty = off. |
| `TEAPORT_AUDIO_DUMP_MAX_SECS` | **600** s (≥ 1) | Caps the recording. |
| `TEAPORT_CAPTION_USER_HOLD_S` | **1.2** s (≥ 0) | Gap after the user's last interim before assistant partials may render again; prevents the doubled assistant bubble in the Talk UI. |
| `ENGINE_LOG` | `~/teaport-engine.log` | The engine's serve log, where the tools read decode ms/step. *Set by the installer.* |

## Installer-owned

In `/etc/teaport/brain.env`. Written by install.sh. Shown read-only.

| Setting | Default | Description |
|---|---|---|
| `BRAIN_PORT` | **7861** port (1–65535) | Passed as --port by the unit. Changing it means re-rendering the plugin config, bridge.env and Caddy. *Set by the installer.* |
| `GATEWAY_PORT` | **7861** port (1–65535) | Code fallback for the listen port when --port is not given. On an installed box BRAIN_PORT is the effective knob. *Set by the installer.* |
| `GATEWAY_TOKEN` | — | Shared secret for /talk. Empty means anyone who can reach the port gets a full agent session and can evict the live call; the brain warns loudly at startup. Mirrored into the plugin config by install.sh. Never change one side alone. *Set by the installer.* |
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
| `uds_path` | — | Must match the sip-brain's --socket. *Set by the installer.* |
| `auto_answer` | **on** | Answer inbound calls. |
| `aec` | **on** | The gateway's echo canceller. Keep it on; the sip-brain's makeup gain assumes it. |
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
down two units — `teaport-sip` (the gateway) and `teaport-sip-brain` (a second
front-end onto the same brain) — but both are gated on a config file that does
not exist yet, so nothing starts.

Turn it on with the wizard:

```
teaport sip configure          # prompts for registrar/SBC host, domain, user, password
teaport sip configure --host sbc.example.net --domain voip.example.net \
                      --user 100 --password '…' --yes   # headless
```

It test-registers against your trunk (briefly, on a throwaway port — never
`:5060`, so a running gateway is untouched) and only on a `200 OK` writes
`~/.config/teaport/teaport-sip.conf` (mode `600`, holds the SIP password) and
enables both units. On a failed register it writes nothing and leaves telephony
off. `teaport sip status` shows the units, the config, and this run's
registration; `teaport sip disable [--purge]` turns it back off.

Already have a gateway `.conf` — from a box that ran `teaport-sip` by hand, or
carried over from another one? Adopt it instead of retyping it:

```
teaport sip configure --conf ~/my-trunk.conf
```

It goes through the same test-register, then is installed as-is except for the
two keys the units own (`sip_port` → 5060, `uds_path` → the socket the SIP brain
is started with). If a hand-launched gateway or SIP brain is still running, the
command refuses and tells you what to stop: the unit it is about to enable needs
`:5060` and the socket.

Once configured, the line **survives reboots and crashes on its own**: both units
are `enabled`, the gateway restarts with a backoff that will not hammer the
registrar, and the SIP brain is bound to the gateway — stopped, started and
restarted *with* it, never alone. That coupling is deliberate: the gateway's
echo canceller references the brain's playout, and a brain relaunched under a
running gateway leaves the canceller eating the caller's speech. So the day-2
commands all work on the pair:

```
teaport sip restart            # gateway + SIP brain together, then waits for the 200 OK
teaport sip aec off            # A/B the echo canceller: flips aec= in the conf, restarts the pair
teaport sip aec on
teaport sip aec                # show the current setting
teaport logs sip -f            # the gateway's journal (sip-brain for the brain's)
teaport doctor                 # includes the pair + whether the trunk is registered
```

The `.conf` is the gateway's own `key=value` format (`registrar_uri`, `id_uri`,
`username`, `password`, `uds_path`, `aec`, `auto_answer`, …). Editing it by hand
is fine; `teaport sip restart` afterwards. Nothing about the line lives in
`/tmp`: the conf is in `~/.config/teaport`, the socket in `/run/teaport` (created
by the unit), the logs in journald.

## One engine, one STT slot

The engine serves **a single speech-to-text session at a time**. The local
OpenClaw brain (`teaport-brain`) and the SIP brain (`teaport-sip-brain`) share
that one slot: **whoever connects first holds it**, and the second one to
connect hears *"Sorry, the voice assistant is busy with another session right
now — please try again in a moment."* On a phone that plays, then the call hangs
up; in the OpenClaw app the session simply ends. This is expected — it is not a
crash.

For a box you want to dedicate to the phone, stop the local brain from
contending for the slot:

```
sudo systemctl disable --now teaport-brain
```

Telephony then always wins the slot. (Re-enable `teaport-brain` to get the local
assistant back.)
