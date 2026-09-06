# brain tests

CI gate for the public repo: **green pytest is the merge gate**.

```
pytest brain/tests/
```

## Setting up a dev box

The tests assert on pipecat internals, so they mean nothing against the wrong version —
`pinned_pipecat.py` refuses to run rather than report a false pass. `brain/uv.lock` pins
the whole closure, so a dev box matches the appliance exactly — install from the LOCK
(`uv sync`), not by re-resolving the loose pyproject constraints (`uv pip install -e`
would hand you tomorrow's transitives while the appliance runs the frozen set):

```sh
uv sync --locked --project brain     # brain/.venv from uv.lock — the appliance set + the locked pytest (dev group)
uv run --project brain python -m pytest brain/tests/test_suite.py -q
```

Expect **28 passed, 2 skipped** off-appliance. The two skips need hardware this box
doesn't have; **anything failing is a real regression**, including on a laptop —
with one exception: if pipecat doesn't match the pin, every test that imports
`pinned_pipecat` fails immediately with a message telling you to reinstall. That's
an environment problem, not a regression; rerun the `uv sync` step above and it goes
away.

That distinction is the point. Those two used to *fail* off-appliance, so this file
called green pytest the merge gate while `pytest brain/tests/` could never be green —
and a genuine regression in them looked exactly like not owning a Jetson. A script
whose dependency is absent now exits `appliance.SKIP_EXIT` and the runner turns it
into a skip (see `appliance.py`). CI needs no `-k` denylist as a result, so a new
appliance-dependent test is handled by declaring its own requirement rather than by
someone remembering to edit a filter string.

`test_suite.py` is the gate — the `test_*.py` files are
self-contained scripts (each has a `__main__` that exits nonzero), and the suite runs
them one per subprocess rather than as pytest natives. Pytest is configured
(`brain/pyproject.toml`) to collect only `test_suite.py`, so the scripts don't also get
collected directly and re-run.

## What needs what

**Everything is hermetic except the two below.** Stated as an exception list on
purpose: this used to name the hermetic five by hand, and it was still naming five
when there were twenty-three. An allowlist has to be remembered; an exception list
is maintained by the tests themselves, since the only way onto it is to declare a
requirement in `appliance.py`.

The suite roster is derived the same way, for the same reason: `test_suite.py`
discovers `test_*.py` rather than listing it. The list had lost five scripts —
`test_followup_injection` (whose import had broken in a refactor with nothing
running it to notice) plus `consult_progress`, `raw_llm_capture`, `repeat_cut` and
`tts_speech_hold`, each of which passes in about a second. Adding a test file is now
all it takes to have it run.

Hermetic here means *run anywhere*, and that is measured, not assumed — as of
2026-08-28 all twenty-one pass inside `unshare -rn` (no network at all) with
`HF_HOME` pointed at an empty directory. Nothing downloads and nothing needs a
pre-seeded model cache: the Silero VAD and smart-turn ONNX files ship inside the
pipecat wheel (`pipecat/audio/vad/data/`, `pipecat/audio/turn/smart_turn/data/`),
so `uv sync` is the whole setup. To re-check after adding a test:

```sh
HF_HUB_OFFLINE=1 HF_HOME=$(mktemp -d) unshare -rn \
  ../.venv/bin/python test_your_new_one.py
```

Need the appliance:

- `test_engine_text_stream.py` — needs a **live engine** (`ENGINE_TTS_STREAM_URL`,
  default `ws://127.0.0.1:8000/...`). Skips off-box; the check is reachability only,
  so if the port answers the test runs and any failure is real.
- `test_remember_tool.py` — needs a **live LLM** (`LLM_BASE_URL` + `LLM_API_KEY` +
  `LLM_MODEL`). Skips when `LLM_BASE_URL` is unset. Asserts the model actually emits the `remember` tool call, so it can flake
  on model behavior rather than on our code. Writes to an isolated tmp memory dir.
