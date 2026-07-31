---
title: Troubleshooting
description: What to check first when teaport isn't working.
---

:::caution[Stub — not yet written]
This page doesn't exist yet in the [teaport repo](https://github.com/Teaspoon-AI/teaport) — both `INSTALL.md` and `FAQ.md` there flag troubleshooting, updates, and uninstall as still to come. What's below is limited to what's already documented elsewhere; it isn't a real troubleshooting guide yet. If you're stuck, [open an issue](https://github.com/Teaspoon-AI/teaport/issues).
:::

## First steps

- **`teaport status`** — check whether the engine, brain, and plugin services
  are up.
- **`teaport doctor`** — run the installer's self-test outside of a fresh
  install.
- **Re-run the installer.** `bash <(curl -fsSL https://get.teaspoon.tech/teaport)`
  repairs an existing install rather than duplicating it — this is the
  documented recovery path for a broken or partial install. See
  [Installation](/getting-started/installation/).

## Can't reach `https://teaport.local`

The appliance serves HTTPS with a self-signed certificate, and the browser
needs HTTPS for the microphone to work. Make sure you accepted the one-time
certificate warning, and that the device you're browsing from is on the same
network as the Jetson.

## Still stuck

There's no expanded troubleshooting guide yet — this product is pre-launch.
[Open an issue on GitHub](https://github.com/Teaspoon-AI/teaport/issues) with
the output of `teaport status` and `teaport doctor`.
