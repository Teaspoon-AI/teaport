// Live smoke test of context notes through a running OpenClaw gateway: one gateway
// connection creates a Talk session (talk.session.create, gateway-relay), waits for
// teaport.talk.capabilities to report it "ready", sends a respond:true note
// (teaport.talk.context) and prints the voice's captions that follow, then closes.
// This is the whole path a Talk client takes: createBridge in the request scope, the
// plugin binding the bridge to its session, the brain's hello/ready, the note, the
// reaction.
//
// The script streams silence as its microphone, as a real client streams its mic from
// the start: the relay opens a Talk turn only on input audio, and fails the session
// when the brain's greeting arrives with no turn open ("no live response owner").
//
// A note must come from the connection that created the session, so this cannot
// reach a call started elsewhere. The brain serves one session at a time: running
// this ends any Talk call in progress.
//
// Run on the gateway host, from a directory where `openclaw` resolves (the installed
// plugin's directory has node_modules/openclaw), e.g.:
//   cp test/note_smoke.mjs ~/.openclaw/extensions/teaport-realtime/ &&
//     node ~/.openclaw/extensions/teaport-realtime/note_smoke.mjs
// Env: OPENCLAW_GATEWAY_URL (default ws://127.0.0.1:18789), OPENCLAW_GATEWAY_TOKEN
// (default: gateway.auth.token from ~/.openclaw/openclaw.json), NOTE, SESSION_KEY.

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const { GatewayClient } = await import("openclaw/plugin-sdk/gateway-runtime");

const url = process.env.OPENCLAW_GATEWAY_URL || "ws://127.0.0.1:18789";
const token =
  process.env.OPENCLAW_GATEWAY_TOKEN ||
  JSON.parse(readFileSync(join(homedir(), ".openclaw", "openclaw.json"), "utf8"))?.gateway?.auth?.token;
const note = process.env.NOTE || "The user tapped the character's left shoulder twice.";
const sessionKey = process.env.SESSION_KEY || "main";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1).padStart(5)}s]`, ...a);

let relaySessionId = null;
const captions = [];
let helloResolve, helloReject;
const hello = new Promise((res, rej) => ((helloResolve = res), (helloReject = rej)));

const client = new GatewayClient({
  url,
  token,
  clientName: "cli",
  clientDisplayName: "teaport note smoke test",
  mode: "cli",
  role: "operator",
  scopes: ["operator.read", "operator.write", "operator.talk"],
  onHelloOk: () => helloResolve(),
  onConnectError: (err) => helloReject(err),
  onEvent: (evt) => {
    const p = evt?.payload;
    if (evt?.event !== "talk.event" || !p || p.relaySessionId !== relaySessionId) return;
    if (p.type === "transcript" && p.final && p.text) {
      captions.push({ at: Date.now(), role: p.role, text: p.text });
      log(`caption ${p.role}: ${JSON.stringify(p.text)}`);
    } else if (p.type === "close" || p.type === "error") {
      log(`${p.type}: ${JSON.stringify(p).slice(0, 160)}`);
    }
  },
});

// Silence as the microphone: 4096-sample PCM16 frames at the session's input rate,
// paced in real time, as the Control UI sends them.
let micTimer = null;
function startSilence(rate) {
  const frame = Buffer.alloc(4096 * 2).toString("base64");
  const ms = Math.round((4096 / rate) * 1000);
  let ts = 0;
  micTimer = setInterval(() => {
    client
      .request("talk.session.appendAudio", { sessionId: relaySessionId, audioBase64: frame, timestamp: ts })
      .catch(() => {});
    ts += ms;
  }, ms);
}

let failed = false;
const fail = (msg) => {
  failed = true;
  log(`FAIL: ${msg}`);
};

client.start();
try {
  await Promise.race([hello, sleep(15000).then(() => Promise.reject(new Error("no hello from the gateway")))]);
  log(`connected to ${url}`);

  const session = await client.request("talk.session.create", {
    sessionKey,
    mode: "realtime",
    transport: "gateway-relay",
    brain: "agent-consult",
  });
  relaySessionId = session.relaySessionId;
  log(`talk.session.create -> ${session.provider}/${session.transport} ${relaySessionId}`);
  if (session.provider !== "teaport") fail(`provider is ${session.provider}, not teaport`);
  startSilence(session.audio?.inputSampleRateHz || 24000);

  // Ask until the brain is ready, as a client would (connecting -> ready).
  let state = null;
  for (let i = 0; i < 60 && state !== "ready"; i++) {
    const caps = await client.request("teaport.talk.capabilities", { sessionId: relaySessionId });
    const s = caps?.session?.state;
    if (s !== state) log(`capabilities: ${s} ${JSON.stringify(caps?.session?.context ?? null)}`);
    state = s;
    if (state === "unsupported" || state === "closed") break;
    if (state !== "ready") await sleep(500);
  }
  if (state !== "ready") throw new Error(`session never became ready (last: ${state})`);

  await sleep(4000); // let the greeting play, so the reaction is its own turn
  const before = captions.length;
  const ack = await client.request("teaport.talk.context", {
    sessionId: relaySessionId,
    text: note,
    respond: true,
    kind: "smoke",
  });
  log(`teaport.talk.context -> ${JSON.stringify(ack)}`);
  if (!ack?.ok) fail("the note was not accepted");

  for (let i = 0; i < 40 && !captions.slice(before).some((c) => c.role === "assistant"); i++) await sleep(500);
  await sleep(1500);
  const reaction = captions.slice(before).filter((c) => c.role === "assistant");
  if (reaction.length) log(`reaction: ${reaction.map((c) => c.text).join(" ")}`);
  else fail("no spoken reaction within 20 s");

  // Another session id is refused by name, without touching this one.
  try {
    await client.request("teaport.talk.context", { sessionId: "no-such-session", text: "x" });
    fail("a made-up session id was accepted");
  } catch (err) {
    log(`made-up session id refused: ${err?.message ?? err}`);
  }
} catch (err) {
  fail(err?.message ?? String(err));
} finally {
  if (micTimer) clearInterval(micTimer);
  if (relaySessionId) {
    await client.request("talk.session.close", { sessionId: relaySessionId }).catch(() => {});
    log("session closed");
  }
  await client.stopAndWait({ timeoutMs: 3000 }).catch(() => {});
  log(failed ? "RESULT: FAIL" : "RESULT: PASS");
  process.exit(failed ? 1 : 0);
}
