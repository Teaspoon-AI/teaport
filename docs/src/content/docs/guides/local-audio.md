---
title: Use a microphone on the device
description: Talk to the agent through a microphone and speaker on the device itself, with no phone, browser, or app.
---

The local-audio audio bridge pipes a microphone and speaker on the device straight into the brain's conversation endpoint, with no phone, browser, or app in between. It is built for the Seeed ReSpeaker XVF3800, a USB microphone array with a speaker jack and hardware echo cancellation; any sound card that carries 16 kHz stereo in both directions works.

<!-- source: brain/teaport_brain/local_audio.py:3-9#bb671baa -->

## Turn the bridge on

The bridge ships switched off. Run the installer with the bridge flag:

```bash
sudo TEAPORT_ENABLE_LOCAL_AUDIO=1 ./install.sh
```

The installer writes `/etc/teaport/local-audio-env` and enables the `teaport-local-audio` service. To use a card other than the default ReSpeaker (`hw:CARD=Array,DEV=0`), also set `TEAPORT_LOCAL_AUDIO_DEVICE` to that card's ALSA name. To turn the bridge off later, run this on the device:

```bash
sudo systemctl disable --now teaport-local-audio
```

A bridge you disabled stays off when the installer runs again without the flag.

<!-- source: install.sh:46-51,2300-2303#3b3d7be1; systemd/teaport-local-audio.service.in:6-7,14#65fb5796 -->

## Talk to the agent

The bridge connects when it starts, and the agent greets you; from then on it behaves like any Talk session. When a conversation ends, the microphone sleeps: the bridge connects again only when someone is louder than the `LOCAL_AUDIO_WAKE_DB` level, and it sends the sound from right before you spoke, so the words that woke it are the ones the agent hears.

The brain runs one conversation at a time. A dashboard Talk session can take the device only while the bridge sleeps; while any conversation is live the brain refuses the bridge, and the bridge waits the conversation out (`LOCAL_AUDIO_BACKOFF_SECS`) before it listens for a voice again.

<!-- source: brain/teaport_brain/local_audio.py:14-27#01d06bc5; brain/teaport_brain/config_schema.toml:202#2ef76416 -->

## Wake words

With `LOCAL_AUDIO_WAKE_WORDS` set to one or more phrases, the bridge holds its session asleep: the brain's speech-to-text keeps transcribing the room, in any language the engine hears, but nothing it hears goes anywhere, not the model's context, not the agent, not a caption, not a log, until a transcript contains one of the phrases.

Matching is exact, on whole words: "teapot" does not wake a device listening for "teaport" unless you list it too.

To send the bridge back to sleep when you are done, say "that's all", "goodnight", or "go to sleep"; the agent says a short goodbye and the microphone sleeps immediately. This works only while wake words are set.

<!-- source: brain/teaport_brain/wake_gate.py:3-12#609ddef1; brain/teaport_brain/wake_words.py:5-11#7bc70717; brain/teaport_brain/config_schema.toml:1617#4ab9e105 -->

## The ring during a phone call

On an XVF3800, the LED ring shows phone-call state: breathing red while a call rings, solid red while you are on it, and back to the ring's normal look after. The installer sets up the permissions this needs, a udev rule and the `teaport-hw` group, and a setting turns the behavior off.

<!-- source: brain/teaport_brain/xvf_led.py:3-8#57bcea8d; install.sh:1105-1116#d37a478f; brain/teaport_brain/config_schema.toml:1694-1706#44d773d1 -->

## Echo

The XVF3800 cancels echo in hardware, and the bridge keeps the chip's echo suppression following the measured echo: how loud the device's own voice is at the microphone, against yours. `LOCAL_AUDIO_ECHO_CONTROL`, `LOCAL_AUDIO_MIC_GAIN_DB`, and `LOCAL_AUDIO_ECHO_MARGIN_DB` adjust the behavior.

<!-- source: brain/teaport_brain/xvf_echo.py:3-9#8c2619eb; brain/teaport_brain/config_schema.toml:2342-2382#c927325b -->

## What's next

- [Settings reference](/reference/settings/), for every `LOCAL_AUDIO_` setting and the busy-lamp setting.
- [Set up phone calls](/guides/phone-calls/), for the calls the ring reports.
- [Troubleshooting](/guides/troubleshooting/), when the device does not hear you or the bridge does not start.
