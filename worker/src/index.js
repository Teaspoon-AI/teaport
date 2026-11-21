// teaport download gate — the Cloudflare Worker in front of the engine's files.
//
// get.teaspoon.tech is the public R2 bucket teaport-get. The engine program, its G2P
// dictionary and POS model, and the two models are not in it: they live in the private
// bucket teaport-engine-dist (no r2.dev URL, no custom domain) under the same keys
// (dl/engine/..., dl/models/...), and only this Worker reads them. A file downloads only
// with a token, and a token is issued only for an acceptance of the Teaport Engine License
// recorded here, in D1. The license text, the manifests, the third-party notices and the
// brain package stay public in teaport-get.
//
//   POST /api/accept   {channel, eula_version, eula_sha256, name, email, use, license_id,
//                       device_id, method, installer}
//                      -> {acceptance_id, token, expires_at}. The license version and
//                      sha256 must be the current ones: the channel's manifest's
//                      engine.eula, read from teaport-get. The acceptance is recorded with
//                      the installer's revision and the request's IP address, country and
//                      user agent.
//   POST /api/token    {acceptance_id} -> {token, expires_at}: a later run on the same box
//                      (a repair, `teaport update`) gets a new token from its receipt.
//                      409 eula_changed when the license changed since: the installer asks
//                      for acceptance again.
//   GET|HEAD /dl/engine/*, /dl/models/*   Authorization: Bearer <token>. Streams the file
//                      from teaport-engine-dist, with byte ranges (curl -C - resumes the
//                      multi-GB models), and logs the download against the acceptance.
//
// GATE (a wrangler.toml var): "enforce" refuses a download without a token. "open" is for
// the switchover only: a request with no Authorization header is served too (logged as
// acceptance "open", with x-teaport-gate: open), so the installer on main that predates the
// gate keeps working until the one that accepts through here is merged; a token that is
// sent is still checked. Anything else, or unset, enforces.
//
// Retention (the daily cron, scheduled()): acceptances and downloads older than
// RETENTION_DAYS are deleted; unset or 0 keeps them. What is kept and why:
// docs/engine-license-data.md.
//
// The token: "v1.<payload>.<HMAC-SHA256>", base64url, signed with TOKEN_KEY (a Worker
// secret). It carries the acceptance, its license version and channel, and an expiry
// (TOKEN_TTL_SECS). Each download also checks the acceptance is not revoked and the
// license version is still the current one. CHECK_TOKEN (a Worker secret, optional) is
// the publish workflow's: manifest/check.py downloads every gated file with it to compare
// its sha256; its downloads are logged as acceptance "ci".
//
// Bindings (wrangler.toml): DIST (teaport-engine-dist), PUBLIC (teaport-get, read for the
// manifests), DB (D1, schema in migrations/), ACCEPT_LIMIT (rate limit per IP on /api/*,
// optional). Tests: node --test worker/test/*.test.mjs (from the repo root; no dependencies).

export const TOKEN_TTL_SECS = 24 * 3600;
const MANIFEST_TTL_MS = 60 * 1000;
const CHANNELS = new Set(["stable", "beta"]);
const GATED = /^\/dl\/(engine|models)\/[A-Za-z0-9._\-/]+$/;
const MAX_BODY = 4096;
const INSTALLER = /^[A-Za-z0-9._:+-]{1,64}$/;

const manifests = new Map();   // channel -> {at, eula: {version, sha256}}

export function resetCache() { manifests.clear(); }

function json(status, body, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...extra },
  });
}

// <more>: fields beside error and message (eula_changed names the current license).
function fail(status, error, message, headers = {}, more = {}) {
  return json(status, { error, message, ...more }, headers);
}

// --- tokens ------------------------------------------------------------------------------

const enc = new TextEncoder();

function b64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function unb64url(s) {
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

async function hmacKey(secret) {
  return crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" },
                                 false, ["sign", "verify"]);
}

export async function makeToken(env, claims) {
  const payload = b64url(enc.encode(JSON.stringify(claims)));
  const sig = await crypto.subtle.sign("HMAC", await hmacKey(env.TOKEN_KEY), enc.encode(`v1.${payload}`));
  return `v1.${payload}.${b64url(new Uint8Array(sig))}`;
}

