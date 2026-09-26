// Unit tests: how the brain's full-text transcripts are handed to the Talk view.
//
// OpenClaw 2026.8.1+ appends assistant partials verbatim, so they must be deltas
// against the view's open bubble, and a new bubble must open with the full text so
// the final (always the full utterance) can replace it. Older hosts replace on
// extension and keep getting full text. See TalkTranscriptAdapter in provider.js.
//
// Run: node --test test/transcripts.test.mjs   (npm test runs it too)

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  appendsAssistantDeltas,
  buildTeaportRealtimeProvider,
  TalkTranscriptAdapter,
} from "../provider.js";

// A clock the test advances; the adapter mirrors a time-bounded rule of the view.
function adapter(opts = {}) {
  const clock = { t: 1000 };
  const a = new TalkTranscriptAdapter({ now: () => clock.t, ...opts });
  const send = (role, text, final = false) => {
    clock.t += 100;
    return a.adapt(role, text, final);
  };
  return { send, clock };
}

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
  const { send } = adapter();
  const text = "I'm running smoothly, with plenty of memory left.";
  const out = snapshots(text).map((s) => send("assistant", s));
  assert.deepEqual(out.slice(0, 4), ["I'm", " running", " smoothly,", " with"]);
  assert.equal(out.join(""), text); // what the view shows once the partials are appended
  assert.equal(send("assistant", text, true), text);
});

test("a later sentence continues the same bubble", () => {
  const { send } = adapter();
  send("assistant", "Sure.");
  assert.equal(send("assistant", "Sure. Here"), " Here");
  assert.equal(send("assistant", "Sure. Here it is."), " it is.");
});

test("an older host gets the full, trimmed text every time", () => {
  const { send } = adapter({ assistantDeltas: false });
  assert.equal(send("assistant", "I'm"), "I'm");
  assert.equal(send("assistant", "I'm running"), "I'm running");
  assert.equal(send("user", " What is", false), "What is");
});

test("text that adds nothing is not sent", () => {
  const { send } = adapter();
  send("assistant", "Hello there");
  assert.equal(send("assistant", "Hello there"), null);
});

test("empty text is forwarded as before and changes nothing", () => {
  const { send } = adapter();
  send("assistant", "Hello");
  assert.equal(send("user", "   "), "");
  assert.equal(send("assistant", "Hello there"), " there"); // bubble still open
});

test("user transcripts are trimmed and pass through", () => {
  const { send } = adapter();
  assert.equal(send("user", " What"), "What");
  assert.equal(send("user", " What time is it?", true), "What time is it?");
});

test("a user transcript that opens a user entry commits the assistant bubble", () => {
  const { send } = adapter();
  send("assistant", "This is a");
  send("user", " Wait");
  send("user", " Wait.", true);
  // The view starts a new bubble here, so it must get everything so far.
  assert.equal(send("assistant", "This is a long answer"), "This is a long answer");
  assert.equal(send("assistant", "This is a long answer."), ".");
});

test("a user interim continuing its open entry leaves the assistant bubble open", () => {
  const { send } = adapter();
  send("assistant", "This is a");
  send("user", " Mm"); // opens a user entry: commits the bubble above
  assert.equal(send("assistant", "This is a long answer"), "This is a long answer");
  send("user", " Mm hmm"); // same entry, extended: the view keeps the new bubble open
  assert.equal(send("assistant", "This is a long answer that"), " that");
});

test("a user interim that starts a new turn commits the assistant bubble", () => {
  const { send } = adapter();
  send("user", " Mm"); // entry left open (no final)
  send("assistant", "Right, so");
  send("user", " What about disk?"); // does not extend "Mm": a new user turn
  assert.equal(send("assistant", "Right, so the disk"), "Right, so the disk");
});

test("a resembling user final soon after the bot spoke closes its own entry", () => {
  const { send } = adapter();
  send("user", " Set a timer for ten");
  send("assistant", "Sure");
  // A reworded final within the view's grace window is that entry's final, not a
  // new turn, so the assistant bubble stays open.
  send("user", " Set a timer for 10 minutes.", true);
  assert.equal(send("assistant", "Sure thing"), " thing");
});

test("the same user final after the grace window is a new turn", () => {
  const { send, clock } = adapter();
  send("user", " Set a timer for ten");
  send("assistant", "Sure");
  clock.t += 5000;
  send("user", " Set a timer for 10 minutes.", true);
  assert.equal(send("assistant", "Sure thing"), "Sure thing");
});

test("any assistant final closes the bubble, including a tool card", () => {
  const { send } = adapter();
  send("assistant", "Let me check.");
  assert.equal(send("assistant", "> 🔧 **web_search**", true), "> 🔧 **web_search**");
  assert.equal(send("assistant", "Let me check. The"), "Let me check. The");
});

test("text restarted under an open bubble: one paragraph break, then hold for the final", () => {
  const { send } = adapter();
  send("assistant", "First answer that");
  // A barge-in no user transcript committed: the brain starts a new caption.
  assert.equal(send("assistant", "Second"), "\n\n");
  assert.equal(send("assistant", "Second answer."), null);
  assert.equal(send("assistant", "Second answer.", true), "Second answer.");
  assert.equal(send("assistant", "Next"), "Next"); // the final closed it
});

test("the bridge forwards the adapted text and skips what adds nothing", () => {
  const calls = [];
  const provider = buildTeaportRealtimeProvider({ url: "ws://brain/talk", hostVersion: "2026.9.1" });
  const bridge = provider.createBridge({
    providerConfig: {},
    onTranscript: (role, text, final) => calls.push([role, text, final]),
  });
  const msg = (role, text, final = false) =>
    bridge._onMessage(JSON.stringify({ type: "transcript", role, text, final }));
  msg("user", " Hi", true);
  msg("assistant", "Hello");
  msg("assistant", "Hello");
  msg("assistant", "Hello there.");
  msg("assistant", "Hello there.", true);
  assert.deepEqual(calls, [
    ["user", "Hi", true],
    ["assistant", "Hello", false],
    ["assistant", " there.", false],
    ["assistant", "Hello there.", true],
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
    bridge._onMessage(JSON.stringify({ type: "transcript", role: "assistant", text: t, final: false }));
  }
  assert.deepEqual(calls, ["Hello", "Hello there."]);
});
