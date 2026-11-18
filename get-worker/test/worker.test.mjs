// node --test get-worker/test/*.test.mjs  (Node 20+; no dependencies)
//
// The Worker against in-memory fakes of its R2 bucket and D1 database. wrangler dev runs
// it against Miniflare's for the end-to-end check (docs/engine-license-gate.md).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import worker, { isGated, readToken } from "../src/index.js";

const EULA = "Teaport Engine License v1.0\nthe terms\n";
const EULA_SHA = createHash("sha256").update(EULA).digest("hex");
const ENGINE = new Uint8Array(1000).map((_, i) => i % 251);

class FakeBucket {
  constructor(objs) { this.objs = new Map(Object.entries(objs).map(([k, v]) => [k, typeof v === "string" ? new TextEncoder().encode(v) : v])); }
  meta(key, bytes) {
    return {
      key, size: bytes.length, httpEtag: `"${key.length}"`, uploaded: new Date(0),
      writeHttpMetadata: (h) => h.set("content-type", "application/octet-stream"),
    };
  }
  async head(key) { const b = this.objs.get(key); return b ? this.meta(key, b) : null; }
  async get(key, opts = {}) {
    const b = this.objs.get(key);
    if (!b) return null;
    const m = this.meta(key, b);
    let slice = b, range;
    const rh = opts.range instanceof Headers ? opts.range.get("range") : null;
    if (rh) {
      const [, s, e] = /bytes=(\d*)-(\d*)/.exec(rh);
      if (s === "") { range = { suffix: Number(e) }; slice = b.slice(b.length - Number(e)); }
      else {
        const off = Number(s), end = e === "" ? b.length - 1 : Number(e);
        if (off >= b.length) throw new Error("get: The requested range is not satisfiable (10039)");
        range = { offset: off, length: end - off + 1 }; slice = b.slice(off, end + 1);
      }
    }
    const inm = opts.onlyIf instanceof Headers ? opts.onlyIf.get("if-none-match") : null;
    if (inm && inm === m.httpEtag) return m;  // no body: precondition failed
    return { ...m, range, body: new Blob([slice]).stream(), arrayBuffer: async () => slice.buffer.slice(slice.byteOffset, slice.byteOffset + slice.length) };
  }
}

class FakeDB {
  constructor() { this.rows = []; }
  prepare(sql) {
    const db = this;
    return {
      bind(...args) {
        return {
          async run() {
            if (sql.startsWith("INSERT INTO acceptances")) {
              const [id, license_version, license_sha256, accepted_at, client_ip, method, installer_version, email] = args;
              db.rows.push({ id, license_version, license_sha256, accepted_at, client_ip, method, installer_version, email, revoked_at: null });
            } else if (sql.startsWith("DELETE FROM acceptances WHERE accepted_at <")) {
              db.rows = db.rows.filter((r) => !(r.accepted_at < args[0]));
            } else throw new Error(`unexpected SQL: ${sql}`);
          },
          async first() {
            if (!sql.startsWith("SELECT license_version, revoked_at FROM acceptances WHERE id")) throw new Error(`unexpected SQL: ${sql}`);
            return db.rows.find((r) => r.id === args[0]) || null;
          },
        };
      },
    };
  }
}

function env(over = {}) {
  return {
    BUCKET: new FakeBucket({
      "eula/teaport-engine-license-1.0.txt": EULA,
      "manifest/stable.json": "{}",
      "dl/engine/teaport-engine-1.6-l4t38-cu13": ENGINE,
      "dl/engine/teaport-engine-1.6-THIRD_PARTY_NOTICES.txt": "notices",
      "dl/models/kokoro/Kokoro_espeak_F16.gguf": "kokoro",
      "dl/brain/teaport-brain_0.1.0_arm64.deb": "deb",
    }),
    DB: new FakeDB(),
    SIGNING_KEY: "k".repeat(48),
    CI_TOKEN: "c".repeat(64),
    URL_TTL_SECONDS: "7200",
    RETENTION_DAYS: "2557",
    PUBLIC_ORIGIN: "https://get.teaspoon.tech",
    ...over,
  };
}

const BIN = "/dl/engine/teaport-engine-1.6-l4t38-cu13";
const KOKORO = "/dl/models/kokoro/Kokoro_espeak_F16.gguf";
const base = {
  license_version: "1.0", license_sha256: EULA_SHA, email: "owner@example.com",
  method: "tty", installer_version: "git:abc1234", paths: [BIN, KOKORO],
};

