// teaport — Discord voice bridge (host service).
//
// Discord voice media is RTP over UDP to per-call voice servers; the NemoClaw
// sandbox is TCP-only by design (OpenShell L7 proxy, UDP a permanent non-goal),
// so this bridge runs bare-metal on the host and owns ONLY the media leg:
//
//   Discord voice servers ⇄ UDP ⇄ this bridge ⇄ ws://127.0.0.1:7861/talk (brain)
//
// The Pipecat brain keeps owning the conversation (STT + LLM + TTS + barge-in),
// exactly like the dashboard Talk path — this is just a second microphone and
// speaker for it. The brain is single-session (one Voxtral slot): joining a
// voice call evicts a live dashboard Talk session and vice versa, by design.
//
// v1 behavior: follow ONE allowlisted user. When they join a voice channel in
// the configured guild, the bot joins and bridges; when they leave, it leaves.
// BRIDGE_JOIN_CHANNEL_ID forces a join at startup (smoke tests).
//
// Audio formats:
//   Discord uplink  : 48 kHz stereo Opus → decode → downmix → 2:1 → PCM16 24k mono
//   Discord downlink: PCM16 24k mono ← brain → 1:2 upsample → 48k stereo → Opus
// The uplink is re-clocked: Discord sends NOTHING during silence, but the
// brain's VAD/smart-turn endpointing needs a continuous mic signal to detect
// end-of-utterance — a 20 ms ticker fills gaps with zero frames.
//
// Plain ESM, no build step. Node >= 22 (global WebSocket, like provider.js).

import { readFileSync } from "node:fs";
import { Readable } from "node:stream";

import {
  createAudioPlayer,
  createAudioResource,
  EndBehaviorType,
  entersState,
  joinVoiceChannel,
  NoSubscriberBehavior,
  StreamType,
  VoiceConnectionStatus,
} from "@discordjs/voice";
import { Client, GatewayIntentBits } from "discord.js";
import prism from "prism-media";

// Required: which server to serve and which user to follow into voice.
// No baked defaults — every deployment names its own.
const GUILD_ID = process.env.BRIDGE_GUILD_ID || "";
const FOLLOW_USER_ID = process.env.BRIDGE_FOLLOW_USER_ID || "";
if (!GUILD_ID || !FOLLOW_USER_ID) {
  console.error(
    "discord-bridge: set BRIDGE_GUILD_ID (your Discord server id) and " +
      "BRIDGE_FOLLOW_USER_ID (the user the bot follows into voice channels).",
  );
  process.exit(1);
}
const BRAIN_URL = process.env.BRAIN_URL || "ws://127.0.0.1:7861/talk";
const FORCE_JOIN_CHANNEL_ID = process.env.BRIDGE_JOIN_CHANNEL_ID || "";
const VOICE = process.env.TEAPORT_VOICE || "";
const TOKEN_FILE =
  process.env.DISCORD_BOT_TOKEN_FILE || `${process.env.HOME}/.config/teaport/discord_bot_token`;

const FRAME_MS = 20;
const BRAIN_RATE = 24000;
const DISCORD_RATE = 48000;
const UPLINK_FRAME_BYTES = (BRAIN_RATE / 1000) * FRAME_MS * 2; // 20 ms PCM16 mono @24k = 960 B
const DOWNLINK_FRAME_BYTES = (DISCORD_RATE / 1000) * FRAME_MS * 2 * 2; // 20 ms 48k stereo 16-bit = 3840 B
// Downlink jitter-buffer prime: audio accumulated before playout starts, to
// absorb the brain's delivery jitter so a late chunk doesn't underrun into a
// mid-word gap. Tunable by ear via BRIDGE_PRIME_MS (default 40; was 80 during
// bring-up, when choppiness later traced to an engine race was mistaken for
// underruns). Lower = less latency; 0 = start on the first frame.
const DOWNLINK_PRIME_MS = Math.max(0, Number(process.env.BRIDGE_PRIME_MS ?? 40));
const DOWNLINK_PRIME_BYTES = Math.round(DOWNLINK_PRIME_MS / FRAME_MS) * DOWNLINK_FRAME_BYTES;
const DOWNLINK_IDLE_MS = 60; // delivery gap this long ⇒ utterance settled: drain/flush its tail

