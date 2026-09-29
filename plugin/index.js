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
//   talk.realtime.providers.teaport.assistantTranscripts = "auto"  // optional; see below
//
// assistantTranscripts is how assistant captions reach the Talk view: "delta" (each
// partial is the text added since the last; what OpenClaw 2026.8.1+ expects), "full"
// (the whole text each time, for a view that replaces), or "auto", the default,
// which picks by the OpenClaw version (provider.js, pickAssistantTranscripts).
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

import { TalkSessions, contextMethods } from "./context.js";
import { buildTeaportRealtimeProvider } from "./provider.js";

// The gateway connection a request came in on. createBridge runs inside the client's
// talk.session.create request, so this names the connection that owns the session
// (context.js). OpenClaw keeps the request scope in a global AsyncLocalStorage
// (src/plugins/runtime/gateway-request-scope.ts, a Symbol.for singleton from 2026.7.1
// through 2026.9.1), read here directly: no SDK import at load, and nothing to race.
const REQUEST_SCOPE = Symbol.for("openclaw.pluginRuntimeGatewayRequestScope");
function currentConnId() {
  const scope = globalThis[REQUEST_SCOPE];
  return typeof scope?.getStore === "function" ? scope.getStore()?.client?.connId : undefined;
}

// OpenClaw's own Talk session registry (src/gateway/talk-session-registry.ts): the
// record talk.session.create makes for each sessionId ({kind, connId, ...}). It is
// internal, not SDK, and a global Map only from 2026.8.1 (module-local before), so
// this reads it defensively: undefined means this host does not expose one.
const TALK_SESSIONS = Symbol.for("openclaw.unifiedTalkSessions");
function talkRegistry() {
  const registry = globalThis[TALK_SESSIONS];
  return registry instanceof Map ? registry : undefined;
}

export default definePluginEntry({
  id: "teaport-realtime",
  name: "Teaport Realtime Voice",
  description:
    "Routes OpenClaw realtime voice (gateway-relay) to an external Pipecat " +
    "speech-to-speech server with heard-grounded barge-in.",
  register(api) {
    // The OpenClaw version is read when a Talk session first needs it, not here:
    // OpenClaw builds the plugin runtime lazily, and on 2026.7.x any read of
    // api.runtime builds all of it (and can throw), which every plugin load
    // (discovery, CLI commands) would otherwise pay for.
    const sessions = new TalkSessions({ currentConnId, talkRegistry });
    api.registerRealtimeVoiceProvider(
      buildTeaportRealtimeProvider({
        hostVersion: () => api.runtime?.version,
        log: (msg) => api.logger?.info?.(msg),
        sessions,
      }),
    );
    // Context notes for live sessions (context.js). The scope is talk.session.*'s:
    // whoever may drive the session may add to what its voice knows.
    if (typeof api.registerGatewayMethod === "function") {
      for (const [method, handler] of Object.entries(contextMethods(sessions))) {
        api.registerGatewayMethod(method, handler, { scope: "operator.talk" });
      }
    }
  },
});