const get = (e, url, headers = {}) => worker.fetch(new Request(url, { headers }), e);
const post = (e, path, body, headers = {}) => worker.fetch(new Request(`https://get.teaspoon.tech${path}`, {
  method: "POST", headers: { "content-type": "application/json", "cf-connecting-ip": "203.0.113.7", ...headers },
  body: typeof body === "string" ? body : JSON.stringify(body),
}), e);

test("gated paths: engine and models, not the notices or anything else", () => {
  assert.ok(isGated(BIN));
  assert.ok(isGated(KOKORO));
  assert.ok(isGated("/dl/models/voxtral/params.json"));
  assert.ok(!isGated("/dl/engine/teaport-engine-1.6-THIRD_PARTY_NOTICES.txt"));
  assert.ok(!isGated("/dl/brain/teaport-brain_0.1.0_arm64.deb"));
  assert.ok(!isGated("/eula/teaport-engine-license-1.0.txt"));
  assert.ok(!isGated("/manifest/stable.json"));
});

test("public objects serve as before; the engine does not", async () => {
  const e = env();
  for (const p of ["/manifest/stable.json", "/eula/teaport-engine-license-1.0.txt",
    "/dl/engine/teaport-engine-1.6-THIRD_PARTY_NOTICES.txt", "/dl/brain/teaport-brain_0.1.0_arm64.deb"]) {
    assert.equal((await get(e, `https://get.teaspoon.tech${p}`)).status, 200, p);
  }
  assert.equal((await get(e, "https://get.teaspoon.tech/")).status, 404);
  assert.equal((await get(e, "https://get.teaspoon.tech/nope")).status, 404);
  const r = await get(e, `https://get.teaspoon.tech${BIN}`);
  assert.equal(r.status, 403);
  assert.match(await r.text(), /Teaport Engine License/);
  assert.equal((await get(e, `https://get.teaspoon.tech${KOKORO}`)).status, 403);
  // A gated path that does not exist is 403 too: nothing about the bucket leaks.
  assert.equal((await get(e, "https://get.teaspoon.tech/dl/engine/other")).status, 403);
});

test("accept records exactly the fields, then its signed URLs download", async () => {
  const e = env();
  const r = await post(e, "/v1/engine-license/accept", base);
  assert.equal(r.status, 201);
  const b = await r.json();
  assert.equal(e.DB.rows.length, 1);
  const row = e.DB.rows[0];
  assert.deepEqual(Object.keys(row).sort(), ["accepted_at", "client_ip", "email", "id", "installer_version",
    "license_sha256", "license_version", "method", "revoked_at"]);
  assert.equal(row.client_ip, "203.0.113.7");
  assert.equal(row.email, "owner@example.com");
  assert.equal(row.license_sha256, EULA_SHA);
  assert.equal(row.method, "tty");
  assert.equal(row.id, b.acceptance_id);
  assert.match(row.accepted_at, /^\d{4}-\d\d-\d\dT.*Z$/);
  assert.deepEqual(Object.keys(b.urls).sort(), [BIN, KOKORO].sort());
  assert.ok(b.urls[BIN].startsWith(`https://get.teaspoon.tech${BIN}?`));

  const d = await get(e, b.urls[BIN]);
  assert.equal(d.status, 200);
  assert.deepEqual(new Uint8Array(await d.arrayBuffer()), ENGINE);

  // A resume: curl -C - sends a Range.
  const p = await get(e, b.urls[BIN], { range: "bytes=900-" });
  assert.equal(p.status, 206);
  assert.equal(p.headers.get("content-range"), "bytes 900-999/1000");
  assert.deepEqual(new Uint8Array(await p.arrayBuffer()), ENGINE.slice(900));
  assert.equal((await get(e, b.urls[BIN], { range: "bytes=5000-" })).status, 416);

  // A URL signed for one path does not open another.
  const other = new URL(b.urls[BIN]); other.pathname = KOKORO;
  assert.equal((await get(e, other.toString())).status, 403);
  // Nor does a tampered expiry or acceptance id.
  for (const [k, v] of [["exp", "9999999999"], ["aid", "someone-else"], ["sig", "x"]]) {
    const u = new URL(b.urls[BIN]); u.searchParams.set(k, v);
    assert.equal((await get(e, u.toString())).status, 403, k);
  }
});

test("signed URLs expire", async () => {
  const e = env({ URL_TTL_SECONDS: "-1" });
  const b = await (await post(e, "/v1/engine-license/accept", base)).json();
  assert.equal((await get(e, b.urls[BIN])).status, 403);
});

