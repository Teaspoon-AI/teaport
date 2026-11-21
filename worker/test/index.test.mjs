// The download gate against fakes of its bindings: R2 buckets in memory, and D1 as a real
// SQLite database (node:sqlite, Node 22.13+) built from migrations/, so the schema and
// every query run as written.
//
//   node --test worker/test/*.test.mjs   (from the repo root)
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import worker, { expire, makeToken, parseRange, resetCache, TOKEN_TTL_SECS } from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const SHA = "33334d85d3f087da738131fcfe5ee6f862859c8130c4bbdd0aad441aff34effb";
const ENGINE = "dl/engine/teaport-engine-1.6-l4t38-cu13";
const BYTES = new TextEncoder().encode("0123456789abcdefghij");   // 20 bytes

function bucket(objects) {
  const meta = (key, o) => ({
    key, size: o.bytes.length, httpEtag: `"etag-${key}"`,
    writeHttpMetadata(h) { h.set("content-type", o.type ?? "application/octet-stream"); },
  });
  return {
    objects,
    async head(key) { const o = objects.get(key); return o ? meta(key, o) : null; },
    async get(key, opts) {
      const o = objects.get(key);
      if (!o) return null;
      const r = opts?.range;
      const bytes = r ? o.bytes.slice(r.offset, r.offset + r.length) : o.bytes;
      return { ...meta(key, o), range: r, body: bytes,
               async json() { return JSON.parse(new TextDecoder().decode(o.bytes)); } };
    },
  };
}

function d1() {
  const db = new DatabaseSync(":memory:");
  for (const f of readdirSync(MIGRATIONS).filter((n) => n.endsWith(".sql")).sort()) {
    db.exec(readFileSync(new URL(f, MIGRATIONS), "utf8"));
  }
  return {
    db,
    prepare(sql) {
      let args = [];
      const stmt = {
        bind(...a) { args = a; return stmt; },
        async run() { db.prepare(sql).run(...args); return { success: true }; },
        async first() { return db.prepare(sql).get(...args) ?? null; },
      };
      return stmt;
    },
  };
}

function manifest(version = "1.0", sha = SHA) {
  return { bytes: new TextEncoder().encode(JSON.stringify({ engine: { eula: { version, sha256: sha } } })) };
}

let env, waits;
const ctx = { waitUntil(p) { waits.push(p); } };

beforeEach(() => {
  resetCache();
  waits = [];
  env = {
    TOKEN_KEY: "test-token-key-0123456789abcdef",
    CHECK_TOKEN: "ci-check-token-0123456789",
    DIST: bucket(new Map([[ENGINE, { bytes: BYTES }]])),
    PUBLIC: bucket(new Map([["manifest/stable.json", manifest()], ["manifest/beta.json", manifest("1.1", "b".repeat(64))]])),
    DB: d1(),
  };
});

async function call(method, path, { body, headers = {} } = {}) {
  const init = { method, headers: { "cf-connecting-ip": "203.0.113.7", ...headers } };
  if (body !== undefined) init.body = typeof body === "string" ? body : JSON.stringify(body);
  const res = await worker.fetch(new Request(`https://get.teaspoon.tech${path}`, init), env, ctx);
  await Promise.all(waits.splice(0));
  return res;
}

const ACCEPT = { eula_version: "1.0", eula_sha256: SHA, name: "Ada Lovelace", email: "ada@example.com",
                 use: "personal", device_id: "ab".repeat(16), method: "tty", installer: "git:0123456789ab" };

async function accepted(extra = {}) {
  const res = await call("POST", "/api/accept", { body: { ...ACCEPT, ...extra } });
  assert.equal(res.status, 200, await res.clone().text());
  return res.json();
}

const bearer = (t) => ({ authorization: `Bearer ${t}` });

// --- /api/accept ---------------------------------------------------------------------------

