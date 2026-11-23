// teaport download gate — the Cloudflare Worker in front of the engine's files.
//
// get.teaspoon.tech is the public R2 bucket teaport-get. The engine program, its G2P
// dictionary and POS model, and the two models are not in it: they live in the private
// bucket teaport-engine-dist (no r2.dev URL, no custom domain) under the same keys
// (dl/engine/..., dl/models/...), and only this Worker reads them. A file downloads only
// with a token, and a token is issued only for an acceptance of the Teaport Engine License
// recorded here, in D1, or for a maker acceptance: the factory's, for a box Teaspoon AI is
// installing before it ships (POST /api/factory). The license text, the manifests, the third-party notices and the
// brain package stay public in teaport-get.
//
//   POST /api/accept   {channel, eula_version, eula_sha256, email, use, license_id,
//                       device_id, method, installer}
//                      -> {acceptance_id, token, expires_at}. The license version and
//                      sha256 must be the current ones: the channel's manifest's
//                      engine.eula, read from teaport-get. The acceptance is recorded with
//                      the installer's revision and the request's IP address, country and
//                      user agent.
//   POST /api/token    {acceptance_id} -> {token, expires_at}: a later run on the same box
//                      (a repair, `teaport update`) gets a new token from its receipt.
//                      409 eula_changed when the license changed since: the installer asks
//                      for acceptance again. A maker acceptance's id gets one whatever the
//                      current license, until it is MAKER_MAX_DAYS old (409 maker_expired
//                      after: the box's owner accepts).
//   POST /api/factory  Authorization: Bearer <FACTORY_TOKEN>, {device_id, installer}
//                      -> {acceptance_id, expires_at}: the factory station's, as it
//                      provisions a box (teaport#464). Teaspoon AI installs its own software
//                      on its own hardware, so this is no one's acceptance of the license:
//                      the box's owner is to accept it at setup (teaport#465). The station
//                      writes the id into the box's receipt, and install.sh's first-boot run
//                      gets tokens with it. One row per box in maker_acceptances; at most
//                      FACTORY_DAILY_MAX boxes in any 24 hours.
//   GET|HEAD /dl/engine/*, /dl/models/*   Authorization: Bearer <token>. Streams the file
//                      from teaport-engine-dist, with byte ranges (curl -C - resumes the
//                      multi-GB models), and logs the download against the acceptance.
//
// GATE (a wrangler.toml var): "enforce" refuses a download without a token. "open" served
// the switchover, and is the way back if enforcing breaks installs: a request with no
// Authorization header is served too (logged as acceptance "open", with x-teaport-gate:
// open), so an installer that does not accept through here still downloads; a token that is
// sent is still checked. Anything else, or unset, enforces.
//
// Retention (the daily cron, scheduled()): acceptances, maker acceptances and downloads
// older than RETENTION_DAYS are deleted; unset or 0 keeps them. What is kept and why:
// docs/engine-license-data.md.
//
// The token: "v1.<payload>.<HMAC-SHA256>", base64url, signed with TOKEN_KEY (a Worker
// secret). It carries the acceptance, its license version and sha256 and channel, and an
// expiry (TOKEN_TTL_SECS). Each download also checks the acceptance is not revoked and the
// license version and sha256 are still the current ones (a token without the sha256 is
// refused). CHECK_TOKEN (a Worker secret, optional) is
// the publish workflow's: manifest/check.py downloads every gated file with it to compare
// its sha256; its downloads are logged as acceptance "ci". FACTORY_TOKEN (a Worker secret,
// optional; unset, /api/factory refuses everything) is the factory station's, and only
// /api/factory takes it: it downloads nothing itself and gets no token, only maker
// acceptances, each for one box. It lives on the station and in the product owner's
// password manager, never in git, R2, a GitHub secret or a box (packaging/RELEASING.md,
// "Worker secrets").
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
const DEVICE = /^[0-9a-f]{16,128}$/;
// A maker acceptance's id: "maker-" and the 24 characters of randomId(). An acceptance's id
// is those 24 characters alone, so the two never meet.
const MAKER_ID = /^maker-[A-Za-z0-9_-]{24}$/;

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

