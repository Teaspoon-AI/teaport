---
title: Installing teaport
description: Install teaport on a Jetson Orin Nano (8 GB).
---

:::note[Pre-launch]
teaport is not released yet. This page describes an installation that we performed and
measured on a Jetson Orin Nano Dev Kit (8 GB). The commands are correct. The hosted
artifacts can change before the first release.
:::

You need a Jetson Orin Nano Dev Kit (8 GB) that you own, SSH (Secure Shell) access or a
screen, and a home network. You must supply your own large language model (LLM). Allow
30 to 45 minutes. Most of that time is the download of 4.6 GB of models.

## What you need

- A **Jetson Orin Nano, NX, or AGX**, flashed with **JetPack 7.2**. JetPack 7.2 installs
  Linux for Tegra (L4T) **R39.2** and CUDA 13.2. The Orin Nano with 8 GB of memory is the
  reference device.
- An **agent**: OpenClaw on the device, or NemoClaw (NVIDIA's OpenClaw package). The agent
  gives you the browser interface, the Talk session, and the tools. You can install teaport
  without an agent, but then nothing can talk to it. See [Install an agent](#3-install-an-agent).
- Your own **large language model (LLM)**. Use a cloud key from OpenRouter, Groq, or
  Cerebras. You can also use a local OpenAI-compatible server. See
  [Configuration](/guides/configuration/).
- A **phone or laptop with a browser** on the same network. Its microphone and speaker carry
  the audio. Nothing connects to the Jetson with a cable.
- Approximately **21 GB of free disk space**: 4.6 GB for the engine and the models, and the
  swap file.

## 1. Flash JetPack 7.2

This step is necessary. The engine works only with JetPack 7.2. Follow NVIDIA's flashing
guide. Teaspoon AI does not host a copy of JetPack.

Use NVIDIA SDK Manager or the Jetson Linux flashing tools. Do not use the JetPack 7.2 ISO:
NVIDIA records a known problem where the ISO does not make the 25 W and MAXN SUPER power
modes available.

## 2. Prepare the Jetson

Set the power mode, the swap file, and stop the desktop. See
[Prepare the Jetson](/getting-started/prepare-the-jetson/). This step is optional but
recommended.

## 3. Install an agent

Choose **one** of the two agents.

### Option A — OpenClaw on the device

OpenClaw needs Node.js version 22 or later. JetPack 7.2 does not include Node.js.

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
it migrates its database, and the lock it holds is not released for about five minutes. The
gateway then restarts in a loop and refuses every connection.

If this happens: run `openclaw gateway stop`, wait five minutes, then run
`openclaw gateway start` once.
:::

Check the gateway:

```bash
openclaw gateway status
```

The line `Runtime:` must say `running`, and `Connectivity probe:` must say `ok`. The gateway
listens on `127.0.0.1:18789`.

### Option B — NemoClaw

NemoClaw is NVIDIA's installer. It runs OpenClaw inside a container. Run it in your own
terminal and give your own consent:

```bash
bash <(curl -fsSL https://www.nvidia.com/nemoclaw.sh)
```

Run NemoClaw as your own user. Do not use `sudo`. The teaport installer looks for
`~/.nemoclaw/sandboxes.json` in your home folder. If NemoClaw installs into another user's
home folder, the teaport installer does not find it.

## 4. Install teaport

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport)
```

The installer asks you three things:

1. **The engine license.** Read it, then type `accept`.
2. **Your LLM endpoint, model, and key.** These questions come *after* the download of
   4.6 GB. The installer waits for you.
3. Nothing else. The installer then completes without further questions.

For OpenRouter, answer:

| Question | Answer |
|---|---|
| LLM base URL | `https://openrouter.ai/api/v1` |
| LLM model | `openai/gpt-oss-120b` |
| LLM API key | your OpenRouter key |

:::caution[OpenRouter needs the full model name]
The installer offers `gpt-oss-120b` as the default. This name works with Groq and with
Cerebras. It does **not** work with OpenRouter. OpenRouter needs the provider name and the
model name: `openai/gpt-oss-120b`. If you accept the default, the installation completes and
then fails at the first sentence you speak.
:::

You can run the installer again at any time. It repairs an existing installation. It does
not make a second installation. It keeps any settings that you added to
`/etc/teaport/brain.env` yourself.

The installer does these tasks:

- It installs the engine, the models, and the pronunciation dictionary into `/opt/teaport`.
- It installs the brain into a Python environment.
- It installs the `teaport` command into `/usr/local/bin`.
- It installs the plugin into your agent and connects the agent to the brain.
- It installs Caddy and Avahi to give you `https://teaport.local`.
- It starts the services and runs a self-test.

## 5. Open teaport in a browser

The installer prints a web address at the end. It looks like this:

```
browser: open https://teaport.local/#token=<a long token> from a LAN device (accept the one-time cert).
```

Open that address on your phone or laptop. Accept the certificate warning once. The device
uses a certificate that it signs itself. The browser needs HTTPS before it allows the use of
a microphone.

:::caution[The token is necessary, and it must follow a `#`]
The gateway refuses the connection without the token. The browser shows the page, then does
nothing.

The token must follow a `#` (a fragment), not a `?`. A fragment stays in your browser. A
query is sent to the server and written to its logs.

If you lose the address, run `openclaw dashboard --no-open` on the Jetson. The token is also
in `~/.openclaw/openclaw.json` under `gateway.auth.token`.
:::

Then start a Talk session and speak.

## 6. Check the installation

```bash
teaport status
teaport doctor
```

`teaport doctor` checks the engine, the models, the pronunciation dictionary, the services,
and the ports. Every line must show a green mark.

To confirm that speech recognition uses the fast path, read the engine's first messages:

```bash
sudo journalctl -u teaport-engine | grep -i marlin
```

You must see `Detected Marlin INT4 model (single-file format)`. Any other message means the
engine uses a slower path. See [Troubleshooting](/guides/troubleshooting/).

## If something does not work

Run `teaport status` and `teaport doctor` first. See
[Troubleshooting](/guides/troubleshooting/). If that page does not solve your problem,
[open an issue](https://github.com/Teaspoon-AI/teaport/issues) and include the output of
both commands.

## Related

- [Prepare the Jetson](/getting-started/prepare-the-jetson/)
- [Configuration](/guides/configuration/)
- [teaport CLI](/reference/cli/)
- [Architecture](/reference/architecture/)
