# openclaw-teaport-realtime

The OpenClaw **realtime-voice provider** for teaport. It connects OpenClaw's
gateway-relay Talk path to the teaport brain. Plain ESM with **no build
step**. It requires **Node ≥ 22** (it uses the global `WebSocket`).

- npm: `@teaspoon-ai/openclaw-teaport-realtime`
- Registers the `teaport` realtime-voice provider (see `openclaw.plugin.json`).
- Pairs with the brain in this repository (`brain/teaport_brain/gateway_server.py`).

## Install and enable

```bash
# from a checkout (local dev):
openclaw plugins install --link ./plugin
openclaw plugins enable teaport-realtime
# or, once published:
# openclaw plugins install @teaspoon-ai/openclaw-teaport-realtime
```

Configure in `~/.openclaw/openclaw.json`:

```jsonc
"talk": {
  "realtime": {
    "provider": "teaport",
    "mode": "realtime",
    "transport": "gateway-relay",
    "brain": "agent-consult",               // required — see the note below
    "providers": { "teaport": {
      "url": "ws://127.0.0.1:7861/talk",    // the teaport brain's /talk WS
      "voice": "af_heart",                   // optional: a voice id the engine provides
      "token": "…"                           // optional: must match the brain's GATEWAY_TOKEN
    } }
  }
}
```

The plugin appends `voice`, `language`, and `token` to the brain WebSocket URL
as query parameters for each session. The `token` value can also come from the
`TEAPORT_GATEWAY_TOKEN` environment variable.

### Why `brain` must be `"agent-consult"`

`brain` is OpenClaw's *tool/agent strategy for realtime sessions* — how the session
reaches tools and the agent. It does **not** decide who speaks: the realtime provider
(teaport) owns speech either way, so `"agent-consult"` cannot make OpenClaw
double-respond.

Two things require it:

- **`talk.client.create` rejects anything else.** It uses an explicit `brain` param when
  given and otherwise falls back to this config value, so a Talk UI client — which sends
  none — inherits whatever is set here. With `"none"`, every session fails at creation
  with `talk.client.create only supports brain="agent-consult"`.
- **It is the path this plugin uses.** The brain's `ask_openclaw` arrives as an
  `openclaw_agent_consult` tool call, which `provider.js` hands to the relay's in-process
  agent-consult machinery (working notices, then a final `submitToolResult` back to the
  brain). With no agent strategy, a consult has nothing to reach.

This was documented as `"none"` until 2026-09-08, on the reasoning that the teaport brain
orchestrates so OpenClaw should stay quiet. That confuses `brain` with response ownership.

## Tests

```bash
npm test           # syntax gate (node --check) — no brain needed, CI-safe
npm run test:live  # full bridge<->brain integration harness — needs a running brain + Node ≥ 22
```

`test/bridge_harness.mjs` exercises the real
`createBridge → connect → sendAudio → onAudio/onTranscript` path against a
running brain, with no OpenClaw gateway in the loop.

## Status

The plugin lives in `teaport` (`plugin/`) for now. The plugin and the
brain are two halves of one appliance and change together. npm publishes the
plugin from this subdirectory. It can move to its own public repository after
launch. MIT.
