# Architecture

> **Scaffold.** This is an outline. The full document comes with the public
> docs pass.

One appliance, three parts:

- **Engine** (`:8000`) — speech-to-text and speech synthesis. The installer
  downloads it. Any engine works here if it is compatible with the
  teaport-engine interface.
- **Brain** (`brain/teaport_brain`, `:7861/talk`) — the Pipecat pipeline. It
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
  phrase works offline. The `wifi_setup` tool is a second way in for when the box
  is online. Both start `teaport-wifi-setup` (`wifi_setup.py`): a temporary setup
  network with a page that phones open automatically. The brain reads the
  setup's progress from a status file and speaks it.

TODO: block diagram, frame/timing flow, port map, the memory and ask_openclaw
consult paths.
