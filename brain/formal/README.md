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
TLA_TOOLS=/path/to/tla2tools.jar ./check.sh
```

`check.sh` runs every row and **gates**: it exits nonzero if any row misses the
expectation in its last column — an `expected: holds` design that is violated, an
`expected: FAILS <Prop>` design that holds or that fails on a *different* invariant
than the one it names, or a row with no verdict at all. Run a single row by hand with,
for example:

```sh
java -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC \
     -nowarning -workers auto -config fu_retireOnRead.cfg Followup.tla
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

Each failing row in `check.sh` **must name** the one property it is kept to demonstrate
(a nameless `FAILS` is rejected), and the gate insists that named property is the one
that fell. A rejected design often
breaks several, and TLC reports whichever its search reaches first (seed-dependent), so
naming pins the intended counterexample: a guard invariant (`TypeOK`, …) breaking
instead — which would otherwise mask the real one — is caught as a wrong-property
mismatch, not waved through.

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
pin it — both verified against pipecat 1.7.0 and re-verified at 1.8.1, and both easy
to get wrong:

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

| LATCH | | `NoSilentLoss` | `NoRepeatRecital` | `NoInterjectMidTurn` | `NoDeadAirDuringTool` |
|---|---|---|---|---|---|
| `held` | pre-PR: `_llm` latched until the answering completion ends | ✓ | ✓ | ✓ | ✗ |
| `clearedOnToolCall` | PR #13: `FunctionCallInProgressFrame` clears it | ✓ | ✓ | ✗ | ✓ |
| `turnAware` | the fix: `_llm` released as above, plus `_turn` for the injector alone | ✓ | ✓ | ✓ | ✓ |

`NoDeadAirDuringTool` is the narrator's side of the trade — during a tool call with
nobody speaking, the gate must read idle, because a synchronous consult runs *inside*
its call for up to 45s and that silence is what a progress line fills. `held` fails it:
the pre-PR dead air, the reason the latch was released at all. The two properties pull
in opposite directions on one flag, which is why the fix is two.

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

### What was changed

`turnAware`, in `followup_gate.py`. `_llm` is still released on
`FunctionCallInProgressFrame`. A second flag, `_turn`, is set on
`LLMFullResponseStartFrame` and cleared by the answering completion's End frame, by a
`FunctionCallResultFrame` whose `run_llm` is False (`tools.no_inference()`, the async
placeholder nothing answers from), or by an interruption. `wait_until_idle(turn_free=True)`
waits on idle *and* `~_turn`; the follow-up injector (`agent_session.speak_followup`)
passes it, the narrator does not. The gate sits before the assistant aggregator, so it
sees the result frame. `test_followup_gate.py` covers the synchronous shape (narrator
idle, injector held until the answering completion ends), the no-inference result, and
the interruption.

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
1.8.1 `services/openai/base_llm.py:611-613` pushes `LLMFullResponseEndFrame` in a
`finally`, after the `InterruptionFrame`, and the ledger of that time took the partial
text as a new `_pending_gen`.

Two designs, selected by `MODE`. `asWritten` is the ledger at PR #13 and fails every
property; its rows are kept failing, pinning the counterexamples. `windowHead` was the
first fix (below) and holds all but one — and was then taken apart in turn by the review
of PR #13, so it is kept here as the second rejected design. The ledger as it stands is
the playout design, modelled in [`LedgerPlayout.tla`](#ledgerplayouttla--the-transcript-ledger-the-playout-design)
below, which holds every row including the one `windowHead` left open.

| row | property | review finding | asWritten | windowHead |
|---|---|---|---|---|
| `ledger_phantom` | `NoPhantomFullHeard` — a reply charted complete had its audio played, or at least queued | `:267` the transport's untagged copy of a filler opens a phantom turn | ✗ (7) | ✓ |
| `ledger_ownStart` | `AudioStartIsOwn` — a ctx-tagged turn's `audio_start` is its own window's, never a filler's | `:310` `_last_started_filler` credits the filler's window to the reply | ✗ (8) | ✓ |
| `ledger_unheard` | `NoUnheardWhenPlayed` — a cut reply the transport played never charts with no `audio_start` | `:222` a reply chained into a filler's window gets no `audio_start` | ✗ (11) | ✓ |
| `ledger_untagged` | same, on the path with no `TTSStartedFrame` | `:222`, via `_ensure_bot` | ✗ (11) | ✓ |
| `ledger_split` | `NoPrematureFullChart` — a turn is never closed complete while the same reply is still synthesizing under another id | `:236` the chained branch charts the first half | ✗ (6) | ✗ |
| `ledger_fillerSet` | `FillerCtxRemembered` — a live filler context is in `_filler_ctxs` | `:220` the overflow guard `.clear()`s the live entry | ✗ (4) | ✓ |
| `ledger_once` | `ChartedAtMostOnce` — no response is charted twice | **new**, below | ✗ (9) | ✓ |
| `ledger_wrongText` | `ChartedTextMatchesContext` — a turn charted under a reply's context carries that reply's text | **new**, below; two replies | ✗ (6) | ✓ |

(Steps are the `asWritten` counterexample lengths.)

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

On its own the second chart is inert: `HeardContextCorrector._reconcile` acts only on
events that are `interrupted and cut_short`, and nothing else reads `ledger.events`. What
bites is the turn the phantom leaves OPEN. `ledger_wrongText` (`ChartedTextMatchesContext`)
fails with two replies, on two paths. TLC's shortest is pre-existing: `_new_bot` prefers
the in-flight generation, so a reply whose TTS starts after the NEXT completion has begun
streaming (text plus a tool call in one completion, the answer streaming before the ack's
TTS starts) is charted under the next completion's text. The longer path is the
phantom's: the ack's untagged copy opens a turn on the cut reply's text; the answer
chains into the ack's window, so its `TTSStarted` takes the adopt-ctx branch instead of
opening a turn; the answer is now charted under the OLD text with no `audio_start`.
Barge into it and the corrector receives a cut event with `heard_text` empty — and
deletes the answer's committed message. Reproduced against the real ledger and
corrector (`HeardCorrector[truncate]: removed unheard reply`; afterwards the context has
no assistant message for the answer at all, so the model has no record it answered).
Not barged, the wrong-text chart is inert, but `_pending_gen` is not consumed (`gen_seq`
mismatch) and the next filler repeats the phantom.

### What was changed (`windowHead` — superseded)

The design at commit 3a51294, kept as it was checked. The review of PR #13 found the
premise it rests on — that the anonymous `BotStartedSpeaking` can be made to say whose
audio the window is for — cannot be patched into soundness: the head's filler-ness was
latched at push time and consumed at playout time, `queue_ahead is not None` closed
turns that had not played a sample and never closed ones whose audio never came, the
chained preset blocked the two accurate `audio_start` stamps, `_gen_claimed` latched
across a turn that charted nothing, a ctx-less turn adopted the next reply's context, the
`_pending` queue stranded entries with no cap, and a heard fraction went negative. See
`LedgerPlayout.tla` for what replaced it.

`windowHead`, in `transcript_ledger.py`. The window the transport is playing is
identified by the FIRST audio pushed since the previous window closed — the transport
plays in push order and the ledger sees every push before the transport can open a
window for it — and that head's filler-ness is decided at push time, while the filler's
context is still in `_filler_ctxs`. From that:

- `BotStartedSpeaking` opens a turn only in a window whose head is not a filler, and
  stamps `audio_start` only on a turn whose own audio is in the window — as the window
  start plus the seconds queued ahead of that audio (`queue_ahead`). A reply queued behind
  a filler gets its real start; a reply whose `TTSStarted` beat the filler's window no
  longer inherits the filler's.
- An untagged audio frame — the transport's rebuild — never opens a turn inside a
  filler's window. Every TTS-service push is tagged (`engine_tts.py` yields audio with
  `context_id`), so nothing real is refused.
