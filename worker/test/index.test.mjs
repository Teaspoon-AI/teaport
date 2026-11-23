// The download gate against fakes of its bindings: R2 buckets in memory, and D1 as a real
// SQLite database (node:sqlite, Node 22.13+) built from migrations/, so the schema and
// every query run as written.
//
//   node --test worker/test/*.test.mjs   (from the repo root)
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import worker, { MAKER_PER_BOX_DAY, expire, makeToken, parseRange, resetCache, TOKEN_TTL_SECS } from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const SHA = "33334d85d3f087da738131fcfe5ee6f862859c8130c4bbdd0aad441aff34effb";
const ENGINE = "dl/engine/teaport-engine-1.6-l4t38-cu13";
const BYTES = new TextEncoder().encode("0123456789abcdefghij");   // 20 bytes

function bucket(objects) {
  const meta = (key, o) => ({
    key, size: o.bytes.length, httpEtag: `"etag-${key}"`,
    writeHttpMetadata(h) {   // as R2's: the object's stored HTTP metadata
      h.set("content-type", o.type ?? "application/octet-stream");
      if (o.cacheControl) h.set("cache-control", o.cacheControl);
    },
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
    TOKEN_KEY: "test-token-key-0123456789abcdef-0123",   // 32+, as FACTORY_TOKEN must be: the
    CHECK_TOKEN: "ci-check-token-0123456789-abcdefgh",   // equality checks are the ones tested
    FACTORY_TOKEN: "factory-station-token-0123456789",
    MAKER_MAX_DAYS: "14",
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

const ACCEPT = { eula_version: "1.0", eula_sha256: SHA, email: "ada@example.com",
                 use: "personal", device_id: "ab".repeat(16), method: "tty", installer: "git:0123456789ab" };

async function accepted(extra = {}) {
  const res = await call("POST", "/api/accept", { body: { ...ACCEPT, ...extra } });
  assert.equal(res.status, 200, await res.clone().text());
  return res.json();
}

const bearer = (t) => ({ authorization: `Bearer ${t}` });

// --- /api/accept ---------------------------------------------------------------------------

test("no name is recorded: an installer that still sends one is accepted, and the name dropped", async () => {
  const r = await accepted({ name: "Ada Lovelace" });
  const row = env.DB.db.prepare("SELECT * FROM acceptances WHERE id = ?").get(r.acceptance_id);
  assert.equal(row.name, "", "the column stays (rollbacks) and is written empty");
  assert.ok(!Object.values(row).includes("Ada Lovelace"));
});

test("migration 0002 blanks the names recorded before it and leaves the schema the old Worker needs", () => {
  const db = new DatabaseSync(":memory:");
  const files = readdirSync(MIGRATIONS).filter((f) => f.endsWith(".sql")).sort();
  db.exec(readFileSync(new URL(files[0], MIGRATIONS), "utf8"));
  db.prepare(`INSERT INTO acceptances (id, created_at, channel, eula_version, eula_sha256, name, email,
                use_type, method, installer) VALUES ('a', 't', 'stable', '1.0', 'x', 'Ada Lovelace',
                'ada@example.com', 'personal', 'tty', 'unknown')`).run();
  db.exec(readFileSync(new URL("0002_drop_acceptance_name.sql", MIGRATIONS), "utf8"));
  assert.equal(db.prepare("SELECT name FROM acceptances").get().name, "");
  // The Worker before 0002 still inserts a name: a rollback to it must keep working.
  db.prepare(`INSERT INTO acceptances (id, created_at, channel, eula_version, eula_sha256, name, email,
                use_type, method, installer) VALUES ('b', 't', 'stable', '1.0', 'x', 'Grace Hopper',
                'grace@example.com', 'personal', 'tty', 'unknown')`).run();
});

test("an acceptance is recorded with who, what and from where, and returns a token", async () => {
  const r = await accepted();
  assert.match(r.acceptance_id, /^[A-Za-z0-9_-]{24}$/);
  assert.match(r.token, /^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
  const row = env.DB.db.prepare("SELECT * FROM acceptances").get();
  assert.equal(row.id, r.acceptance_id);
  assert.deepEqual([row.channel, row.eula_version, row.eula_sha256, row.email, row.use_type,
                    row.license_id, row.device_id, row.method, row.installer, row.ip],
                   ["stable", "1.0", SHA, "ada@example.com", "personal",
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
  for (const [field, value] of [["email", "not-an-email"], ["email", 42], ["use", "business"],
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

// A request body from <chunks> (strings), streamed without a Content-Length unless one is
// given; pulls counts how many chunks the Worker read.
function streamed(chunks, headers = {}) {
  const state = { pulls: 0 };
  let i = 0;
  const stream = new ReadableStream({
    pull(c) {
      state.pulls++;
      if (i < chunks.length) c.enqueue(new TextEncoder().encode(chunks[i++]));
      else c.close();
    },
  }, { highWaterMark: 0 });
  const request = new Request("https://get.teaspoon.tech/api/accept", {
    method: "POST", body: stream, duplex: "half",
    headers: { "cf-connecting-ip": "203.0.113.7", "content-type": "application/json", ...headers },
  });
  return { request, state };
}

test("a body over 4 KB is refused without being read whole, with a Content-Length or without", async () => {
  const count = () => env.DB.db.prepare("SELECT count(*) AS n FROM acceptances").get().n;
  // No Content-Length (chunked): reading stops at the cap, not at the end of the body.
  const big = streamed(Array(1000).fill("x".repeat(1024)));
  assert.equal(big.request.headers.get("content-length"), null);
  let res = await worker.fetch(big.request, env, ctx);
  assert.equal(res.status, 400);
  assert.ok(big.state.pulls <= 6, `read ${big.state.pulls} KB of a 1000 KB body`);
  // A Content-Length over the cap: refused before a byte is read.
  const declared = streamed([JSON.stringify(ACCEPT)], { "content-length": "5000" });
  res = await worker.fetch(declared.request, env, ctx);
  assert.equal(res.status, 400);
  assert.equal(declared.state.pulls, 0);
  assert.equal(count(), 0);
  // A body under the cap, chunked without a Content-Length, is read whole.
  const text = JSON.stringify(ACCEPT);
  const small = streamed([text.slice(0, 10), text.slice(10, 50), text.slice(50)]);
  res = await worker.fetch(small.request, env, ctx);
  assert.equal(res.status, 200, await res.clone().text());
  assert.equal(count(), 1);
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

// --- /api/factory: maker acceptances --------------------------------------------------------

const BOX = "cd".repeat(32);
const FACTORY = { device_id: BOX, installer: "git:0123456789ab" };
const box = (n) => n.toString(16).padStart(2, "0").repeat(16);
const ago = (secs) => new Date(Date.now() - secs * 1000).toISOString();

async function maker(extra = {}, secret = env.FACTORY_TOKEN) {
  const res = await call("POST", "/api/factory", { body: { ...FACTORY, ...extra }, headers: bearer(secret) });
  assert.equal(res.status, 200, await res.clone().text());
  return res.json();
}

async function makerToken(acceptance_id) {
  const res = await call("POST", "/api/token", { body: { acceptance_id } });
  assert.equal(res.status, 200, await res.clone().text());
  return res.json();
}

const askFactory = async (extra = {}, secret = env.FACTORY_TOKEN) =>
  call("POST", "/api/factory", { body: { ...FACTORY, ...extra }, headers: bearer(secret) });

test("the station records a maker acceptance for a box; the box gets tokens with it, and its downloads are logged under it", async () => {
  const { acceptance_id, expires_at } = await maker();
  assert.match(acceptance_id, /^maker-[A-Za-z0-9_-]{24}$/);
  assert.ok(Math.abs(Date.parse(expires_at) - (Date.now() + 14 * 86400 * 1000)) < 5000);
  const row = env.DB.db.prepare("SELECT * FROM maker_acceptances").get();
  assert.deepEqual([row.id, row.device_id, row.installer, row.ip, row.revoked_at],
                   [acceptance_id, BOX, "git:0123456789ab", "203.0.113.7", null]);
  assert.match(row.mark, /^[0-9a-f]{16}$/);
  assert.equal(env.DB.db.prepare("SELECT count(*) AS n FROM acceptances").get().n, 0);   // nobody's acceptance
  const { token } = await makerToken(acceptance_id);
  const claims = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString());
  assert.deepEqual([claims.a, claims.k], [acceptance_id, "maker"]);
  const res = await call("GET", `/${ENGINE}`, { headers: { ...bearer(token), range: "bytes=15-" } });
  assert.equal(res.status, 206);
  assert.equal(new TextDecoder().decode(await res.arrayBuffer()), "fghij");
  const log = env.DB.db.prepare("SELECT acceptance_id, status FROM downloads").all();
  assert.deepEqual(log.map((r) => ({ ...r })), [{ acceptance_id, status: 206 }]);
  assert.ok(env.DB.db.prepare("SELECT last_token_at FROM maker_acceptances").get().last_token_at);
});

test("a maker acceptance is no one's acceptance of a license: a license change does not touch it", async () => {
  const { acceptance_id } = await maker();
  const { token } = await makerToken(acceptance_id);
  env.PUBLIC.objects.set("manifest/stable.json", manifest("2.0", "d".repeat(64)));
  resetCache();
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(token) })).status, 200);
  assert.equal((await call("POST", "/api/token", { body: { acceptance_id } })).status, 200);
});

test("past MAKER_MAX_DAYS a maker acceptance gets no token and downloads nothing: the owner accepts", async () => {
  const { acceptance_id } = await maker();
  const { token } = await makerToken(acceptance_id);
  // A token never outlives its record: one asked for a minute before the end ends with it.
  env.DB.db.prepare("UPDATE maker_acceptances SET created_at = ?").run(ago(14 * 86400 - 60));
  const late = await makerToken(acceptance_id);
  assert.ok(Date.parse(late.expires_at) - Date.now() < 61 * 1000, late.expires_at);
  env.DB.db.prepare("UPDATE maker_acceptances SET created_at = ?").run(ago(14 * 86400 + 1));
  for (const [res, status] of [[await call("POST", "/api/token", { body: { acceptance_id } }), 409],
                               [await call("GET", `/${ENGINE}`, { headers: bearer(token) }), 403]]) {
    assert.equal(res.status, status);   // 409: the installer has the owner accept; 403: refused
    assert.equal((await res.json()).error, "maker_expired");
  }
  // Without MAKER_MAX_DAYS, none gets any: it fails closed, as the server's fault (503:
  // the installer tries again later) rather than the record's.
  env.DB.db.prepare("UPDATE maker_acceptances SET created_at = ?").run(ago(60));
  const fresh = (await makerToken(acceptance_id)).token;
  for (const v of [undefined, "0", "x", "1.5"]) {
    env.MAKER_MAX_DAYS = v;
    for (const res of [await call("POST", "/api/token", { body: { acceptance_id } }),
                       await call("GET", `/${ENGINE}`, { headers: bearer(fresh) })]) {
      assert.equal(res.status, 503, String(v));
      assert.equal((await res.json()).error, "factory_misconfigured");
    }
  }
});

test("a revoked maker acceptance gets no token and downloads nothing, the gate open or not", async () => {
  const { acceptance_id } = await maker();
  const { token } = await makerToken(acceptance_id);
  env.DB.db.prepare("UPDATE maker_acceptances SET revoked_at = 'now'").run();
  assert.equal((await call("POST", "/api/token", { body: { acceptance_id } })).status, 403);
  for (const gate of [undefined, "open"]) {
    env.GATE = gate;
    assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(token) })).status, 403, String(gate));
  }
});

test("only FACTORY_TOKEN records one, and only with a box and an installer", async () => {
  const count = () => env.DB.db.prepare("SELECT count(*) AS n FROM maker_acceptances").get().n;
  const { token } = await accepted();
  for (const headers of [{}, bearer("wrong"), bearer(env.CHECK_TOKEN), bearer(token),
                         { authorization: `Basic ${env.FACTORY_TOKEN}` }]) {
    const res = await call("POST", "/api/factory", { body: FACTORY, headers });
    assert.equal(res.status, 401, JSON.stringify(headers));
    assert.equal((await res.json()).error, "bad_factory_token");
  }
  for (const bad of [{ device_id: undefined }, { device_id: "AB".repeat(16) }, { device_id: "ab".repeat(7) },
                     { device_id: "ab".repeat(65) }, { device_id: 12345678901234567890 }, { installer: undefined },
                     { installer: "has spaces" }]) {
    assert.equal((await askFactory(bad)).status, 400, JSON.stringify(bad));
  }
  assert.equal((await call("POST", "/api/factory", { body: "[]", headers: bearer(env.FACTORY_TOKEN) })).status, 400);
  assert.equal((await call("GET", "/api/factory", { headers: bearer(env.FACTORY_TOKEN) })).status, 405);
  assert.equal(count(), 0);
  delete env.FACTORY_TOKEN;   // unset: nothing records one
  assert.equal((await askFactory({}, "factory-station-token-0123456789")).status, 401);
  assert.equal(count(), 0);
});

test("FACTORY_TOKEN downloads nothing and gets no token itself", async () => {
  for (const gate of [undefined, "open"]) {
    env.GATE = gate;
    const res = await call("GET", `/${ENGINE}`, { headers: bearer(env.FACTORY_TOKEN) });
    assert.equal(res.status, 401, String(gate));
  }
  assert.equal((await call("POST", "/api/token", { body: { acceptance_id: env.FACTORY_TOKEN } })).status, 404);
});

test("a maker token is signed like any other; a maker id is never taken for an acceptance's, nor the reverse", async () => {
  const { acceptance_id } = await maker();
  const { token } = await makerToken(acceptance_id);
  const claims = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString());
  const now = Math.floor(Date.now() / 1000);
  const sig = token.split(".")[2];
  const tampered = `v1.${Buffer.from(JSON.stringify({ ...claims, a: `maker-${"x".repeat(24)}` })).toString("base64url")}.${sig}`;
  for (const t of [await makeToken(env, { ...claims, exp: now - 1 }), await makeToken({ TOKEN_KEY: "another key" }, claims), tampered]) {
    assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(t) })).status, 401);
  }
  // "maker" on an acceptance's id, or a maker id without it, finds nothing.
  const { acceptance_id: person } = await accepted();
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(await makeToken(env, { a: person, k: "maker", exp: now + 60 })) })).status, 401);
  const plain = await makeToken(env, { a: acceptance_id, v: "1.0", s: SHA, c: "stable", exp: now + 60 });
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(plain) })).status, 403);
  assert.equal((await call("POST", "/api/token", { body: { acceptance_id: `maker-${"y".repeat(24)}` } })).status, 404);
});

