---
title: Troubleshooting
description: Find the cause when teaport does not work correctly.
---

This page lists the failures seen on real devices. Each section starts with the
symptom. Find yours, then follow the fix.

:::note[Seen on]
Every failure on this page occurred on a Jetson Orin Nano Dev Kit (8 GB) with
JetPack 7.2. If your problem is not here,
[open an issue](https://github.com/Teaspoon-AI/teaport/issues). Include the
output of `teaport status` and `teaport doctor`.
:::

## Start here

Run these three commands first. They identify most problems.

```bash
teaport status      # are the services running?
teaport doctor      # are the files, the settings, and the ports correct?
teaport logs        # what did the brain report?
```

`teaport doctor --fix` runs the same checks and restarts a service whose unit
has stopped. It prints the unit that it restarts.

<!-- source: cli/teaport:6#fcb6da6d; cli/teaport:176-279#f2b6cc27 -->

You can run the installer again at any time. It repairs an installation without
making a second one and keeps the settings that you added yourself.

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport)
```

:::caution[`teaport doctor` does not test speech quality]
`doctor` tests that files exist and that services run. Two important failures
pass every test: the slow speech-recognition kernel, and the missing
pronunciation dictionary. See
[The speech sounds robotic](#the-speech-sounds-robotic) and
[Speech recognition is slow](#speech-recognition-is-slow).
:::

## The installer stops immediately

### `not an NVIDIA Jetson (/etc/nv_tegra_release missing)`

**Cause** You are not on a Jetson, or the Jetson image carries no
`/etc/nv_tegra_release`. The installer works only on Jetson devices.

<!-- source: install.sh:250#6bef2c33 -->

**Fix**

1. Run the installer on a Jetson that runs JetPack. To set one up, see
   [Prepare the Jetson](/getting-started/prepare-the-jetson/).

### `unsupported L4T` with a version number

**Cause** The device runs a JetPack that the engine does not support. The engine
ships for JetPack 7.2 (L4T R38 or R39, CUDA 13) only.

<!-- source: install.sh:231-245#a6431d65 -->

**Fix**

1. Check the version:

   ```bash
   cat /etc/nv_tegra_release
   apt-cache policy nvidia-jetpack | head -2
   ```

2. teaport accepts Linux for Tegra (L4T) R38 and R39. JetPack 7.2 reports
   **R39**. That is correct; do not look for R38. On any other version, install
   JetPack 7.2, and then run the installer again. See
   [Prepare the Jetson](/getting-started/prepare-the-jetson/).

### `libopenblas.so.0 not installed`

**Cause** The engine links against `libopenblas.so.0`. JetPack does not include
it.

<!-- source: install.sh:277-286#99af061f -->

**Fix**

1. Install it:

   ```bash
   sudo apt-get install -y libopenblas0
   ```

### `manifest has no engine.g2p_dict`

**Cause** The release manifest carries no `engine.g2p_dict` entry, with its URL
and sha256. A device installed without the pronunciation dictionary speaks with
robotic espeak prosody. The dictionary check is necessary, so keep it.

<!-- source: install.sh:451-461#fc133eae -->

**Fix**

1. Wait until the manifest is republished with the entry, and then run the
   installer again. See [The speech sounds robotic](#the-speech-sounds-robotic).

### `sha256 mismatch`

**Cause** A downloaded file does not agree with the checksum in the manifest.
The partial `.part` file remains on the disk.

<!-- source: install.sh:163-166#d700a861 -->

**Fix**

1. Delete the partial files, and run the installer again:

   ```bash
   sudo rm -f /opt/teaport/models/voxtral/*.part /opt/teaport/bin/*.part
   bash <(curl -fsSL https://get.teaspoon.tech/teaport)
   ```

2. If the same file fails again, the published manifest and the published file
   disagree. [Open an issue](https://github.com/Teaspoon-AI/teaport/issues).

## A service is stopped

**Cause** The engine or the brain crashed, or someone stopped it. A fresh start
also needs up to a minute before the engine port listens.

**Fix**

1. Run `teaport doctor --fix`. It restarts `teaport-engine` or `teaport-brain`
   when the unit is stopped, and prints the unit that it restarts.
2. Run `teaport doctor` again. The last line reads `doctor: all good`.
3. If the same check fails again, read the log. See
   [Where the logs are](#where-the-logs-are).

<!-- source: cli/teaport:176-279#f2b6cc27 -->

## The browser cannot open `https://teaport.local`

**Cause** The name `teaport.local` is not published on the network, or the front
door is not listening. Most home networks carry the name. Some guest networks
and some corporate networks block it.

**Fix** Do these checks in order.

1. Test the name from a computer on the same network:

   ```bash
   getent hosts teaport.local
   ```

   If the command prints nothing, restart the mDNS service on the Jetson:

   ```bash
   sudo systemctl restart avahi-daemon
   ```

   The mDNS service reads its configuration only at startup. An installation
   that sets the name but does not restart the service leaves the device under
   the old name.

2. Use the address instead, for example `https://192.168.1.67`. The browser
   shows a stronger warning, because the certificate names `teaport.local`
   only.

3. Test the front door. On the Jetson:

   ```bash
   sudo ss -tlnp | grep -E ':(443|18789) '
   ```

   Caddy must listen on port 443, and the gateway on `127.0.0.1:18789`.

<!-- source: install.sh:1536#2ca3ef5a; install.sh:1792-1840#fb689c3c -->

## The page opens but nothing happens

**Cause** The gateway refuses the browser because the address carries no token.
The dashboard shows no error.

**Fix**

1. Get the full address:

   ```bash
   openclaw dashboard --no-open
   ```

2. Open the address that the command prints. It contains `#token=`.

:::caution[The token must follow a `#`]
A `#` makes a fragment, and the browser keeps a fragment. A `?` makes a query:
the browser sends the query to the server, and the server writes it to its log.
Always use `#`.
:::

The token is also in `~/.openclaw/openclaw.json`, under `gateway.auth.token`.

<!-- source: install.sh:1411-1424#c562c839 -->

## The gateway restarts again and again

**Cause** The gateway started twice. `openclaw gateway install` installs the
service and starts it. A later `openclaw gateway start` restarts the service.
The restart stops the first process during a database migration, and the lock
stays for approximately five minutes.

**Fix**

1. Stop the gateway:

   ```bash
   openclaw gateway stop
   ```

2. Wait five minutes.
3. Start the gateway one time:

   ```bash
   openclaw gateway start
   ```

From then on, use `openclaw gateway restart`. Do not pair `stop` with `start`.

## The gateway is not running after a restart of the device

**Cause** The gateway is a systemd user service. A user service starts when the
user signs in. A device with no screen never signs in.

<!-- source: install.sh:1430-1434#bf1269bd -->

**Fix**

1. On the Jetson, enable linger for your user:

   ```bash
   sudo loginctl enable-linger $USER
   ```

2. Check it:

   ```bash
   loginctl show-user $USER --property=Linger
   ```

   The output shows `Linger=yes`. The installer enables linger.

## `openclaw qr` refuses to make a code

The command prints:

```text
Error: Gateway is only bound to loopback. Set gateway.bind=lan, enable tailscale serve,
or configure plugins.entries.device-pair.config.publicUrl.
```

**Cause** The gateway listens on `127.0.0.1` only. That is correct: Caddy is the
front door. The pairing plugin does not know that the front door exists. It
needs the public URL.

<!-- source: install.sh:1262-1274#a28d74bc -->

**Fix**

1. Write the configuration:

   ```bash
   cat > /tmp/oc-pair.json <<'JSON'
   { "plugins": { "entries": { "device-pair": {
     "enabled": true, "config": { "publicUrl": "wss://teaport.local" } } } } }
   JSON
   ```

2. Apply it, and restart the gateway:

   ```bash
   openclaw config patch --file /tmp/oc-pair.json
   openclaw gateway restart
   ```

The installer writes this configuration. The error occurs only with an older
installer.

:::note[`enabled` is necessary]
The plugin has `enabledByDefault` in its manifest. It is also a bundled plugin,
and a bundled plugin needs permission on this system. Configuration alone gives
you `plugin disabled (bundled (disabled by default)) but config is present`, and
pairing stays dead.
:::

## The phone does not pair

**Cause** The application cannot accept the certificate. teaport signs its own
certificate. A browser lets you accept a warning; an application does not.

**Fix**

1. On the phone, open `http://teaport.local/teaport-ca.crt` in the browser, and
   select **Allow**.
2. Open Settings. Select General. Select **VPN & Device Management**. Install
   the profile.
3. Open Settings. Select General. Select About. Select **Certificate Trust
   Settings**. Switch on the device's certificate. Its name starts with `Teaport`
   and the end of the device's Wi-Fi address, for example
   `Teaport ab12 Local Authority`; the pairing page names it.
4. Make a new code. Say "connect my phone" to the device, scan the code on its
   screen, and choose **Use the OpenClaw app**; that pairs the app with no approval
   step. Or make one on the Jetson, and scan it:

   ```bash
   openclaw qr
   ```

5. Approve the device on the Jetson:

   ```bash
   openclaw devices list
   openclaw devices approve REQUEST_ID
   ```

   `REQUEST_ID` is the request identifier from the list.

:::caution[Step 3 is a different step from step 2]
An installed certificate is not a trusted certificate. If you do step 2 but not
step 3, the application fails exactly as before. This is the most frequent
cause.
:::

Each code carries a token. The token stops after 10 minutes. Every `openclaw qr`
makes a new code. Do not scan a photograph of an old code.

<!-- source: install.sh:1792-1840#fb689c3c; install.sh:1709-1734#7e517176 -->

### The phone pairs, but it has no permissions

**Cause** The pairing used `ws://` instead of `wss://`. OpenClaw gives a
cleartext connection a limited profile. The profile permits speech only. It
does not permit operator commands.

**Fix** Pair again over `wss://`. Set `publicUrl` as in
[`openclaw qr` refuses to make a code](#openclaw-qr-refuses-to-make-a-code).

## The pairing code says expired or already used

**Cause** Each pairing code works one time, for 5 minutes at most. Opening the link
uses nothing; pressing **Open the dashboard here** or **Use the OpenClaw app** uses
the code up, even when the dashboard or the app then does not connect. The page says
**already used** when a choice was pressed before: on a second device, or on this
phone when the dashboard or the app did not connect the first time. That is most often
a phone that does not trust the device's certificate yet. The page says **expired**
when the 5 minutes passed, when the device restarted after it showed the code, or when
someone tried several wrong codes, which ends the code for safety.

**Fix** If the dashboard or the app did not connect, trust the device's certificate
first, as in [The phone does not pair](#the-phone-does-not-pair), steps 1 to 3 (the
page links the certificate). Then ask the device again, for example "connect my
phone", and scan the new code. Do not scan a photograph of an old code.

If the page says that the connection could not start, the device could not make
the dashboard link or the app's setup code. The code is over and the screen clears. Ask
again; if it keeps failing, check the gateway with `openclaw gateway status`.

Pairing links work only through the front door, `https://teaport.local/p/...`. The
same path on the brain's own port, `http://<device>:7861/p/...`, answers
**Not Found**.

### The pairing link does not open

**Cause** The front door on the device was set up before it served pairing links.
`./install.sh --only brain` updates the brain only. It does not rewrite the front
door's configuration or `brain.env`.

**Fix** Run the installer again without `--only`, as in
[Install teaport](/getting-started/installation/#install-teaport). A re-run repairs
the installation and keeps your settings. You do this one time.

<!-- source: install.sh:1855-1861#4326c857; install.sh:1922-1924#1153cfec -->

## Phone calls go unanswered

**Cause** One of three things, and `teaport doctor` names which: the
`teaport-sip` gateway is not running; the brain's SIP front-end is not connected
to it (`TEAPORT_SIP_SOCKET` empty in `/etc/teaport/brain.env`); or the trunk
registration is missing, so calls from the provider never arrive.

<!-- source: cli/teaport:176-279#f2b6cc27 -->

**Fix**

1. Run `teaport doctor`. The `SIP` lines name the break: `teaport-sip is ...`,
   `the brain's SIP front-end is ...`, or `SIP trunk NOT registered`.
2. Run `teaport doctor --fix`. It restarts `teaport-sip`, and the brain
   reconnects to it. If the front-end was the fault, it restarts the brain. A
   restart of the brain ends a live Talk session.
3. Wait ten seconds after a gateway start, and run `teaport doctor` again: the
   registration appears in the journal within approximately ten seconds.
4. If the trunk is still unregistered, check the account against
   [Set up phone calls](/guides/phone-calls/).

## The speech sounds robotic

**Cause** The engine did not load the pronunciation dictionary. It fell back to
plain `espeak-ng`, which has no word stress: short words sound too long, and the
rhythm is wrong.

<!-- source: install.sh:276#649012b4; install.sh:441-442#12d8444d -->

**Fix**

1. Check the dictionary:

   ```bash
   sudo journalctl -u teaport-engine | grep 'dict G2P'
   ```

   `dict G2P unavailable` means the dictionary is absent.
2. The dictionary must be `/opt/teaport/bin/g2p/kokoro_g2p.v2.dict`. Run the
   installer again to get it.
3. The installer writes `VOX_REQUIRE_DICT_G2P=1` into `/etc/teaport/engine.env`.
   An engine that supports this setting refuses to start without the dictionary.
   That is safer than a device that runs and sounds wrong.

:::caution[The published engine ignores this setting]
The engine that `teaport update` installs today does not know
`VOX_REQUIRE_DICT_G2P`. The setting does nothing. The engine starts, and it
sounds robotic. Use the journal command in step 1. Do not rely on the setting
until a new engine is published.
:::

:::caution[`teaport doctor` passes without the dictionary]
`doctor` tests that `espeak-ng` exists. `espeak-ng` is the fallback, not the
dictionary. A device with correct speech and a device with robotic speech both
pass. Use the journal command in step 1.
:::

## Speech recognition is slow

**Cause** The engine has three kernels and picked a slow one, because the model
file on the disk is not the one the manifest describes.

**Fix**

1. Check which kernel the engine chose:

   ```bash
   sudo journalctl -u teaport-engine | grep -i marlin
   ```

   The output must contain:

   ```text
   Detected Marlin INT4 model (single-file format)
   ```

2. Any other message means a slow kernel. Compare the model file against the
   manifest:

   ```bash
   sha256sum /opt/teaport/models/voxtral/consolidated.safetensors
   grep -A2 '"voxtral"' /opt/teaport/manifest.json
   ```

3. If the values differ, delete the model, and run the installer again:

   ```bash
   sudo rm -f /opt/teaport/models/voxtral/consolidated.safetensors
   bash <(curl -fsSL https://get.teaspoon.tech/teaport)
   ```

:::caution[`teaport doctor` passes on the slow kernel]
`doctor` tests that the model file exists. It does not test which kernel the
engine chose. Use the journal command in step 1.
:::

## The first sentence fails

**Cause** The model name is wrong for the endpoint. OpenRouter needs the
supplier and the model; other services need the model alone. The brain can also
miss the endpoint or the key.

| Endpoint | Model name |
|---|---|
| OpenRouter | `openai/gpt-oss-120b` |
| Groq | `gpt-oss-120b` |
| Cerebras | `gpt-oss-120b` |

**Fix**

1. Correct `LLM_MODEL` in `/etc/teaport/brain.env`.
2. To apply the change, restart the brain:

   ```bash
   sudo systemctl restart teaport-brain
   ```

3. Run `teaport doctor`. It prints `LLM_BASE_URL unset in
   /etc/teaport/brain.env` when the endpoint is missing, and
   `no LLM key - set LLM_API_KEY or write ~/.config/teaport/llm_key` when the
   key is missing.

The installer offers `gpt-oss-120b` as the default value. That default is wrong
for OpenRouter.

<!-- source: install.sh:1513#2e67295a; install.sh:1526-1529#cefd034b -->

## teaport does not remember anything

**Cause** The brain calls the gateway for memory, and it lacks the gateway's own
token. It then skips the memory tools, and logs
`no gateway token; skipping tool`.

<!-- source: brain/teaport_brain/openclaw_client.py:148#d3cc622a; install.sh:1411-1424#c562c839 -->

**Fix**

1. Check the token file, and the brain log:

   ```bash
   ls -l ~/.config/teaport/openclaw_token
   teaport logs brain | grep -i 'gateway token'
   ```

2. `no gateway token; skipping tool 'memory_search'` means the file is absent.
   Write it:

   ```bash
   python3 -c "import json;print(json.load(open('$HOME/.openclaw/openclaw.json'))['gateway']['auth']['token'])" \
     > ~/.config/teaport/openclaw_token
   chmod 600 ~/.config/teaport/openclaw_token
   ```

3. To apply the change, restart the brain:

   ```bash
   sudo systemctl restart teaport-brain
   ```

:::note[teaport uses three different secrets]
Do not confuse them.

| Secret | Purpose | Location |
|---|---|---|
| `GATEWAY_TOKEN` | the agent connects to the brain's `/talk` | `/etc/teaport/brain.env` |
| `openclaw_token` | the brain calls the gateway for memory and tools | `~/.config/teaport/openclaw_token` |
| `llm_key` | the brain calls your large language model (LLM) | `~/.config/teaport/llm_key` |
:::

## teaport stops speaking in the middle of a reply

**Cause** A fault in the engine. The engine keeps two audio slots, and releases
a slot only after it finishes the abandoned sentence. That takes approximately
1.5 seconds. Two interruptions within that time use both slots, and the next
sentence gets no slot.

**Fix** Leave approximately two seconds between interruptions.

:::caution[The correction is not in the published engine yet]
The correction exists. The engine that `teaport update` installs today does not
carry it.
:::

## teaport does not answer quiet speech

**Cause** Speech recognition returns an empty result for quiet audio. It does
not report a fault.

**Fix** Speak closer to the microphone. Raise the input level of the browser
device.

Tests confirm it: 16% of a standard test set peaks below −30 dBFS, and
24 of 2600 files returned an empty result.

## Where the logs are

| Command | Contents |
|---|---|
| `teaport logs brain` | the conversation, the LLM, the tools |
| `teaport logs engine` | model loading, kernel choice, the dictionary |
| `journalctl --user -u openclaw-gateway` | pairing, browser connections, authentication |
| `sudo journalctl -u caddy` | the front door and the certificate |
| `sudo journalctl -u avahi-daemon` | the `teaport.local` name |

Add `-f` to follow a log. Add `-n 200` to see more lines.

The gateway also writes a file. `openclaw gateway status` prints its path. The
path changes each day:

```text
File logs: /tmp/openclaw/openclaw-2026-08-07.log
```

:::note
`openclaw gateway` has no `logs` command. Its commands are `install`, `start`,
`stop`, `restart`, and `status`.
:::

## Related

- [Installation](/getting-started/installation/)
- [Configuration](/guides/configuration/)
- [Set up phone calls](/guides/phone-calls/)
- [Use a microphone on the device](/guides/local-audio/)
- [teaport CLI reference](/reference/cli/)
