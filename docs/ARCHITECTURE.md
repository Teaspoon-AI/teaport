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
- **Plugin** (`plugin/`) — the OpenClaw realtime-voice provider. It connects a
  NemoClaw agent to the brain's `/talk` WebSocket.

Your voice does not leave the device. The LLM runs where you point it.

TODO: block diagram, frame/timing flow, port map, the memory and ask_openclaw
consult paths.
