---
title: Configuration
description: Configure teaport's engine, brain, and LLM endpoint.
---

:::note[Pre-launch scaffold]
This page is an outline from the [teaport repo](https://github.com/Teaspoon-AI/teaport). A full settings table, with default values and safe-to-change-live information, will come with the public docs pass.
:::

Non-secret configuration files are in `/etc/teaport/{engine,brain}.env`. Per-user secret files are in `~/.config/teaport/`. The installer does not write secrets into units or images.

## Important settings

- `LLM_BASE_URL`: the address of your OpenAI-compatible large language model (LLM) endpoint. This setting is required. Examples: `https://api.groq.com/openai/v1`, `https://api.cerebras.ai/v1`, `https://openrouter.ai/api/v1`, or `http://127.0.0.1:8182/v1` for a local server.
- `LLM_API_KEY`: the key for that endpoint. You can also write the key to the file `~/.config/teaport/llm_key`.
- `LLM_MODEL`: the name of the served model. The default value is `gpt-oss-120b`.
- `LLM_REASONING_EFFORT`: the reasoning effort for models that support this feature. The default value is `low`. Set this value to `""` to turn the feature off.
- `LLM_EXTRA_BODY`: optional JSON (JavaScript Object Notation) text to merge into the request `extra_body`. Example, for OpenRouter routing: `{"provider":{"order":["Groq"],"allow_fallbacks":true}}`.
- `KOKORO_RESERVE_FPT`: the amount of memory that the engine reserves. Use the value **6** with a NemoClaw sandbox. Use the value **12** for a voice-only device. The installer always sets this value. The default value in the code is not safe to use on an 8 GB device.
- `TEAPORT_URL`: the address of the engine's realtime WebSocket. The default value is `ws://127.0.0.1:8000/v1/realtime`.
- `OPENCLAW_GATEWAY_URL`: the address of the gateway on the same device. teaport uses this gateway for the shared persona and for memory recall.
- `~/.config/teaport/`: this folder holds the files `openrouter_key`, `groq_key`, `cerebras_key`, `openclaw_token`, and `persona.md`.
