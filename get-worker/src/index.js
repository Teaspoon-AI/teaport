// get.teaspoon.tech — the download host, with the engine license in front of the engine.
//
// Everything the bucket (teaport-get) holds is served from here, as the bucket's public
// custom domain served it before: the manifests, the one-liner's bootstrap (/teaport), the
// license texts (/eula/...), the brain package, the OpenRouter connect page, and the
// engine's THIRD_PARTY_NOTICES.txt. Two prefixes are different: /dl/engine/ (the engine
// binary, its G2P dict and POS tagger) and /dl/models/ (the models it runs). Those answer
// only a URL this Worker signed, and it signs one only for an installer that has accepted
// the engine license here, so that every copy of the engine goes to someone who agreed to
// its terms, and Teaspoon AI holds the record of it (docs/engine-license-gate.md).
//
//   POST /v1/engine-license/accept   record an acceptance; returns a token + signed URLs
//   POST /v1/engine-license/urls     a token from /accept -> fresh signed URLs
//   GET|HEAD /dl/engine/..., /dl/models/...   a signed URL (or the CI token) -> the object
//   GET|HEAD anything else           the object, public, as before
//
// What /accept stores, and for how long: docs/engine-license-data.md. Keep the two in step.
//
// Bindings (wrangler.toml): BUCKET (R2 teaport-get), DB (D1 teaport-engine-license),
// ACCEPT_LIMIT (rate limit, per client IP). Secrets: SIGNING_KEY (HMAC key for tokens and
// URLs), CI_TOKEN (manifest/check.py's bearer token for the gated objects). Vars:
// URL_TTL_SECONDS, RETENTION_DAYS, and GATE: "enforce" (the default) answers 403 to an
// unsigned engine download; "open" still serves it, for the switchover only, while
// installers that predate the gate are the ones in use (docs/engine-license-gate.md).

const GATED_PREFIXES = ["/dl/engine/", "/dl/models/"];
// The attributions ship beside the engine but are not the engine: they stay public, as the
// licenses they carry ask. install.sh's gated_path() is the same rule; keep them in step.
const PUBLIC_SUFFIXES = ["THIRD_PARTY_NOTICES.txt"];

const MAX_PATHS = 32;
const MAX_BODY = 16 * 1024;
const VERSION_RE = /^[0-9]+(\.[0-9]+){1,3}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
// Deliberately loose: one @, something on each side, a dot in the domain, no spaces or
// control characters. The identity is what the person typed, not a verified address.
const EMAIL_RE = /^[^\s@\x00-\x1f]{1,64}@[^\s@\x00-\x1f]+\.[^\s@\x00-\x1f]+$/;
const METHODS = new Set(["tty", "flag"]);

export function isGated(path) {
  return GATED_PREFIXES.some((p) => path.startsWith(p)) &&
    !PUBLIC_SUFFIXES.some((s) => path.endsWith(s));
}

export function eulaKey(version) {
  return `eula/teaport-engine-license-${version}.txt`;
}

// --- crypto -------------------------------------------------------------------

const enc = new TextEncoder();