test("rotating FACTORY_TOKEN stops new records, not the boxes already provisioned", async () => {
  const { acceptance_id } = await maker();
  const old = env.FACTORY_TOKEN;
  env.FACTORY_TOKEN = "the-rotated-factory-station-token-0";
  assert.equal((await askFactory({}, old)).status, 401);
  const { token } = await makerToken(acceptance_id);
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(token) })).status, 200);
  await maker({ device_id: box(2) });
});

test("the mark is keyed with TOKEN_KEY: nothing in a row tests guesses at FACTORY_TOKEN", async () => {
  await maker();
  const mark = () => env.DB.db.prepare("SELECT mark FROM maker_acceptances ORDER BY rowid DESC").get().mark;
  const f = mark();
  const plain = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`teaport-factory:${env.FACTORY_TOKEN}`));
  assert.notEqual(f, Buffer.from(plain).subarray(0, 8).toString("hex"));
  env.TOKEN_KEY = "another-token-key-0123456789abcdef";
  await maker({ device_id: box(2) });
  assert.notEqual(mark(), f);
});

test("the cap counts boxes, not records: a box provisioned again and again does not use it up", async () => {
  env.FACTORY_DAILY_MAX = "2";
  for (let i = 0; i < 5; i++) await maker({ device_id: box(1) });
  await maker({ device_id: box(2) });
  const res = await askFactory({ device_id: box(3) });
  assert.equal(res.status, 429);
  assert.equal((await res.json()).error, "factory_cap");
  await maker({ device_id: box(2) });   // still a box it has seen today
  for (const v of [undefined, "0"]) {
    env.FACTORY_DAILY_MAX = v;
    await maker({ device_id: box(9) });
  }
});

