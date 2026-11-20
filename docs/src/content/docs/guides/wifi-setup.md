---
title: Connect the device to Wi-Fi
description: Join the device to a Wi-Fi network through its temporary setup network, from a phone.
---

If the device has no internet connection, put it in setup mode: it opens a temporary setup network and a page that connects it to Wi-Fi. You join that network from a phone, pick your Wi-Fi network, and type its password; the device does the rest. The whole flow runs on the device, so it works with no internet and no cloud model.

<!-- source: brain/teaport_brain/wifi_setup.py:3-5#91575268 -->

## Before you begin

- The device is powered on and has no internet connection over Wi-Fi.
- A phone.
- The paper insert that shipped with the device, if you have it: it prints the setup network's name, its password, and a QR code. On a Teaspoon AI device the insert has no login for the device: you create the login on the setup page, as [Create your login](#create-your-login) describes.

<!-- source: brain/teaport_brain/provision.py:24-26#b71000d2; brain/teaport_brain/provision.py:233#64e25256 -->
- To start by voice, a microphone and a speaker on the device through the local-audio audio bridge. See [Use a microphone on the device](/guides/local-audio/).

## Put the device in setup mode

Say "set up Wi-Fi" or "connect to Wi-Fi" at the device. While the device has no internet, either phrase starts setup mode without the agent: the device asks whether to go into setup mode, and after you say yes, it opens its setup network and says that it is in setup mode.

<!-- source: brain/teaport_brain/wifi_voice.py:5-13#039c023d; brain/teaport_brain/wifi_voice.py:695-697,959#aced4884; brain/teaport_brain/config_schema.toml:150#c71fe78e -->

With `TEAPORT_SETUP_ON_FIRST_BOOT` on, a new device does this by itself. When it has never
been online and still has no internet a minute after it starts, it goes into setup mode
without a wake word or a question, and it introduces itself first.
If you cancel, or nothing connects in time, it tells you to say "set up Wi-Fi" to
try again, and it starts by itself again only at the next start-up. A setup you start by
voice in the first minute counts as that start-up's try. The first time the device is
online, it stops doing this for good; if that happens during setup mode, for example when
you plug in a network cable, it leaves setup mode by itself.

<!-- source: brain/teaport_brain/wifi_voice.py:118-122#3b3a158c; brain/teaport_brain/first_boot.py:191-214#eb3223f5; brain/teaport_brain/wifi_voice.py:731-739#4eeee6c0 -->

If the device is already online, the phrase is ordinary speech. The agent hears it and can start the same procedure with its Wi-Fi setup tool; ask the agent to run it. If no model is set up yet, there is no agent to ask, so the phrase starts setup mode online too.

<!-- source: brain/teaport_brain/config_schema.toml:1799-1806#389fa64a; brain/teaport_brain/wifi_voice.py:1054-1058#e2d218b7 -->

## Join the setup network

A device with a screen shows, under the title "Setup mode", the setup network's name, its password, and an address to open, with a QR code beside them when the screen can draw one. The device tells you what to do in the same order:

1. Join the setup network from your phone. With a QR code on the screen, scan it with the phone's camera. Otherwise, join the network shown on the screen, named `teaport-XXXX` (`teaport` plus the last four characters of the device's Wi-Fi MAC address, for example `teaport-ab12`), and type the password shown on the screen.
2. In the phone's browser, go to the address on the screen. It is usually `http://teaport-XXXX.local`, or `http://10.42.0.1`, which always works.
3. Pick your Wi-Fi network and type its password. On a device that ships ready to set up, also choose the password for your login, as [Create your login](#create-your-login) describes. The setup network goes down and the device joins yours.

Say "repeat" to hear the network's name, the password, and the address read out. A device without a screen reads them out straight away. Some phones open the page on their own after they join the setup network; if yours does not, use the address.

<!-- source: brain/teaport_brain/wifi_voice.py:339-387#92babf91; brain/teaport_brain/wifi_voice.py:1087#d902fb7c; brain/teaport_brain/wifi_setup.py:832-844#5198d7ad; brain/teaport_brain/wifi_setup.py:976-993#29d0dfed; brain/teaport_brain/wifi_setup.py:127-129#4b966a03; brain/teaport_brain/wifi_setup.py:22-26#8149162a -->

If you start setup by voice, the screen shows its words in the language that the device speaks; if you start it another way, in English. After you pick your network on the setup page, the screens that follow are in the page's language. Network names appear as they are written. This depends on fonts for other scripts on the device: a device without them shows the words in English and drops the characters outside ASCII from a network's name, for example "Cafe" for "Café". If that changes the setup network's name and a QR code is on the screen, the screen shows "(scan the code)" in place of the name.

<!-- source: brain/teaport_brain/wifi_setup.py:589-593#ccf7af71; brain/teaport_brain/wifi_setup.py:734#3f54bcf1; brain/teaport_brain/wifi_setup.py:894-898#14cffbaa; brain/teaport_brain/wifi_setup.py:985-994#87626463; brain/teaport_brain/display.py:115-128,147-148#f3e8f797; brain/teaport_brain/wifi_setup.py:131-138#f5be084a; brain/teaport_brain/wifi_voice.py:775-780#cebe7db2 -->

The setup network uses WPA2. Its password is the one printed on the paper insert, stored on the device as `WIFI_SETUP_PASSWORD` in `/etc/teaport/wifi-setup.env`. Without that stored password, the device makes a fresh random one for each run: twelve characters in three groups of four, for example `k7mq-x4tn-p9wr`, lowercase letters and digits without look-alikes or sound-alikes (no 0 or o, no 1, l or i, no q). The hyphens are part of the password; type it as the screen shows it. The screen shows it, and the device reads it out, character by character with "dash" between the groups, when it has no screen or you say "repeat". A password for the paper insert should use the same grouped form: `teaport setup-card --new-password` makes one, stores it, and prints what goes on the insert (see [teaport setup-card](/reference/cli/#teaport-setup-card)).

<!-- source: brain/teaport_brain/wifi_setup.py:7-12,115-122#e06919a7; brain/teaport_brain/config_schema.toml:98-101#61c30a35; install.sh:3740-3744#0fa4dad8 -->

## Create your login

On a device that ships ready to set up, such as a Teaspoon AI device, the setup page has a **Create your login** step under the Wi-Fi password. The login lets you sign in to the device from a computer, with SSH, and run administrator commands with `sudo`. Its username is always `teaport`; you choose only the password. On a Teaspoon AI device, nobody can sign in until you create it. A device that you installed yourself never shows this step.

<!-- source: brain/teaport_brain/wifi_setup.py:37-51#de9e3f82; brain/teaport_brain/wifi_setup.py:362-378#a9c80e12; brain/teaport_brain/wifi_setup.py:941#ad104058 -->

The step is required while the device has no login: the page doesn't connect to your Wi-Fi network until the login is created. If the device can't reach the helper that creates the login, the page shows no step, and the device still connects. If the helper fails because of a problem on the device, not with your password, the page says that you can connect now: from then on, until setup mode ends, you can leave the login fields empty and tap **Connect**. After a failure like that, the settings offer the step later, as [Create your login in the settings](/guides/configuration/#create-your-login-in-the-settings) shows. If the step appears in neither place, see [You can't sign in to the device](/guides/troubleshooting/#you-cant-sign-in-to-the-device).

<!-- source: brain/teaport_brain/wifi_setup.py:747-766#018b3b92; brain/teaport_brain/owner_login.py:99-103#3acb30a5 -->

To create your login on the setup page, follow these steps:

1. Under **Choose a password**, type a password of at least 12 characters. Spaces and letters from any language are fine; invisible or control characters are not. The longest password is 256 bytes: 256 unaccented letters, for example, or 85 Chinese, Japanese, or Korean characters. An accented letter, such as é, takes 2 bytes.
2. Under **Type it again**, type the same password.
3. Tap **Connect** with your Wi-Fi choice.

The device creates the login as soon as you tap **Connect**, before it leaves the setup network, because this needs no internet. The page that says the device is joining your network also says that your login is ready. After the device is online, sign in from a computer on the same network:

```bash
ssh teaport@DEVICE_NAME.local
```

Replace `DEVICE_NAME` with the device's name, for example `teaport`. `sudo` asks for the same password.

<!-- source: brain/teaport_brain/wifi_setup.py:703-761#aa3523c5; brain/teaport_brain/wifi_setup.py:783-794#c77b64d7; brain/teaport_brain/login_apply.py:84-87,96-97#1dd88d3a; brain/teaport_brain/login_apply.py:137-148#020cf4bc; brain/teaport_brain/login_apply.py:238-242#57a7a7ce -->

If the device can't create the login, it doesn't start the join: the setup page comes back with the reason at the top, and the device stays in setup mode. Type your Wi-Fi password and both login passwords again:

- If the passwords don't match, or the password is too short or too long, or it has a character that isn't allowed: fix the password.
- If the page says that the device already has a login: the step goes away. Tap **Connect** again for the Wi-Fi alone. If you didn't create that login, see [The device already has a login](/guides/troubleshooting/#the-device-already-has-a-login).
- If the page says that it couldn't create your login and that you can connect now: try again, or leave the login fields empty and tap **Connect**, and create the login later in the settings. The next try usually finishes one that stopped partway. If the device then says that it already has a login, or the step doesn't come back, see [The device already has a login](/guides/troubleshooting/#the-device-already-has-a-login).

<!-- source: brain/teaport_brain/wifi_setup.py:703-761#aa3523c5; brain/teaport_brain/owner_login.py:52-69#622d193b; brain/teaport_brain/login_apply.py:16-28#47ab3ecd -->

## Connect your AI account

When the device's language model endpoint is OpenRouter, the setup page also has an **AI account** section, which you can skip. Leave it closed to keep the account the device has now. To use your own OpenRouter account, open it and paste a key from that account before you tap **Connect**.

The setup network has no internet, so the device checks the key after it joins your network and is online. It saves the key only if OpenRouter accepts it and, on a device with OpenClaw, OpenClaw takes it too, as [One key for OpenClaw too](/guides/configuration/#one-key-for-openclaw-too) describes. When it says that it is connected, it also says whether your AI account is connected, and, if your OpenRouter account has no credit yet, that you need to add some on OpenRouter's website before it can answer. If the key did not work, the device keeps the account it had; say "open my settings" to try again from your phone, as [Open the settings from your phone](/guides/configuration/#open-the-settings-from-your-phone) shows.

Signing in to OpenRouter instead of pasting a key does not work in setup mode: neither the phone nor the device can reach the internet there. When the device offers the sign-in afterwards (it has a settings site, its endpoint is OpenRouter, and it has no key of your own), the section says so: leave the key empty, and when the device is online it offers the sign-in by itself, or you can say "open my settings", as [Connect your OpenRouter account](/guides/configuration/#connect-your-openrouter-account) shows.

<!-- source: brain/teaport_brain/wifi_setup.py:328-347#9c83724e -->

<!-- source: brain/teaport_brain/wifi_setup.py:322-325#85aa669c; brain/teaport_brain/wifi_setup.py:908-921#3ae9aa03; brain/teaport_brain/wifi_setup.py:1064-1067#84fbce23; brain/teaport_brain/wifi_voice.py:123-134#457019ed; systemd/teaport-wifi-setup.service.in:38-42#ddd3d10e -->

## If the device connects

The page tells you that the device is joining your network, so switch your phone back to it. The device leaves setup mode and says that it is connected to your network and online again; a screen shows "Connected to" and the network's name under the title "Wi-Fi" for a few seconds.

<!-- source: brain/teaport_brain/wifi_setup.py:434-446#4344163d; brain/teaport_brain/wifi_voice.py:982-987#799aa40d; brain/teaport_brain/wifi_setup.py:1019-1021#534563de -->

## If the join fails

The device drops the network profile it tried, the setup network comes back, and the page says what failed. Pick the network and type its password again.

<!-- source: brain/teaport_brain/wifi_setup.py:28-29#50476f67 -->

## If nothing has connected after 10 minutes

The device leaves setup mode on its own, undoes a half-finished join, and restores the connection it had before. The service stops itself at the 15-minute mark at the latest.

<!-- source: brain/teaport_brain/wifi_setup.py:52-53#f8886e10; systemd/teaport-wifi-setup.service.in:84-85#364bb92a -->

## Leave setup mode

Say "cancel" at the device while it is in setup mode, stop it from the configuration page, or run this on the device:

```bash
sudo systemctl stop teaport-wifi-setup
```

The device takes the setup network down, undoes a half-finished join, and restores the previous connection.

<!-- source: systemd/teaport-wifi-setup.service.in:72-74#70ebb3ad; brain/teaport_brain/wifi_setup.py:925#21bb505f -->

## What's next

- [Use a microphone on the device](/guides/local-audio/), for the spoken phrase.
- [Settings reference](/reference/settings/), for the Wi-Fi setup settings, including the setup-network password.
- [Troubleshooting](/guides/troubleshooting/), if the device does not come online.
