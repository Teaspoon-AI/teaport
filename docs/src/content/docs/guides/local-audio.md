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

<!-- source: install.sh:66-67,3128-3131#22836ff1; systemd/teaport-local-audio.service.in:6-7,14#65fb5796 -->

## Talk to the agent

The bridge connects when it starts, and the agent greets you; from then on it behaves like any Talk session. When a conversation ends, the microphone sleeps: the bridge connects again only when someone is louder than the `LOCAL_AUDIO_WAKE_DB` level, and it sends the sound from right before you spoke, so the words that woke it are the ones the agent hears.

The brain runs one conversation at a time. A dashboard Talk session can take the device only while the bridge sleeps; while any conversation is live the brain refuses the bridge, and the bridge waits the conversation out (`LOCAL_AUDIO_BACKOFF_SECS`) before it listens for a voice again.

<!-- source: brain/teaport_brain/local_audio.py:14-27#01d06bc5; brain/teaport_brain/config_schema.toml:220#2ef76416 -->

## Wake words

With `LOCAL_AUDIO_WAKE_WORDS` set to one or more phrases, the bridge holds its session asleep: the brain's speech-to-text keeps transcribing the room, in any language the engine hears, but nothing it hears goes anywhere, not the model's context, not the agent, not a caption, not a log, until a transcript contains one of the phrases.

Matching is exact, on whole words: "teapot" does not wake a device listening for "teaport" unless you list it too.

To send the bridge back to sleep when you are done, say "go to sleep". The device answers "Okay, going to sleep." and the microphone sleeps after that has played. This works even when the agent can't answer, for example with no AI account set up or one that has run out of credit. Plain variants work too: "okay, go to sleep", "you can go back to sleep now", "can you go to sleep?", "stop listening". The phrase is understood in English and in the voice's language. A question about sleep, such as "how do I make you go to sleep?", is passed to the agent as usual. Said while the agent is talking, the phrase stops the answer, and the device says its line straight away. Saying "go to sleep" again while the line plays still sends it to sleep; if you say something else before the line ends, the device stays awake.

Saying only a goodbye works the same way: "good night", "okay, bye", "see you tomorrow". The device answers "Okay, going to sleep." without asking the agent, and the microphone sleeps after that has played. It has to be the whole sentence. "Good night, and remind me at seven" or "how do I say good night in French?" goes to the agent. In Spanish and Italian, "buenas noches" and "ciao" also mean hello, so they don't send the device to sleep if they are the first thing you say after waking it. Unlike "go to sleep", a goodbye doesn't end the conversation while the device still owes you an answer or is still putting a reply together. The agent hears your words instead.

You can also say "that's all"; the agent says a short goodbye and the microphone sleeps immediately. If the agent answers with only a goodbye but doesn't go back to sleep, the microphone sleeps after that goodbye has played, unless you have started talking again. Goodbyes are understood in English, Spanish and Italian; in the other voice languages, only English goodbyes count for now. An answer the device still owes you keeps it awake, unless all that is owed is a request that failed and you said goodbye yourself. Anything still owed is kept with the conversation for the next time you wake the device.

All of this works only while wake words are set, and only on the device's own microphone: a Talk session has no sleep, and a phone call ends when you hang up.

<!-- source: brain/teaport_brain/wake_gate.py:3-12#609ddef1; brain/teaport_brain/wake_words.py:5-11#7bc70717; brain/teaport_brain/config_schema.toml:1757#a4e8fb74; brain/teaport_brain/goodbye.py:13-19#bace4f09; brain/teaport_brain/goodbye.py:43-50#8ab45d2d; brain/teaport_brain/goodbye.py:53-58#76a042d2; brain/teaport_brain/goodbye.py:63-81#4363c977; brain/teaport_brain/goodbye.py:87-107#afe96c7b; brain/teaport_brain/goodbye.py:565#ffd4f075; brain/teaport_brain/agent_session.py:1011-1016#0a35e83a -->

## The ring during a phone call

On an XVF3800, the LED ring shows phone-call state: breathing red while a call rings, solid red while you are on it, and back to the ring's normal look after. The installer sets up the permissions this needs, a udev rule and the `teaport-hw` group, and a setting turns the behavior off.

<!-- source: brain/teaport_brain/xvf_led.py:3-8#57bcea8d; install.sh:1425-1436#d37a478f; brain/teaport_brain/config_schema.toml:1894-1906#44d773d1 -->

## Echo

The XVF3800 cancels echo in hardware. The bridge replaces the chip's automatic gain with a fixed gain, and sets the chip's echo suppression to remove the device's own voice from the microphone while still letting your voice through when you talk over it. `LOCAL_AUDIO_ECHO_CONTROL` and `LOCAL_AUDIO_MIC_GAIN_DB` adjust the behavior.

<!-- source: brain/teaport_brain/xvf_echo.py:2-3,73-77#37dc7dcc; brain/teaport_brain/config_schema.toml:2650-2657,2670-2680#208b84d6 -->

## What's next

- [Settings reference](/reference/settings/), for every `LOCAL_AUDIO_` setting and the busy-lamp setting.
- [Set up phone calls](/guides/phone-calls/), for the calls the ring reports.
- [Troubleshooting](/guides/troubleshooting/), when the device does not hear you or the bridge does not start.