test("a box gets at most MAKER_PER_BOX_DAY records in 24 hours, whatever the cap", async () => {
  for (const cap of [undefined, "200"]) {
    env.DB.db.exec("DELETE FROM maker_acceptances");
    env.FACTORY_DAILY_MAX = cap;
    for (let i = 0; i < MAKER_PER_BOX_DAY; i++) await maker({ device_id: box(1) });
    assert.equal((await askFactory({ device_id: box(1) })).status, 429, String(cap));
    await maker({ device_id: box(2) });                        // another box is not held up
  }
  env.DB.db.prepare("UPDATE maker_acceptances SET created_at = ?").run(ago(25 * 3600));
  await maker({ device_id: box(1) });                          // a day later, it may again
  env.DB.db.exec("DELETE FROM maker_acceptances");
  for (let i = 0; i < MAKER_PER_BOX_DAY; i++) await maker({ device_id: box(1) });
  env.FACTORY_TOKEN = "the-rotated-factory-station-token-0";  // a new secret starts again
  await maker({ device_id: box(1) });
});

test("the cap's window is 24 hours, for the count and for a box seen before; rotating starts the count again", async () => {
  env.FACTORY_DAILY_MAX = "1";
  await maker({ device_id: box(1) });
  env.DB.db.prepare("UPDATE maker_acceptances SET created_at = ?").run(ago(23 * 3600));
  assert.equal((await askFactory({ device_id: box(2) })).status, 429);   // 23 hours ago still counts
  env.DB.db.prepare("UPDATE maker_acceptances SET created_at = ?").run(ago(25 * 3600));
  assert.equal((await askFactory({ device_id: box(2) })).status, 200);   // 25 hours ago does not
  assert.equal((await askFactory({ device_id: box(1) })).status, 429);   // nor does it make box 1 seen today
  env.DB.db.exec("DELETE FROM maker_acceptances");
  await maker({ device_id: box(1) });   // a leak used the cap up...
  env.FACTORY_TOKEN = "the-rotated-factory-station-token-0";
  assert.equal((await askFactory({ device_id: box(1) })).status, 200);   // ...the new secret starts again
  assert.equal((await askFactory({ device_id: box(2) })).status, 429);
  // A box seen under the old secret only is a new box to the new one.
  env.DB.db.exec("DELETE FROM maker_acceptances");
  env.FACTORY_TOKEN = "factory-station-token-0123456789";
  await maker({ device_id: box(1) });
  env.FACTORY_TOKEN = "the-rotated-factory-station-token-0";
  await maker({ device_id: box(2) });
  assert.equal((await askFactory({ device_id: box(1) })).status, 429);
});

