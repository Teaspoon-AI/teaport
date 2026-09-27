// Unit tests: how the brain's full-text transcripts are handed to the Talk view.
//
// OpenClaw 2026.7.2+ appends assistant partials verbatim and lets a final replace a
// bubble only when it extends the bubble's text. So partials go out as deltas, a
// user transcript that starts a user entry first closes the open bubble with a final
// of its text, and if the voice carries on, the next bubble and the final carry only
// what follows the cut. "full" mode sends the whole text instead. See
// TalkTranscriptAdapter in provider.js.
//
// Run: node --test test/transcripts.test.mjs   (npm test runs it too)

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  appendsAssistantDeltas,
  buildTeaportRealtimeProvider,
  pickAssistantTranscripts,
  TalkTranscriptAdapter,
} from "../provider.js";

const A = "assistant";
const U = "user";

// The brain's caption partials for one sentence: one full-text snapshot per word.
const snapshots = (text) => [...text.matchAll(/\S+/g)].map((m) => text.slice(0, m.index + m[0].length));

test("host version decides deltas: 2026.7.2 and newer, or unknown", () => {
  for (const v of ["2026.7.2-beta.1", "2026.7.2-beta.3", "2026.8.1", "2026.9.1", "2026.9.6", "2027.1.0", "v2026.10.2"]) {
    assert.equal(appendsAssistantDeltas(v), true, v);
  }
  for (const v of ["2026.7.1", "2026.7.1-beta.6", "2026.6.35", "2025.12.9"]) {
    assert.equal(appendsAssistantDeltas(v), false, v);
  }
  // "0.0.0" is OpenClaw's own "version unknown".
  for (const v of [undefined, null, "", "dev", "0.0.0"]) assert.equal(appendsAssistantDeltas(v), true, String(v));
});

test("assistantTranscripts pins the shape; auto goes by the version", () => {
  let reads = 0;
  const read = (v) => () => {
    reads += 1;
    return v;
  };
  assert.deepEqual(pickAssistantTranscripts("full", read("2026.9.1")), { deltas: false, reason: 'assistantTranscripts="full"' });
  assert.deepEqual(pickAssistantTranscripts("delta", read("2026.7.1")), { deltas: true, reason: 'assistantTranscripts="delta"' });
  assert.equal(reads, 0); // a pinned shape never reads the version
  assert.deepEqual(pickAssistantTranscripts(undefined, read("2026.9.1")), { deltas: true, reason: "auto, OpenClaw 2026.9.1" });
  assert.deepEqual(pickAssistantTranscripts("auto", read("2026.7.1")), { deltas: false, reason: "auto, OpenClaw 2026.7.1" });
  assert.equal(pickAssistantTranscripts("deltas", read("2026.7.1")).deltas, false); // a typo falls back to auto
  assert.match(pickAssistantTranscripts("deltas", read("2026.7.1")).reason, /ignored assistantTranscripts="deltas"/);
  const throws = () => {
    throw new Error("plugin runtime module could not be resolved");
  };
  assert.deepEqual(pickAssistantTranscripts("auto", throws), { deltas: true, reason: "auto, OpenClaw version unknown" });
});

test("assistant partials become deltas; the final stays the full text", () => {
  const a = new TalkTranscriptAdapter();
  const text = "I'm running smoothly, with plenty of memory left.";
  const out = snapshots(text).flatMap((s) => a.adapt(A, s, false, "u1"));
  assert.deepEqual(out.slice(0, 3), [[A, "I'm", false], [A, " running", false], [A, " smoothly,", false]]);
  assert.equal(out.map(([, t]) => t).join(""), text); // the bubble once the partials are appended
  assert.deepEqual(a.adapt(A, text, true, "u1"), [[A, text, true]]);
});

test("a later sentence continues the same bubble", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Sure.", false, "u1");
  assert.deepEqual(a.adapt(A, "Sure. Here", false, "u1"), [[A, " Here", false]]);
});

test("full mode sends the full, trimmed text every time", () => {
  const a = new TalkTranscriptAdapter({ assistantDeltas: false });
  assert.deepEqual(a.adapt(A, "I'm", false, "u1"), [[A, "I'm", false]]);
  assert.deepEqual(a.adapt(A, "I'm running", false, "u1"), [[A, "I'm running", false]]);
  assert.deepEqual(a.adapt(U, " What is", false), [[U, "What is", false]]);
});

