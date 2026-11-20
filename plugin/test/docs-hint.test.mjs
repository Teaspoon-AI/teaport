// The teaport docs hint (docs-hint.js): a static system-prompt paragraph, sent only while the
// memory wiki that holds the docs is enabled.
//
//   node --test test/docs-hint.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";

import { DOCS_HINT, docsHintHandler, wikiEnabled } from "../docs-hint.js";

const WIKI_ON = { plugins: { entries: { "memory-wiki": { enabled: true } } } };

test("the hint names where the docs are and how to search them", () => {
  // The page titles docs-wiki-sync writes, and both ways to reach the wiki corpus.
  assert.match(DOCS_HINT, /"teaport docs: <page> — <section>"/);
  assert.match(DOCS_HINT, /wiki_search/);
  assert.match(DOCS_HINT, /memory_search with corpus=wiki/);
});

test("the hint is appended to the system prompt while the wiki is enabled", () => {
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
