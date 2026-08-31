# teaport-brain

The teaport voice brain, packaged for install: the Pipecat pipeline (engine STT →
your LLM → engine TTS) with heard-grounded barge-in, memory recall, and tools.

```
uv sync --locked                     # in brain/ — venv from uv.lock, the validated closure
uv run teaport-brain --port 7861     # serves the OpenClaw plugin at ws://<host>:7861/talk
```

Needs the engine reachable at `TEAPORT_URL` (STT + TTS); it is not started here.
Config lives in the environment — see `docs/CONFIG.md`.

## Layout

`teaport_brain/` is the import closure of the entry point, `gateway_server` (the
`teaport-brain` console script) — nothing in it is reachable only from tests or dev
harnesses. Some highlights:

- `gateway_server` — the `/talk` WebSocket + `/health` app, session eviction, `main()`.
- `services` — the LLM/STT/TTS factories (provider selection reads the env at call time).
- `endpointing` — the endpointing policy: VAD gates, Smart Turn v3 threshold, barge-in guard.
- `transcript_ledger` / `heard_context` — heard-grounding: what the user actually *heard*.
- `tools` / `openclaw_client` / `memory_recall` — tool calls, memory read/write, consults.

A WebRTC dev-harness entry is deliberately **not** part of this package: it would drag
the SmallWebRTC transport + its prebuilt frontend into a product install. `services.py`
is the shared factory, so nothing is lost.

## Tests

```
pip install pytest
pytest brain/tests/
```

`tests/test_suite.py` is the gate. The `test_*.py` files are standalone scripts (each
exits nonzero on failure); the suite runs them one per subprocess, which is why pytest
is configured to collect only the aggregate.

Two of them need the box, not just a venv: `test_engine_text_stream.py` needs a **live
engine** on `ENGINE_TTS_STREAM_URL`, and `test_remember_tool.py` calls a **live LLM** (it
asserts the model emits the `remember` tool call, so it can flake on model behavior).
The rest are hermetic — see `brain/tests/README.md` for the current count and the
mechanism that keeps this list from going stale again.
