<!-- SPDX-License-Identifier: MIT -->
# SIP brain — LLM tools wired in — offline acceptance run log

Proves the SIP brain (`sip_server.py`) now answers with **REAL tool results**
instead of hallucinating them. Before this change the SIP path built its
`LLMContext` with **no tools schema**, so on a live call the model saw the tool
*names* in its persona text but had no schema/handlers — and it invented answers
(reported live: it made up the time "eight thirty-three" and claimed "nine and a
half gigabytes free of sixteen", though the box is an **8 GB** Jetson).

Same offline harness as `RUNLOG-sip-offline.md`: a fake teaport-sip gateway drives
a scripted call over an AF_UNIX SOCK_SEQPACKET socket; the real brain pipeline
(Voxtral STT → LLM → Kokoro TTS) answers. No live phone call, no engine mutation.

## What was wired (mirrors `gateway_server.py`)

- `LLMContext(..., tools=build_tools_schema())` — the full tool schema now rides
  the context (get_host_status, get_current_time, web_search, web_fetch,
  search_memory, remember, ask_openclaw, list_voices, switch_voice).
- After the `PipelineTask`: `register_tools(llm, lang=tts.espeak_language,
  tts=tts, followup=_make_consult_followup(task, context, followup_gate))`.
- `_make_consult_followup(task, context, gate)` — verbatim from `gateway_server`;
  drives `ask_openclaw`'s ASYNC path (speaks the consult answer as an unprompted
  follow-up turn when it lands).
- `FollowupGate()` added to the pipeline right after `transport.output()` (holds
  the follow-up turn for a clear moment).
- `ThinkingSound()` added just before `transport.output()` (soft typing bed over a
  long `ask_openclaw` wait; its 24 kHz frames are resampled to the 16 kHz SIP wire
  by the output transport, same as the TTS frames). No-op for the fast tools.
- Kept intact: `HalfDuplexInputGate` and `cancel_on_idle_timeout=False`.

### `ask_openclaw` on SIP (documented degrade — not a full defer)

The async follow-up machinery is fully wired. The **native** in-process consult
(`openclaw_agent_consult` tool_call over the transport) needs an OpenClaw plugin
to service the round-trip; on SIP there is no such plugin, and the `sip_serializer`
correctly **drops** that non-protocol control message (returns `None`). So the
background waiter's 1.5 s ack times out with `fut.working` unset and it degrades to
the **CLI** path `oc.agent_consult(request)` — still off the voice turn, still
delivered via the same follow-up injector. `ask_openclaw` therefore does not crash
and does return an answer when the CLI agent is available; only the faster native
relay path is unavailable on SIP. The fast tools run unchanged.

## Setup

- **Target:** Jetson appliance (`teaspoon@192.168.1.234`), `/opt/teaport/venv`
  (unmodified — run via `PYTHONPATH=$HOME/teaport-src/brain`).
- **Engine:** `ws://127.0.0.1:8000` (Voxtral STT + Kokoro TTS).
- **LLM:** OpenRouter → qwen3 (env from `/etc/teaport/brain.env`, loaded
  **literally** in a read loop, not `source`, so `LLM_EXTRA_BODY` JSON isn't
  mangled — see `run_sip_tools_test.sh`).
- **Sockets:** brain (client) → fake gateway (server) on the TEST socket
  `/tmp/teaport-fakegw.sock`. The LIVE `/tmp/teaport-sip.sock` (real teaport-sip
  gateway) was **not touched**.
- **Question WAV:** `question_host_status.wav` — 4.67 s, 16 kHz mono, real speech
  via the engine TTS: *"What is your host status right now? How much memory do you
  have free?"* (chosen because `get_host_status` returns a live free-memory number
  that is trivially distinguishable from a hallucination).
