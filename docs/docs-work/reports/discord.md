# discord

## Changed files

- `src/content/docs/guides/discord-bridge.md` (new page, 89 lines)
- `docs-work/reports/discord.md` (this report)

## What changed

- New task page "Set up the Discord bridge" per the `discord` packet (PROMPT.md, Wave 1).
- Covers: what the bridge is (host-side media leg, brain keeps the conversation, single-session behavior, agent powers via its own Discord tools); creating the Discord bot and storing the token at `~/.config/teaport/discord_bot_token` mode 600; enabling via `TEAPORT_ENABLE_BRIDGE=1` plus `TEAPORT_BRIDGE_GUILD_ID` / `TEAPORT_BRIDGE_FOLLOW_USER_ID`; idempotent re-run and prompt fallback; token file/`DISCORD_BOT_TOKEN_FILE`/`DISCORD_BOT_TOKEN` precedence; checking via `systemctl status`, `journalctl -u teaport-discord-bridge`, and a join test; disabling with `systemctl disable --now`.
- 14 source citations to the pinned commit; key anchors: `bridge/discord/README.md:1-41`, `bridge/discord/index.js:1-55,392-418`, `install.sh:1888-1956`, `brain/teaport_brain/config_schema.toml:75-80,2151-2231`, verified with grep against `/tmp/teaport-pin`.
- Tuning settings (`BRIDGE_JOIN_CHANNEL_ID`, `BRIDGE_TTS_VOICE`, `BRIDGE_PRIME_MS`) deliberately left to the Settings reference; page links to it.

## Removed content

None (new page).

## Open questions

1. Canonical on-device invocation of the installer: the page shows `./install.sh` with env vars; no existing docs page documents how `install.sh` is fetched/run on the Jetson, and `install.sh:1888-1956` only assumes a repo checkout. Cross-check against the installation page when it exists; adjust the command block if the documented path differs (page section "Install and enable the bridge").

## Lint

node docs-work/lint.mjs src/content/docs/guides/discord-bridge.md
0 error(s), 0 warning(s) in 1 file(s)
(one fix pass: line 31 "settings page" -> approved wording)

Build: flock /tmp/teaport-docs-build.flock npx astro build -- 12 page(s) built, "Complete!" (lock acquired immediately; 2.57s)
