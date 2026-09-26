// Unit tests: how the brain's full-text transcripts are handed to the Talk view.
//
// OpenClaw 2026.8.1+ appends assistant partials verbatim and lets a final replace a
// bubble only when it extends the bubble's text. So partials go out as deltas, a
// user transcript first closes the open bubble with a final of its text, and if the
// voice carries on, the next bubble and the final carry only what follows the cut.
// Older hosts replace on extension and keep getting full text. See
// TalkTranscriptAdapter in provider.js.
//
// Run: node --test test/transcripts.test.mjs   (npm test runs it too)

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  appendsAssistantDeltas,
  buildTeaportRealtimeProvider,
  TalkTranscriptAdapter,
} from "../provider.js";

const A = "assistant";
const U = "user";

// The brain's caption partials for one sentence: one full-text snapshot per word.
const snapshots = (text) => [...text.matchAll(/\S+/g)].map((m) => text.slice(0, m.index + m[0].length));

test("host version decides deltas: 2026.8.1 and newer, or unknown", () => {
  for (const v of ["2026.8.1", "2026.8.1-beta.1", "2026.9.1", "2026.9.6", "2027.1.0", "v2026.10.2"]) {
    assert.equal(appendsAssistantDeltas(v), true, v);
  }
  for (const v of ["2026.7.1", "2026.7.35", "2026.6.35", "2025.12.9"]) {
    assert.equal(appendsAssistantDeltas(v), false, v);
  }
  for (const v of [undefined, null, "", "dev"]) assert.equal(appendsAssistantDeltas(v), true, String(v));
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

test("an older host gets the full, trimmed text every time", () => {
  const a = new TalkTranscriptAdapter({ assistantDeltas: false });
  assert.deepEqual(a.adapt(A, "I'm", false, "u1"), [[A, "I'm", false]]);
  assert.deepEqual(a.adapt(A, "I'm running", false, "u1"), [[A, "I'm running", false]]);
  assert.deepEqual(a.adapt(U, " What is", false), [[U, "What is", false]]);
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

test("a tool card cuts the caption bubble and stands alone", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "Let me check.", false, "u1");
  assert.deepEqual(a.adapt(A, "> 🔧 **web_search**", true), [
    [A, "Let me check.", true],
    [A, "> 🔧 **web_search**", true],
  ]);
  assert.deepEqual(a.adapt(A, "Let me check. The", false, "u1"), [[A, "The", false]]);
  assert.deepEqual(a.adapt(A, "Let me check. The weather.", true, "u1"), [[A, "The weather.", true]]);
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

test("an untagged caption (older brain) carries on when it extends the text", () => {
  const a = new TalkTranscriptAdapter();
  a.adapt(A, "This is", false);
  a.adapt(U, " Mm.", true);
  assert.deepEqual(a.adapt(A, "This is a long answer.", false), [[A, "a long answer.", false]]);
  assert.deepEqual(a.adapt(A, "Something else", false), [[A, "a long answer.", true], [A, "Something else", false]]);
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

test("the bridge sends full text to an older host", () => {
  const calls = [];
  const provider = buildTeaportRealtimeProvider({ url: "ws://brain/talk", hostVersion: "2026.7.1" });
  const bridge = provider.createBridge({
    providerConfig: {},
    onTranscript: (role, text) => calls.push(text),
  });
  for (const t of ["Hello", "Hello there."]) {
    bridge._onMessage(JSON.stringify({ type: "transcript", role: A, text: t, final: false, utterance: "u1" }));
  }
  assert.deepEqual(calls, ["Hello", "Hello there."]);
});
