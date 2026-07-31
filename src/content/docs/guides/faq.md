---
title: FAQ
description: Frequently asked questions about teaport.
---

:::note[Pre-launch scaffold]
This page has a short, early list of questions from the [teaport repo](https://github.com/Teaspoon-AI/teaport). More questions will come with the public docs pass.
:::

**Does my voice leave the device?**
No. Speech recognition and speech synthesis run on the device. Only your chosen large language model (LLM) is remote. Set the brain to use your own endpoint. Then the LLM is local too.

**Do I need a graphics processor (GPU) in the cloud?**
No. You must supply an LLM. Use a cloud key, for example from OpenRouter or Groq. You can also use a local OpenAI-compatible server. See [Configuration](/guides/configuration/).

**Which Jetson boards work with teaport?**
teaport works with the Jetson Orin Nano, NX, and AGX boards. All boards need JetPack 7.2 and CUDA 13. The Nano with 8 GB of memory is the reference device.

**What license covers this repository?**
The brain and the plugin, in the [teaport repo](https://github.com/Teaspoon-AI/teaport), use the MIT license. The engine is a separate component with its own license terms. See the license text shown at install time. The installer downloads the engine for you.

**Can I run teaport without NemoClaw?**
Yes. You can run teaport in voice-only mode. Install NemoClaw later. Then run the teaport installer again to add the agent.