function botToken() {
  if (process.env.DISCORD_BOT_TOKEN) return process.env.DISCORD_BOT_TOKEN.trim();
  return readFileSync(TOKEN_FILE, "utf-8").trim();
}

function brainUrl() {
  if (!VOICE) return BRAIN_URL;
  return BRAIN_URL + (BRAIN_URL.includes("?") ? "&" : "?") + "voice=" + encodeURIComponent(VOICE);
}

const log = (...args) => console.log(new Date().toISOString(), ...args);

// ---- sample-rate conversion (speech-grade, pure JS; 2:1 ratios only) -------

// 48k stereo Int16 interleaved → 24k mono: downmix L/R, then average pairs.
function discordToBrain(buf) {
  const samples = buf.length >> 2; // frames of [L,R] Int16
  const out = Buffer.alloc((samples >> 1) * 2);
  for (let i = 0; i + 1 < samples; i += 2) {
    const a = (buf.readInt16LE(i * 4) + buf.readInt16LE(i * 4 + 2)) >> 1;
    const b = (buf.readInt16LE((i + 1) * 4) + buf.readInt16LE((i + 1) * 4 + 2)) >> 1;
    out.writeInt16LE((a + b) >> 1, i);
  }
  return out;
}

// 24k mono Int16 → 48k stereo: linear-interpolated 1:2 upsample, duplicated L/R.
function brainToDiscord(buf) {
  const n = buf.length >> 1;
  const out = Buffer.alloc(n * 8);
  for (let i = 0; i < n; i++) {
    const s = buf.readInt16LE(i * 2);
    const next = i + 1 < n ? buf.readInt16LE((i + 1) * 2) : s;
    const mid = (s + next) >> 1;
    const o = i * 8;
    out.writeInt16LE(s, o);
    out.writeInt16LE(s, o + 2);
    out.writeInt16LE(mid, o + 4);
    out.writeInt16LE(mid, o + 6);
  }
  return out;
}

// ---- downlink jitter buffer -------------------------------------------------

// @discordjs/voice pulls this Readable at its own 20 ms cadence (through the
// opus encoder), so we return exactly one complete 48k-stereo frame per read —
// real speech when buffered, silence when idle. Player-clocked, so no drift.
// Fixes two v1 glitches the naive "write each chunk straight to the stream"
// approach caused:
//   - dropped last word: a sub-frame final chunk used to sit in the encoder
//     until the NEXT turn completed the frame; here it is padded to a full
//     frame and flushed as soon as delivery settles.
//   - choppiness: a small prime buffer absorbs the brain's delivery jitter so a
//     late chunk doesn't underrun into a mid-word gap. Barge-in is a cheap
//     queue flush, not a player teardown/rebuild (which itself glitched audio).
class PacedDownlink extends Readable {
  constructor() {
    super({ highWaterMark: DOWNLINK_FRAME_BYTES * 8 });
    this._buf = Buffer.alloc(0);
    this._playing = false;
    this._lastEnqueue = 0;
    this._silence = Buffer.alloc(DOWNLINK_FRAME_BYTES);
  }

  enqueue(pcm) {
    // Latency probe: stamp the arrival of a reply's FIRST chunk so _read can
    // report how long the prime+pacing held it before playout began.
    if (!this._playing && this._buf.length === 0) this._firstChunkAt = Date.now();
    this._buf = Buffer.concat([this._buf, pcm]);
    this._lastEnqueue = Date.now();
  }

  // Barge-in: drop queued speech; playout falls back to silence immediately.
  flush() {
    this._buf = Buffer.alloc(0);
    this._playing = false;
  }

  _settled() {
    return Date.now() - this._lastEnqueue >= DOWNLINK_IDLE_MS;
  }

  _read() {
    // Prime a little before playing (smooths jitter); a short reply that never
    // reaches the prime threshold still starts once delivery settles.
    if (!this._playing && this._buf.length > 0) {
      if (this._buf.length >= DOWNLINK_PRIME_BYTES || this._settled()) {
        this._playing = true;
        if (this._firstChunkAt) {
          // Bridge-owned downlink cost: first brain chunk → first played frame.
          log(`timing: downlink hold ${Date.now() - this._firstChunkAt}ms (prime+pacing)`);
          this._firstChunkAt = 0;
        }
      }
    }
    let frame = this._silence;
    if (this._playing && this._buf.length > 0) {
      if (this._buf.length >= DOWNLINK_FRAME_BYTES) {
        frame = this._buf.subarray(0, DOWNLINK_FRAME_BYTES);
        this._buf = this._buf.subarray(DOWNLINK_FRAME_BYTES);
      } else if (this._settled()) {
        // Utterance done: pad the final chunk to a whole frame and flush it now.
        frame = Buffer.concat([this._buf, this._silence.subarray(this._buf.length)]);
        this._buf = Buffer.alloc(0);
        this._playing = false; // re-prime for the next utterance
      }
      // else: partial chunk mid-delivery — hold it, emit silence, wait for more.
    }
    this.push(frame);
  }
}