- `BotStoppedSpeaking` closes only a turn whose audio was in that window. Found by the
  model once the above was in: `TTSStarted` is pushed at synthesis start, and a slow
  first chunk (Kokoro shares the GPU with STT) lets a filler queued just before it play
  out and close its own window first — which charted the reply complete with no audio
  and then charted it again. `test_e` in the regression file.
- An interruption drops both the in-flight text and every queued reply: pipecat cancels
  the completion (its End frame still comes, empty now) and flushes the TTS queue.
- `_pending_gen` is a queue, oldest first, and a turn takes the front when it opens; the
  in-flight text is used only with nothing queued. Two more paths the model found on the
  way: a single slot lost the older text when two completions finished before the first's
  TTS began (`test_f`), and a reply whose playout ends before its End frame arrives must
  not have that End queue its text as unspoken (`_gen_claimed`, `test_g`).
- `_filler_ctxs` evicts its oldest entry on overflow instead of clearing.

`brain/tests/test_ledger_phantom_cascade.py` is the model's counterexamples as a script,
seven cases; `test_ledger_context.py` still passes, with one timeline corrected:
`test_b_filler_opening_first_does_not_steal_the_reply` scheduled a reply's words inside
the filler's playout and asserted a heard fraction that counted the filler's seconds as
the reply's — the `:310` credit written down as an expectation.

Two environment facts were added to the model for the fix to be checkable, both
pipecat's: the TTS service starts contexts in the order responses reached it, and an
interruption drops a completed reply whose synthesis has not begun.

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
- **Synthesis is sequential, and in response order.** One context is between
  `TTSStarted` and `TTSStopped` at a time, a reply's context cannot start while an
  earlier live reply's has not, and an interruption drops a completed reply whose
  synthesis has not begun — all the TTS service's behaviour; playout is free to lag. A
  stop frame is dropped only by an interruption; the audio-context timeout path is not
  modelled. **Not modelled either: a completed reply with nothing speakable**, whose
  context never starts. Its text would sit at the front of the queue and the next reply's
  context would take it — the residual `windowHead` cannot close, because the ledger
  cannot see which response a context belongs to.
- **`Split` is an assumption the cfg selects**, as the gateway ordering is in
  `SipCall.tla`. The PR's comment says a re-created context keeps its id; pipecat pins
  that only while `context_id == _turn_context_id`, which `TTSSpeakFrame` nulls. The model
  makes the assumption explicit rather than settling it.
- **`Interrupt` is an environment action.** Its only precondition is that there is
  something to interrupt; nothing here models whether the user can actually obtain one.
  That was an unexamined axiom until it failed live on 2026-09-09, and it is now
  discharged by `UserTurn.tla` rather than assumed. The same applies to
  `LedgerPlayout.tla` and to `Followup.tla`'s `UserStart`.
- **`NoPhantomFullHeard` allows "queued".** The chained branch charts a turn complete
  while its tail may still be at the transport; a barge-in that flushes it is then
  misrecorded as heard. Residual, and pre-existing.
- **`ledger_split` is still open under `windowHead`.** The chained branch charts the
  first half of a reply re-created under a second context id; the fix did not touch it
  (`:236`), and whether the case arises depends on the `Split` assumption above.
- **Scale.** One reply and one filler is a few hundred states; two of each is 2.2M states
  and a minute. The rows use the small instance; the bugs need nothing larger.

## `LedgerPlayout.tla` — the transcript ledger, the playout design

The ledger after the review of PR #13 (`transcript_ledger.py` as it stands). The premise
`windowHead` shared with `asWritten` — read whose audio a window is for off the
anonymous `BotStartedSpeaking` — is dropped; what replaces it is in the module header
and the file's. `Ledger.tla` keeps the two rejected designs and their counterexamples;
this module has the same environment with the facts the redesign rests on spelled out:

- The TTS re-pushes a response's `LLMFullResponseEndFrame` — the same frame, same id —
  once the response's context has drained (pipecat 1.8.1,
  `tts_service._maybe_reset_word_timestamps`). Sighted a second time, below the TTS, it
  names the response the context spoke and says its synthesis is over. It comes only if
  the End reached the TTS before the drain; a context that drains on the stop-frame
  timeout while the LLM still streams gets none, and is re-created under the SAME id if
  its audio resumes (`Resume`; `tts_service.append_to_audio_context`).
