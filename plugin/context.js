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
// module ties the two together itself:
//   * At createBridge, which runs inside the client's talk.session.create request, the
//     bridge records the connection that created it (index.js reads it from the
//     gateway request scope).
//   * The first note for a sessionId binds it to that connection's newest live
//     teaport bridge. The brain serves one session at a time (a new /talk connection
//     evicts the old one), so in practice there is exactly one.
//   * Where the host exposes its Talk session registry (OpenClaw 2026.9), the id is
//     checked against it first: unknown ids and other connections' sessions are
//     refused before anything is bound.
//
// This module imports nothing from the OpenClaw SDK, like provider.js, so it can be
// tested standalone (test/context.test.mjs).

export const CONTEXT_METHOD = "teaport.talk.context";
export const CAPABILITIES_METHOD = "teaport.talk.capabilities";

// Transport sanity bounds. The brain enforces the real limits (TEAPORT_CONTEXT_*)
// and announces them in its hello; these only keep garbage off the socket before it has.
const MAX_TEXT_CHARS = 16000;
const MAX_KIND_CHARS = 64;
const MAX_SESSION_ID_CHARS = 256;
// Closed session ids remembered so a late note hears "closed" rather than "unknown".
const CLOSED_IDS_KEPT = 64;

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

/** The brain's announced context limits, in the gateway's camelCase. */
function contextLimits(features) {
  const c = features && features.context;
  if (!c || typeof c !== "object") return null;
  return {
    maxChars: c.max_chars,
    maxNotes: c.max_notes,
    respondIntervalMs: Math.round(Number(c.respond_interval_s) * 1000),
  };
}

/**
 * The live teaport bridges and the Talk sessions they serve.
 * @param {{currentConnId?: () => string|undefined,
 *          lookupTalkSession?: (id: string) => object|null|undefined}} hooks
 *   currentConnId: the gateway connection of the request running now.
 *   lookupTalkSession: the host's record for a Talk session id; null when there is
 *   none, undefined when the host does not expose one (then only connections are
 *   checked).
 */
export class TalkSessions {
  constructor({ currentConnId, lookupTalkSession } = {}) {
    this._currentConnId = currentConnId || (() => undefined);
    this._lookup = lookupTalkSession || (() => undefined);
    this._live = []; // bridges not yet ended, oldest first
    this._bySession = new Map(); // sessionId -> bridge
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
    const entry = { bridge, connId, sessionId: undefined };
    this._live.push(entry);
    return () => this._ended(entry);
  }

  _ended(entry) {
    this._live = this._live.filter((e) => e !== entry);
    if (entry.sessionId === undefined) return;
    this._bySession.delete(entry.sessionId);
    this._closed.push(entry.sessionId);
    if (this._closed.length > CLOSED_IDS_KEPT) this._closed.shift();
  }

  /** The bridge serving `sessionId` for the caller on `connId`, or {error}. */
  resolve(sessionId, connId) {
    const owned = (c) => !c || !connId || c === connId;
    const bound = this._bySession.get(sessionId);
    if (bound) {
      if (!owned(bound.connId)) {
        return { error: invalid("Talk session is not owned by this connection", "not_owner") };
      }
      return { bridge: bound.bridge };
    }
    if (this._closed.includes(sessionId)) {
      return { error: invalid("Talk session is closed", "closed_session") };
    }
    const record = this._lookup(sessionId);
    if (record === null) return { error: invalid("Unknown Talk session", "unknown_session") };
    if (record) {
      if (record.kind !== "realtime-relay") {
        return {
          error: invalid("teaport.talk.context needs a gateway-relay realtime session", "not_relay"),
        };
      }
      if (!owned(record.connId)) {
        return { error: invalid("Talk session is not owned by this connection", "not_owner") };
      }
    }
    const entry = this._live.filter((e) => e.sessionId === undefined && owned(e.connId)).at(-1);
    if (!entry) {
      return {
        error: invalid("No live teaport voice session for this Talk session", "unknown_session"),
      };
    }
    entry.sessionId = sessionId;
    this._bySession.set(sessionId, entry);
    return { bridge: entry.bridge };
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
  if (text.length > MAX_TEXT_CHARS) {
    return { error: invalid(`text is longer than ${MAX_TEXT_CHARS} characters`, "too_long") };
  }
  if (respond !== undefined && typeof respond !== "boolean") {
    return { error: invalid("respond must be a boolean", "bad_params") };
  }
  if (kind !== undefined && (typeof kind !== "string" || kind.length > MAX_KIND_CHARS)) {
    return { error: invalid(`kind must be a string of at most ${MAX_KIND_CHARS} characters`, "bad_params") };
  }
  return { sessionId: sessionId.trim(), text: text.trim(), respond: respond === true, kind };
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
 */
export function contextMethods(sessions) {
  return {
    [CONTEXT_METHOD]: async ({ params, client, respond }) => {
      const p = parseContextParams(params);
      if (p.error) return respond(false, undefined, p.error);
      const found = sessions.resolve(p.sessionId, client?.connId);
      if (found.error) return respond(false, undefined, found.error);
      const { bridge } = found;
      if (!bridge.isConnected()) {
        return respond(
          false,
          undefined,
          bridge.hasEnded()
            ? invalid("Talk session is closed", "closed_session")
            : unavailable("the voice session is still connecting", "connecting", 500),
        );
      }
      // The brain's own limit, when it has said what it is, refused here rather than
      // after a round trip.
      const limits = contextLimits(bridge.brainFeatures());
      if (limits && Number.isFinite(limits.maxChars) && p.text.length > limits.maxChars) {
        return respond(
          false,
          undefined,
          invalid(`the note is ${p.text.length} characters; the limit is ${limits.maxChars}`, "too_long"),
        );
      }
      let ack;
      try {
        ack = await bridge.sendContext({ text: p.text, respond: p.respond, kind: p.kind });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        return respond(false, undefined, unavailable(message, "brain_unavailable"));
      }
      if (!ack || ack.ok !== true) return respond(false, undefined, brainError(ack || {}));
      return respond(true, { ok: true, status: ack.status === "queued" ? "queued" : "applied" });
    },

    [CAPABILITIES_METHOD]: async ({ params, client, respond }) => {
      const result = {
        ok: true,
        context: { method: CONTEXT_METHOD, version: 1, respond: true },
      };
      const sessionId = params && typeof params === "object" ? params.sessionId : undefined;
      if (typeof sessionId === "string" && sessionId.trim()) {
        const found = sessions.resolve(sessionId.trim(), client?.connId);
        if (found.error) return respond(false, undefined, found.error);
        const limits = contextLimits(found.bridge.brainFeatures());
        // null: this session's brain has not announced context notes (an older brain,
        // or one still connecting); notes to it will not be answered.
        result.session = { sessionId: sessionId.trim(), context: limits };
      }
      return respond(true, result);
    },
  };
}
