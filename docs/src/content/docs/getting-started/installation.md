---
title: Install teaport
description: Install teaport on a Jetson Orin Nano (8 GB).
---

This page walks you through installing teaport on a Jetson Orin Nano Dev Kit (8 GB). When you finish, the device answers you from the browser of a phone or a laptop. Allow 30 to 45 minutes. Downloading the engine and its models, about 15 GB, takes most of that time.

## Before you begin

- A **Jetson Orin Nano, NX, or AGX**, flashed with **JetPack 7.2**. JetPack 7.2 ships Linux for Tegra (L4T) R39.2 with CUDA 13.2; the installer accepts L4T R38 or R39. The Orin Nano with 8 GB of memory is the reference device.
- SSH access or a screen, and a home network.
- An **agent**: OpenClaw on the device, or NemoClaw, which is NVIDIA's OpenClaw package. The agent gives you the browser interface, the Talk session, and the tools. You can skip the agent, but then nothing can talk to the device. See [Install an agent](#install-an-agent).
- Your own **large language model (LLM)**. Use a cloud key from OpenRouter, Groq, or Cerebras, or a local OpenAI-compatible server. See [Configuration](/guides/configuration/).
- A **phone or laptop with a browser** on the same network. Its microphone and speaker carry the audio. Nothing to plug in: the device is already powered and on the network.
- About **15 GB** of free disk space for the engine and its models. The installer warns when it finds less.

<!-- source: install.sh:224-238,245-246,283,1506-1517 @1e5604c -->

## Install JetPack 7.2

This step is necessary. The engine ships for JetPack 7.2 on L4T R38 or R39 with CUDA 13. On any other release, the installer stops.

<!-- source: install.sh:224-238,429-431 @1e5604c -->

Use NVIDIA SDK Manager or the Jetson Linux flashing tools, and follow NVIDIA's flashing guide. Teaspoon AI does not host a copy of JetPack. Do not use the JetPack 7.2 ISO: NVIDIA records a known problem where the ISO does not make the 25 W and MAXN SUPER power modes available.

## Prepare the Jetson

Set the power mode and the swap file, and stop the desktop. See
[Prepare the Jetson](/getting-started/prepare-the-jetson/). This step is optional but
recommended.

## Install an agent

Choose **one** of the two agents. If both are present, the installer uses NemoClaw.

<!-- source: install.sh:1189-1191 @1e5604c -->

### Option A — OpenClaw on the device