test("an acceptance is recorded with who, what and from where, and returns a token", async () => {
  const r = await accepted();
  assert.match(r.acceptance_id, /^[A-Za-z0-9_-]{24}$/);
  assert.match(r.token, /^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
  const row = env.DB.db.prepare("SELECT * FROM acceptances").get();
  assert.equal(row.id, r.acceptance_id);
  assert.deepEqual([row.channel, row.eula_version, row.eula_sha256, row.name, row.email, row.use_type,
                    row.license_id, row.device_id, row.method, row.installer, row.ip],
                   ["stable", "1.0", SHA, "Ada Lovelace", "ada@example.com", "personal",
                    null, "ab".repeat(16), "tty", "git:0123456789ab", "203.0.113.7"]);
  assert.ok(Math.abs(Date.parse(r.expires_at) - (Date.now() + TOKEN_TTL_SECS * 1000)) < 5000);
});

test("only the current license can be accepted, and the refusal names it", async () => {
  for (const wrong of [{ eula_version: "0.9" }, { eula_sha256: "c".repeat(64) }, { channel: "beta" }]) {
    const res = await call("POST", "/api/accept", { body: { ...ACCEPT, ...wrong } });
    assert.equal(res.status, 409);
    const b = await res.json();
    assert.equal(b.error, "eula_changed");
    assert.ok(b.eula_version === "1.0" || b.eula_version === "1.1", b);
  }
  assert.equal(env.DB.db.prepare("SELECT count(*) AS n FROM acceptances").get().n, 0);
});

test("licensed use needs a license id; personal use records none", async () => {
  let res = await call("POST", "/api/accept", { body: { ...ACCEPT, use: "licensed" } });
  assert.equal(res.status, 400);
  assert.match((await res.json()).message, /license_id/);
  await accepted({ use: "licensed", license_id: "TS-2026-0042" });
  await accepted({ license_id: "TS-ignored" });
  const rows = env.DB.db.prepare("SELECT use_type, license_id FROM acceptances ORDER BY created_at, rowid").all();
  assert.deepEqual(rows.map((r) => [r.use_type, r.license_id]), [["licensed", "TS-2026-0042"], ["personal", null]]);
});

test("each field is checked", async () => {
  for (const [field, value] of [["name", ""], ["name", "   "], ["name", "x".repeat(201)], ["name", "a\nb"],
                                ["email", "not-an-email"], ["email", 42], ["use", "business"],
                                ["method", "env"], ["device_id", "XYZ"], ["channel", "nightly"],
                                ["installer", undefined], ["installer", ""], ["installer", "git: x"],
                                ["installer", "x".repeat(65)]]) {
    const res = await call("POST", "/api/accept", { body: { ...ACCEPT, [field]: value } });
    assert.equal(res.status, 400, `${field}=${JSON.stringify(value)}`);
  }
  for (const raw of ["not json", "[]", "null", JSON.stringify({ ...ACCEPT, pad: "x".repeat(5000) })]) {
    assert.equal((await call("POST", "/api/accept", { body: raw })).status, 400, raw.slice(0, 20));
  }
  assert.equal((await call("GET", "/api/accept")).status, 405);
  assert.equal(env.DB.db.prepare("SELECT count(*) AS n FROM acceptances").get().n, 0);
});

test("a channel whose manifest has no released license accepts nothing", async () => {
  env.PUBLIC.objects.set("manifest/stable.json", manifest("1.0", "TODO"));
  const res = await call("POST", "/api/accept", { body: ACCEPT });
  assert.equal(res.status, 503);
});

test("the rate limit refuses before anything is read or written", async () => {
  env.ACCEPT_LIMIT = { async limit({ key }) { assert.equal(key, "203.0.113.7"); return { success: false }; } };
  assert.equal((await call("POST", "/api/accept", { body: ACCEPT })).status, 429);
  assert.equal(env.DB.db.prepare("SELECT count(*) AS n FROM acceptances").get().n, 0);
});

// --- /api/token ----------------------------------------------------------------------------

test("a recorded acceptance gets a new token while its license is current", async () => {
  const { acceptance_id } = await accepted();
  const res = await call("POST", "/api/token", { body: { acceptance_id } });
  assert.equal(res.status, 200);
  const { token } = await res.json();
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(token) })).status, 200);
});

