---
title: Prepare the Jetson
description: Set the power mode, swap, and desktop before you install teaport.
---

Do these steps after you flash JetPack 7.2 and before you install teaport. They give the
device more memory and more processor speed. Each step is optional. The device works
without them. The device works better with them.

<!-- Measured on a Jetson Orin Nano Dev Kit (8 GB), JetPack 7.2, NVMe. -->

## Before you start

Check that the device runs JetPack 7.2:

```bash
apt-cache policy nvidia-jetpack | head -2
```

The candidate version must start with `7.2`. Also check the Linux for Tegra (L4T) version:

```bash
cat /etc/nv_tegra_release
```

JetPack 7.2 reports **R39**. This is correct. Do not look for R38.

## 1. Set the power mode to MAXN SUPER

The default power mode is 25 W. MAXN SUPER removes the power limit.

```bash
sudo nvpmodel -m 2
sudo nvpmodel -q
```

The command `nvpmodel -q` must print `NV Power Mode: MAXN_SUPER`.

:::note
If mode 2 is not available, your device was flashed with the JetPack 7.2 ISO. NVIDIA
records this as a known problem. Flash the device again with NVIDIA SDK Manager or with the
Jetson Linux flashing tools on an x86_64 host.
:::

Do not run `jetson_clocks`. It holds the clocks at maximum. This raises the temperature and
the power use. It made speech recognition only 2.5% faster in our measurements. The
processor already reaches its maximum speed under load without it.

## 2. Give the device more swap

JetPack 7.2 makes a swap file of 2 GB. This is small for a device that loads a model of
4.4 GB.

**On a device with an NVMe drive**, make the swap file larger:

```bash
sudo swapoff /swapfile
sudo rm -f /swapfile
sudo fallocate -l 16G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

Check that `/etc/fstab` contains this line. JetPack 7.2 usually adds it for you:

```
/swapfile none swap sw 0 0
```

**On a device that starts from an SD card**, do not make the swap file larger. Writes to an
SD card are slow. They also wear out the card. Keep the swap file at 2 GB.

Then tell the kernel to keep the model in memory:

```bash
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-teaport.conf
sudo sysctl -p /etc/sysctl.d/99-teaport.conf
```

:::caution[The swap file uses disk space]
`fallocate` reserves the space immediately. A swap file of 16 GB uses 16 GB of the drive.
The installer needs approximately 15 GB more for the engine and the models. Make sure the
drive has sufficient free space before you continue.
:::

## 3. Stop the desktop

teaport does not use the graphical desktop. Stop it to release memory:

```bash
sudo systemctl set-default multi-user.target
sudo systemctl disable nvargus-daemon.service
sudo reboot
```

`nvargus-daemon` is the camera service. A voice device does not use it.

Your SSH (Secure Shell) connection continues to work after this change. Only the screen
output stops.

These two changes released approximately 600 MB in our measurements.

## Result

After the reboot, check the device:

```bash
nvpmodel -q          # NV Power Mode: MAXN_SUPER
free -h              # more free memory than before
swapon --show        # 16G on NVMe, or 2G on an SD card
systemctl get-default # multi-user.target
```

The device is now ready. Continue to [Installation](/getting-started/installation/).

## What we measured

These figures come from a Jetson Orin Nano Dev Kit (8 GB) with an NVMe drive.

| Change | Result |
|---|---|
| Stop the desktop and the camera service | 600 MB more memory available |
| Swap file 2 GB to 16 GB | Uses 14 GB more disk space |
| MAXN SUPER power mode | Temperature rose 1 °C when the device was not busy |
| `jetson_clocks` | Speech recognition 2.5% faster. Not recommended. |

The device did not become too hot at any time. The temperature stayed at 47 °C when the
device was not busy and 62 °C at maximum clock speed. The fan stayed at its quiet setting.

## Related

- [Installation](/getting-started/installation/)
- [NVIDIA: RAM optimization for the Jetson Orin Nano](https://www.jetson-ai-lab.com/tutorials/ram-optimization/)
- [NVIDIA: Jetson Orin Nano Developer Kit Quick Start Guide](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/quick_start.html)
