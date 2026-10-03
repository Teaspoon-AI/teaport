// Barge-in is the brain's: it stops for speech it heard and sends {"type":"clear"}.
// OpenClaw's own barge-in (the Control UI cancelling the output when its microphone is
// loud) must not end the session. See handleBargeIn and capabilities in provider.js.
//
//   node --test test/bargein.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";

import { buildTeaportRealtimeProvider } from "../provider.js";

function bridgeWithEvents() {
  const events = [];
  const provider = buildTeaportRealtimeProvider({ url: "ws://brain/talk" });
  const bridge = provider.createBridge({ providerConfig: {}, onEvent: (e) => events.push(e) });
  return { provider, bridge, events };
}

test("the provider leaves barge-in to the brain", () => {
  const { provider } = bridgeWithEvents();
  // OpenClaw 2026.9.6's Control UI reads this from talk.catalog and then never cancels.
  assert.equal(provider.capabilities.supportsBargeIn, false);
});

test("a relay cancel is confirmed at once, so the session survives it", () => {
  // OpenClaw 2026.9.1 cancels on the Control UI's loudness check, then closes the
  // session unless the provider confirms the cancelled response within 1 s.
  const { bridge, events } = bridgeWithEvents();
  bridge.handleBargeIn({ audioPlaybackActive: true });
  assert.deepEqual(events, [{ direction: "server", type: "response.cancelled" }]);
});

test("a forced consult's flush is not a cancel", () => {
  // force:true flushes output for a forced consult; a cancellation there would end the
  // live talk turn.
  const { bridge, events } = bridgeWithEvents();
  bridge.handleBargeIn({ audioPlaybackActive: true, force: true });
  assert.deepEqual(events, []);
});

test("without onEvent, a cancel is still harmless", () => {
  const provider = buildTeaportRealtimeProvider({ url: "ws://brain/talk" });
  const bridge = provider.createBridge({ providerConfig: {} });
  bridge.handleBargeIn();
  bridge.handleBargeIn({ audioPlaybackActive: true });
});
