---
title: Troubleshooting
description: What to check first if teaport does not work correctly.
---

:::caution[Stub, not yet written]
This page does not yet exist in the [teaport repo](https://github.com/Teaspoon-AI/teaport). The files `INSTALL.md` and `FAQ.md`, in that repo, list troubleshooting, updates, and uninstall steps as not yet written. The information below comes only from other pages on this site. It is not a complete troubleshooting guide yet. If this page does not solve your problem, [open an issue](https://github.com/Teaspoon-AI/teaport/issues).
:::

## First steps

- **`teaport status`**: this command shows whether the engine, the brain, and the plugin services are running.
- **`teaport doctor`**: this command runs the installer's self-test. You can run it any time after installation, not only during a fresh install.
- **Re-run the installer.** Run `bash <(curl -fsSL https://get.teaspoon.tech/teaport)`. This command repairs an existing installation. It does not create a duplicate installation. Use this command to recover from a broken or partial installation. See [Installation](/getting-started/installation/).

## You cannot reach `https://teaport.local`

The appliance uses a self-signed HTTPS certificate. The browser needs HTTPS to allow use of the microphone. Check that you accepted the one-time certificate warning. Check that the device you use to browse is on the same network as the Jetson.

## If this page does not solve the problem

There is no expanded troubleshooting guide yet. This product is in the pre-launch stage. [Open an issue on GitHub](https://github.com/Teaspoon-AI/teaport/issues). Include the output of `teaport status` and `teaport doctor` in the issue.
