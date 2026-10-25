// OpenClaw's spoken run control (#230): the relay's spoken replies reach the brain
// (sendUserMessage, as a host note); the bridge answers the brain's question whether the
// relay's run control took a final said while a consult runs (run_control), claiming it
// only once the relay acts; and the model's own openclaw_agent_control calls go to the
// relay with their results routed back. No OpenClaw and no brain: a fake WebSocket plays
// the brain, and stand-ins play the SDK's shouldAutoControlRealtimeVoiceAgentText and
// isLikelyRealtimeVoiceAssistantEchoTranscript.
//
//   node --test test/run-control.test.mjs

import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";

import { CONFIRM_MS, ECHO_LOOKBACK_MS, buildTeaportRealtimeProvider } from "../provider.js";

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

// setImmediate, not setTimeout: the confirmation tests mock the timers.
const tick = () => new Promise((r) => setImmediate(r));
const noEcho = () => false;

async function session({ runControl = async () => ({ isControl, isEcho: noEcho }), hello = true, ready = true } = {}) {
  const logs = [];
  const toolCalls = [];
  const events = [];
  const provider = buildTeaportRealtimeProvider({
    url: "ws://brain/talk",
    hostVersion: "2026.9.7",
    log: (m) => logs.push(m),
    runControl,
  });
  const bridge = provider.createBridge({
    providerConfig: {},
    onToolCall: (c) => toolCalls.push(c),
    onEvent: (e) => events.push(e),
    onTranscript: () => {},
  });
  await bridge.connect();
  const ws = FakeWebSocket.last;
  if (hello) ws.reply(HELLO);
  if (hello && ready) ws.reply({ type: "ready" });
  return { bridge, ws, logs, toolCalls, events };
}

const notes = (ws) => ws.sent.filter((m) => m?.type === "context");

const results = (ws, id) => ws.sent.filter((m) => m?.type === "run_control_result" && m.id === id);

async function ask(ws, text, id = "r1") {
  ws.reply({ type: "run_control", id, text });
  for (let i = 0; i < 5; i += 1) await tick();
  return results(ws, id);
}

// The brain's user final, as UserTranscriptEmitter sends it; the bridge forwards it to the
// relay, which reads it for run control.
function said(ws, text) {
  ws.reply({ type: "transcript", role: "user", text, final: true });
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

test("control words are claimed once the relay acts on them, and its reply follows the claim", async () => {
  const { bridge, ws, toolCalls } = await session();
  startConsult(ws, bridge);
  assert.equal(toolCalls.length, 1, "the consult still reaches the relay");
  said(ws, "cancel that");
  assert.deepEqual(await ask(ws, "cancel that", "r1"), [], "nothing until the relay acts");
  bridge.sendUserMessage(STATUS);
  const order = ws.sent.filter((m) => m?.type === "run_control_result" || m?.type === "context").map((m) => m.type);
  assert.deepEqual(order, ["run_control_result", "context"], "the claim reaches the brain before the relay's reply");
  assert.deepEqual(results(ws, "r1"), [{ type: "run_control_result", id: "r1", claimed: true }]);
  // Other words are the model's at once.
  assert.deepEqual(await ask(ws, "What's the weather like?", "r2"), [
    { type: "run_control_result", id: "r2", claimed: false },
  ]);
});

test("a consult the relay cancelled confirms the claim, and the transcript and question may cross", async () => {
  const { bridge, ws } = await session();
  startConsult(ws, bridge);
  // The question first, the transcript after: the relay acts once it reads the words.
  assert.deepEqual(await ask(ws, "never mind", "r1"), []);
  said(ws, "never mind");
  bridge.submitToolResult("teaport-consult-a1", { status: "cancelled", message: "Cancelled the active OpenClaw run." });
  assert.deepEqual(results(ws, "r1"), [{ type: "run_control_result", id: "r1", claimed: true }]);
  // The relay already acted when the question comes (the transcript went first): at once.
  startConsult(ws, bridge, "teaport-consult-a2");
  said(ws, "how's it going?");
  bridge.sendUserMessage(STATUS);
  assert.deepEqual(await ask(ws, "how's it going?", "r2"), [{ type: "run_control_result", id: "r2", claimed: true }]);
});

test("words said again are not confirmed by the act on the first time", async (t) => {
  // "cancel that" twice, the second asked about before its own transcript reaches the
  // bridge: matched with the first final, the relay's act on the first would claim it.
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { bridge, ws } = await session();
  startConsult(ws, bridge);
  said(ws, "cancel that");
  assert.deepEqual(await ask(ws, "cancel that", "r1"), []);
  bridge.sendUserMessage(STATUS);
  assert.deepEqual(results(ws, "r1"), [{ type: "run_control_result", id: "r1", claimed: true }]);
  startConsult(ws, bridge, "teaport-consult-a2");
  assert.deepEqual(await ask(ws, "cancel that", "r2"), [], "the first act claims nothing new");
  said(ws, "cancel that");
  bridge.sendUserMessage(STATUS);
  assert.deepEqual(results(ws, "r2"), [{ type: "run_control_result", id: "r2", claimed: true }]);
  // Asked ahead again, and this time the relay does not act: the model's words.
  assert.deepEqual(await ask(ws, "cancel that", "r3"), []);
  said(ws, "cancel that");
  t.mock.timers.tick(CONFIRM_MS);
  await tick();
  assert.deepEqual(results(ws, "r3"), [{ type: "run_control_result", id: "r3", claimed: false }]);
});

test("words the relay does not act on within CONFIRM_MS are the model's", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { bridge, ws, logs } = await session();
  startConsult(ws, bridge);
  // An act from before the words reached the relay does not count for them.
  bridge.sendUserMessage(STATUS);
  said(ws, "actually make it tomorrow");
  assert.deepEqual(await ask(ws, "actually make it tomorrow", "r1"), []);
  t.mock.timers.tick(CONFIRM_MS - 1);
  await tick();
  assert.deepEqual(results(ws, "r1"), []);
  t.mock.timers.tick(1);
  await tick();
  assert.deepEqual(results(ws, "r1"), [{ type: "run_control_result", id: "r1", claimed: false }]);
  assert.match(logs.join("\n"), /did not act within 2000 ms/);
  // A late act confirms nothing any more.
  bridge.sendUserMessage(STATUS);
  assert.equal(results(ws, "r1").length, 1);
});

