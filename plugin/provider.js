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
// A delta is only correct against the bubble the view has open, and a final only
// replaces a bubble whose text it extends. So on 2026.8.1+ TalkTranscriptAdapter
// opens and closes every assistant bubble itself and always knows what it holds:
//   * A user transcript cuts the open bubble. The adapter first sends a final of
//     exactly its text. The view would commit the bubble at the user's first words
//     anyway, but without a final; sending one saves what the bubble showed to the
//     session transcript and leaves no doubt the bubble is closed.
//   * If the voice carries on after the cut, the next bubble starts where the cut
//     one ended, and the utterance's final carries only the text after the cut.
//   * A final that is not part of the utterance (a tool card, a debug chip), or a
//     new utterance, also cuts the open bubble, so nothing merges into it.
// The brain tags its captions with their utterance (the TTS context id), which is
// how a carry-on is told apart from a new reply that begins with the same words.
// Untagged text (an older brain) counts as a carry-on when it extends the text sent.

const ASSISTANT_DELTAS_SINCE = [2026, 8, 1];

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

/**
 * Turns the brain's full-text transcripts into what the host's Talk view needs.
 * adapt() returns the [role, text, final] events to hand onTranscript, in order.
 */
export class TalkTranscriptAdapter {
  constructor({ assistantDeltas = true } = {}) {
    this._deltas = assistantDeltas;
    this._utt = null; // the utterance being shown: its id, "" if untagged, null if none
    this._said = ""; // all of its text sent to the view so far
    this._base = 0; // how much of _said is in closed bubbles
    this._open = false; // the view has a bubble open with the rest of _said
  }

  adapt(role, text, final, utterance) {
    const full = typeof text === "string" ? text.trim() : "";
    // The view ignores empty text, so it changes nothing there.
    if (!this._deltas || !full) return [[role, full, final]];
    if (role !== "assistant") return [...this._cut(), [role, full, final]];
    const id = typeof utterance === "string" && utterance ? utterance : null;
    const out = [];
    if (!this._continues(id, full)) {
      out.push(...this._cut());
      // An untagged final that is not part of the utterance is a card of its own;
      // the utterance carries on after it.
      if (final && !id) return [...out, [role, full, true]];
      this._utt = id ?? "";
      this._said = "";
      this._base = 0;
    }
    if (final) {
      // The final carries what is not in a closed bubble yet, and ends the utterance.
      const closed = this._said.slice(0, this._base);
      const rest = full.startsWith(closed) ? full.slice(this._base).trim() : full;
      this._utt = null;
      this._said = "";
      this._base = 0;
      this._open = false;
      return rest ? [...out, [role, rest, true]] : out;
    }
    // Text sent can't be taken back; the brain only ever extends an utterance.
    if (!full.startsWith(this._said)) return out;
    const add = this._open
      ? full.slice(this._said.length)
      : full.slice(this._base).trimStart(); // a new bubble: what follows the cut
    this._said = full;
    if (!add) return out;
    this._open = true;
    return [...out, [role, add, false]];
  }

  _continues(id, full) {
    if (this._utt === null) return false;
    return id ? id === this._utt : full.startsWith(this._said);
  }

  // Close the open bubble with a final of exactly its text: the view keeps the text
  // and ends the bubble. What it showed becomes the utterance's closed part.
  _cut() {
    if (!this._open) return [];
    this._open = false;
    const shown = this._said.slice(this._base).trim();
    this._base = this._said.length;
    return [["assistant", shown, true]];
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
          // an OpenClaw that appends assistant partials (2026.8.1+), shapes the
          // assistant bubbles (see TalkTranscriptAdapter): one brain message can
          // become several events, or none. Trimming matters for the user role:
          // the brain's Voxtral interims are a cumulative buffer of SentencePiece
          // deltas, so each begins with a leading space (" What", " What is", ...),
          // and the Talk reducer APPENDS whitespace-leading user text instead of
          // replacing the turn ("What What is What is the ..."). Finals always stay
          // the complete text, so the turn still closes and the LLM responds.
          const events = this._transcripts.adapt(msg.role, msg.text, Boolean(msg.final), msg.utterance);
          for (const [role, text, final] of events) {
            if (TRACE) {
              console.log(`[DBLTRACE-plugin] forward role=${role} final=${final} text=${JSON.stringify(text.slice(0, 45))}`);
            }
            this._req.onTranscript(role, text, final);
          }
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
