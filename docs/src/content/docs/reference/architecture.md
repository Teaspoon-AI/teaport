---
title: Architecture
description: How the engine, the brain, and the plugin share one device, one conversation, and one speech slot.
---

One device runs the whole voice assistant, and it holds one conversation at a time. Three parts share the work: the engine, the brain, and the plugin.

<!-- source: README.md:3-14#be1c153f; brain/teaport_brain/session_arbiter.py:5-6#e6796b58 -->

## The engine

The engine is the speech service `teaport-engine`, a closed binary that runs Voxtral for speech-to-text (STT) and Kokoro for text-to-speech (TTS). The installer downloads it, and any engine works in its place when it speaks the teaport-engine interface.

<!-- source: systemd/teaport-engine.service.in:1#0119ec23; README.md:12-14#6df7c2a1 -->

The engine transcribes one session at a time. That single STT slot is the scarce resource on the device, and [One conversation at a time](#one-conversation-at-a-time) describes the policy around it.

<!-- source: brain/teaport_brain/session_arbiter.py:5-7#2f9f0300 -->

## The brain

The brain is the voice pipeline service `teaport-brain`. It runs a Pipecat pipeline: the engine's STT, your large language model (LLM), and then the engine's TTS. The LLM runs wherever you point the brain, and the brain reads its configuration from the environment.

<!-- source: brain/README.md:3-4#4d22de21; brain/README.md:12#050b5bc3; README.md:18#cc733da3 -->

The brain owns the conversation: barge-in, the heard-grounding ledger, memory recall, the tools, and the persona. It sends plain text to the engine and receives audio and per-word timestamps back.

<!-- source: README.md:6-8#f5cf0fb3; brain/teaport_brain/persona.py:2-7#6b43c813; brain/teaport_brain/engine_tts.py:4-12#8b2db407; brain/README.md:27#147b489c -->

The heard-grounding ledger records what you actually heard, the endpointing policy adds voice activity detection (VAD) gates, a Smart Turn threshold, and a barge-in guard, and on a phone call the playout pauses the moment you talk over the brain.

<!-- source: brain/README.md:26-31#3abc9795 -->

One process serves every front end: the `/talk` WebSocket and the phone line live in `teaport-brain`. Each session still gets a pipeline of its own, so one session's failure ends that session only.

<!-- source: systemd/teaport-brain.service.in:1-2#4ed91011; brain/teaport_brain/sip_server.py:16-23#ee15f50c; brain/teaport_brain/agent_session.py:653-656#50aa8078 -->

## The plugin

The plugin is the OpenClaw realtime-voice provider, and the device does not need it. It connects OpenClaw's gateway-relay Talk path to the brain's `/talk` WebSocket. With a co-resident gateway (`TEAPORT_AGENT=openclaw`), the gateway gives the brain its web search, its memory, and the consult tools; with none (`TEAPORT_AGENT=none`, the installer's voice-only path), the brain runs on its own tools, fronted by the phone line or the Discord bridge.

<!-- source: brain/teaport_brain/agent_backend.py:4-7,13-20#b09df9f9; brain/teaport_brain/gateway_server.py:8-16#0169bcbe; brain/teaport_brain/tools.py:4-8,19-20#eee714d1 -->

You configure the plugin with the brain's URL, for example `ws://127.0.0.1:7861/talk`, an optional voice id such as `af_heart`, and a token that must match the brain's `GATEWAY_TOKEN`.

<!-- source: plugin/README.md:21-33#4af46124 -->

## The brain's front ends

Every conversation reaches the brain through one of four front ends:

- **The `/talk` WebSocket.** The brain's main entrance, served with a `/health` endpoint. The OpenClaw plugin, the Discord bridge, the room microphone, the OpenClaw app, and the dashboard connect here as clients.

  <!-- source: brain/README.md:20#9528fc28; brain/teaport_brain/session_arbiter.py:9-11#f1671043 -->

- **The phone line.** The SIP front end (`sip_server.py`) runs in the brain's process and event loop and connects to the `teaport-sip` gateway over a UNIX domain socket whenever the gateway is up. Each call gets a pipeline built fresh, and tearing it down at hangup frees the engine's single STT slot.

  <!-- source: brain/teaport_brain/sip_server.py:3-25#49ac27be -->

- **The Discord bridge.** A separate process on the host (`teaport-discord-bridge`). Discord voice media is RTP over UDP to per-call servers and the agent sandbox is TCP-only by design, so the bridge owns the media leg alone and pipes the audio to the brain's `/talk` WebSocket. It dials the brain itself and rejoins on your next voice move after a brain restart.

  <!-- source: systemd/teaport-discord-bridge.service.in:1-10#9f7532ad -->

- **The room microphone.** The optional `teaport-local-audio` service attaches a microphone and a speaker on the device to `/talk`. It announces the `volume` and `restart` client features, and it sleeps until a wake word.

  <!-- source: systemd/teaport-local-audio.service.in:1-10#4c9ee60d; brain/teaport_brain/tools.py:31-36#88656142; brain/teaport_brain/session_arbiter.py:10-13#80bbf27f -->

## One conversation at a time

The engine transcribes one session at a time, so the brain's session arbiter owns that slot and applies one policy to every front end. Each front end asks the arbiter for the slot before its pipeline connects to the engine.

<!-- source: brain/teaport_brain/session_arbiter.py:5-7#2f9f0300 -->

The arbiter grants the slot to a newcomer in four cases: the slot is free; the holder's client is gone or has sent nothing for `STALE_SECS`, and the arbiter reaps that session; the newcomer reconnected under the same `?client=` id, so it replaces its own previous session; or the room microphone still sleeps, and a sleeping mic yields quietly.

<!-- source: brain/teaport_brain/session_arbiter.py:16-24#9e64b0c2; brain/teaport_brain/session_arbiter.py:45-46#31d04ecb -->

A phone call against a live conversation is the one case where the arbiter asks first: the agent asks the conversation ("Someone's calling me, should I step away for a moment?"). A yes holds that conversation, keeps its context, and resumes it after the call; a no declines the call, and the brain never answers it, so the caller hears it ring until they give up. A call that no one can ask, because the `answer_phone_call` tool is off, ends differently: an awake room refuses it, the brain answers long enough to play the busy line, and the call hangs up. Every other newcomer hears a spoken line that says the agent is busy, unless it is a sleeping room that nobody addressed.

<!-- source: brain/teaport_brain/session_arbiter.py:25-34#00d6c5f4; brain/teaport_brain/session_arbiter.py:40-43#79fd77f8; brain/teaport_brain/sip_server.py:27-37#4ed7a2bd -->

Nothing is evicted without a word: a live conversation keeps the slot, hears the question and decides, or hears why it lost the slot. The only silent ends are a client replacing its own session and a microphone that was asleep.

<!-- source: brain/teaport_brain/session_arbiter.py:36-38#086baa05 -->

Because every front end shares one process, a hard crash in native code takes a Talk session and a live call down together. The gateway keeps the caller and hands the call to the restarted brain, which replays the call in progress; the replacement session opens with a short "sorry, I lost you for a moment, where were we?".

<!-- source: brain/teaport_brain/sip_server.py:5-8#61ca4650; brain/teaport_brain/sip_server.py:45-53#55c08c9e -->

## What the voice is told

On Talk, the plugin sends what OpenClaw handed it, the instructions and the tools, as one `session_config` frame before any audio. The brain starts the system prompt with those instructions as sent, follows them with its own voice rules, and gives the model OpenClaw's consult tool schemas.

<!-- source: brain/teaport_brain/host_context.py:13-16,19-24#e5dd3ee5; plugin/README.md:59-63#40485acd -->

The room microphone, the phone, and any client without a `session_config` take the longer path: the brain asks the gateway's teaport plugin for voice context at the start of the session, and the plugin answers with the agent paragraph and the IDENTITY, USER, and SOUL profile, at most 6,000 characters, plus the consult tool. The brain's voice rules follow the host text. The brain never reads the OpenClaw workspace itself.

<!-- source: brain/teaport_brain/host_context.py:25-34#b40aee14; plugin/voice-context.js:3-22#bb5f6d8d; brain/teaport_brain/persona.py:428-433#702d05bf -->

With `TEAPORT_AGENT=none`, or a gateway that cannot answer, the session uses `TEAPORT_PERSONA_FILE`, or a built-in persona, plus the brain's own copy of the consult schema, and the journal records a warning. The brain's voice rules then add the tool names, a consult paragraph shaped to the surface, and the delivery text, and they tell the model to answer from the conversation before it consults again and never to describe the agent's other sessions.

<!-- source: brain/teaport_brain/persona.py:13-16,24-31,218-223#dd416842; brain/teaport_brain/host_context.py:31-34#72c9dae8 -->

Only Talk can stop or steer a request in flight, through the relay's spoken run control and the `openclaw_agent_control` tool. The room microphone and the phone say in their prompt that a request in progress cannot be stopped or changed.

<!-- source: brain/teaport_brain/tools.py:38-42,206-209,1081-1093#eea93d14; brain/teaport_brain/run_control.py:4-11#e7dc27ec; brain/teaport_brain/persona.py:213-216#312dcc92 -->

## Tools in agent mode and in voice-only mode

Every tool is one record: a schema for the model, a handler, an on and off switch named `TEAPORT_TOOL_<NAME>`, a call timeout, and the phrase the system prompt names it with. One function, `active_tools`, decides which tools a session gets, so the offered schema, the registered handlers, and the prompt name the same set. Every tool defaults on except `restart_session`.

<!-- source: brain/teaport_brain/tools.py:14-29#8f5c3056 -->

The full set is `get_host_status`, `get_current_time`, `web_search`, `web_fetch`, `search_memory`, `remember`, `openclaw_agent_consult`, `openclaw_agent_control`, `list_voices`, `switch_voice`, `set_volume`, `restart_session`, `end_conversation`, `answer_phone_call`, and `wifi_setup`.

<!-- source: brain/teaport_brain/tools.py:95-342#056035b3 -->

In agent mode (`TEAPORT_AGENT=openclaw`), the brain calls the co-resident OpenClaw gateway over loopback, so the voice agent shares the agent's web search, web fetch, and long-term memory. The relay runs `openclaw_agent_consult` itself on Talk; the gateway's agent lane runs it on every other front end.

<!-- source: brain/teaport_brain/tools.py:4-8#dcdda3e4; brain/teaport_brain/tools.py:38-40,495-496#0927ccf8; plugin/voice-context.js:23-25#5e69ee5b -->

A tool appears in a session only when what it needs is there: the agent gateway behind `web_search`, `web_fetch`, the memory tools, and `openclaw_agent_consult`; the session's voice behind `list_voices` and `switch_voice`; a client that performs the tool itself behind `set_volume` and `restart_session`; the `teaport-wifi-setup` unit behind `wifi_setup`; and a conversation that a phone call can ask to make way behind `answer_phone_call`. A tool that is off, or that lacks what it needs, is not offered, not registered, and not named. Voice-only mode (`TEAPORT_AGENT=none`) is the same rule with the agent's tools absent.

<!-- source: brain/teaport_brain/tools.py:19-23,1358-1382,1395-1406,1407-1409,1412-1413,1419-1423#f268879a; brain/teaport_brain/wifi_voice.py:192-194#37ba731d; brain/teaport_brain/agent_backend.py:13-16#e7c24fbe -->

Client tools run on the client, not in the brain: the client announces its features on `/talk` (`?features=volume,restart`), the brain sends a `client_tool` request, and the client returns the result. The room-microphone bridge is the only client that announces features; the plugin, the phone, and Discord announce none and never see these tools.

<!-- source: brain/teaport_brain/tools.py:31-36#88656142; brain/teaport_brain/local_audio.py:232-235#1adbafcf; plugin/README.md:39-44#638406a1 -->

## Network ports

The table lists each port a teaport service listens on, and the service behind it:

| Port | Service | What it serves |
| --- | --- | --- |
| 8000 | `teaport-engine` | speech-to-text and text-to-speech for every front end |
| 7861 | `teaport-brain` | the `/talk` WebSocket and `/health`; the plugin, the Discord bridge, and the room microphone connect here as clients |
| 80 | `teaport-wifi-setup`, through a redirect | captive-portal probes on the setup network, sent to the setup page while the service runs |

<!-- source: brain/teaport_brain/gateway_server.py:27-28#c17952f7; install.sh:1528#b5bc5408; brain/README.md:8#8e48c7c2; brain/README.md:20#9528fc28; systemd/teaport-brain.service.in:1-2#4ed91011; plugin/README.md:31#0ebd73fa; systemd/teaport-wifi-setup.service.in:36-38#1d45013f -->

The `teaport-sip` gateway binds the SIP port that its trunk configuration names, and it binds nothing before `teaport sip configure` writes the credentials file. The gateway talks to the brain over a UNIX domain socket under `/run/teaport`, not TCP. The Discord bridge and the room microphone open no ports; both connect out to the brain's `/talk` WebSocket.

<!-- source: systemd/teaport-sip.service.in:3-7#0d07b370; systemd/teaport-sip.service.in:30-34#d40a540c; systemd/teaport-brain.service.in:14-17#be04c219; systemd/teaport-discord-bridge.service.in:1-4#f101e6be -->

## What leaves the device

What crosses the network depends on the parts you use:

- The room's audio stays on the device: the microphone, the brain, and the engine exchange it directly, and the LLM runs wherever you point the brain, on the device or elsewhere. See [Configure teaport](/guides/configuration/).
- Web search and web fetch leave the device through the co-resident OpenClaw gateway.
- A phone call crosses the SIP trunk or registrar that the `teaport-sip` gateway bridges to the brain.
- Discord voice media travels as RTP over UDP to Discord's per-call servers, and the bridge pipes it to the brain.

<!-- source: README.md:3-4,16,18#b1351cf0; brain/teaport_brain/tools.py:4-8#dcdda3e4; systemd/teaport-sip.service.in:1-2#ccee4d55; systemd/teaport-discord-bridge.service.in:1-3#0f842336 -->
