---
title: Set up the Discord bridge
description: Install the optional Discord bridge so teaport joins you in a voice channel on your Discord server.
---

The Discord bridge is an optional host service that carries a Discord voice channel into teaport. When the user it follows joins a voice channel on your server, the teaport bot joins the same channel: you talk to the assistant through Discord, and it answers there. The installer adds the bridge only when you ask for it; a plain install leaves no bridge behind.

<!-- source: bridge/discord/README.md:1-19#adc3e30a; install.sh:2114-2123#9125a571 -->

## How the bridge works

Discord carries voice as UDP media, and the brain runs in a sandbox that allows only TCP. The bridge therefore runs on the host next to the brain, owns only the audio leg, and pipes it to the brain's `/talk` endpoint. The brain keeps doing what it does on the dashboard Talk page: speech recognition, the language model, speech synthesis, and barge-in.

<!-- source: bridge/discord/README.md:3-8#1292d453; bridge/discord/index.js:1-12#2bb29166 -->

The brain holds one conversation at a time. When the bot joins, a live dashboard Talk session ends; a live bridge session refuses the dashboard the same way. Joining again replaces the bot's own session. What the assistant can do in your server, such as posting or pinning, comes from the agent's own Discord tools and permissions.

<!-- source: bridge/discord/README.md:14-28#88c67612 -->

## Before you begin

- A running teaport brain: the bridge pipes the audio to it over `ws://127.0.0.1:7861/talk`.
- Node.js 22 or later on the host. The installer stops when it is missing.
- The ID of your Discord server (the guild ID) and your Discord user ID, both numeric.

<!-- source: bridge/discord/index.js:5-8#7578d7f1; bridge/discord/index.js:25#448d6113; bridge/discord/index.js:45-52#07dbad15; install.sh:2127#c8db4d8e -->

## Create the Discord bot

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications) and add a bot to it.
2. Enable the Server Members intent for the bot.
3. Invite the bot to your server, and give it the Connect and Speak permissions.
4. Save the token to `~/.config/teaport/discord_bot_token` with mode `600`:

   ```bash
   mkdir -p ~/.config/teaport
   printf '%s' DISCORD_BOT_TOKEN > ~/.config/teaport/discord_bot_token
   chmod 600 ~/.config/teaport/discord_bot_token
   ```

   Replace `DISCORD_BOT_TOKEN` with the token from the application you created.
   The file now holds the token, and only your user can read it.

<!-- source: bridge/discord/README.md:30-34#e127e2a2; install.sh:2144-2146#bd40f660 -->

## Install and enable the bridge

The installer does the rest: it copies the bridge, installs its dependencies, writes `/etc/teaport/bridge.env` and the `teaport-discord-bridge` unit, and enables the service.

<!-- source: install.sh:2161-2177#661f6e8a; bridge/discord/README.md:36-39#214091a7 -->

- On the Jetson, re-run the installer with the bridge turned on:

  ```bash
  TEAPORT_ENABLE_BRIDGE=1 TEAPORT_BRIDGE_GUILD_ID=GUILD_ID \
      TEAPORT_BRIDGE_FOLLOW_USER_ID=USER_ID ./install.sh
  ```

  Replace the following:

  - `GUILD_ID`: the ID of your Discord server.
  - `USER_ID`: the ID of the user the bot follows into voice channels.

The installer writes both values to `/etc/teaport/bridge.env`.

Leave the variables out and the installer prompts for the server and the user, and for the token when no token file exists. The installer also accepts the token as `DISCORD_BOT_TOKEN` and writes it to the token file. A repair run keeps the values already in `bridge.env` unless you set the variables again.

<!-- source: install.sh:2129-2146#b5b61c2e -->

The token itself never lands in the unit or in `bridge.env`: the env file records the token path, `DISCORD_BOT_TOKEN_FILE`, `~/.config/teaport/discord_bot_token` by default. The bridge reads the token from that file at startup, and a `DISCORD_BOT_TOKEN` environment variable wins when set. A new token needs a bridge restart to take effect.

<!-- source: install.sh:2142-2143#8e83a59d; brain/teaport_brain/config_schema.toml:2283-2303#282a1296 -->

## Check the bridge

1. Check that the service is enabled and active:

   ```bash
   systemctl status teaport-discord-bridge
   ```

   An inactive unit with no log entries means the installer skipped the bridge; re-run it with `TEAPORT_ENABLE_BRIDGE=1`.
2. Follow the log while you test:

   ```bash
   journalctl -u teaport-discord-bridge -f
   ```

   While the bridge waits for the user, the output is similar to the following:

   ```text
   waiting for user 271828182845159064 to join a voice channel
   ```

   The line `bot is not in the configured guild` means you have not added the bot to the server in `BRIDGE_GUILD_ID`.
3. Join a voice channel on the server as the user the bot follows, and speak. The bot joins the channel with you and leaves when you leave; the assistant hears you and answers in the channel.

<!-- source: bridge/discord/index.js:392-411#cd325e18; bridge/discord/README.md:14-16#297e36b4 -->

## Turn off the bridge

- To stop the bridge and keep it from starting at boot:

  ```bash
  sudo systemctl disable --now teaport-discord-bridge
  ```

  The bot leaves the channel, and the unit no longer starts at boot. To use the bridge again, run the same command with `enable`.

<!-- source: bridge/discord/index.js:414-418#e6746af4 -->

## What's next

For the settings the bridge reads, and for the other ways to talk to teaport, see:

- [Configure teaport](/guides/configuration/)
- [Settings reference](/reference/settings/)
- [Phone calls](/guides/phone-calls/)
- [Troubleshooting](/guides/troubleshooting/)
