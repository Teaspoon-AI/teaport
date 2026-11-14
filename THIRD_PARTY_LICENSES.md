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
| num2words | number-to-words conversion, a required pipecat dependency — **LGPL** (see below) | LGPL-2.1 |
| certifi, tqdm | the CA certificate bundle; progress bars (both come with pipecat's dependencies) | MPL-2.0, MPL-2.0 AND MIT |
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

### soxr, libsndfile and num2words (LGPL)

Three libraries in the brain's installed environment are under the GNU Lesser General
Public License (LGPL), rather than a permissive license like MIT, BSD or Apache:

- **soxr**, the Python resampling package, with the libsoxr library it builds in
  (LGPL-2.1-or-later). It is a required dependency of pipecat-ai.
- **libsndfile**, the audio file library, which comes inside the soundfile wheel
  (LGPL-2.1-or-later).
- **num2words**, a pure-Python number-to-words package and a required dependency of
  pipecat-ai. The license file it ships is the LGPL, version 2.1.

The LGPL is a weak copyleft license. It does not change the MIT license of teaport's own
code. It asks that anyone who receives the library can replace it with their own version,
and that whoever distributes the library also provides its license text and a way to get
its source.

The `teaport-brain` package includes all three libraries. CI builds the package from
`brain/uv.lock` with `packaging/build-deb.sh`, and it contains the PyPI wheels pinned in
that lock, unmodified, installed as an ordinary Python environment under
`/opt/teaport/brain`. If you receive the package, here is what you get and what you can
do:

- **License texts.** Each Python package keeps its license files in its `.dist-info`
  directory. soxr's, together with libsoxr's, are under
  `soxr-<version>.dist-info/licenses/`; num2words' is `num2words-<version>.dist-info/COPYING`.
  libsndfile's is `_soundfile_data/COPYING` in the soundfile package; soundfile's own
  BSD license is in its `.dist-info` directory. The package also installs this file and a
  `copyright` file under `/usr/share/doc/teaport-brain/`.
- **Source.** The versions are the ones in `brain/uv.lock`. Their source is the source
  distribution on PyPI (`soxr`, which includes the libsoxr source it builds in,
  `soundfile`, and `num2words`), and upstream at <https://github.com/dofuuz/python-soxr>
  (soxr and libsoxr), <https://github.com/libsndfile/libsndfile> (libsndfile),
  <https://github.com/bastibe/python-soundfile> (soundfile) and
  <https://github.com/savoirfairelinux/num2words> (num2words). Teaspoon AI will also send
  the source of those versions to anyone who asks at stefano@teaspoon.tech, for three years
  from the release that shipped them.
- **Replacing the libraries.** num2words is plain Python: replace its `num2words`
  directory in the environment's `site-packages`, or install your own build with the
  `uv pip install` command below. libsndfile is a separate shared object,
  `_soundfile_data/libsndfile_<arch>.so` in the soundfile package: put your own build in
  its place. soxr links libsoxr into its Python extension: rebuild the wheel from the
  source distribution against your own libsoxr, then install it into the environment with
  `sudo uv pip install --python /opt/teaport/brain/bin/python <wheel>`. The environment
  does not include pip, and uv is not part of the package. After a replacement,
  `teaport doctor` and `dpkg -V teaport-brain` list the changed files, as they do for any
  other change, and the next install of the package restores the shipped files. To keep
  a replacement, pin it in `brain/pyproject.toml`, run `uv lock`, and build the package
  with `packaging/build-deb.sh`; the package is built from the lock.

**What this means for how teaport is built.** The brain is an open Python environment of
unmodified wheels, with its own source in this repository and its dependencies' source on
PyPI. It is never frozen or bundled into a single closed binary, such as a PyInstaller
build or a closed image, because that would take away your ability to replace these
libraries, which is what the LGPL requires.

certifi and tqdm are under the Mozilla Public License 2.0, which asks for the source of the
files it covers to stay available. The package includes both unmodified, and their source is
the source distribution on PyPI. Their license texts are in their `.dist-info` directories.

Maintainers: re-audit the full dependency tree at each public release. A pipecat upgrade
can add a dependency under a new license, and the versions shipped follow the lock.

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
