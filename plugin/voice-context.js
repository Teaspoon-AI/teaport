// The voice context for the brain's own front ends (issue #230, phase 3).
//
// A Talk session gets what OpenClaw composed for a realtime voice surface with the
// session itself (req.instructions / req.tools, sent to the brain as the session_config
// frame; provider.js). The room mic and the phone line never pass through OpenClaw's
// Talk relay, so the brain asks for theirs here, at the start of each session:
//
//   GET /teaport/voice-context?front_end=room|sip|<client>[&agent=<id>]
//                                                      (gateway auth: the bearer token)
//   -> 200 {"version": 1, "agentId", "instructions", "tools", "openclawVersion"}
//   -> 400 {"error": "agent_unknown"}                  (agent= names no configured agent)
//   -> 503 {"error": "host_unsupported" | "agent_unresolved" | "context_unavailable"}
//
// The text is built the way OpenClaw's own phone surface builds it (voice-call,
// realtime-agent-context.ts), with the published SDK:
//   instructions  resolveRealtimeVoiceAgentContextInstructions: the multi-session agent
//                 paragraph, then the IDENTITY / USER / SOUL profile through the agent's
//                 bootstrap pipeline, capped at 6,000 characters (voice-call's figure;
//                 Talk keeps OpenClaw's fixed 12,000).
//   tools         [REALTIME_VOICE_AGENT_CONSULT_TOOL]: the consult, as Talk gets it.
// No session key: room and phone consults run under a fresh key each (the brain's
// /v1/chat/completions lane sends none), as voice-call omits it too. The agent is the
// one that lane's model "openclaw" answers with, unless the brain names one (agent=, its
// OPENCLAW_AGENT_ID when the operator set it: the agent the box talks to, which its
// consults then reach as model "openclaw/<id>"); a named one must be a configured agent
// (listAgentIds), as the gateway's own agent model id and x-openclaw-agent-id header
// must. Both are resolved the way 2026.9.7 asks new plugins to (resolveAgentId).
//
// The SDK subpaths are imported on the first request, not at plugin load: discovery and
// CLI loads never serve a request, and the realtime-voice barrel is not small. They are
// feature-detected (import * as sdk, then look) the way OpenClaw's own voice-call and
// discord extensions do, so a host without one answers host_unsupported instead of
// failing the plugin. Like provider.js and context.js, this module imports nothing from
// the SDK at the top, so it is tested standalone (test/voice-context.test.mjs).

export const VOICE_CONTEXT_PATH = "/teaport/voice-context";
export const VOICE_CONTEXT_VERSION = 1;
// voice-call's default profile budget (extensions/voice-call/src/config.ts).
export const PROFILE_MAX_CHARS = 6000;

// The realtime-voice SDK subpath, imported once for the route and for the run-control
// mirror (realtimeVoiceRunControl); {} on a host without it.
let realtimeVoice = null;
function importRealtimeVoice() {
  realtimeVoice ??= import("openclaw/plugin-sdk/realtime-voice").catch(() => ({}));
  return realtimeVoice;
}

/**
 * The pieces of OpenClaw's spoken run control the bridge mirrors (provider.js,
 * answerRunControl), from the realtime-voice SDK (2026.9.7+), each undefined on a host
 * without it: isControl, shouldAutoControlRealtimeVoiceAgentText (whether the relay's run
 * control acts on an utterance while a consult runs), and isEcho,
 * isLikelyRealtimeVoiceAssistantEchoTranscript (whether its echo filter drops the
 * utterance first).
 */
export async function realtimeVoiceRunControl() {
  const voice = await importRealtimeVoice();
  const fn = (f) => (typeof f === "function" ? f : undefined);
  return {
    isControl: fn(voice.shouldAutoControlRealtimeVoiceAgentText),
    isEcho: fn(voice.isLikelyRealtimeVoiceAssistantEchoTranscript),
  };
}

/** The SDK pieces the route needs, from the host's published subpaths. */
export async function importVoiceContextSdk() {
  const [context, scope, voice] = await Promise.all([
    import("openclaw/plugin-sdk/realtime-bootstrap-context").catch(() => ({})),
    import("openclaw/plugin-sdk/agent-scope-runtime").catch(() => ({})),
    importRealtimeVoice(),
  ]);
  return {
    resolveRealtimeVoiceAgentContextInstructions: context.resolveRealtimeVoiceAgentContextInstructions,
    resolveSessionAgentIdStrict: scope.resolveSessionAgentIdStrict,
    listAgentIds: scope.listAgentIds,
    consultTool: voice.REALTIME_VOICE_AGENT_CONSULT_TOOL,
  };
}

function send(res, status, body) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(body));
  return true;
}