test("a changed license, an unknown or a revoked acceptance gets no token", async () => {
  const { acceptance_id } = await accepted();
  assert.equal((await call("POST", "/api/token", { body: { acceptance_id: "nosuchacceptance" } })).status, 404);
  assert.equal((await call("POST", "/api/token", { body: { acceptance_id: "bad id!" } })).status, 400);
  env.PUBLIC.objects.set("manifest/stable.json", manifest("2.0", "d".repeat(64)));
  resetCache();
  const res = await call("POST", "/api/token", { body: { acceptance_id } });
  assert.equal(res.status, 409);
  assert.deepEqual(await res.json(), { error: "eula_changed", message: "the license changed to version 2.0: read and accept it",
                                       eula_version: "2.0", eula_sha256: "d".repeat(64) });
  env.DB.db.prepare("UPDATE acceptances SET revoked_at = 'now'").run();
  assert.equal((await call("POST", "/api/token", { body: { acceptance_id } })).status, 403);
});

// --- /dl -----------------------------------------------------------------------------------

test("no token, no file: the refusal says how to get one", async () => {
  const res = await call("GET", `/${ENGINE}`);
  assert.equal(res.status, 401);
  assert.equal(res.headers.get("www-authenticate"), "Bearer");
  assert.match((await res.json()).message, /get\.teaspoon\.tech\/teaport/);
  for (const h of [bearer("v1.e30.AAAA"), bearer("garbage"), { authorization: "Basic abc" }]) {
    assert.equal((await call("GET", `/${ENGINE}`, { headers: h })).status, 401);
  }
});

test("a token downloads the file whole, and the download is logged", async () => {
  const { acceptance_id, token } = await accepted();
  const res = await call("GET", `/${ENGINE}`, { headers: bearer(token) });
  assert.equal(res.status, 200);
  assert.deepEqual(new Uint8Array(await res.arrayBuffer()), BYTES);
  assert.equal(res.headers.get("content-length"), "20");
  assert.equal(res.headers.get("accept-ranges"), "bytes");
  assert.equal(res.headers.get("cache-control"), "private, no-store");
  const log = env.DB.db.prepare("SELECT acceptance_id, key, range_start, status FROM downloads").all();
  assert.deepEqual(log.map((r) => ({ ...r })), [{ acceptance_id, key: ENGINE, range_start: null, status: 200 }]);
});

test("curl -C - resumes: a range is served as 206, a range past the end as 416", async () => {
  const { token } = await accepted();
  let res = await call("GET", `/${ENGINE}`, { headers: { ...bearer(token), range: "bytes=15-" } });
  assert.equal(res.status, 206);
  assert.equal(res.headers.get("content-range"), "bytes 15-19/20");
  assert.equal(res.headers.get("content-length"), "5");
  assert.equal(new TextDecoder().decode(await res.arrayBuffer()), "fghij");
  res = await call("GET", `/${ENGINE}`, { headers: { ...bearer(token), range: "bytes=20-" } });
  assert.equal(res.status, 416);
  assert.equal(res.headers.get("content-range"), "bytes */20");
  res = await call("HEAD", `/${ENGINE}`, { headers: bearer(token) });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("content-length"), "20");
  assert.equal(await res.text(), "");
});

test("an expired token, a forged one, a revoked acceptance or a changed license stop downloads", async () => {
  const { acceptance_id, token } = await accepted();
  const now = Math.floor(Date.now() / 1000);
  const expired = await makeToken(env, { a: acceptance_id, v: "1.0", c: "stable", exp: now - 1 });
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(expired) })).status, 401);
  const forged = await makeToken({ TOKEN_KEY: "another key" }, { a: acceptance_id, v: "1.0", c: "stable", exp: now + 60 });
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(forged) })).status, 401);
  const [, payload, sig] = token.split(".");
  const swapped = JSON.parse(Buffer.from(payload, "base64url").toString());
  swapped.a = "someone-else";
  const tampered = `v1.${Buffer.from(JSON.stringify(swapped)).toString("base64url")}.${sig}`;
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(tampered) })).status, 401);
  env.PUBLIC.objects.set("manifest/stable.json", manifest("2.0", "d".repeat(64)));
  resetCache();
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(token) })).status, 403);
  env.PUBLIC.objects.set("manifest/stable.json", manifest());
  resetCache();
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(token) })).status, 200);
  env.DB.db.prepare("UPDATE acceptances SET revoked_at = 'now'").run();
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(token) })).status, 403);
});

