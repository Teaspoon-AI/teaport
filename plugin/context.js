// Context notes for live gateway-relay Talk sessions (issue #71).
//
// A Talk client can only reach the voice LLM through the microphone. These gateway
// methods let it send a short text note instead, such as a tap on an on-screen
// character, which the teaport brain adds to the LLM context without speaking or
// captioning it, and, with respond:true, answers aloud:
//
//   teaport.talk.context       { sessionId, text, respond?, kind? } -> { ok, status }
//   teaport.talk.capabilities  { sessionId? }                      -> what is supported
//
// sessionId is the relaySessionId talk.session.create returned. OpenClaw never tells a
// provider's bridge which relay session it serves (createBridge gets no id), so this
// module ties the two together itself. createBridge runs inside the client's
// talk.session.create request, so the bridge records the gateway connection that
// created it (index.js reads it from the gateway request scope). OpenClaw then records
// the session in its Talk session registry (rememberUnifiedTalkSession, a global Map)
// in the same synchronous run of talk.session.create, so a microtask queued in add()
// finds the record: the first realtime-relay record for this connection that was not
// in the registry when the bridge was made. The bridge is bound to that session at
// creation, and the registry vets every id: unknown ids, other kinds of session and
// other connections' sessions are refused.
//
// The plugin supports OpenClaw 2026.9.x, where the registry is a global (checked on
// 2026.9.1 and 2026.9.6). A host that does not expose it gets host_unsupported: without
// it nothing tells the plugin which session a bridge serves.
//
// This module imports nothing from the OpenClaw SDK, like provider.js, so it can be
// tested standalone (test/context.test.mjs).

export const CONTEXT_METHOD = "teaport.talk.context";
export const CAPABILITIES_METHOD = "teaport.talk.capabilities";

// A transport bound on the note, in characters (code points, as the brain counts them).
// The brain's own limit (TEAPORT_CONTEXT_MAX_CHARS) is the real one; capabilities
// reports the smaller of the two, so a client never meets this one unannounced.
const MAX_TEXT_CHARS = 16000;
const MAX_KIND_CHARS = 64;
const MAX_SESSION_ID_CHARS = 256;
// Closed session ids remembered so a late note hears "closed" rather than "unknown".
const CLOSED_IDS_KEPT = 64;
// How long after its socket opens a bridge waits for the brain's hello before taking
// the brain for one that predates context notes. The brain sends the hello as soon as
// it accepts the socket, before anything slow (gateway_server.py), so this is only ever
// reached by a brain that sends none.
export const HELLO_TIMEOUT_MS = 15000;
// How long after the hello a bridge waits for "ready" before giving up on notes for
// the session. The brain says ready once its slot is free (acquire_slot, up to ~5 s),
// its pipeline runs and greet() found the STT (an unreachable engine takes ~10 s to
// fail, and then the session ends instead). Past this the brain is wedged.
export const READY_TIMEOUT_MS = 30000;
// How long a request waits for a bridge whose binding is still pending (a microtask
// after createBridge; see TalkSessions.add) before reporting bridge_not_ready.
const BIND_WAIT_MS = 500;

function invalid(message, reason) {
  return { code: "INVALID_REQUEST", message, details: { reason } };
}

function unavailable(message, reason, retryAfterMs) {
  return {
    code: "UNAVAILABLE",
    message,
    retryable: true,
    ...(retryAfterMs !== undefined ? { retryAfterMs } : {}),
    details: { reason },
  };
}

const notOwner = () => invalid("Talk session is not owned by this connection", "not_owner");
const hostUnsupported = () =>
  invalid(
    "this OpenClaw does not expose its Talk session registry; context notes need OpenClaw 2026.9 or newer",
    "host_unsupported",
  );

/** Length in code points, which is what the brain's len() counts. */
function chars(text) {
  let n = 0;
  for (const _ of text) n += 1;
  return n;
}