// Which FACTORY_TOKEN recorded a maker acceptance: the first 64 bits of an HMAC of it,
// keyed with TOKEN_KEY (never the secret, nor anything to test guesses at it with). The
// daily cap counts the current one's boxes, so rotating it after a leak used the cap up
// lets the factory go on.
async function factoryMark(env) {
  const sig = await crypto.subtle.sign("HMAC", await hmacKey(env.TOKEN_KEY), enc.encode(`teaport-factory:${env.FACTORY_TOKEN}`));
  return Array.from(new Uint8Array(sig).slice(0, 8), (b) => b.toString(16).padStart(2, "0")).join("");
}

// How long a maker acceptance gets tokens, in seconds: MAKER_MAX_DAYS, a whole number of
// days over 0; anything else gives none (null), so a missing setting fails closed.
function makerTtl(env) {
  const d = Number(env.MAKER_MAX_DAYS);
  return Number.isSafeInteger(d) && d > 0 ? d * 86400 : null;
}

// A maker acceptance row's state at <now>: {refuse: a Response refusing it}, or {until}
// when it gets tokens until <until> (seconds).
function makerState(env, row, now) {
  if (!row) return { refuse: fail(404, "unknown_acceptance", "no acceptance is recorded under this id: accept the license") };
  if (row.revoked_at) return { refuse: fail(403, "revoked", "this box's factory install record was revoked: contact stefano@teaspoon.tech") };
  const ttl = makerTtl(env);
  const until = Math.floor(Date.parse(row.created_at) / 1000) + (ttl ?? 0);
  if (ttl === null || !(until > now)) {
    return { refuse: fail(409, "maker_expired", "this box's factory install record no longer gets downloads: its owner accepts the Teaport Engine License (run the installer on a terminal, or the setup page)") };
  }
  return { until };
}

