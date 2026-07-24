# teaport

A local voice assistant for the NVIDIA Jetson Orin. You speak to the device.
The device answers with speech. Your voice does not leave the device.

This repository contains the product and the brain. The brain is a Pipecat
voice pipeline. It gives you barge-in, memory recall, and tools. The
repository also contains the installer, the release manifest, and the
OpenClaw realtime-voice plugin. The plugin turns the assistant into a full
agent.

The brain uses **teaport-engine** for speech-to-text and text-to-speech.
`install.sh` downloads and installs the engine for you. You can use a
different engine if it is compatible with the teaport-engine interface.

- **Local voice loop:** speech recognition and speech synthesis run on the device
- **Target:** Jetson Orin (Nano / NX / AGX), JetPack 7.2 / CUDA 13
- **LLM:** you supply the LLM — a cloud key or a local OpenAI-compatible endpoint
- **Agent:** runs as an OpenClaw realtime-voice provider under NemoClaw (NVIDIA's OpenClaw packaging)

## Quickstart — your own Jetson Orin Nano (8 GB)

```bash
# 1. Flash JetPack 7.2 (see docs/INSTALL.md)
# 2. Install NemoClaw (NVIDIA's installer — your consent, your terminal)
bash <(curl -fsSL https://www.nvidia.com/nemoclaw.sh)
# 3. Install teaport
bash <(curl -fsSL https://get.teaspoon.tech/teaport)
```

Accept the engine license. Answer the LLM question. The installer then sets up
the engine, the voices, the brain, the OpenClaw plugin, and the services. It
ends with a self-test. You can run the installer again at any time. It repairs
an installation and does not duplicate it. See
[docs/INSTALL.md](docs/INSTALL.md) for the full procedure.

## Layout

- `brain/` — the voice brain, an installable Python package (`teaport_brain`)
- `plugin/` — the OpenClaw realtime-voice provider (npm `@teaspoon-ai/openclaw-teaport-realtime`)
- `manifest/` — the release manifest for the installer and `teaport update`
- `install.sh` · `cli/teaport` · `systemd/` — install and operate the product
- `docs/` — INSTALL, ARCHITECTURE, CONFIG, FAQ

## Licensing

teaport (the brain and the plugin) is **MIT**. The engine is a separate
component with its own license terms. The installer downloads the engine at
install time. The engine is not in this repository. See
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

> **Status: pre-launch.** The brain (`brain/`) and the plugin (`plugin/`) are
> complete and validated on Python 3.12 (x86 and the arm64 appliance). The
> installer, the `teaport` CLI, and the systemd units are ready. The hosted
> engine artifacts are not published yet.