OpenClaw runs on Node.js. JetPack 7.2 does not include Node.js.

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g openclaw
```

Give OpenClaw a minimal configuration and start its gateway:

```bash
cat > /tmp/oc-init.json <<'JSON'
{ "gateway": { "mode": "local", "bind": "loopback", "port": 18789 } }
JSON
openclaw config patch --file /tmp/oc-init.json
openclaw gateway install
```

:::caution[Do not run `gateway start` after `gateway install`]
`openclaw gateway install` installs the service **and starts it**. If you then run
`openclaw gateway start`, OpenClaw restarts the service. This stops the first process while
it migrates its database, and the process keeps its lock for about five minutes. The
gateway then restarts in a loop and refuses every connection.

If this happens: run `openclaw gateway stop`, wait five minutes, then run
`openclaw gateway start` once.
:::

Check the gateway:

```bash
openclaw gateway status
```

The line `Runtime:` must say `running`, and `Connectivity probe:` must say `ok`. The gateway listens on `127.0.0.1:18789`; the installer fronts it with Caddy.

<!-- source: install.sh:1365,1529 @1e5604c -->

### Option B — NemoClaw

NemoClaw is NVIDIA's installer. It runs OpenClaw inside a container. Run it in your own terminal and give your own consent:

```bash
bash <(curl -fsSL https://www.nvidia.com/nemoclaw.sh)
```

Run NemoClaw as your own user. Do not use `sudo`. The installer looks for `~/.nemoclaw/sandboxes.json` in your home folder. If NemoClaw installs into another user's home folder, the installer does not find it.

<!-- source: install.sh:1193-1201 @1e5604c -->

You can skip both. The installer then finishes in voice-only mode. To add an agent later, install OpenClaw or NemoClaw and run the installer again.

<!-- source: install.sh:1289-1297 @1e5604c -->

## Install teaport

On the Jetson, run:

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport)
```

The address serves a small bootstrap script. The bootstrap clones the teaport repository
into a temporary directory and runs the installer from that checkout. The installer reads
the release manifest and downloads the engine and the models that it names.

<!-- source: install.sh:3,5;install-bootstrap.sh:3-13;manifest/stable.json:5-6 @1e5604c -->

The installer asks you:

1. **The engine license.** Press Enter to read it, then type `accept`.
2. **Your LLM endpoint, model, and API key.** The model question offers `gpt-oss-120b` as
   the default. The key can stay empty for a keyless local server.
3. **Your Discord server and user IDs, and the bot token.** The installer asks these only
   when you set `TEAPORT_ENABLE_BRIDGE=1` to add the Discord bridge.

<!-- source: install.sh:41,64,207-211,1510-1517,1917-1919 @1e5604c -->

For OpenRouter, the base URL is `https://openrouter.ai/api/v1`.

:::caution[OpenRouter needs the full model name]
The model question offers `gpt-oss-120b` as the default. That name works with Groq and with
Cerebras. It does not work with OpenRouter: OpenRouter needs the provider name and the model
name, `openai/gpt-oss-120b`. If you accept the default, the installation completes and then
fails at the first sentence you speak.
:::

Every question has an environment variable, so you can also run the installer unattended:
`TEAPORT_ACCEPT_EULA=1` accepts the license, and `TEAPORT_LLM_BASE_URL`, `TEAPORT_LLM_MODEL`,
and `TEAPORT_LLM_API_KEY` preset the LLM answers. The flags and variables are in
[Installer reference](/reference/installer/).

<!-- source: install.sh:41,64,1506-1509 @1e5604c -->

You can run the installer again at any time. A re-run repairs the existing installation; it
does not create a second one. It keeps settings that you added to `/etc/teaport/brain.env`
yourself.

<!-- source: install.sh:5,60,1564,1589-1616 @1e5604c -->

The installer does these tasks:

- It downloads the engine, release 1.6, with its models and the pronunciation dictionary
  into `/opt/teaport`.
- It builds the brain in a Python 3.12 environment.
- It installs the `teaport` command and the configuration helper.
- It installs the plugin into your agent and connects the agent to the brain.
- It installs the Caddy reverse proxy and Avahi, and serves the browser page at
  `https://teaport.local`.
- It starts the services and checks the installation.

<!-- source: install.sh:59,266,1026,1436,1449,2412,2570,2576,1534,1695;manifest/stable.json:5-21 @1e5604c -->

## Optional components

teaport has three optional components. The installer sets up none of them by default:

- **Phone calls.** teaport can take calls on a phone line over SIP (Session Initiation
  Protocol). Phone calls are off by default; `teaport sip configure` turns them on. See
  [Set up phone calls](/guides/phone-calls/).
- **The Discord bridge.** The bridge joins a Discord voice channel and follows one user
  into the conversation. Turn it on with `TEAPORT_ENABLE_BRIDGE=1` when you install. See
  [Set up the Discord bridge](/guides/discord-bridge/).
- **A microphone on the device.** A ReSpeaker XVF3800 microphone array on the Jetson hears
  and speaks without a phone. Turn it on with `TEAPORT_ENABLE_LOCAL_AUDIO=1`. See
  [Use a microphone on the device](/guides/local-audio/).

<!-- source: install.sh:43,46,2385;cli/teaport:227;systemd/teaport-.service.in:10 @1e5604c -->

## Check the installation

1. On the Jetson, run this command:

   ```bash
   teaport status
   ```

   All three services show `active`, with the engine and brain ports and the memory use.

   <!-- source: cli/teaport:5,84,1387 @1e5604c -->

2. Run the health check:

   ```bash
   teaport doctor
   ```

   The doctor checks the install root, the engine and its models, the brain, the LLM
   configuration, the services, and the ports. Every check prints `ok`, or a warning that
   says what to fix.

   <!-- source: cli/teaport:6,183-213 @1e5604c -->

3. Optional: to confirm that speech recognition uses the fast path, read the engine's first
   messages:

   ```bash
   sudo journalctl -u teaport-engine | grep -i marlin
   ```

   You must see `Detected Marlin INT4 model (single-file format)`. Any other message means
   the engine uses a slower path.

If a check fails, see [Troubleshooting](/guides/troubleshooting/).

## Start your first conversation

The installer printed a web address at the end, similar to the following:

```text
browser: open https://teaport.local/#token=a-long-token from a LAN device (accept the one-time cert).
```

<!-- source: install.sh:2460-2461 @1e5604c -->

Open that address on the phone or laptop. Accept the certificate warning once: the device
signs its own certificate, and the browser needs HTTPS before it allows the microphone.

:::caution[The token is necessary, and it must follow a `#`]
The gateway refuses the connection without the token. The browser shows the page, then does
nothing.

The token must follow a `#` (a fragment), not a `?`. A fragment stays in your browser. A
query goes to the server and into its logs.

If you lose the address, run `openclaw dashboard --no-open` on the Jetson. On a NemoClaw
device, run the sandbox command that the installer printed instead. The token is also in
`~/.openclaw/openclaw.json` under `gateway.auth.token`.
:::

<!-- source: install.sh:2464-2466,1409-1417 @1e5604c -->

Start a Talk session in the page and speak. The device answers through the phone or the
laptop's microphone and speaker.

## What's next

The installation is done. From here:

- [Configure teaport](/guides/configuration/): every setting, including the LLM credentials.
- [teaport CLI reference](/reference/cli/): every `teaport` command.
- [Architecture](/reference/architecture/): how the engine, the brain, and the agent fit
  together.
