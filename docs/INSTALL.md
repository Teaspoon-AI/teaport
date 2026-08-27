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
- **Step 5 (optional) — Add a phone line.** SIP telephony is **opt-in**: the
  installer places the SIP units but leaves them off. To answer real phone calls,
  point the box at your SIP trunk / SBC:

  ```
  teaport sip configure
  ```

  The wizard asks for your registrar host, domain, username and password,
  test-registers, and — only if that succeeds — enables the line. Manage it with
  `teaport sip status` and `teaport sip disable`. The local assistant and the
  phone line share one speech slot; see **docs/CONFIG.md → SIP telephony** for
  how that works and how to dedicate a box to the phone.

TODO: expand each step; add troubleshooting, uninstall, and update paths.