- Test overrides: `SIP_HALF_DUPLEX=0` (full-duplex offline), `LOGURU_LEVEL=DEBUG`
  (surface the handler's own debug line + real return values).

## Command

```
# on the Jetson — the live SIP brain was stopped first to free the single STT slot
bash /tmp/run_sip_tools_test.sh      # committed as brain/test/run_sip_tools_test.sh
# which runs, with brain.env loaded literally + SIP_HALF_DUPLEX=0 + LOGURU_LEVEL=DEBUG:
#   python3 fake_gateway.py --socket /tmp/teaport-fakegw.sock \
#           --wav question_host_status.wav --out-wav /tmp/sip-tools-audio-out.wav \
#           --greet-wait 14 --reply-idle 3 --reply-cap 75   &
#   PYTHONPATH=$HOME/teaport-src/brain /opt/teaport/venv/bin/python \
#           -m teaport_brain.sip_server --socket /tmp/teaport-fakegw.sock
```

## Result: PASS — tool fired, real value spoken (NOT a hallucination)

### Real host state snapshot at test start (for comparison)
```
MemAvailable:    1003160 kB          # ~979 MB free before the brain loaded
loadavg:         2.34 1.55 1.36
```

### STT split the utterance into two user turns (real Voxtral)
```
{'role': 'user', 'content': 'What is your host status right now?'}
{'role': 'user', 'content': 'How much memory do you have free?'}
```

### The tool ACTUALLY FIRED (not persona-text hallucination)
```
pipecat.services.llm_service:_run_function_call  -  OpenAILLMService#0 Calling
    function [get_host_status:fc_ab9708b8-...] with arguments {}
teaport_brain.tools:_get_host_status:198  -  get_host_status -> {'device':
    'NVIDIA Jetson Orin Nano 8GB', 'memory_available_mb': 758,
    'cpu_load_1min': 2.76, 'decode_ms_per_step': None}
```
The real result was then injected into the LLM context as a `tool` message:
```
{'role': 'tool',
 'content': '{"device": "NVIDIA Jetson Orin Nano 8GB", "memory_available_mb": 758,
             "cpu_load_1min": 2.76, "decode_ms_per_step": null}',
 'tool_call_id': 'fc_ab9708b8-...'}
```
`memory_available_mb: 758` is a **live** reading — lower than the 979 MB snapshot
because the brain + models were now resident; `cpu_load_1min: 2.76` likewise
tracks the rising loadavg. Real, not invented.

### Spoken reply carries the REAL number
```
EngineTTSService: engine TTS [About seven hundred fifty‑eight megabytes of memory
    are free.]
LEDGER +assistant: [20.1-27.6] 'About seven hundred fifty‑eight megabytes of
    memory are free.'
```
"seven hundred fifty‑eight" == `memory_available_mb: 758`, exactly the tool's
return value.

- **What a hallucination looked like (before):** invented "nine and a half
  gigabytes free of sixteen" — a 16 GB machine that does not exist.
- **What the tools-enabled brain said (now):** the real 758 MB free on the real
  "NVIDIA Jetson Orin Nano 8GB", straight from the tool.

### audio.out returned to the gateway (tag `0x11`, 640 B / 20 ms frames)
```
RESULT audio.out: 288 frames, 184320 bytes, 5.76s   (greeting ~113 + reply ~175)
RESULT wrote     : /tmp/sip-tools-audio-out.wav
PASS: non-trivial audio.out reply received
```
Reply audio copied into the repo as `RUNLOG-sip-tools-audio-out.wav` (16 kHz mono).

### Clean teardown
- `call.state=disconnected` reset the LLM context (tools schema preserved —
  `set_messages` leaves `_tools` intact); brain then stopped by PID.
- No leftover `sip_server` / `fake_gateway` processes.
- Live `teaport-sip` gateway + `/tmp/teaport-sip.sock` untouched; `teaport-engine`
  and `teaport-brain` services still `active`.
- **The live SIP brain was left STOPPED** (the parent restarts everything for the
  live test).

## Still deferred vs. the OpenClaw pipeline

Memory recall (`MemoryRecall`) + reclaim (`MemoryReclaim`), captions/transcript
emitters (no SIP control type carries them), turn-timing taps. `ask_openclaw`'s
native relay path (CLI-fallback only on SIP, as above). Barge-in still can't flush
the gateway's ~1 s playout queue (protocol v0 has no "clear" control).
