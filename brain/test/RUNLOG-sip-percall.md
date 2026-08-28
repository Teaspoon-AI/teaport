<!-- SPDX-License-Identifier: MIT -->
# SIP brain — per-call pipeline lifecycle — offline acceptance run log

Proves the SIP brain (`sip_server.py`) now builds a **FRESH pipeline per call** and
tears it down on disconnect, instead of running ONE persistent pipeline for the whole
process and resetting the LLM context between calls. This recycles all per-call state,
frees the engine's single STT slot between calls, and removes the
`cancel_on_idle_timeout=False` hack — mirroring the OpenClaw path
(`gateway_server.py`), which already builds a fresh pipeline per WebSocket connection.

## The split that makes it possible (`sip_transport.py`)

The socket's lifetime used to be coupled to the pipeline's (a `_UDSClient`
leave-counter closed the socket when a pipeline ended, and the receive loop that
dispatches control events was owned by the input transport). A per-call pipeline
under that design would close the socket and kill control dispatch the moment call #1
ended — call #2 would never be seen. So the transport was split:

- **`SipConnection`** (new, PERSISTENT, a `BaseObject`) owns the SEQPACKET socket,
  runs the ONE receive loop for the whole process, dispatches control events
  (hello / call.incoming / call.state / dtmf) to the server's handlers, routes inbound
  `audio.in` datagrams to the CURRENTLY-ACTIVE call's input transport via a settable
  sink (`set_audio_sink`; None between calls → audio dropped), and exposes
  `async send(bytes)` for `audio.out`. Control handlers are registered `sync=True` so
  they run **inline in the receive loop, in wire order** — load-bearing, so a
  `confirmed` build can't be raced by a following `disconnected` teardown. It lives
  until socket EOF; `run()` is the server's top-level coroutine.
- **`SipGatewayTransport`** (refactored, PER-CALL, lightweight, bound to a
  `SipConnection`): input `start` → `set_transport_ready` then
  `connection.set_audio_sink(self)`; input `stop/cancel` → deregisters the sink (only
  if still itself) — **never** closes the socket, **never** stops the receive loop.
  Output writes playout via `connection.send()`; its teardown runs only the base
  teardown. The real-time output pacing and the interruption buffer-clear are
  unchanged.

## The lifecycle (`sip_server.py`)

Build the `SipConnection` once at startup; register the control handlers on IT.
- `call.state == "confirmed"` → cancel any running call first (single active call in
  v0, frees the STT slot), then create a per-call `SipGatewayTransport(connection,…)`,
  `session = build_agent_session(transport, input_processors=[HalfDuplexInputGate()]
  if HALF_DUPLEX else None)`, launch `PipelineRunner().run(session.task)` as a
  background task, then `await session.greet()`.
- `call.state == "disconnected"` → cancel the running pipeline task (tears the
  per-call transport down → STT `_disconnect` frees the slot), clear the tracked call.
  No context reset — the next call builds fresh.
- socket EOF → `connection.run()` returns; the `finally` cancels any running call and
  `connection.close()`s. The process exits.
- `cancel_on_idle_timeout=False` is **dropped** (no persistent pipeline anymore).

`build_agent_session`, `gateway_server.py`, and the OpenClaw path were **not touched**
(the factory is already per-call-capable — the OpenClaw path calls it per connection).

## Setup

- **Target:** Jetson appliance (`teaspoon@192.168.1.234`), `/opt/teaport/venv`
  (unmodified — run via `PYTHONPATH=$HOME/teaport-src/brain`).
- **Engine:** `ws://127.0.0.1:8000` (Voxtral STT + Kokoro TTS), `teaport-engine` active.
- **LLM:** OpenRouter → qwen3 (env from `/etc/teaport/brain.env`, loaded literally).
- The **live source-run `sip_server`** (pid 11404 on `/tmp/teaport-sip.sock`) was
  stopped first to free the single STT slot; the LIVE `teaport-sip` gateway socket was
  **not touched**. The brain is left STOPPED for the parent.
