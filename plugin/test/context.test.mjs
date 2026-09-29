// Context notes (issue #71): the teaport.talk.context / teaport.talk.capabilities
// gateway methods, the session registry that finds the bridge for a sessionId, and the
// bridge's side of the /talk exchange (hello, context, context_result). No OpenClaw
// and no brain: a fake WebSocket plays the brain, and a Map plays OpenClaw's Talk
// session registry where a test needs one.
//
//   node --test test/context.test.mjs

import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";

import { CAPABILITIES_METHOD, CONTEXT_METHOD, TalkSessions, contextMethods } from "../context.js";
import { buildTeaportRealtimeProvider } from "../provider.js";

// ── a fake brain on the other end of the bridge's WebSocket ─────────────────────
class FakeWebSocket {
  static last = null;
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.listeners = {};
    FakeWebSocket.last = this;
    queueMicrotask(() => this.emit("open", {}));
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  emit(type, ev) {
    for (const fn of this.listeners[type] || []) fn(ev);
  }
  send(data) {
    this.sent.push(typeof data === "string" ? JSON.parse(data) : data);
  }
  close() {
    queueMicrotask(() => this.emit("close", {}));
  }
  // brain -> plugin
  reply(msg) {
    this.emit("message", { data: JSON.stringify(msg) });
  }
}

let realWebSocket;
beforeEach(() => {
  realWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = FakeWebSocket;
});
afterEach(() => {
  globalThis.WebSocket = realWebSocket;
});

const HELLO = { type: "hello", features: { context: { max_chars: 40, max_notes: 20, respond_interval_s: 15 } } };

/**
 * A provider wired to a registry as index.js wires it, with `who.conn` as the caller.
 * `registry` (a Map) plays OpenClaw's Talk session registry (2026.8.1+); without it
 * the host is one that does not expose one (2026.7.x).
 */
function setup({ registry, options } = {}) {
  const who = { conn: "conn-A" };
  const sessions = new TalkSessions({
    currentConnId: () => who.conn,
    talkRegistry: registry ? () => registry : undefined,
  });
  const provider = buildTeaportRealtimeProvider({ url: "ws://brain/talk", sessions });
  const methods = contextMethods(sessions, options);
  return { who, sessions, provider, methods, registry };
}

async function openBridge(provider, { hello = true } = {}) {
  const bridge = provider.createBridge({ providerConfig: {} });
  await bridge.connect();
  const ws = FakeWebSocket.last;
  if (hello) ws.reply(HELLO);
  return { bridge, ws };
}

/**
 * talk.session.create as OpenClaw 2026.8.1+ runs it: createBridge inside the caller's
 * request, then the registry record, in one synchronous run; the socket opens after.
 */
async function createSession(env, sessionId, { conn = "conn-A", hello = true } = {}) {
  const prev = env.who.conn;
  env.who.conn = conn;
  const bridge = env.provider.createBridge({ providerConfig: {} });
  env.who.conn = prev;
  env.registry.set(sessionId, { kind: "realtime-relay", connId: conn, relaySessionId: sessionId });
  await bridge.connect();
  const ws = FakeWebSocket.last;
  if (hello) ws.reply(HELLO);
  return { bridge, ws };
}

/** Call a gateway method handler; resolves with what it passed to respond(). */
function call(methods, name, params, connId = "conn-A") {
  return new Promise((resolve, reject) => {
    const respond = (ok, payload, error) => resolve({ ok, payload, error });
    const client = connId === null ? {} : { connId };
    Promise.resolve(methods[name]({ params, client, respond })).catch(reject);
  });
}

const unanswered = (ws) => ws.sent.find((m) => m.type === "context" && !m.answered);

/** Answer the next context note the fake brain receives. */
async function brainAnswers(ws, answer) {
  for (let i = 0; i < 50 && !unanswered(ws); i += 1) {
    await new Promise((r) => setTimeout(r, 1));
  }
  const note = unanswered(ws);
  assert.ok(note, "no context note reached the brain");
  note.answered = true;
  ws.reply({ type: "context_result", id: note.id, ...answer });
  return note;
}