// ---- bridge session ---------------------------------------------------------

let session = null; // { connection, ws, player, teardown() } — single slot

async function startBridge(channel) {
  if (session) {
    log("bridge already active — ignoring join for", channel.id);
    return;
  }
  log(`joining voice channel #${channel.name} (${channel.id})`);
  const connection = joinVoiceChannel({
    channelId: channel.id,
    guildId: channel.guild.id,
    adapterCreator: channel.guild.voiceAdapterCreator,
    selfDeaf: false,
    selfMute: false,
  });

  const cleanup = [];
  const teardown = (reason) => {
    if (!session) return;
    session = null;
    log("bridge teardown:", reason);
    for (const fn of cleanup.reverse()) {
      try {
        fn();
      } catch {
        /* best effort */
      }
    }
  };
  session = { connection, teardown };
  cleanup.push(() => connection.destroy());

  try {
    await entersState(connection, VoiceConnectionStatus.Ready, 20_000);
  } catch (err) {
    log("voice connection never became ready:", err.message);
    teardown("voice connect failed");
    return;
  }
  log("voice connection ready — dialing brain", BRAIN_URL);

  // --- brain WS ---
  const ws = new WebSocket(brainUrl());
  ws.binaryType = "arraybuffer";
  session.ws = ws;
  cleanup.push(() => {
    try {
      ws.send(JSON.stringify({ type: "close" }));
    } catch {
      /* gone */
    }
    ws.close();
  });

  // --- downlink: brain speech → Discord ---
  // One never-ending paced stream (real speech + silence fill) keeps the player
  // in a steady 20 ms cadence, so playout stays smooth and the last frame of an
  // utterance flushes without waiting for the next turn. Created once; barge-in
  // flushes the buffer rather than tearing the player down.
  const downlink = new PacedDownlink();
  const player = createAudioPlayer({
    behaviors: { noSubscriber: NoSubscriberBehavior.Play },
  });
  player.play(createAudioResource(downlink, { inputType: StreamType.Raw }));
  connection.subscribe(player);
  cleanup.push(() => {
    player.stop();
    downlink.push(null);
  });

  ws.addEventListener("open", () => log("brain session open"));
  let lastVoiceSentAt = 0; // set by the uplink ticker on every REAL mic frame
  ws.addEventListener("message", (ev) => {
    if (typeof ev.data === "string") {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.type === "clear") {
        log("barge-in: clearing queued speech");
        downlink.flush();
      } else if (msg.type === "transcript" && msg.final) {
        log(`transcript[${msg.role}]: ${String(msg.text).slice(0, 120)}`);
      }
      return;
    }
    // Latency probe: a reply's first chunk landing on an idle downlink marks
    // the brain's response latency as seen from the bridge (endpointing + STT
    // + LLM + first TTS). Guarded to recent speech so bot-initiated audio
    // (greetings) doesn't log a bogus figure.
    if (!downlink._playing && downlink._buf.length === 0 && lastVoiceSentAt
        && Date.now() - lastVoiceSentAt < 10_000) {
      log(`timing: brain first audio +${Date.now() - lastVoiceSentAt}ms after last mic frame`);
    }
    downlink.enqueue(brainToDiscord(Buffer.from(ev.data)));
  });
  ws.addEventListener("close", () => {
    // Evicted (e.g. a dashboard Talk session took the single brain slot) or
    // brain restarted. Leave the channel; re-join follows the user's next move.
    teardown("brain socket closed");
  });
  ws.addEventListener("error", (ev) => log("brain socket error:", ev?.message || "unknown"));

  // --- uplink: Discord mic → brain, re-clocked with silence fill ---
  const pending = [];
  const opusStream = connection.receiver.subscribe(FOLLOW_USER_ID, {
    end: { behavior: EndBehaviorType.Manual },
  });
  const decoder = new prism.opus.Decoder({
    rate: DISCORD_RATE,
    channels: 2,
    frameSize: 960,
  });
  opusStream.pipe(decoder);
  decoder.on("data", (pcm48) => pending.push({ buf: discordToBrain(pcm48), t: Date.now() }));
  decoder.on("error", (err) => log("opus decode error:", err.message));
  cleanup.push(() => {
    opusStream.destroy();
    decoder.destroy();
  });

  const silence = Buffer.alloc(UPLINK_FRAME_BYTES);
  let fwdMax = 0; // worst decoder→ws forward delay this stats window
  let backlogMax = 0; // worst pending-queue depth this stats window
  const ticker = setInterval(() => {
    if (ws.readyState !== WebSocket.OPEN) return;
    if (pending.length > backlogMax) backlogMax = pending.length;
    const sendEntry = (entry) => {
      const held = Date.now() - entry.t;
      if (held > fwdMax) fwdMax = held;
      lastVoiceSentAt = Date.now();
      try {
        ws.send(entry.buf);
      } catch {
        /* close handler owns teardown */
      }
    };
    const entry = pending.shift();
    if (entry) {
      sendEntry(entry);
      // Catch-up drain: Discord delivers jitter bursts (measured: 11 frames at
      // talkspurt start), and at one frame per tick a backlog NEVER clears —
      // the whole utterance reaches the brain backlog×20ms late (measured
      // 217ms), delaying endpointing and every downstream step. Send up to two
      // extra frames per tick while behind, always leaving one in reserve so
      // real speech never gets a silence frame spliced into it mid-utterance.
      for (let extra = 0; extra < 2 && pending.length > 1; extra++) {
        sendEntry(pending.shift());
      }
    } else {
      try {
        ws.send(silence);
      } catch {
        /* close handler owns teardown */
      }
    }
  }, FRAME_MS);
  cleanup.push(() => clearInterval(ticker));

  // Latency probe: periodic stats — Discord voice-server RTT (the part no code
  // of ours can remove) + uplink forwarding health (the part that IS ours).
  const stats = setInterval(() => {
    const p = connection.ping ?? {};
    log(`timing: stats ping ws=${p.ws ?? "?"}ms udp=${p.udp ?? "?"}ms | uplink fwd max=${fwdMax}ms backlog max=${backlogMax}`);
    fwdMax = 0;
    backlogMax = 0;
  }, 30_000);
  cleanup.push(() => clearInterval(stats));

  connection.on(VoiceConnectionStatus.Disconnected, () => teardown("voice disconnected"));
}