function b64url(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function unb64url(s) {
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

async function hmac(key, msg) {
  if (!key || key.length < 32) throw new Error("SIGNING_KEY is not set (or shorter than 32 characters)");
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64url(await crypto.subtle.sign("HMAC", k, enc.encode(msg)));
}

// Constant-time compare of two strings of the same alphabet.
function same(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

async function sha256hex(buf) {
  const d = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Token: "v1.<payload>.<mac>", payload = base64url JSON {a: acceptance id, v: license
// version}. It never expires on its own: the engine is the box's to repair for as long as
// the license it accepted is the current one, and install.sh asks again when it changes.
// Revoking one is `UPDATE acceptances SET revoked_at = ...` (urls() checks the row).
export async function makeToken(env, id, version) {
  const payload = b64url(enc.encode(JSON.stringify({ a: id, v: version })));
  return `v1.${payload}.${await hmac(env.SIGNING_KEY, `tok\n${payload}`)}`;
}

export async function readToken(env, token) {
  if (typeof token !== "string" || token.length > 512) return null;
  const [ver, payload, mac] = token.split(".");
  if (ver !== "v1" || !payload || !mac) return null;
  if (!same(mac, await hmac(env.SIGNING_KEY, `tok\n${payload}`))) return null;
  try {
    const { a, v } = JSON.parse(new TextDecoder().decode(unb64url(payload)));
    return typeof a === "string" && typeof v === "string" ? { id: a, version: v } : null;
  } catch {
    return null;
  }
}

// A signed URL names the path, its expiry and the acceptance it was issued under (so a
// URL that turns up somewhere it should not leads back to an acceptance).
async function urlMac(env, path, exp, aid) {
  return hmac(env.SIGNING_KEY, `dl\n${path}\n${exp}\n${aid}`);
}

export async function signUrl(env, origin, path, aid, now) {
  const exp = Math.floor(now / 1000) + Number(env.URL_TTL_SECONDS || 7200);
  const q = new URLSearchParams({ aid, exp: String(exp), sig: await urlMac(env, path, exp, aid) });
  return { url: `${origin}${path}?${q}`, exp };
}

async function signedOk(env, url, now) {
  const aid = url.searchParams.get("aid"), exp = url.searchParams.get("exp"), sig = url.searchParams.get("sig");
  if (!aid || !exp || !sig || !/^[0-9]{1,12}$/.test(exp)) return false;
  // Checked when a request starts: a transfer under way when the URL expires runs on.
  if (Number(exp) * 1000 < now) return false;
  return same(sig, await urlMac(env, url.pathname, exp, aid));
}

function ciOk(env, request) {
  const h = request.headers.get("authorization") || "";
  return !!env.CI_TOKEN && env.CI_TOKEN.length >= 32 && same(h, `Bearer ${env.CI_TOKEN}`);
}

// --- responses ----------------------------------------------------------------

function json(status, body, extra = {}) {
  return new Response(JSON.stringify(body) + "\n", {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...extra },
  });
}

const err = (status, error, message) => json(status, { error, message });

function forbidden() {
  return new Response(
    "403 Forbidden: the Teaport engine and its models are downloaded by install.sh once you\n" +
      "have accepted the Teaport Engine License (https://get.teaspoon.tech/teaport).\n",
    { status: 403, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } },
  );
}

// An R2 object as an HTTP response: what the bucket's public custom domain did, ranges and
// conditional requests included (curl -C - resumes a model download with a Range).
async function serveObject(env, request, key) {
  const head = request.method === "HEAD";
  // A range starting at or past the end is 416, as the bucket's custom domain answers it
  // (curl -C - on a complete .part): checked here, since what the binding does with one
  // is not specified (Miniflare serves the whole object as a 206).
  const start = /^bytes=(\d+)-/.exec(request.headers.get("range") || "");
  if (start && !head) {
    const meta = await env.BUCKET.head(key);
    if (meta && Number(start[1]) >= meta.size) {
      return new Response(null, { status: 416, headers: { "content-range": `bytes */${meta.size}` } });
    }
  }
  let obj;
  try {
    obj = head
      ? await env.BUCKET.head(key)
      : await env.BUCKET.get(key, { range: request.headers, onlyIf: request.headers });
  } catch (e) {
    // A Range the object cannot satisfy.
    if (/range/i.test(String(e && e.message))) return new Response(null, { status: 416 });
    throw e;
  }
  if (obj === null) return new Response("404 Not Found\n", { status: 404, headers: { "content-type": "text/plain" } });
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set("etag", obj.httpEtag);
  headers.set("accept-ranges", "bytes");
  headers.set("last-modified", obj.uploaded.toUTCString());
  if (head) {
    headers.set("content-length", String(obj.size));
    return new Response(null, { status: 200, headers });
  }
  if (!("body" in obj)) {
    // onlyIf did not hold: If-None-Match matched (304) or If-Match / If-Unmodified-Since
    // did not (412).
    const status = request.headers.has("if-none-match") || request.headers.has("if-modified-since") ? 304 : 412;
    return new Response(null, { status, headers });
  }
  const r = obj.range;
  if (request.headers.has("range") && r) {
    const offset = r.offset ?? (r.suffix !== undefined ? obj.size - r.suffix : 0);
    const length = r.length ?? (r.suffix !== undefined ? r.suffix : obj.size - offset);
    headers.set("content-range", `bytes ${offset}-${offset + length - 1}/${obj.size}`);
    headers.set("content-length", String(length));
    return new Response(obj.body, { status: 206, headers });
  }
  headers.set("content-length", String(obj.size));
  return new Response(obj.body, { status: 200, headers });
}

// --- the license endpoint -----------------------------------------------------

async function readJson(request) {
  if (!(request.headers.get("content-type") || "").startsWith("application/json")) return null;
  const text = await request.text();
  if (text.length > MAX_BODY) return null;
  try {
    const v = JSON.parse(text);
    return v && typeof v === "object" && !Array.isArray(v) ? v : null;
  } catch {
    return null;
  }
}

function pathsFrom(body) {
  const paths = body.paths;
  if (!Array.isArray(paths) || paths.length === 0 || paths.length > MAX_PATHS) return null;
  for (const p of paths) {
    if (typeof p !== "string" || p.length > 512 || !isGated(p) || p.includes("..") || p.includes("?") || p.includes("#")) return null;
  }
  return [...new Set(paths)];
}

async function signAll(env, origin, paths, aid, now) {
  const urls = {};
  let exp = 0;
  for (const p of paths) {
    const s = await signUrl(env, origin, p, aid, now);
    urls[p] = s.url;
    exp = s.exp;
  }
  return { urls, expires_at: new Date(exp * 1000).toISOString() };
}

function clean(s, max) {
  return typeof s === "string" && s.length <= max && !/[\x00-\x1f\x7f]/.test(s) ? s : null;
}

async function accept(request, env, origin, now) {
  const ip = request.headers.get("cf-connecting-ip") || "";
  if (env.ACCEPT_LIMIT) {
    const { success } = await env.ACCEPT_LIMIT.limit({ key: ip || "unknown" });
    if (!success) return err(429, "rate_limited", "too many license acceptances from this address; try again in a minute");
  }
  const body = await readJson(request);
  if (!body) return err(400, "bad_request", "expected a JSON object (content-type: application/json)");
  const version = clean(body.license_version, 16);
  const sha = clean(body.license_sha256, 64);
  const email = clean(typeof body.email === "string" ? body.email.trim() : body.email, 254);
  const method = clean(body.method, 8);
  const installer = clean(body.installer_version, 64);
  const paths = pathsFrom(body);
  if (!version || !VERSION_RE.test(version)) return err(400, "bad_license_version", "license_version must be the manifest's engine.eula.version, e.g. 1.0");
  if (!sha || !SHA256_RE.test(sha)) return err(400, "bad_license_sha256", "license_sha256 must be the lowercase hex SHA-256 of the license text");
  if (!email || !EMAIL_RE.test(email)) return err(400, "bad_email", "an email address is required to accept the license");
  if (!method || !METHODS.has(method)) return err(400, "bad_method", 'method must be "tty" or "flag"');
  if (installer === null || !installer) return err(400, "bad_installer_version", "installer_version is required");
  if (!paths) return err(400, "bad_paths", `paths must be 1-${MAX_PATHS} gated download paths (/dl/engine/..., /dl/models/...)`);

  // The text accepted must be the text published for that version, byte for byte: the
  // record then says exactly what was agreed to.
  const text = await env.BUCKET.get(eulaKey(version));
  if (text === null) return err(404, "unknown_license_version", `no Teaport Engine License v${version} is published`);
  const published = await sha256hex(await text.arrayBuffer());
  if (published !== sha) {
    return err(409, "license_text_mismatch", `the license text accepted is not the published v${version} (sha256 ${published})`);
  }

  const id = crypto.randomUUID();
  const at = new Date(now).toISOString();
  await env.DB.prepare(
    "INSERT INTO acceptances (id, license_version, license_sha256, accepted_at, client_ip, method, installer_version, email) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind(id, version, sha, at, ip, method, installer, email).run();

  const signed = await signAll(env, origin, paths, id, now);
  return json(201, {
    acceptance_id: id,
    license_version: version,
    license_sha256: sha,
    accepted_at: at,
    token: await makeToken(env, id, version),
    ...signed,
  });
}

async function urls(request, env, origin, now) {
  const body = await readJson(request);
  if (!body) return err(400, "bad_request", "expected a JSON object (content-type: application/json)");
  const tok = await readToken(env, body.token);
  if (!tok) return err(401, "bad_token", "not a token this server issued; accept the license again (install.sh)");
  const paths = pathsFrom(body);
  if (!paths) return err(400, "bad_paths", `paths must be 1-${MAX_PATHS} gated download paths (/dl/engine/..., /dl/models/...)`);
  const row = await env.DB.prepare("SELECT license_version, revoked_at FROM acceptances WHERE id = ?").bind(tok.id).first();
  if (!row || row.revoked_at) return err(401, "acceptance_revoked", "this acceptance is no longer valid; accept the license again (install.sh)");
  return json(200, { acceptance_id: tok.id, license_version: row.license_version, ...(await signAll(env, origin, paths, tok.id, now)) });
}

// --- entry points ----------------------------------------------------------------

export default {
  async fetch(request, env) {
    try {
      return await handle(request, env);
    } catch (e) {
      // A misconfiguration (no SIGNING_KEY) or a D1/R2 failure: nothing about it goes to
      // the client; Workers Logs has the exception.
      console.error("teaport-get:", e && e.stack ? e.stack : e);
      return err(500, "server_error", "the download server failed; try again later");
    }
  },

  // Daily: acceptance records past their retention are deleted (docs/engine-license-data.md).
  async scheduled(_event, env) {
    const days = Number(env.RETENTION_DAYS || 0);
    if (!(days > 0)) return;
    const cutoff = new Date(Date.now() - days * 86400 * 1000).toISOString();
    await env.DB.prepare("DELETE FROM acceptances WHERE accepted_at < ?").bind(cutoff).run();
  },
};

async function handle(request, env) {
  const url = new URL(request.url);
  const origin = env.PUBLIC_ORIGIN || url.origin;
  const now = Date.now();
  const path = url.pathname;

  // Without the key nothing can be signed or checked: fail before anything is recorded.
  if ((path.startsWith("/v1/") || isGated(path)) && !(env.SIGNING_KEY && env.SIGNING_KEY.length >= 32)) {
    throw new Error("SIGNING_KEY is not set (or shorter than 32 characters)");
  }
  if (path.startsWith("/v1/engine-license/")) {
    if (request.method !== "POST") return err(405, "method_not_allowed", "POST only");
    if (path === "/v1/engine-license/accept") return accept(request, env, origin, now);
    if (path === "/v1/engine-license/urls") return urls(request, env, origin, now);
    return err(404, "not_found", "no such endpoint");
  }

  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("405 Method Not Allowed\n", { status: 405, headers: { allow: "GET, HEAD" } });
  }
  let key;
  try {
    key = decodeURIComponent(path.slice(1));
  } catch {
    return new Response("400 Bad Request\n", { status: 400 });
  }
  // The bucket has no index: "/" and any ".../" are 404, as before (install.sh's
  // preflight takes the bare host's 404 as the healthy answer).
  if (!key || key.endsWith("/")) return new Response("404 Not Found\n", { status: 404, headers: { "content-type": "text/plain" } });
  if (isGated(path) && env.GATE !== "open" && !(await signedOk(env, url, now)) && !ciOk(env, request)) return forbidden();
  return serveObject(env, request, key);
}
