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
| vaderSentiment | scores a reply's words for the face's mood (`face_mood.py`); brings `requests` (Apache-2.0), `urllib3` (MIT), `charset-normalizer` (MIT) | MIT |
| h2 | HTTP/2 to the language model (`llm_http2.py`); brings `hpack`, `hyperframe` (both MIT) | MIT |

Notable transitive dependencies of `pipecat-ai`:

| Component | Why it is here | License |
|---|---|---|
| onnxruntime | runs Silero VAD + Smart Turn v3 in the brain process | MIT |
| soxr | resampling — **LGPL-2.1-or-later** (see below) | LGPL-2.1-or-later |
| resampy | pipecat's other resampler (`ResampyResampler`) | ISC |
| numba | JIT for resampy's kernels; a direct pipecat dependency too | BSD-2-Clause |
| llvmlite | numba's LLVM binding (compiled) | BSD-2-Clause |
| sentencex | sentence segmentation behind pipecat's text aggregation (pipecat ≥ 1.12; replaced nltk) | MIT |
| soundfile | audio file I/O for pipecat's eval harness and `SoundfileMixer`, a required pipecat dependency since 1.9 that the brain never imports; its Linux wheels bundle **libsndfile (LGPL-2.1-or-later)** — same footing as soxr below | BSD-3-Clause |
| cffi, pycparser | soundfile's binding to libsndfile | MIT-0, BSD-3-Clause |
| python-dotenv | `.env` loading for pipecat's CLI and evals (unused by the brain) | BSD-3-Clause |
| loudness | ITU-R BS.1770 loudness measurement for the VAD's volume gate (pipecat ≥ 1.8.0; replaced pyloudnorm + scipy) | MIT |
| onnx model weights | Silero VAD + smart-turn-v3, shipped inside the pipecat wheel | see pipecat |

This list is the unconditional closure of `pipecat-ai[websocket]` in `brain/uv.lock`,
not everything pipecat can pull: `transformers` used to be listed here for Smart Turn's
`WhisperFeatureExtractor` and is in neither the lock nor the venv — pipecat only
requires it under the `local-smart-turn` extra, which the note below says not to install.

Notes:

- The brain is torch-free. Do **not** install the pipecat `local-smart-turn`
  extra — it pulls in torch, torchaudio, and coremltools. The install uses
  `pipecat-ai[websocket]` (no `openai` extra: the OpenAI LLM service the brain uses is
  a BASE dependency — see the note in `brain/pyproject.toml`).
- The installer installs the `espeak-ng` system package (GPL-3.0) for the
  engine. The brain does not run espeak-ng, and no GPL code links into the
  software in this repository. espeak-ng runs as a separate program.

### soxr and libsndfile (LGPL-2.1) — accepted, with what shipping them asks

(libsndfile arrives inside the soundfile wheel since pipecat 1.9. What follows about soxr
holds for it too, except where a line below says otherwise.)

**Decision (accepted).** `soxr` is **LGPL-2.1-or-later** and an *unconditional* dependency
of pipecat-ai, so the brain's installed tree is **not** uniformly MIT/BSD/Apache — but this
is compliant and ordinary for an open-source Python package. LGPL is *weak* copyleft: it
does not relicense teaport's MIT code. It asks that a recipient be able to replace the LGPL
library, and that whoever distributes the library hands over its licence text and a way to
get its source.

**What teaport ships.** The `teaport-brain` package, built in CI from `brain/uv.lock` by
`packaging/build-deb.sh`, holds the PyPI wheels of that lock, unmodified, as an ordinary
Python environment under `/opt/teaport/brain`. So teaport distributes soxr and libsndfile,
and does what that asks:

- **Licence texts.** Each Python package's own licence files are in its `.dist-info`
  directory in the tree (soxr's, with libsoxr's, under
  `soxr-<version>.dist-info/licenses/`; soundfile's beside its `METADATA`). The package also
  installs this file and a `copyright` file under `/usr/share/doc/teaport-brain/`.
- **Source.** The versions are the ones in `brain/uv.lock`. Their source is the source
  distribution on PyPI (`soxr`, which carries the libsoxr source it bundles; `soundfile`),
  and upstream at <https://github.com/dofuuz/python-soxr> (soxr and libsoxr),
  <https://github.com/libsndfile/libsndfile> (libsndfile) and
  <https://github.com/bastibe/python-soundfile> (soundfile). Teaspoon AI will also send the
  source of those versions to anyone who asks at hello@teaspoon.tech, for three years from
  the release that shipped them.
- **Replacing them.** libsndfile is a separate shared object, `_soundfile_data/libsndfile_<arch>.so`
  in the soundfile package: put your own build in its place. soxr links libsoxr into its
  Python extension: rebuild the wheel from the source distribution against your own
  libsoxr and install it into the tree (`sudo uv pip install --python
  /opt/teaport/brain/bin/python <wheel>`; the tree ships without pip, and uv is not part
  of the package). `teaport doctor`
  and `dpkg -V teaport-brain` then list the changed files, as they list any other change,
  and the next install of the package puts the shipped ones back. To keep a replacement,
  pin it in `brain/pyproject.toml`, run `uv lock`, and build the package with
  `packaging/build-deb.sh`: the lock is what the package is built from.

**The one constraint (why this stays a non-issue):** the brain stays an open Python
environment of unmodified wheels, with its source in this repository and the dependencies'
source on PyPI. It must **never** be frozen or bundled into a single **closed** binary
(PyInstaller, a shipped closed image) — that would take away the recipient's way to replace
the library, which is the one thing the LGPL asks for. Keep the brain open and nothing
further is owed beyond the above. (Re-audit the full tree at each public cut — a pipecat
bump could add a new-licensed dep, and the versions named above move with the lock.)

## Plugin (`plugin/`, MIT)

The OpenClaw realtime-voice provider. It has **no third-party runtime dependencies** —
plain ESM using Node's built-in `WebSocket` / `Buffer`, with only a `peerDependencies` on
the host `openclaw` (not bundled). So the shipped plugin tree is MIT with nothing further
to attribute. (Re-check `plugin/package.json` if runtime deps are ever added.)

## Engine (separate — not in this repository)

The engine is a separate component with its own license terms. Its attributions
(`engine/THIRD_PARTY_NOTICES.txt` in the teagram-engine repo) are published as the
`engine.notices` asset in the release manifest and installed by `install.sh` to
`/opt/teaport/THIRD_PARTY_NOTICES.txt`; `teaport doctor` checks it is present.
