<!-- SPDX-License-Identifier: MIT -->
# SIP brain — scripted per-call corpus on pipecat 1.8.1 — acceptance run log

The scripted-corpus leg of issue #24's acceptance criteria, run on the appliance after
the 1.7.0 → 1.8.1 bump. Companion to `RUNLOG-sip-percall.md`, which recorded the same
driver on 1.7.0 for a different question (the per-call pipeline lifecycle itself).

**Build:** `chore/pipecat-1.8.1` @ `9644bd1`, pipecat **1.8.1**, Python 3.12.3,
appliance `teaport` (Jetson Orin Nano 8GB, aarch64). `/etc/teaport/brain.env` loaded, so
`LEDGER_TRACE=1` and `TEAPORT_ENDPOINT_DEBUG=1` were in force (27 `[EP]` lines).
**Command:** `bash brain/test/run_sip_percall_test.sh` — 3 sequential calls on one
persistent SEQPACKET socket, each streaming `question_host_status.wav` (*"What is your
host status right now? How much memory do you have free?"*).

The live telephony path was untouched throughout: the rig binds `/tmp/teaport-fakegw.sock`
and its stale-sweep matches the brain strictly on that socket, so
`teaport-sip-brain`'s own `--socket /run/teaport/teaport-sip.sock` process was never a
candidate. Verified still registered afterwards.

## Result: PASS

```
PASS: all 3 calls received + answered on one persistent socket
RESULT: drove 3/3 calls on one persistent socket
  call 1 (fakecall-0001): total=201f greeting~73f reply~128f
  call 2 (fakecall-0002): total=211f greeting~73f reply~138f
  call 3 (fakecall-0003): total=201f greeting~73f reply~128f
```

### Fresh pipeline per call — the STT slot is freed between callers
```
TeaportSTTService#0: session created sess_1788741723_3451
TeaportSTTService#1: session created sess_1788741750_2921
TeaportSTTService#2: session created sess_1788741780_3555
```

### The tool returned a real, moving value on every call
```
call 1  {'device':'NVIDIA Jetson Orin Nano 8GB','memory_available_mb':933,'cpu_load_1min':1.1}
call 2  {... 'memory_available_mb':891,'cpu_load_1min':1.59 ...}
call 3  {... 'memory_available_mb':878,'cpu_load_1min':1.41 ...}
```

### The engine decoded every question — no 0-char segments
Six finals, two per call: `done (2.1s audio, 36 chars)` / `done (2.1s audio, 34 chars)`.
This is the failure mode that made a phone call look dead in September
(`engine-stt-zero-chars`, resolved as an AEC desync); the bump did not reintroduce it.

### None of 1.8.0's new failure paths fired
Counted over the whole run: **0** service write-offs (`is_usable` cleared), **0**
setup/start timeouts, **0** zero-audio TTS contexts, **0** tracebacks. Those are the
three doors 1.8.0 opened and this branch closed; the corpus exercises the normal path
and confirms none of them opens spuriously.

## Heard fractions — and the baseline that does not exist

Every charted turn:

```
call 1  +assistant [3.5-5.0] 'Hello, good to hear you.'                       heard 100%
        +user      [14.4-16.6] 'What is your host status right now?'
        +user      [16.7-18.8] 'How much memory do you have free?'
        +assistant [20.0-22.6] 'About nine hundred thirty-three megabytes free.'  heard 100%

call 2  +assistant [1.3-2.7] 'Hello, welcome back!'                           heard 100%
        +user      [14.4-16.6] 'What is your host status right now?'
        +assistant CUT heard~0%: NOT heard 'Free memory is about eight hundred ninety…'
        +user OVERLAP [16.7-18.8] 'How much memory do you have free?'

call 3  (identical in shape to call 2)
```

**The two 0% cuts are the corpus, not a regression.** The WAV plays both questions
back-to-back on a fixed clock — Q2 begins 0.1 s after Q1's final — so whether Q2 lands
on top of the reply to Q1 depends entirely on how fast the LLM answers. In call 1 the
reply started at t=20.0, *after* Q2 had finished at 18.8, and was heard whole. In calls
2 and 3 the reply started before 16.7 and Q2 cut it. The driver is not listening for the
bot; it cannot yield.

What the ledger did with that is the point: it charted the cut reply as `heard ''` rather
than as something the caller heard, and labelled the interrupting turn `+user OVERLAP`.
The model is not told it said something nobody heard. That is the behaviour PR #13 added
and #23 refined, working unchanged on 1.8.1.

**On #24's "within noise of the 1.7.0 baseline":** there is no such baseline to compare
against. `RUNLOG-sip-percall.md` predates the ledger's context labels and records no
heard fractions, no `CUT` lines and no `OVERLAP` labels — so the criterion cannot be
evaluated as written. Producing one would mean downgrading the appliance venv to 1.7.0
and re-running, which costs a deploy cycle on the box that serves live calls. **This run
is recorded as the baseline instead.** The numbers above are what a later bump should be
compared against; the thing to watch is call 1's shape (a reply that starts after the
caller has finished is heard whole), not the 0% cuts, which move with model latency
rather than with anything in the pipeline.
