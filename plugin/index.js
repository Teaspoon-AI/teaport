// OpenClaw plugin entry: register the `teaport` realtime-voice provider.
//
// Install for local dev:  openclaw plugins install --link ./plugin
// Enable:                 openclaw plugins enable teaport-realtime
// Configure (~/.openclaw/openclaw.json):
//   talk.realtime.provider  = "teaport"
//   talk.realtime.mode      = "realtime"
//   talk.realtime.transport = "gateway-relay"
//   talk.realtime.brain     = "agent-consult"  // see below; "none" breaks talk.client.create
//   talk.realtime.providers.teaport.url = "ws://<pipecat-host>:7861/talk"
//
// `brain` is OpenClaw's "tool/agent strategy for realtime sessions" — how the session
// reaches TOOLS AND THE AGENT. It does not decide who speaks: the realtime *provider*
// (teaport) owns speech either way, so "agent-consult" cannot make OpenClaw double-respond.
// It must be "agent-consult" for two reasons:
//   * talk.client.create rejects anything else outright. It takes an explicit `brain`
//     param if given and otherwise falls back to this config value, so a Talk UI client
//     — which passes none — inherits whatever is set here. With "none" every session
//     dies at creation with `talk.client.create only supports brain="agent-consult"`.
//   * it is the machinery this plugin actually uses: the brain's ask_openclaw arrives
//     as an `openclaw_agent_consult` tool call, which provider.js hands to the relay's
//     in-process agent-consult path (working notices, then submitToolResult back to the
//     brain). With no agent strategy there is nothing for a consult to reach.
// This line read "none" until 2026-09-08, on the reasoning that Pipecat orchestrates and
// OpenClaw should stay quiet. That confuses `brain` with response ownership, and cost
// several rounds of flipping the value back and forth against a live gateway.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { buildTeaportRealtimeProvider } from "./provider.js";

export default definePluginEntry({
  id: "teaport-realtime",
  name: "Teaport Realtime Voice",
  description:
    "Routes OpenClaw realtime voice (gateway-relay) to an external Pipecat " +
    "speech-to-speech server with heard-grounded barge-in.",
  register(api) {
    api.registerRealtimeVoiceProvider(buildTeaportRealtimeProvider());
  },
});