// The claims of a well-signed, unexpired token, else null.
export async function readToken(env, token, now) {
  const parts = token.split(".");
  if (parts.length !== 3 || parts[0] !== "v1") return null;
  let sig, claims;
  try {
    sig = unb64url(parts[2]);
    const ok = await crypto.subtle.verify("HMAC", await hmacKey(env.TOKEN_KEY), sig,
                                          enc.encode(`v1.${parts[1]}`));
    if (!ok) return null;
    claims = JSON.parse(new TextDecoder().decode(unb64url(parts[1])));
  } catch {
    return null;
  }
  if (typeof claims !== "object" || claims === null || !(claims.exp > now)) return null;
  return claims;
}

// Constant-time comparison for CHECK_TOKEN: both sides hashed, then compared in full.
async function sameSecret(a, b) {
  const [x, y] = await Promise.all([a, b].map((s) => crypto.subtle.digest("SHA-256", enc.encode(s))));
  const u = new Uint8Array(x), v = new Uint8Array(y);
  let diff = 0;
  for (let i = 0; i < u.length; i++) diff |= u[i] ^ v[i];
  return diff === 0;
}

async function issue(env, acceptance, channel, version, now) {
  const exp = now + TOKEN_TTL_SECS;
  const token = await makeToken(env, { a: acceptance, v: version, c: channel, exp });
  return { token, expires_at: new Date(exp * 1000).toISOString() };
}

// --- the manifest: which license is current ------------------------------------------------

// The channel's current license {version, sha256}, from manifest/<channel>.json in
// teaport-get (what install.sh reads), cached for a minute. null: no such manifest, or
// one without a released license.
async function currentEula(env, channel, nowMs) {
  const hit = manifests.get(channel);
  if (hit && nowMs - hit.at < MANIFEST_TTL_MS) return hit.eula;
  const obj = await env.PUBLIC.get(`manifest/${channel}.json`);
  let eula = null;
  if (obj) {
    try {
      const e = (await obj.json())?.engine?.eula;
      if (e && typeof e.version === "string" && /^[0-9a-f]{64}$/.test(e.sha256 ?? "")) {
        eula = { version: e.version, sha256: e.sha256 };
      }
    } catch {
      eula = null;
    }
  }
  manifests.set(channel, { at: nowMs, eula });
  return eula;
}

// --- /api ----------------------------------------------------------------------------------

async function body(request) {
  const text = await request.text();
  if (text.length > MAX_BODY) return null;
  try {
    const v = JSON.parse(text);
    return typeof v === "object" && v !== null && !Array.isArray(v) ? v : null;
  } catch {
    return null;
  }
}

const CONTROL = /[\u0000-\u001f\u007f]/;
function text(v, max) {
  if (typeof v !== "string") return null;
  const t = v.trim();
  return t && t.length <= max && !CONTROL.test(t) ? t : null;
}

function channelOf(v) {
  const c = v === undefined ? "stable" : v;
  return CHANNELS.has(c) ? c : null;
}

function client(request) {
  return {
    ip: request.headers.get("cf-connecting-ip") ?? "",
    country: request.cf?.country ?? "",
    ua: (request.headers.get("user-agent") ?? "").slice(0, 200),
  };
}

function randomId() {
  return b64url(crypto.getRandomValues(new Uint8Array(18)));
}

