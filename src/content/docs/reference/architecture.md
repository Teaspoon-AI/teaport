---
title: Architecture
description: How the engine, the brain, and the OpenClaw plugin work together in teaport.
---

:::note[Pre-launch scaffold]
This page is an outline from the [teaport repo](https://github.com/Teaspoon-AI/teaport). The public docs pass will add:

- A block diagram.
- A description of the audio timing flow.
- A list of network ports.
- A description of how the brain asks OpenClaw for extra information.
:::

teaport has one appliance and three parts:

- **Engine** (port `8000`): this part does speech-to-text and speech synthesis. The installer downloads this part. You can use a different engine if it is compatible with the teaport-engine interface.
- **Brain** (`brain/teaport_brain`, port `7861`, path `/talk`): this part is the Pipecat pipeline. It connects your speech, your large language model (LLM), and the spoken reply. It allows you to interrupt teaport while it speaks (this feature is called barge-in). It keeps a record of what it confirms that it heard from you (this record is called the heard-grounding ledger). It also handles memory recall, tools, and the persona. It sends plain text to the engine. It receives audio and per-word time stamps back from the engine.
- **Plugin** (folder `plugin/`): this part is the OpenClaw realtime-voice provider. It connects a NemoClaw agent to the brain's `/talk` WebSocket.

Your voice does not leave the device. You control where the LLM runs. See [Configuration](/guides/configuration/).
