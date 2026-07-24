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

/**
 * Build the RealtimeVoiceProviderPlugin object OpenClaw registers.
 * @param {{url?: string, voice?: string, language?: string}} defaults  fallback config
 *   (mainly for tests); live config arrives per-session as req.providerConfig
 *   (talk.realtime.providers.teaport.*).
 */
export function buildTeaportRealtimeProvider(defaults = {}) {
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
      return new TeaportBridge(req, url);
    },
  };
}

// RealtimeVoiceBridge implementation. createBridge() is synchronous and callbacks
// must not fire before it returns, so all WS work starts in connect().
class TeaportBridge {
  constructor(req, url) {
    this._req = req; // RealtimeVoiceBridgeCreateRequest (callbacks + providerConfig)
    this._url = url; // ws://<pipecat-host>:7861/talk
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
          // Trim surrounding whitespace before forwarding. The brain's Voxtral
          // interims are a cumulative buffer of SentencePiece deltas, so each
          // partial begins with a leading space (" What", " What is", ...).
          // OpenClaw's Talk reducer treats any whitespace-leading transcript as a
          // delta to APPEND rather than replace, which stacks the growing partials
          // into one bubble ("What What is What is the ..."). We always send the
          // FULL utterance (never deltas), so trimming keeps each partial a clean
          // full-text replacement of the active turn — and the final stays a
          // complete transcript, so the turn still closes and the LLM responds.
          const text = typeof msg.text === "string" ? msg.text.trim() : "";
          if (TRACE) {
            console.log(`[DBLTRACE-plugin] forward role=${msg.role} final=${Boolean(msg.final)} text=${JSON.stringify(text.slice(0, 45))}`);
          }
          this._req.onTranscript(msg.role, text, Boolean(msg.final));
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