/** Send a note; whichever fake brain gets it accepts it. Returns the result and its index. */
async function noteLandsOn(methods, sessionId, text, sockets, connId = "conn-A") {
  const pending = call(methods, CONTEXT_METHOD, { sessionId, text }, connId);
  for (let i = 0; i < 50 && !sockets.some(unanswered); i += 1) {
    await new Promise((r) => setTimeout(r, 1));
  }
  const ws = sockets.find(unanswered);
  if (ws) await brainAnswers(ws, { ok: true, status: "applied" });
  return { result: await pending, on: ws ? sockets.indexOf(ws) : -1 };
}

const reason = (r) => r.error?.details?.reason;

// ── the round trip ──────────────────────────────────────────────────────────────

test("a note reaches the brain and its answer is the RPC result", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider);
  const pending = call(methods, CONTEXT_METHOD, {
    sessionId: "relay-1",
    text: "  The user tapped the character.  ",
    respond: true,
    kind: "ui-event",
  });
  const note = await brainAnswers(ws, { ok: true, status: "queued" });
  assert.deepEqual(
    { type: note.type, text: note.text, respond: note.respond, kind: note.kind },
    { type: "context", text: "The user tapped the character.", respond: true, kind: "ui-event" },
  );
  assert.deepEqual(await pending, { ok: true, payload: { ok: true, status: "queued" }, error: undefined });
});

test("respond defaults to false and kind is optional", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider);
  const pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "camera: kitchen" });
  const note = await brainAnswers(ws, { ok: true, status: "applied" });
  assert.equal(note.respond, false);
  assert.equal("kind" in note, false);
  assert.equal((await pending).payload.status, "applied");
});

test("the brain's refusals become gateway errors a client can act on", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider);
  let pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "tap", respond: true });
  await brainAnswers(ws, { ok: false, error: "rate_limited", message: "one per 15 s", retry_after_ms: 9000 });
  let r = await pending;
  assert.equal(r.ok, false);
  assert.equal(r.error.code, "UNAVAILABLE");
  assert.equal(r.error.retryable, true);
  assert.equal(r.error.retryAfterMs, 9000);
  assert.equal(reason(r), "rate_limited");

  pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "tap" });
  await brainAnswers(ws, { ok: false, error: "empty", message: "the note has no text" });
  r = await pending;
  assert.equal(r.error.code, "INVALID_REQUEST");
  assert.equal(reason(r), "empty");

  pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "tap" });
  await brainAnswers(ws, { ok: false });
  r = await pending;
  assert.equal(r.error.code, "INVALID_REQUEST");
  assert.equal(reason(r), "refused");
});

test("the brain's announced length limit is enforced before the round trip, in code points", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider); // hello says max_chars 40
  const r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "x".repeat(41) });
  assert.equal(reason(r), "too_long");
  assert.equal(ws.sent.filter((m) => m.type === "context").length, 0);
  // 40 emoji are 80 UTF-16 units but 40 characters to the brain's len(): within the limit.
  const pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "\u{1F600}".repeat(40) });
  await brainAnswers(ws, { ok: true, status: "applied" });
  assert.equal((await pending).ok, true);
});

test("bad params are refused without touching a session", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider);
  for (const params of [
    {},
    { sessionId: "relay-1" },
    { sessionId: "relay-1", text: "   " },
    { sessionId: "relay-1", text: "ok", respond: "yes" },
    { sessionId: "relay-1", text: "ok", kind: 7 },
    { sessionId: "", text: "ok" },
  ]) {
    const r = await call(methods, CONTEXT_METHOD, params);
    assert.equal(r.ok, false, JSON.stringify(params));
    assert.equal(r.error.code, "INVALID_REQUEST");
  }
  assert.equal(ws.sent.length, 0);
});

// ── before the brain is ready (#71: "connecting" is not "unsupported") ─────────────

test("before the brain's hello a note is refused as connecting and never sent", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider, { hello: false });
  const r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "tap" });
  assert.equal(r.error.code, "UNAVAILABLE");
  assert.equal(r.error.retryable, true);
  assert.equal(reason(r), "connecting");
  assert.equal(ws.sent.filter((m) => m.type === "context").length, 0, "a note went out before the hello");
  // The hello arrives: the same note now goes through.
  ws.reply(HELLO);
  const pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "tap" });
  await brainAnswers(ws, { ok: true, status: "applied" });
  assert.equal((await pending).ok, true);
});