- The engine TTS pushes no `TTSStoppedFrame` at all — `push_stop_frames` is False, and the
  box's journal of 2026-09-02 traces 476 `TTSStartedFrame`s and not one stop — so the
  re-push is the ledger's only completion signal, and `BotStoppedSpeaking` is the
  transport's 0.35s silence timeout alone, which fires mid-reply when synthesis stalls.
  `MaxChunks = 2` makes that stall reachable.
- Every TTS push is tagged with its context (`engine_tts.py`); the transport's untagged
  rebuild of each chunk it played is recognised and ignored, and the thinking-sound bed
  (untagged, pushed INTO the transport) is told from it by the processor that sighted
  it first. The untagged legacy path is out of scope.

The ledger, in the model's terms: turns open only on the TTS's own frames, never on
`BotStartedSpeaking`; a turn claims the oldest expected context (the queue of ended
responses), else the response streaming now; several turns may be open at once — a
reply queued behind another at the transport; a turn's `audio_start` is its own chunk's
place in the transport's queue, laid out from the window's start; a window closing
credits every queued chunk as played and charts, oldest first, the turns whose audio has
all played and whose synthesis is over; a context starting proves every OTHER open
context ended, since the TTS drains one at a time; the drain re-push confirms a turn's
response or corrects it; an interruption charts every open turn with its own played
portion. The heard arithmetic is `test_ledger_playout.py`'s, on the real ledger.

| row | property | LedgerPlayout |
|---|---|---|
| `lp_phantom` | `NoPhantomFullHeard` — a reply charted complete had every chunk PLAYED. Strict: `Ledger.tla`'s allowed "queued at the transport", the residual noted under its limits | ✓ |
| `lp_unheard` | `NoUnheardWhenPlayed` | ✓ |
| `lp_once` | `ChartedAtMostOnce` | ✓ |
| `lp_wrongText` | `ChartedTextMatchesContext`, two replies | ✓ (365k states) |
| `lp_premature` | `NoPrematureFullChart`, with stalls and timeouts | ✓ |
| `lp_playedCharted` | `PlayedIsCharted` — **new**: audio the transport played is charted once its turn is over; nothing played vanishes | ✓ |
| `lp_fillerSet` | `FillerCtxRemembered` | ✓ |
| `lp_resume` | `NoPrematureFullChart` under `Resume` | ✗ (9) — below |
| `lp_wrongText_unspeakable` | `ChartedTextMatchesContext` with `Unspeakable = {r1}`, the ledger before `has_speech` (`LedgerSpeakCheck = FALSE`) | ✗ (6) — below |
| `lp_wrongText_unspeakable_checked` | the same, the shipped ledger (`LedgerSpeakCheck = TRUE`) | ✓ |
| `lph_phantom`, `lph_once`, `lph_wrongText` | the same, hermetic wiring (`Drain = FALSE`) | ✓ |
| `lph_premature` | `NoPrematureFullChart`, hermetic wiring | ✗ (8) — below |

Two switches were folded in afterwards, for what the module's first cut assumed away or
left out. **`Unspeakable`**: a reply the TTS never opens a context for — nothing
synthesizable, or the guard emptied it. `CanStart` had assumed "text reaching the TTS
always opens a context, so no reply is stranded"; with one that does not, the ledger
queues its text and the next context claims it. **`Drain = FALSE`**: the hermetic wiring
the test suite runs (`TranscriptLedger()` with no `tts`): no drain signal exists, and
`_close_ready` takes a turn as synthesized once its response has ended.

`lp_wrongText_unspeakable` fails in 6 steps — the residual `windowHead` could not close,
and this design closes only after the fact:

```
LlmStart(r1) LlmEnd(r1)   r1 completes; the TTS opens no context for it; the ledger queues it
LlmStart(r2)              the next reply streams
TtsStart(r2)              its context opens and claims the queue's head -- r1's text
Interrupt                 barge-in before r2's End has reached the TTS, so before any drain
                          could correct the claim: charted under r1's text, cut
```

After the drain the claim IS corrected and the unspeakable text dropped, as `_end_drained`
says; a barge-in before it charts the wrong text, and the corrector then rewrites the
context with a prefix of it. The ledger learns which response a context is for one
response too late; only the TTS knows it at the context's start.

**Closed in code**, from the other side: the ledger reads a response's text at the TTS's
sighting, so it can ask the engine's own question of it. `transcript_ledger._llm_side`
now applies `tts_text.has_speech` — one regex, shared with `split_clauses_ramp`'s chunk
filter, so the ledger's "will this open a context" cannot drift from the engine's — and
never queues a response the TTS will not open a context for (nor a spoken notice).
`lp_wrongText_unspeakable` keeps the counterexample; `lp_wrongText_unspeakable_checked`
is the ledger as shipped, and `test_ledger_playout.py`'s case `k` is the same on the real
ledger. What the predicate cannot see — the engine failing a clause it accepted — is a
different path: the context exists, and the turn is charted never-played.

`lph_premature` fails in 8 steps, and only in the hermetic wiring: the response ends, the
first chunk plays, synthesis stalls, the window closes on silence — and "ended" is taken
as "synthesized", so the reply is charted complete with a chunk still to come. Live, the
drain re-push is the completion signal and the turn stays open (`lp_premature` holds).
It means a stall is invisible to the test suite's ledger, not that the shipped one
mischarts it.

`ledger_split`'s question is settled rather than carried: pipecat re-creates a timed-out
context under the SAME id, so a reply is never split across two ids and the ledger's
`_turn_for(ctx)` finds the same open turn. `ledger_untagged` has no row (out of scope,
above). `AudioStartIsOwn` holds by construction — `audio_start` is computed from the
turn's own chunk or not at all — and has no row.

### What the model found

Writing the drain step found a hole in the code as first written: the re-push for a
response whose turn was ALREADY charted (closed on other evidence — a newer context, or
the hermetic fallback) reassigned the newest open turn to that response. Fixed before
the tests ran: a charted turn's re-push is a no-op, and only an UNCLAIMED response's drain
corrects the newest turn (`_end_drained`).