async function issue(env, acceptance, channel, eula, now) {
  const exp = now + TOKEN_TTL_SECS;
  const token = await makeToken(env, { a: acceptance, v: eula.version, s: eula.sha256, c: channel, exp });
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

// The request's JSON object, else null. At most MAX_BODY bytes are read: a Content-Length
// over it is refused unread, and a body sent without one (chunked) stops being read at the cap.
async function body(request) {
  const declared = request.headers.get("content-length");
  if (declared !== null && !(Number(declared) <= MAX_BODY)) return null;
  if (!request.body) return null;
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY) {
      await reader.cancel().catch(() => {});
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) { bytes.set(c, at); at += c.byteLength; }
  try {
    const v = JSON.parse(new TextDecoder().decode(bytes));   // as request.text() decodes
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
  // No name is recorded (migrations/0002): the column stays for rollbacks and is written
  // empty. An installer from before that still sends one; it is ignored rather than refused,
  // so released installers keep working.
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
     VALUES (?1, ?2, ?3, ?4, ?5, '', ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?2)`,
  ).bind(id, new Date(now * 1000).toISOString(), channel, eula.version, eula.sha256, email, b.use,
         license, device, b.method, b.installer, c.ip, c.country, c.ua).run();
  return json(200, { acceptance_id: id, ...(await issue(env, id, channel, eula, now)) });
}

async function token(request, env, now) {
  const b = await body(request);
  const id = b && typeof b.acceptance_id === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(b.acceptance_id)
    ? b.acceptance_id : null;
  if (!id) return fail(400, "bad_request", "acceptance_id: the id your acceptance was recorded under");
  if (MAKER_ID.test(id)) {
    const maker = await env.DB.prepare("SELECT created_at, revoked_at FROM maker_acceptances WHERE id = ?1").bind(id).first();
    const st = makerState(env, maker, now);
    if (st.refuse) return st.refuse;
    await env.DB.prepare("UPDATE maker_acceptances SET last_token_at = ?2 WHERE id = ?1")
      .bind(id, new Date(now * 1000).toISOString()).run();
    const exp = Math.min(now + TOKEN_TTL_SECS, st.until);   // never past the record's end
    return json(200, { token: await makeToken(env, { a: id, k: "maker", exp }), expires_at: new Date(exp * 1000).toISOString() });
  }
  const row = await env.DB.prepare(
    "SELECT channel, eula_version, eula_sha256, revoked_at FROM acceptances WHERE id = ?1").bind(id).first();
  if (!row) return fail(404, "unknown_acceptance", "no acceptance is recorded under this id: accept the license again");
  if (row.revoked_at) return fail(403, "revoked", "this acceptance was revoked: contact stefano@teaspoon.tech");
  const eula = await currentEula(env, row.channel, now * 1000);
  if (!eula) return fail(503, "no_license", `the ${row.channel} channel has no published license`);
  // The text, not only its version: a license changed under the same version is accepted again.
  if (row.eula_version !== eula.version || row.eula_sha256 !== eula.sha256) {
    return fail(409, "eula_changed", `the license changed to version ${eula.version}: read and accept it`, {}, { eula_version: eula.version, eula_sha256: eula.sha256 });
  }
  await env.DB.prepare("UPDATE acceptances SET last_token_at = ?2 WHERE id = ?1")
    .bind(id, new Date(now * 1000).toISOString()).run();
  return json(200, await issue(env, id, row.channel, eula, now));
}

// The factory station's maker acceptance for one box (see the header). The secret is checked
// first, so nobody without it reaches the body, the daily count or the table.
async function factory(request, env, now) {
  const m = /^Bearer ([A-Za-z0-9._~+/-]{1,2048}=*)$/.exec(request.headers.get("authorization") ?? "");
  if (!env.FACTORY_TOKEN || !m || !(await sameSecret(m[1], env.FACTORY_TOKEN))) {
    return fail(401, "bad_factory_token", "this is the factory station's: it needs its FACTORY_TOKEN",
                { "www-authenticate": "Bearer" });
  }
  // Set up wrong, it records nothing: a short FACTORY_TOKEN could be guessed, one equal to
  // CHECK_TOKEN would download as "ci", one equal to TOKEN_KEY would let whoever holds it
  // sign tokens of their own, a cap that is not a whole number would be no cap, and without
  // MAKER_MAX_DAYS the record would get no tokens.
  const max = Number(env.FACTORY_DAILY_MAX || 0);
  if (!(Number.isSafeInteger(max) && max >= 0) || makerTtl(env) === null || env.FACTORY_TOKEN.length < 32
      || (env.CHECK_TOKEN && await sameSecret(env.FACTORY_TOKEN, env.CHECK_TOKEN))
      || await sameSecret(env.FACTORY_TOKEN, env.TOKEN_KEY)) {
    return fail(503, "factory_misconfigured", "the factory kind is not set up right: FACTORY_TOKEN must be at least 32 characters and differ from CHECK_TOKEN and TOKEN_KEY, FACTORY_DAILY_MAX be a whole number and MAKER_MAX_DAYS one over 0");
  }
  const b = await body(request);
  if (!b) return fail(400, "bad_request", "expected a JSON object of at most 4 KB");
  if (typeof b.device_id !== "string" || !DEVICE.test(b.device_id)) {
    return fail(400, "bad_request", "device_id: the box's, from its serial: 16 to 128 lowercase hex digits");
  }
  if (typeof b.installer !== "string" || !INSTALLER.test(b.installer)) {
    return fail(400, "bad_request", "installer: the revision the box will install, such as git:0123456789ab");
  }
  const mark = await factoryMark(env);
  // A brake on a leaked FACTORY_TOKEN recording made-up boxes, not a lock: one maker
  // acceptance is enough to download every gated file. The records show a leak; a rotation
  // stops new ones, revoking (revoked_at) stops recorded ones. It counts boxes, not records,
  // so a box provisioned again never uses it up; a box recorded in the last 24 hours always
  // gets another. Only the current FACTORY_TOKEN's rows count. Unset or 0: no cap.
  // (Counted, then written: k requests at once can go k-1 over.)
  if (max > 0) {
    const since = new Date((now - 86400) * 1000).toISOString();
    const seen = await env.DB.prepare(
      "SELECT 1 AS y FROM maker_acceptances WHERE device_id = ?1 AND created_at >= ?2 AND mark = ?3 LIMIT 1",
    ).bind(b.device_id, since, mark).first();
    if (!seen) {
      const { n } = await env.DB.prepare(
        "SELECT count(DISTINCT device_id) AS n FROM maker_acceptances WHERE created_at >= ?1 AND mark = ?2",
      ).bind(since, mark).first();
      if (n >= max) {
        return fail(429, "factory_cap", `${max} boxes were recorded in the last 24 hours, the most FACTORY_DAILY_MAX allows`);
      }
    }
  }
  const id = `maker-${randomId()}`;
  const c = client(request);
  await env.DB.prepare(
    `INSERT INTO maker_acceptances (id, created_at, device_id, installer, ip, country, user_agent, mark)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)`,
  ).bind(id, new Date(now * 1000).toISOString(), b.device_id, b.installer, c.ip, c.country, c.ua, mark).run();
  return json(200, { acceptance_id: id, expires_at: new Date((now + makerTtl(env)) * 1000).toISOString() });
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
  const bad = () => fail(401, "bad_token", "the download token is invalid or expired: run the installer again",
                         { "www-authenticate": "Bearer" });
  if (!claims || typeof claims.a !== "string") return bad();
  if (claims.k === "maker") {
    // No license to check: the maker accepted none. Its record must still be live.
    if (!MAKER_ID.test(claims.a)) return bad();
    const row = await env.DB.prepare("SELECT created_at, revoked_at FROM maker_acceptances WHERE id = ?1").bind(claims.a).first();
    const st = makerState(env, row, now);
    if (st.refuse) return st.refuse;
    return claims.a;
  }
  if (!CHANNELS.has(claims.c)) return bad();
  const row = await env.DB.prepare("SELECT revoked_at FROM acceptances WHERE id = ?1").bind(claims.a).first();
  if (!row) return fail(403, "unknown_acceptance", "this acceptance is no longer on record: run the installer again to accept the license");
  if (row.revoked_at) return fail(403, "revoked", "this acceptance was revoked: contact stefano@teaspoon.tech");
  const eula = await currentEula(env, claims.c, now * 1000);
  if (!eula || eula.version !== claims.v || eula.sha256 !== claims.s) {
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
  // The object's own metadata first (its content type), then what this sets over it: an
  // object stored with a Cache-Control of its own must not make a gated file cacheable.
  const headers = new Headers();
  head.writeHttpMetadata(headers);
  headers.set("accept-ranges", "bytes");
  headers.set("cache-control", "private, no-store");
  headers.set("etag", head.httpEtag);
  if (who === "open") headers.set("x-teaport-gate", "open");
  const c = client(request);
  // A HEAD is never logged (docs/engine-license-data.md): only a GET sends the file.
  const log = (status) => request.method !== "HEAD" && ctx.waitUntil(env.DB.prepare(
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
    if (path === "/api/accept" || path === "/api/token" || path === "/api/factory") {
      if (request.method !== "POST") return fail(405, "method_not_allowed", "POST only", { allow: "POST" });
      if (env.ACCEPT_LIMIT) {
        const { success } = await env.ACCEPT_LIMIT.limit({ key: client(request).ip || "unknown" });
        if (!success) return fail(429, "rate_limited", "too many requests: try again in a minute");
      }
      if (path === "/api/factory") return factory(request, env, now);
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
    `DELETE FROM downloads WHERE at < ?1 OR acceptance_id IN (SELECT id FROM acceptances WHERE created_at < ?1)
       OR acceptance_id IN (SELECT id FROM maker_acceptances WHERE created_at < ?1)`,
  ).bind(cutoff).run();
  await env.DB.prepare("DELETE FROM acceptances WHERE created_at < ?1").bind(cutoff).run();
  await env.DB.prepare("DELETE FROM maker_acceptances WHERE created_at < ?1").bind(cutoff).run();
}