/** The brain's announced context limits, in the gateway's camelCase, or null. */
function contextLimits(features) {
  const c = features && features.context;
  if (!c || typeof c !== "object") return null;
  const brainMax = Number(c.max_chars);
  return {
    maxChars: Number.isFinite(brainMax) && brainMax > 0 ? Math.min(brainMax, MAX_TEXT_CHARS) : MAX_TEXT_CHARS,
    maxNotes: c.max_notes,
    respondIntervalMs: Math.round(Number(c.respond_interval_s) * 1000),
  };
}

/**
 * Where a bridge's brain stands on context notes:
 *   "connecting"  - the socket is not open yet; or the brain has not sent its hello and
 *                   still may (within helloTimeoutMs of the open); or it announced
 *                   context notes and has not said "ready" yet (its pipeline and STT are
 *                   still coming up, or the session is ending because the STT is not
 *                   there, and then "closed" follows);
 *   "ready"       - the brain takes notes now;
 *   "unsupported" - the hello announced none, or none came in time (a brain that
 *                   predates context notes never sends one), or "ready" never
 *                   followed the hello within readyTimeoutMs;
 *   "closed"      - the session is over.
 */
export function contextState(
  bridge,
  { helloTimeoutMs = HELLO_TIMEOUT_MS, readyTimeoutMs = READY_TIMEOUT_MS, now = Date.now } = {},
) {
  if (bridge.hasEnded()) return "closed";
  const features = bridge.brainFeatures();
  if (features !== null && features !== undefined) {
    if (!contextLimits(features)) return "unsupported";
    if (bridge.brainReady()) return "ready";
    const helloAt = bridge.helloAt();
    return helloAt !== null && helloAt !== undefined && now() - helloAt >= readyTimeoutMs
      ? "unsupported"
      : "connecting";
  }
  const openedAt = bridge.openedAt();
  if (openedAt === null || openedAt === undefined) return "connecting";
  return now() - openedAt < helloTimeoutMs ? "connecting" : "unsupported";
}

/**
 * The live teaport bridges and the Talk sessions they serve.
 * @param {{currentConnId?: () => string|undefined,
 *          talkRegistry?: () => Map<string, object>|undefined}} hooks
 *   currentConnId: the gateway connection of the request running now.
 *   talkRegistry: the host's Talk session registry (sessionId -> {kind, connId, ...}),
 *   or undefined when this host does not expose one (then nothing is ever bound).
 */
export class TalkSessions {
  constructor({ currentConnId, talkRegistry } = {}) {
    this._currentConnId = currentConnId || (() => undefined);
    this._registry = talkRegistry || (() => undefined);
    this._live = []; // bridges not yet ended, oldest first
    this._bySession = new Map(); // sessionId -> entry
    this._closed = []; // recently ended session ids
  }

  /** Called by createBridge. Returns the callback the bridge calls when it ends. */
  add(bridge) {
    let connId;
    try {
      connId = this._currentConnId();
    } catch {
      connId = undefined;
    }
    // A bridge made outside a gateway request has no owner anyone could check, so it
    // is never bound, and notes cannot reach it.
    const entry = { bridge, connId, sessionId: undefined, claiming: false, ended: false };
    this._live.push(entry);
    const registry = connId ? this._registry() : undefined;
    if (registry) {
      // Bind at creation (the module comment). The record lands after this returns,
      // in the same synchronous run, so look for it in a microtask.
      const before = new Set(registry.keys());
      entry.claiming = true;
      queueMicrotask(() => {
        entry.claiming = false;
        this._claim(entry, before);
      });
    }
    return () => this._ended(entry);
  }

  /** Whether this host exposes the Talk session registry notes depend on. */
  hostSupported() {
    return Boolean(this._registry());
  }

