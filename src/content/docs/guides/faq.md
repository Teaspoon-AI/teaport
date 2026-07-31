---
title: FAQ
description: Frequently asked questions about teaport.
---

:::note[Pre-launch scaffold]
These are seed questions, ported from the [teaport repo](https://github.com/Teaspoon-AI/teaport). More come with the public docs pass.
:::

**Does my voice leave the device?**
No. Speech recognition and speech synthesis run on the device. Only your
chosen LLM is remote. Point the brain at your own endpoint and the LLM is
local too.

**Do I need a GPU cloud key?**
No. You supply an LLM: a cloud key (OpenRouter / Groq / …) or a local
OpenAI-compatible server. See [Configuration](/guides/configuration/).

**Which Jetson?**
Orin (Nano / NX / AGX), JetPack 7.2 / CUDA 13. The Nano 8 GB is the
reference device.

**How is this repository licensed?**
The brain and the plugin in the [teaport repo](https://github.com/Teaspoon-AI/teaport)
are MIT. The engine is a separate component with its own license terms — see
the license shown at install time. The installer downloads the engine for
you.

**Can I run it without NemoClaw?**
Yes — voice-only. Install NemoClaw later and run the installer again to add
the agent.
