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
// Talk view merges each event into the open entry for its role, and since
// OpenClaw 2026.7.2 the two roles merge differently:
//   user       text that extends the entry REPLACES it; whitespace-leading text is
//              APPENDED (which is why every transcript is trimmed here).
//   assistant  a partial is APPENDED verbatim — a delta, like OpenAI Realtime's
//              output transcript deltas. Only a final replaces the bubble's text,
//              and only when it extends or resembles what the bubble shows.
// So full-text assistant partials stack ("I'mI'm runningI'm running smoothly,…").
// Before 2026.7.2 both roles used the user rule and took full text. The plugin
// supports OpenClaw 2026.9.1 and newer, which all append; the assistantTranscripts
// setting picks the shape (see pickAssistantTranscripts).
//
// The view closes the open assistant bubble only when a user transcript starts a
// new user entry: the first words after the user's last final, or words that don't
// continue the open user entry once the voice has spoken since. Any other user text
// just updates its entry, and the assistant bubble stays open.
//
// A delta is only correct against the bubble the view has open, and a final only
// replaces a bubble whose text it extends. So in delta mode TalkTranscriptAdapter
// opens and closes every assistant bubble itself and always knows what it holds:
//   * A user transcript that starts a user entry cuts the open bubble. The adapter
//     first sends a final of exactly its text. The view would close the bubble
//     anyway, but without a final; sending one saves what the bubble showed to the
//     session transcript and leaves no doubt the bubble is closed. (The relay also
//     compares user finals of 4+ words with recent assistant finals to drop echo, so
//     that shown text is now in the comparison too. It only gates voice control of a
//     running agent consult, and it helps catch real echo of the words cut into.)
//   * If the voice carries on after the cut, the next bubble starts where the cut
//     one ended, and the utterance's final carries only the text after the cut.
//   * A new utterance cuts the open bubble too, and so does the end of the voice:
//     a barge-in clear (the brain sends no final for a barged utterance) or the
//     session closing.
//   * A final that is not a caption (a tool card, a debug chip) waits while a caption
//     bubble is open, and follows once that bubble is cut or its utterance ends. A
//     tool runs when the model writes the call, but captions follow the audio, so the
//     card would otherwise land mid-sentence ("Let" | card | "me check.").
// The brain tags its captions with their utterance (the TTS context id), which is
// how a carry-on is told apart from a new reply that begins with the same words.
// Untagged text (an older brain) only continues a bubble that is still open.

const ASSISTANT_DELTAS_SINCE = [2026, 7, 2];

/**
 * Whether an OpenClaw host appends assistant transcript partials as deltas, i.e. is
 * 2026.7.2 or newer. A version that says nothing counts as current OpenClaw: a
 * missing or unparsable one (a dev build, or a runtime that would not say), and
 * "0.0.0", which OpenClaw reports when it can't resolve its own version. Full text
 * stacks on a current view, while deltas still read correctly on an older one,
 * apart from a repeated word that can drop out until the final.
 */
export function appendsAssistantDeltas(version) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)/.exec(String(version ?? "").trim());
  if (!m || Number(m[1]) === 0) return true;
  for (let i = 0; i < 3; i += 1) {
    const d = Number(m[i + 1]) - ASSISTANT_DELTAS_SINCE[i];
    if (d !== 0) return d > 0;
  }
  return true;
}

/**
 * How assistant partials reach the Talk view, from the assistantTranscripts setting
 * (talk.realtime.providers.teaport.assistantTranscripts). "delta" or "full" pins the
 * shape, for a client whose view is versioned apart from the gateway or a host the
 * version check gets wrong. "auto", the default, goes by the OpenClaw version, and
 * only then is readVersion() called.
 * Returns { deltas, reason }, the reason for the log.
 */
export function pickAssistantTranscripts(setting, readVersion = () => undefined) {
  if (setting === "delta" || setting === "full") {
    return { deltas: setting === "delta", reason: `assistantTranscripts="${setting}"` };
  }
  let version;
  try {
    version = readVersion();
  } catch {
    version = undefined;
  }
  const auto =
    setting === undefined || setting === null || setting === "auto"
      ? "auto"
      : `auto; ignored assistantTranscripts=${JSON.stringify(setting)}`;
  return {
    deltas: appendsAssistantDeltas(version),
    reason: `${auto}, OpenClaw ${version || "version unknown"}`,
  };
}

const SPACE = /\s/;

// The rest of `text` past `prefix`, or null when `text` does not start with it.
// Whitespace runs only have to line up, not match, so a sentence joined after a
// line break in one message and after a space in the next still continues.
function after(text, prefix) {
  if (text.startsWith(prefix)) return text.slice(prefix.length);
  let i = 0;
  let j = 0;
  while (j < prefix.length) {
    if (SPACE.test(prefix[j])) {
      if (i >= text.length || !SPACE.test(text[i])) return null;
      while (j < prefix.length && SPACE.test(prefix[j])) j += 1;
      while (i < text.length && SPACE.test(text[i])) i += 1;
    } else if (text[i] === prefix[j]) {
      i += 1;
      j += 1;
    } else {
      return null;
    }
  }
  return text.slice(i);
}