test("full mode drops a final that repeats the bubble the user's words closed", () => {
  const a = new TalkTranscriptAdapter({ assistantDeltas: false });
  a.adapt(A, "Hey there.", false, "u1");
  a.adapt(U, " Hi", false);
  assert.deepEqual(a.adapt(A, "Hey there.", true, "u1"), []);
  // With no user words in between, the final goes out as always.
  a.adapt(A, "Sure.", false, "u2");
  assert.deepEqual(a.adapt(A, "Sure.", true, "u2"), [[A, "Sure.", true]]);
});

test("text that adds nothing is not sent", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Hello there", false, "u1");
  assert.deepEqual(a.adapt(A, "Hello there", false, "u1"), []);
});

test("empty text is forwarded as before and changes nothing", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Hello", false, "u1");
  assert.deepEqual(a.adapt(U, "   ", false), [[U, "", false]]);
  assert.deepEqual(a.adapt(A, "Hello there", false, "u1"), [[A, " there", false]]); // still open
});

test("user transcripts are trimmed and pass through", () => {
  const a = new TalkTranscriptAdapter();
  assert.deepEqual(a.adapt(U, " What time is it?", true), [[U, "What time is it?", true]]);
});

test("talking over the voice cuts its bubble; the carry-on starts after the cut", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "One, two,", false, "c");
  a.adapt(A, "One, two, three,", false, "c");
  // The bubble is closed with a final of its text before the user's words go out.
  assert.deepEqual(a.adapt(U, " Stop", false), [[A, "One, two, three,", true], [U, "Stop", false]]);
  assert.deepEqual(a.adapt(U, " Stop.", true), [[U, "Stop.", true]]);
  // The voice carried on: the new bubble holds only what came after the cut.
  assert.deepEqual(a.adapt(A, "One, two, three, four, five,", false, "c"), [[A, "four, five,", false]]);
  assert.deepEqual(a.adapt(A, "One, two, three, four, five, six.", false, "c"), [[A, " six.", false]]);
  // The final carries the same span, so it replaces that bubble.
  assert.deepEqual(a.adapt(A, "One, two, three, four, five, six.", true, "c"), [[A, "four, five, six.", true]]);
});

test("a final with nothing past the cut sends nothing", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Sure thing.", false, "u1");
  a.adapt(U, " Thanks", false);
  assert.deepEqual(a.adapt(A, "Sure thing.", true, "u1"), []);
});

test("a new reply that begins with the cut words is shown whole", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Okay,", false, "u1");
  a.adapt(U, " Actually wait.", true); // barge: u1 never finishes
  assert.deepEqual(a.adapt(A, "Okay, what's", false, "u2"), [[A, "Okay, what's", false]]);
});

test("a tool card waits for the caption's utterance, then stands alone", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Let", false, "u1");
  // The tool ran while "Let me check." was still playing: hold the card.
  assert.deepEqual(a.adapt(A, "> 🔧 **web_search**", true), []);
  assert.deepEqual(a.adapt(A, "Let me check.", false, "u1"), [[A, " me check.", false]]);
  assert.deepEqual(a.adapt(A, "Let me check.", true, "u1"), [
    [A, "Let me check.", true],
    [A, "> 🔧 **web_search**", true],
  ]);
});

test("a tool card with no caption bubble open goes out at once", () => {
  const a = new TalkTranscriptAdapter();
  assert.deepEqual(a.adapt(A, "> 🔧 **web_search**", true), [[A, "> 🔧 **web_search**", true]]);
});

test("a held tool card follows the cut when the user talks", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Let", false, "u1");
  a.adapt(A, "> 🔧 **web_search**", true);
  assert.deepEqual(a.adapt(U, " Wait", false), [
    [A, "Let", true],
    [A, "> 🔧 **web_search**", true],
    [U, "Wait", false],
  ]);
});

test("a line break the partials didn't carry neither freezes the bubble nor repeats the cut", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "To be. Whether", false, "u1");
  a.adapt(U, " Hm", false); // cut: "To be. Whether"
  // An older brain joins the sentence after its line break in later messages.
  assert.deepEqual(a.adapt(A, "To be. \nWhether 'tis", false, "u1"), [[A, "'tis", false]]);
  assert.deepEqual(a.adapt(A, "To be. \nWhether 'tis nobler.", false, "u1"), [[A, " nobler.", false]]);
  assert.deepEqual(a.adapt(A, "To be. \nWhether 'tis nobler.", true, "u1"), [[A, "'tis nobler.", true]]);
});

test("a new utterance under an open bubble closes it first", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "First answer that", false, "u1"); // barged with no user transcript
  assert.deepEqual(a.adapt(A, "Second", false, "u2"), [
    [A, "First answer that", true],
    [A, "Second", false],
  ]);
});

