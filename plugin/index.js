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
import { createHash } from "node:crypto";

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { TalkSessions, contextMethods } from "./context.js";
import { buildTeaportRealtimeProvider } from "./provider.js";

// The gateway connection a request came in on. createBridge runs inside the client's
// talk.session.create request, so this names the connection that owns the session
// (context.js). OpenClaw runs every gateway method, core ones included, inside the
// request scope the SDK's getPluginRuntimeGatewayRequestScope reads. OpenClaw 2026.9.1
// kept that scope in a global AsyncLocalStorage and 2026.9.6 moved it into the plugin
// execution frame; the accessor reads either.
//
// The accessor is exported only from openclaw/plugin-sdk/plugin-runtime, a broad barrel
// (2026.9.1 through 2026.9.7 have no narrower subpath for it). So it is imported only
// when the gateway loads the plugin for real (registrationMode "full"), not for
// discovery or CLI metadata, and asynchronously: createBridge is synchronous, and the
// first talk.session.create comes long after the gateway has loaded its plugins.
let getRequestScope = null;
function loadRequestScope() {
  import("openclaw/plugin-sdk/plugin-runtime")
    .then((sdk) => {
      getRequestScope = sdk.getPluginRuntimeGatewayRequestScope;
    })
    .catch(() => {
      /* no SDK accessor: bridges stay unbound and notes answer no_voice_session */
    });
}
function currentConnId() {
  return getRequestScope ? getRequestScope()?.client?.connId : undefined;
}

// The Talk client a session is for, as the brain's ?client= (provider.js): the paired
// device the client connected to the gateway as (connect.device.id), else its
// self-reported instance id. The device id outlives a reconnect, which is the point: the
// brain lets the same client replace its own session, so an app whose old connection
// froze takes it back, while a different device is told the agent is busy. Not always:
// the Control UI served over plain HTTP gets no device identity (that needs a secure
// context) and a fresh instance id per page load, so its reload is a new client. That
// case leans on the brain instead: a reload closes the old socket, and the brain's session
// arbiter frees a session whose socket is closed or has gone silent (session_arbiter.py,
// "reaped"). Hashed, so the brain's logs carry no device identifier; undefined when the
// host shows neither.
function currentClientKey() {
  const connect = getRequestScope ? getRequestScope()?.client?.connect : undefined;
  const id = connect?.device?.id || connect?.client?.instanceId;
  if (!id) return undefined;
  return "openclaw:" + createHash("sha256").update(String(id)).digest("hex").slice(0, 16);
}

// OpenClaw's own Talk session registry (src/gateway/talk-session-registry.ts, and
// src/gateway/talk/session-registry.ts from 2026.9.6): the record talk.session.create
// makes for each sessionId ({kind, connId, ...}). It is internal, not SDK, so this
// reads it defensively: undefined means this host does not expose one, and context
// notes then answer host_unsupported.
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
    // OpenClaw builds the plugin runtime lazily, and a read of api.runtime can build
    // all of it (and throw), which every plugin load (discovery, CLI commands) would
    // otherwise pay for.
    // Discovery and CLI loads only list what the plugin offers; nothing will run a
    // Talk session in them. (A host that does not say is treated as a full load.)
    if (api.registrationMode === undefined || api.registrationMode === "full") loadRequestScope();
    const sessions = new TalkSessions({ currentConnId, talkRegistry });
    api.registerRealtimeVoiceProvider(
      buildTeaportRealtimeProvider({
        hostVersion: () => api.runtime?.version,
        log: (msg) => api.logger?.info?.(msg),
        sessions,
        clientKey: currentClientKey,
      }),
    );
    // Context notes for live sessions (context.js). operator.talk is the scope OpenClaw
    // 2026.9 gives its own Talk methods: whoever may drive the session may add to what
    // its voice knows.
    if (typeof api.registerGatewayMethod === "function") {
      for (const [method, handler] of Object.entries(contextMethods(sessions))) {
        api.registerGatewayMethod(method, handler, { scope: "operator.talk" });
      }
    }
  },
});