test("set up wrong, the factory records nothing", async () => {
  for (const [k, v] of [["FACTORY_DAILY_MAX", "two hundred"], ["FACTORY_DAILY_MAX", "-1"], ["FACTORY_DAILY_MAX", "Infinity"],
                        ["FACTORY_DAILY_MAX", "1.5"], ["MAKER_MAX_DAYS", undefined], ["MAKER_MAX_DAYS", "0"],
                        ["MAKER_MAX_DAYS", "fourteen"], ["FACTORY_TOKEN", env.CHECK_TOKEN],
                        ["FACTORY_TOKEN", env.TOKEN_KEY], ["FACTORY_TOKEN", "short-but-sent-right"]]) {
    const saved = env[k];
    env[k] = v;
    const res = await askFactory();
    assert.equal(res.status, 503, `${k}=${v}`);
    assert.equal((await res.json()).error, "factory_misconfigured");
    env[k] = saved;
  }
  assert.equal(env.DB.db.prepare("SELECT count(*) AS n FROM maker_acceptances").get().n, 0);
});

test("the rate limit comes before the factory secret is checked", async () => {
  env.ACCEPT_LIMIT = { async limit() { return { success: false }; } };
  for (const secret of ["wrong", env.FACTORY_TOKEN]) assert.equal((await askFactory({}, secret)).status, 429, secret);
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

test("an object stored with its own Cache-Control is still served private and uncached", async () => {
  env.DIST.objects.set(ENGINE, { bytes: BYTES, type: "application/x-engine", cacheControl: "public, max-age=31536000" });
  const { token } = await accepted();
  for (const [method, headers, gate] of [["GET", bearer(token)], ["HEAD", bearer(token)],
                                         ["GET", { range: "bytes=0-3", ...bearer(token) }], ["GET", {}, "open"]]) {
    env.GATE = gate;
    const res = await call(method, `/${ENGINE}`, { headers });
    assert.ok(res.status === 200 || res.status === 206, `${method} ${res.status}`);
    assert.equal(res.headers.get("cache-control"), "private, no-store", method);
    assert.equal(res.headers.get("etag"), `"etag-${ENGINE}"`);
    assert.equal(res.headers.get("content-type"), "application/x-engine");   // its metadata is kept
  }
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

test("a license text changed under the same version stops the token and the download, not the check token", async () => {
  const { acceptance_id, token } = await accepted();
  const claims = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString());
  assert.equal(claims.s, SHA);   // the token names the exact text accepted
  env.PUBLIC.objects.set("manifest/stable.json", manifest("1.0", "e".repeat(64)));
  resetCache();
  const dl = await call("GET", `/${ENGINE}`, { headers: bearer(token) });
  assert.equal(dl.status, 403);
  assert.equal((await dl.json()).error, "eula_changed");
  const res = await call("POST", "/api/token", { body: { acceptance_id } });
  assert.equal(res.status, 409);
  assert.deepEqual(await res.json(), { error: "eula_changed", message: "the license changed to version 1.0: read and accept it",
                                       eula_version: "1.0", eula_sha256: "e".repeat(64) });
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(env.CHECK_TOKEN) })).status, 200);
  // The text back as accepted: the same token downloads again.
  env.PUBLIC.objects.set("manifest/stable.json", manifest());
  resetCache();
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(token) })).status, 200);
  // A token without the sha256 (the claims before it) is refused.
  const now = Math.floor(Date.now() / 1000);
  const old = await makeToken(env, { a: acceptance_id, v: "1.0", c: "stable", exp: now + 60 });
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(old) })).status, 403);
});