test("what the relay takes for the assistant's echo is never claimed", async () => {
  // The relay drops a final that overlaps the assistant's recent speech before its run
  // control reads it (isLikelyAssistantEchoTranscript): mirrored with the SDK's check, on
  // the finals this bridge forwarded, as they stood when the words reached the relay.
  const seen = [];
  const isEcho = (params) => {
    seen.push(params);
    return params.text === "cancel that check on the inbox";
  };
  const { bridge, ws } = await session({ runControl: async () => ({ isControl: () => true, isEcho }) });
  startConsult(ws, bridge);
  ws.reply({ type: "transcript", role: "assistant", text: "I'll cancel that check on the inbox.", final: true, utterance: "u1" });
  said(ws, "cancel that check on the inbox");
  assert.deepEqual(await ask(ws, "cancel that check on the inbox", "r1"), [
    { type: "run_control_result", id: "r1", claimed: false },
  ]);
  const [params] = seen;
  assert.equal(params.lookbackMs, ECHO_LOOKBACK_MS);
  assert.equal(ECHO_LOOKBACK_MS, 12_000);
  assert.deepEqual(
    params.transcript.map(({ role, text }) => [role, text]),
    [
      ["assistant", "I'll cancel that check on the inbox."],
      ["user", "cancel that check on the inbox"],
    ],
  );
  assert.ok(params.transcript.every((e) => !Number.isNaN(Date.parse(e.at))), "entries carry ISO times, as the SDK reads them");
  // Not an echo: claimed once the relay acts, as usual.
  said(ws, "stop it");
  assert.deepEqual(await ask(ws, "stop it", "r2"), []);
  bridge.sendUserMessage(STATUS);
  assert.equal(results(ws, "r2")[0].claimed, true);
});

test("nothing is claimed with no consult the relay is running", async () => {
  const { bridge, ws } = await session();
  assert.equal((await ask(ws, "cancel that", "r1"))[0].claimed, false, "no consult at all");
  // Asked for, but the relay never acknowledged it (the brain's CLI lane runs it).
  startConsult(ws, bridge, "teaport-consult-b", { acked: false });
  assert.equal((await ask(ws, "cancel that", "r2"))[0].claimed, false, "not acknowledged");
  // Acknowledged, then answered: over.
  bridge.submitToolResult("teaport-consult-b", { status: "working" }, { willContinue: true });
  bridge.submitToolResult("teaport-consult-b", { text: "Sunny." });
  assert.equal((await ask(ws, "cancel that", "r3"))[0].claimed, false, "answered");
  // A tool result for one of the brain's display-only calls is no consult.
  bridge.submitToolResult("fc_1", { ok: true }, { willContinue: true });
  assert.equal((await ask(ws, "cancel that", "r4"))[0].claimed, false);
});

