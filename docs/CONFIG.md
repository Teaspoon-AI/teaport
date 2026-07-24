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
- `KOKORO_RESERVE_FPT` — the engine memory reserve. Use **6** with a NemoClaw
  sandbox. Use **12** for a voice-only device. The installer always sets this
  value. The code default is not safe on an 8 GB device.
- `TEAPORT_URL` — the engine realtime WebSocket (default
  `ws://127.0.0.1:8000/v1/realtime`).
- `OPENCLAW_GATEWAY_URL` — the co-resident gateway for shared persona and
  memory recall.
- `~/.config/teaport/` — `openrouter_key` · `groq_key` · `cerebras_key` ·
  `openclaw_token` · `persona.md`

TODO: full table with defaults, and which settings are safe to change live.
