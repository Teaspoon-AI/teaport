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
  completion tokens: **1024** at `low`, **3072** at `medium`, **8192** at `high`.
  Set **0** for no cap — correct for a local or un-metered endpoint.
- `TEAPORT_LLM_TEXT_GUARD` — fold degenerate unicode out of the model's replies
  and cut a runaway ellipsis collapse (default on). Set `0` to disable.
- `TEAPORT_LLM_GUARD_RECOVERY` — the one line spoken when that guard trips.
  Override it for a non-English deployment.
- `TEAPORT_RAW_LLM_CAPTURE` — log a completion verbatim when it degenerates
  (default on; healthy turns log nothing). Set `0` to disable.
- `TEAPORT_THINKING_SOUND` — the typing bed during a long agent consult
  (default on). Set `0` to disable.

Boolean settings accept `0`/`false`/`no`/`off` and `1`/`true`/`yes`/`on`. An
empty value means *unset* — the default applies. A flag that is off says so in
the journal at startup.
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

TODO: full table with defaults, and which settings are safe to change live.