test("a caption final with no partials is sent whole", () => {
  const a = new TalkTranscriptAdapter();
  assert.deepEqual(a.adapt(A, "Done.", true, "u1"), [[A, "Done.", true]]);
});

test("an untagged caption (older brain) continues only its open bubble", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Okay,", false);
  assert.deepEqual(a.adapt(A, "Okay, so", false), [[A, " so", false]]);
  a.adapt(U, " Actually wait.", true); // cut; an older brain sends no final on barge-in
  // Without an utterance id this may be a new reply that starts with the same words,
  // so it is shown whole rather than cut short.
  assert.deepEqual(a.adapt(A, "Okay, so what's up?", false), [[A, "Okay, so what's up?", false]]);
});

test("user text that updates its entry leaves the carry-on bubble open", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "One, two,", false, "c");
  assert.deepEqual(a.adapt(U, " Stop", false), [[A, "One, two,", true], [U, "Stop", false]]);
  assert.deepEqual(a.adapt(A, "One, two, three, four,", false, "c"), [[A, "three, four,", false]]);
  // A slow interim and the late final extend the user's open entry: the view updates
  // it in place and keeps the carry-on bubble open, so nothing is cut.
  assert.deepEqual(a.adapt(U, " Stop please", false), [[U, "Stop please", false]]);
  assert.deepEqual(a.adapt(U, " Stop please.", true), [[U, "Stop please.", true]]);
  assert.deepEqual(a.adapt(A, "One, two, three, four, five.", false, "c"), [[A, " five.", false]]);
  assert.deepEqual(a.adapt(A, "One, two, three, four, five.", true, "c"), [[A, "three, four, five.", true]]);
});

test("new user words after the voice spoke start an entry and cut the bubble", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "One,", false, "c");
  a.adapt(U, " Stop", false);
  a.adapt(A, "One, two,", false, "c");
  // "Wait" neither extends nor repeats "Stop": the view starts a new user entry.
  assert.deepEqual(a.adapt(U, " Wait", false), [[A, "two,", true], [U, "Wait", false]]);
  // The user's final closed the entry; the next words start another one.
  a.adapt(U, " Wait.", true);
  a.adapt(A, "One, two, three,", false, "c");
  assert.deepEqual(a.adapt(U, " Okay", false), [[A, "three,", true], [U, "Okay", false]]);
});

test("user text while the entry still streams doesn't cut", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Hello", false, "u1");
  a.adapt(U, " So", false); // cut
  // The voice hasn't spoken since, so any revision just updates the entry.
  assert.deepEqual(a.adapt(U, " Sew what", false), [[U, "Sew what", false]]);
});

test("an empty user interim leaves the bubble open for its final", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Hello", false, "u1");
  assert.deepEqual(a.adapt(U, "", false), [[U, "", false]]);
  assert.deepEqual(a.adapt(A, "Hello", true, "u1"), [[A, "Hello", true]]);
});

test("the voice ending closes the bubble and lets the held cards out", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Let me", false, "u1");
  a.adapt(A, "> 🔧 **web_search**", true);
  assert.deepEqual(a.end(), [
    [A, "Let me", true],
    [A, "> 🔧 **web_search**", true],
  ]);
  assert.deepEqual(a.end(), []);
  // The barged utterance is over: text tagged with it again is a new bubble.
  assert.deepEqual(a.adapt(A, "Let me see", false, "u1"), [[A, "Let me see", false]]);
});

test("the bridge forwards every event the adapter makes", () => {
  const calls = [];
  const provider = buildTeaportRealtimeProvider({ url: "ws://brain/talk", hostVersion: "2026.9.1" });
  const bridge = provider.createBridge({
    providerConfig: {},
    onTranscript: (role, text, final) => calls.push([role, text, final]),
  });
  const msg = (role, text, final, utterance) =>
    bridge._onMessage(JSON.stringify({ type: "transcript", role, text, final, utterance }));
  msg(A, "Hello", false, "u1");
  msg(A, "Hello", false, "u1");
  msg(U, " Hi", true);
  msg(A, "Hello there.", false, "u1");
  msg(A, "Hello there.", true, "u1");
  assert.deepEqual(calls, [
    [A, "Hello", false],
    [A, "Hello", true],
    [U, "Hi", true],
    [A, "there.", false],
    [A, "there.", true],
  ]);
});

const bridgeFor = (config = {}, defaults = {}) => {
  const calls = [];
  const logs = [];
  const provider = buildTeaportRealtimeProvider({ url: "ws://brain/talk", log: (m) => logs.push(m), ...defaults });
  const bridge = provider.createBridge({
    providerConfig: config,
    onTranscript: (role, text, final) => calls.push([role, text, final]),
    onClearAudio: () => calls.push("clear"),
  });
  const msg = (m) => bridge._onMessage(JSON.stringify(m));
  return { bridge, calls, logs, msg };
};

