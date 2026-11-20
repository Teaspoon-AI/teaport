// The teaport docs hint (docs-hint.js): a static system-prompt paragraph, sent only where the
// installer put the docs in the memory wiki and the wiki is still there for the agent.
//
//   node --test test/docs-hint.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";

import { DOCS_HINT, docsHintHandler, docsInWiki, wikiEnabled } from "../docs-hint.js";

const WIKI_ON = {
  plugins: {
    entries: { "memory-wiki": { enabled: true }, "teaport-realtime": { config: { docsInWiki: true } } },
  },
};
const withPlugins = (extra, entries = {}) => ({
  plugins: { ...WIKI_ON.plugins, ...extra, entries: { ...WIKI_ON.plugins.entries, ...entries } },
});

test("the hint names where the docs are and how to search them", () => {
  // The page titles docs-wiki-sync writes, and both ways to reach the wiki corpus.
  assert.match(DOCS_HINT, /"teaport docs: <page> — <section>"/);
  assert.match(DOCS_HINT, /wiki_search/);
  assert.match(DOCS_HINT, /memory_search with corpus=wiki/);
});

test("the hint is appended to the system prompt where the installer put the docs", () => {
  const handler = docsHintHandler({ config: () => WIKI_ON });
  // appendSystemContext (cacheable), never per-turn context.
  assert.deepEqual(handler(), { appendSystemContext: DOCS_HINT });
});

test("no hint without the wiki, or without a config", () => {
  for (const cfg of [{}, undefined, { plugins: { entries: { "memory-wiki": { enabled: false } } } },
    { plugins: { entries: { "memory-wiki": {} } } }]) {
    assert.equal(wikiEnabled(cfg), false, JSON.stringify(cfg));
    assert.equal(docsHintHandler({ config: () => cfg })(), undefined);
  }
  assert.equal(docsHintHandler({})(), undefined);
  const thrown = docsHintHandler({ config: () => { throw new Error("runtime not built"); } });
  assert.equal(thrown(), undefined);
});

test("the config is read on every turn: turning the wiki off drops the hint", () => {
  let cfg = WIKI_ON;
  const handler = docsHintHandler({ config: () => cfg });
  assert.ok(handler());
  cfg = {};
  assert.equal(handler(), undefined);
});

test("no hint where the docs are not, or the agent cannot reach the wiki", () => {
  const cases = {
    "installer did not say": withPlugins({}, { "teaport-realtime": {} }),
    "installer could not": withPlugins({}, { "teaport-realtime": { config: { docsInWiki: false } } }),
    "one vault per agent": withPlugins({}, { "memory-wiki": { enabled: true, config: { vault: { scope: "agent" } } } }),
    "plugins off": withPlugins({ enabled: false }),
    "wiki denied": withPlugins({ deny: ["memory-wiki"] }),
    "not in the allow list": withPlugins({ allow: ["teaport-realtime"] }),
  };
  for (const [name, cfg] of Object.entries(cases)) {
    assert.equal(docsInWiki(cfg), false, name);
    assert.equal(docsHintHandler({ config: () => cfg })(), undefined, name);
  }
  // A shared vault said outright, and an allow list that has it, are fine.
  assert.equal(docsInWiki(withPlugins({ allow: ["memory-wiki", "teaport-realtime"] },
    { "memory-wiki": { enabled: true, config: { vault: { scope: "global" } } } })), true);
});
