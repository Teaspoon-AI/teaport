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
    "brain": "agent-consult",               // the default; never "none" — see the note below
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
caption protocol it speaks (see below), `session_config=1` (see
[What OpenClaw tells the voice](#what-openclaw-tells-the-voice)), and `run_control=1`
(see [Spoken run control](#spoken-run-control)).

## What OpenClaw tells the voice

OpenClaw composes what a realtime voice surface is told and hands it to the provider
with each session (`req.instructions` and `req.tools`, OpenClaw 2026.9.7 and newer):
its realtime voice instructions, the operator's `talk.realtime.instructions`, the
agent paragraph and the IDENTITY / USER / SOUL profile, and the consult tools
`openclaw_agent_consult` and `openclaw_agent_control`. As soon as the brain's socket
opens, before any audio, the plugin sends them as one frame:

```json
{"type": "session_config", "instructions": "<req.instructions, or \"\">", "tools": [<req.tools, or none>]}
```

The frame always goes out, empty or not: `session_config=1` on the URL tells the brain
to wait for it, so it must always come. The brain starts the session's system prompt
with the instructions as sent, then its own voice rules, and gives its language model
OpenClaw's `openclaw_agent_consult` and `openclaw_agent_control` schemas from the tool
list. A brain that gets no flag (another client) builds its own prompt, as before.

### Spoken run control

A request the agent is still working on can be asked about, cancelled, redirected or
added to by voice, two ways, which never act on the same words:

- **OpenClaw's relay** reads every final user transcript the brain sends while a consult
  runs, and acts on what its classifier recognizes: "cancel that" cancels the run,
  "how's it going?" reports on it, "actually ..." steers it, "also ..." queues a
  follow-up. Those words are the relay's, not the model's (below).
- **The model** gets `openclaw_agent_control` for everything else, in any phrasing or
  language ("scratch that", "how much longer?", "olvídalo"), as OpenClaw's own voice
  rules tell it to. The relay does not run it: the brain's call goes to the relay's
  `onToolCall` like a consult's, the relay shows it to the Talk client as a `toolCall`
  event, and the client runs it (the Control UI: `talk.session.steer`) and submits the
  result back through the relay, which the plugin passes to the brain for that call's
  id. The brain waits at most 10 s for it (the Control UI holds a tool result until the
  voice it is playing has finished) and has the model say it in plain words.

What keeps the brain in step:

- **The relay's reply is spoken.** The relay hands its answer to the provider
  (`sendUserMessage`); the plugin sends it to the brain as a context note marked
  `"source": "host"`, and the brain's next reply says what it means for the user's
  request in plain words (OpenClaw's status names "the active OpenClaw run" and tools,
  so it is not read out verbatim). Only the plugin sets that mark;
  `teaport.talk.context` never does, and the brain believes it only from a client that
  announced `?run_control=1`.
- **The brain does not answer the relay's words.** For a final said while it has a
  consult running, the brain asks `{"type": "run_control", "id", "text"}` and holds that
  turn for the answer. The plugin answers `{"type": "run_control_result", "id",
  "claimed"}`. Not claimed, at once: no consult the relay acknowledged is unanswered,
  OpenClaw's classifier (`shouldAutoControlRealtimeVoiceAgentText`) does not call the
  words control, or the relay's echo filter would drop them
  (`isLikelyRealtimeVoiceAssistantEchoTranscript` over the finals the plugin forwarded,
  12 s lookback, as the relay keeps them). Otherwise claimed only once the relay acts on
  them (its reply, or a consult's `{"status": "cancelled"}` result), and not claimed if it
  has not within 2 s: the relay counts a consult's run only once the Talk client's
  `talk.client.toolCall` and `chat.send` are acknowledged, and forgets it just before the
  result, so in those moments it acts on nothing and the model answers instead. A
  claimed turn ends with nothing for the model to answer. A second final said while one
  is held is asked about in its turn.
- **A cancelled consult is owed nothing.** The relay's `{"status": "cancelled"}` result
  closes the consult silently on the brain's side, whichever way it was cancelled. A
  control call's own result is the control's, never read as a consult's.
- **A consult the brain gave up on stops.** Past its own wait the brain sends
  `{"type": "tool_cancel", "call_id"}`, and the plugin tells the relay the call was
  cancelled (`tool.call.cancelled`), which aborts the run, so the relay's run control
  has nothing left to act on either.

The room mic and the phone have no relay: no run control, and no control tool.

### The room mic and the phone

The box's own microphone and its phone line reach the brain without OpenClaw's Talk
relay, and so does any other `/talk` client that sends no `session_config` (the Discord
bridge), so the brain asks the plugin for their context at the start of each session:

```
GET /teaport/voice-context?front_end=room|sip|<client>[&agent=<id>]   (gateway auth: the gateway token)
-> 200 {"version": 1, "agentId": "...", "instructions": "...", "tools": [...], "openclawVersion": "..."}
-> 400 {"error": "agent_unknown"}
-> 503 {"error": "host_unsupported" | "agent_unresolved" | "context_unavailable"}
```

The plugin builds it with OpenClaw's SDK the way OpenClaw's own phone surface
(voice-call) does: `resolveRealtimeVoiceAgentContextInstructions` for the agent the
brain's `/v1/chat/completions` consults answer with, or the configured agent the brain
names (`agent=`, its `OPENCLAW_AGENT_ID` when set), resolved with
`resolveSessionAgentIdStrict` as 2026.9.7 asks new plugins to — the agent paragraph and the IDENTITY / USER / SOUL profile, at most 6,000
characters, no session key — and `REALTIME_VOICE_AGENT_CONSULT_TOOL`. The brain follows
it with its own voice rules. A gateway still starting or restarting (503 with
`Retry-After`, or refusing connections) is asked again for a few seconds by a room
session opening asleep and for about two by anyone else; past that, or on any other
failure, the brain uses its persona file and logs a warning, and a room session built
that way asks once more when its wake word is heard.

### Why `brain` must be `"agent-consult"`

It is the default: with no `brain` in the config, `talk.client.create` uses
`"agent-consult"`, and `talk.catalog` lists teaport with it because the provider
declares `supportsToolCalls: true`. Setting it to anything else breaks Talk.

`brain` is OpenClaw's *tool/agent strategy for realtime sessions* — how the session
reaches tools and the agent. It does **not** decide who speaks: the realtime provider
(teaport) owns speech either way, so `"agent-consult"` cannot make OpenClaw
double-respond.

Two things require it:

- **`talk.client.create` rejects anything else.** It uses an explicit `brain` param when
  given and otherwise falls back to this config value, so a Talk UI client — which sends
  none — inherits whatever is set here. With `"none"`, every session fails at creation
  with `talk.client.create only supports brain="agent-consult"`.
- **It is the path this plugin uses.** The brain's consult is an
  `openclaw_agent_consult` tool call, which `provider.js` hands to the relay's in-process
  agent-consult machinery (working notices, then a final `submitToolResult` back to the
  brain). With no agent strategy, a consult has nothing to reach.

This was documented as `"none"` until 2026-09-08, on the reasoning that the teaport brain
orchestrates so OpenClaw should stay quiet. That confuses `brain` with response ownership.

## Context notes for a live session

A Talk client can give the voice some context during a call without speaking: a tap
on an on-screen character, a changed setting, a new camera view. The plugin
registers two gateway methods for this. Both need the `operator.talk` scope, the one
OpenClaw 2026.9 gives its own Talk methods, and work only for `gateway-relay` sessions
on OpenClaw 2026.9 and newer.

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
- Notes are accepted once the session's brain is ready for them. It says what it
  takes (its `hello`) as soon as it accepts the connection, and `ready` once its
  pipeline runs and its speech recognition works, usually within a few seconds of
  `talk.session.create`. Before `ready` a note gets the retryable `connecting`. A
  session whose brain finds no speech recognition never gets `ready`: it plays a
  warning and ends, and its notes go from `connecting` to `closed_session`. A brain
  that sends no `hello` within 15 s of connecting predates context notes, and notes
  to it get `unsupported`; so do notes to a brain that said `hello` but no `ready`
  within 30 s.

Errors:

| Case | `code` | `details.reason` |
|---|---|---|
| Bad params, empty or too-long text | `INVALID_REQUEST` | `bad_params`, `empty`, `too_long` |
| The host has no Talk session with this id | `INVALID_REQUEST` | `unknown_session` |
| Not a gateway-relay session | `INVALID_REQUEST` | `not_relay` |
| No teaport voice session serves this id (another provider's session) | `INVALID_REQUEST` | `no_voice_session` |
| Another connection's session, or a caller with no connection id | `INVALID_REQUEST` | `not_owner` |
| The host does not expose its Talk session registry (OpenClaw before 2026.9) | `INVALID_REQUEST` | `host_unsupported` |
| Session closed, or closed before the brain answered | `INVALID_REQUEST` | `closed_session` |
| The session's brain predates context notes | `INVALID_REQUEST` | `unsupported` |
| The brain refused the note for a reason this plugin does not name | `INVALID_REQUEST` | `refused` |
| Too frequent | `UNAVAILABLE`, `retryable`, `retryAfterMs` | `rate_limited` |
| The brain has not said `ready` yet | `UNAVAILABLE`, `retryable`, `retryAfterMs` | `connecting` |
| The session's bridge is not bound yet (an instant after `talk.session.create`; the plugin waits up to 0.5 s for it first) | `UNAVAILABLE`, `retryable`, `retryAfterMs` | `bridge_not_ready` |
| The brain did not answer within 3 s | `UNAVAILABLE`, `retryable` | `brain_unavailable` |

```
teaport.talk.capabilities
  params: { sessionId?: string }
  result: {
    ok: true,
    context: { method: "teaport.talk.context", version: 1, respond: true } | null,  // null: host unsupported
    session?: {
      sessionId,
      state: "connecting" | "ready" | "unsupported",
      context: { maxChars, maxNotes, respondIntervalMs } | null   // null unless "ready"
    }
  }
```

A client that gets "unknown method" from either call is talking to an older plugin.
It should fall back to `chat.inject`, which reaches the text agent but not the voice.
`session.state` says where the session's brain stands: `connecting` until it says
`ready` (ask again), `ready` with its limits, or `unsupported` for a brain that
predates context notes, a brain that never became ready, or a host that does not
expose its Talk session registry (final for this session). `maxChars` is the smaller
of the brain's limit and the plugin's own 16,000. The capabilities errors are the ones
above that apply to finding the session; on an unsupported host it answers `ok` with
`context: null` instead of `host_unsupported`.

`createBridge` is never told which relay session it serves, so the plugin links the
two itself. It records the gateway connection that created each bridge (through the
SDK's `getPluginRuntimeGatewayRequestScope`), binds each bridge to its session the
moment `talk.session.create` makes it, and checks every id against OpenClaw's Talk
session registry. That registry is internal to OpenClaw, not part of its SDK; it is a
global on 2026.9.1 and 2026.9.6, where this was checked.

## Transcripts in the Talk view

The brain sends the full text on every transcript event. The Control UI's Talk view
in OpenClaw 2026.8.1 and later (and in the 2026.7.2 prereleases) merges assistant
text differently from user text. It appends each assistant partial as a delta, and
only a final replaces the bubble. 2026.7.1 and earlier, and the 2026.7.33–7.35
maintenance releases, replace the bubble with each partial instead; there is no
2026.7.2 stable release. The package declares `openclaw >=2026.9.7`. The captions
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
npm test           # syntax gate (node --check) + unit tests (node --test) — no brain needed, CI-safe
npm run test:live  # full bridge<->brain integration harness — needs a running brain + Node ≥ 22
```

`test/bridge_harness.mjs` exercises the real
`createBridge → connect → sendAudio → onAudio/onTranscript` path against a
running brain, with no OpenClaw gateway in the loop. `NOTE="…"` (plus `RESPOND=1`)
sends a context note after the greeting; see the header of the file.

`test/note_smoke.mjs` is the same check through a running OpenClaw gateway, the way a
Talk client does it: one gateway connection creates a session, waits for
`teaport.talk.capabilities` to report it ready, sends a `respond: true` note and
prints the spoken reaction (`RESULT: PASS` / `FAIL`). It runs on the gateway host
from a directory where `openclaw` resolves (copy it into the installed plugin's
directory), and it ends any Talk call in progress: the brain serves one at a time.

## Status

The plugin lives in `teaport` (`plugin/`) for now. The plugin and the
brain are two halves of one appliance and change together. npm publishes the
plugin from this subdirectory. It can move to its own public repository after
launch. MIT.
