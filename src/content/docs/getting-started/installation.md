---
title: Installing teaport
description: Install teaport on a Jetson Orin Nano (8 GB).
---

:::note[Pre-launch scaffold]
This is the outline from the [teaport repo](https://github.com/Teaspoon-AI/teaport). The full walkthrough — with screenshots and troubleshooting for each step — comes with the first hosted release.
:::

You need: a Jetson Orin Nano Dev Kit (8 GB) that you own, a screen or SSH
access, and a home network. Plan 45–60 minutes of active time, plus model
downloads. You bring your own Jetson and your own LLM.

1. **Flash JetPack 7.2.** Required — the engine supports JetPack 7.2 / CUDA 13
   only. Follow NVIDIA's flashing guide; we do not mirror JetPack.
2. **Trim the desktop (recommended).** Run
   `sudo systemctl set-default multi-user.target` and reboot. Keep zram. Do
   not add an SD-card swap file.
3. **Install NemoClaw.** This is NVIDIA's installer. Run it on your own
   terminal and give your own consent:

   ```bash
   bash <(curl -fsSL https://www.nvidia.com/nemoclaw.sh)
   ```

4. **Install teaport.**

   ```bash
   bash <(curl -fsSL https://get.teaspoon.tech/teaport)
   ```

   Accept the engine license. Answer the LLM question. The installer sets up
   the engine, the voices, the brain, the OpenClaw plugin, and the services,
   then ends with a self-test. You can run it again at any time — it repairs
   an installation rather than duplicating it.

5. **Open the front door and talk.** From any device on your network, open
   `https://teaport.local` in a browser and accept the one-time certificate
   warning. The appliance serves HTTPS with a self-signed certificate — the
   browser needs HTTPS for the microphone to work, and the installer sets up
   the certificate and the `teaport.local` name for you. Pair the device,
   then start a Talk session.

If something looks wrong, run `teaport status` or `teaport doctor` — see
[Troubleshooting](/guides/troubleshooting/).

## What you'll need

- A **Jetson Orin Nano (8 GB)** you own, flashed with a matching JetPack
  (L4T r38 / CUDA 13).
- An **OpenClaw or NemoClaw agent** on the board. The installer adds teaport
  to it as a plugin. No agent yet? The installer points you to NemoClaw's
  one-line install, then you re-run it.
- Your own **LLM** — a cloud key (Groq, OpenRouter, Cerebras, …) or a local
  OpenAI-compatible endpoint. See [Configuration](/guides/configuration/).
- A **phone, laptop, or any browser** on the same network. Pair it once from
  the OpenClaw dashboard and start a Talk session; that device's mic and
  speaker are what you use — nothing plugs into the Jetson itself.