/**
 * Turns the brain's full-text transcripts into what the host's Talk view needs.
 * adapt() returns the [role, text, final] events to hand onTranscript, in order;
 * end() closes out the voice once it has stopped for good.
 */
export class TalkTranscriptAdapter {
  constructor({ assistantDeltas = true } = {}) {
    this._deltas = assistantDeltas;
    this._setUtterance(null);
    this._cards = []; // non-caption finals waiting for the open bubble to close
    // The view's open user entry, null once a final closed it: the text it shows
    // (null when unsure) and whether it is still streaming, i.e. no assistant text
    // has reached the view since the entry last changed.
    this._user = null;
    // Full mode: the text of the open assistant bubble, and of the one the user's
    // words last closed.
    this._shown = "";
    this._cutText = "";
  }

  /** How many non-caption finals are held back right now (for the trace). */
  get held() {
    return this._cards.length;
  }

  adapt(role, text, final, utterance) {
    const full = typeof text === "string" ? text.trim() : "";
    // The view ignores empty text, so it changes nothing there.
    if (!full) return [[role, full, final]];
    if (!this._deltas) return this._adaptFull(role, full, final);
    if (role !== "assistant") {
      const starts = this._startsUserEntry(full);
      const out = starts ? this._closeBubble() : [];
      this._user = final ? null : { text: starts ? full : this._userText(full), streaming: true };
      return [...out, [role, full, final]];
    }
    const id = typeof utterance === "string" && utterance ? utterance : null;
    if (final && !id && !this._continues(null, full)) {
      if (!this._open) return this._toView([[role, full, true]]);
      this._cards.push(full);
      return [];
    }
    const out = [];
    if (!this._continues(id, full)) {
      out.push(...this._closeBubble());
      this._setUtterance(id ?? "");
    }
    if (final) {
      // The final carries what is not in a closed bubble yet, and ends the utterance.
      const rest = this._pastCut(full).trim();
      this._setUtterance(null);
      if (rest) out.push([role, rest, true]);
      return this._toView([...out, ...this._release()]);
    }
    // Text sent can't be taken back; the brain only ever extends an utterance.
    const more = after(full, this._said);
    if (more === null) return this._toView(out);
    const add = this._open ? more : this._pastCut(full).trimStart(); // a new bubble: what follows the cut
    this._said = full;
    if (add) {
      this._open = true;
      out.push([role, add, false]);
    }
    return this._toView(out);
  }

  /**
   * The voice stopped and won't carry on: a barge-in clear, or the session ending.
   * Closes the open bubble and sends the cards held behind it.
   */
  end() {
    if (!this._deltas) return [];
    const out = this._closeBubble();
    this._setUtterance(null);
    return out;
  }

  // Full text, for a view that replaces a bubble whose text the new text extends.
  // Such a view closes the assistant bubble at the user's words, and a final that
  // only repeats what that bubble showed would open a duplicate of it after the
  // user's message, so that final is dropped.
  _adaptFull(role, full, final) {
    if (role !== "assistant") {
      if (this._shown) this._cutText = this._shown;
      this._shown = "";
    } else if (!final) {
      this._shown = full;
      this._cutText = "";
    } else {
      this._shown = "";
      if (full === this._cutText) {
        this._cutText = "";
        return [];
      }
    }
    return [[role, full, final]];
  }

  // Show a new utterance: its id, "" when untagged, or null for none.
  _setUtterance(id) {
    this._utt = id;
    this._said = ""; // all of its text sent to the view so far
    this._closed = ""; // the part of _said that is in closed bubbles
    this._open = false; // the view has a bubble open with the rest of _said
  }

  // The part of the utterance's text `full` that is not in a closed bubble yet.
  _pastCut(full) {
    return after(full, this._closed) ?? full;
  }

  _continues(id, full) {
    if (this._utt === null) return false;
    if (id || this._utt) return id === this._utt;
    // Untagged: after a cut, a carry-on can't be told from a new reply that starts
    // with the same words, so only the open bubble continues.
    return this._open && after(full, this._said) !== null;
  }

  // Whether this user text starts a new user entry in the view, which closes the
  // assistant bubble (OpenClaw's updateRealtimeTalkConversation). When the entry's
  // text is unknown, say yes: a needless cut only splits the bubble, while a missed
  // one would send deltas on into a bubble the view has closed. (The view also lets
  // a user final rewrite a closed entry within 1.5 s; the timing is out of reach
  // here, so that case is cut too.)
  _startsUserEntry(full) {
    const u = this._user;
    if (!u) return true;
    if (u.streaming) return false;
    if (u.text === null) return true;
    return !(full.startsWith(u.text) || u.text.endsWith(full));
  }

