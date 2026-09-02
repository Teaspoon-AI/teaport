# Formal models of the brain's concurrent protocols

The brain is single-threaded asyncio, so it has no data races. Every race it *does*
have is an **interleaving race**: which of several live coroutines runs between two
`await` points. That is what these models check.

asyncio makes the mapping unusually clean. The atomic grain is not a judgment call —
**an `async def` body is atomic between `await`s** — so each stretch of code between
awaits becomes one TLA+ action, and there is a defensible correspondence between the
spec and the Python.

State spaces here are small (hundreds of states) and checks run in about a second, so
these are cheap to keep in CI.

## Running them

TLC needs Java 11+ and `tla2tools.jar`
([tlaplus releases](https://github.com/tlaplus/tlaplus/releases)):

```sh
java -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC \
     -nowarning -workers auto -config retireOnRead.cfg Followup.tla
```

## `Followup.tla` — the async `ask_openclaw` follow-up

Models `FollowupGate` (`followup_gate.py`) together with `_make_consult_followup`
(`agent_session.py`): when a background consult finishes, a one-shot USER-role trigger
("tell the user now…") is appended to the context and an `LLMRunFrame` is queued.

Retiring that trigger is a **two-sided** constraint, and each side has already cost a
live session:

| | |
|---|---|
| `NoSilentLoss` | retired before any completion read it → the answer is never spoken |
| `NoRepeatRecital` | still live after delivery → a later turn recites it again |

Three designs, selected by the `MODE` constant:

| MODE | what it models | `NoSilentLoss` | `NoRepeatRecital` |
|---|---|---|---|
| `asWritten` | `gate.wait_until_delivered()` — waits on `_busy`/`_idle` | ✗ | ✗ |
| `gateOnOwn` | wait for *our* completion to start, then end | ✓ | ✗ |
| `retireOnRead` | retire **at** the read | ✓ | ✓ |

The as-written design violates **both**: the trigger can be retired before anything
read it, *and* — on a different interleaving — stay live long enough for two
completions to read it. The two live incidents it caused were one bug, not two.

Each failing row in `check.sh` therefore checks exactly **one** property. A rejected
design often breaks several, and TLC reports whichever its search reaches first, which
varies with the seed; a row checking several at once prints a different name run to run
and is worthless as a gate.

`asWritten` fails in 8 steps, and the trace needs nothing exotic — just the user
speaking shortly after a consult lands:

```
FWaitIdle    wait_until_idle() returns; the conversation is quiet
FQueue       trigger appended, LLMRunFrame queued
UserStart    the user speaks; the interruption flushes the queued LLMRunFrame
FWaitBusy    wait_until_delivered()'s _busy.wait() is satisfied by THE USER
UserStop     the user stops
FWaitIdle2   _idle.wait() is satisfied
FNeutralize  the trigger is retired — reads = 0, nothing ever read it
```

`_busy` is set by *any* activity, so it cannot distinguish "the turn I queued
happened" from "someone else spoke".

The obvious fix (`gateOnOwn`) trades one bug for the other: a barge-in between the
read and the retirement leaves the trigger live for the next turn to recite a second
time. Only retiring **at** the read satisfies both.

### What was changed

`FollowupTrigger` (`followup_gate.py`) implements `retireOnRead`. Two placement facts
pin it — both verified against pipecat 1.7.0 and both easy to get wrong:

- It sits **directly below the LLM**. `LLMTextFrame` is consumed by `TTSService`
  (`push_text_frames=False`), so it never reaches `FollowupGate`'s position after
  `transport.output()`.
- It fires on the first **`LLMTextFrame`**, not `LLMFullResponseStartFrame`. pipecat
  pushes the start frame *before* `_process_context` serializes the context into the
  request (`pipecat/services/openai/base_llm.py`), so retiring there would neutralize
  the trigger in place before the model ever saw it — losing *every* answer.

`speak_followup` arms the one-shot **before** `queue_frames` (the completion can start
while `queue_frames` is still awaiting) and retries a flushed turn at the next quiet
window instead of treating it as delivered.

The counterexample is a regression test: `brain/tests/test_followup_injection.py`,
`test_a_flushed_turn_does_not_retire_the_trigger_unread`.

### PR #13: the latch across a tool call

`followup_gate.py:159` (PR #13) clears `_llm` on `FunctionCallInProgressFrame`. The reason
is real: for a bare tool call the TTS service holds `LLMFullResponseEndFrame` (empty text
never creates an audio context), so the latch stayed set forever — every narrator line
skipped, the injector burning its full `max_wait` in genuine silence.

The model above could not see that change. It had no tool call: a turn that is two
completions with the tool between them was not in the state space, so `Followup.tla`
passed unchanged against a `followup_gate.py` it no longer described. `LATCH` adds it —
`ToolCall` (the bare call; `_llm` cleared or not) and `ToolResult` (the answering
completion, which is another read of the context):

| LATCH | | `NoSilentLoss` | `NoRepeatRecital` | `NoInterjectMidTurn` |
|---|---|---|---|---|
| `held` | pre-PR: `_llm` latched until the answering completion ends | ✓ | ✓ | ✓ |
| `clearedOnToolCall` | PR #13: `FunctionCallInProgressFrame` clears it | ✓ | ✓ | ✗ |

The middle two columns are the point. A follow-up appended during a tool call is read
exactly once — by the tool's own answering completion — so both read-count properties
hold while the two turns collide. The existing invariants are blind to this class. It
needs a third, which is the gate's own docstring ("don't step on … the assistant
mid-answer about something else") written down: `NoInterjectMidTurn`, the trigger is
never appended while a turn is in flight. TLC violates it in 7 steps:

```
FWaitIdle    wait_until_idle() returns; the conversation is quiet
UserStart    the user asks something
UserStop     the turn is queued
RunStart     the completion starts -- LLMFullResponseStartFrame, _llm = True
ToolCall     it is a BARE tool call: FunctionCallInProgressFrame clears _llm
             (followup_gate.py:159); _bot is already False. The gate reads IDLE
FQueue       the trigger is appended and an LLMRunFrame queued -- into a turn
             whose answering completion has not started yet
```

`FQueue` re-checks `Idle` at the append, which the code does not (there is no await
between `wait_until_idle()` returning and the context write), so the model is stricter
than the code and the violation holds a fortiori. `fu_clearedOnToolCall_existing` is
kept as a row precisely because it *holds*: it is the record that the two original
properties do not gate this.

Not fixed in the code. The narrow release — clear the latch only for a call whose result
comes back with `no_inference()` (the async `ask_openclaw` shape, where the tool result
genuinely ends the response) — is what the model suggests; a `LATCH` value for it is the
next row to add.

### Known limits of this model

- **Liveness is not established.** `EventuallyDelivered` (`live.cfg`) fails only
  because the `MaxRuns` bound is exhausted, not because of a defect — the trace ends
  with `started = MaxRuns` and a turn still queued. Raising the bound doesn't settle
  it; a proper liveness argument needs fairness assumptions about the user, which the
  model doesn't currently state.
- **Durations are out of scope.** `_QUIET_SECS`, `_DELIVERY_START_TIMEOUT` and the
  0.3 s STT settle are timing assumptions TLA+ cannot judge. It can say *whether* a
  wait is needed, never *how long*.
- **The residual risk it makes explicit:** a completion that reads the trigger and is
  then barged away before speaking retires it having delivered nothing. The ledger
  already computes `heard_fraction` for exactly that turn, so re-arming at
  `heard_fraction == 0` would close it. Not built.

## `SttSlot.tla` — arbitration of the engine's single STT slot

The engine serves one STT session. Two front-ends contend for it through **separate
arbiters with no shared lock**: the OpenClaw path tracks `_active_session`
(`agent_session.py`), the SIP path tracks `active["call"]` (`sip_server.py`). Each
evicts only its own predecessor; neither can see the other's session.

Both then hand the gap over to `await asyncio.sleep(0.3)` — a timing assumption
standing in for synchronization. Nothing anywhere observes that the engine has
actually freed the slot.

| MODE | | `NoFalseBusy` |
|---|---|---|
| `fixedSettle` | as written: settle, connect once, a refusal is final | ✗ |
| `retryWhileBusy` | retry a rejection instead of trusting the sleep | ✓ |

`NoFalseBusy` is the user-visible failure: someone is told *"Sorry, the voice
assistant is busy with another session right now"* — and on SIP hung up on — when no
session held the slot at all. It fails with one OpenClaw session and one SIP call:

```
Arbitrate/Settle    both sessions run their own arbiter; neither sees the other's
ConnectOk(g2)       g2 takes the slot
Leave(g2)           its client disconnects
Teardown(g2)        the socket closes — but the engine has NOT freed the slot yet
ConnectRefused(p1)  the caller's 0.3s settle already elapsed: 503, holder = none
                    -> "busy", hang up. Nothing was using it.
```

Note what makes this fail *permanently* rather than momentarily: `stt.py`'s
`_connect_websocket` makes a **single** attempt and sets `_stt_available = False`, and
`greet()` polls only until the tri-state *resolves* — so a 503 resolves to False in
milliseconds and the 12 s poll window buys nothing against it.

### What was changed

`_connect_websocket` now retries a **rejection** (4 attempts, 0.4 s apart), which
covers the hand-off window while staying inside `greet()`'s resolution window. Only a
rejection: an unreachable host will not fix itself, and each such attempt costs the
websockets open timeout (~10 s), which would leave the caller in silence instead of
hearing the warning. `_is_slot_busy` duck-types the status off the exception because
`websockets` is deliberately unpinned and the field moved (`.response.status_code` on
≥14, `.status_code` before); an unrecognised exception is treated as not retryable, so
the worst case is the previous behaviour.

### Known limits of this model

- `retryWhileBusy` retries *while the refusal is transient*, but the brain cannot
  actually tell a transient 503 from a genuine one — the engine returns the same
  thing either way. The implementation therefore bounds the retry by wall-clock, so
  the property holds only under the assumption that **the engine frees the slot
  within the retry budget**. That is a timing fact this model states rather than
  proves; see the limits note above.
- `MutualExclusion` is enforced by the engine, not by the brain, so it is a sanity
  check on the model rather than a claim about our code.
- The model says nothing about *which* session should win. Eviction policy is a
  product decision, not a safety property.

## `SipCall.tla` — the SIP per-call lifecycle

One AF_UNIX SEQPACKET connection carries call control *and* caller audio, and
`SipConnection._receive_messages` is its only reader. Call-control handlers are
registered `sync=True` so they run inline there, in wire order — which is load-bearing,
because a `confirmed` builds a pipeline and a following `disconnected` tears it down.

Two findings from the PR review meet here, and they compound:

| MODE | | `NoWrongTeardown` | `NoBlockedWithPendingControl` |
|---|---|---|---|
| `asWritten` | teardown ignores `call_id`; handler blocks | ✗ | ✗ |
| `callIdChecked` | teardown matches `call_id` | ✓ | ✗ |
| `asyncSetup` | + the slow bring-up moved off the loop | ✓ | ✓ |

`NoWrongTeardown` fails in 9 steps — confirm A, confirm B, disconnect A; the stale
`disc(A)` is dispatched after B has replaced A and **tears down B**. B's caller is left
connected to a gateway with no brain: no audio, no hangup, and no further `confirmed`
ever coming. `active["call"]` never recorded which call it belonged to.

`NoBlockedWithPendingControl` is a *structural* statement, not a discovery: `asyncSetup`
satisfies it by construction, because not blocking is what the fix does. It is here to
pin why the two compound — the backlog a blocked reader permits is what lets a stale
`disconnected` be dispatched after a newer call exists.

### What was changed

`active["call"]` now carries the `call_id`, and both `cancel_active_call` and the new
`cancel_setup` take it as a guard. The bring-up (`bring_up_call`) runs as a task rather
than inline, so the reader keeps draining; a `disconnected` arriving mid-bring-up
cancels it. Teardown stays inline — ordering there is load-bearing and it is bounded by
the existing 5 s wait.

Separately and not modelled: `SipConnection.send()` issued concurrent
`loop.sock_sendall()` on one fd. asyncio keeps at most one writer per fd, so the second
call cancels the first's handle and the first future is never resolved — the audio
`MediaSender` blocks forever and the call goes silent with nothing logged. Reproduced
directly against a socketpair and fixed with a send lock; the reproduction is
`test_concurrent_sends_do_not_strand_each_other`.

### Known limits of this model

- **The magnitude is out of scope.** The ~17 s figure (greet()'s 12 s poll plus
  `wait_until_delivered`'s 10 s) is a timing fact; no amount of model checking
  establishes it. Measure it, don't prove it.
- Teardown still blocks the reader, bounded by `cancel_active_call`'s 5 s wait plus its
  0.3 s settle. The model treats teardown as fast, so `asyncSetup` passing does **not**
  mean the reader never stalls — only that the long pole is gone.
- The gateway is modelled as free to emit `confirmed(B)` before `disconnected(A)`.
  That is the case the code's own comments anticipate; if the gateway in fact
  guarantees ordering, the wrong-teardown trace needs the blocking to arise, but the
  `call_id` guard is cheap either way.

## `Ledger.tla` — the transcript ledger's bot-turn state machine

`TranscriptLedger` (`transcript_ledger.py`) is a deterministic observer: it charts what the
assistant said and how much of it was heard from the ORDER of frames over a small
alphabet. It is not concurrency, and it was item 1 under "worth modeling next" — its
comments already recorded four bugs of exactly that shape. Eight of PR #13's review
findings landed in it, one of them the "mid-turn `TTSStarted` clobber" that list named.

The model keeps what the numbers are computed from and drops the numbers: which turn a
frame is attributed to, whether a turn got an `audio_start` and from which playout
window, which response's text it claims, and whether it is charted cut or complete.
`heard_fraction` is 0 exactly when a cut turn has no `audio_start` (`_finish_bot`), so
"no `audio_start`" *is* the heard-nothing verdict — and `HeardContextCorrector` then
deletes the assistant message the user heard part of.

The environment is the pipeline, and it includes the frame shapes the code's own
comments and tests acknowledge: filler contexts (`append_to_context=False`), the output
transport's UNTAGGED re-push of every played chunk (`_audio_ok`'s docstring: "verified"),
gapless chaining inside one `BotStartedSpeaking` window, a reply path with no
`TTSStartedFrame` and no `context_id` (`_ensure_bot`'s docstring), and — under `Split` —
a reply re-created under a second context id mid-turn. Synthesis is sequential per the
TTS service; playout lags it arbitrarily. A cancelled completion still ends: pipecat
0.0.108 `base_llm.py:619-621` pushes `LLMFullResponseEndFrame` in a `finally`, after the
`InterruptionFrame`, and the ledger takes the partial text as a new `_pending_gen`.

It is the ledger AS WRITTEN at PR #13. There is no fix `MODE` yet, so every row fails —
by design: the rows pin the counterexamples until the code changes.

| row | property | review finding | steps |
|---|---|---|---|
| `ledger_phantom` | `NoPhantomFullHeard` — a reply charted complete had its audio played, or at least queued | `:267` the transport's untagged copy of a filler opens a phantom turn | 7 |
| `ledger_ownStart` | `AudioStartIsOwn` — a ctx-tagged turn's `audio_start` is its own window's, never a filler's | `:310` `_last_started_filler` credits the filler's window to the reply | 8 |
| `ledger_unheard` | `NoUnheardWhenPlayed` — a cut reply the transport played never charts with no `audio_start` | `:222` a reply chained into a filler's window gets no `audio_start` | 11 |
| `ledger_untagged` | same, on the path with no `TTSStartedFrame` | `:222`, via `_ensure_bot` | 11 |
| `ledger_split` | `NoPrematureFullChart` — a turn is never closed complete while the same reply is still synthesizing under another id | `:236` the chained branch charts the first half | 6 |
| `ledger_fillerSet` | `FillerCtxRemembered` — a live filler context is in `_filler_ctxs` | `:220` the overflow guard `.clear()`s the live entry | 4 |
| `ledger_once` | `ChartedAtMostOnce` — no response is charted twice | **new**, below | 9 |

`ledger_phantom` is the shortest, and the one the new test misses. The answer is
streaming while the ack plays; the ack's played chunk comes back from the transport
without a `context_id`, and `_is_filler` cannot tell it from a reply's:

```
LlmStart(r1)     the answering completion starts streaming -- _gen_acc is live
TtsStart(f1)     the ack: TTSStarted(ctx=f1, append_to_context=False) -- a filler
TtsAudio(f1)     its tagged audio -- dropped, f1 is in _filler_ctxs
PlayStart        BotStartedSpeaking -- skipped, _last_started_filler
PlayChunk(f1)    the transport plays it and re-pushes it UNTAGGED: not in
                 _filler_ctxs, no flag -> _ensure_bot opens a turn claiming r1
PlayStop         BotStoppedSpeaking closes the ack's window -> r1 is charted
                 COMPLETE, heard 1.0. Its TTS has not started.
```

`test_b_filler_only_playout_never_becomes_a_turn` passes because it omits the untagged
copy; its sibling `test_b_untagged_transport_copies_do_not_inflate_the_denominator` is
the proof the authors know the copy occurs.

`ledger_ownStart` is the same three frames in a different order — the reply's
`TTSStarted` arrives before the filler's window opens, so the boolean says "not a filler"
and the filler's `BotStartedSpeaking` becomes the reply's `audio_start`:

```
TtsStart(f1) TtsAudio(f1) TtsStop(f1)   the ack, synthesized and queued
TtsStart(r1)     the reply's context opens a turn; _last_started_filler = False
PlayStart        the ack begins playing; BotStarted -> the REPLY's audio_start
Interrupt        the reply is charted cut, its heard time counted from the ack
```

### The new finding

`ChartedAtMostOnce` fails in 9 steps with one reply and one filler. The reply is cut
during synthesis — charted, correctly, as heard 0 — and nothing resets `_gen_acc`:
`InterruptionFrame` only calls `_finish_bot`. The next filler's untagged copy re-opens a
live turn on the same text, and that filler's `BotStoppedSpeaking` charts it a second
time, complete. With the pinned pipecat the mechanism is `_pending_gen` instead: the
cancelled completion's `finally` pushes the End frame after the interruption, the partial
text becomes `_pending_gen`, and `_ensure_bot` claims it. Same result. It needs no user
turn in between — a false VAD trigger (an interruption with no transcription) followed by
a narrator line is enough.

### What did not reproduce

`:208` (the flag latches, so a later reply's `BotStartedSpeaking` is skipped) has no
trace. The model enforces what the pipeline enforces: the ledger sees a reply's audio
frame before the transport can open a window for it, so by the time `BotStarted` arrives
the turn already exists, and the assignment — which sits outside the flag's guard — sets
`audio_start` regardless. The flag's staleness only bites through the `:222` shape, where
the window was opened by the filler. That review item folds into `:222`.

### Known limits of this model

- **Durations are out of scope**, as everywhere here. `:525` (the `t_end` clamp dropping
  an overlap) is qualitative in mechanism and quantitative in effect; the model does not
  chart `t_end`.
- **Words are not modelled.** `TTSTextFrame`s only add `spoken` entries and never open or
  close a turn on a path audio does not; they are omitted. `heard_text`'s pts cut is
  numeric anyway.
- **Synthesis is sequential.** One context is between `TTSStarted` and `TTSStopped` at a
  time, which is the TTS service's behaviour; playout is free to lag. A stop frame is
  dropped only by an interruption; the audio-context timeout path is not modelled.
- **`Split` is an assumption the cfg selects**, as the gateway ordering is in
  `SipCall.tla`. The PR's comment says a re-created context keeps its id; pipecat pins
  that only while `context_id == _turn_context_id`, which `TTSSpeakFrame` nulls. The model
  makes the assumption explicit rather than settling it.
- **`NoPhantomFullHeard` allows "queued".** The chained branch charts a turn complete
  while its tail may still be at the transport; a barge-in that flushes it is then
  misrecorded as heard. Residual, and pre-existing.
- **No fix `MODE`.** Every row fails as written. The filler bookkeeping wants "which
  context is playing" rather than a boolean set by the last started frame, and the
  untagged transport copy wants attributing by what the transport is playing; both are
  design decisions the model can check but should not make.
- **Scale.** One reply and one filler is a few hundred states; two of each is 2.2M states
  and a minute. The rows use the small instance; the bugs need nothing larger.

## Worth modeling next

1. **A fix `MODE` for `Ledger.tla`.** Every row fails as written; the first design that
   makes `ledger_phantom`, `ledger_ownStart` and `ledger_unheard` hold together is the
   one to build.
2. **`HeardContextCorrector`'s `_done`/`_mark` bounds** against ledger growth and
   `set_messages`. PR #13's `append_to_context=False` on the tool-ack line
   (`tools.py:737`) belongs here too: a turn the bot spoke that leaves no assistant
   message in the context is a context invariant, not a ledger one.
3. **`MemoryRecall`'s single-flight + turn generation tag** — small and already
   carefully written, so cheap regression insurance rather than a suspected bug.