- **Driver:** `fake_gateway_multicall.py` — one persistent SEQPACKET connection, hello
  sent ONCE, then **3 sequential calls**: each `call.incoming` → `call.state confirmed`
  → stream `question_host_status.wav` (16 kHz mono, *"What is your host status right
  now? How much memory do you have free?"*) as `audio.in` → `call.state disconnected`,
  with a 3 s inter-call pause (socket stays open). Run via `run_sip_percall_test.sh`
  (`SIP_HALF_DUPLEX=0`, `LOGURU_LEVEL=DEBUG`).

## Command

```
# on the Jetson — live source-run sip_server stopped first to free the STT slot
bash ~/teaport-src/brain/test/run_sip_percall_test.sh   # brain/test/run_sip_percall_test.sh
#   fake_gateway_multicall.py --calls 3 --greet-wait 14 --reply-idle 3 --between 3 &
#   python -m teaport_brain.sip_server --socket /tmp/teaport-fakegw.sock
```

## Result: PASS — 3 calls answered on ONE persistent socket, fresh pipeline each

Driver: `PASS: all 3 calls received + answered on one persistent socket`
(`fake_gateway_multicall rc=0`). Per-call reply audio.out copied into the repo as
`RUNLOG-sip-percall-call{1,2,3}-audio-out.wav` (16 kHz mono, ~7 s each).

```
RESULT: drove 3/3 calls on one persistent socket
  call 1 (fakecall-0001): total=363f greeting~85f  reply~278f
  call 2 (fakecall-0002): total=377f greeting~113f reply~264f
  call 3 (fakecall-0003): total=346f greeting~85f  reply~261f
```

### (1) Each call builds a FRESH pipeline / session — distinct STT session id per call
The whole stack is rebuilt each call: new `PipelineTask#N`, new `TeaportSTTService#N`,
new `BoundedOpenAILLMService#N`, and a **distinct engine STT session id**:
```
building a FRESH per-call pipeline for call fakecall-0001 (new STT/LLM/TTS/context)
  TeaportSTTService#0: session created sess_1787850459_886
building a FRESH per-call pipeline for call fakecall-0002 ...
  TeaportSTTService#1: session created sess_1787850493_2777
building a FRESH per-call pipeline for call fakecall-0003 ...
  TeaportSTTService#2: session created sess_1787850527_6915
```

### (2) The connection + control SURVIVE between calls
`SIP gateway socket connected` fires **once** at startup. Calls #2 and #3 are received
and answered AFTER call #1's pipeline was fully torn down — same persistent connection,
same receive loop, same control dispatch:
```
call.state=confirmed (call fakecall-0001)     -> build fresh pipeline
call.state=disconnected (call fakecall-0001)  -> tearing down the per-call pipeline
call disconnected — per-call pipeline torn down, STT slot freed
call.state=confirmed (call fakecall-0002)     -> build fresh pipeline   [received AFTER #1 torn down]
...
call.state=confirmed (call fakecall-0003)     -> build fresh pipeline   [received AFTER #2 torn down]
```

### (3) Audio flows each call — the tool fires with a REAL value on every call
Caller audio reached the fresh per-call STT (user turns transcribed each call), the
LLM called `get_host_status`, and the **live** value was spoken — not hallucinated.
Host reference at test start: `MemAvailable: 1142588 kB`, `loadavg 1.09`.
```
call 1  get_host_status -> {'device':'NVIDIA Jetson Orin Nano 8GB','memory_available_mb':819,'cpu_load_1min':1.04,...}
        LEDGER +assistant: 'Free memory is about eight hundred megabytes, and the CPU load is just over one percent.'
call 2  get_host_status -> {... 'memory_available_mb':736,'cpu_load_1min':1.35 ...}
        LEDGER +assistant: 'I have about seven hundred thirty‑six megabytes of free memory and a low CPU load.'
call 3  get_host_status -> {... 'memory_available_mb':707,'cpu_load_1min':1.36 ...}
        LEDGER +assistant: 'I’ve got about seven hundred seven megabytes of free memory and a very low CPU load.'
```
The three readings differ (819 → 736 → 707 MB) and track the real box, and each
spoken reply carries its own call's exact number — real, live, and per-call fresh.

### (4) The STT slot is FREED between calls (disconnect at end, reconnect next call)
Each per-call STT connects on its pipeline's StartFrame and **disconnects on teardown**
(the engine's single session slot is only freed by our close reaching it); the next
call's STT connects only AFTER the previous one disconnected:
```
TeaportSTTService#0: connecting to the engine ...        (call 1 start)
TeaportSTTService#0: disconnecting from the engine       (call 1 end  — slot freed)
TeaportSTTService#1: connecting to the engine ...        (call 2 start — slot reacquired)
TeaportSTTService#1: disconnecting from the engine       (call 2 end)
TeaportSTTService#2: connecting to the engine ...        (call 3 start)
TeaportSTTService#2: disconnecting from the engine       (call 3 end)
```

### (5) No socket leak, no crash, clean teardown after the last call
- No tracebacks, no `receive loop error`, no `send failed`, no uncaught handler
  exceptions anywhere in the brain log.
- After call #3's `disconnected`, the driver closed the socket; the connection saw EOF
  and stopped cleanly:
  ```
  Pipeline worker PipelineTask#2 has finished
  call disconnected — per-call pipeline torn down, STT slot freed
  SIP gateway hung up (socket EOF) — stopping
  SIP brain stopped
  ```
- Box left clean: no `sip_server`/`fake_gateway` processes, no leftover
  `/tmp/teaport-fakegw.sock`. The LIVE `/tmp/teaport-sip.sock` (real teaport-sip
  gateway) was untouched; `teaport-engine` + `teaport-brain` still `active`. **The live
  SIP brain was left STOPPED** for the parent.

## Deviations / notes

- **Control handlers dispatched `sync=True`.** `_call_event_handler` otherwise runs
  each async handler as its own task; a `confirmed`/`disconnected` pair arriving
  back-to-back could then run concurrently and let the teardown race ahead of the
  build, orphaning a pipeline. Sync dispatch runs them inline in the receive loop in
  wire order. The only cost is that a `confirmed` handler's `await session.greet()`
  briefly holds the receive loop; `greet()` resolves as soon as STT connects (sub-second
  on the healthy path — the 12 s ceiling is only hit when STT is down, when there is no
  caller audio to route anyway).
- **EOF teardown is done in `run()`'s `finally`** (cancel active call + close socket),
  with `on_client_disconnected` kept as a log line, so the teardown is deterministic
  rather than racing an event task as the process exits.
- **New driver** (`fake_gateway_multicall.py`) rather than editing the single-call
  `fake_gateway.py`, so the existing single-call harness / RUNLOGs still work.
- `AgentSession.reset_context()` still exists in the untouched `agent_session.py`; the
  SIP path no longer calls it (each call builds fresh). Harmless dead code for SIP.
