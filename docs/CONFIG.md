# Configuration

> **Scaffold.** This is an outline. The full document comes with the public
> docs pass.

Non-secret configuration lives in `/etc/teaport/{engine,brain}.env`. Per-user
secrets live in `~/.config/teaport/`. The installer does not write secrets
into units or images.

Important settings:

- `LLM_BASE_URL` — **required.** Your OpenAI-compatible endpoint. Examples:
  `https://api.groq.com/openai/v1`, `https://api.cerebras.ai/v1`,
  `https://openrouter.ai/api/v1`, or `http://127.0.0.1:8182/v1` for a local
  server.
- `LLM_API_KEY` — the key for that endpoint. You can also write it to
  `~/.config/teaport/llm_key`.
- `LLM_MODEL` — the served model name (default `gpt-oss-120b`).
- `LLM_REASONING_EFFORT` — reasoning effort for models that support it
  (default `low`; set `""` to disable).
- `LLM_EXTRA_BODY` — optional JSON merged into the request `extra_body`.
  Example for OpenRouter routing:
  `{"provider":{"order":["Groq"],"allow_fallbacks":true}}`.
- `LLM_MAX_TOKENS` — completion cap. A credit-metered gateway reserves against
  the model's ceiling (65536 for gpt-oss-120b) and refuses the request when the
  remaining balance cannot cover it, however short the answer would be. The
  default follows `LLM_REASONING_EFFORT`, because reasoning tokens are billed as
  completion tokens: **1024** at `low`, **3072** at `medium`, **8192** at `high`,
  and **4096** when the effort is unset or `""` — in that case the model applies
  its own default effort, so the cap has to leave room for an unknown one.
  Set **0** for no cap — correct for a local or un-metered endpoint.
- `TEAPORT_LLM_TEXT_GUARD` — fold degenerate unicode out of the model's replies
  and cut a runaway ellipsis collapse (default on). Set `0` to disable.
- `TEAPORT_LLM_GUARD_RECOVERY` — the one line spoken when that guard trips.
  Override it for a non-English deployment.
- `TEAPORT_RAW_LLM_CAPTURE` — log a completion verbatim when it degenerates
  (default on; healthy turns log nothing). Set `0` to disable.
- `TEAPORT_THINKING_SOUND` — the typing bed during a long agent consult
  (default on). Set `0` to disable.
- `LLM_TIMEOUT_SECS` — how long one completion attempt may take before it is
  abandoned (default **20**). Without it the OpenAI SDK waits **600** — ten
  minutes of a voice turn hanging with no error, no log line and no audio. This
  bounds each *attempt*: one retry follows, so the worst case before the failure
  is spoken is roughly twice this. Raise it for a slow local llama.cpp box.
- `TEAPORT_LLM_ERROR_SPEECH` — say a short line out loud when the model call
  fails, instead of leaving the room silent (default on). Set `0` to disable.
- `TEAPORT_LLM_ERROR_SPEECH_DEBOUNCE` — seconds between spoken failure notices
  (default **30**), so a failing endpoint is reported once per window rather
  than once per turn.
- `TEAPORT_SILENT_TURN_SECS` — how long a committed turn may produce no audio
  before it is reported in the journal (default **12**). A turn still waiting on
  an agent consult is not counted against it.
- `KOKORO_RESERVE_FPT` — the engine memory reserve. Use **6** with a NemoClaw
  sandbox. Use **12** for a voice-only device. The installer always sets this
  value. The code default is not safe on an 8 GB device.
- `TEAPORT_URL` — the engine realtime WebSocket (default
  `ws://127.0.0.1:8000/v1/realtime`).
- `OPENCLAW_GATEWAY_URL` — the co-resident gateway for shared persona and
  memory recall.
- `~/.config/teaport/` — `llm_key` · `openclaw_token` · `persona.md`.
  One key for one OpenAI-compatible endpoint; the per-provider key files were
  merged into `llm_key`, and nothing reads the old names.

Boolean settings accept `0`/`false`/`no`/`off` and `1`/`true`/`yes`/`on`. An
empty value means *unset* — the default applies. A flag that is off says so in
the journal at startup.

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
off. `teaport sip status` shows the units, whether the config exists, and the
last registration; `teaport sip disable [--purge]` turns it back off.

The `.conf` is the gateway's own `key=value` format (`registrar_uri`, `id_uri`,
`username`, `password`, `uds_path`, `aec`, `auto_answer`, …). Editing it by hand
is fine; restart `teaport-sip` afterwards.

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

TODO: full table with defaults, and which settings are safe to change live.
