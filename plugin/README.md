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
`TEAPORT_GATEWAY_TOKEN` environment variable. It also appends `captions=2`, the
caption protocol it speaks (see below).

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

## Transcripts in the Talk view

The brain sends the full text on every transcript event. The Control UI's Talk view
in OpenClaw 2026.8.1 and later (and in the 2026.7.2 prereleases) merges assistant
text differently from user text. It appends each assistant partial as a delta, and
only a final replaces the bubble. 2026.7.1 and earlier, and the 2026.7.33–7.35
maintenance releases, replace the bubble with each partial instead; there is no
2026.7.2 stable release. The package declares `openclaw >=2026.4.0`. The captions
were checked against the Talk view code of 2026.7.1, 2026.7.35 and 2026.9.1. The
optional `talk.realtime.providers.teaport.assistantTranscripts` setting picks what
the plugin sends:

- `"auto"` (the default) reads the host version (`api.runtime.version`) once, when
  the first Talk session starts. It sends deltas on 2026.7.2 and newer, and when the
  version can't be read or reads `0.0.0` (how OpenClaw reports a version it couldn't
  resolve). It sends full text to older hosts. The 2026.7.33–7.35 maintenance
  releases get deltas too, which only delays a repeated word until the final.
- `"delta"` or `"full"` fixes the shape. Use it for a client whose view is versioned
  separately from the gateway, or for a host the version check gets wrong. Case and
  surrounding spaces are ignored.

The gateway log shows which one each session got (`teaport-realtime: assistant captions
sent as …`). The plugin predicts when the view starts a new user entry, since that
closes the assistant bubble. When it can't tell, it assumes a new entry: a wrong
guess splits a bubble in two, but never leaves one open or repeats text. In delta
mode the plugin shapes the assistant bubbles:

- Each partial is the text added since the last one.
- The view closes the open bubble when user words start a new user entry. That
  happens on the first words after the user's last final, or on words that don't
  continue the open entry once the voice has spoken since. The plugin first sends a
  final with exactly the bubble's text, so the session transcript keeps what was
  shown. Other user text only updates its entry, and the bubble stays open.
- If the voice carries on after a cut, the next bubble, and that utterance's final,
  carry only the text after the cut.
- A barge-in (`clear`) closes the open bubble the same way. The brain sends no final
  for a barged utterance, and captions that still arrive for it are dropped.
- When the brain ends the session, the open bubble is closed the same way. When
  OpenClaw ends it (the user stops Talk, the client disconnects, the session
  expires), OpenClaw 2026.9.1 drops the session before it closes the provider and
  ignores what the plugin sends then, so the open bubble gets no final.
- A tool card gets a bubble of its own. It waits while a caption bubble is open, and
  follows once that bubble closes. The tool runs when the model writes the call, but
  captions follow the audio, so the card would otherwise land mid-sentence.

The brain tags each caption with its utterance id, so the plugin can tell the voice
carrying on from a new reply that begins with the same words. An older brain sends
no id, so its captions only continue a bubble that is still open. User transcripts
are always full text.

In full mode, user words that start an entry, and a barge-in, close the bubble with
a final of its text too. An assistant final that only repeats a bubble the user's words closed is
dropped, since it would show up again after the user's message. A carry-on after a
cut shows the whole utterance again.

With `captions=2` on the URL, the brain sends every utterance's final and leaves the
repeat to the plugin. Without it (an older plugin, or another client), the brain
skips a final that exactly repeats the bubble while the user is talking, as it
always has. See `TalkTranscriptAdapter` in `provider.js`.

## Barge-in

The brain decides when the user has interrupted the voice: it stops for speech it
actually heard and sends `{"type":"clear"}`. The provider declares
`supportsBargeIn: false`, which keeps OpenClaw's own barge-in out of it:

- OpenClaw 2026.9.6 reads that from `talk.catalog`. Its Control UI then does not
  run its loudness-based barge-in, and its relay ignores barge-in cancels.
- OpenClaw 2026.9.1 does neither. Its Control UI cancels the output whenever its
  microphone is loud while the voice plays, the voice's own echo included, and the
  relay closes the session if the provider does not confirm the cancel within 1 s.
  The plugin confirms it at once (`handleBargeIn`), so the session survives; the
  browser still drops the audio it had queued, so the voice skips a moment and
  carries on. The cancel still aborts a desktop-agent consult in flight.

## Tests

```bash
npm test           # syntax gate (node --check) + transcript and barge-in unit tests — no brain needed, CI-safe
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