test("without the SDK's classifier, or when it throws, nothing is claimed, and every question is answered", async () => {
  for (const runControl of [
    async () => ({}),
    async () => undefined,
    async () => {
      throw new Error("no realtime-voice SDK");
    },
    async () => ({
      isControl: () => {
        throw new Error("classifier bug");
      },
    }),
    null, // nothing given at all
  ]) {
    const { bridge, ws } = await session({ runControl });
    startConsult(ws, bridge);
    assert.deepEqual(await ask(ws, "cancel that"), [{ type: "run_control_result", id: "r1", claimed: false }]);
  }
  // Without the echo check (or with one that throws), the claim still waits for the relay.
  const { bridge, ws } = await session({
    runControl: async () => ({
      isControl,
      isEcho: () => {
        throw new Error("echo bug");
      },
    }),
  });
  startConsult(ws, bridge);
  assert.deepEqual(await ask(ws, "cancel that"), []);
  bridge.sendUserMessage(STATUS);
  assert.equal(results(ws, "r1")[0].claimed, true);
});

test("the SDK pieces are loaded once, when the session is made", async () => {
  let loads = 0;
  const { bridge, ws } = await session({
    runControl: async () => {
      loads += 1;
      return { isControl, isEcho: noEcho };
    },
  });
  assert.equal(loads, 1);
  startConsult(ws, bridge);
  await ask(ws, "cancel that", "r1");
  bridge.sendUserMessage(STATUS);
  await ask(ws, "never mind", "r2");
  assert.equal(loads, 1);
});

test("the plugin tells the brain it answers run-control questions", async () => {
  const { ws } = await session();
  assert.equal(new URL(ws.url).searchParams.get("run_control"), "1");
});

// ── the model's openclaw_agent_control ─────────────────────────────────────────

test("a control call goes to the relay, and its result back to the brain, never as a consult's", async () => {
  const { bridge, ws, toolCalls } = await session();
  startConsult(ws, bridge);
  const args = { text: "scratch that", mode: "cancel" };
  ws.reply({ type: "tool_call", call_id: "teaport-control-c1", name: "openclaw_agent_control", args });
  assert.deepEqual(toolCalls.at(-1), {
    itemId: "teaport-control-c1",
    callId: "teaport-control-c1",
    name: "openclaw_agent_control",
    args,
  });
  // A claim is waiting when the control's own result comes back (the Control UI's
  // talk.session.steer result: a cancel carries providerResult {status: "cancelled"}).
  said(ws, "cancel that");
  assert.deepEqual(await ask(ws, "cancel that", "r1"), []);
  const result = {
    ok: true,
    mode: "cancel",
    active: true,
    aborted: true,
    message: "Cancelled the active OpenClaw run.",
    providerResult: { status: "cancelled", message: "Cancelled the active OpenClaw run." },
  };
  bridge.submitToolResult("teaport-control-c1", result);
  assert.deepEqual(ws.sent.at(-1), { type: "tool_result", call_id: "teaport-control-c1", result, will_continue: false });
  assert.deepEqual(results(ws, "r1"), [], "a control's result is not the relay's spoken run control acting");
  // The consult is still open: its own cancelled result is what closes it.
  bridge.submitToolResult("teaport-consult-a1", { status: "cancelled", message: "Cancelled the active OpenClaw run." });
  assert.equal(results(ws, "r1")[0].claimed, true);
  assert.equal((await ask(ws, "cancel that", "r2"))[0].claimed, false, "no consult open any more");
});

test("a consult the brain gave up on is cancelled at the relay", async () => {
  const { bridge, ws, events } = await session();
  startConsult(ws, bridge);
  ws.reply({ type: "tool_cancel", call_id: "teaport-consult-a1" });
  assert.deepEqual(events, [{ direction: "server", type: "tool.call.cancelled", itemId: "teaport-consult-a1" }]);
  assert.equal((await ask(ws, "cancel that"))[0].claimed, false, "no consult open any more");
  // Only a consult the relay has: anything else is ignored.
  ws.reply({ type: "tool_cancel", call_id: "teaport-consult-gone" });
  ws.reply({ type: "tool_cancel" });
  assert.equal(events.length, 1);
});