  _claim(entry, before) {
    if (entry.ended) return;
    const registry = this._registry();
    if (!registry) return;
    // In insertion order: the first new record is the one made for this bridge. A
    // later one on the same connection belongs to a bridge made after this one.
    for (const [id, record] of registry) {
      if (before.has(id) || this._bySession.has(id) || this._closed.includes(id)) continue;
      if (!record || record.kind !== "realtime-relay" || record.connId !== entry.connId) continue;
      this._bind(entry, id);
      return;
    }
  }

  _bind(entry, sessionId) {
    entry.sessionId = sessionId;
    this._bySession.set(sessionId, entry);
  }

  _ended(entry) {
    entry.ended = true;
    this._live = this._live.filter((e) => e !== entry);
    if (entry.sessionId === undefined) return;
    this._bySession.delete(entry.sessionId);
    this._closed.push(entry.sessionId);
    if (this._closed.length > CLOSED_IDS_KEPT) this._closed.shift();
  }

  /** The bridge serving `sessionId` for the caller on `connId`, or {error}. */
  resolve(sessionId, connId) {
    // As OpenClaw's requireUnifiedTalkSessionConn: no connection, no ownership.
    if (!connId) return { error: notOwner() };
    const bound = this._bySession.get(sessionId);
    if (bound) {
      if (bound.connId !== connId) return { error: notOwner() };
      return { bridge: bound.bridge };
    }
    if (this._closed.includes(sessionId)) {
      return { error: invalid("Talk session is closed", "closed_session") };
    }
    const registry = this._registry();
    if (!registry) return { error: hostUnsupported() };
    const record = registry.get(sessionId);
    if (!record) return { error: invalid("Unknown Talk session", "unknown_session") };
    if (record.kind !== "realtime-relay") {
      return {
        error: invalid("teaport.talk.context needs a gateway-relay realtime session", "not_relay"),
      };
    }
    if (record.connId !== connId) return { error: notOwner() };
    // A live relay session of this connection that no teaport bridge serves: either
    // its bridge is a microtask away from binding, or it is another provider's.
    if (this._live.some((e) => e.claiming && e.connId === connId)) {
      return {
        error: unavailable("the voice session's bridge is not bound yet", "bridge_not_ready", 100),
      };
    }
    return {
      error: invalid("this Talk session is not served by the teaport voice provider", "no_voice_session"),
    };
  }

  /** resolve(), waiting out a binding still pending (bounded by BIND_WAIT_MS). */
  async resolveSettled(sessionId, connId) {
    const deadline = Date.now() + BIND_WAIT_MS;
    for (;;) {
      const found = this.resolve(sessionId, connId);
      if (found.error?.details?.reason !== "bridge_not_ready" || Date.now() >= deadline) return found;
      await new Promise((r) => setTimeout(r, 10));
    }
  }
}

function parseContextParams(params) {
  const p = params && typeof params === "object" ? params : {};
  const { sessionId, text, respond, kind } = p;
  if (typeof sessionId !== "string" || !sessionId.trim() || sessionId.length > MAX_SESSION_ID_CHARS) {
    return { error: invalid("sessionId must be the relaySessionId of a Talk session", "bad_params") };
  }
  if (typeof text !== "string" || !text.trim()) {
    return { error: invalid("text must be a non-empty string", "bad_params") };
  }
  const trimmed = text.trim();
  if (chars(trimmed) > MAX_TEXT_CHARS) {
    return { error: invalid(`text is longer than ${MAX_TEXT_CHARS} characters`, "too_long") };
  }
  if (respond !== undefined && typeof respond !== "boolean") {
    return { error: invalid("respond must be a boolean", "bad_params") };
  }
  if (kind !== undefined && (typeof kind !== "string" || kind.length > MAX_KIND_CHARS)) {
    return { error: invalid(`kind must be a string of at most ${MAX_KIND_CHARS} characters`, "bad_params") };
  }
  return { sessionId: sessionId.trim(), text: trimmed, respond: respond === true, kind };
}

