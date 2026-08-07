---
title: Troubleshooting
description: Find the cause when teaport does not work correctly.
---

This page lists the failures that we saw on real devices. Each section starts with what you
see. Find your symptom, then do the steps.

:::note[Pre-launch]
teaport is not released yet. Every failure on this page occurred on a Jetson Orin Nano Dev
Kit (8 GB) with JetPack 7.2. If your problem is not here,
[open an issue](https://github.com/Teaspoon-AI/teaport/issues). Include the output of
`teaport status` and `teaport doctor`.
:::

## Start here

Run these three commands first. They identify most problems.

```bash
teaport status      # are the services running?
teaport doctor      # are the files, the settings, and the ports correct?
teaport logs        # what did the brain report?
```

You can run the installer again at any time. It repairs an installation. It does not make a
second installation. It keeps the settings that you added yourself.

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport)
```

:::caution[`teaport doctor` does not test speech quality]
`doctor` tests that files exist and that services run. Two important failures pass every
test: the slow speech-recognition kernel, and the missing pronunciation dictionary. Read
[The speech sounds robotic](#the-speech-sounds-robotic) and
[Speech recognition is slow](#speech-recognition-is-slow).
:::

## The installer stops immediately

### `not an NVIDIA Jetson (/etc/nv_tegra_release missing)`

You are not on a Jetson. The installer works only on Jetson devices.

### `unsupported L4T` with a version number

Your device has a JetPack version that teaport does not support. Check the version:

```bash
cat /etc/nv_tegra_release
apt-cache policy nvidia-jetpack | head -2
```

teaport accepts Linux for Tegra (L4T) R38 and R39. JetPack 7.2 reports **R39**. This is
correct. Do not look for R38.

<!-- source: install.sh:178 -->

### `libopenblas.so.0 not installed`

The engine links against OpenBLAS. JetPack does not include it. Install it:

```bash
sudo apt-get install -y libopenblas0
```

The current installer does this for you. This error occurs only with an older installer.

<!-- source: install.sh:208-214 -->

### `manifest has no engine.g2p_dict`

The release manifest is older than the installer. Wait for a new manifest, or use an older
installer. Do not remove the check. The dictionary is necessary. See
[The speech sounds robotic](#the-speech-sounds-robotic).

### A checksum does not agree

The installer downloaded a file, then found a different checksum. Delete the partial file and
run the installer again:

```bash
sudo rm -f /opt/teaport/models/voxtral/*.part /opt/teaport/bin/*.part
bash <(curl -fsSL https://get.teaspoon.tech/teaport)
```

If the error continues, the published manifest and the published file do not agree. This is
our fault, not yours. [Open an issue](https://github.com/Teaspoon-AI/teaport/issues).

## The browser cannot open `https://teaport.local`

Do these checks in order.

**1. Test the name.** From a computer on the same network:

```bash
getent hosts teaport.local
```

If you get no result, the multicast Domain Name System (mDNS) name is not published. On the
Jetson, restart the mDNS service:

```bash
sudo systemctl restart avahi-daemon
```

The installer sets the published name in `/etc/avahi/avahi-daemon.conf`. The service reads
that file only when it starts. An installer that writes the name but does not restart the
service leaves the device with its old name.

**2. Use the address instead.** If the name still fails, use the Jetson's address, for
example `https://192.168.1.67`. Your browser shows a stronger warning, because the
certificate names `teaport.local` only. Your network does not permit mDNS. Some guest
networks and some corporate networks block it.

**3. Test the front door.** On the Jetson:

```bash
sudo ss -tlnp | grep -E ':(443|18789) '
```

You must see Caddy on port 443 and the gateway on `127.0.0.1:18789`.

<!-- source: install.sh:494-497 -->

## The page opens but nothing happens

You see the dashboard. It does not connect. It shows no error.

The gateway refuses your browser because you did not supply a token. Get the full address:

```bash
openclaw dashboard --no-open
```

Open the address that this command prints. It contains `#token=`.

:::caution[The token must follow a `#`]
A `#` makes a fragment. Your browser keeps a fragment. A `?` makes a query. Your browser
sends a query to the server, and the server writes it to its log. Always use `#`.
:::

The token is also in `~/.openclaw/openclaw.json`, under `gateway.auth.token`.

## The gateway restarts again and again

`openclaw gateway status` shows `Runtime:` with a changing process number. The dashboard
refuses every connection.

You started the gateway twice. `openclaw gateway install` installs the service **and starts
it**. A later `openclaw gateway start` restarts the service. This stops the first process
during a database migration. The lock stays for approximately five minutes.

Do this:

```bash
openclaw gateway stop
# wait five minutes
openclaw gateway start
```

Use `openclaw gateway restart` after this. Do not use `stop` and `start` together.

## The gateway is not running after a restart of the device

The gateway is a systemd **user** service. A user service does not start until the user logs
in. A device with no screen never logs in.

```bash
sudo loginctl enable-linger $USER
```

The installer does this. Check it:

```bash
loginctl show-user $USER --property=Linger
```

## `openclaw qr` refuses to make a code

```
Error: Gateway is only bound to loopback. Set gateway.bind=lan, enable tailscale serve,
or configure plugins.entries.device-pair.config.publicUrl.
```

The gateway listens only on `127.0.0.1`. This is correct. Caddy is the front door. The
pairing plugin does not know that the front door exists. Tell it:

```bash
cat > /tmp/oc-pair.json <<'JSON'
{ "plugins": { "entries": { "device-pair": {
  "enabled": true, "config": { "publicUrl": "wss://teaport.local" } } } } }
JSON
openclaw config patch --file /tmp/oc-pair.json
openclaw gateway restart
```

The installer does this. This error occurs only with an older installer.

<!-- source: install.sh:326-340 -->

:::note[`enabled` is necessary]
The plugin has `enabledByDefault` in its manifest. It is also a bundled plugin, and a bundled
plugin needs permission on this system. Configuration alone gives you
`plugin disabled (bundled (disabled by default)) but config is present`, and pairing stays
dead.
:::

## The phone does not pair

You scan the code. The application fails. It gives no useful message.

The application cannot accept our certificate. teaport signs its own certificate. A browser
lets you accept a warning. An application cannot.

Install the certificate on the phone:

1. Open `http://teaport.local/teaport-ca.crt` in the phone's browser. Select **Allow**.
2. Open Settings. Select General. Select **VPN & Device Management**. Install the profile.
3. Open Settings. Select General. Select About. Select **Certificate Trust Settings**. Switch
   the certificate on.

:::caution[Step 3 is a different step from step 2]
An installed certificate is not a trusted certificate. If you do step 2 but not step 3, the
application fails exactly as before. This is the most frequent cause.
:::

Then make a new code and scan it:

```bash
openclaw qr
```

Each code contains a token. The token stops after **10 minutes**. Each `openclaw qr` command
makes a new code. Do not use a photograph of an old code.

After you scan, approve the device on the Jetson:

```bash
openclaw devices list
openclaw devices approve <requestId>
```

### The phone pairs, but it has no permissions

You paired over `ws://` instead of `wss://`. OpenClaw gives a cleartext connection a limited
profile. That profile permits speech only. It does not permit operator commands.

Pair again over `wss://`. See `publicUrl` in
[`openclaw qr` refuses to make a code](#openclaw-qr-refuses-to-make-a-code).

## The speech sounds robotic

The voice is flat. Short words sound too long. The rhythm is wrong.

The engine did not load the pronunciation dictionary. It uses a fallback that has no word
stress. Check:

```bash
sudo journalctl -u teaport-engine | grep 'dict G2P'
```

`dict G2P unavailable` means the dictionary is absent. The dictionary must be in
`/opt/teaport/bin/g2p/`. Run the installer again to get it.

<!-- source: install.sh:636-642, install.sh:805 -->

The installer writes `VOX_REQUIRE_DICT_G2P=1` into `/etc/teaport/engine.env`. An engine that
supports this setting refuses to start without the dictionary. This is safer than a device
that runs and sounds wrong.

:::caution[The published engine ignores this setting]
The engine that `teaport update` installs today does not know `VOX_REQUIRE_DICT_G2P`. The
setting does nothing. The engine starts, and it sounds robotic. Use the journal command
above to test. Do not rely on the setting until we publish a new engine.
:::

:::caution[`teaport doctor` passes without the dictionary]
`doctor` tests that `espeak-ng` exists. `espeak-ng` is the fallback, not the dictionary. A
device with correct speech and a device with robotic speech both pass. Use the journal
command above.
:::

## Speech recognition is slow

Speech recognition takes several seconds for a short sentence.

The engine has three kernels. Only one is fast. Check which one it chose:

```bash
sudo journalctl -u teaport-engine | grep -i marlin
```

You must see:

```
Detected Marlin INT4 model (single-file format)
```

Any other message means a slow kernel. The cause is the wrong model file. Compare the file
against the manifest:

```bash
sha256sum /opt/teaport/models/voxtral/consolidated.safetensors
grep -A2 '"voxtral"' /opt/teaport/manifest.json
```

If the values do not agree, delete the model and run the installer again:

```bash
sudo rm -f /opt/teaport/models/voxtral/consolidated.safetensors
bash <(curl -fsSL https://get.teaspoon.tech/teaport)
```

:::caution[`teaport doctor` passes on the slow kernel]
`doctor` tests that the model file exists. It does not test which kernel the engine chose.
Use the journal command above.
:::

## teaport does not remember anything

teaport answers correctly. It does not recall earlier conversations.

The brain calls the gateway for memory. It needs the gateway's own token. Check the file:

```bash
ls -l ~/.config/teaport/openclaw_token
teaport logs brain | grep -i 'gateway token'
```

`no gateway token; skipping tool 'memory_search'` means the file is absent. Write it:

```bash
python3 -c "import json;print(json.load(open('$HOME/.openclaw/openclaw.json'))['gateway']['auth']['token'])" \
  > ~/.config/teaport/openclaw_token
chmod 600 ~/.config/teaport/openclaw_token
sudo systemctl restart teaport-brain
```

<!-- source: install.sh:421-433 -->

:::note[teaport uses three different secrets]
Do not confuse them.

| Secret | Purpose | Location |
|---|---|---|
| `GATEWAY_TOKEN` | the agent connects to the brain's `/talk` | `/etc/teaport/brain.env` |
| `openclaw_token` | the brain calls the gateway for memory and tools | `~/.config/teaport/openclaw_token` |
| `llm_key` | the brain calls your large language model (LLM) | `~/.config/teaport/llm_key` |
:::

## teaport stops speaking in the middle of a reply

teaport speaks. It stops. The rest of the reply does not arrive. This happens after you
interrupt teaport two times quickly.

This is a fault in the engine. The engine keeps two audio slots. It releases a slot only
after it finishes the abandoned sentence. That takes approximately 1.5 seconds. Two
interruptions in that time use both slots, and the next sentence gets no slot.

:::caution[The correction is not in the published engine yet]
The correction exists. It is not in the engine that `teaport update` installs today. Until we
publish a new engine, leave approximately two seconds between interruptions.
:::

## The first sentence fails

The installation completes. The first sentence gives an error from the LLM.

Your model name is wrong for your endpoint. OpenRouter needs the supplier and the model. Other
services need the model alone.

| Endpoint | Model name |
|---|---|
| OpenRouter | `openai/gpt-oss-120b` |
| Groq | `gpt-oss-120b` |
| Cerebras | `gpt-oss-120b` |

Correct `LLM_MODEL` in `/etc/teaport/brain.env`, then:

```bash
sudo systemctl restart teaport-brain
```

The installer offers `gpt-oss-120b` as the default value. That default is wrong for
OpenRouter.

## teaport does not answer quiet speech

You speak quietly. teaport does nothing. It gives no error.

Speech recognition returns an empty result for quiet audio. It does not report a fault. Speak
closer to the microphone. Raise the input level of the browser device.

This is a known limitation. We measured it: 16% of one standard test set peaks below
−30 dBFS, and 24 of 2600 files returned an empty result.

## Where the logs are

| Command | Contents |
|---|---|
| `teaport logs brain` | the conversation, the LLM, the tools |
| `teaport logs engine` | model loading, kernel choice, the dictionary |
| `journalctl --user -u openclaw-gateway` | pairing, browser connections, authentication |
| `sudo journalctl -u caddy` | the front door and the certificate |
| `sudo journalctl -u avahi-daemon` | the `teaport.local` name |

Add `-f` to follow a log. Add `-n 200` to get more lines.

The gateway also writes a file. `openclaw gateway status` prints its path. The path changes
each day:

```
File logs: /tmp/openclaw/openclaw-2026-08-07.log
```

:::note
`openclaw gateway` has no `logs` command. Its commands are `install`, `start`, `stop`,
`restart`, and `status`.
:::

## Related

- [Installation](/getting-started/installation/)
- [teaport CLI](/reference/cli/)
- [Configuration](/guides/configuration/)
