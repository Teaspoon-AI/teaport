# Installing teaport

> **Scaffold.** The full walkthrough comes with the first hosted release.

You need: a Jetson Orin Nano Dev Kit (8 GB) that you own, a screen or SSH
access, and a home network. Plan 45–60 minutes of active time, plus model
downloads. You bring your own Jetson and your own LLM.

- **Step 0 — Flash JetPack 7.2.** This step is required. The engine supports
  JetPack 7.2 / CUDA 13 only. Follow NVIDIA's flashing guide. We do not mirror
  JetPack.
- **Step 1 — Trim the desktop (recommended).** Run
  `sudo systemctl set-default multi-user.target` and reboot. Keep zram. Do not
  add an SD-card swap file.
- **Step 2 — Install NemoClaw.** This is NVIDIA's installer. Run it on your
  own terminal and give your own consent:
  `bash <(curl -fsSL https://www.nvidia.com/nemoclaw.sh)`.
- **Step 3 — Install teaport.** Run
  `bash <(curl -fsSL https://get.teaspoon.tech/teaport)`.
- **Step 4 — Open the front door and talk.** From any device on your network,
  open **`https://teaport.local`** in a browser and accept the one-time
  certificate warning. The appliance serves HTTPS with a self-signed
  certificate. The browser needs HTTPS for the microphone to work; the
  installer sets up the certificate and the `teaport.local` name for you. Pair
  the device, then start a Talk session. If something looks wrong, run
  `teaport status` or `teaport doctor`.

TODO: expand each step; add troubleshooting, uninstall, and update paths.