  // What the open user entry shows once this user text merges into it; null when
  // unsure (the view splices text that neither extends nor repeats the entry).
  _userText(full) {
    const t = this._user?.text;
    if (t == null) return null;
    if (full.startsWith(t)) return full;
    return t.endsWith(full) ? t : null;
  }

  // Assistant text reaching the view stops the open user entry's streaming.
  _toView(events) {
    if (this._user && events.some(([role, text]) => role === "assistant" && text)) {
      this._user.streaming = false;
    }
    return events;
  }

  // Cut the open bubble with a final of exactly its text, so the view keeps the text
  // and ends the bubble (what it showed becomes the utterance's closed part), then
  // send the cards that waited on it.
  _closeBubble() {
    const out = [];
    if (this._open) {
      this._open = false;
      out.push(["assistant", this._pastCut(this._said).trim(), true]);
      this._closed = this._said;
    }
    return this._toView([...out, ...this._release()]);
  }

  _release() {
    const cards = this._cards.map((t) => ["assistant", t, true]);
    this._cards = [];
    return cards;
  }
}

/**
 * Build the RealtimeVoiceProviderPlugin object OpenClaw registers.
 * @param {{url?: string, voice?: string, language?: string, token?: string,
 *   assistantTranscripts?: string, hostVersion?: string | (() => string | undefined),
 *   log?: (msg: string) => void}} defaults
 *   fallback config (mainly for tests); live config arrives per-session as
 *   req.providerConfig (talk.realtime.providers.teaport.*). hostVersion is the
 *   OpenClaw version, or a function that reads it (index.js reads api.runtime.version);
 *   it is read once, when a session first needs it. log reports how each session
 *   sends assistant captions.
 */
export function buildTeaportRealtimeProvider(defaults = {}) {
  let version = null; // { value } once read
  const readVersion = () => {
    if (version === null) {
      let value;
      try {
        value = typeof defaults.hostVersion === "function" ? defaults.hostVersion() : defaults.hostVersion;
      } catch {
        value = undefined; // pickAssistantTranscripts treats it as unknown
      }
      version = { value };
    }
    return version.value;
  };
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
      const { deltas, reason } = pickAssistantTranscripts(
        cfg.assistantTranscripts ?? defaults.assistantTranscripts,
        readVersion,
      );
      defaults.log?.(`teaport-realtime: assistant captions sent as ${deltas ? "deltas" : "full text"} (${reason})`);
      return new TeaportBridge(req, url, new TalkTranscriptAdapter({ assistantDeltas: deltas }));
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
        this._endTranscripts();
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
        // A barge-in: the brain drops the rest of the utterance and sends no final
        // for it, so close its bubble and let out the cards held behind it.
        this._forward(this._transcripts.end());
      } else if (msg.type === "transcript") {
        // The brain sends the FULL text every time; the adapter trims it and, in
        // delta mode, shapes the assistant bubbles (see TalkTranscriptAdapter): one
        // brain message can become several events, or none. Trimming matters for the
        // user role: the brain's Voxtral interims are a cumulative buffer of
        // SentencePiece deltas, so each begins with a leading space (" What",
        // " What is", ...), and the Talk reducer APPENDS whitespace-leading user text
        // instead of replacing the turn ("What What is What is the ..."). A user final
        // is always the complete text, so the turn still closes and the LLM responds;
        // an assistant final can hold just the text after a cut.
        const events = this._transcripts.adapt(msg.role, msg.text, Boolean(msg.final), msg.utterance);
        if (TRACE) {
          const text = typeof msg.text === "string" ? msg.text : "";
          console.log(
            `[DBLTRACE-plugin] in role=${msg.role} final=${Boolean(msg.final)} utt=${msg.utterance ?? "-"} ` +
              `text=${JSON.stringify(text.slice(-45))} -> ${events.length} event(s), ${this._transcripts.held} card(s) held`,
          );
        }
        this._forward(events);
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

  _forward(events) {
    if (!this._req.onTranscript) return;
    for (const [role, text, final] of events) {
      if (TRACE) {
        console.log(`[DBLTRACE-plugin] forward role=${role} final=${final} text=${JSON.stringify(text.slice(0, 45))}`);
      }
      this._req.onTranscript(role, text, final);
    }
  }

  // The session is ending: close the open bubble and send the held cards while the
  // relay still takes transcripts, so they are shown and saved.
  _endTranscripts() {
    try {
      this._forward(this._transcripts.end());
    } catch {
      /* the relay may already be tearing the session down */
    }
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
    if (!this._closed) this._endTranscripts();
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
