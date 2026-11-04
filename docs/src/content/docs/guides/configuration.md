---
title: Configure teaport
description: Change teaport settings in the configuration page or in the configuration files over SSH.
---

Each teaport service reads its settings once, at startup. To change a setting, edit it, then restart the service that reads it. Nothing hot-reloads.

<!-- source: brain/teaport_brain/config_ui.py:37-38#433b1e87 -->

You can make the edit in two places: the brain's configuration page, or the configuration files over SSH. The page edits the same files you edit by hand; the two paths differ only in convenience. For every setting, its default, and the file that holds it, see the [Settings reference](/reference/settings/).

## Before you begin

- The IP address of the device on your network. The steps call it `DEVICE_IP`.
- The gateway token `GATEWAY_TOKEN`, if you set one: the shared secret that clients use at `/talk`. You add it to the page address once, and the browser remembers it after that. The installer writes the same token into the plugin configuration.
- An SSH connection to the device, for the procedure that edits files.

<!-- source: brain/teaport_brain/config_ui.py:15-17#68fec1eb; brain/teaport_brain/config_schema.toml:2177-2186#38d9c1ca; install.sh:2087#31d4462b -->

## Change a setting in the configuration page

The brain serves the page at `http://DEVICE_IP:7861/config` (port 7861 is the default; if you changed `BRAIN_PORT`, the page moves with it). The page edits the env files and the secret files where the settings live, and shows each value as the running services read it. It never displays a secret: for a secret it shows whether one is set, and it accepts a new value.

<!-- source: brain/teaport_brain/config_ui.py:8-12,29-35#a24a491a; brain/teaport_brain/config_schema.toml:2152-2162#39346419; brain/teaport_brain/static/config.html:283-293#2b63b6f7 -->

To change a setting there:

