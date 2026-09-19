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

TODO: block diagram, frame/timing flow, port map, the memory and ask_openclaw
consult paths.
