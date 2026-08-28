<!-- SPDX-License-Identifier: MIT -->
# SIP brain (M2) — offline acceptance run log

Autonomous, offline end-to-end test of the teaport-sip brain client: a fake
gateway drives a scripted call over an AF_UNIX SOCK_SEQPACKET socket, and the
real brain pipeline (Voxtral STT → LLM → Kokoro TTS) answers. No live phone call,
no user in the loop, no engine mutation.

## Setup

- **Target:** Jetson appliance (`teaspoon@192.168.1.234`), pipecat **1.7.0**,
  `/opt/teaport/venv` (unmodified — run via `PYTHONPATH=$HOME/teaport-src/brain`).
- **Engine:** `ws://127.0.0.1:8000/v1/realtime` (Voxtral STT + Kokoro TTS, 24 kHz).
- **LLM:** `LLM_BASE_URL=https://openrouter.ai/api/v1`, `LLM_MODEL=qwen/qwen3.8-27b`
  (env from `/etc/teaport/brain.env`, loaded literally like systemd does).
- **Sockets:** brain (client) → fake gateway (server) on the TEST socket
  `/tmp/teaport-fakegw.sock`. The LIVE `/tmp/teaport-sip.sock` (real teaport-sip
  gateway + its `stub_brain` echo) was **not touched**.
- **Question WAV:** `brain/test/question.wav` — 3.42 s, 16 kHz mono, real speech
  synthesized via the engine TTS (`make_question_wav.py`):
  *"What is the capital of France, and is it a large city?"*

## Command

```
# on the Jetson
python3 brain/test/fake_gateway.py --socket /tmp/teaport-fakegw.sock \
        --wav brain/test/question.wav --out-wav /tmp/sip-audio-out.wav \
        --greet-wait 8 --reply-idle 2 --reply-cap 40 &
PYTHONPATH=$HOME/teaport-src/brain /opt/teaport/venv/bin/python \
        -m teaport_brain.sip_server --socket /tmp/teaport-fakegw.sock
```

## Result: PASS

### Control handshake (tag `0x01`)
```
[gw->brain] hello {proto:0, role:gateway, codec:s16le, rate:16000, channels:1, ptime_ms:20}
[gw->brain] call.incoming {call_id:fakecall-0001, from:sip:tester@fake, to:sip:100v@fake}
[gw->brain] call.state   {call_id:fakecall-0001, state:confirmed}
[brain->gw] hello        {proto:0, role:brain}          # optional ack, sent by the transport
[gw->brain] call.state   {call_id:fakecall-0001, state:disconnected}
```

### Greeting on `call.state=confirmed` (LLM turn)
```
EngineTTSService: engine TTS [Hey there!]
EngineTTSService: engine TTS [Good to hear you.]
LEDGER +assistant: [3.6-8.8] 'Hey there! Good to hear you.'
fake gateway: greeting audio.out so far: 96 frames
```

### STT transcript of the spoken question (real Voxtral)
```
LEDGER +user OVERLAP: [8.3-11.7] 'What is the capital of France, and is it a large city?'
```
(Exact transcript. `OVERLAP` because the question began while the greeting was
still playing out — the barge-in guard fired and the interruption reached TTS,
so this run also exercised barge-in.)

### LLM reply text + TTS
```
EngineTTSService: engine TTS [The capital of France is Paris, and yes, it's a
    major city with about nine million people right in the urban area.]
EngineTTSService: run_tts done — 6.8s audio
```

### audio.out returned to the gateway (tag `0x11`, 640 B / 20 ms frames)
```
RESULT audio.out: 432 frames, 276480 bytes, 8.64s   (greeting ~96 + reply ~336)
RESULT wrote     : /tmp/sip-audio-out.wav   (16 kHz mono, 8.64 s)
PASS: non-trivial audio.out reply received
```
Recorded reply copied into the repo as `RUNLOG-sip-offline-audio-out.wav`
(16 kHz mono, 8.64 s) for inspection.

### Clean teardown
- `call.state=disconnected` reset the LLM context (pipeline kept running); socket
  EOF then stopped the pipeline; STT disconnected cleanly (single engine slot freed).
- No leftover `sip_server` / `fake_gateway` processes.
- Live `teaport-sip` gateway (PID unchanged) + `stub_brain` + `/tmp/teaport-sip.sock`
  untouched; `teaport-brain` and `teaport-engine` services still `active`.
- Peak memory during the run: ~1.0 GB available throughout (no pressure).

## Deferred vs. the OpenClaw pipeline (gateway_server.py)

Reused: shared `services.make_stt/make_llm/make_tts`, persona + VOICE_OVERLAY,
Silero VAD + Smart-Turn endpointing, min-words barge-in guard, the heard-grounding
`TranscriptLedger` (observer) + `HeardContextCorrector`.

Deferred for M2 (documented, not wired): tools / `ask_openclaw` consult (no SIP
control type carries a consult round-trip yet), memory recall + reclaim, captions
/ transcript emitters (protocol v0 has no transcript/caption control message),
thinking-sound bed, follow-up gate, turn-timing taps. Also: on barge-in the brain
stops sending audio but cannot flush the gateway's playout queue (protocol v0 has
no "clear" control), so up to ~1 s of already-queued audio can still play out.
