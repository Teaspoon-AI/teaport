// Context notes (issue #71): the teaport.talk.context / teaport.talk.capabilities
// gateway methods, the session registry that finds the bridge for a sessionId, and the
// bridge's side of the /talk exchange (hello, context, context_result). No OpenClaw
// and no brain: a fake WebSocket plays the brain.
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

/** A provider wired to a registry as index.js wires it, with `conn` as the caller. */
function setup({ lookup } = {}) {
  const who = { conn: "conn-A" };
  const sessions = new TalkSessions({ currentConnId: () => who.conn, lookupTalkSession: lookup });
  const provider = buildTeaportRealtimeProvider({ url: "ws://brain/talk", sessions });
  const methods = contextMethods(sessions);
  return { who, sessions, provider, methods };
}

async function openBridge(provider, { hello = true } = {}) {
  const bridge = provider.createBridge({ providerConfig: {} });
  await bridge.connect();
  const ws = FakeWebSocket.last;
  if (hello) ws.reply(HELLO);
  return { bridge, ws };
}

/** Call a gateway method handler; resolves with what it passed to respond(). */
function call(methods, name, params, connId = "conn-A") {
  return new Promise((resolve, reject) => {
    const respond = (ok, payload, error) => resolve({ ok, payload, error });
    Promise.resolve(methods[name]({ params, client: { connId }, respond })).catch(reject);
  });
}

/** Answer the next context note the fake brain receives. */
async function brainAnswers(ws, answer) {
  for (let i = 0; i < 50 && !ws.sent.some((m) => m.type === "context" && !m.answered); i += 1) {
    await new Promise((r) => setTimeout(r, 1));
  }
  const note = ws.sent.find((m) => m.type === "context" && !m.answered);
  assert.ok(note, "no context note reached the brain");
  note.answered = true;
  ws.reply({ type: "context_result", id: note.id, ...answer });
  return note;
}

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
  assert.equal(r.error.details.reason, "rate_limited");

  pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "tap" });
  await brainAnswers(ws, { ok: false, error: "empty", message: "the note has no text" });
  r = await pending;
  assert.equal(r.error.code, "INVALID_REQUEST");
  assert.equal(r.error.details.reason, "empty");
});

test("the brain's announced length limit is enforced before the round trip", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider); // hello says max_chars 40
  const r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "x".repeat(41) });
  assert.equal(r.error.details.reason, "too_long");
  assert.equal(ws.sent.filter((m) => m.type === "context").length, 0);
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
  assert.equal(r.error.details.reason, "closed_session");
});

test("the brain going away fails the note waiting on it", async () => {
  const { provider, methods } = setup();
  const { ws } = await openBridge(provider);
  const pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" });
  await new Promise((r) => setTimeout(r, 5));
  ws.emit("close", {}); // evicted by a newer /talk connection, say
  const r = await pending;
  assert.equal(r.ok, false);
  assert.equal(r.error.code, "UNAVAILABLE");
});

test("with no live session at all the id is unknown", async () => {
  const { methods } = setup();
  const r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-9", text: "hi" });
  assert.equal(r.error.details.reason, "unknown_session");
});

test("another connection's session is not reachable", async () => {
  const { provider, methods } = setup();
  await openBridge(provider); // created on conn-A
  const r = await call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" }, "conn-B");
  assert.equal(r.ok, false);
  assert.equal(r.error.details.reason, "unknown_session");
});

test("the host's Talk registry vets the id before anything is bound", async () => {
  const records = new Map([
    ["relay-1", { kind: "realtime-relay", connId: "conn-A" }],
    ["relay-B", { kind: "realtime-relay", connId: "conn-B" }],
    ["room-1", { kind: "managed-room", connId: "conn-A" }],
  ]);
  const { provider, methods } = setup({ lookup: (id) => records.get(id) ?? null });
  const { ws } = await openBridge(provider);
  assert.equal((await call(methods, CONTEXT_METHOD, { sessionId: "made-up", text: "hi" })).error.details.reason, "unknown_session");
  assert.equal((await call(methods, CONTEXT_METHOD, { sessionId: "relay-B", text: "hi" })).error.details.reason, "not_owner");
  assert.equal((await call(methods, CONTEXT_METHOD, { sessionId: "room-1", text: "hi" })).error.details.reason, "not_relay");
  // None of those bound the bridge: the real id still gets it.
  const pending = call(methods, CONTEXT_METHOD, { sessionId: "relay-1", text: "hi" });
  await brainAnswers(ws, { ok: true, status: "applied" });
  assert.equal((await pending).ok, true);
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

// ── discovery ───────────────────────────────────────────────────────────────────

test("capabilities name the method, and the session's limits when asked", async () => {
  const { provider, methods } = setup();
  let r = await call(methods, CAPABILITIES_METHOD, {});
  assert.equal(r.ok, true);
  assert.equal(r.payload.context.method, "teaport.talk.context");
  assert.equal(r.payload.session, undefined);

  await openBridge(provider);
  r = await call(methods, CAPABILITIES_METHOD, { sessionId: "relay-1" });
  assert.deepEqual(r.payload.session, {
    sessionId: "relay-1",
    context: { maxChars: 40, maxNotes: 20, respondIntervalMs: 15000 },
  });
});

test("a brain that sent no hello reports no context support", async () => {
  const { provider, methods } = setup();
  await openBridge(provider, { hello: false });
  const r = await call(methods, CAPABILITIES_METHOD, { sessionId: "relay-1" });
  assert.equal(r.payload.session.context, null);
});
