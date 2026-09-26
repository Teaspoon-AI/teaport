// teaport realtime-voice provider for OpenClaw.
//
// Bridges OpenClaw's gateway-relay Talk path to an external Pipecat speech-to-speech
// server (brain/teaport_brain/gateway_server.py). OpenClaw drives this as a
// bridge-only provider: the gateway calls createBridge({...}) then connect(), pumps
// the user's PCM16/24k mic audio in via sendAudio(Buffer), and relays our
// onAudio / onClearAudio / onTranscript back to the Talk client. Pipecat owns the
// whole brain (STT + LLM + TTS) and the heard-grounded barge-in.
//
// Plain ESM, no build step. Uses Node's global WebSocket (Node >= 22) and Buffer.
//
// This module deliberately imports nothing from the OpenClaw SDK, so the bridge can
// be unit-tested standalone (see test/bridge_harness.mjs). index.js wires it to
// `definePluginEntry` / `api.registerRealtimeVoiceProvider`.

const PCM16_24K = { encoding: "pcm16", sampleRateHz: 24000, channels: 1 };

// TEAPORT_TRACE=1 enables verbose transcript-forwarding traces (debug aid; the
// traced text is user speech, so keep this off in normal operation).
const TRACE = /^(1|true)$/i.test(process.env.TEAPORT_TRACE || "");

/**
 * Append OpenClaw-selected TTS voice/language — and the gateway auth token — to the
 * brain's WS URL as query params. The Pipecat brain reads ?voice=&language=&token=
 * per session (gateway_server.py). A Kokoro voice's prefix implies its language, so
 * voice alone usually suffices; language is an optional phonemizer override. Voice
 * and language come from talk.realtime.providers.teaport.*; token from the same
 * config or the TEAPORT_GATEWAY_TOKEN env (must match the gateway's GATEWAY_TOKEN).
 */
function withVoiceParams(url, cfg) {
  if (!url) return url;
  const q = [];
  if (cfg && cfg.voice) q.push("voice=" + encodeURIComponent(cfg.voice));
  if (cfg && cfg.language) q.push("language=" + encodeURIComponent(cfg.language));
  if (cfg && cfg.token) q.push("token=" + encodeURIComponent(cfg.token));
  if (!q.length) return url;
  return url + (url.includes("?") ? "&" : "?") + q.join("&");
}

// ── How the Talk view merges transcripts ─────────────────────────────────────
// The brain sends the FULL text on every transcript event. OpenClaw's Control UI
// Talk view merges each event into the open bubble for its role, and since
// OpenClaw 2026.8.1 the two roles merge differently:
//   user       text that extends the bubble REPLACES it; whitespace-leading text is
//              APPENDED (which is why every transcript is trimmed here).
//   assistant  a partial is APPENDED verbatim — a delta, like OpenAI Realtime's
//              output transcript deltas. Only a final replaces the bubble's text,
//              and only when it extends or resembles what the bubble shows.
// So full-text assistant partials stack on 2026.8.1+ ("I'mI'm runningI'm running
// smoothly,…"). Before 2026.8.1 both roles used the user rule, so full text was
// correct there, and it is still what those versions get.
//
// A delta is only correct relative to the view's open bubble, and a new bubble must
// start with the full text so far, or the final (the full utterance) cannot replace
// it. TalkTranscriptAdapter therefore mirrors the view's reducer (the realtime Talk
// conversation state in the Control UI bundle) over the exact event stream this
// plugin forwards: which assistant bubble is open and what it holds, and the open
// user entry, whose next event can commit that assistant bubble.

const ASSISTANT_DELTAS_SINCE = [2026, 8, 1];
// The view's window for a user final that only resembles its entry to still count
// as that entry's final rather than a new turn (OpenClaw's constant).
const USER_FINAL_GRACE_MS = 1500;

/**
 * Whether an OpenClaw host appends assistant transcript partials as deltas, i.e. is
 * 2026.8.1 or newer. A missing or unparsable version counts as current OpenClaw.
 */