// ---- discord client ---------------------------------------------------------

const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates],
});

client.on("voiceStateUpdate", (oldState, newState) => {
  if (newState.guild.id !== GUILD_ID) return;
  if (newState.id !== FOLLOW_USER_ID) return;
  if (newState.channelId && !session) {
    const channel = newState.channel;
    if (channel) startBridge(channel).catch((err) => log("startBridge failed:", err.message));
  } else if (!newState.channelId && session) {
    session.teardown("followed user left the channel");
  }
});

client.once("clientReady", async () => {
  log(`logged in as ${client.user.tag}`);
  const guild = await client.guilds.fetch(GUILD_ID).catch(() => null);
  if (!guild) {
    log("bot is not in the configured guild", GUILD_ID);
    return;
  }
  if (FORCE_JOIN_CHANNEL_ID) {
    const channel = await guild.channels.fetch(FORCE_JOIN_CHANNEL_ID).catch(() => null);
    if (channel?.isVoiceBased()) {
      startBridge(channel).catch((err) => log("startBridge failed:", err.message));
    } else {
      log("BRIDGE_JOIN_CHANNEL_ID is not a voice channel:", FORCE_JOIN_CHANNEL_ID);
    }
    return;
  }
  // If the followed user is already mid-call when the service starts, join them.
  const member = await guild.members.fetch(FOLLOW_USER_ID).catch(() => null);
  if (member?.voice?.channel) {
    startBridge(member.voice.channel).catch((err) => log("startBridge failed:", err.message));
  } else {
    log(`waiting for user ${FOLLOW_USER_ID} to join a voice channel`);
  }
});

process.on("SIGTERM", () => {
  session?.teardown("SIGTERM");
  client.destroy();
  process.exit(0);
});

client.login(botToken());