/** The brain's refusal of a note, as a gateway error. */
function brainError(ack) {
  const message = typeof ack.message === "string" && ack.message ? ack.message : "note refused";
  if (ack.error === "rate_limited") {
    const retry = Number.isFinite(ack.retry_after_ms) ? ack.retry_after_ms : undefined;
    return unavailable(message, "rate_limited", retry);
  }
  return invalid(message, typeof ack.error === "string" ? ack.error : "refused");
}

/**
 * The gateway method handlers, keyed by method name, for api.registerGatewayMethod.
 * @param {TalkSessions} sessions
 * @param {{helloTimeoutMs?: number, now?: () => number}} [options] for tests
 */
export function contextMethods(sessions, options = {}) {
  return {
    [CONTEXT_METHOD]: async ({ params, client, respond }) => {
      const p = parseContextParams(params);
      if (p.error) return respond(false, undefined, p.error);
      const found = await sessions.resolveSettled(p.sessionId, client?.connId);
      if (found.error) return respond(false, undefined, found.error);
      const { bridge } = found;
      // Only once the brain has said "ready". Before it, a note would wait in the socket
      // while the brain sets up (acquire_slot waits out the previous session's
      // teardown), and could time out here yet still be applied there, so a retry
      // would add it twice; or it would be acked for a session about to end.
      switch (contextState(bridge, options)) {
        case "closed":
          return respond(false, undefined, invalid("Talk session is closed", "closed_session"));
        case "connecting":
          return respond(false, undefined, unavailable("the voice session is still connecting", "connecting", 500));
        case "unsupported":
          return respond(
            false,
            undefined,
            invalid("this session's teaport brain does not take context notes", "unsupported"),
          );
        default:
          break;
      }
      const limits = contextLimits(bridge.brainFeatures());
      const length = chars(p.text);
      if (length > limits.maxChars) {
        return respond(
          false,
          undefined,
          invalid(`the note is ${length} characters; the limit is ${limits.maxChars}`, "too_long"),
        );
      }
      let ack;
      try {
        ack = await bridge.sendContext({ text: p.text, respond: p.respond, kind: p.kind });
      } catch (err) {
        if (bridge.hasEnded()) {
          return respond(
            false,
            undefined,
            invalid("Talk session closed before the brain answered the note", "closed_session"),
          );
        }
        const message = err instanceof Error ? err.message : String(err);
        return respond(false, undefined, unavailable(message, "brain_unavailable"));
      }
      if (!ack || ack.ok !== true) return respond(false, undefined, brainError(ack || {}));
      return respond(true, { ok: true, status: ack.status === "queued" ? "queued" : "applied" });
    },

    [CAPABILITIES_METHOD]: async ({ params, client, respond }) => {
      // context is null on a host that could never route a note (no Talk registry).
      const result = {
        ok: true,
        context: sessions.hostSupported() ? { method: CONTEXT_METHOD, version: 1, respond: true } : null,
      };
      const sessionId = params && typeof params === "object" ? params.sessionId : undefined;
      if (typeof sessionId === "string" && sessionId.trim() && !sessions.hostSupported()) {
        // The same answer as without a sessionId: notes cannot work on this host.
        result.session = { sessionId: sessionId.trim(), state: "unsupported", context: null };
      } else if (typeof sessionId === "string" && sessionId.trim()) {
        const id = sessionId.trim();
        const found = await sessions.resolveSettled(id, client?.connId);
        if (found.error) return respond(false, undefined, found.error);
        const state = contextState(found.bridge, options);
        if (state === "closed") {
          return respond(false, undefined, invalid("Talk session is closed", "closed_session"));
        }
        // context is null unless state is "ready". "connecting" is worth asking about
        // again; "unsupported" is final for this session.
        result.session = {
          sessionId: id,
          state,
          context: state === "ready" ? contextLimits(found.bridge.brainFeatures()) : null,
        };
      }
      return respond(true, result);
    },
  };
}
