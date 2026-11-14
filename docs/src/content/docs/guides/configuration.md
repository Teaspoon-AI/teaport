---
title: Configure teaport
description: Change teaport settings in the configuration page or in the configuration files over SSH.
---

Each teaport service reads its settings once, at startup. To change a setting, edit it, then restart the service that reads it. Nothing hot-reloads.

<!-- source: brain/teaport_brain/config_ui.py:42-43#433b1e87 -->

You can make the edit in two places: the brain's configuration page, or the configuration files over SSH. The page edits the same files you edit by hand; the two paths differ only in convenience. For every setting, its default, and the file that holds it, see the [Settings reference](/reference/settings/).

## Before you begin

- The IP address of the device on your network. The steps call it `DEVICE_IP`.
- The gateway token `GATEWAY_TOKEN`, if you set one: the shared secret that clients use at `/talk`. You enter it on the page once, and the browser remembers it after that. The installer writes the same token into the plugin configuration.
- An SSH connection to the device, for the procedure that edits files.

<!-- source: brain/teaport_brain/config_ui.py:17-24#3f5d74ac; brain/teaport_brain/config_schema.toml:2228-2237#38d9c1ca; install.sh:2590#31d4462b -->

## Change a setting in the configuration page

The brain serves the page at `https://DEVICE_NAME.local:8443/settings/config`, on the device's settings site (see [Open the settings from your phone](/guides/configuration/#open-the-settings-from-your-phone)). On the device itself it also serves the page at `http://127.0.0.1:7861/config` (port 7861 is the default; if you changed `BRAIN_PORT`, the page moves with it). That plain-HTTP address answers only the device itself, so the token never crosses the network unencrypted; from another computer without the settings site, tunnel to it over SSH, for example `ssh -L 7861:127.0.0.1:7861 DEVICE_IP`, and open `http://127.0.0.1:7861/config`. The page edits the env files and the secret files where the settings live, and shows each value as the running services read it. It never displays a secret: for a secret it shows whether one is set, and it accepts a new value.

<!-- source: brain/teaport_brain/config_ui.py:100-116#6e4abba8; brain/teaport_brain/settings_access.py:221-233#82127577; brain/teaport_brain/config_ui.py:8-12,34-40#a24a491a; brain/teaport_brain/config_schema.toml:2203-2213#39346419; brain/teaport_brain/static/config.html:382-392#2b63b6f7 -->

To change a setting there:

1. Open `https://DEVICE_NAME.local:8443/settings/config#token` and enter `GATEWAY_TOKEN`. The browser remembers it. Over the SSH tunnel, open `http://127.0.0.1:7861/config`, which asks for the same token. If you did not set a token, the tunnelled address opens without one, and the brain warns about that at startup; the settings site never opens without a token or a phone session.
2. Find the setting: type its name or words from its description in the search field, or open the group that covers it. At first the page shows the settings most devices need. Turn on **Show tuning & diagnostics** to list the rest, along with the groups for parts the device has not set up. The page shows each value as the services read it, which is not always the way the file spells it.

   <!-- source: brain/teaport_brain/static/config.html:325,327,896,900#316e5355 -->