test("the text accepted must be the published text of that version", async () => {
  const e = env();
  let r = await post(e, "/v1/engine-license/accept", { ...base, license_sha256: "0".repeat(64) });
  assert.equal(r.status, 409);
  r = await post(e, "/v1/engine-license/accept", { ...base, license_version: "9.9" });
  assert.equal(r.status, 404);
  assert.equal(e.DB.rows.length, 0);
});

test("accept validates every field", async () => {
  const e = env();
  const bad = [
    { email: "" }, { email: "not-an-email" }, { email: "a@b" }, { email: "a b@c.d" },
    { method: "env" }, { method: undefined }, { license_version: "1" }, { license_version: "../x" },
    { installer_version: "" }, { paths: [] }, { paths: ["/manifest/stable.json"] },
    { paths: ["/dl/engine/../brain/x"] }, { paths: Array.from({ length: 33 }, (_, i) => `/dl/engine/${i}`) },
  ];
  for (const over of bad) {
    const r = await post(e, "/v1/engine-license/accept", { ...base, ...over });
    assert.equal(r.status, 400, JSON.stringify(over));
  }
  assert.equal((await post(e, "/v1/engine-license/accept", "not json")).status, 400);
  assert.equal((await post(e, "/v1/engine-license/accept", base, { "content-type": "text/plain" })).status, 400);
  assert.equal(e.DB.rows.length, 0);
});

test("the token renews URLs until it is revoked; a forged one does not", async () => {
  const e = env();
  const b = await (await post(e, "/v1/engine-license/accept", { ...base, method: "flag" })).json();
  assert.deepEqual(await readToken(e, b.token), { id: b.acceptance_id, version: "1.0" });
  let r = await post(e, "/v1/engine-license/urls", { token: b.token, paths: [BIN] });
  assert.equal(r.status, 200);
  const u = await r.json();
  assert.equal((await get(e, u.urls[BIN])).status, 200);
  assert.equal(e.DB.rows.length, 1, "a renewal records nothing");

  const [v, payload] = b.token.split(".");
  r = await post(e, "/v1/engine-license/urls", { token: `${v}.${payload}.AAAA`, paths: [BIN] });
  assert.equal(r.status, 401);
  r = await post(e, "/v1/engine-license/urls", { token: b.token, paths: [BIN] }, {});
  assert.equal(r.status, 200);

  e.DB.rows[0].revoked_at = new Date().toISOString();
  r = await post(e, "/v1/engine-license/urls", { token: b.token, paths: [BIN] });
  assert.equal(r.status, 401);
});

test("the CI token reads gated objects; nothing else does", async () => {
  const e = env();
  assert.equal((await get(e, `https://get.teaspoon.tech${BIN}`, { authorization: `Bearer ${"c".repeat(64)}` })).status, 200);
  assert.equal((await get(e, `https://get.teaspoon.tech${BIN}`, { authorization: "Bearer wrong" })).status, 403);
  const short = env({ CI_TOKEN: "short" });
  assert.equal((await get(short, `https://get.teaspoon.tech${BIN}`, { authorization: "Bearer short" })).status, 403);
});

test("rate limit on accept", async () => {
  const e = env({ ACCEPT_LIMIT: { limit: async () => ({ success: false }) } });
  assert.equal((await post(e, "/v1/engine-license/accept", base)).status, 429);
});

test("no SIGNING_KEY: the endpoint fails rather than signing with nothing", async () => {
  const e = env({ SIGNING_KEY: "" });
  assert.equal((await post(e, "/v1/engine-license/accept", base)).status, 500);
  assert.equal(e.DB.rows.length, 0);
  assert.equal((await get(e, `https://get.teaspoon.tech${BIN}?aid=a&exp=9999999999&sig=x`)).status, 500);
});

test("GATE=open serves the engine unsigned (the switchover); anything else enforces", async () => {
  assert.equal((await get(env({ GATE: "open" }), `https://get.teaspoon.tech${BIN}`)).status, 200);
  assert.equal((await get(env({ GATE: "enforce" }), `https://get.teaspoon.tech${BIN}`)).status, 403);
  assert.equal((await get(env({ GATE: "Open" }), `https://get.teaspoon.tech${BIN}`)).status, 403);
});

test("retention: the daily job deletes records past RETENTION_DAYS", async () => {
  const e = env({ RETENTION_DAYS: "30" });
  e.DB.rows.push({ id: "old", accepted_at: "2000-01-01T00:00:00.000Z" }, { id: "new", accepted_at: new Date().toISOString() });
  await worker.scheduled({}, e);
  assert.deepEqual(e.DB.rows.map((r) => r.id), ["new"]);
});
