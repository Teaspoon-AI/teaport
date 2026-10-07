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
  test-registers, and — only if that succeeds — enables the line (`--conf FILE`
  adopts a gateway `.conf` you already have). From then on the line comes back on
  its own after a reboot or a crash. Manage it with `teaport sip status`,
  `teaport sip restart`, `teaport sip aec on|off` and `teaport sip disable`. The
  local assistant and the phone line share one speech slot; see **docs/CONFIG.md
  → SIP telephony** for how that works and how to dedicate a box to the phone.
- **Step 6 (optional) — Talk to the box itself.** Plug a USB mic array into the
  Jetson (built for the ReSpeaker XVF3800; put the speaker on its 3.5 mm jack so
  its echo canceller hears what plays) and re-run the installer with
  `TEAPORT_ENABLE_LOCAL_AUDIO=1`. It enables `teaport-local-audio`, which pipes
  the card to the assistant and plays the replies back. The device and mic
  channel live in `/etc/teaport/local-audio.env` (**docs/CONFIG.md → Local audio
  bridge**). It shares the Talk slot: it dials the assistant when it starts,
  and once a session ends (a browser Talk session took the slot, or the
  assistant hung up) it waits until someone speaks near the mic before dialling
  again — which takes the slot back from a browser session. Turn it off with
  `sudo systemctl disable --now teaport-local-audio`; re-running the installer
  keeps it off until you pass `TEAPORT_ENABLE_LOCAL_AUDIO=1` again.
  Through it the assistant can also turn its speaker up or down when asked
  (`set_volume`, kept across restarts), and restart the conversation from
  scratch (`restart_session`, a testing aid that is off until you set
  `TEAPORT_TOOL_RESTART_SESSION=1` in `/etc/teaport/brain.env`).
- **Step 7 (optional) — Give it a face.** Wire a 128x64 SSD1306 OLED to the
  Jetson's I2C header and re-run the installer with
  `TEAPORT_ENABLE_OLED_AVATAR=1`. It fetches
  [teaport-oled-avatar](https://github.com/Teaspoon-AI/teaport-oled-avatar) and
  runs its installer, which finds the panel (`0x3c`/`0x3d` on any I2C bus;
  `TEAPORT_OLED_PORT`/`TEAPORT_OLED_ADDR` to name it) and enables
  `teaport-oled-avatar`. With the local mic bridge (Step 6) running, the eyes
  follow the conversation and the mouth moves with the voice. No panel found:
  the service is installed but left off. Re-run with the flag to update it.

## Updating the brain

The brain (the voice pipeline) updates on its own, without the engine download or
any of the installer's prompts:

```
git pull && ./install.sh --only brain          # from a checkout of this repo
bash <(curl -fsSL https://get.teaspoon.tech/teaport) --only brain   # or the one-liner
```

It builds a new Python environment from `brain/uv.lock` next to the running one,
self-checks it, then swaps it in and restarts the brain (and the phone line, if it
is running — a call in progress is dropped). If the brain does not come back healthy
it puts the previous environment back by itself. The newest three environments are
kept, plus the running one and the rollback target. `--only brain` leaves the systemd units and `/etc/teaport` alone, so a brain
change that needs a new setting there goes in with a full `./install.sh` run instead.
A full run builds the brain the same way and restarts everything on it — the phone
line too, if it is on — but does not roll back on its own; `./install.sh --rollback
brain` does that by hand.
To go back by hand:
`./install.sh --rollback brain` (run it again to undo the rollback). Add `--dry-run`
to either to see the plan first.

A checkout that is behind the branch it tracks is refused, since installing it would
quietly put an older brain on the box — `git pull` first.

`teaport doctor` reports whether the brain's environment still matches the lock it
was built from. Anything installed into it by hand shows up there as drift; the fix
is another `--only brain`.

TODO: expand each step; add troubleshooting and uninstall.
