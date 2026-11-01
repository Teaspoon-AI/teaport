---
title: Configure teaport
description: Change teaport settings in the configuration page or in the configuration files over SSH.
---

Each teaport service reads its settings once, at startup. To change a setting, edit it, then restart the service that reads it. Nothing hot-reloads.

<!-- source: brain/teaport_brain/config_ui.py:27-28#433b1e87 -->

You can make the edit in two places: the brain's configuration page, or the configuration files over SSH. The page edits the same files you edit by hand; the two paths differ only in convenience. For every setting, its default, and the file that holds it, see the [Settings reference](/reference/settings/).

## Before you begin

- The IP address of the device on your network. The steps call it `DEVICE_IP`.
- The gateway token `GATEWAY_TOKEN`, if you set one: the shared secret that clients use at `/talk`. You add it to the page address once, and the browser remembers it after that. The installer writes the same token into the plugin configuration.
- An SSH connection to the device, for the procedure that edits files.

<!-- source: brain/teaport_brain/config_ui.py:15-17#f8a4e1af; brain/teaport_brain/config_schema.toml:1941-1950#38d9c1ca; install.sh:1887#31d4462b -->

## Change a setting in the configuration page

The brain serves the page at `http://DEVICE_IP:7861/config` (port 7861 is the default; if you changed `BRAIN_PORT`, the page moves with it). The page edits the env files and the secret files where the settings live, and shows each value as the running services read it. It never displays a secret: for a secret it shows whether one is set, and it accepts a new value.

<!-- source: brain/teaport_brain/config_ui.py:8-12,19-25#a24a491a; brain/teaport_brain/config_schema.toml:1916-1926#39346419; brain/teaport_brain/static/config.html:263-273#2b63b6f7 -->

To change a setting there:

1. Open `http://DEVICE_IP:7861/config?token=GATEWAY_TOKEN`. After the page has loaded once with the token in the address, the browser remembers it. If you did not set a token, open the address without the parameter; an unset token means the page is open, and the brain warns about that at startup.
2. Find the setting in the group that covers it. The page shows each value as the services read it, which is not always the way the file spells it.
3. Enter the new value and save. The page checks the value against the schema, its type and its bounds, before it writes anything.
4. The page then lists every service that still runs the old values: a service whose configuration file changed after the unit started. Restart each one from the page, or apply the restarts yourself as [Apply the change](/guides/configuration/#apply-the-change) describes.

Each save also writes a timestamped copy of the previous file next to the original, and keeps a bounded ring of those backups.

<!-- source: brain/teaport_brain/config_apply.py:33-38,107-115#c890634a -->

## Change a setting in a configuration file

The [Settings reference](/reference/settings/) names the file that holds each setting. Except for the SIP configuration and the secret files, that is one of the env files in `/etc/teaport/`: `engine.env`, `brain.env`, `bridge.env`, `.env`, and `wifi-setup.env`. The SIP line keeps its own file, `~/.config/teaport/teaport-sip.conf`; [Set up phone calls](/guides/phone-calls/) covers what goes in it.

<!-- source: brain/teaport_brain/config_schema.toml:61-102#bddd0583 -->

The env files are root-owned, so edit them with sudo. Keep one `KEY=value` line per setting; comments and blank lines are fine. A boolean reads `0`, `false`, `no`, or `off` as off, and `1`, `true`, `yes`, or `on` as on. An empty value means the setting is unset, and the default applies; space around the value is not part of it. A flag that is off says so in the journal at startup. A few settings treat an empty value as a value in its own right, off for example; the Settings reference marks them.

<!-- source: brain/teaport_brain/settings.py:26-44#2c73b0c4; install.sh:1664#f2ca1efb -->

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

<!-- source: install.sh:1931,2087#999850dd; brain/teaport_brain/config_schema.toml:76,104-105#74b8d898; install.sh:1421-1423,1510-1511,1526-1528#d7455a4b -->

You edit `persona.md` whenever you like. A repair writes the starter persona only when the file is missing, so your changes to it survive.

<!-- source: install.sh:1940-1953#7403c329 -->

For every secret, an environment variable that is set wins over the file: every reader checks the environment first. A secret that is also set in an env file is a trap, because a new value in the file changes nothing while the variable is set. If you edit the file, remove the matching line from the env file too, or empty its value. The configuration page does this for you: saving a secret writes the file and drops the env-file copy of the same name. The units that read the secret at startup then show as pending, because their environment holds the old value until the restart.

<!-- source: brain/teaport_brain/config_ui.py:33-38#bb168482; brain/teaport_brain/settings.py:45#e8179f7f -->

A new value in a secret file takes hold on its own schedule. The brain reads the large language model (LLM) key once per session, so the next conversation uses it and no restart is needed; the Discord bridge reads its token once at startup, so a new token there needs the usual restart.

<!-- source: brain/teaport_brain/config_ui.py:29-32#b15d56cc -->

## Apply the change

Restart the service that reads the file you changed. The table lists each configuration file, the service that reads it, and what the restart does.

| File | Restart it with | What the restart does |
| --- | --- | --- |
| `/etc/teaport/engine.env` | `sudo systemctl restart teaport-engine` | Reloads the models on the GPU; the brain loses its engine session, a Talk session, and a phone call alike. |
| `/etc/teaport/brain.env` | `sudo systemctl restart teaport-brain` | Ends the live Talk session. A phone call stays up at the gateway; the restarted brain takes the call back and says it lost you for a moment. |
| `~/.config/teaport/teaport-sip.conf` | `teaport sip restart` | Restarts the gateway process; `teaport sip configure` test-registers before it writes a new conf. |
| `/etc/teaport/bridge.env` | `sudo systemctl restart teaport-discord-bridge` | Restarts the bridge. |
| `/etc/teaport/local-audio.env` | `sudo systemctl restart teaport-local-audio` | Ends the local audio session, and the bridge dials the brain again at once. |

<!-- source: brain/teaport_brain/config_schema.toml:61-102#bddd0583; brain/teaport_brain/sip_server.py:45-52#9213415f; install.sh:895#38ebb95b -->

A change to `wifi-setup.env` needs no restart: the Wi-Fi setup unit reads the file each time it runs, and the unit runs only on request.

<!-- source: brain/teaport_brain/config_schema.toml:98-102#a13e1c84 -->

On the configuration page, the restart runs the same command two seconds later. The delay gives the brain time to answer the page before it replaces itself, and the timer survives the brain exiting.

<!-- source: brain/teaport_brain/config_apply.py:40-46#77a7cfcd -->

## Set up the language model

Three settings point the brain at its LLM: `LLM_BASE_URL`, the address of an OpenAI-compatible endpoint, which is required; `LLM_API_KEY`, the key for that endpoint; and `LLM_MODEL`, the name of the served model, `gpt-oss-120b` by default. Endpoints include `https://api.groq.com/openai/v1`, `https://api.cerebras.ai/v1`, and `https://openrouter.ai/api/v1`; for a server on the device itself, `http://127.0.0.1:8182/v1`.

<!-- source: brain/teaport_brain/config_schema.toml:208-239#cdd4a315 -->

### With the configuration page

1. Open the configuration page as [Change a setting in the configuration page](/guides/configuration/#change-a-setting-in-the-configuration-page) shows.
2. In the language model section, enter your endpoint in `LLM_BASE_URL`.
3. Enter the key in `LLM_API_KEY`. The page writes it to `~/.config/teaport/llm_key` and drops any `LLM_API_KEY` copy from the env file, so the new key is the one that applies. A server that ignores keys still needs a placeholder, `sk-local` for example.
4. Enter the model name in `LLM_MODEL`, or leave it empty for the default, `gpt-oss-120b`.
5. Save. The page lists the brain as still running the old values; restart it from the page, or as [Apply the change](/guides/configuration/#apply-the-change) describes.

<!-- source: brain/teaport_brain/config_ui.py:33-38#bb168482; brain/teaport_brain/config_schema.toml:220-239#3812d0b5 -->

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

   <!-- source: install.sh:1526-1528#37da8b7d -->

4. If `brain.env` also sets `LLM_API_KEY`, remove that line or empty its value; the environment wins over the file.
5. Restart the brain as [Apply the change](/guides/configuration/#apply-the-change) describes.

## What's next

- [Settings reference](/reference/settings/), for every setting, its default, and the file it lives in.
- [Set up phone calls](/guides/phone-calls/), for the SIP line and its configuration file.
- [Troubleshooting](/guides/troubleshooting/), when a change does not behave as you expected.
