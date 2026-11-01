---
title: FAQ
description: Frequently asked questions about teaport.
---

Common questions about teaport. Each answer ends with a link to the page that has the details.

## Does my voice leave the device?

No. Speech recognition and speech synthesis run on the device: the brain uses the teaport-engine service for both. Only the conversation text goes to the large language model (LLM) endpoint that you configure. The settings are in [Configure teaport](/guides/configuration/).

<!-- source: README.md:3-4,12,16,18#dd5edb4b -->

## Do I need a GPU in the cloud?

No. The speech models run on the Jetson, so nothing in the cloud needs a GPU. You supply the LLM: a cloud key from a provider such as Groq or OpenRouter, or a local OpenAI-compatible server. The two ways to set one are in [Set up the language model](/guides/configuration/#set-up-the-language-model).

<!-- source: README.md:16,18#0cb1a833; brain/teaport_brain/config_schema.toml:216#6ff18580 -->

## Which Jetson devices does teaport support?

teaport targets the NVIDIA Jetson Orin family (Nano, NX, and AGX) and needs JetPack 7.2 with CUDA 13. The Jetson Orin Nano Dev Kit (8 GB) is the reference device. Before you buy hardware, read [Prepare the Jetson](/getting-started/prepare-the-jetson/).

<!-- source: README.md:17,21#ae06b0a1; install.sh:6-7#ea9fc9cc -->

## How is teaport licensed?

The brain and the plugin are MIT. The engine is a separate component with its own license. The installer displays the full text, asks you to accept it, and remembers the accepted version, so a re-run prompts again only after the license changes. The license prompt is part of [Install teaport](/getting-started/installation/).

<!-- source: README.md:54-55#db00dcd0; install.sh:164-184#ca249099 -->

## Can I run teaport without NemoClaw?

Yes. Install without an agent and teaport runs in voice-only mode: the brain offers only the tools that live on the device, and the installer writes `TEAPORT_AGENT=none`. Anything that needs the OpenClaw gateway, such as web search, memory, or the agent consult, is not available in this mode. Installing OpenClaw later and running the installer again turns the agent on; the choices are in [Install an agent](/getting-started/installation/#install-an-agent).

<!-- source: install.sh:1754-1773#cbbb90e9; brain/teaport_brain/config_schema.toml:138,144#7d3215ea -->

## Can teaport answer phone calls?

Yes. Phone calls are opt-in: the installer puts the `teaport-sip` gateway unit in place, but until its configuration exists nothing starts, so a new device is a local voice assistant only. You turn the line on by configuring a Session Initiation Protocol (SIP) trunk or session border controller (SBC) account; the brain answers the calls itself. The setup, and the hardware and account you need, are in [Set up phone calls](/guides/phone-calls/).

<!-- source: systemd/teaport-sip.service.in:3-7,16-19#2dd620a9 -->

## Why does my second session get refused?

The engine serves a single speech-to-text session at a time, and the brain holds one conversation at a time. A session arbiter decides which front end gets the engine: a Talk client, the device's own microphone, the Discord bridge, or a phone call. While a conversation is live, the brain refuses the other front ends. A phone call is the exception: the brain asks the people in the conversation whether to take the call. The session arbiter is described in [Architecture](/reference/architecture/), and how a call interrupts a conversation is in [Set up phone calls](/guides/phone-calls/).

<!-- source: brain/teaport_brain/session_arbiter.py:5-33#76be4da1 -->

## How do updates work?

The brain updates on its own, without a new engine download. Run the installer again with `--only brain`, and it builds a new environment beside the running one, checks it, swaps it in, and restarts `teaport-brain`. The restart drops a Talk session in progress; a phone call stays up, and the restarted brain picks it up again. If the new environment is not healthy, the previous one comes back by itself. The `teaport update` command compares the installation with the release manifest; the details are in [Update teaport](/guides/updating/).

<!-- source: install.sh:15-21,888#544cd662; brain/teaport_brain/sip_server.py:45-52#9213415f; cli/teaport:7#a00a5a5b -->