test("before the socket opens a note is refused as connecting", async () => {
  const { provider, methods } = setup();
  provider.createBridge({ providerConfig: {} }); // connect() not called yet
  const r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "tap" });
  assert.equal(reason(r), "connecting");
});

test("a brain that sends no hello in time is unsupported, not connecting", async () => {
  let t = 1_000_000;
  const now = () => t;
  const { provider, methods } = setup({ options: { helloTimeoutMs: 15000, now } });
  const realNow = Date.now;
  Date.now = now; // the bridge stamps its open with Date.now()
  let ws;
  try {
    ({ ws } = await openBridge(provider, { hello: false }));
  } finally {
    Date.now = realNow;
  }
  t += 14_000;
  let r = await call(methods, CAPABILITIES_METHOD, { sessionId: "relay-1" });
  assert.equal(r.payload.session.state, "connecting");
  t += 2_000;
  r = await call(methods, CAPABILITIES_METHOD, { sessionId: "relay-1" });
  assert.deepEqual(r.payload.session, { sessionId: "relay-1", state: "unsupported", context: null });
  r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "tap" });
  assert.equal(r.error.code, "INVALID_REQUEST");
  assert.equal(reason(r), "unsupported");
  assert.equal(ws.sent.filter((m) => m.type === "context").length, 0);
});

test("a hello without context notes is unsupported", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider, { hello: false });
  ws.reply({ type: "hello", features: {} });
  const r = await call(methods, CAPABILITIES_METHOD, { sessionId: "relay-1" });
  assert.equal(r.payload.session.state, "unsupported");
  assert.equal(r.payload.session.context, null);
});

// ── which session ───────────────────────────────────────────────────────────────

test("a closed session fails, and keeps failing as closed", async () => {
  const { provider, methods } = setup();
  const { bridge, ws } = await openBridge(provider);
  const first = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" });
  await brainAnswers(ws, { ok: true, status: "applied" });
  await first;
  bridge.close();
  const r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi again" });
  assert.equal(r.ok, false);
  assert.equal(reason(r), "closed_session");
});

test("the brain going away fails the note waiting on it at once, as closed", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider);
  const started = Date.now();
  const pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" });
  await new Promise((r) => setTimeout(r, 5));
  ws.emit("close", {}); // evicted by a newer /talk connection, say
  const r = await pending;
  assert.equal(r.ok, false);
  assert.equal(reason(r), "closed_session");
  assert.ok(Date.now() - started < 1000, "the note waited out its ack timeout");
});

test("without the host registry: no live session at all is no_voice_session", async () => {
  const { methods } = setup();
  const r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-9", text: "hi" });
  assert.equal(reason(r), "no_voice_session");
});

test("another connection's session is not reachable, before or after it is bound", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider); // created on conn-A
  let r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" }, "conn-B");
  assert.equal(r.ok, false);
  assert.equal(reason(r), "no_voice_session");
  const pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" });
  await brainAnswers(ws, { ok: true, status: "applied" });
  assert.equal((await pending).ok, true);
  // Bound to conn-A now: conn-B is refused by name.
  r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" }, "conn-B");
  assert.equal(reason(r), "not_owner");
  r = await call(methods, CAPABILITIES_METHOD, { sessionId: "relay-1" }, "conn-B");
  assert.equal(reason(r), "not_owner");
});

test("a caller with no connection id owns nothing", async () => {
  const { provider, methods } = setup();
  await openBridge(provider);
  const r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" }, null);
  assert.equal(reason(r), "not_owner");
  assert.equal(reason(await call(methods, CAPABILITIES_METHOD, { sessionId: "relay-1" }, null)), "not_owner");
});

test("a bridge made outside a gateway request is never bound", async () => {
  const { who, provider, methods } = setup();
  who.conn = undefined;
  await openBridge(provider);
  who.conn = "conn-A";
  const r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" });
  assert.equal(reason(r), "no_voice_session");
});

