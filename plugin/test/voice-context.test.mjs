// The voice context route (issue #230, phase 3): GET /teaport/voice-context, which the
// brain calls at the start of a room-mic or phone session for what OpenClaw tells a
// voice surface. No gateway: a fake SDK and fake req/res stand in, and the last test
// loads index.js the way OpenClaw does, with a fake `openclaw` package on disk, and
// calls the route it registered. All profile text is synthetic.
//
//   node --test test/voice-context.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";

import { PROFILE_MAX_CHARS, VOICE_CONTEXT_PATH, voiceContextHandler } from "../voice-context.js";

const CONSULT = {
  type: "function",
  name: "openclaw_agent_consult",
  description: "Ask the agent.",
  parameters: { type: "object", properties: { question: { type: "string" } }, required: ["question"] },
};

function fakeSdk(over = {}) {
  const calls = [];
  return {
    calls,
    sdk: {
      resolveRealtimeVoiceAgentContextInstructions: async (params) => {
        calls.push(params);
        return "Agent context: synthetic.\n\n### IDENTITY.md\nName: Test Voice";
      },
      resolveDefaultAgentId: () => "main",
      tryResolveDefaultAgentId: () => "main",
      consultTool: CONSULT,
      ...over,
    },
  };
}

// node:http's ServerResponse, as much of it as the handler uses.
function fakeRes() {
  const res = {
    statusCode: 200,
    headers: {},
    body: undefined,
    setHeader(k, v) {
      this.headers[k.toLowerCase()] = v;
    },
    end(body) {
      this.body = body;
    },
    json() {
      return JSON.parse(this.body);
    },
  };
  return res;
}

async function get(handler, url = `${VOICE_CONTEXT_PATH}?front_end=room`, method = "GET") {
  const res = fakeRes();
  const handled = await handler({ method, url }, res);
  assert.equal(handled, true, "the route must say it handled the request");
  return res;
}

test("the route answers with OpenClaw's agent context and consult tool", async () => {
  const { sdk, calls } = fakeSdk();
  const config = { agents: { list: [{ id: "main" }] } };
  const logs = [];
  const handler = voiceContextHandler({
    config: () => config,
    version: () => "2026.9.7",
    loadSdk: async () => sdk,
    log: { info: (m) => logs.push(m), warn: (m) => logs.push(m) },
  });
  const res = await get(handler, `${VOICE_CONTEXT_PATH}?front_end=sip`);
  assert.equal(res.statusCode, 200);
  assert.match(res.headers["content-type"], /application\/json/);
  assert.deepEqual(res.json(), {
    version: 1,
    agentId: "main",
    instructions: "Agent context: synthetic.\n\n### IDENTITY.md\nName: Test Voice",
    tools: [CONSULT],
    openclawVersion: "2026.9.7",
  });
  // Built like voice-call's: the gateway's config, the default agent, a 6,000-char
  // profile, and no session key.
  assert.equal(calls.length, 1);
  assert.equal(calls[0].config, config);
  assert.equal(calls[0].agentId, "main");
  assert.equal(calls[0].maxChars, PROFILE_MAX_CHARS);
  assert.equal(PROFILE_MAX_CHARS, 6000);
  assert.equal("sessionKey" in calls[0], false);
  assert.ok(logs.some((m) => m.includes("for sip")), logs);
});

test("a host without the SDK pieces answers host_unsupported", async () => {
  for (const missing of ["resolveRealtimeVoiceAgentContextInstructions", "consultTool"]) {
    const { sdk } = fakeSdk({ [missing]: undefined });
    const handler = voiceContextHandler({ config: () => ({}), loadSdk: async () => sdk });
    const res = await get(handler);
    assert.equal(res.statusCode, 503, missing);
    assert.deepEqual(res.json(), { error: "host_unsupported" });
  }
  // An SDK import that fails outright is the same thing.
  const handler = voiceContextHandler({ config: () => ({}), loadSdk: async () => Promise.reject(new Error("nope")) });
  assert.deepEqual((await get(handler)).json(), { error: "host_unsupported" });
});

