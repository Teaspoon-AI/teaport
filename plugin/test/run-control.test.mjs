// OpenClaw's spoken run control (#230): the relay's spoken replies reach the brain
// (sendUserMessage, as a host note), and the bridge answers the brain's question whether
// the relay's run control took a final said while a consult runs (run_control). No
// OpenClaw and no brain: a fake WebSocket plays the brain, and a stand-in plays the SDK's
// shouldAutoControlRealtimeVoiceAgentText.
//
//   node --test test/run-control.test.mjs

import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";

import { buildTeaportRealtimeProvider } from "../provider.js";

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

const HELLO = { type: "hello", features: { context: { max_chars: 2000, max_notes: 20, respond_interval_s: 15 } } };
// What the relay hands sendUserMessage (buildRealtimeVoiceAgentControlSpeechMessage).
const STATUS = [
  "Internal OpenClaw voice control result.",
  "Do not delegate this message or call any tools.",
  "Speak this exact OpenClaw status to the voice call, without adding, removing, or rephrasing words.",
  'Status: "Cancelled the active OpenClaw run."',
].join("\n");
// A stand-in for the SDK's classifier, over a few synthetic phrasings.
const isControl = (text) => /^(cancel that|never mind|how's it going\??|actually\b.*)$/i.test(text);

const tick = () => new Promise((r) => setTimeout(r, 0));

async function session({ controlText = async () => isControl, hello = true, ready = true } = {}) {
  const logs = [];
  const toolCalls = [];
  const provider = buildTeaportRealtimeProvider({
    url: "ws://brain/talk",
    hostVersion: "2026.9.7",
    log: (m) => logs.push(m),
    controlText,
  });
  const bridge = provider.createBridge({ providerConfig: {}, onToolCall: (c) => toolCalls.push(c) });
  await bridge.connect();
  const ws = FakeWebSocket.last;
  if (hello) ws.reply(HELLO);
  if (hello && ready) ws.reply({ type: "ready" });
  return { bridge, ws, logs, toolCalls };
}

const notes = (ws) => ws.sent.filter((m) => m?.type === "context");

async function ask(ws, text, id = "r1") {
  ws.reply({ type: "run_control", id, text });
  for (let i = 0; i < 5; i += 1) await tick();
  return ws.sent.filter((m) => m?.type === "run_control_result" && m.id === id);
}

function startConsult(ws, bridge, callId = "teaport-consult-a1", { acked = true } = {}) {
  ws.reply({ type: "tool_call", call_id: callId, name: "openclaw_agent_consult", args: { question: "q" } });
  if (acked) bridge.submitToolResult(callId, { status: "working" }, { willContinue: true });
}

// ── sendUserMessage ─────────────────────────────────────────────────────────────

test("the relay's spoken reply goes to the brain as a host note it answers", async () => {
  const { bridge, ws } = await session();
  bridge.sendUserMessage(`  ${STATUS}  `);
  const [note] = notes(ws);
  assert.deepEqual(
    { text: note.text, respond: note.respond, kind: note.kind, source: note.source },
    { text: STATUS, respond: true, kind: "openclaw-voice", source: "host" },
  );
  ws.reply({ type: "context_result", id: note.id, ok: true, status: "queued" });
});

test("a host note the brain cannot take is dropped with a log line, never queued", async () => {
  const early = await session({ ready: false });
  early.bridge.sendUserMessage(STATUS);
  assert.equal(notes(early.ws).length, 0, "a note went out before ready");
  assert.match(early.logs.join("\n"), /dropped/);

  const old = await session({ hello: false });
  old.bridge.sendUserMessage(STATUS);
  assert.equal(notes(old.ws).length, 0);

  const refused = await session();
  refused.bridge.sendUserMessage(STATUS);
  const [note] = notes(refused.ws);
  refused.ws.reply({ type: "context_result", id: note.id, ok: false, error: "too_long", message: "x" });
  await tick();
  assert.match(refused.logs.join("\n"), /refused an OpenClaw voice message \(too_long\)/);

  const blank = await session();
  blank.bridge.sendUserMessage("   ");
  assert.equal(notes(blank.ws).length, 0);
});

test("a client's note never passes for the host's", async () => {
  // sendContext is what teaport.talk.context calls (context.js); only sendUserMessage
  // marks a note as the host's.
  const { bridge, ws } = await session();
  const pending = bridge.sendContext({ text: "tap", respond: true, kind: "ui-event" });
  const [note] = notes(ws);
  assert.equal("source" in note, false);
  ws.reply({ type: "context_result", id: note.id, ok: true, status: "applied" });
  await pending;
});

// ── run control ─────────────────────────────────────────────────────────────────

test("with a consult running, OpenClaw's control words are claimed and others are not", async () => {
  const { bridge, ws, toolCalls } = await session();
  startConsult(ws, bridge);
  assert.equal(toolCalls.length, 1, "the consult still reaches the relay");
  assert.deepEqual(await ask(ws, "cancel that", "r1"), [{ type: "run_control_result", id: "r1", claimed: true }]);
  assert.deepEqual(await ask(ws, "What's the weather like?", "r2"), [
    { type: "run_control_result", id: "r2", claimed: false },
  ]);
});

test("nothing is claimed with no consult the relay is running", async () => {
  const { bridge, ws } = await session();
  assert.equal((await ask(ws, "cancel that", "r1"))[0].claimed, false, "no consult at all");
  // Asked for, but the relay never acknowledged it (the brain's CLI lane runs it).
  startConsult(ws, bridge, "teaport-consult-b", { acked: false });
  assert.equal((await ask(ws, "cancel that", "r2"))[0].claimed, false, "not acknowledged");
  // Acknowledged, then answered: over.
  bridge.submitToolResult("teaport-consult-b", { status: "working" }, { willContinue: true });
  assert.equal((await ask(ws, "cancel that", "r3"))[0].claimed, true);
  bridge.submitToolResult("teaport-consult-b", { status: "cancelled", message: "Cancelled." });
  assert.equal((await ask(ws, "cancel that", "r4"))[0].claimed, false, "answered");
  // A tool result for one of the brain's display-only calls is no consult.
  bridge.submitToolResult("fc_1", { ok: true }, { willContinue: true });
  assert.equal((await ask(ws, "cancel that", "r5"))[0].claimed, false);
});

test("without the SDK's classifier, or when it throws, nothing is claimed, and every question is answered", async () => {
  for (const controlText of [
    async () => undefined,
    async () => {
      throw new Error("no realtime-voice SDK");
    },
    async () => () => {
      throw new Error("classifier bug");
    },
    null, // no classifier given at all
  ]) {
    const { bridge, ws } = await session({ controlText });
    startConsult(ws, bridge);
    assert.deepEqual(await ask(ws, "cancel that"), [{ type: "run_control_result", id: "r1", claimed: false }]);
  }
});

test("the classifier is loaded once, when the session is made", async () => {
  let loads = 0;
  const { bridge, ws } = await session({
    controlText: async () => {
      loads += 1;
      return isControl;
    },
  });
  assert.equal(loads, 1);
  startConsult(ws, bridge);
  await ask(ws, "cancel that", "r1");
  await ask(ws, "never mind", "r2");
  assert.equal(loads, 1);
});

test("the plugin tells the brain it answers run-control questions", async () => {
  const { ws } = await session();
  assert.equal(new URL(ws.url).searchParams.get("run_control"), "1");
});