test("without the host registry the first id binds the caller's newest bridge", async () => {
  const { provider, methods } = setup();
  const one = await openBridge(provider);
  const two = await openBridge(provider);
  const { result, on } = await noteLandsOn(methods, "relay-2", "for the newer", [one.ws, two.ws]);
  assert.equal(result.ok, true);
  assert.equal(on, 1, "bound the older bridge");
});

test("a bound session stays with its bridge; a newer session gets the newer bridge", async () => {
  const { provider, methods } = setup();
  const one = await openBridge(provider);
  let pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "one" });
  await brainAnswers(one.ws, { ok: true, status: "applied" });
  await pending;
  const two = await openBridge(provider);
  pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-2", text: "two" });
  await brainAnswers(two.ws, { ok: true, status: "applied" });
  await pending;
  pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "one again" });
  const note = await brainAnswers(one.ws, { ok: true, status: "applied" });
  assert.equal(note.text, "one again");
  await pending;
});

// ── with the host's Talk session registry (OpenClaw 2026.8.1+) ────────────────────

test("with the registry each session is bound to its own bridge at creation", async () => {
  // The review's repro: S1 on bridge a and S2 on bridge b, both live on one
  // connection. Bound on first use, S1's first note took the NEWEST bridge (b).
  const env = setup({ registry: new Map() });
  const a = await createSession(env, "S1");
  const b = await createSession(env, "S2");
  let got = await noteLandsOn(env.methods, "S1", "for S1", [a.ws, b.ws]);
  assert.equal(got.on, 0, "S1's note reached S2's brain");
  got = await noteLandsOn(env.methods, "S2", "for S2", [a.ws, b.ws]);
  assert.equal(got.on, 1, "S2's note reached S1's brain");
  // And when a closes, S2 keeps working.
  a.bridge.close();
  got = await noteLandsOn(env.methods, "S2", "S2 again", [a.ws, b.ws]);
  assert.equal(got.result.ok, true);
  assert.equal(got.on, 1);
  assert.equal(reason(await call(env.methods, CONTEXT_METHOD, { sessionId: "S1", text: "late" })), "closed_session");
});

test("with the registry, sessions on two connections each get their own bridge", async () => {
  const env = setup({ registry: new Map() });
  const a = await createSession(env, "S-A", { conn: "conn-A" });
  const b = await createSession(env, "S-B", { conn: "conn-B" });
  let got = await noteLandsOn(env.methods, "S-B", "for B", [a.ws, b.ws], "conn-B");
  assert.equal(got.on, 1);
  got = await noteLandsOn(env.methods, "S-A", "for A", [a.ws, b.ws], "conn-A");
  assert.equal(got.on, 0);
  assert.equal(reason(await call(env.methods, CONTEXT_METHOD, { sessionId: "S-A", text: "x" }, "conn-B")), "not_owner");
});