test("no default agent answers agent_unresolved", async () => {
  const { sdk } = fakeSdk({
    resolveDefaultAgentId: () => {
      throw new Error("agent selection required");
    },
    tryResolveDefaultAgentId: () => undefined,
  });
  const res = await get(voiceContextHandler({ config: () => ({}), loadSdk: async () => sdk }));
  assert.equal(res.statusCode, 503);
  assert.deepEqual(res.json(), { error: "agent_unresolved" });
});

test("a context the host fails to build answers context_unavailable", async () => {
  const { sdk } = fakeSdk({
    resolveRealtimeVoiceAgentContextInstructions: async () => {
      throw new Error("boom");
    },
  });
  const res = await get(voiceContextHandler({ config: () => ({}), loadSdk: async () => sdk }));
  assert.equal(res.statusCode, 503);
  assert.deepEqual(res.json(), { error: "context_unavailable" });
});

test("only GET and HEAD", async () => {
  const { sdk } = fakeSdk();
  const res = await get(voiceContextHandler({ config: () => ({}), loadSdk: async () => sdk }), VOICE_CONTEXT_PATH, "POST");
  assert.equal(res.statusCode, 405);
});

test("index.js registers the route behind gateway auth and serves it from the SDK", async (t) => {
  const { mkdtempSync, mkdirSync, writeFileSync, copyFileSync, readFileSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { pathToFileURL, fileURLToPath } = await import("node:url");
  const dir = mkdtempSync(join(tmpdir(), "teaport-plugin-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const sdkDir = join(dir, "node_modules", "openclaw", "plugin-sdk");
  mkdirSync(sdkDir, { recursive: true });
  const subpaths = {
    "plugin-entry": "export const definePluginEntry = (entry) => entry;\n",
    "plugin-runtime": "export const getPluginRuntimeGatewayRequestScope = () => undefined;\n",
    "realtime-bootstrap-context":
      "export const resolveRealtimeVoiceAgentContextInstructions = async ({ agentId, maxChars }) =>\n" +
      "  `Agent context for ${agentId} within ${maxChars}.`;\n",
    "agent-scope-runtime":
      "export const resolveDefaultAgentId = (cfg) => cfg.agents.list[0].id;\n" +
      "export const tryResolveDefaultAgentId = (cfg) => cfg.agents.list[0].id;\n",
    "realtime-voice": `export const REALTIME_VOICE_AGENT_CONSULT_TOOL = ${JSON.stringify(CONSULT)};\n`,
  };
  const exports = {};
  for (const [name, src] of Object.entries(subpaths)) {
    writeFileSync(join(sdkDir, `${name}.js`), src);
    exports[`./plugin-sdk/${name}`] = `./plugin-sdk/${name}.js`;
  }
  writeFileSync(
    join(dir, "node_modules", "openclaw", "package.json"),
    JSON.stringify({ name: "openclaw", type: "module", exports }),
  );
  writeFileSync(join(dir, "package.json"), JSON.stringify({ type: "module" }));
  const here = fileURLToPath(new URL("..", import.meta.url));
  const { files } = JSON.parse(readFileSync(join(here, "package.json"), "utf8"));
  for (const f of files) copyFileSync(join(here, f), join(dir, f));
  const entry = (await import(pathToFileURL(join(dir, "index.js")).href)).default;
  const routes = [];
  entry.register({
    registrationMode: "full",
    logger: { info: () => {}, warn: () => {} },
    runtime: { version: "2026.9.7", config: { current: () => ({ agents: { list: [{ id: "voice" }] } }) } },
    registerRealtimeVoiceProvider: () => {},
    registerGatewayMethod: () => {},
    registerHttpRoute: (route) => routes.push(route),
  });
  assert.equal(routes.length, 1);
  const [route] = routes;
  assert.equal(route.path, "/teaport/voice-context");
  assert.equal(route.auth, "gateway");
  assert.equal(route.match, "exact");
  const res = await get(route.handler);
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.json(), {
    version: 1,
    agentId: "voice",
    instructions: "Agent context for voice within 6000.",
    tools: [CONSULT],
    openclawVersion: "2026.9.7",
  });
});
