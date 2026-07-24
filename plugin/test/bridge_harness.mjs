// Standalone test of the teaport bridge <-> Pipecat gateway_server path, with NO
// OpenClaw gateway in the loop. Exercises exactly the code in provider.js the way
// the relay would: createBridge -> connect -> sendAudio(Buffer) -> onAudio/onClear/
// onTranscript. Reports the assistant audio + transcripts that come back.
//
//   node test/bridge_harness.mjs [user.wav] [ws://127.0.0.1:7861/talk] [out.wav]
//
// With no WAV it just connects and captures the server's greeting (proves WS +
// pipeline + LLM + TTS + audio serialization). With a 24k mono PCM16 WAV it streams
// it as "user mic" audio (proves STT + a full turn). Requires Node >= 22 (global
// WebSocket) and a running gateway_server.py.

import { readFileSync, writeFileSync } from "node:fs";

import { buildTeaportRealtimeProvider } from "../provider.js";

const wavPath = process.argv[2] && process.argv[2] !== "-" ? process.argv[2] : null;
const url = process.argv[3] || "ws://127.0.0.1:7861/talk";
const outPath = process.argv[4] || "/tmp/teaport_bridge_out.wav";
const SR = 24000;

function readWavPcm(path) {
  const buf = readFileSync(path);
  let off = 12; // skip RIFF/size/WAVE
  while (off + 8 <= buf.length) {
    const id = buf.toString("ascii", off, off + 4);
    const size = buf.readUInt32LE(off + 4);
    if (id === "data") return buf.subarray(off + 8, off + 8 + size);
    off += 8 + size + (size & 1);
  }
  throw new Error("no data chunk in WAV");
}

function pcmToWav(pcm, sr) {
  const h = Buffer.alloc(44);
  h.write("RIFF", 0);
  h.writeUInt32LE(36 + pcm.length, 4);
  h.write("WAVE", 8);
  h.write("fmt ", 12);
  h.writeUInt32LE(16, 16);
  h.writeUInt16LE(1, 20); // PCM
  h.writeUInt16LE(1, 22); // mono
  h.writeUInt32LE(sr, 24);
  h.writeUInt32LE(sr * 2, 28);
  h.writeUInt16LE(2, 32);
  h.writeUInt16LE(16, 34);
  h.write("data", 36);
  h.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([h, pcm]);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const audioOut = [];
const events = [];
let cleared = 0;

const req = {
  providerConfig: { url },
  onAudio: (buf) => audioOut.push(Buffer.from(buf)),
  onClearAudio: () => {
    cleared++;
    events.push("CLEAR");
  },
  onTranscript: (role, text, final) =>
    events.push(`transcript[${role}${final ? "/final" : ""}]: ${text}`),
  onReady: () => events.push("READY"),
  onError: (err) => events.push(`ERROR: ${err.message}`),
  onClose: (reason) => events.push(`CLOSE: ${reason}`),
};

const provider = buildTeaportRealtimeProvider();
console.log("provider:", provider.id, "| configured:", provider.isConfigured(req));
const bridge = provider.createBridge(req);

await bridge.connect();
console.log("connected:", bridge.isConnected());

if (wavPath) {
  if (process.env.BARGE) {
    // Barge-in test: wait for the greeting to START, then talk over it and expect
    // a {"type":"clear"} (onClearAudio) as the pipeline interrupts the bot.
    for (let t = 0; t < 80 && audioOut.length === 0; t++) await sleep(50);
    console.log("greeting started — barging in");
  } else {
    await sleep(3000); // let the on-connect greeting finish, so this is a clean turn
    audioOut.length = 0; // keep only the response audio in out.wav
  }
  const pcm = readWavPcm(wavPath);
  const frame = SR * 0.02 * 2; // ~20 ms
  console.log(
    `streaming ${pcm.length} bytes (~${(pcm.length / (SR * 2)).toFixed(1)}s) of user audio`,
  );
  for (let i = 0; i < pcm.length; i += frame) {
    bridge.sendAudio(pcm.subarray(i, Math.min(i + frame, pcm.length)));
    await sleep(20);
  }
} else {
  console.log("no WAV given — capturing greeting only");
}

console.log("waiting for response...");
for (let t = 0; t < 240 && audioOut.length === 0; t++) await sleep(50);
await sleep(8000); // let the full reply arrive

bridge.close();
await sleep(200);

const total = Buffer.concat(audioOut);
if (total.length) writeFileSync(outPath, pcmToWav(total, SR));
console.log("\n--- events ---");
for (const e of events) console.log(e);
console.log(
  `\nassistant audio: ${total.length} bytes (~${(total.length / (SR * 2)).toFixed(1)}s)` +
    (total.length ? ` -> ${outPath}` : ""),
);
console.log("clears:", cleared);
process.exit(0);