test("the host's Talk registry vets the id", async () => {
  const env = setup({ registry: new Map() });
  const { ws } = await createSession(env, "relay-1");
  env.registry.set("relay-B", { kind: "realtime-relay", connId: "conn-B" });
  env.registry.set("room-1", { kind: "managed-room", connId: "conn-A" });
  env.registry.set("other-provider", { kind: "realtime-relay", connId: "conn-A" }); // no teaport bridge
  const say = async (sessionId) => reason(await call(env.methods, CONTEXT_METHOD, { sessionId, text: "hi" }));
  assert.equal(await say("made-up"), "unknown_session");
  assert.equal(await say("relay-B"), "not_owner");
  assert.equal(await say("room-1"), "not_relay");
  assert.equal(await say("other-provider"), "no_voice_session");
  // None of those disturbed the binding: the real id still reaches its bridge.
  const pending = call(env.methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" });
  await brainAnswers(ws, { ok: true, status: "applied" });
  assert.equal((await pending).ok, true);
});

test("a request in the instant before the bridge binds waits for it (bridge_not_ready)", async () => {
  const env = setup({ registry: new Map() });
  const bridge = env.provider.createBridge({ providerConfig: {} });
  env.registry.set("relay-1", { kind: "realtime-relay", connId: "conn-A" });
  // Synchronously, before the binding microtask: the id is known and not yet bound.
  const now = env.sessions.resolve("relay-1", "conn-A");
  assert.equal(reason(now), "bridge_not_ready");
  assert.equal(now.error.retryable, true);
  // The handlers wait it out.
  const r = await call(env.methods, CAPABILITIES_METHOD, { sessionId: "relay-1" });
  assert.equal(r.ok, true);
  assert.equal(r.payload.session.state, "connecting");
  await bridge.connect();
  FakeWebSocket.last.reply(HELLO);
  assert.equal((await call(env.methods, CAPABILITIES_METHOD, { sessionId: "relay-1" })).payload.session.state, "ready");
});

// ── discovery ───────────────────────────────────────────────────────────────────

test("capabilities name the method, and the session's state and limits when asked", async () => {
  const { provider, methods } = setup();
  let r = await call(methods, CAPABILITIES_METHOD, {});
  assert.equal(r.ok, true);
  assert.equal(r.payload.context.method, "teaport.talk.context");
  assert.equal(r.payload.session, undefined);

  await openBridge(provider);
  r = await call(methods, CAPABILITIES_METHOD, { sessionId: "relay-1" });
  assert.deepEqual(r.payload.session, {
    sessionId: "relay-1",
    state: "ready",
    context: { maxChars: 40, maxNotes: 20, respondIntervalMs: 15000 },
  });
});

test("capabilities before the brain's hello say connecting, not unsupported", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider, { hello: false });
  let r = await call(methods, CAPABILITIES_METHOD, { sessionId: "relay-1" });
  assert.deepEqual(r.payload.session, { sessionId: "relay-1", state: "connecting", context: null });
  ws.reply(HELLO);
  r = await call(methods, CAPABILITIES_METHOD, { sessionId: "relay-1" });
  assert.equal(r.payload.session.state, "ready");
});

test("capabilities report the effective length limit, never above the plugin's own", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider, { hello: false });
  ws.reply({ type: "hello", features: { context: { max_chars: 100000, max_notes: 20, respond_interval_s: 15 } } });
  const r = await call(methods, CAPABILITIES_METHOD, { sessionId: "relay-1" });
  assert.equal(r.payload.session.context.maxChars, 16000);
});

test("capabilities never bind: a stale id asked about first does not take the bridge", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider);
  assert.equal((await call(methods, CAPABILITIES_METHOD, { sessionId: "stale-id" })).ok, true);
  const pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" });
  await brainAnswers(ws, { ok: true, status: "applied" });
  assert.equal((await pending).ok, true, "the capabilities call bound the bridge to the stale id");
});

test("with the registry, a bridge takes the record made with it, not an older unclaimed one", async () => {
  const env = setup({ registry: new Map() });
  // Another provider's relay session on the same connection, made earlier: nothing of
  // ours serves it, and the next teaport bridge must not take it.
  env.registry.set("other-provider", { kind: "realtime-relay", connId: "conn-A" });
  const a = await createSession(env, "S1");
  const got = await noteLandsOn(env.methods, "S1", "for S1", [a.ws]);
  assert.equal(got.result.ok, true, JSON.stringify(got.result));
  assert.equal(reason(await call(env.methods, CONTEXT_METHOD, { sessionId: "other-provider", text: "x" })), "no_voice_session");
});

test("with the registry, two sessions made in one turn of the event loop bind in order", async () => {
  // OpenClaw runs createBridge and records the session in one synchronous run, and each
  // bridge binds in a microtask. A second talk.session.create that runs before the
  // first bridge's microtask leaves two new records for it to choose from.
  const env = setup({ registry: new Map() });
  const bridges = {};
  queueMicrotask(() => {
    bridges.b = env.provider.createBridge({ providerConfig: {} });
    env.registry.set("S2", { kind: "realtime-relay", connId: "conn-A" });
  });
  bridges.a = env.provider.createBridge({ providerConfig: {} });
  env.registry.set("S1", { kind: "realtime-relay", connId: "conn-A" });
  await new Promise((r) => setTimeout(r, 0));
  await bridges.a.connect();
  const wsA = FakeWebSocket.last;
  wsA.reply(HELLO);
  await bridges.b.connect();
  const wsB = FakeWebSocket.last;
  wsB.reply(HELLO);
  assert.equal((await noteLandsOn(env.methods, "S1", "one", [wsA, wsB])).on, 0);
  assert.equal((await noteLandsOn(env.methods, "S2", "two", [wsA, wsB])).on, 1);
});