async function accept(request, env, now) {
  const b = await body(request);
  if (!b) return fail(400, "bad_request", "expected a JSON object of at most 4 KB");
  const channel = channelOf(b.channel);
  if (!channel) return fail(400, "bad_request", "channel: stable or beta");
  const eula = await currentEula(env, channel, now * 1000);
  if (!eula) return fail(503, "no_license", `the ${channel} channel has no published license`);
  if (b.eula_version !== eula.version || b.eula_sha256 !== eula.sha256) {
    return fail(409, "eula_changed", `the current license is version ${eula.version}: read and accept that one`,
                {}, { eula_version: eula.version, eula_sha256: eula.sha256 });
  }
  const name = text(b.name, 200);
  if (!name) return fail(400, "bad_request", "name: your name, at most 200 characters");
  const email = text(b.email, 254);
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return fail(400, "bad_request", "email: an email address");
  }
  if (b.use !== "personal" && b.use !== "licensed") {
    return fail(400, "bad_request", "use: personal (an individual's personal use) or licensed");
  }
  let license = null;
  if (b.use === "licensed") {
    license = text(b.license_id, 100);
    if (!license || !/^[A-Za-z0-9._:-]+$/.test(license)) {
      return fail(400, "bad_request", "license_id: the id of your Teaspoon AI license (licensed use needs one)");
    }
  }
  let device = null;
  if (b.device_id !== undefined && b.device_id !== null && b.device_id !== "") {
    device = typeof b.device_id === "string" && /^[0-9a-f]{16,128}$/.test(b.device_id) ? b.device_id : undefined;
    if (device === undefined) return fail(400, "bad_request", "device_id: 16 to 128 lowercase hex digits");
  }
  if (b.method !== "tty" && b.method !== "flag") return fail(400, "bad_request", "method: tty or flag");
  if (typeof b.installer !== "string" || !INSTALLER.test(b.installer)) {
    return fail(400, "bad_request", "installer: the installer's revision, such as git:0123456789ab (an installer that sends none is out of date: update it)");
  }

  const id = randomId();
  const c = client(request);
  await env.DB.prepare(
    `INSERT INTO acceptances (id, created_at, channel, eula_version, eula_sha256, name, email, use_type,
       license_id, device_id, method, installer, ip, country, user_agent, last_token_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?2)`,
  ).bind(id, new Date(now * 1000).toISOString(), channel, eula.version, eula.sha256, name, email, b.use,
         license, device, b.method, b.installer, c.ip, c.country, c.ua).run();
  return json(200, { acceptance_id: id, ...(await issue(env, id, channel, eula.version, now)) });
}

async function token(request, env, now) {
  const b = await body(request);
  const id = b && typeof b.acceptance_id === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(b.acceptance_id)
    ? b.acceptance_id : null;
  if (!id) return fail(400, "bad_request", "acceptance_id: the id your acceptance was recorded under");
  const row = await env.DB.prepare(
    "SELECT channel, eula_version, revoked_at FROM acceptances WHERE id = ?1").bind(id).first();
  if (!row) return fail(404, "unknown_acceptance", "no acceptance is recorded under this id: accept the license again");
  if (row.revoked_at) return fail(403, "revoked", "this acceptance was revoked: contact stefano@teaspoon.tech");
  const eula = await currentEula(env, row.channel, now * 1000);
  if (!eula) return fail(503, "no_license", `the ${row.channel} channel has no published license`);
  if (row.eula_version !== eula.version) {
    return fail(409, "eula_changed", `the license changed to version ${eula.version}: read and accept it`, {}, { eula_version: eula.version, eula_sha256: eula.sha256 });
  }
  await env.DB.prepare("UPDATE acceptances SET last_token_at = ?2 WHERE id = ?1")
    .bind(id, new Date(now * 1000).toISOString()).run();
  return json(200, await issue(env, id, row.channel, eula.version, now));
}

// --- /dl -----------------------------------------------------------------------------------

const DENIED = "This file downloads only after the Teaport Engine License is accepted: install with "
  + "the teaport installer, bash <(curl -fsSL https://get.teaspoon.tech/teaport)";

// Who may download: "ci" for CHECK_TOKEN, an acceptance id, "open" for no token while
// GATE is "open", or a Response refusing.
async function authorize(request, env, now) {
  const auth = request.headers.get("authorization");
  if (auth === null && env.GATE === "open") return "open";
  const m = /^Bearer ([A-Za-z0-9._~+/-]{1,2048}=*)$/.exec(auth ?? "");   // RFC 6750 token68
  if (!m) return fail(401, "license_not_accepted", DENIED, { "www-authenticate": "Bearer" });
  if (env.CHECK_TOKEN && await sameSecret(m[1], env.CHECK_TOKEN)) return "ci";
  const claims = await readToken(env, m[1], now);
  if (!claims || typeof claims.a !== "string" || !CHANNELS.has(claims.c)) {
    return fail(401, "bad_token", "the download token is invalid or expired: run the installer again",
                { "www-authenticate": "Bearer" });
  }
  const row = await env.DB.prepare("SELECT revoked_at FROM acceptances WHERE id = ?1").bind(claims.a).first();
  if (!row) return fail(403, "unknown_acceptance", "this acceptance is no longer on record: run the installer again to accept the license");
  if (row.revoked_at) return fail(403, "revoked", "this acceptance was revoked: contact stefano@teaspoon.tech");
  const eula = await currentEula(env, claims.c, now * 1000);
  if (!eula || eula.version !== claims.v) {
    return fail(403, "eula_changed", "the license changed: run the installer again to read and accept it");
  }
  return claims.a;
}