### Known limits of this model

- **`lp_resume`.** A context drained on the stop-frame timeout with its LLM still
  streaming, then a filler starting — which proves, to the ledger, that the context ended
  — then the window closing: the reply is charted complete. If its audio then resumes
  under the same id, the tail plays under an anonymous turn and is charted nowhere. Real,
  and accepted: it needs a 15s engine stall mid-reply with the LLM still streaming and a
  narrator line in the gap; `TTS_STOP_FRAME_TIMEOUT_S` is the knob.
- **Numbers are out of scope**, as in `Ledger.tla`: the layout's seconds, `heard_fraction`,
  the pts cut.
- **Spoken notices** (a non-filler `TTSSpeakFrame`, queued as an expected context of its
  own) are not modelled. The hermetic fallback is `Drain = FALSE`, above.
- **A cancelled completion's End is assumed to precede the next run.** pipecat pushes it
  from the cancelled task's `finally`, at cancellation; the model had let it arrive after
  the next response began streaming, and the ledger — which cannot tell whose End it is
  and ends the stream it holds — closed that response on the stale frame. Excluded as an
  ordering fact (`LlmStart` waits for it) rather than modelled; real only if the event
  loop stalls between the cancel and the next run.
- **Scale.** One reply and one filler is 3k states; two replies 258k (365k before the
  End-ordering fact pruned the stale-End interleavings) and about two minutes.

## `UserTurn.tla` — whether the user can interrupt the bot at all

Every other model here takes barge-in as a **free environment action**. `Ledger.tla:378`
and `LedgerPlayout.tla` both write

```tla
Interrupt ==                                       \* the user barges in
    /\ interrupts < MaxInterrupts
    /\ window \/ Synthesizing \/ Streaming
```

— a bounding counter and "there is something to interrupt", nothing else — and
`Followup.tla`'s `UserStart` is the same shape. They model what happens *after* an
interruption and assume one is always available. Not one of them has a variable for the
user turn, and the user turn is the only thing that produces an interruption.

That assumption failed in production on 2026-09-09: the bot kept speaking over the user
and stayed uninterruptible for as long as they went on trying. This module is the
assumption's discharge — the one place `Interrupt`'s enabling condition is a conclusion
rather than an axiom.

Barge-in in this brain is a user turn START and nothing else: `MinWordsUserTurnStartStrategy`
counts the words, the aggregator broadcasts the `InterruptionFrame` from
`on_user_turn_started`. So two guards in pipecat's `UserTurnController` decide it between
them, and each is right on its own:

| | |
|---|---|
| `_trigger_user_turn_start` | *"Prevent two consecutive user turn starts"* — refuses while a turn is open |
| `_trigger_user_turn_stop` | *"Never finalize while the user is audibly speaking"* — refuses a stop signal that has gone stale |

Together they make **an open turn an uninterruptible one**, and the only exit is the 5 s
`user_turn_stop_timeout` — an *inactivity* timer that every VAD frame, every transcript
and every audio frame re-arms. Recovery therefore requires the user to stop speaking, and
continuing to speak is what holds it shut.

It gets in through `trigger_user_turn_stopped()`, which is two events across the second
guard:

```python
await self.trigger_user_turn_inference_triggered()   # push_aggregation() -> the LLM runs
await self.trigger_user_turn_finalized(...)          # <- refused if _user_speaking
```

The first awaits through `push_aggregation()` → `push_context_frame()` → `push_frame()`
across the whole pipeline below the aggregator; when the trigger came from the stop
strategy's own `_timeout_handler` **task**, the aggregator's input task is free to run a
queued `VADUserStartedSpeakingFrame` in that gap. `Resume` is that gap, and it is the
only interleaving the model needs.

| MODE | | `NoStrandedTurn` | `NoMissedBargeIn` |
|---|---|---|---|
| `asWritten` | stock pipecat — identical on 1.5.0, 1.7.0 and 1.8.1 | ✗ | ✗ |
| `retryOnQuiet` | `keep_barge_in_reachable`: a finalization refused **after its inference ran** is re-applied at the user's next quiet moment | ✓ | ✓ |

`NoStrandedTurn` is the state itself — a turn whose inference has already run, still open
once the user has fallen quiet. `NoMissedBargeIn` is what the user experiences, and its
counterexample is nine states with nothing exotic in it:

```
VadStart     the user speaks
Interject    >= INTERRUPT_MIN_WORDS: the turn opens, the bot is cut. Barge-in worked.
VadStop      they pause
Inference    the stop strategy concludes; the words go to the context, the LLM is asked
Resume       they resume INSIDE that await
Finalize     refused: "never finalize while the user is audibly speaking".
             The turn stays open with its inference already spent.
BotStart     the reply that inference produced starts playing
Interject    >= INTERRUPT_MIN_WORDS over the bot -> _trigger_user_turn_start returns
             early because a turn is open. No interruption: the barge-in is missed.
```

Everything after `Finalize` is stable: every further `Interject` re-arms the watchdog,
so `ForceStop` never becomes enabled, and the bot cannot be cut until the user stops
speaking for long enough that `Arm` fires.

### What was changed

`endpointing.py`'s `keep_barge_in_reachable` wraps the one controller the session builds.
It does **not** widen the guard — a refusal with no inference behind it is still honoured,
which is the case the guard exists for and is pinned by
`test_a_refusal_with_no_inference_behind_it_is_still_honoured`. It only says that once the
inference *has* run, the turn's content is already spent: refusing the finalize cannot
un-ask the question, only strand the turn. So that decision is re-applied at the user's
next quiet moment — `ENDPOINT_STOP_SECS`, against the watchdog's unreachable 5 s.