test("a HEAD writes no download row, a range past the end included; a GET does", async () => {
  const { token } = await accepted();
  const rows = () => env.DB.db.prepare("SELECT status FROM downloads ORDER BY id").all().map((r) => r.status);
  for (const range of [undefined, "bytes=5-", "bytes=20-"]) {
    const res = await call("HEAD", `/${ENGINE}`, { headers: { ...bearer(token), ...(range ? { range } : {}) } });
    assert.equal(res.status, range === "bytes=20-" ? 416 : range ? 206 : 200, String(range));
  }
  env.GATE = "open";
  assert.equal((await call("HEAD", `/${ENGINE}`, { headers: { range: "bytes=20-" } })).status, 416);
  assert.deepEqual(rows(), []);
  assert.equal((await call("GET", `/${ENGINE}`, { headers: { ...bearer(token), range: "bytes=20-" } })).status, 416);
  assert.deepEqual(rows(), [416]);
});

test("the check token downloads, logged as ci; other paths and methods are refused", async () => {
  const res = await call("GET", `/${ENGINE}`, { headers: bearer(env.CHECK_TOKEN) });
  assert.equal(res.status, 200);
  assert.equal(env.DB.db.prepare("SELECT acceptance_id FROM downloads").get().acceptance_id, "ci");
  delete env.CHECK_TOKEN;
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer("ci-check-token-0123456789-abcdefgh") })).status, 401);
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
  const part = await call("GET", `/${ENGINE}`, { headers: { range: "bytes=5-9" } });
  assert.equal(part.status, 206);
  assert.equal(part.headers.get("x-teaport-gate"), "open");
  const head = await call("HEAD", `/${ENGINE}`);
  assert.equal(head.status, 200);
  assert.equal(head.headers.get("x-teaport-gate"), "open");
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
  const { acceptance_id, token } = await accepted();
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
  // Recent downloads: the old acceptance's go with it, the CI check's stay.
  const recent = new Date((now + 30 * 86400) * 1000).toISOString();
  for (const who of [acceptance_id, "ci"]) {
    env.DB.db.prepare("INSERT INTO downloads (at, acceptance_id, key, status) VALUES (?, ?, 'k', 200)").run(recent, who);
  }
  await expire(env, now + 31 * 86400);
  assert.deepEqual([count("acceptances"), count("downloads")], [0, 1]);
  assert.equal(env.DB.db.prepare("SELECT acceptance_id FROM downloads").get().acceptance_id, "ci");
  const res = await call("POST", "/api/token", { body: { acceptance_id } });
  assert.equal(res.status, 404);
  // A token still in date stops at the deleted acceptance, which it does not call revoked.
  const gone = await call("GET", `/${ENGINE}`, { headers: bearer(token) });
  assert.equal(gone.status, 403);
  assert.equal((await gone.json()).error, "unknown_acceptance");
});

test("the retention cron deletes maker acceptances and their downloads past RETENTION_DAYS", async () => {
  const { acceptance_id } = await maker();
  const { token } = await makerToken(acceptance_id);
  assert.equal((await call("GET", `/${ENGINE}`, { headers: bearer(token) })).status, 200);
  const count = (table) => env.DB.db.prepare(`SELECT count(*) AS n FROM ${table}`).get().n;
  const now = Math.floor(Date.now() / 1000);
  env.RETENTION_DAYS = "30";
  await expire(env, now + 29 * 86400);
  assert.deepEqual([count("maker_acceptances"), count("downloads")], [1, 1]);
  // A recent download goes with its old record.
  env.DB.db.prepare("INSERT INTO downloads (at, acceptance_id, key, status) VALUES (?, ?, 'k', 200)")
    .run(new Date((now + 30 * 86400) * 1000).toISOString(), acceptance_id);
  await expire(env, now + 31 * 86400);
  assert.deepEqual([count("maker_acceptances"), count("downloads")], [0, 0]);
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