test("the bridge sends full text when the host is old or the config says so", () => {
  for (const [config, defaults] of [[{}, { hostVersion: "2026.7.1" }], [{ assistantTranscripts: "full" }, { hostVersion: "2026.9.1" }]]) {
    const { calls, logs, msg } = bridgeFor(config, defaults);
    for (const t of ["Hello", "Hello there."]) msg({ type: "transcript", role: A, text: t, final: false, utterance: "u1" });
    assert.deepEqual(calls.map(([, t]) => t), ["Hello", "Hello there."]);
    assert.match(logs[0], /full text/);
  }
});

test("assistantTranscripts in the session config overrides the version", () => {
  const { calls, logs, msg } = bridgeFor({ assistantTranscripts: "delta" }, { hostVersion: "2026.7.1" });
  for (const t of ["Hello", "Hello there."]) msg({ type: "transcript", role: A, text: t, final: false, utterance: "u1" });
  assert.deepEqual(calls.map(([, t]) => t), ["Hello", " there."]);
  assert.equal(logs[0], 'teaport-realtime: assistant captions sent as deltas (assistantTranscripts="delta")');
});

test("a barge-in clear closes the bubble and sends the held cards", () => {
  const { calls, msg } = bridgeFor();
  msg({ type: "transcript", role: A, text: "Let me", final: false, utterance: "u1" });
  msg({ type: "transcript", role: A, text: "> 🔧 **web_search**", final: true });
  msg({ type: "clear" });
  assert.deepEqual(calls, [[A, "Let me", false], "clear", [A, "Let me", true], [A, "> 🔧 **web_search**", true]]);
});

test("closing the session closes the bubble and sends the held cards", () => {
  const { bridge, calls, msg } = bridgeFor();
  msg({ type: "transcript", role: A, text: "Let me", final: false, utterance: "u1" });
  msg({ type: "transcript", role: A, text: "> 🔧 **web_search**", final: true });
  bridge.close();
  bridge.close();
  assert.deepEqual(calls, [[A, "Let me", false], [A, "Let me", true], [A, "> 🔧 **web_search**", true]]);
});

test("the plugin registers without reading the host version", async (t) => {
  // OpenClaw builds its plugin runtime lazily; registering must not force it. The
  // version is read when a Talk session starts, and one that can't be read is unknown.
  const { mkdtempSync, mkdirSync, writeFileSync, copyFileSync, readFileSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { pathToFileURL, fileURLToPath } = await import("node:url");
  const dir = mkdtempSync(join(tmpdir(), "teaport-plugin-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const sdk = join(dir, "node_modules", "openclaw", "plugin-sdk");
  mkdirSync(sdk, { recursive: true });
  writeFileSync(join(dir, "node_modules", "openclaw", "package.json"),
    JSON.stringify({ name: "openclaw", type: "module", exports: { "./plugin-sdk/plugin-entry": "./plugin-sdk/plugin-entry.js" } }));
  writeFileSync(join(sdk, "plugin-entry.js"), "export const definePluginEntry = (entry) => entry;\n");
  writeFileSync(join(dir, "package.json"), JSON.stringify({ type: "module" }));
  // Load exactly what the package ships, so a module index.js gains is loaded too.
  const here = fileURLToPath(new URL("..", import.meta.url));
  const { files } = JSON.parse(readFileSync(join(here, "package.json"), "utf8"));
  for (const f of files) copyFileSync(join(here, f), join(dir, f));
  const entry = (await import(pathToFileURL(join(dir, "index.js")).href)).default;
  const registered = [];
  const logs = [];
  let runtimeReads = 0;
  entry.register({
    get runtime() {
      runtimeReads += 1;
      throw new Error("plugin runtime module could not be resolved");
    },
    logger: { info: (m) => logs.push(m) },
    registerRealtimeVoiceProvider: (p) => registered.push(p),
  });
  assert.deepEqual(registered.map((p) => p.id), ["teaport"]);
  assert.equal(runtimeReads, 0);
  registered[0].createBridge({ providerConfig: { url: "ws://brain/talk" } });
  registered[0].createBridge({ providerConfig: { url: "ws://brain/talk" } });
  assert.equal(runtimeReads, 1); // read once, on the first session
  assert.deepEqual(logs, Array(2).fill("teaport-realtime: assistant captions sent as deltas (auto, OpenClaw version unknown)"));
});
