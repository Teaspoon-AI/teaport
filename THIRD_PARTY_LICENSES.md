# Third-Party Licenses & Attributions — teaport

teaport (the brain package + the OpenClaw plugin) is MIT. This file tracks the
third-party components it depends on and their obligations. The **engine** is a
separate component that ships under its own license terms with its own attributions;
nothing here covers the engine.

## Brain (`brain/teaport_brain`, MIT)

Python package. Direct runtime dependencies — each one is imported somewhere in the
package (authoritative list lives in `brain/pyproject.toml`):

| Component | Role | License |
|---|---|---|
| pipecat-ai | voice pipeline framework | BSD-2-Clause |
| uvicorn | ASGI server for the `/talk` gateway | BSD-3-Clause |
| fastapi | the `/talk` WebSocket + `/health` app | MIT |
| loguru | logging | MIT |
| numpy | audio buffers (TTS + thinking-sound) | BSD-3-Clause |
| websockets | client to the engine's realtime interface | BSD-3-Clause |
| audioop-lts | `audioop.ratecv` shim, Python ≥ 3.13 only (PEP 594) | PSF-2.0 |

Notable transitive dependencies of `pipecat-ai`:

| Component | Why it is here | License |
|---|---|---|
| onnxruntime | runs Silero VAD + Smart Turn v3 in the brain process | MIT |
| transformers | `WhisperFeatureExtractor` for Smart Turn v3 | Apache-2.0 |
| soxr | resampling — **LGPL-2.1-or-later** (see below) | LGPL-2.1-or-later |
| onnx model weights | Silero VAD + smart-turn-v3, shipped inside the pipecat wheel | see pipecat |

Notes:

- The brain is torch-free. Do **not** install the pipecat `local-smart-turn`
  extra — it pulls in torch, torchaudio, and coremltools. The install uses
  `pipecat-ai[openai,websocket]`.
- The installer installs the `espeak-ng` system package (GPL-3.0) for the
  engine. The brain does not run espeak-ng, and no GPL code links into the
  software in this repository. espeak-ng runs as a separate program.

### soxr (LGPL-2.1) — accepted, with one distribution constraint

**Decision (accepted).** `soxr` is **LGPL-2.1-or-later** and an *unconditional* dependency
of pipecat-ai, so the brain's installed tree is **not** uniformly MIT/BSD/Apache — but this
is compliant and ordinary for an open-source Python package. LGPL is *weak* copyleft: it
does not relicense teaport's MIT code; it only requires that a recipient be able to
replace the LGPL library. Because `soxr` is fetched from PyPI by `pip` and this repo neither
vendors, modifies, nor redistributes it, that condition is met automatically
(`pip install soxr==<other>` swaps it). teaport ships none of its bytes; the pip wheel
— which *does* statically bundle a modified libsoxr — is soxr's own LGPL-licensed artifact,
distributed from PyPI, not by us.

**The one constraint (why this stays a non-issue):** the brain is distributed as
**source + pip-installed dependencies** (the installer runs `pip install ./brain` into a
venv). It must **never** be frozen or bundled into a single **closed** binary (PyInstaller,
a vendored/redistributed wheel, a shipped closed image) — that is the only action that would
turn soxr's LGPL into a real relink/redistribution obligation. Keep the brain open and
pip-installed and nothing further is owed beyond this attribution. (Re-audit the full tree
at each public cut — a pipecat bump could add a new-licensed dep.)

## Plugin (`plugin/`, MIT)

The OpenClaw realtime-voice provider. It has **no third-party runtime dependencies** —
plain ESM using Node's built-in `WebSocket` / `Buffer`, with only a `peerDependencies` on
the host `openclaw` (not bundled). So the shipped plugin tree is MIT with nothing further
to attribute. (Re-check `plugin/package.json` if runtime deps are ever added.)

## Engine (separate — not in this repository)

The engine is a separate component with its own license terms. Its attributions
ship with the engine download, not here.
