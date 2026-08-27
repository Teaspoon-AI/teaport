# brain tests

CI gate for the public repo: **green pytest is the merge gate**.

```
pytest brain/tests/
```

## Setting up a dev box

The tests assert on pipecat internals, so they mean nothing against the wrong version —
`pinned_pipecat.py` refuses to run rather than report a false pass. The pin
(`brain/pyproject.toml`) is on PyPI, so a dev box can match the appliance exactly:

```sh
uv venv --python 3.12 .venv          # the package requires >=3.12
uv pip install -e ./brain pytest
cd brain/tests && ../../.venv/bin/python -m pytest test_suite.py -q
```

Expect **2 failed, 16 passed** off-appliance: the two failures below need hardware this
box doesn't have. Anything else failing is a real regression.

`test_suite.py` is the gate — the `test_*.py` files are
self-contained scripts (each has a `__main__` that exits nonzero), and the suite runs
them one per subprocess rather than as pytest natives. Pytest is configured
(`brain/pyproject.toml`) to collect only `test_suite.py`, so the scripts don't also get
collected directly and re-run.

Hermetic (run anywhere): `test_captions`, `test_user_transcript`, `test_heard_truncate`,
`test_ledger_words`, `test_memory_recall`.

Need the appliance:

- `test_engine_text_stream.py` — needs a **live engine** (`ENGINE_TTS_STREAM_URL`,
  default `ws://127.0.0.1:8000/...`). Fails with `ConnectionRefusedError` off-box.
- `test_remember_tool.py` — needs a **live LLM** (`LLM_BASE_URL` + `LLM_API_KEY` +
  `LLM_MODEL`). Asserts the model actually emits the `remember` tool call, so it can flake
  on model behavior rather than on our code. Writes to an isolated tmp memory dir.