export function appendsAssistantDeltas(version) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)/.exec(String(version ?? "").trim());
  if (!m) return true;
  for (let i = 0; i < 3; i += 1) {
    const d = Number(m[i + 1]) - ASSISTANT_DELTAS_SINCE[i];
    if (d !== 0) return d > 0;
  }
  return true;
}

const words = (s) => [...s.toLowerCase().matchAll(/[\p{L}\p{N}]+/gu)].map((w) => w[0]);
const squash = (s) => s.toLowerCase().replace(/\s+/g, " ").trim();

// The view's "same utterance, reworded" test: same first word, and the same second
// word or a long enough shared prefix.
function resembles(a, b) {
  const wa = words(a);
  const wb = words(b);
  if (!wa.length || !wb.length || wa[0] !== wb[0]) return false;
  if (wa.length > 1 && wb.length > 1 && wa[1] === wb[1]) return true;
  const sa = squash(a);
  const sb = squash(b);
  const n = Math.min(sa.length, sb.length);
  let i = 0;
  while (i < n && sa[i] === sb[i]) i += 1;
  return i >= 6 && i / Math.max(1, n) >= 0.45;
}

/**
 * Turns the brain's full-text transcripts into what the host's Talk view needs.
 * adapt() returns the text to hand onTranscript, or null to send nothing.
 */
export class TalkTranscriptAdapter {
  constructor({ assistantDeltas = true, now = Date.now } = {}) {
    this._deltas = assistantDeltas;
    this._now = now;
    this._assistant = null; // text of the view's open assistant bubble, or null
    this._held = false; // that bubble stopped matching the brain's text (see adapt)
    // The view's open user entry: {text, streaming, since}. `text` is the last text
    // sent, which is what the view shows while the brain sends full text.
    this._user = null;
  }

  adapt(role, text, final) {
    const full = typeof text === "string" ? text.trim() : "";
    // The view ignores empty text, so it changes nothing there.
    if (!this._deltas || !full) return full;
    let out = full;
    if (role === "assistant" && !final && this._assistant !== null) {
      if (full.startsWith(this._assistant)) {
        out = full.slice(this._assistant.length);
      } else if (!this._held) {
        // The brain restarted its text while the view's bubble is still open (a
        // barge-in that no user transcript committed). A partial cannot replace
        // text, and only a final closes a bubble — finals are saved to the
        // session transcript, so this never invents one. Break the paragraph
        // once and hold this utterance's partials; its final lands after the break.
        this._held = true;
        out = "\n\n";
      } else {
        out = "";
      }
    }
    if (!out) return null;
    this._observe(role, out, final);
    return out;
  }

  // Apply one forwarded, non-empty event the way the view does.
  _observe(role, text, final) {
    const now = this._now();
    if (role === "assistant") {
      // Any assistant event stops the open user entry streaming; its next event is
      // then checked for a new turn (_newUserTurn).
      if (this._user) this._user = { ...this._user, streaming: false, since: now };
      if (final) this._closeAssistant();
      else this._assistant = this._assistant === null ? text : this._assistant + text;
      return;
    }
    const newTurn = this._user !== null && this._newUserTurn(text, final, now);
    // A user event commits the assistant bubble when it opens a user entry.
    if (this._user === null || newTurn) this._closeAssistant();
    this._user = final ? null : { text, streaming: true, since: null };
  }

  // Whether the view starts a new user entry for this text instead of merging it into
  // the open one. Both texts are trimmed and non-empty, so the view's blank and
  // leading-whitespace cases never apply.
  _newUserTurn(text, final, now) {
    const u = this._user;
    if (u.streaming) return false;
    const prev = u.text;
    return !(
      text.startsWith(prev) ||
      prev.endsWith(text) ||
      (final && now - u.since <= USER_FINAL_GRACE_MS && resembles(prev, text))
    );
  }

  _closeAssistant() {
    this._assistant = null;
    this._held = false;
  }
}

/**
 * Build the RealtimeVoiceProviderPlugin object OpenClaw registers.
 * @param {{url?: string, voice?: string, language?: string, hostVersion?: string}}
 *   defaults  fallback config (mainly for tests); live config arrives per-session as
 *   req.providerConfig (talk.realtime.providers.teaport.*). hostVersion is the
 *   OpenClaw version (index.js passes api.runtime.version); it picks how assistant
 *   partials are sent, see TalkTranscriptAdapter.
 */