This is not a pipecat regression. The controller is byte-identical across 1.5.0, 1.7.0 and
1.8.1, so the hazard has been here since the initial release; what changed was exposure.
`e983f90` deleted `LatchedTurnStopStrategy`, whose overrides were the only thing that
re-attempted a lost stop — the three bugs it worked around were fixed in 1.6.0/1.7.0, this
fourth one lives in the *controller* rather than the strategy and was never covered.
`f23503b` then cut `ENDPOINT_STOP_SECS` 0.5 → 0.2, multiplying the VAD edges per utterance,
and 1.8.1's rolling volume window holds `speaking` ~0.35 s longer past the end of speech.

### The speculative reply (`SPEC`)

`speculate.py` (`TEAPORT_SPECULATIVE_REPLY`, off by default) asks the LLM the moment a
final transcript lands on a turn the stop strategy has *not* concluded on — Smart Turn said
INCOMPLETE and `SMARTTURN_STOP_SECS` is running — against a snapshot of the context with
that text as the user message. At the commit the LLM service is offered that stream instead
of opening its own. It is a second machine riding on this one, so it is modelled here rather
than on its own: `SpecStart` reads `userTurn` and writes nothing the controller's guards
read, and whether that leaves barge-in intact is a question for TLC, not a promise.

The premise — a final in hand while the ceiling runs — is only true with
`TEAPORT_STT_COMMIT_ON=vad-stop`, the STT committing at the VAD stop before the verdict
exists. Under the default `verdict` (#43, `SttCommit.tla` below) the STT holds its segment
open through that same wait, no final lands before the commit, and `SpecStart` is
unreachable in the code. The rows below are unaffected: `SpecStart` is a free action, so
the model checks a superset of what either configuration can do, and the properties are
safety properties. They describe `vad-stop`; under `verdict` the speculation is inert and
`agent_session` says so at startup.

The property it adds is `NoStaleReply`: a speculated reply is spoken only for the context it
was asked against. The text being right is not enough. Between the snapshot and the commit
the context can have other writers, and `CtxChange` is any of them. In the code as it
stands there is no known one: `HeardContextCorrector._reconcile` on the very
`LLMContextFrame` that carries the commit was the first miss seen live, so `speculate.py`
now runs it *before* the snapshot; `MemoryRecall`'s note is injected before the aggregator
sees the final, so it is always inside the snapshot; the consult follow-up posts only with
the turn free, and a speculation only exists inside an open turn. The model keeps
`CtxChange` as a free action because nothing in the machine prevents the next writer, and
the whole-context check is what turns one into a miss instead of a wrong reply.

| SPEC | promotes when | `NoStrandedTurn` | `NoMissedBargeIn` | `NoStaleReply` |
|---|---|---|---|---|
| `off` | — (the four MODE rows above, unchanged: 47 states) | | | |
| `byText` | the committed text is what was asked | ✓ | ✓ | ✗ |
| `byContext` | the whole context equals the snapshot — `Speculator.take` | ✓ | ✓ | ✓ |

The `byText` counterexample is seven states and needs no barge-in at all:

```
VadStart     the user speaks
Interject    the turn opens
VadStop      they pause; Smart Turn says INCOMPLETE, the ceiling starts
SpecStart    the final lands, the stop does not fire: the LLM is asked now
CtxChange    something writes the context (the corrector's truncation of the cut
             reply, before it was moved ahead of the snapshot)
Inference    the ceiling ends the turn; the text is what was asked, so byText
             adopts the stream -- a reply the model produced against a context
             the commit no longer has
```

`byContext` closes it: `specGen = gen` is the equality check on `context.messages`.
The adoption rule is one definition (`Adopt`) shared by both designs, and `staleReply`
is raised whenever an adopted reply's generation is not the commit's — so the byContext
row is a real check of the equality rule, not exempt by fiat (drop the `specGen = gen`
conjunct from `Adopt` and the row falls). The two barge-in rows hold with it on because nothing in
`SpecStart`/`CtxChange` touches `userTurn`, `userSpeaking` or `stopInFlight`. A resume
(`VadStart`) and a new turn (`Interject`) both close the speculation, which is why `byText`
never fails on the *text*: every live speculation's text is the committed one, and the
whole difference between the two designs is what else the context holds. The commit is
`Inference` or `ForceStop` — the watchdog's stop pushes the aggregation too, so it reaches
`Speculator.take` and decides the speculation the same way.

### Known limits of this model

- **`Inference` requires `~userSpeaking`**, which the controller does not: the guard is on
  the second await only. It is a fact about the *strategy* — both of its conclusion points
  run with VAD quiet. Without it TLC finds a two-step "the user simply never stopped"
  trace the implementation cannot produce, and the counterexample stops being a bug report.
  The cost is one excluded entry: `_timeout_handler` already running when the resume's
  `_discard_pending_end_of_turn()` tries to cancel it, which reaches the same state one
  step earlier.
- **`BotStart` waits for `~stopInFlight`.** An LLM round trip plus synthesis against two
  awaits — an ordering fact, stated rather than modelled, as with `LlmStart` in
  `LedgerPlayout.tla`.
- **A VAD frozen in SPEAKING is out of scope, and out of the fix's scope too.**
  `SIP_HALF_DUPLEX` left on, or a desynced gateway AEC feeding the bot's own audio back
  in, pins `userSpeaking` forever: no `VadStop` ever arrives, so nothing re-applies the
  finalization and nothing arms the watchdog. Those kill barge-in on their own, upstream
  of all of this, and both have their own kill switch. Modelling them here would only
  make this module look like it covered them.
- **Durations are out of scope**, as everywhere here. That the watchdog is 5 s and the
  retry fires within `ENDPOINT_STOP_SECS` is the reason one is unreachable and the other
  is not; the model only distinguishes reachable from absorbing. The same goes for the
  speculation: whether it is worth anything is the ceiling against the final's latency
  and the LLM's (measured in `speculate.py`), and the model says nothing about that —
  only that a stale reply is never spoken and barge-in is as it was.
- **`CtxChange` is a free action** bounded by `MaxCtxWrites`, not a list of writers.
  The model does not say which component a miss will come from — `[SPEC] miss
  reason=ctx-changed` in the journal does — and it does not know that the code's known
  writers all land outside the window today (see above); it checks the design that
  stays right when one does not.
- **Scale.** 47 distinct states with `SPEC = "off"`, 291 with it on; well under a second.

## `SttCommit.tla` — when the STT closes the engine's transcript segment (#43)

A commit to the engine is irreversible: `finish()` right-pads and redecodes the buffer,
then resets the stream. `stt.py` sent one on every `VADUserStoppedSpeakingFrame`, before
Smart Turn had answered — so a breath the model would have called INCOMPLETE split the
caller's sentence into two decodes that never saw each other (`"Hey, what's"` +
`"Time is here."`, measured 2026-09-18 at `ENDPOINT_STOP_SECS=0.2`, 4 of 4 runs). The
pipecat turn stayed open, as the verdict said it should; the segment under it was gone.

