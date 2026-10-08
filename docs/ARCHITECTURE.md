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

## Tools

Every tool the voice model can call follows one contract
(`brain/teaport_brain/tools.py`, *THE TOOL CONTRACT*):

- **One record per tool.** Each tool has its schema, its handler, an on/off
  switch (`TEAPORT_TOOL_<NAME>`, see docs/CONFIG.md → Tools), its call timeout
  and the phrase the system prompt names it with.
- **What it needs.** A tool can need the OpenClaw gateway (web, memory,
  `ask_openclaw`), the session's voice (`list_voices`, `switch_voice`), a client
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

TODO: block diagram, frame/timing flow, port map, the memory and ask_openclaw
consult paths.
