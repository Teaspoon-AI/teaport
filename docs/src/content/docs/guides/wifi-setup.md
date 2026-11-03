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

Say "set up Wi-Fi" or "connect to Wi-Fi" at the device. While the device has no internet, either phrase starts setup mode without the agent: the device asks whether to go into setup mode, and after you say yes, it opens its setup network and says that it is in setup mode.

<!-- source: brain/teaport_brain/wifi_voice.py:5-13#039c023d; brain/teaport_brain/wifi_voice.py:398-400,647#aced4884; brain/teaport_brain/config_schema.toml:150#c71fe78e -->

With `TEAPORT_SETUP_ON_FIRST_BOOT` on, a new device does this by itself. When it has never
been online and still has no internet a minute after it starts, it goes into setup mode
without a wake word or a question, and it introduces itself first.
If you cancel, or nothing connects in time, it tells you to say "set up Wi-Fi" to
try again, and it starts by itself again only at the next start-up. A setup you start by
voice in the first minute counts as that start-up's try. The first time the device is
online, it stops doing this for good; if that happens during setup mode, for example when
you plug in a network cable, it leaves setup mode by itself.

<!-- source: brain/teaport_brain/wifi_voice.py:106-110#3b3a158c; brain/teaport_brain/first_boot.py:192-215#eb3223f5; brain/teaport_brain/wifi_voice.py:434-442#4eeee6c0 -->

If the device is already online, the phrase is ordinary speech. The agent hears it and can start the same procedure with its Wi-Fi setup tool; ask the agent to run it. If no model is set up yet, there is no agent to ask, so the phrase starts setup mode online too.

<!-- source: brain/teaport_brain/config_schema.toml:1719-1726#389fa64a; brain/teaport_brain/wifi_voice.py:745-749#e2d218b7 -->

## Join the setup network

A device with a screen shows, under the title "Setup mode", the setup network's name, its password, and an address to open, with a QR code beside them when the screen can draw one. The device tells you what to do in the same order:

1. Join the setup network from your phone. With a QR code on the screen, scan it with the phone's camera. Otherwise, join the network shown on the screen, named `teaport-XXXX` (`teaport` plus the last four characters of the device's Wi-Fi MAC address, for example `teaport-ab12`), and type the password shown on the screen.
2. In the phone's browser, go to the address on the screen. It is usually `http://teaport-XXXX.local`, or `http://10.42.0.1`, which always works.
3. Pick your Wi-Fi network and type its password. The setup network goes down and the device joins yours.

Say "repeat" to hear the network's name, the password, and the address read out. A device without a screen reads them out straight away. Some phones open the page on their own after they join the setup network; if yours does not, use the address.

<!-- source: brain/teaport_brain/wifi_voice.py:260-295#a7a905c9; brain/teaport_brain/wifi_voice.py:778#d902fb7c; brain/teaport_brain/wifi_setup.py:468-479#3f154da2; brain/teaport_brain/wifi_setup.py:585-596#0e1e6742; brain/teaport_brain/wifi_setup.py:85#bf4c222f; brain/teaport_brain/wifi_setup.py:15-19#a67a8952 -->

The setup network uses WPA2. Its password is the one printed on the paper insert, stored on the device as `WIFI_SETUP_PASSWORD` in `/etc/teaport/wifi-setup.env`. Without that stored password, the device makes fresh random digits for each run, shown on the screen, or read out when the device has no screen or you say "repeat".

<!-- source: brain/teaport_brain/wifi_setup.py:7-22#cfde57cc; brain/teaport_brain/config_schema.toml:98-101#61c30a35; install.sh:2817#c8f9533e -->

## If the device connects

The page tells you that the device is joining your network, so switch your phone back to it. The device leaves setup mode and says that it is connected to your network and online again; a screen shows "Connected to" and the network's name under the title "Wi-Fi" for a few seconds.

<!-- source: brain/teaport_brain/wifi_setup.py:259-268#99955446; brain/teaport_brain/wifi_voice.py:662-663#d7938890; brain/teaport_brain/wifi_setup.py:617-618#bd5d3568; brain/teaport_brain/wifi_setup.py:87#296b6e77 -->

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

<!-- source: systemd/teaport-wifi-setup.service.in:61-63#70ebb3ad; brain/teaport_brain/wifi_setup.py:539#21bb505f -->

## What's next

- [Use a microphone on the device](/guides/local-audio/), for the spoken phrase.
- [Settings reference](/reference/settings/), for the Wi-Fi setup settings, including the setup-network password.
- [Troubleshooting](/guides/troubleshooting/), if the device does not come online.