export function buildTeaportRealtimeProvider(defaults = {}) {
  const assistantDeltas = appendsAssistantDeltas(defaults.hostVersion);
  return {
    id: "teaport",
    label: "Teaport (on-device Pipecat)",
    capabilities: {
      transports: ["gateway-relay"],
      inputAudioFormats: [PCM16_24K],
      outputAudioFormats: [PCM16_24K],
      supportsBargeIn: true,
      supportsToolCalls: false,
      supportsBrowserSession: false,
    },
    isConfigured: ({ providerConfig }) =>
      Boolean((providerConfig && providerConfig.url) || defaults.url),
    createBridge: (req) => {
      const cfg = req.providerConfig || {};
      const base = cfg.url || defaults.url;
      // voice/language fall back to the plugin defaults if not set per-session.
      const url = withVoiceParams(base, {
        voice: cfg.voice || defaults.voice,
        language: cfg.language || defaults.language,
        token: cfg.token || defaults.token || process.env.TEAPORT_GATEWAY_TOKEN,
      });
      return new TeaportBridge(req, url, new TalkTranscriptAdapter({ assistantDeltas }));
    },
  };
}

// RealtimeVoiceBridge implementation. createBridge() is synchronous and callbacks
// must not fire before it returns, so all WS work starts in connect().
class TeaportBridge {
  constructor(req, url, transcripts) {
    this._req = req; // RealtimeVoiceBridgeCreateRequest (callbacks + providerConfig)
    this._url = url; // ws://<pipecat-host>:7861/talk
    this._transcripts = transcripts; // one per session, like the Talk view's state
    this._ws = null;
    this._open = false;
    this._closed = false; // we initiated close()
    this._errored = false;
    this._queue = []; // audio buffered until the socket opens (sendAudio races connect)
    // The brain delegates heavy requests by emitting an openclaw_agent_consult
    // tool call; the relay runs the agent turn IN-PROCESS and returns the result
    // via submitToolResult below. Declaring continuation support switches the
    // relay's consult machinery on (working responses + final tool_result).
    this.supportsToolResultContinuation = true;
  }

  // Relay -> brain: consult results (and working notices, willContinue=true).
  // The brain resolves its pending ask_openclaw future on the final result.
  submitToolResult(callId, result, options) {
    if (!this._ws || !this._open) return;
    try {
      this._ws.send(JSON.stringify({
        type: "tool_result",
        call_id: callId,
        result: result === undefined ? null : result,
        will_continue: Boolean(options && options.willContinue),
      }));
    } catch (err) {
      if (this._req.onError) {
        this._req.onError(err instanceof Error ? err : new Error(String(err)));
      }
    }
  }

  connect() {
    return new Promise((resolve, reject) => {
      if (!this._url) {
        reject(new Error("teaport provider: missing providerConfig.url"));
        return;
      }
      let ws;
      try {
        ws = new WebSocket(this._url);
      } catch (err) {
        reject(err instanceof Error ? err : new Error(String(err)));
        return;
      }
      ws.binaryType = "arraybuffer";
      this._ws = ws;

      ws.addEventListener("open", () => {
        this._open = true;
        for (const buf of this._queue) this._rawSend(buf);
        this._queue = [];
        if (this._req.onReady) this._req.onReady();
        resolve();
      });
      ws.addEventListener("message", (ev) => this._onMessage(ev.data));
      ws.addEventListener("error", (ev) => {
        this._errored = true;
        const detail = (ev && (ev.error?.message || ev.message)) || "unknown";
        const err = new Error(`teaport provider WS error: ${detail}`);
        // Pre-open: connect() rejection is the failure signal — mark the bridge
        // settled so the WS's follow-up "close" event doesn't ALSO fire onClose
        // for a session the relay never saw succeed. Post-open: report.
        if (!this._open) {
          this._closed = true;
          reject(err);
          return;
        }
        if (this._req.onError) this._req.onError(err);
      });
      ws.addEventListener("close", () => {
        if (this._closed) return; // we initiated it; relay already knows
        this._closed = true;
        this._open = false;
        if (this._req.onClose) this._req.onClose(this._errored ? "error" : "completed");
      });
    });
  }