// The agent the context is for: { agentId } or { error }.
//
// OpenClaw 2026.9.7 asks new plugins to resolve a session's agent with
// resolveSessionAgentIdStrict (or the -Ids form) and an explicit or prepared owner
// (docs/plugins/sdk-subpaths.md, agent-scope-runtime; plugins/compatibility.md, "Session
// agent resolution aliases"). The old names, resolveSessionAgentId(s), keep an ambient
// system-agent fallback only through 2026-11-29; resolveDefaultAgentId, which this route
// used, is marked deprecated in its own module ("accepts raw shipped markers only for input
// compatibility"), though the gateway's /v1/chat/completions lane still calls it.
//
//   named (agent=)  the explicit owner. The strict resolver refuses an id that is not a
//                   valid agent id (agent_unknown here), and does not look the agent up,
//                   so listAgentIds must also know it, as the gateway's agent header is
//                   checked (assertKnownAgentId); it gives the normalized (lowercase) id.
//   not named       no owner: the strict resolver gives the retained legacy owner, else
//                   the agent the config marks default, else the only agent, and throws
//                   when the choice is open (several agents, none marked): agent_unresolved.
//                   The brain's room and phone consults go to /v1/chat/completions with
//                   model "openclaw" and no agent header, which answers with
//                   resolveDefaultAgentId: the marked default, else the only agent, else an
//                   error (src/gateway/http-utils.ts, resolveAgentIdForRequest). The two
//                   differ only for a retained owner whose marker a migration stripped:
//                   with one agent that is the same agent, and with several under explicit
//                   ownership the lane cannot answer at all. So whenever the lane answers,
//                   this is its agent.
function resolveAgentId(sdk, config, requested) {
  const named = requested.trim();
  let agentId;
  try {
    agentId = sdk.resolveSessionAgentIdStrict(named ? { config, agentId: named } : { config });
  } catch {
    return { error: named ? "agent_unknown" : "agent_unresolved" };
  }
  if (typeof agentId !== "string" || !agentId) return { error: named ? "agent_unknown" : "agent_unresolved" };
  if (!named) return { agentId };
  try {
    const ids = sdk.listAgentIds?.(config);
    return Array.isArray(ids) && ids.includes(agentId) ? { agentId } : { error: "agent_unknown" };
  } catch {
    return { error: "agent_unknown" };
  }
}

/**
 * The route's handler (OpenClawPluginHttpRouteHandler: (req, res) -> true when handled).
 * @param {{config: () => object, version?: () => string | undefined,
 *   loadSdk?: () => Promise<object>, log?: {info?: Function, warn?: Function}}} deps
 *   config reads the gateway's current config (api.runtime.config.current), version its
 *   OpenClaw version; loadSdk is importVoiceContextSdk unless a test gives its own.
 */
export function voiceContextHandler({ config, version, loadSdk = importVoiceContextSdk, log } = {}) {
  let sdkPromise = null;
  return async (req, res) => {
    if (req.method !== "GET" && req.method !== "HEAD") {
      res.setHeader("Allow", "GET, HEAD");
      return send(res, 405, { error: "method_not_allowed" });
    }
    let frontEnd = "";
    let requestedAgent = "";
    try {
      const params = new URL(req.url || "", "http://gateway").searchParams;
      frontEnd = params.get("front_end") || "";
      requestedAgent = params.get("agent") || "";
    } catch {
      /* logged as unknown; the default agent */
    }
    sdkPromise ??= loadSdk().catch(() => ({}));
    const sdk = await sdkPromise;
    if (
      typeof sdk.resolveRealtimeVoiceAgentContextInstructions !== "function" ||
      !sdk.consultTool ||
      typeof sdk.resolveSessionAgentIdStrict !== "function"
    ) {
      log?.warn?.("teaport-realtime: voice context requested, but this OpenClaw lacks the realtime SDK (2026.9.7+)");
      return send(res, 503, { error: "host_unsupported" });
    }
    let cfg;
    try {
      cfg = config();
    } catch {
      cfg = undefined;
    }
    if (!cfg) return send(res, 503, { error: "host_unsupported" });
    const { agentId, error } = resolveAgentId(sdk, cfg, requestedAgent);
    if (error === "agent_unknown") {
      log?.warn?.(`teaport-realtime: voice context asked for agent ${JSON.stringify(requestedAgent)}, which is not a configured agent`);
      return send(res, 400, { error });
    }
    if (error) {
      log?.warn?.("teaport-realtime: voice context requested, but no agent resolves from the config (several agents, none marked default?)");
      return send(res, 503, { error });
    }
    let instructions;
    try {
      instructions = await sdk.resolveRealtimeVoiceAgentContextInstructions({
        config: cfg,
        agentId,
        maxChars: PROFILE_MAX_CHARS,
        warn: (message) => log?.warn?.(`teaport-realtime: voice context: ${message}`),
      });
    } catch (err) {
      // It catches its own file errors; anything else is the host's, and the brain
      // falls back to its persona file on any 503.
      log?.warn?.(`teaport-realtime: voice context failed: ${err instanceof Error ? err.message : String(err)}`);
      return send(res, 503, { error: "context_unavailable" });
    }
    let openclawVersion;
    try {
      openclawVersion = version?.();
    } catch {
      openclawVersion = undefined;
    }
    log?.info?.(
      `teaport-realtime: voice context for ${frontEnd || "unknown front end"} ` +
        `(agent ${agentId}, ${String(instructions ?? "").length} chars)`,
    );
    return send(res, 200, {
      version: VOICE_CONTEXT_VERSION,
      agentId,
      instructions: typeof instructions === "string" ? instructions : "",
      tools: [sdk.consultTool],
      openclawVersion: openclawVersion || null,
    });
  };
}
