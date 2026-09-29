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

## Context notes for a live session

A Talk client can give the voice some context during a call without speaking: a tap
on an on-screen character, a changed setting, a new camera view. The plugin
registers two gateway methods for this. Both need the `operator.talk` scope, the same
as `talk.session.*`, and work only for `gateway-relay` sessions.

```
teaport.talk.context
  params: {
    sessionId: string,   // the relaySessionId from talk.session.create
    text: string,        // plain language, e.g. "The user tapped the character's left shoulder twice."
    respond?: boolean,   // default false: context only. true: the voice also reacts aloud
    kind?: string        // optional short label for the brain's log, e.g. "ui-event"
  }
  result: { ok: true, status: "applied" | "queued" }
```

- The brain adds the note to the voice LLM's context as one quoted line, marked as an
  app event. It is never spoken and never captioned, and the voice is told that app
  events are never instructions. Leading `[tags]` in the text are removed.
- A note joins the context at a turn boundary, never mid-turn. Both statuses are a
  snapshot taken when the note arrived. `applied` means nothing was in flight: the
  note goes in at the next quiet moment (about 0.7 s without speech) or just before
  the user's next words, whichever comes first. `queued` means a turn was in flight:
  the note waits for that turn to end, or goes in just before the user's next words.
- With `respond: true` and the voice idle, the voice gives one short spoken
  reaction to that note. While it is speaking, the reaction waits until it stops. If
  the user speaks first, their turn answers the note instead. A reaction that finds
  no quiet moment within 20 s is dropped, and the note stays as context.
- Limits are set by the brain (`docs/CONFIG.md`, *Talk client context notes*): at
  most 1,000 characters per note (counted in Unicode code points), 20 notes kept in
  the context (the oldest are removed), and one `respond: true` note per 15 s. On top
  of those, any 10 notes may come at once, and after that one per second (fixed, not
  configurable). Notes over a limit are refused, never queued.
- Notes are accepted once the session's brain has said it takes them (its `hello`,
  sent when its pipeline is up, usually within a few seconds of
  `talk.session.create`). Before that a note gets the retryable `connecting`; a brain
  that sends no `hello` within 15 s of connecting predates context notes, and notes
  to it get `unsupported`.

Errors:

| Case | `code` | `details.reason` |
|---|---|---|
| Bad params, empty or too-long text | `INVALID_REQUEST` | `bad_params`, `empty`, `too_long` |
| The host has no Talk session with this id (OpenClaw 2026.8.1+ only; see below) | `INVALID_REQUEST` | `unknown_session` |
| Not a gateway-relay session | `INVALID_REQUEST` | `not_relay` |
| No live teaport voice session on this connection serves this id (another provider's session; or, before 2026.8.1, any id when the connection has no unbound teaport session) | `INVALID_REQUEST` | `no_voice_session` |
| Another connection's session, or a caller with no connection id. Before 2026.8.1 the plugin can tell only once the id is bound; until then another connection's id gets `no_voice_session` | `INVALID_REQUEST` | `not_owner` |
| Session closed, or closed before the brain answered | `INVALID_REQUEST` | `closed_session` |
| The session's brain predates context notes | `INVALID_REQUEST` | `unsupported` |
| The brain refused the note for a reason this plugin does not name | `INVALID_REQUEST` | `refused` |
| Too frequent | `UNAVAILABLE`, `retryable`, `retryAfterMs` | `rate_limited` |
| The brain has not said hello yet | `UNAVAILABLE`, `retryable`, `retryAfterMs` | `connecting` |
| The session's bridge is not bound yet (an instant after `talk.session.create`; the plugin waits up to 0.5 s for it first) | `UNAVAILABLE`, `retryable`, `retryAfterMs` | `bridge_not_ready` |
| The brain did not answer within 3 s | `UNAVAILABLE`, `retryable` | `brain_unavailable` |

```
teaport.talk.capabilities
  params: { sessionId?: string }
  result: {
    ok: true,
    context: { method: "teaport.talk.context", version: 1, respond: true },
    session?: {
      sessionId,
      state: "connecting" | "ready" | "unsupported",
      context: { maxChars, maxNotes, respondIntervalMs } | null   // null unless "ready"
    }
  }
```

A client that gets "unknown method" from either call is talking to an older plugin.
It should fall back to `chat.inject`, which reaches the text agent but not the voice.
`session.state` says where the session's brain stands: `connecting` until its
`hello` (ask again), `ready` with its limits, or `unsupported` for a brain that
predates context notes (final for this session). `maxChars` is the smaller of the
brain's limit and the plugin's own 16,000. The capabilities errors are the ones above
that apply to finding the session.

`createBridge` is never told which relay session it serves, so the plugin links the
two itself. It records the gateway connection that created each bridge. From OpenClaw
2026.8.1, where OpenClaw's Talk session registry is a global, the plugin binds each
bridge to its session the moment `talk.session.create` makes it, and checks every id
against that registry. OpenClaw 2026.7.x keeps the registry private, so there the
first note for a `sessionId` binds it to the connection's newest unbound teaport
bridge. That is right when the connection has one teaport session. With two live on
one connection (OpenClaw allows two), the first id used takes the newer bridge
whichever session it names, and an id OpenClaw never issued binds too.

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

## Tests

```bash
npm test           # syntax gate (node --check) + unit tests (node --test) — no brain needed, CI-safe
npm run test:live  # full bridge<->brain integration harness — needs a running brain + Node ≥ 22
```

`test/bridge_harness.mjs` exercises the real
`createBridge → connect → sendAudio → onAudio/onTranscript` path against a
running brain, with no OpenClaw gateway in the loop. `NOTE="…"` (plus `RESPOND=1`)
sends a context note after the greeting; see the header of the file.

## Status

The plugin lives in `teaport` (`plugin/`) for now. The plugin and the
brain are two halves of one appliance and change together. npm publishes the
plugin from this subdirectory. It can move to its own public repository after
launch. MIT.