1. Open `http://DEVICE_IP:7861/config?token=GATEWAY_TOKEN`. After the page has loaded once with the token in the address, the browser remembers it. If you did not set a token, open the address without the parameter; an unset token means the page is open, and the brain warns about that at startup.
2. Find the setting in the group that covers it. The page shows each value as the services read it, which is not always the way the file spells it.
3. Enter the new value and save. The page checks the value against the schema, its type and its bounds, before it writes anything.
4. The page then lists every service that still runs the old values: a service whose configuration file changed after the unit started. Restart each one from the page, or apply the restarts yourself as [Apply the change](/guides/configuration/#apply-the-change) describes.

Each save also writes a timestamped copy of the previous file next to the original, and keeps a bounded ring of those backups.

<!-- source: brain/teaport_brain/config_apply.py:33-38,107-115#c890634a -->

## Open the settings from your phone

On a device with a front door, the owner can reach the same page from a phone without the token. Say "open my settings" at the device, or in a Talk conversation. The phrase works without the agent and without a model, so it also works before an AI account is set up. A phone call never hears it.

<!-- source: brain/teaport_brain/settings_access.py:16-20#8a2ca8e8; brain/teaport_brain/agent_session.py:891-895#abcad8a7 -->

1. The device shows a QR code under the title "Settings", with the code itself under it. Scan the QR code with the phone's camera, then tap the link that the camera shows. A device without a screen that draws QR codes reads out an address, `DEVICE_NAME.local/settings` for example, and a six-character code instead: open the address in the phone's browser and enter the code there.
2. Tap **Continue**. You can use the code one time, within five minutes. Asking again replaces it.
3. The phone shows the **AI account** step at the top: paste a key from your OpenRouter account and tap **Test and save**. Tap **Show advanced** for every other setting on the page.

<!-- source: brain/teaport_brain/settings_access.py:21-30#aa6588f4; brain/teaport_brain/settings_access.py:88-91#a87770ee; brain/teaport_brain/pairing.py:144-145#a83a8487 -->

The settings open at `https://DEVICE_NAME.local:8443/settings/config`: a port of their own, set by `TEAPORT_SETTINGS_PORT`, so that no page the gateway serves on the usual HTTPS port shares an origin with them. The plain address that the device reads out redirects there. Phones on your network must be able to reach TCP port 8443 on the device. The full installer sets this site up; an `install.sh --only brain` update does not touch Caddy, so on a device last fully installed before it, the device says that its settings can't be reached from a phone until you run the installer without `--only`. The phone stays signed in for an hour, in a cookie the browser sends only to that address over HTTPS. Without a session nothing on that port opens, whether or not `GATEWAY_TOKEN` is set. The token keeps working on `http://DEVICE_IP:7861/config`.

<!-- source: brain/teaport_brain/settings_access.py:76#2e08b20b; brain/teaport_brain/settings_access.py:272-277#251d20e7; brain/teaport_brain/settings_access.py:207-216#f38a8411 -->

Until the phone trusts the device's certificate, the browser warns that the connection is not private. The pages link to the steps that install the device's certificate on each kind of phone and computer.

<!-- source: brain/teaport_brain/settings_access.py:359-368#35429dc2 -->

If the device is on no network at all and spoken Wi-Fi setup is on, the device asks you to get it online first: a phone cannot reach it yet. A home network without internet is enough. In setup mode the setup page takes the key too, as [Connect the device to Wi-Fi](/guides/wifi-setup/#connect-your-ai-account) shows.

<!-- source: brain/teaport_brain/settings_access.py:93#fd6140cc -->

## Change a setting in a configuration file

The [Settings reference](/reference/settings/) names the file that holds each setting. Except for the SIP configuration and the secret files, that is one of the env files in `/etc/teaport/`: `engine.env`, `brain.env`, `bridge.env`, `.env`, and `wifi-setup.env`. The SIP line keeps its own file, `~/.config/teaport/teaport-sip.conf`; [Set up phone calls](/guides/phone-calls/) covers what goes in it.

<!-- source: brain/teaport_brain/config_schema.toml:61-102#bddd0583 -->

The env files are root-owned, so edit them with sudo. Keep one `KEY=value` line per setting; comments and blank lines are fine. A boolean reads `0`, `false`, `no`, or `off` as off, and `1`, `true`, `yes`, or `on` as on. An empty value means the setting is unset, and the default applies; space around the value is not part of it. A flag that is off says so in the journal at startup. A few settings treat an empty value as a value in its own right, off for example; the Settings reference marks them.

<!-- source: brain/teaport_brain/settings.py:26-44#2c73b0c4; install.sh:1776#f2ca1efb -->

To change a setting this way:

1. Sign in to the device:

   ```bash
   ssh USER@DEVICE_IP
   ```

   Replace the following:

   - `USER`: your user name on the device.
   - `DEVICE_IP`: the IP address of the device.

2. Open the file that holds the setting. The brain's settings, for example:

   ```bash
   sudo nano /etc/teaport/brain.env
   ```

3. Change the value. The format rules earlier on this page apply.
4. Apply the change: restart the service that reads the file, as [Apply the change](/guides/configuration/#apply-the-change) describes.

## Where secrets live

The secret files live in `~/.config/teaport/`: `llm_key`, `openclaw_token`, `discord_bot_token`, `persona.md`, and `teaport-sip.conf`. The installer creates the directory with mode 700 and writes each secret with mode 600, and it keeps the secrets out of the units and images.

<!-- source: install.sh:2132,2296#999850dd; brain/teaport_brain/config_schema.toml:76,104-105#74b8d898; install.sh:1525-1527,1614-1615,1630-1632#d7455a4b -->

You edit `persona.md` whenever you like. A repair writes the starter persona only when the file is missing, so your changes to it survive.

<!-- source: install.sh:2141-2154#7403c329 -->

For every secret, an environment variable that is set wins over the file: every reader checks the environment first. A secret that is also set in an env file is a trap, because a new value in the file changes nothing while the variable is set. If you edit the file, remove the matching line from the env file too, or empty its value. The configuration page does this for you: saving a secret writes the file and drops the env-file copy of the same name. The units that read the secret at startup then show as pending, because their environment holds the old value until the restart.

<!-- source: brain/teaport_brain/config_ui.py:43-48#bb168482; brain/teaport_brain/settings.py:45#e8179f7f -->

A new value in a secret file takes hold on its own schedule. The brain reads the large language model (LLM) key once per session, so the next conversation uses it and no restart is needed; the Discord bridge reads its token once at startup, so a new token there needs the usual restart.

<!-- source: brain/teaport_brain/config_ui.py:39-42#b15d56cc -->

## Apply the change

Restart the service that reads the file you changed. The table lists each configuration file, the service that reads it, and what the restart does.

| File | Restart it with | What the restart does |
| --- | --- | --- |
| `/etc/teaport/engine.env` | `sudo systemctl restart teaport-engine` | Reloads the models on the GPU; the brain loses its engine session, a Talk session, and a phone call alike. |
| `/etc/teaport/brain.env` | `sudo systemctl restart teaport-brain` | Ends the live Talk session. A phone call stays up at the gateway; the restarted brain takes the call back and says it lost you for a moment. |
| `~/.config/teaport/teaport-sip.conf` | `teaport sip restart` | Restarts the gateway process; `teaport sip configure` test-registers before it writes a new conf. |
| `/etc/teaport/bridge.env` | `sudo systemctl restart teaport-discord-bridge` | Restarts the bridge. |
| `/etc/teaport/local-audio.env` | `sudo systemctl restart teaport-local-audio` | Ends the local audio session, and the bridge dials the brain again at once. |

<!-- source: brain/teaport_brain/config_schema.toml:61-102#bddd0583; brain/teaport_brain/sip_server.py:45-52#9213415f; install.sh:891#38ebb95b -->

A change to `wifi-setup.env` needs no restart: the Wi-Fi setup unit reads the file each time it runs, and the unit runs only on request.

<!-- source: brain/teaport_brain/config_schema.toml:98-102#a13e1c84 -->

On the configuration page, the restart runs the same command two seconds later. The delay gives the brain time to answer the page before it replaces itself, and the timer survives the brain exiting.

<!-- source: brain/teaport_brain/config_apply.py:40-46#77a7cfcd -->

## Set up the language model

Three settings point the brain at its LLM: `LLM_BASE_URL`, the address of an OpenAI-compatible endpoint, which is required; `LLM_API_KEY`, the key for that endpoint; and `LLM_MODEL`, the name of the served model, `gpt-oss-120b` by default. Endpoints include `https://api.groq.com/openai/v1`, `https://api.cerebras.ai/v1`, and `https://openrouter.ai/api/v1`; for a server on the device itself, `http://127.0.0.1:8182/v1`.

<!-- source: brain/teaport_brain/config_schema.toml:220-251#cdd4a315 -->

### With the configuration page

1. Open the configuration page as [Change a setting in the configuration page](/guides/configuration/#change-a-setting-in-the-configuration-page) shows.
2. In the language model section, enter your endpoint in `LLM_BASE_URL`.
3. Enter the key in `LLM_API_KEY`. Before it saves the key, the page sends one request with it: for OpenRouter, the key's own record at `/api/v1/key`, and for any other endpoint, its `/models` list. If the endpoint refuses the key or does not answer, the page says why and saves the key only when you choose **Save anyway**. The page writes the key to `~/.config/teaport/llm_key` and drops any `LLM_API_KEY` copy from the env file, so the new key is the one that applies. A server that ignores keys still needs a placeholder, `sk-local` for example.
4. Enter the model name in `LLM_MODEL`, or leave it empty for the default, `gpt-oss-120b`.
5. Save. The page lists the brain as still running the old values; restart it from the page, or as [Apply the change](/guides/configuration/#apply-the-change) describes.

<!-- source: brain/teaport_brain/config_ui.py:43-48#bb168482; brain/teaport_brain/config_schema.toml:232-251#3812d0b5; brain/teaport_brain/ai_account.py:140-150#7532ad3d; brain/teaport_brain/config_ui.py:608-621#0c2a4295 -->

### In the configuration files

1. On the device, set the endpoint in `/etc/teaport/brain.env`:

   ```ini
   # the address of your endpoint
   LLM_BASE_URL=https://api.groq.com/openai/v1
   ```

2. Leave `LLM_MODEL` unset for the default, `gpt-oss-120b`; otherwise set it to the name of the model your endpoint serves.
3. Write the key to the secret file:

   ```bash
   printf '%s' 'API_KEY' > ~/.config/teaport/llm_key
   chmod 600 ~/.config/teaport/llm_key
   ```

   Replace the following:

   - `API_KEY`: the key your endpoint gave you.

   The mode keeps the file readable only by you, as the installer writes it.

   <!-- source: install.sh:1630-1632#37da8b7d -->

4. If `brain.env` also sets `LLM_API_KEY`, remove that line or empty its value; the environment wins over the file.
5. Restart the brain as [Apply the change](/guides/configuration/#apply-the-change) describes.

## The included trial credit

A device can ship with a starter key: an OpenRouter key with a small spending limit, so that the device answers before you connect your own account. Provisioning marks which key that is in `~/.local/state/teaport/starter_key_sha256`, the run user's state directory: the SHA-256 of the key in hexadecimal, alone on one line. The file holds the hash only, never the key. Write it with `printf %s "$KEY" | sha256sum | cut -d' ' -f1 > ~/.local/state/teaport/starter_key_sha256`, where `$KEY` holds the key without a newline; the hash of the key followed by one newline, which `sha256sum` of a key file gives, also counts. Any other key that you save later is your own, and everything in this section stops.

<!-- source: brain/teaport_brain/credit.py:5-14#fc01df58; brain/teaport_brain/credit.py:59#cb5b8dc8 -->

While the device runs on the starter key, the brain reads the key's limit and what is left from OpenRouter, `GET /api/v1/key`, every 30 minutes and after each conversation. The read never holds up a reply, and a read that fails changes nothing. The device warns you at three points:

- When half of the credit is used, set by `TEAPORT_STARTER_HALF_USED`.
- When $1 or less is left, set by `TEAPORT_STARTER_LOW_USD`.
- When nothing is left.

When OpenRouter's answer also has an `effective_budget` with a limit and what is left, the brain judges by whichever has less left, the key's own limit or that budget. Saving a key reads it the same way: a key whose budget is used up is not saved, as with a key that has no credit left.

<!-- source: brain/teaport_brain/credit.py:16-32#673997c1; brain/teaport_brain/ai_account.py:210-216#1b6deadd; brain/teaport_brain/config_schema.toml:1762-1798#3b64f06d -->

The first two are said one time, at the start of a conversation in the room or in Talk, and again at most once a day while they last. They tell you to say "open my settings" and connect your own account. The device does not listen while one plays, and a line that you interrupt anyway is said again at the next conversation. A phone call or the Discord bridge never hears about the trial credit. The configuration page shows the same state as a line in the **AI account** step, and `teaport status` shows it as `trial credit (brain)`.

<!-- source: brain/teaport_brain/credit.py:32-42#bd581a1c; brain/teaport_brain/credit.py:98-101#5b43489c -->

When nothing is left, the device stops answering questions. It does not switch to another model. It says that its trial credit has run out and how to connect your own account; "open my settings" and setup mode still work by voice. After you save your own key, the next conversation answers again.

<!-- source: brain/teaport_brain/credit.py:104-105#58343f27; brain/teaport_brain/services.py:950-954#1508a36d -->

## One key for OpenClaw too

On a device with OpenClaw beside the brain, OpenClaw also pays for its answers at OpenRouter. When you save a key on the configuration page or on the setup page, the brain gives OpenClaw the same key as its OpenRouter key, so the device has one key and one balance. The brain does this with OpenClaw's own command, `openclaw models auth paste-api-key --provider openrouter`, and passes the key on its input, never on the command line. OpenClaw keeps its model choice. Unless `OPENCLAW_AGENT_ID` names an agent, OpenClaw chooses the agent itself, its only configured agent; with several agents and none named, the save fails and nothing changes.

<!-- source: brain/teaport_brain/agent_key.py:11-32#5a301540; brain/teaport_brain/agent_key.py:34-39#688a246f -->

Both take the key, or neither does, and one save runs at a time, even when the setup page and the configuration page save at the same time. If OpenClaw cannot take it, the brain keeps the key that it had, and the page says that the key was not saved. If OpenClaw might have taken the new key before it failed, the brain gives it the previous key back. When that is not possible, or a save stops midway, the page asks you to save the key again, and `teaport status` says that the two might be on different keys until a save goes through. When OpenClaw's gateway runs on another host and does not share this device's auth store, sharing does not apply: the brain saves the key alone, and `teaport status` says that OpenClaw keeps its own key there. Each time the brain starts, it reads OpenClaw's `gateway.mode` again, so a save shares the key again after the gateway is local.

<!-- source: brain/teaport_brain/agent_key.py:65-75#2758bddd; brain/teaport_brain/agent_key.py:11-32#5a301540; brain/teaport_brain/agent_key.py:563-571#c05312bd -->

When OpenClaw needs a restart to use a new key, the brain restarts it with `openclaw gateway restart --safe`, but only after no conversation has been live for 30 seconds after the last one ended: not in the room, in Talk, or on a phone call. The gateway then restarts after its own running work finishes. A key that you write to the key file by hand, or that the installer writes, is not copied. If both already have the same key, the brain finds that out one time, while nobody is talking, from what `openclaw models status --json` shows of OpenClaw's saved key; until then, or when that shows nothing it can compare, `teaport status` says `unknown, not yet synced`, and the next key that you save settles it. `teaport status` shows where it stands as `model key (OpenClaw)`. To keep a separate key for OpenClaw, set `TEAPORT_AGENT_SHARES_KEY=false`.

<!-- source: brain/teaport_brain/agent_key.py:53-63#78e2d9ae; brain/teaport_brain/agent_key.py:41-52#7d213413; cli/teaport:143-173#a609a6f5 -->

## What's next

- [Settings reference](/reference/settings/), for every setting, its default, and the file it lives in.
- [Set up phone calls](/guides/phone-calls/), for the SIP line and its configuration file.
- [Troubleshooting](/guides/troubleshooting/), when a change does not behave as you expected.
