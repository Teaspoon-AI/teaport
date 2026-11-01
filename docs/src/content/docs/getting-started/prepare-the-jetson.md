---
title: Prepare the Jetson
description: Check the JetPack release and trim the desktop before you install teaport.
---

Prepare the Jetson for installation: confirm the JetPack release, stop the desktop from starting at boot, and keep the swap settings as they are.

## Before you begin

- A Jetson Orin Nano Dev Kit (8 GB) flashed with JetPack 7.2. To flash it, follow NVIDIA's flashing guide.
- Screen or SSH access to the device.

<!-- source: README.md:17,24#a49fa2b7; install.sh:235#227cbba2 -->

## Check the JetPack release

JetPack 7.2 ships the Linux for Tegra (L4T) R38 or R39 release, and the installer accepts both.

To check the release, run this command on the device:

```bash
cat /etc/nv_tegra_release
```

The release appears in the output as `R38` or `R39`. If the output shows a different release, flash JetPack 7.2 before you continue. The installer stops on unsupported releases.

<!-- source: install.sh:231-246#fe7128e6 -->

## Trim the desktop

To stop the desktop from starting at boot, follow these steps. The change is recommended, not required:

1. On the device, run this command:

   ```bash
   sudo systemctl set-default multi-user.target
   ```

2. Reboot the device:

   ```bash
   sudo reboot
   ```

The desktop no longer starts at boot. SSH access works as before.

## Keep the swap settings

Keep the zram swap that the device already uses. Don't add an SD-card swap file.

## What's next

- [Install teaport](/getting-started/installation/)
- [Troubleshooting](/guides/troubleshooting/)