test("index.js reads the caller's connection and the Talk registry from OpenClaw's globals", async (t) => {
  // The plugin entry as OpenClaw loads it, against the two globals OpenClaw keeps: the
  // gateway request scope (an AsyncLocalStorage) and, from 2026.8.1, the Talk registry.
  const { AsyncLocalStorage } = await import("node:async_hooks");
  const { mkdtempSync, mkdirSync, writeFileSync, copyFileSync, readFileSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { pathToFileURL, fileURLToPath } = await import("node:url");
  const dir = mkdtempSync(join(tmpdir(), "teaport-plugin-"));
  const SCOPE = Symbol.for("openclaw.pluginRuntimeGatewayRequestScope");
  const REGISTRY = Symbol.for("openclaw.unifiedTalkSessions");
  const scope = new AsyncLocalStorage();
  const registry = new Map();
  globalThis[SCOPE] = scope;
  globalThis[REGISTRY] = registry;
  t.after(() => {
    delete globalThis[SCOPE];
    delete globalThis[REGISTRY];
    rmSync(dir, { recursive: true, force: true });
  });
  const sdk = join(dir, "node_modules", "openclaw", "plugin-sdk");
  mkdirSync(sdk, { recursive: true });
  writeFileSync(join(dir, "node_modules", "openclaw", "package.json"),
    JSON.stringify({ name: "openclaw", type: "module", exports: { "./plugin-sdk/plugin-entry": "./plugin-sdk/plugin-entry.js" } }));
  writeFileSync(join(sdk, "plugin-entry.js"), "export const definePluginEntry = (entry) => entry;\n");
  writeFileSync(join(dir, "package.json"), JSON.stringify({ type: "module" }));
  const here = fileURLToPath(new URL("..", import.meta.url));
  const { files } = JSON.parse(readFileSync(join(here, "package.json"), "utf8"));
  for (const f of files) copyFileSync(join(here, f), join(dir, f));
  const entry = (await import(pathToFileURL(join(dir, "index.js")).href)).default;
  const providers = [];
  const methods = {};
  entry.register({
    logger: { info: () => {} },
    registerRealtimeVoiceProvider: (p) => providers.push(p),
    registerGatewayMethod: (name, handler, opts) => {
      methods[name] = handler;
      assert.deepEqual(opts, { scope: "operator.talk" });
    },
  });
  assert.deepEqual(Object.keys(methods).sort(), [CAPABILITIES_METHOD, CONTEXT_METHOD]);
  // talk.session.create on conn-A: createBridge inside the request scope, then the record.
  scope.run({ client: { connId: "conn-A" } }, () => {
    providers[0].createBridge({ providerConfig: { url: "ws://brain/talk" } });
    registry.set("S1", { kind: "realtime-relay", connId: "conn-A" });
  });
  await new Promise((r) => setTimeout(r, 0));
  let r = await call(methods, CAPABILITIES_METHOD, { sessionId: "S1" }, "conn-A");
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.payload.session.state, "connecting");
  r = await call(methods, CAPABILITIES_METHOD, { sessionId: "S1" }, "conn-B");
  assert.equal(reason(r), "not_owner");
  r = await call(methods, CAPABILITIES_METHOD, { sessionId: "nope" }, "conn-A");
  assert.equal(reason(r), "unknown_session");
});

test("a closed bridge leaves the registry at once, not when its socket closes", async () => {
  const { sessions, provider, methods } = setup();
  const { bridge, ws } = await openBridge(provider);
  const first = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" });
  await brainAnswers(ws, { ok: true, status: "applied" });
  await first;
  bridge.close(); // the socket's own close event comes later (a microtask here)
  assert.equal(reason(sessions.resolve("relay-1", "conn-A")), "closed_session");
});
