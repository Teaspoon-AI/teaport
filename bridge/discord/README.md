# Discord voice bridge

Native realtime Discord voice for your teaport assistant. Discord voice media
is RTP over **UDP** to per-call voice servers — the NemoClaw sandbox is
TCP-only by design — so this small host-side process owns only the media leg
and pipes audio to the brain's `/talk` WebSocket. The brain keeps owning the
conversation (STT, LLM, TTS, barge-in), exactly as it does for the dashboard
Talk path.

```
Discord voice servers ⇄ UDP ⇄ bridge (host) ⇄ ws://127.0.0.1:7861/talk (brain)
```

The bridge follows ONE configured user: when they join a voice channel in the
configured server, the bot joins and bridges; when they leave, it leaves. The
brain is single-session — a Discord call and a dashboard Talk session evict
each other.

## Setup

Create a Discord application + bot (Developer Portal), enable the **Server
Members** intent, invite it to your server with Connect + Speak permissions,
and save the bot token to `~/.config/teaport/discord_bot_token` (mode 0600).

**Installer path (recommended).** The teaport installer sets all of this up
when you opt in: re-run it with `TEAPORT_ENABLE_BRIDGE=1` (or with the token
already saved) and it installs the deps under the prefix, writes the
`teaport-discord-bridge` service, and enables it. Pass
`TEAPORT_BRIDGE_GUILD_ID` / `TEAPORT_BRIDGE_FOLLOW_USER_ID` non-interactively, or
let it prompt. The manual steps below are for development or a standalone bridge.

```bash
cd bridge/discord
npm install     # native @discordjs/opus builds from source on arm64 (needs gcc/make/python3);
                # opusscript is the pure-JS fallback if no toolchain is present
```

## Configuration (environment)

| Var | Required | Meaning |
|---|---|---|
| `BRIDGE_GUILD_ID` | **yes** | Discord server (guild) id to serve |
| `BRIDGE_FOLLOW_USER_ID` | **yes** | user the bot follows into voice channels |
| `DISCORD_BOT_TOKEN` / `DISCORD_BOT_TOKEN_FILE` | token or file | defaults to `~/.config/teaport/discord_bot_token` |
| `BRAIN_URL` | no | brain WebSocket (default `ws://127.0.0.1:7861/talk`) |
| `BRIDGE_PRIME_MS` | no | downlink jitter-buffer prime (default 40; lower = less latency) |
| `BRIDGE_JOIN_CHANNEL_ID` | no | force-join a voice channel at startup (smoke tests) |
| `TEAPORT_VOICE` | no | TTS voice id passed to the brain |

## Run

```bash
BRIDGE_GUILD_ID=<server-id> BRIDGE_FOLLOW_USER_ID=<user-id> node index.js
```

The log prints `logged in as <bot>` then either joins your current voice
channel or waits for you. `timing:` log lines report latency health (Discord
ping, uplink forwarding, downlink hold) for by-ear tuning.
