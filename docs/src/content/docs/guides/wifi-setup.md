---
title: Connect the device to Wi-Fi
description: Join the device to a Wi-Fi network through its temporary setup network, from a phone.
---

If the device has no internet connection, put it in setup mode: it opens a temporary setup network and a page that connects it to Wi-Fi. You join that network from a phone, pick your Wi-Fi network, and type its password; the device does the rest. The whole flow runs on the device, so it works with no internet and no cloud model.

<!-- source: brain/teaport_brain/wifi_setup.py:3-5#91575268 -->

## Before you begin

- The device is powered on and has no internet connection over Wi-Fi.
- A phone.
- The paper insert that shipped with the device, if you have it: it prints the setup network's password.
- To start by voice, a microphone and a speaker on the device through the local-audio audio bridge. See [Use a microphone on the device](/guides/local-audio/).

## Put the device in setup mode

Say "set up Wi-Fi" or "connect to Wi-Fi" at the device. While the device has no internet, either phrase puts it in setup mode on its own: after you say yes, the device opens the setup network, says that it is in setup mode, and announces the network's name, the password, and the page's address. A device with a screen shows the same details under the title "Setup mode".

<!-- source: brain/teaport_brain/wifi_voice.py:5-13#1ccb9b5a; brain/teaport_brain/wifi_voice.py:333-335,501#aced4884; brain/teaport_brain/wifi_setup.py:85#bf4c222f; brain/teaport_brain/config_schema.toml:150#b4c3920e -->

If the device is already online, the phrase is ordinary speech. The agent hears it and can start the same procedure with its Wi-Fi setup tool; ask the agent to run it.

<!-- source: brain/teaport_brain/config_schema.toml:1696-1703#5be20f46 -->

## Join the setup network

1. On the phone, join the Wi-Fi network named `teaport-XXXX`: `teaport` plus the last four characters of the device's Wi-Fi MAC address, for example `teaport-ab12`.
2. The Wi-Fi page usually opens by itself through the phone's captive-portal check. If it does not, open `http://10.42.0.1/`.
3. On the page, pick your Wi-Fi network and type its password. The setup network goes down and the device joins yours.

The setup network uses WPA2. Its password is the one printed on the paper insert, stored on the device as `WIFI_SETUP_PASSWORD` in `/etc/teaport/wifi-setup.env`. Without that stored password, the device speaks fresh random digits for each run, so listen for it in the announcement.

<!-- source: brain/teaport_brain/wifi_setup.py:7-22#cfde57cc; brain/teaport_brain/config_schema.toml:98-101#61c30a35; install.sh:2667#c8f9533e -->

## If the device connects

The page confirms the connection and the device leaves setup mode. The device says that it is connected to your network and online again.

<!-- source: brain/teaport_brain/wifi_setup.py:21#f6b9d41f; brain/teaport_brain/wifi_voice.py:510-512#8b5358ef -->

## If the join fails

The device drops the network profile it tried, the setup network comes back, and the page says what failed. Pick the network and type its password again.

<!-- source: brain/teaport_brain/wifi_setup.py:21-22#50476f67 -->

## If nothing has connected after 10 minutes

The device leaves setup mode on its own, undoes a half-finished join, and restores the connection it had before. The service stops itself at the 15-minute mark at the latest.

<!-- source: brain/teaport_brain/wifi_setup.py:23-24#f8886e10; systemd/teaport-wifi-setup.service.in:73-74#364bb92a -->

## Leave setup mode

Say "cancel" at the device while it is in setup mode, stop it from the configuration page, or run this on the device:

```bash
sudo systemctl stop teaport-wifi-setup
```

The device takes the setup network down, undoes a half-finished join, and restores the previous connection.

<!-- source: systemd/teaport-wifi-setup.service.in:61-63#70ebb3ad; brain/teaport_brain/wifi_setup.py:503#21bb505f -->

## What's next

- [Use a microphone on the device](/guides/local-audio/), for the spoken phrase.
- [Settings reference](/reference/settings/), for the Wi-Fi setup settings, including the setup-network password.
- [Troubleshooting](/guides/troubleshooting/), if the device does not come online.