test("the check token downloads, logged as ci; other paths and methods are refused", async () => {
  const res = await call("GET", `/${ENGINE}`, { headers: bearer(env.CHECK_TOKEN) });
  assert.equal(res.status, 200);
  assert.equal(env.DB.db.prepare("SELECT acceptance_id FROM downloads").get().acceptance_id, "ci");
  delete env.CHECK_TOKEN;
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer("ci-check-token-0123456789") })).status, 401);
  const { token } = await accepted();
  assert.equal((await call("GET", "/dl/engine/missing", { headers: bearer(token) })).status, 404);
  assert.equal((await call("GET", "/dl/engine/a/../b", { headers: bearer(token) })).status, 404);
  assert.equal((await call("PUT", `/${ENGINE}`, { headers: bearer(token) })).status, 405);
  assert.equal((await call("GET", "/manifest/stable.json")).status, 404);
});

test("a check token in base64, with + / and =, is taken whole", async () => {
  env.CHECK_TOKEN = "q+Lw/9Zx3v==";
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer("q+Lw/9Zx3v==") })).status, 200);
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer("q+Lw/9Zx3v") })).status, 401);
});

test("GATE open serves a request without a token, marked and logged; a token sent is still checked", async () => {
  env.GATE = "open";
  const res = await call("GET", `/${ENGINE}`);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-teaport-gate"), "open");
  assert.deepEqual(new Uint8Array(await res.arrayBuffer()), BYTES);
  for (const h of [bearer("garbage"), { authorization: "" }, { authorization: "Basic abc" }]) {
    assert.equal((await call("GET", `/${ENGINE}`, { headers: h })).status, 401);
  }
  const { token } = await accepted();
  const ok = await call("GET", `/${ENGINE}`, { headers: bearer(token) });
  assert.equal(ok.status, 200);
  assert.equal(ok.headers.get("x-teaport-gate"), null);
  const log = env.DB.db.prepare("SELECT acceptance_id FROM downloads ORDER BY id").all();
  assert.equal(log[0].acceptance_id, "open");
  assert.notEqual(log.at(-1).acceptance_id, "open");
});

test("any GATE but open, or none, enforces", async () => {
  for (const g of [undefined, "enforce", "OPEN", "1", ""]) {
    env.GATE = g;
    assert.equal((await call("GET", `/${ENGINE}`)).status, 401, String(g));
  }
});

test("the retention cron deletes acceptances and downloads past RETENTION_DAYS, and nothing unset", async () => {
  const { token } = await accepted();
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(token) })).status, 200);
  const count = (table) => env.DB.db.prepare(`SELECT count(*) AS n FROM ${table}`).get().n;
  const now = Math.floor(Date.now() / 1000);
  for (const days of [undefined, "0", "abc"]) {
    env.RETENTION_DAYS = days;
    await expire(env, now + 3650 * 86400);
    assert.deepEqual([count("acceptances"), count("downloads")], [1, 1], String(days));
  }
  env.RETENTION_DAYS = "30";
  await expire(env, now + 29 * 86400);
  assert.deepEqual([count("acceptances"), count("downloads")], [1, 1]);
  await expire(env, now + 31 * 86400);
  assert.deepEqual([count("acceptances"), count("downloads")], [0, 0]);
  const res = await call("POST", "/api/token", { body: { acceptance_id: "x".repeat(24) } });
  assert.equal(res.status, 404);
});

test("parseRange takes one bytes= range and serves anything else whole", () => {
  assert.deepEqual(parseRange("bytes=0-4", 20), { offset: 0, length: 5 });
  assert.deepEqual(parseRange("bytes=5-", 20), { offset: 5, length: 15 });
  assert.deepEqual(parseRange("bytes=-3", 20), { offset: 17, length: 3 });
  assert.deepEqual(parseRange("bytes=10-99", 20), { offset: 10, length: 10 });
  assert.equal(parseRange("bytes=20-", 20), "unsatisfiable");
  assert.equal(parseRange("bytes=-0", 20), "unsatisfiable");
  for (const h of [null, "", "bytes=5-2", "bytes=-", "bytes=0-1,4-5", "items=0-1"]) assert.equal(parseRange(h, 20), null, h);
});