  _onMessage(data) {
    if (typeof data === "string") {
      let msg;
      try {
        msg = JSON.parse(data);
      } catch {
        return;
      }
      if (msg.type === "clear") {
        if (this._req.onClearAudio) this._req.onClearAudio();
      } else if (msg.type === "transcript") {
        if (this._req.onTranscript) {
          // The brain sends the FULL text every time; the adapter trims it and, for
          // an OpenClaw that appends assistant partials (2026.8.1+), sends those as
          // deltas (see TalkTranscriptAdapter). Trimming matters for the user role:
          // the brain's Voxtral interims are a cumulative buffer of SentencePiece
          // deltas, so each begins with a leading space (" What", " What is", ...),
          // and the Talk reducer APPENDS whitespace-leading user text instead of
          // replacing the turn ("What What is What is the ..."). Finals always stay
          // the complete text, so the turn still closes and the LLM responds.
          const final = Boolean(msg.final);
          const text = this._transcripts.adapt(msg.role, msg.text, final);
          if (TRACE) {
            console.log(`[DBLTRACE-plugin] forward role=${msg.role} final=${final} text=${text === null ? "(nothing new)" : JSON.stringify(text.slice(0, 45))}`);
          }
          if (text !== null) this._req.onTranscript(msg.role, text, final);
        }
      } else if (msg.type === "tool_call") {
        // Surface the brain's tool calls in the OpenClaw Talk UI: the relay's
        // onToolCall emits a tool.call event — the same card the text agent's
        // tool calls render as. "openclaw_agent_consult" is special: it invokes
        // the relay's IN-PROCESS agent-consult machinery (working responses +
        // a final submitToolResult back to the brain) — that is the brain's
        // deliberate delegation path, so it passes through with its call_id.
        // NOTE for the future voice-call (phone) surface: that handler EXECUTES
        // every onToolCall name — informational events must be re-guarded there.
        if (
          this._req.onToolCall &&
          typeof msg.name === "string" &&
          msg.name
        ) {
          const callId = typeof msg.call_id === "string" && msg.call_id ? msg.call_id : undefined;
          this._req.onToolCall({
            itemId: callId,
            callId,
            name: msg.name,
            args: msg.args && typeof msg.args === "object" ? msg.args : {},
          });
        }
      }
      return;
    }
    // binary: bot speech, PCM16/24k -> hand a Buffer to the relay
    if (this._req.onAudio) this._req.onAudio(Buffer.from(data));
  }

  sendAudio(audio) {
    // audio: Buffer of PCM16/24k from the relay. Queue until the socket is open.
    if (this._open && this._ws) this._rawSend(audio);
    else this._queue.push(audio);
  }

  _rawSend(audio) {
    try {
      this._ws.send(audio); // Buffer is a Uint8Array view; sent as a binary frame
    } catch (err) {
      if (this._req.onError) {
        this._req.onError(err instanceof Error ? err : new Error(String(err)));
      }
    }
  }

  setMediaTimestamp(_ts) {
    // Echo / barge-in timing is owned by Pipecat's VAD on the forwarded mic audio.
  }

  handleBargeIn(_options) {
    // Pipecat's VAD detects barge-in from the forwarded audio and sends us back a
    // {"type":"clear"}; we still forward an explicit hint when the relay asks.
    if (this._open && this._ws) {
      try {
        this._ws.send(JSON.stringify({ type: "barge_in" }));
      } catch {
        /* best effort */
      }
    }
  }

  acknowledgeMark() {
    // The relay uses markStrategy "ack-immediately"; nothing to track here.
  }

  close() {
    this._closed = true;
    this._open = false;
    if (this._ws) {
      try {
        this._ws.send(JSON.stringify({ type: "close" }));
      } catch {
        /* socket may already be gone */
      }
      try {
        this._ws.close();
      } catch {
        /* ignore */
      }
    }
  }

  isConnected() {
    return this._open;
  }
}
