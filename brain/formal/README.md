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

## Worth modeling next

1. **`TranscriptLedger`'s bot-turn state machine.** Not concurrency at all — pure
   frame ordering over a small alphabet. Its comments already record four bugs of
   exactly that shape (stale `_pending_gen` snapshot, mid-turn `TTSStarted` clobber,
   audio double-counted across processors, the pts lead).
2. **`HeardContextCorrector`'s `_done`/`_mark` bounds** against ledger growth and
   `set_messages`.
3. **`MemoryRecall`'s single-flight + turn generation tag** — small and already
   carefully written, so cheap regression insurance rather than a suspected bug.