3. Enter the new value and save. The page checks the value against the schema, its type and its bounds, before it writes anything.
4. The page then lists every service that still runs the old values: a service whose configuration file changed after the unit started. Restart each one from the page, or apply the restarts yourself as [Apply the change](/guides/configuration/#apply-the-change) describes.

Each save also writes a timestamped copy of the previous file next to the original, and keeps a bounded ring of those backups.

<!-- source: brain/teaport_brain/config_apply.py:30-35,104-112#c890634a -->

## Open the settings from your phone

On a device with a front door, the owner can reach the same page from a phone without the token. Say "open my settings" at the device, or in a Talk conversation. You can also ask it as a question, such as "Can you open my settings?". A sentence that only mentions the settings, such as "How do I open my settings?", or one that goes on after the request, goes to the assistant as usual, which, with a model set up, answers it in words. Said while the assistant is talking, the phrase stops the answer, and the device shows the code and tells you so straight away, at the device and in Talk. The phrase works without the agent and without a model, so it also works before an AI account is set up. A phone call never hears it.

<!-- source: brain/teaport_brain/settings_access.py:16-20#8a2ca8e8; brain/teaport_brain/wifi_voice.py:451-469#9d2a2a28; brain/teaport_brain/agent_session.py:1006-1010#abcad8a7; brain/teaport_brain/wifi_voice.py:268-277#58fd5ac9 -->

When a model is set up, you can also ask in your own words, for example "I need to change my AI key". The model then opens the settings the same way the phrase does, with the same code on the screen. This works in the room and in Talk on a device whose settings site is set up, never on a phone call or through the Discord bridge, and `TEAPORT_TOOL_OPEN_SETTINGS` turns it off.

<!-- source: brain/teaport_brain/tools.py:1942-1948#c2a8ba3d -->

1. The device shows a QR code under the title "Settings", with the code itself under it. Scan the QR code with the phone's camera, then tap the link that the camera shows. A device without a screen that draws QR codes reads out an address, `DEVICE_NAME.local/settings` for example, and a six-character code instead: open the address in the phone's browser and enter the code there.
2. Tap **Continue**. You can use the code one time, within five minutes. Asking again replaces it.
3. The phone shows the **AI account** step at the top: tap **Connect your OpenRouter account**, or paste a key from your OpenRouter account and tap **Test and save**. Tap **Show advanced** for every other setting on the page.

<!-- source: brain/teaport_brain/settings_access.py:21-30#aa6588f4; brain/teaport_brain/settings_access.py:88-91#a87770ee; brain/teaport_brain/pairing.py:144-145#a83a8487 -->

The settings open at `https://DEVICE_NAME.local:8443/settings/config`: a port of their own, set by `TEAPORT_SETTINGS_PORT`, so that no page the gateway serves on the usual HTTPS port shares an origin with them. The plain address that the device reads out redirects there. Phones on your network must be able to reach TCP port 8443 on the device. The full installer sets this site up; an `install.sh --only brain` update does not touch Caddy, so on a device last fully installed before it, the device says that its settings can't be reached from a phone until you run the installer without `--only`. The phone stays signed in for an hour, in a cookie the browser sends only to that address over HTTPS. Without a session or the token nothing on that port opens, and an unset `GATEWAY_TOKEN` opens nothing there.

<!-- source: brain/teaport_brain/settings_access.py:78#2e08b20b; brain/teaport_brain/settings_access.py:330-340#8831dd56; brain/teaport_brain/settings_access.py:253-274#90eeb13f -->

Until the phone trusts the device's certificate, the browser warns that the connection is not private. The pages link to the steps that install the device's certificate on each kind of phone and computer.

<!-- source: brain/teaport_brain/settings_access.py:423-432#35429dc2 -->

If the device is on no network at all and spoken Wi-Fi setup is on, the device asks you to get it online first: a phone cannot reach it yet. A home network without internet is enough. In setup mode the setup page takes the key too, as [Connect the device to Wi-Fi](/guides/wifi-setup/#connect-your-ai-account) shows.

<!-- source: brain/teaport_brain/settings_access.py:101#fd6140cc -->

## Connect your OpenRouter account

In the **AI account** step, **Connect your OpenRouter account** signs you in to OpenRouter instead of pasting a key:

1. Tap **Connect your OpenRouter account**. The phone goes to OpenRouter. Sign in, or create an account, and approve the connection.
2. OpenRouter sends the phone to a Teaspoon page, which sends it on to the device's settings. The phone must be on the same network as the device.
3. Tap **Finish**. The device gets a key for your account from OpenRouter, checks it, and saves it the way it saves a pasted key, so OpenClaw gets it too and the trial credit warnings stop. A new key can be refused for a few seconds after OpenRouter makes it, so the device keeps asking for up to 6 more seconds before it treats the key as refused. If your OpenRouter account has no credit yet, the device still saves the key and says to add credit on OpenRouter before it can answer.

<!-- source: brain/teaport_brain/openrouter_connect.py:34-57#5068eb0d; brain/teaport_brain/ai_account.py:433-442#576c7858; brain/teaport_brain/ai_account.py:499-534#c7fe96e5 -->

The Teaspoon page is set by `TEAPORT_OPENROUTER_CONNECT_URL`. When that page does not answer, the sign-in works without it: OpenRouter shows a code at the end instead of sending the phone on. Copy the code, go back to the settings, and paste it into **Code from OpenRouter**. The Teaspoon page also shows the code when it cannot send the phone on. You can finish a sign-in one time, within 20 minutes of tapping the button, and OpenRouter's code expires 10 minutes after you approve. When anything fails, the page says why in plain words and the paste field stays. If the device gets a key but cannot save it, for example because OpenClaw does not take it, the settings offer **Save it again** for that key for 10 minutes.

<!-- source: brain/teaport_brain/openrouter_connect.py:18-32#acc5ebd9; brain/teaport_brain/openrouter_connect.py:63-69#30042127 -->

The button shows only on the settings site, at the port `TEAPORT_SETTINGS_PORT` sets (8443 by default), only when the device's endpoint is OpenRouter, and not when `LLM_API_KEY` is set somewhere the settings cannot change. The Teaspoon page keeps no secret: the code is useless without a verifier that only the device holds. The page sends the phone only to an HTTPS address on a home network, a `.local` name or a private IP address.

<!-- source: brain/teaport_brain/openrouter_connect.py:224-229#9f8c7080 -->

Setup mode can't use the sign-in, because neither the phone nor the device has internet there. When Wi-Fi setup ends without a key on the setup page, and the device has no key of your own, the device offers it: it says so out loud and shows the settings QR code on its screen, the same code that "open my settings" shows. A device without a screen that draws it reads out the address and the code instead.

<!-- source: brain/teaport_brain/openrouter_connect.py:232-247#4ad57aee; brain/teaport_brain/settings_access.py:94-99#d3d45cfa -->

## Change a setting in a configuration file

The [Settings reference](/reference/settings/) names the file that holds each setting. Except for the SIP configuration and the secret files, that is one of the env files in `/etc/teaport/`: `engine.env`, `brain.env`, `bridge.env`, `.env`, and `wifi-setup.env`. The SIP line keeps its own file, `~/.config/teaport/teaport-sip.conf`; [Set up phone calls](/guides/phone-calls/) covers what goes in it.

<!-- source: brain/teaport_brain/config_schema.toml:61-102#bddd0583 -->

The env files are root-owned, so edit them with sudo. Keep one `KEY=value` line per setting; comments and blank lines are fine. A boolean reads `0`, `false`, `no`, or `off` as off, and `1`, `true`, `yes`, or `on` as on. An empty value means the setting is unset, and the default applies; space around the value is not part of it. A flag that is off says so in the journal at startup. A few settings treat an empty value as a value in its own right, off for example; the Settings reference marks them.

<!-- source: brain/teaport_brain/settings.py:26-44#2c73b0c4; install.sh:2172#f2ca1efb -->

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

<!-- source: install.sh:2627,2784#999850dd; brain/teaport_brain/config_schema.toml:76,104-105#74b8d898; install.sh:1868-1870,1974-1975,2019-2021#d7455a4b -->

You edit `persona.md` whenever you like. A repair writes the starter persona only when the file is missing, so your changes to it survive.

<!-- source: install.sh:2637-2650#7403c329 -->

For every secret, an environment variable that is set wins over the file: every reader checks the environment first. A secret that is also set in an env file is a trap, because a new value in the file changes nothing while the variable is set. If you edit the file, remove the matching line from the env file too, or empty its value. The configuration page does this for you: saving a secret writes the file and drops the env-file copy of the same name. The units that read the secret at startup then show as pending, because their environment holds the old value until the restart.

<!-- source: brain/teaport_brain/config_ui.py:48-53#bb168482; brain/teaport_brain/settings.py:45#e8179f7f -->

A new value in a secret file takes hold on its own schedule. The brain reads the large language model (LLM) key once per session, so the next conversation uses it and no restart is needed; the Discord bridge reads its token once at startup, so a new token there needs the usual restart.

<!-- source: brain/teaport_brain/config_ui.py:44-47#b15d56cc -->

## Apply the change

Restart the service that reads the file you changed. The table lists each configuration file, the service that reads it, and what the restart does.

| File | Restart it with | What the restart does |
| --- | --- | --- |
| `/etc/teaport/engine.env` | `sudo systemctl restart teaport-engine` | Reloads the models on the GPU; the brain loses its engine session, a Talk session, and a phone call alike. |
| `/etc/teaport/brain.env` | `sudo systemctl restart teaport-brain` | Ends the live Talk session. A phone call stays up at the gateway; the restarted brain takes the call back and says it lost you for a moment. |
| `~/.config/teaport/teaport-sip.conf` | `teaport sip restart` | Restarts the gateway process; `teaport sip configure` test-registers before it writes a new conf. |
| `/etc/teaport/bridge.env` | `sudo systemctl restart teaport-discord-bridge` | Restarts the bridge. |
| `/etc/teaport/local-audio.env` | `sudo systemctl restart teaport-local-audio` | Ends the local audio session, and the bridge dials the brain again at once. |

<!-- source: brain/teaport_brain/config_schema.toml:61-102#bddd0583; brain/teaport_brain/sip_server.py:45-52#9213415f; install.sh:1149#38ebb95b -->

A change to `wifi-setup.env` needs no restart: the Wi-Fi setup unit reads the file each time it runs, and the unit runs only on request.

<!-- source: brain/teaport_brain/config_schema.toml:98-102#a13e1c84 -->

On the configuration page, the restart runs the same command two seconds later. The delay gives the brain time to answer the page before it replaces itself, and the timer survives the brain exiting.

<!-- source: brain/teaport_brain/config_apply.py:37-43#77a7cfcd -->

## Set up the language model

Three settings point the brain at its LLM: `LLM_BASE_URL`, the address of an OpenAI-compatible endpoint, which is required; `LLM_API_KEY`, the key for that endpoint; and `LLM_MODEL`, the name of the served model. The installer writes the name the endpoint takes: `openai/gpt-oss-120b` for OpenRouter, `gpt-oss-120b` for any other endpoint. Endpoints include `https://api.groq.com/openai/v1`, `https://api.cerebras.ai/v1`, and `https://openrouter.ai/api/v1`.

<!-- source: brain/teaport_brain/config_schema.toml:226-257#c8ffd4ac -->

:::caution[gpt-oss-120b: pick who serves it]
On Groq and on Cerebras, `gpt-oss-120b` sometimes breaks down in the middle of a reply, most often right after it used a tool (looked something up, checked the time), and the device says it lost its train of thought. BaseTen serves the same model without that. Use OpenRouter as the endpoint, `openai/gpt-oss-120b` as the model, and send OpenRouter to BaseTen first with `LLM_EXTRA_BODY`:

```ini
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=openai/gpt-oss-120b
LLM_EXTRA_BODY={"provider":{"order":["BaseTen","Groq"],"allow_fallbacks":false}}
```

Groq stays second, for when BaseTen does not answer. Write `LLM_EXTRA_BODY` in `brain.env` exactly as shown, on one line.
:::

<!-- source: brain/teaport_brain/config_schema.toml:296-303#bd725c65 -->

### With the configuration page

1. Open the configuration page as [Change a setting in the configuration page](/guides/configuration/#change-a-setting-in-the-configuration-page) shows.
2. In the language model section, enter your endpoint in `LLM_BASE_URL`.
3. Enter the key in `LLM_API_KEY`. Before it saves the key, the page first sends one request with it: for OpenRouter, the key's own record at `/api/v1/key`, and for any other endpoint, its `/models` list. If the endpoint refuses the key or does not answer, the page says why and saves the key only when you choose **Save anyway**. For an OpenRouter key that works, the page also reads the account's credit at `/api/v1/credits`, or, when OpenRouter does not answer that for the key, the key's own record, where an account that has never paid for credit, with a key that has no limit of its own, counts as having none. When the account has no credit, it saves the key and says that the device can't answer until you add credit on OpenRouter. The page writes the key to `~/.config/teaport/llm_key` and drops any `LLM_API_KEY` copy from the env file, so the new key is the one that applies.
4. Enter the model name in `LLM_MODEL`, or leave it empty for the default, `gpt-oss-120b`.
5. Save. The page lists the brain as still running the old values; restart it from the page, or as [Apply the change](/guides/configuration/#apply-the-change) describes.

<!-- source: brain/teaport_brain/config_ui.py:48-53#bb168482; brain/teaport_brain/config_schema.toml:238-257#4b586e90; brain/teaport_brain/ai_account.py:177-187#7532ad3d; brain/teaport_brain/config_ui.py:676-693#3511862a; brain/teaport_brain/ai_account.py:499-534#c7fe96e5 -->

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

   <!-- source: install.sh:2019-2021#37da8b7d -->

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

<!-- source: brain/teaport_brain/credit.py:16-32#673997c1; brain/teaport_brain/ai_account.py:253-259#1b6deadd; brain/teaport_brain/config_schema.toml:1813-1849#3b64f06d -->

The first two are said one time, at the start of a conversation in the room or in Talk, and again at most once a day while they last. They tell you to say "open my settings" and connect your own account. The device does not listen while one plays, and a line that you interrupt anyway is said again at the next conversation. A phone call or the Discord bridge never hears about the trial credit. The configuration page shows the same state as a line in the **AI account** step, and `teaport status` shows it as `trial credit (brain)`.

<!-- source: brain/teaport_brain/credit.py:32-42#bd581a1c; brain/teaport_brain/credit.py:98-101#5b43489c -->

When nothing is left, the device stops answering questions. It does not switch to another model. It says that its trial credit has run out and how to connect your own account; "open my settings" and setup mode still work by voice. After you save your own key, the next conversation answers again.

<!-- source: brain/teaport_brain/credit.py:104-105#58343f27; brain/teaport_brain/services.py:1398-1402#1508a36d -->

## One key for OpenClaw too

On a device with OpenClaw beside the brain, OpenClaw also pays for its answers at OpenRouter. When you save a key on the configuration page or on the setup page, the brain gives OpenClaw the same key as its OpenRouter key, so the device has one key and one balance. The brain does this with OpenClaw's own command, `openclaw models auth paste-api-key --provider openrouter`, and passes the key on its input, never on the command line. OpenClaw keeps its model choice. Unless `OPENCLAW_AGENT_ID` names an agent, OpenClaw chooses the agent itself, its only configured agent; with several agents and none named, the save fails and nothing changes.

<!-- source: brain/teaport_brain/agent_key.py:11-32#5a301540; brain/teaport_brain/agent_key.py:34-39#688a246f -->

Both take the key, or neither does, and one save runs at a time, even when the setup page and the configuration page save at the same time. If OpenClaw cannot take it, the brain keeps the key that it had, and the page says that the key was not saved. If OpenClaw might have taken the new key before it failed, the brain gives it the previous key back. When that is not possible, or a save stops midway, the page asks you to save the key again, and `teaport status` says that the two might be on different keys until a save goes through. When OpenClaw's gateway runs on another host and does not share this device's auth store, sharing does not apply: the brain saves the key alone, and `teaport status` says that OpenClaw keeps its own key there. Each time the brain starts, it reads OpenClaw's `gateway.mode` again, so a save shares the key again after the gateway is local.

<!-- source: brain/teaport_brain/agent_key.py:67-77#2758bddd; brain/teaport_brain/agent_key.py:11-32#5a301540; brain/teaport_brain/agent_key.py:565-573#c05312bd -->

When OpenClaw needs a restart to use a new key, the brain restarts it with `openclaw gateway restart --safe`, but only after no conversation has been live for 30 seconds after the last one ended: not in the room, in Talk, or on a phone call. The gateway then restarts after its own running work finishes. A key that you write to the key file by hand, or that the installer writes, is not copied. If both already have the same key, the brain finds that out one time, while nobody is talking, from what `openclaw models status --json` shows of OpenClaw's saved key; until then, or when that shows nothing it can compare, `teaport status` says `unknown, not yet synced`, and the next key that you save settles it. `teaport status` shows where it stands as `model key (OpenClaw)`. To keep a separate key for OpenClaw, set `TEAPORT_AGENT_SHARES_KEY=false`.

<!-- source: brain/teaport_brain/agent_key.py:55-65#78e2d9ae; brain/teaport_brain/agent_key.py:41-53#3259fff3; cli/teaport:169-199#a609a6f5 -->

## What's next

- [Settings reference](/reference/settings/), for every setting, its default, and the file it lives in.
- [Set up phone calls](/guides/phone-calls/), for the SIP line and its configuration file.
- [Troubleshooting](/guides/troubleshooting/), when a change does not behave as you expected.
