# Architecture

> **Scaffold.** This is an outline. The full document comes with the public
> docs pass.

One appliance, three parts:

- **Engine** (`:8000`) — speech-to-text and speech synthesis. The installer
  downloads it. Any engine works here if it is compatible with the
  teaport-engine interface.
- **Brain** (`brain/teaport_brain`, `:7861/talk` and the SIP gateway's socket) —
  the Pipecat pipeline, in one process (`teaport-brain`) with two front-ends: the
  `/talk` WebSocket and the phone line (`sip_server.py`, which connects to the
  `teaport-sip` gateway whenever it is up). It
  connects your speech, your LLM, and the spoken reply. It owns barge-in, the
  heard-grounding ledger, memory recall, tools, and the persona. It sends
  plain text to the engine. It receives audio and per-word timestamps back.
- **Plugin** (`plugin/`, optional) — the OpenClaw realtime-voice provider,
  when a gateway is co-resident. It connects a NemoClaw or host OpenClaw agent
  to the brain's `/talk` WebSocket, and the gateway gives the brain its web
  search, memory and consult tools. A box without one (`TEAPORT_AGENT=none`,
  the installer's voice-only path) runs the brain with its local tools alone,
  fronted by the SIP or Discord bridge instead.

Your voice does not leave the device. The LLM runs where you point it.

## One conversation at a time

The engine transcribes one session at a time, so the brain's session arbiter
(`brain/teaport_brain/session_arbiter.py`) owns that slot and applies one policy
to every front-end — `/talk` (the OpenClaw plugin, Discord, the box's mic
bridge) and phone calls, all in the one brain process. A pipeline is still built
per session, so a session's failure ends that session only; what one process gives
up is isolation from a hard crash in native code, which now takes Talk and a live
call down together (the gateway keeps the caller and hands the call to the
restarted brain). A newcomer gets the engine only if nothing is live,
if it is the same client reconnecting, if the box's mic is asleep, or if it is
a call and the live session is a remote Talk session (whose user is told
first). Everyone else is told the agent is busy. The table is in
docs/CONFIG.md → One engine, one conversation.

## What the voice is told

OpenClaw composes what a realtime voice surface is told (its voice rules, the
operator's `talk.realtime.instructions`, the agent paragraph and the IDENTITY / USER
/ SOUL profile) and the consult tool, and teaport follows that contract
(`brain/teaport_brain/host_context.py`, issue #230):

- **Talk.** The plugin sends what OpenClaw handed it (`req.instructions`,
  `req.tools`) as the first frame on `/talk`, a `session_config` it announces with
  `?session_config=1`. The brain reads it before it builds the session: the system
  prompt starts with those instructions as sent, followed by the brain's voice rules,
  and the model's consult tool takes OpenClaw's schema. A client that announces no
  `session_config` (the Discord bridge, an older plugin) is served like the room mic,
  below. A request in progress is controlled two ways that never act on the same words:
  the relay's spoken run control acts on what its classifier recognizes in the
  transcripts the brain sends, says so as a host note the brain speaks, and the plugin
  claims those words for it once it has acted, so the model does not answer them too
  (`run_control.py`); the model calls `openclaw_agent_control` for everything else, which
  the Talk client runs and answers through the relay like a consult (`tools.py`, run
  control; plugin README, "Spoken run control"). The room mic and the phone have
  neither: their prompt says a request in progress cannot be stopped or changed.
- **Room mic, phone and other `/talk` clients.** At the start of each session the brain
  asks the gateway's teaport plugin (`GET /teaport/voice-context`, gateway auth with the
  brain's token; `plugin/voice-context.js`). The plugin builds the text the way
  OpenClaw's own phone surface does, with the SDK: the agent paragraph and the IDENTITY /
  USER / SOUL profile, at most 6,000 characters, plus the consult tool. The brain's voice
  rules follow it. A gateway still starting is asked again briefly (a few seconds for a
  room session opening asleep), and a room session that still got nothing asks once more
  at its wake word. The brain never reads the OpenClaw workspace itself.
- **No context.** With `TEAPORT_AGENT=none`, or a gateway that cannot answer
  (starting, restarting, an older plugin or OpenClaw), the session uses
  `TEAPORT_PERSONA_FILE` (else a built-in persona) and the brain's copy of OpenClaw's
  consult schema (`tools.OPENCLAW_AGENT_CONSULT`), with a warning in the journal.

The consult is OpenClaw's `openclaw_agent_consult` on every front end; the relay runs
it in-process on Talk, and the gateway's agent lane runs it otherwise.

After OpenClaw's text come the brain's voice rules (`persona.py`): its tools, a consult
paragraph, and the tuned delivery text. The consult paragraph depends on what the host
text covers. Under Talk's text, which carries OpenClaw's realtime rules, it overrides
the ones that clash with this surface (no control tool; a specific spoken preamble
instead of the stock "Let me check that for you"). Under the room's and the phone's,
which carry only the agent paragraph and profile, it says when to consult instead. Both
tell the model to answer from the conversation before consulting again and never to
describe the agent's other sessions. The wording was chosen by an eval on the box's
model (issue #230, arm B2). With the persona file the overlay is the tuned one,
unchanged.

## Tools

Every tool the voice model can call follows one contract
(`brain/teaport_brain/tools.py`, *THE TOOL CONTRACT*):

- **One record per tool.** Each tool has its schema, its handler, an on/off
  switch (`TEAPORT_TOOL_<NAME>`, see docs/CONFIG.md → Tools), its call timeout
  and the phrase the system prompt names it with.
- **What it needs.** A tool can need the OpenClaw gateway (web, memory,
  `openclaw_agent_consult`), the session's voice (`list_voices`, `switch_voice`), a client
  that can do it itself (`set_volume`, `restart_session`), or a part installed
  on the box (`wifi_setup` needs the `teaport-wifi-setup` unit and a client at
  the box).
- **One decision.** `active_tools()` picks a session's tools, and three things
  are built from that one list: the schema the model is offered, the handlers
  on the LLM and the tools the system prompt names. A tool that is off, or
  missing what it needs, appears in none of them.
- **Client tools.** The client performs these, not the brain. A `/talk` client
  announces what it can do (`?features=volume,restart`). The brain then sends it
  `{"type":"client_tool", ...}` and waits for its `{"type":"tool_result", ...}`.
  The local audio bridge is the only client that announces features; the
  OpenClaw plugin, SIP and Discord announce none and never see these tools.
- **Wi-Fi setup** needs no LLM, because a box without internet has none. The
  brain listens for the phrase "set up Wi-Fi" itself (`wifi_voice.py`), so the
  phrase works offline — and only offline (NetworkManager's connectivity is not
  "full"): online, the phrase goes to the model like any other words, and the model
  decides whether to use the `wifi_setup` tool, the way in for when the box is
  online. Both start `teaport-wifi-setup` (`wifi_setup.py`): a temporary setup
  network with a page that phones open automatically. The brain reads the
  setup's progress from a status file and speaks it. None of that is in the model's
  context, so when a setup the model started ends (or any setup leaves the box
  online), one English line saying how goes into the context through the context
  notes' path (`client_notes.py`), riding along with the next turn; it never names
  a password. The spoken and displayed text is translated with
  Python's standard `gettext` (`i18n.py`, one `.po` catalog per voice language),
  because the brain says it itself rather than through the LLM.
- **Text on the box's display** goes through `display.py`. A box with the OLED
  avatar shows a "screen" (a title and a few lines) over the face for whatever a
  person has to read off it, and optionally a QR code; Wi-Fi setup shows the
  setup network's name, password and address with a code a phone camera joins
  from (`WIFI:T:WPA;S:…;P:…;;`), then the join. A screen has an id and a time to live: a sender holds
  one up by re-sending it every few seconds, so a crashed sender's screen goes by
  itself. Screens go only to an avatar that says it draws them: while it runs it
  keeps a features file next to its socket (`face.sock.features`, JSON such as
  `{"screen": 1, "qr": 1, "qr_max_bytes": 53}`), and `display.py` reads it before
  every send. A code goes only to an avatar that says `"qr"` and draws one that long
  (in UTF-8 bytes) at a size a phone scans; only then does the voice say "scan the
  code on my screen". An older avatar would take the event, draw nothing and log
  it, setup password included. The avatar's built-in font is Latin only, so screens
  keep to ASCII: network names are folded into it, and a setup network name that
  folding would mangle shows as "(scan the code)" when the code is there.
- **The phone on the face** is `display.CallFace`, driven by the SIP front-end
  (`sip_server.CALL_FACE`): `ringing` with the caller id (the From header's display
  name unless it is a placeholder such as "WIRELESS CALLER" or "Anonymous", else the
  number or SIP user) from `call.incoming` until the call is answered (the brain
  answers it, after the session arbiter's grant and a couple of rings), `active`
  while the call is up, `none` at every end. Held states are re-sent with a ttl, and
  nothing is sent to an avatar whose features file lacks `"call"`. The brain's
  `call.incoming` journal line carries the From header (`from=`). The busy lamp
  breathes red while the face rings.

TODO: block diagram, frame/timing flow, port map, the memory and
openclaw_agent_consult paths.