// A single "bytes=" range of an object of <size> bytes: {offset, length}, null for none
// (or one this does not take, served whole as HTTP allows), or "unsatisfiable".
export function parseRange(header, size) {
  if (!header) return null;
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!m || (m[1] === "" && m[2] === "")) return null;
  if (m[1] === "") {   // the last N bytes
    const n = Math.min(Number(m[2]), size);
    return n === 0 ? "unsatisfiable" : { offset: size - n, length: n };
  }
  const start = Number(m[1]);
  if (start >= size) return "unsatisfiable";
  const end = m[2] === "" ? size - 1 : Math.min(Number(m[2]), size - 1);
  if (end < start) return null;
  return { offset: start, length: end - start + 1 };
}

async function download(request, env, ctx, now, path) {
  const who = await authorize(request, env, now);
  if (who instanceof Response) return who;
  const key = path.slice(1);
  const head = await env.DIST.head(key);
  if (!head) return fail(404, "not_found", `no such file: ${path}`);
  const range = parseRange(request.headers.get("range"), head.size);
  const headers = new Headers({ "accept-ranges": "bytes", "cache-control": "private, no-store", etag: head.httpEtag });
  head.writeHttpMetadata(headers);
  if (who === "open") headers.set("x-teaport-gate", "open");
  const c = client(request);
  const log = (status) => ctx.waitUntil(env.DB.prepare(
    "INSERT INTO downloads (at, acceptance_id, key, range_start, status, ip, country) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
  ).bind(new Date(now * 1000).toISOString(), who, key, range && range !== "unsatisfiable" ? range.offset : null,
         status, c.ip, c.country).run().catch(() => {}));
  if (range === "unsatisfiable") {
    headers.set("content-range", `bytes */${head.size}`);
    log(416);
    return new Response(null, { status: 416, headers });
  }
  const status = range ? 206 : 200;
  headers.set("content-length", String(range ? range.length : head.size));
  if (range) headers.set("content-range", `bytes ${range.offset}-${range.offset + range.length - 1}/${head.size}`);
  if (request.method === "HEAD") return new Response(null, { status, headers });
  const obj = await env.DIST.get(key, range ? { range } : undefined);
  if (!obj) return fail(404, "not_found", `no such file: ${path}`);
  log(status);
  return new Response(obj.body, { status, headers });
}

// --- routing -------------------------------------------------------------------------------

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const now = Math.floor(Date.now() / 1000);
    const path = url.pathname;
    if (path === "/api/accept" || path === "/api/token") {
      if (request.method !== "POST") return fail(405, "method_not_allowed", "POST only", { allow: "POST" });
      if (env.ACCEPT_LIMIT) {
        const { success } = await env.ACCEPT_LIMIT.limit({ key: client(request).ip || "unknown" });
        if (!success) return fail(429, "rate_limited", "too many requests: try again in a minute");
      }
      return path === "/api/accept" ? accept(request, env, now) : token(request, env, now);
    }
    if (path.startsWith("/dl/engine/") || path.startsWith("/dl/models/")) {
      if (request.method !== "GET" && request.method !== "HEAD") {
        return fail(405, "method_not_allowed", "GET or HEAD only", { allow: "GET, HEAD" });
      }
      if (!GATED.test(path) || path.split("/").includes("..")) return fail(404, "not_found", `no such file: ${path}`);
      return download(request, env, ctx, now, path);
    }
    return fail(404, "not_found", `no such endpoint: ${path}`);
  },

  // Daily (wrangler.toml [triggers]): what is past RETENTION_DAYS goes.
  async scheduled(_event, env) {
    await expire(env, Math.floor(Date.now() / 1000));
  },
};

export async function expire(env, now) {
  const days = Number(env.RETENTION_DAYS || 0);
  if (!(days > 0)) return;
  const cutoff = new Date((now - days * 86400) * 1000).toISOString();
  // An acceptance's downloads go with it, however recent, so none outlive the row that
  // finds them (docs/engine-license-data.md: access and erasure). Downloads first, so the
  // subquery still sees the acceptances; a run cut short between the two finishes tomorrow.
  await env.DB.prepare(
    "DELETE FROM downloads WHERE at < ?1 OR acceptance_id IN (SELECT id FROM acceptances WHERE created_at < ?1)",
  ).bind(cutoff).run();
  await env.DB.prepare("DELETE FROM acceptances WHERE created_at < ?1").bind(cutoff).run();
}
