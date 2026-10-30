---
title: Architecture
description: How the engine, the brain, and the plugin share one device, one conversation, and one speech slot.
---

One device runs the whole voice assistant, and it holds one conversation at a time. Three parts share the work: the engine, the brain, and the plugin.

<!-- source: docs/ARCHITECTURE.md:6 @1e5604c; docs/ARCHITECTURE.md:29 @1e5604c -->

## The engine

The engine is the speech service `teaport-engine`, a closed binary that runs Voxtral for speech-to-text (STT) and Kokoro for text-to-speech (TTS). The installer downloads it, and any engine works in its place when it speaks the teaport-engine interface.

<!-- source: systemd/teaport-engine.service.in:1 @1e5604c; docs/ARCHITECTURE.md:8-10 @1e5604c -->

The engine transcribes one session at a time. That single STT slot is the scarce resource on the device, and [One conversation at a time](#one-conversation-at-a-time) describes the policy around it.

<!-- source: docs/ARCHITECTURE.md:29 @1e5604c; brain/teaport_brain/session_arbiter.py:5-7 @1e5604c -->

## The brain

The brain is the voice pipeline service `teaport-brain`. It runs a Pipecat pipeline: the engine's STT, your large language model (LLM), and then the engine's TTS. The LLM runs wherever you point the brain, and the brain reads its configuration from the environment.

<!-- source: brain/README.md:3-4 @1e5604c; brain/README.md:12 @1e5604c; docs/ARCHITECTURE.md:25 @1e5604c -->

The brain owns the conversation: barge-in, the heard-grounding ledger, memory recall, the tools, and the persona. It sends plain text to the engine and receives audio and per-word timestamps back.

<!-- source: docs/ARCHITECTURE.md:11-17 @1e5604c -->

The heard-grounding ledger records what you actually heard, the endpointing policy adds voice activity detection (VAD) gates, a Smart Turn threshold, and a barge-in guard, and on a phone call the playout pauses the moment you talk over the brain.

<!-- source: brain/README.md:26-31 @1e5604c -->

One process serves every front end: the `/talk` WebSocket and the phone line live in `teaport-brain`. Each session still gets a pipeline of its own, so one session's failure ends that session only.

<!-- source: systemd/teaport-brain.service.in:1-2 @1e5604c; docs/ARCHITECTURE.md:32-33 @1e5604c -->

## The plugin

The plugin is the OpenClaw realtime-voice provider, and the device does not need it. It connects OpenClaw's gateway-relay Talk path to the brain's `/talk` WebSocket. With a co-resident gateway (`TEAPORT_AGENT=openclaw`), the gateway gives the brain its web search, its memory, and the consult tools; with none (`TEAPORT_AGENT=none`, the installer's voice-only path), the brain runs on its own tools, fronted by the phone line or the Discord bridge.

<!-- source: docs/ARCHITECTURE.md:18-23 @1e5604c; brain/teaport_brain/tools.py:18-19 @1e5604c -->

You configure the plugin with the brain's URL, for example `ws://127.0.0.1:7861/talk`, an optional voice id such as `af_heart`, and a token that must match the brain's `GATEWAY_TOKEN`.

<!-- source: plugin/README.md:21-33 @1e5604c -->

## The brain's front ends

Every conversation reaches the brain through one of four front ends:

- **The `/talk` WebSocket.** The brain's main entrance, served with a `/health` endpoint. The OpenClaw plugin, the Discord bridge, the room microphone, the OpenClaw app, and the dashboard connect here as clients.

  <!-- source: brain/README.md:20 @1e5604c; brain/teaport_brain/session_arbiter.py:9-11 @1e5604c -->

- **The phone line.** The SIP front end (`sip_server.py`) runs in the brain's process and event loop and connects to the `teaport-sip` gateway over a UNIX domain socket whenever the gateway is up. Each call gets a pipeline built fresh, and tearing it down at hangup frees the engine's single STT slot.

  <!-- source: brain/teaport_brain/sip_server.py:3-25 @1e5604c -->

- **The Discord bridge.** A separate process on the host (`teaport-discord-bridge`). Discord voice media is RTP over UDP to per-call servers and the agent sandbox is TCP-only by design, so the bridge owns the media leg alone and pipes the audio to the brain's `/talk` WebSocket. It dials the brain itself and rejoins on your next voice move after a brain restart.

  <!-- source: systemd/teaport-discord-bridge.service.in:1-10 @1e5604c -->

- **The room microphone.** The optional `teaport-` service attaches a microphone and a speaker on the device to `/talk`. It announces the `volume` and `restart` client features, and it sleeps until a wake word.

  <!-- source: systemd/teaport-.service.in:1-10 @1e5604c; brain/teaport_brain/tools.py:30-35 @1e5604c; brain/teaport_brain/session_arbiter.py:10-13 @1e5604c -->

## One conversation at a time

The engine transcribes one session at a time, so the brain's session arbiter owns that slot and applies one policy to every front end. Each front end asks the arbiter for the slot before its pipeline connects to the engine.

<!-- source: docs/ARCHITECTURE.md:29-32 @1e5604c; brain/teaport_brain/session_arbiter.py:5-7 @1e5604c -->

The arbiter grants the slot to a newcomer in four cases: the slot is free; the holder's client is gone or has sent nothing for `STALE_SECS`, and the arbiter reaps that session; the newcomer reconnected under the same `?client=` id, so it replaces its own previous session; or the room microphone still sleeps, and a sleeping mic yields quietly.

<!-- source: brain/teaport_brain/session_arbiter.py:16-24 @1e5604c; brain/teaport_brain/session_arbiter.py:45-46 @1e5604c -->

A phone call against a live conversation is the one case where the arbiter asks first: the agent asks the conversation ("Someone's calling me, should I step away for a moment?"). A yes holds that conversation, keeps its context, and resumes it after the call; a no declines the call, and the brain never answers it, so the caller hears it ring until they give up. A call that no one can ask, because the `answer_phone_call` tool is off, ends differently: an awake room refuses it, the brain answers long enough to play the busy line, and the call hangs up. Every other newcomer hears a spoken line that says the agent is busy, unless it is a sleeping room that nobody addressed.

<!-- source: brain/teaport_brain/session_arbiter.py:25-34 @1e5604c; brain/teaport_brain/session_arbiter.py:40-43 @1e5604c; brain/teaport_brain/sip_server.py:27-37 @1e5604c -->

Nothing is evicted without a word: a live conversation keeps the slot, hears the question and decides, or hears why it lost the slot. The only silent ends are a client replacing its own session and a microphone that was asleep.

<!-- source: brain/teaport_brain/session_arbiter.py:36-38 @1e5604c -->

Because every front end shares one process, a hard crash in native code takes a Talk session and a live call down together. The gateway keeps the caller and hands the call to the restarted brain, which replays the call in progress; the replacement session opens with a short "sorry, I lost you for a moment, where were we?".

<!-- source: docs/ARCHITECTURE.md:33-35 @1e5604c; brain/teaport_brain/sip_server.py:45-53 @1e5604c -->

## What the voice is told

On Talk, the plugin sends what OpenClaw handed it, the instructions and the tools, as one `session_config` frame before any audio. The brain starts the system prompt with those instructions as sent, follows them with its own voice rules, and gives the model OpenClaw's consult tool schemas.

<!-- source: docs/ARCHITECTURE.md:49-53 @1e5604c; plugin/README.md:59-63 @1e5604c -->

The room microphone, the phone, and any client without a `session_config` take the longer path: the brain asks the gateway's teaport plugin for voice context at the start of the session, and the plugin answers with the agent paragraph and the IDENTITY, USER, and SOUL profile, at most 6,000 characters, plus the consult tool. The brain's voice rules follow the host text. The brain never reads the OpenClaw workspace itself.

<!-- source: docs/ARCHITECTURE.md:63-70 @1e5604c -->

With `TEAPORT_AGENT=none`, or a gateway that cannot answer, the session uses `TEAPORT_PERSONA_FILE`, or a built-in persona, plus the brain's own copy of the consult schema, and the journal records a warning. The brain's voice rules then add the tool names, a consult paragraph shaped to the surface, and the delivery text, and they tell the model to answer from the conversation before it consults again and never to describe the agent's other sessions.

<!-- source: docs/ARCHITECTURE.md:71-74 @1e5604c; docs/ARCHITECTURE.md:79-90 @1e5604c -->

Only Talk can stop or steer a request in flight, through the relay's spoken run control and the `openclaw_agent_control` tool. The room microphone and the phone say in their prompt that a request in progress cannot be stopped or changed.

<!-- source: docs/ARCHITECTURE.md:55-62 @1e5604c -->

## Tools in agent mode and in voice-only mode

Every tool is one record: a schema for the model, a handler, an on and off switch named `TEAPORT_TOOL_<NAME>`, a call timeout, and the phrase the system prompt names it with. One function, `active_tools`, decides which tools a session gets, so the offered schema, the registered handlers, and the prompt name the same set. Every tool defaults on except `restart_session`.

<!-- source: brain/teaport_brain/tools.py:14-28 @1e5604c -->

The full set is `get_host_status`, `get_current_time`, `web_search`, `web_fetch`, `search_memory`, `remember`, `openclaw_agent_consult`, `openclaw_agent_control`, `list_voices`, `switch_voice`, `set_volume`, `restart_session`, `end_conversation`, `answer_phone_call`, and `wifi_setup`.

<!-- source: brain/teaport_brain/tools.py:93-340 @1e5604c -->

In agent mode (`TEAPORT_AGENT=openclaw`), the brain calls the co-resident OpenClaw gateway over loopback, so the voice agent shares the agent's web search, web fetch, and long-term memory. The relay runs `openclaw_agent_consult` itself on Talk; the gateway's agent lane runs it on every other front end.

<!-- source: brain/teaport_brain/tools.py:4-8 @1e5604c; docs/ARCHITECTURE.md:76-77 @1e5604c -->

A tool appears in a session only when what it needs is there: the agent gateway behind `web_search`, `web_fetch`, the memory tools, and `openclaw_agent_consult`; the session's voice behind `list_voices` and `switch_voice`; a client that performs the tool itself behind `set_volume` and `restart_session`; the `teaport-wifi-setup` unit behind `wifi_setup`; and a conversation that a phone call can ask to make way behind `answer_phone_call`. A tool that is off, or that lacks what it needs, is not offered, not registered, and not named. Voice-only mode (`TEAPORT_AGENT=none`) is the same rule with the agent's tools absent.

<!-- source: docs/ARCHITECTURE.md:100-104 @1e5604c; brain/teaport_brain/tools.py:20-22 @1e5604c -->

Client tools run on the client, not in the brain: the client announces its features on `/talk` (`?features=volume,restart`), the brain sends a `client_tool` request, and the client returns the result. The room-microphone bridge is the only client that announces features; the plugin, the phone, and Discord announce none and never see these tools.

<!-- source: docs/ARCHITECTURE.md:109-113 @1e5604c; brain/teaport_brain/tools.py:30-35 @1e5604c -->

## Network ports

The table lists each port a teaport service listens on, and the service behind it:

| Port | Service | What it serves |
| --- | --- | --- |
| 8000 | `teaport-engine` | speech-to-text and text-to-speech for every front end |
| 7861 | `teaport-brain` | the `/talk` WebSocket and `/health`; the plugin, the Discord bridge, and the room microphone connect here as clients |
| 80 | `teaport-wifi-setup`, through a redirect | captive-portal probes on the setup network, sent to the setup page while the service runs |

<!-- source: docs/ARCHITECTURE.md:8 @1e5604c; brain/README.md:8 @1e5604c; brain/README.md:20 @1e5604c; systemd/teaport-brain.service.in:1-2 @1e5604c; plugin/README.md:31 @1e5604c; systemd/teaport-wifi-setup.service.in:36-38 @1e5604c -->

The `teaport-sip` gateway binds the SIP port that its trunk configuration names, and it binds nothing before `teaport sip configure` writes the credentials file. The gateway talks to the brain over a UNIX domain socket under `/run/teaport`, not TCP. The Discord bridge and the room microphone open no ports; both connect out to the brain's `/talk` WebSocket.

<!-- source: systemd/teaport-sip.service.in:3-7 @1e5604c; systemd/teaport-sip.service.in:30-34 @1e5604c; systemd/teaport-brain.service.in:14-17 @1e5604c; systemd/teaport-discord-bridge.service.in:1-4 @1e5604c -->

## What leaves the device

What crosses the network depends on the parts you use:

- The room's audio stays on the device: the microphone, the brain, and the engine exchange it directly, and the LLM runs wherever you point the brain, on the device or elsewhere. See [Configure teaport](/guides/configuration/).
- Web search and web fetch leave the device through the co-resident OpenClaw gateway.
- A phone call crosses the SIP trunk or registrar that the `teaport-sip` gateway bridges to the brain.
- Discord voice media travels as RTP over UDP to Discord's per-call servers, and the bridge pipes it to the brain.

<!-- source: docs/ARCHITECTURE.md:25 @1e5604c; brain/teaport_brain/tools.py:4-8 @1e5604c; systemd/teaport-sip.service.in:1-2 @1e5604c; systemd/teaport-discord-bridge.service.in:1-3 @1e5604c -->