The fix has the stop strategy answer every VAD stop with a `TurnVerdictFrame` pushed
*upstream* — the model's verdict, and the silence ceiling's when an INCOMPLETE one falls
through — and the STT commit on a complete one, holding the segment open on an incomplete
one. That is a new concurrent protocol: two processors, two FIFO queues in opposite
directions (a verdict can arrive after the VAD *start* that followed its stop), and two
timer tasks — the hold's expiry, and the stranded backstop that now has to stand down while
a segment is held and stand back up when it is released. This module is that protocol,
with two faults in the environment: a verdict lost in flight (`LoseVerdict`) and a VAD stop
the VAD never reports (`VadStopMissed`, the fault the backstop exists for). A third
environment action closes the turn under a running ceiling on something other than the
ceiling (`TurnStop`: the controller re-applying a stop it refused earlier, or its stop
watchdog); what the strategy does then is `TURNSTOP`.

| MODE | | `NoSplitOnIncomplete` | `NoStrandedSegment` | `NoDoubleAnswer` | `NoStaleClose` | `NoOrphanedHold` |
|---|---|---|---|---|---|---|
| `vadStop` | commit at the raw VAD stop (until #43) | ✗ | ✓ | ✓ | ✓ | — |
| `verdict`, `TURNSTOP = "silent"` | the first cut: nothing reported when the turn is closed under the ceiling | ✓ | ✓ | ✓ | ✓ | ✗ |
| `verdict`, `TURNSTOP = "verdict"` | `TEAPORT_STT_COMMIT_ON=verdict`, as shipped | ✓ | ✓ | ✓ | ✓ | ✓ |

`NoSplitOnIncomplete` is the bug, and `vadStop` fails it in five steps with nothing exotic:

```
VadStart     the caller speaks
VadStop      they pause; the STT commits -- the segment is closed, unjudged
StratStart
StratStop    the model says INCOMPLETE; the ceiling starts
VadStart     they resume inside it: the model was right, the sentence is two decodes
```

The other three are what the old design did right and the new one has to keep doing:

- **`NoStrandedSegment`** — words in the segment, the caller quiet, and no action that will
  close them: no timer that can fire, no frame in flight, no ceiling counting. It is written
  with `ENABLED Backstop` / `ENABLED HoldExpire` rather than the armed flags, because a
  model whose expiry never fires must not pass on the strength of `hold = TRUE`. This is
  the property the hold threatens, since the hold is what stands the backstop down.
- **`NoDoubleAnswer`** — one VAD stop, at most one commit answering it. A second one closes
  an empty segment, pays the engine's fixed `finish()`, and logs an EMPTY final.
- **`NoStaleClose`** — a verdict never closes the segment once the caller has audibly
  resumed: the STT already knows that pause was not the end, and the words belong in this
  segment. Holds by queue order plus one cleared flag (`_commit_pending`); it is here so the
  flag cannot quietly stop being cleared.
- **`NoOrphanedHold`** — a held segment always has a verdict on its way: one in flight, a
  stop the strategy has yet to process, or a ceiling counting — unless a verdict for *this*
  stop was lost. The expiry is the backstop for that loss and for nothing else. A design
  that leaves a hold with no verdict coming has made the expiry its normal way out, and
  none of the three above can see that: the expiry keeps the segment from stranding and
  answers the stop exactly once. It is also what turns `HoldExpire`'s stated timing fact
  into a checked one — with this holding, the expiry is reachable only after a loss.

### What the model found

Writing `NoStrandedSegment` found a state the first cut of the code could reach and the old
code could not: a stop is held, the segment's words lag in from the engine (the backstop
stands down, correctly), the caller resumes (the hold is released), and their next stop is
*missed*. Nothing owns the words — the old code had already banked them at the first stop.
`stt.py` now puts the backstop back on duty at the resume when the held segment has words
(`VadStart`'s `backstop' = backstop \/ (pending /\ segSpeech)`); drop that conjunct and the
row falls in ten states. Every other load-bearing piece was checked the same way, by
mutation: the expiry's timing fact (dropping it fells `NoSplitOnIncomplete`), the expiry
itself (`NoStrandedSegment`), the flag a resume clears (`NoStaleClose`), and the backstop's
two guards together (`NoSplitOnIncomplete`; either alone suffices, the code keeps both).

The review of the code (PR #45) then found the state the model could not reach: a turn
closed under a running ceiling. The stock strategy is reset and its analyzer cleared at
every turn stop, whichever path closes the turn, and three paths close it under an
INCOMPLETE verdict's ceiling — the ceiling itself with a final in hand (it fires the stop
inline, and the flag the push compared was already reset by the time it was read); the same
inline stop at the VAD stop for a COMPLETE verdict with the final already in hand (reported
as INCOMPLETE, for the same reason); and `keep_barge_in_reachable` re-applying a refused
stop right after the strategies handled the VAD stop. In every case the STT held the
segment to its expiry and logged a lost frame. The model had `HoldExpire` enabled only once
no verdict could still be coming — the timing fact, *stated* — so it could not represent a
hold that nothing would ever answer, and every row passed. `TurnStop` and `NoOrphanedHold`
are what was missing: with `TURNSTOP = "silent"` the row falls in seven states
(`VadStart`, `VadStop`, `StratStart`, `StratStop` incomplete, `TurnStop`, the INCOMPLETE
delivered — pending, nothing in flight, no ceiling, nothing lost), and with the strategy
reporting the close it holds. The two inline cases are the same hole entered from the
strategy's own handlers; the code closes it by reading what the turn did (the inference
count) rather than the flag, and the model does not distinguish them from `TurnStop`.

### The final (PR #49): whose utterance a late final is, and when the turn may end on it

PR #49 fixed the turn ending on the *previous* utterance's final. The engine's `finish()`
takes ~0.7 s, a caller who speaks in bursts starts the next utterance inside it, and the
stock strategy took every finalized `TranscriptionFrame` as the transcript of the utterance
it was waiting on: at B's VAD stop the COMPLETE verdict ended the turn on A's words, B's
final then opened a new turn and its interruption cut the reply to A before a word played
— 38 of 431 replies since 2026-09-16. The PR as opened left this model alone on the
strength of two of the limits below as they then read ("the final is not modelled", "the
engine's own segment close is one processor's local rule on a final"), and those were
exactly what the change touched: the model passed unchanged against code it no longer
described, which is the PR #13 failure this file documents. The review found three
orderings the first cut got wrong, all interleavings of the same two processors plus the
engine, so the model was extended rather than a new one written.

What was added: the engine's own close (`EngineClose`, its endpointer on a shorter silence
than the VAD's floor or a breath inside a sentence, bounded by `MaxCloses`), the dones on
their way back (`fromEngine` — one per commit, in order, and the engine's own closes
interleaved), the STT's queue of commits awaiting an answer and how a done is paired and
stamped (`Done`), the interims and finals on the strategy's queue, and the strategy's turn
itself: `turnOpen`, `_text`, `_transcript_finalized`, `_turn_complete`, the p99 safety
net as a timer that can fire any time after the stop, pipecat's reset at a turn start
(`OpenTurn`, with the late-start rule) and the end (`EndTurn`, given priority in `Next`
because the code ends the turn inside the handler that made it possible). Words are
tracked by utterance so the monitor can say what was still to come. Two constants select
the design and the wire:

| `DESIGN` | `DONES` | | `NoStaleTurnEnd` | `StaleOnlyByMispairing` | `NoStrandedTurn` |
|---|---|---|---|---|---|
| `clock` | `unmarked` | PR #49 as opened: a final placed by clock (`sc_final_clock`) | ✗ | | |
| `clock` | `marked` | the same on a wire that says which dones answer commits (`sc_final_clock_marked`) | ✗ | | |
| `stop` | `unmarked` | as shipped, on the wire as it is (`sc_final_stop`, `sc_final_stop_residual`) | ✗ | ✓ | ✓ |
| `stop` | `marked` | as shipped, with the one-field engine change (`sc_final_stop_marked`) | ✓ | | ✓ |

`NoStaleTurnEnd` is the property: the turn never ends on VAD stop *k* with words of an
utterance up to *k* still in the engine's open segment, in a done on its way, or in a
final queued to the strategy — what lands next opens a new turn on them and cuts the
reply. `NoStrandedTurn` is its dual, so that the wait the fix introduces cannot become
the aggregator's 5 s watchdog: a turn the caller has stopped for always has a frame or a
done in flight, a ceiling or the net counting, a timer that can fire, or the end enabled.
The five commit properties are checked again under the same environment
(`sc_final_stop_residual`).

**The `clock` rows fall in nine states**, on the marked wire too, so the fault is the
rule's own: the engine cuts a breath inside the sentence; the VAD stop for the whole
sentence puts the segment on hold; the engine's done for the first half lands *after* the
stop, and the first cut stamped it with its arrival (later than the strategy's read of the
VAD start) and released the hold on the strength of it. The strategy took the half as the
utterance under way, ended the turn on it, and the rest of the sentence sat in a segment
nothing would commit: the COMPLETE verdict found nothing pending. That is the review's
finding 1 (the arrival stamp) and finding 3 (the engine's split of one utterance) in one
trace; the clock's other fault — two readings taken at different queue positions, so that
a commit sent after the STT saw a VAD start can still be stamped before the strategy
dequeued it — is why the stamp became a *count*.

**The `stop` design was rewritten twice by this model before it held.** The rule as
first written here gated the turn's end on "the latest stop counted has been answered,
with the caller quiet since", reset at every turn boundary like the stock state around
it. With `MaxCloses = 0` the model found the backstop committing half a sentence while
the caller was still talking (stamp 0: no stop yet), the stop's verdict INCOMPLETE and
the rest held, the controller closing the turn under the ceiling (`TurnStop`), and the
backstop's half landing to open a *new* turn and end it through pipecat's fallback for a
transcript with no VAD stop in sight — 0.3 s later, with the held half still owed. Gating
that path on the transcript's stamp was the second cut, and with the engine's close on the
model found its twin: the engine's cut landing after the stop is stamped as the *next*
stop's, so the stamp gate let it through. What the strategy actually knows in both traces
is that it counted stop 1 and never saw a close for it; so the shipped rule is that a
counted stop is *owed* its close until a close stamped with it lands — whoever is
speaking when it does — and no path ends the turn while a stop is owed: not the verdict,
the ceiling, the p99 net, nor the fallback, and not across a turn boundary either, since
the debt is a fact about the STT's segment, not the turn. The STT changed with it: a done
nothing asked for no longer releases a hold (the first cut's release is what stranded the
rest above; the ceiling's commit now closes the silent tail, which the engine answers
without a decode), and its stamp is the next stop's if it carries words, the last
commit's if it does not. `tests/test_earlier_final_does_not_end_the_turn.py` pins each
shape.

**The `stop` row on the unmarked wire falls in ten states, on one cause.** The engine
cuts a breath; the VAD stop holds; the verdict commits *before* the engine's done for the
first half has landed; the STT, pairing dones with commits by order because the wire does
not say which is which, hands the commit's stamp to the engine's done; the strategy takes
the half as the stop's close and ends the turn while the commit's real answer — the rest
of the sentence — is still on its way. `sc_final_stop_residual` is the statement that this
is the *only* way the shipped design ends a turn stale: `StaleOnlyByMispairing` holds,
with the turn never stranding and the five commit properties intact, over the same
environment. `sc_final_stop_marked` is the statement that a done which says whether it
answers a client commit (`voxtral_websocket.c`'s `worker_send_done` sends the same message
for its VAD close and for a commit) closes it entirely. Live, the mispairing needs the
engine's finish for the first half to outlast the rest of the sentence plus the VAD's
floor and the model's verdict — the GPU-contended finish — and its cost is one cut reply,
the shape the fix is for, at a small fraction of its old rate.

### Known limits of this model

- **Durations are out of scope**, as everywhere here, and two timing facts are stated as
  enabling conditions. `HoldExpire` is enabled only once no verdict can still be on its way
  — none in flight, no stop the strategy has yet to process, no ceiling counting — which is
  the claim that `SMARTTURN_STOP_SECS + 1.0 s` outlasts all three (`NoOrphanedHold` checks
  that such a state is only ever reached after a loss; the number itself is a guess, and
  `stt.py` logs how late a verdict that missed its expiry was, which is what to size it
  from). `Backstop` can fire whenever the segment has words and no stop has claimed them,
  including while the caller is still talking: that is the known edge `stt.py` documents
  (stalled interims), kept.
- **The over-bot path is not modelled.** A VAD stop while the bot is speaking commits at
  once (the flush is the barge-in, teagram-engine#7); it is the `vadStop` design applied to
  one stop, and the verdict that follows is ignored exactly as a stale one is.
- **The session-wide fallback is not modelled.** A session in which no verdict ever reaches
  the STT falls back to `vad-stop` at its first expiry, and back onto the verdict at the
  first verdict that arrives; that is a policy on repeated faults, and `MaxLost = 1` never
  reaches it.
- **The engine's own close is an environment action with no timing.** It can fire whenever
  the segment holds speech — the breath inside a sentence, the trailing silence the VAD
  has not confirmed yet — and its done lands whenever. That is a superset of the engine
  (`g_vad_trailing_ms` of its VAD's silence, one finish at a time), so the rows hold for
  it too. Its close is not a split the STT is charged with (`NoSplitOnIncomplete` reads the
  STT's commits), and it is bounded by `MaxCloses`; the four commit rows run with it off,
  where their faults (`MaxMissed`, `MaxLost`) keep the state space small.
- **The wire's pairing is the shipped design's residual, stated as a row.** A done that
  answers a commit and a done from the engine's own close are the same message, so the
  STT pairs by order and the engine's close can take a commit's place (`mispaired`).
  `sc_final_stop` keeps the counterexample and `sc_final_stop_residual` proves it is the
  only one; the engine-side marker is the fix, and `DONES = "marked"` is that engine:
  teagram-engine's `transcription.done` carries `"reason": "commit" | "vad"` since the
  trailing-silence credit (its PR for issue #29), and `stt.py` places a done marked
  `vad` as the engine's own close whatever the queue holds. On that engine the marked
  rows are the shipped configuration; against an older one the field is absent and the
  queue pairs as before, so the unmarked rows still describe it.
- **One interim per segment.** `Delta` fires once per open segment: further deltas change
  nothing this model reads, and the engine streams none for a new segment until the
  previous finish is done (one worker per session), which is when the STT's buffer
  empties. Without it the queue to the strategy is unbounded.
- **The backstop is bounded** (`MaxBackstops`) because every firing opens a segment the
  caller can refill; `WayOut` reads its guard rather than the bounded action so the bound
  cannot fake a stranding.
- **The no-stop fallback's turn end is modelled; the missed-stop turn is not rescued.** A
  turn whose VAD stop the VAD never reports is stranded at the strategy (it never sees a
  stop) and ends on the aggregator's watchdog, as live; `NoStrandedTurn` asks only about
  stops the strategy saw.
- **The turn-stop split is accepted, not checked.** `TurnStop` closes a segment the model
  called INCOMPLETE, and a caller who then resumes is split — the continuation
  `keep_barge_in_reachable` documents and accepts. The `NoSplitOnIncomplete` monitor is
  disarmed with the ceiling at that close, deliberately, and the stale-end monitor is not
  consulted at it either: what `TurnStop` leaves owed is what the owed-close rule then
  keeps the *next* turn from ending on.
- **A commit whose send fails is not modelled.** `stt.py` queues no stamp for it (the first
  cut left its stamp for the next done to inherit); the turn it strands ends on the
  watchdog, and the receive task owns the reconnect.
- **Scale.** The four commit rows: about 70,000 distinct states each with the engine's
  close off, a few seconds. The five final rows: 2.8 million states and about half a
  minute each for the two that hold (`MaxStops = 2`, `MaxLost = 1`, `MaxCloses = 1`,
  `MaxBackstops = 1`, `MaxMissed = 0` — with the missed stop on as well the space passes
  30 million and the row is not worth its minutes); the three that fall find their trace in
  under ten states. `check.sh` passes `-deadlock`: the bounds make terminal states ordinary.

## Worth modeling next

`UserTurn.tla` was never on this list, and that is the most useful thing this list has
told us. Everything below is a consequence a model already reaches; the barge-in
precondition was a *premise* five models shared, and a shared premise is invisible to
every one of them. Worth asking of each new entry: is this a step the machine takes, or
something the machine is assumed to be handed?

1. **Spoken notices in `LedgerPlayout.tla`.** The two `TTSSpeakFrame` call sites that
   still default to `append_to_context=True` (`llm_error_speaker.py:81`,
   `agent_session.py:401`) are now charted as utterances of their own; whether they
   should be committed to the LLM context at all is `HeardContextCorrector`'s invariant,
   not the ledger's — item 2.
2. **`HeardContextCorrector`'s `_done`/`_mark` bounds** against ledger growth and
   `set_messages`. PR #13's `append_to_context=False` on the tool-ack line
   (`tools.py:737`) belongs here too: a turn the bot spoke that leaves no assistant
   message in the context is a context invariant, not a ledger one.
3. **`MemoryRecall`'s single-flight + turn generation tag** — small and already
   carefully written, so cheap regression insurance rather than a suspected bug.
