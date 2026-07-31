---
title: Installing teaport
description: Install teaport on a Jetson Orin Nano (8 GB).
---

:::note[Pre-launch scaffold]
This page is the outline from the [teaport repo](https://github.com/Teaspoon-AI/teaport). The full walkthrough, with screenshots and troubleshooting steps, will come with the first hosted release.
:::

You need a Jetson Orin Nano Dev Kit (8 GB) that you own, a screen or SSH (Secure Shell) access, and a home network. Plan 45 to 60 minutes of active work time. You must also allow time to download the models. You must supply your own Jetson and your own large language model (LLM).

1. **Flash JetPack 7.2.** This step is necessary. The engine works only with JetPack 7.2, which includes CUDA 13. Follow NVIDIA's flashing guide. Teaspoon AI does not host a copy of JetPack.
2. **Trim the desktop (recommended).** Run `sudo systemctl set-default multi-user.target`. Reboot the Jetson. Keep zram active. Do not add an SD-card swap file.
3. **Install NemoClaw.** NemoClaw is NVIDIA's installer. Run this command in your terminal:

   ```bash
   bash <(curl -fsSL https://www.nvidia.com/nemoclaw.sh)
   ```

   Accept the license terms when the installer asks.

4. **Install teaport.**

   ```bash
   bash <(curl -fsSL https://get.teaspoon.tech/teaport)
   ```

   Accept the engine license. Answer the question about your LLM. The installer then does these tasks:

   - It sets up the engine and the voices.
   - It sets up the brain and the OpenClaw plugin.
   - It starts the required services.
   - It runs a self-test.

   You can run the installer again at any time. It repairs an existing installation. It does not create a duplicate installation.

5. **Connect to teaport and talk.** From any device on your network, open `https://teaport.local` in a browser. Accept the one-time certificate warning. The appliance uses a self-signed HTTPS certificate. The browser needs HTTPS to allow use of the microphone. The installer sets up this certificate and the `teaport.local` name for you. Pair the device. Then start a Talk session.

If teaport does not work correctly, run `teaport status` or `teaport doctor`. See [Troubleshooting](/guides/troubleshooting/) for more help.

## What you need

- A **Jetson Orin Nano (8 GB)** that you own. It must be flashed with a matching JetPack version (L4T r38 and CUDA 13).
- An **OpenClaw or NemoClaw agent** on the board. The teaport installer adds itself to the agent as a plugin. If you do not yet have an agent, the installer shows you the NemoClaw one-line install command. Run the teaport installer again after that.
- Your own **large language model (LLM)**. Use a cloud key, for example from Groq, OpenRouter, or Cerebras. You can also use a local OpenAI-compatible endpoint. See [Configuration](/guides/configuration/).
- A **phone, laptop, or other device with a browser**, on the same network. Pair this device once from the OpenClaw dashboard. Start a Talk session from this device. This device's microphone and speaker carry the audio. No device connects physically to the Jetson.
