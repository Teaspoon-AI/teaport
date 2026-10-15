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

### The delivery loop (#30): delivered means heard

The table above stops at "a completion read it". #33 moved the bar: the trigger is read
the moment the model starts answering, before a sample reaches the caller, so a delivery
barged over at 0% heard was retired as delivered (live 2026-09-10 17:13). The loop in
`speak_followup` now asks whether the caller **heard** it, and below `_MIN_HEARD` (0.3,
`TEAPORT_FOLLOWUP_MIN_HEARD`) posts it again, up to `_DELIVERY_ATTEMPTS`. `MODE = "heard"`
models that loop, and the two read-count properties mean what the loop needs:

| | `MODE = "heard"` |
|---|---|
| `NoSilentLoss` | the loop never ends *delivered*, and no copy left in the context says "already given", unless a recital was heard to the end or cut at ≥ `_MIN_HEARD`. Running out of attempts is not a loss: the note left behind (`_UNTOLD`) says it never arrived |
| `NoRepeatRecital` | no recital begins after the caller has heard one — the loop retries on purpose, so "read twice" is no longer the line |
| `NoStandingOrder` | the loop never ends with a live trigger (the budget retires it regardless) |
| `LiveIsArmed` | a live trigger always has a shot armed: whatever completion reads it next takes it out |

What the model adds to reach those: the read point at the completion's **first text**
(`FollowupTrigger`, which fires *every* armed shot on *any* completion's first text);
the heard outcome `HEARD | CUT_ABOVE | CUT_BELOW` from **two signals** — the transport's
`Delivery` (settled by `BotStoppedSpeaking` once it spoke, or by any interruption) and the
ledger's `next_assistant()` (which charts a cut at the interruption, a finished reply only
later, and **no turn at all for a reply whose TTS never opened**); the window's claim and
`TurnCommitMark`; whether the trigger is still the **tail** of the context; and the attempt
budget. A trigger at the tail is the turn the model answers. One buried under the
caller's words may be answered or not, and both happened live: 08:17 recited a buried one,
17:36 answered the caller instead. So the model lets it go either way.

The environment includes what the code can actually see happen:
- a caller's turn that commits nothing (an empty final: no completion queued);
- a completion whose text never becomes audio (emptied by the guard, #70; no speech; the
  LLM failing after its first token);
- a filler line (tool-ack, narrator: `append_to_context=False`) playing during a tool call;
- `FollowupTrigger` firing on *any* token while a shot is armed, not only on a
  completion's first.

One timing assumption is explicit: a queued completion claims the window (at
`TurnCommitMark`) and starts within milliseconds, long before a 0.7 s debounce could
end. The gate's claim is a single flag, released by the first completion to start, so
with two queued the second runs unclaimed. Untimed, no window opens while one is queued.

`REPOST` picks where a retry puts the trigger, `FIX` (a set, `{}` = the code before
this change) the parts of the fix, and `TIMED` the exits only a timer takes:
`max_wait` and `wait_out_claim`'s give-up, a 20 s start timeout that overtakes a slow
completion, and the 45 s heard timeout while audio is still playing. Untimed, every wait
ends on the event it waits for, or on its timer once that event can no longer come.

**17:36, found by TLC this time** (`fu_heard_inPlace_loss`). The first retry restored the
trigger *in place*. By then the caller's barge-in is the tail and the trigger is buried
above it. The retry's completion answers the caller, its first text fires our shot, the
ledger charts *that* reply cut at ≥ 30%, and the loop returns delivered. The live
incident was exactly this. `42d0a2b` re-posts at the tail (`REPOST = "tail"`).

**Before this change, the loop still lost answers and still repeated them.** Each
counterexample below was confirmed against the code and pipecat 1.8.1 by an independent
review:

1. **Cut before the TTS opened** (`fu_heard_shipped_loss`, 7 steps). Our completion
   answers from the trigger, and its first text retires it. (With mood tags on, that
   first text is the `[mood: …]` tag itself.) The caller barges in while the TTS is still
   aggregating the first sentence. `TranscriptLedger._interrupt` charts only the turns a
   `TTSStartedFrame` opened and drops the streaming entry, so nothing is charted.
   `Delivery` is cut with `spoke == False`. The loop waits `_DELIVERY_CHART_TIMEOUT` for
   a chart that never comes and logs "cut, but the ledger charted no reply to measure —
   leaving it as delivered". The caller heard 0%. 17:13 was this, 84 ms on the safe side
   of `TTSStartedFrame`. If the caller's own reply opens and is charted within those
   5 s, the waiter measures *that* reply instead, with the same result.
2. **The caller's reply fires our shot** (`fu_heard_no_void_repeat`). A barge-in right
   after the post, before anything answered: the caller's words are committed below the
   trigger, and their reply's first text fires our still-armed shot. That reply read the
   trigger buried. It may have answered the caller and not the trigger, and its audio is
   what the heard check then measures.
3. **A live trigger with no shot** (`fu_heard_shipped_repeat`). An attempt abandoned on
   the start timeout ("turn was flushed before the model read it") leaves the copy *live*
   and disarmed while it waits for the next window. The easy way in is a barge-in that
   commits nothing (a 0-char final). Every turn the caller takes meanwhile reads it
   buried, with nothing to retire it: the 08:17 standing order, back inside the retry
   loop. A recital the caller hears there is followed by the re-post's recital.
4. **A filler line counts as the answer playing** (`fu_heard_no_ownAudio_loss`). The
   gate marked a watched `Delivery` spoke on *any* `BotStartedSpeaking`, and settled it
   as played on the next `BotStoppedSpeaking`. When the follow-up's completion calls a
   tool first, the tool-ack line plays, the watch settles as played, and the answering
   completion's first text fires the shot. The loop logs "delivered and played to the
   end" before the answer has made a sound.
5. **Every re-post says "already given"** (`fu_heard_no_supersede_loss`). `_post_trigger`
   retired the earlier copy with `_retire()`'s default, `_SPENT` ("already given to the
   user"). A re-post only happens because the caller did *not* get it. After the last
   attempt fails, only the newest copy says so, and every earlier one still says the
   opposite: the #80 lie, once per attempt.
6. **Text that never becomes audio** settles nothing. No `BotStarted`, no `BotStopped`,
   no interruption. The 45 s heard timeout then "left it as delivered". That timer is
   not standing in for a slow event here, since the event never comes.
7. **A context that opens and never plays** (`fu_heard_ownStart_loss`), found after the
   first version of this fix by the ledger's own audit: pipecat opens a TTS context for
   any non-blank text, so an emoji-only reply, or one whose every clause the engine
   failed, passes its `TTSStartedFrame` and then nothing. A watch that counted that frame
   as the reply speaking rode the heard timeout to "delivered".

**What was changed.** The fix is `FIX = {"void", "retireUnarmed", "unspoken",
"ownAudio", "supersede"}`, and `fu_heard_fixed_all` holds. It lives in
`followup_gate.py` (`OneShot.void`, `FollowupTrigger` on `InterruptionFrame`, `Delivery`
marked spoke by the reply's own `TTSStartedFrame`) and in `speak_followup`. Each
counterexample is a test in `brain/tests/test_followup_heard.py` /
`test_followup_trigger.py` / `test_followup_gate.py`, failing against the loop before it:

- `void`: an `InterruptionFrame` voids every armed shot that asked for it (armed with
  `on_void`) instead of leaving it for the next completion's text. The consult's copy is
  retired as `_UNTOLD` at that moment, and the loop treats the attempt as flushed.
  Closes 2.
- `retireUnarmed`: an attempt abandoned unanswered retires its copy as `_UNTOLD` *before*
  waiting for the window. Closes 3. `LiveIsArmed` is the invariant this buys.
- `unspoken`: a reply with `Delivery.spoke == False` is heard 0%, whether it was cut or
  hit the heard timeout, without asking the ledger. Closes 1 and 6.
- `ownAudio`: for the consult's watch (`watch_delivery(replies_only=True)`),
  `Delivery.spoke` is set by audio that follows a reply's own `TTSStartedFrame` (not a
  filler's, `append_to_context=False`), not by `BotStartedSpeaking`. Frames reach the gate
  in playout order, so audio between a reply's `TTSStartedFrame` and `TTSStoppedFrame` is
  that reply's. The `TTSStartedFrame` alone is not enough (`ownStart`, closes 4 but not
  7). Watchers of a line they speak themselves (`say_last_line`, the call prompt) keep
  "any audio", since their line may be a filler itself. Closes 4 and 7.
- `supersede`: the earlier copy is left saying only that a later notice supersedes it.
  Closes 5.

Each part is necessary: the `fu_heard_no_*` rows drop one each and fail. `no_void` is
the instructive one. With `unspoken` but no `void`, the barge-in's own reply can recite
the buried trigger and be heard while our watch, settled by the barge-in, says nothing
spoke. The loop retries, and the caller hears the answer twice.

**Timers** (`fu_heard_fixed_timed_*`). With the timer exits on, both properties fall
even with the fix. `NoSilentLoss` falls to a reply still playing at the 45 s heard
timeout ("never finished nor was cut within the window — leaving it as delivered").
`NoRepeatRecital` falls to a start timeout that overtakes a completion slower than 20 s,
which then recites a copy the loop has already retired and re-posted. Both need a real
wait longer than its timer: these are the accepted residuals, in the sense of UserTurn's
timed rows. `NoInterjectMidTurn` falls to the give-up by design ("speak anyway"). The
budget, the one live trigger and the narrator still hold.

**Exactly one live trigger.** `_post_trigger` retires the earlier copy before appending,
synchronously. The model has one slot for the current copy plus the strongest claim any
earlier copy makes (`stale`), so "two live copies" is not expressible in it. It is
checked by the code's construction, not by TLC.

### Known limits of this model

- **Liveness is not established.** `EventuallyDelivered` (`live.cfg`) fails only
  because the `MaxRuns` bound is exhausted, not because of a defect — the trace ends
  with `started = MaxRuns` and a turn still queued. Raising the bound doesn't settle
  it; a proper liveness argument needs fairness assumptions about the user, which the
  model doesn't currently state.
- **Durations are out of scope.** `_QUIET_SECS`, the delivery timeouts and the 0.3 s
  STT settle are timing assumptions TLA+ cannot judge. `TIMED` only says which
  properties a timer's exit can break, never how likely the wait is to outlast it.
- **One injector.** `ClientNotes` arms its reaction orders on the same
  `FollowupTrigger` and competes for the same claimed windows. The model has the consult
  follow-up alone. The claim keeps the two apart except on a give-up (`TIMED`). `void`
  is opt-in (a shot armed with `on_void`), and a reaction is armed without one. Its
  order stays through a barge-in on purpose, so the caller's turn folds the reaction
  into its reply. Nothing checks or retries a reaction, so none of the properties above
  are at stake for it.
- **The session ending** (`_session_over`, the copy retired untold) is not modelled. It
  only exits the loop.
- **What the model says** is reduced to "recites the answer or not". A buried trigger
  may be answered either way, as the live record shows. A reply that answers both
  the caller and the trigger counts as a recital.
- The 0%-heard residual this section used to list (a completion that reads the trigger
  and is barged away before speaking) was built in `cbe2795` (#33), with the
  `TEAPORT_FOLLOWUP_MIN_HEARD` threshold. The delivery loop above models it, and its
  first counterexample shows `heard_fraction` alone does not close it: the ledger never
  charts a reply whose TTS never opened.

## `SttSlot.tla` — arbitration of the engine's single STT slot

The engine serves one STT session. Who may hold it is the brain's decision; whether the
engine has *let go* of it when the brain thinks so is this model's question. `ARBITER`
selects how the brain decided:

- **`split`** (until #58 step 2, 8ef333f) — two front-ends contended through **separate
  arbiters with no shared lock**: the OpenClaw path tracked `_active_session`, the SIP
  path `active["call"]`. Each evicted only its own predecessor and neither could see
  the other's session.
- **`one`** (as shipped) — one `SessionArbiter` for every front-end of the process
  (Talk, the room mic, and since #58 step 3 the in-process SIP front-end). A newcomer
  asks under one lock; a holder that has to go is ended and waited out (its `release()`,
  bounded by `END_WAIT_SECS`) or, for a call against a conversation, put on hold (its
  STT disconnects); then `SETTLE_SECS`, then the grant. A session that ends on its own
  releases after its pipeline has stopped — a /talk session at once, a call after its
  teardown's own 0.3 s sleep — and a conversation held for that call gets the slot back
  at once and reconnects. Who may take the slot from whom (the ask, the hold, the
  resume) is `SessionArbiter.tla`'s; this model keeps only what touches the engine,
  and lets a newcomer end any holder but a call (the SIP front-end's contract).

In both, nothing observes that the engine has freed the slot: every wait is a sleep or
a release, and a release says the brain's socket closed, not that the engine has
processed the close. `closing` is that window.

| row | `ARBITER` | `MODE` | `ENDWAIT` | `NoFalseBusy` | `NoGrantedBusy` |
|---|---|---|---|---|---|
| `stt_fixedSettle` | `split` | settle, connect once, a refusal is final | | ✗ | |
| `stt_retryWhileBusy` | `split` | retry a rejection instead of trusting the sleep | | ✓ | |
| `stt_one_fixedSettle` | `one` | connect once | untimed | ✗ | |
| `stt_one` | `one` | retry — as shipped | untimed (the timing fact) | ✓ | ✓ |
| `stt_one_endTimeout` | `one` | retry — as shipped | timeout | | ✗ |

`NoFalseBusy` is the user-visible failure: someone is told *"Sorry, the voice
assistant is busy with another session right now"* — and on SIP hung up on — when no
session held the slot at all. Under `split` it fails with one OpenClaw session and one
SIP call:

```
Arbitrate/Settle    both sessions run their own arbiter; neither sees the other's
ConnectOk(g2)       g2 takes the slot
Leave(g2)           its client disconnects
Teardown(g2)        the socket closes — but the engine has NOT freed the slot yet
ConnectRefused(p1)  the caller's 0.3s settle already elapsed: 503, holder = none
                    -> "busy", hang up. Nothing was using it.
```

Note what made this fail *permanently* rather than momentarily: `stt.py`'s
`_connect_websocket` made a **single** attempt and set `_stt_available = False`, and
`greet()` polls only until the tri-state *resolves* — so a 503 resolved to False in
milliseconds and the 12 s poll window bought nothing against it.

The one arbiter did not make the retry redundant (`stt_one_fixedSettle`, seven
states): a Talk session whose client leaves releases the arbiter's slot as soon as its
pipeline has stopped, with no settle, so the next newcomer is granted and connects
inside the close; and a conversation held for a call reconnects the moment the call
releases, its settle being the call's own 0.3 s. The settle in `_ending`/`_holding`
covers only the holders the arbiter itself ends.

`NoGrantedBusy` is the same line when the arbiter *had* granted the slot: the session
it gave way to still holds the engine, so both lose — the one the arbiter told it was
ending (a Talk user hears that a call is coming in), and the one it let in (the caller
hears "busy" and is hung up on). It holds as shipped under one stated timing fact,
that an ended holder's pipeline lets go within `END_WAIT_SECS` (5 s). `_ending` does
not wait past that: it logs "did not let the speech engine go", clears the holder,
settles and grants, and `stt_one_endTimeout` is the counterexample in seven states (a
Talk session told to end whose teardown is still running when the wait gives up; the
newcomer's retry window of 1.2 s is spent against a socket that is still open). That
is accepted rather than fixed: the bound is what keeps a wedged pipeline from wedging
the arbiter, and a teardown that slow is already a fault of its own.

### What was changed

`_connect_websocket` now retries a **rejection** (4 attempts, 0.4 s apart), which
covers the hand-off window while staying inside `greet()`'s resolution window. Only a
rejection: an unreachable host will not fix itself, and each such attempt costs the
websockets open timeout (~10 s), which would leave the caller in silence instead of
hearing the warning. `_is_slot_busy` duck-types the status off the exception because
`websockets` is deliberately unpinned and the field moved (`.response.status_code` on
≥14, `.status_code` before); an unrecognised exception is treated as not retryable, so
the worst case is the previous behaviour.

On 2026-10-14 the model was brought to the one arbiter (#58): it still had the two
arbiters and both front-ends' 0.3 s sleeps, and so said nothing about the code it
named. `ARBITER = "split"` keeps the first finding reproducible; the `one` rows are the
code as it is.

### Known limits of this model

- `retryWhileBusy` retries *while the refusal is transient*, but the brain cannot
  actually tell a transient 503 from a genuine one — the engine returns the same
  thing either way. The implementation therefore bounds the retry by wall-clock, so
  the property holds only under the assumption that **the engine frees the slot
  within the retry budget**. That is a timing fact this model states rather than
  proves; see the limits note above.
- `MutualExclusion` is enforced by the engine, not by the brain, so it is a sanity
  check on the model rather than a claim about our code.
- The model says nothing about *which* session should win, nor about the ask: policy
  is `SessionArbiter.tla`'s, which in turn does not model the close window or the
  retry (its `NoEngineRefusal` reads a refusal as one the retry would have to absorb).
  The one policy fact kept here is that the arbiter never ends a call; a call's claim
  ended while a conversation is held for it is `SessionArbiter.tla`'s `sa_overlap_*`.
- A hold that outlasts `HOLD_WAIT_SECS` is cancelled mid-`hold()`; if that lands
  inside the STT's `_disconnect`, the close it was awaiting is abandoned with the
  socket reference dropped. Not modelled.

## `SipCall.tla` — the SIP per-call lifecycle, and the call's claim

One AF_UNIX SEQPACKET connection carries call control *and* caller audio, and
`SipConnection._receive_messages` is its only reader. Call-control handlers are
registered `sync=True` so they run inline there, in wire order — which is load-bearing,
because what one event starts the next may have to undo.

Since #111 the brain answers the call: `call.incoming` starts a bring-up **task**
(`start_call`) that asks the session arbiter for the engine (`CallClaim.start` →
`ARBITER.acquire`), builds and *publishes* the pipeline (`active["call"]`), sends
`call.answer`, and greets once `confirmed` arrives. Teardown (`cancel_active_call`) stays
inline when a handler runs it and ends with `presence.end(call_id)`. A pipeline that ends
on its own (`on_pipeline_finished`) tears itself down from a separate task, concurrent
with the reader. The 2026-08 modes keep the 2026-08 gateway (it confirms by itself, and may
confirm a second call before the first's `disconnected`); the shipped modes run behind
teaport-sip ≥ 0.6.0: one call ringing or up at a time (a second INVITE gets 486), except
that an INVITE may land while a call the *brain* hung up is still on its way out
(teaport-sip `call.cpp` `onIncomingCall`), and `auto_answer=false` unless `AUTO`.

| MODE | | `NoWrongTeardown` | `NoBringUpBlocksReader` | `NoBlockedWithPendingControl` | `NoUnclaimedCall` | `NoReplacedCall` |
|---|---|---|---|---|---|---|
| `asWritten` | 2026-08: teardown ignores `call_id`; bring-up inline | ✗ | ✗ | ✗ | | |
| `callIdChecked` | 2026-08: teardown matches `call_id` | ✓ | ✗ | ✗ | | |
| `heldClaim` | 2026-10, before the claim fix: `presence.end()` releases the claim held *now* | ✓ | ✓ | ✗ | **✗** | **✗** |
| `ownClaim` | the claim fix's identity check alone | ✓ | ✓ | ✗ | ✓ | **✗** |
| `shipped` | `sip_server.py` today | ✓ | ✓ | ✗ (accepted) | ✓ | ✓ |

`shipped` also holds `NoDeadLine` (an answered call is never let go without a hangup),
`NoStrandedRing` (a ringing call the brain heard is being brought up or hung up),
`SequentialClaims` (a call's claim is let go before the next call's is taken — the
contract `SessionArbiter.tla`'s `SIP = "sequential"` assumes) and `NoClaimLeftBehind`
(once every call is over, every claim has been let go), with and without `AUTO`.
`NoDeadLine` and `NoStrandedRing` lean on the 0.6.0 gateway: `start_call` supersedes a call
without hanging it up, which the 2026-08 gateway turned into a dead line (both 2026-08
modes fail `NoDeadLine`).

`NoWrongTeardown` (2026-08) — confirm A, confirm B, disconnect A; the stale `disc(A)`,
queued behind the blocked reader, is dispatched after B has replaced A and **tears down
B**. Fixed by the `call_id` guard; the bring-up task is why the backlog no longer forms.

`NoBlockedWithPendingControl` is the strict structural statement (any inline wait with
control queued). The 2026-08 fix removed the long pole (`NoBringUpBlocksReader`: the
arbiter's question, up to ~30 s, and greet()'s 12 s poll); the inline teardown still
blocks, bounded by `cancel_active_call`'s 5 s wait + 0.3 s settle (+ the reclaim).
`sip_shipped_teardown` keeps that as the accepted residual.

### The 2026-10-14 audit: what the model had drifted from

The model still described the 2026-08 code. Against `sip_server.py` then (the line
numbers are the audit's, before the claim fix):

1. Only `confirmed`/`disconnected` existed. Now `call.incoming` starts the bring-up
   (`sip_server.py:609-638`, `start_call` `:928-949`), a `confirmed` for a ringing call
   only marks it answered (`:886-904`), and the brain sends `call.answer` (`:698-712`).
2. "A new confirmed supersedes any running call" — now `start_call` from `incoming`,
   unless a call is up and answered (`:624-636`), or from a `confirmed` with no ring.
3. `SetupDone` published the call at the end of the bring-up; the code publishes
   *before* greeting (`:840-842`), so a `disconnected` can find it mid-bring-up.
4. Teardown was atomic. It is inline and awaits (`:532-591`, run from `:917-918` and
   `:945`), and the reader is blocked meanwhile.
5. No claim. `CallClaim` (`:330-382`) takes the arbiter in the bring-up (`:751`) and is
   released by `presence.end()` at the end of every teardown (`:573`) and on a
   `disconnected` that leaves nothing behind (`:919-920`).
6. No `on_pipeline_finished` teardown task (`:798-835`).
7. The bring-up's own failures ("never confirmed", "STT unavailable") tear down inside
   the task (`:847-849`, `:857-868`); what `cancel_setup` does to it depends on where it
   is — in `wait_for(runner_task)` the cancel is *swallowed* (`:566`) and the teardown
   completes; in the 0.3 s settle it propagates and `presence.end()` never runs.

### The finding: the next call's claim released by the last call's teardown

As found (before the claim fix, now `MODE = "heldClaim"`):

`CallClaim.end()` (`sip_server.py:379`) releases **whatever claim it holds now**, and
`cancel_active_call` calls it *after* its awaits. The teardown task of a pipeline that
ended on its own runs concurrently with the reader, so by the time it gets there the
next call's bring-up may have replaced `_claim`:

```
c2 rings; its bring-up gets the engine, publishes, sends call.answer
PipelineEnds(c2)     the pipeline dies (STT written off, a timeout): call.hangup,
                     teardown TASK T starts (active = None), awaits the runner + 0.3 s
gateway              c2 on its way out -> c1's INVITE is accepted, call.incoming(c1)
start_call(c1)       cancel_setup(c2's bring-up); active is None -> nothing to tear down
c1's bring-up        presence._claim = c1; acquire: holder is still c2 -> REPLACED,
                     c2 released; 0.3 s settle
T: presence.end()    releases c1's claim
c1's acquire         `if new.released: return None` -- "granted", with NO holder
c1's pipeline        built, STT on the engine; ARBITER.holder is None
```

From then on the arbiter reads the box as free while a call is live: `call_live()` is
false (the busy lamp is off), `status()["call"]` is false (a room mic that yielded
reconnects), and the next Talk client or room session is *granted* — its STT then
collides with the call's on the engine, and whichever connects second says it cannot
hear and ends. The window is the teardown's runner wait + 0.3 s settle after a pipeline
dies, so it takes a second caller within about half a second: rare, but nothing
recovers it until the call ends. The same window (and the bring-up's own teardown
cancelled in its settle) lets the next call's acquire find the last call's claim still
held — `NoReplacedCall` — which the arbiter handles badly when a conversation is on hold
for that call (`SessionArbiter.tla`, `sa_overlap_refused`).

### What was changed (2026-10-15, the claim fix)

The model led: `shipped` is now the design it checked, and the design as found is kept
as `heldClaim` (`sip_heldClaim_unclaimed`, `sip_heldClaim_replaced`).

- `CallClaim` keeps its claims **by call id**, and `end(call_id)` lets go of that call's
  claim only (`end()` with no id, for the connection going away, lets go of all). Every
  teardown ends its own call's: `cancel_active_call` (`presence.end(_call_id)`) and the
  `disconnected` that leaves nothing behind.
- `cancel_active_call` lets go in a **`finally`**, so the bring-up's own teardown
  ("never confirmed", "STT unavailable") abandoned in its 0.3 s settle by the next call's
  `cancel_setup` still releases its claim before that call asks.
- `_bring_up` **waits out the `teardowns` in flight** before `presence.start`, so a
  pipeline that ended on its own has let its claim go before the next call asks. The
  audit put this wait in `start_call`; it is in the bring-up task instead, so the reader
  is not blocked by it (`NoBlockedWithPendingControl` gains no new case) and a
  `disconnected` for the new call cancels the wait like any bring-up.

`sip_ownClaim_replaced` keeps the identity check alone as a rejected design: it fixes
`NoUnclaimedCall` but a teardown cancelled in its settle never lets go and a teardown
task in flight still holds the slot when the next call asks — `NoReplacedCall` falls.
The `finally` and the wait are what close that; with both, the identity check is what
keeps a late `end` from touching a newer call should anything else ever reorder them.

The tests are `test_session_arbiter.py`'s
`test_a_call_that_rings_in_as_the_last_one_is_torn_down_keeps_its_claim` (the teardown
task; fails without the wait) and
`test_a_call_abandoned_in_its_own_teardown_lets_go_before_the_next_asks` (a Talk session
on hold for the abandoned call; fails without the `finally`), through the real
`sip_server` connection loop, `CallClaim` and `SessionArbiter`.

### What was changed (2026-08)

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

- **Magnitudes are out of scope** (the reader's teardown stall, the window above).
  Measure them, don't prove them.
- The arbiter is reduced to what calls see: the holder is a call or nobody. A refusal
  (busy, declined — the call left ringing) and the conversation put on hold are
  `SessionArbiter.tla`'s.
- Not modelled: the reconnect replay (`replay: true`), the connection's EOF `finally`,
  the declined call answered anyway (`answer_busy` — a task in `teardowns`, so the next
  bring-up waits out its busy line too), DTMF and audio routing.
- The over-approximation: each handler's steps (cancel the bring-up, start the
  teardown, the rest) are separate steps even where the code does not yield between
  them. Every counterexample above was checked against the code by hand.

## `SessionArbiter.tla` — one conversation at a time: the ask, the hold, the resume

`session_arbiter.py` (#58, #111) decides who holds the engine's one STT slot: a Talk
session, the room mic (asleep or awake), or a phone call. A call during a live
conversation **asks** it (PROMPT); a yes puts the conversation **on hold** (its STT let
go, context kept) and **resumes** it when the call releases the slot. `acquire()` runs
under one `asyncio.Lock` and awaits the lock, any end/hold/resume of the holder in
flight, the question, the hold and the end; `release()` is synchronous and runs from any
session outside the lock; the hold, the end and the resume are shielded tasks of their
own, so a caller hanging up mid-way cancels the acquire and they run on. That is the
concurrency risk worth a model: a resume can be started by a `release()` while some
acquire is between its decision and its `_set_holder(new)`.

The model has the Talk and room claims, two call claims (the same `"sip"` client), the
room's asleep flag flipping freely, the gateway answering a call mid-question (`AUTO`),
the holder leaving mid-question, a hold that fails, and an engine that refuses a second
STT connect. Time is not: every bounded wait is a step that may happen.

| row | | holds / fails |
|---|---|---|
| `sa_shipped` | as shipped, SIP's contract (a call lets its claim go before the next asks; `SipCall.tla`'s `SequentialClaims`, which `sip_shipped` holds) | all ✓ |
| `sa_unaskable` | `answer_phone_call` off (CALL_IN ends Talk; an awake room refuses) | all ✓ |
| `sa_overlap_refused` | before the SIP claim fix, with the overlap `SipCall.tla` found | `NoEngineRefusal` ✗ |
| `sa_overlap_redecide` | an arbiter-side alternative, not taken: decide again after the end | all ✓ |
| `sa_roomWakes` | witness | `NoSilentLiveEnd` ✗ (accepted) |

"All": `SttOnlyHolder` (only the holder's STT is connected), `OneConversation` (never two
sessions live and not on hold), `NoEngineRefusal` (no session the arbiter let in is
refused by the engine), `NoClobber` (`_set_holder(new)` never overwrites another holder),
`NoStrandedHold` (a conversation on hold is the arbiter's held one or being resumed),
`NoOrphanHold` (a hold lasts only while its call's claim does), `NoStuckAcquire` (where
nothing more can happen, every acquire, end, hold and resume has finished — a ringing
call is never stranded inside `acquire()`).

Under the SIP front-end's contract the protocol holds, including a caller hanging up
mid-question or mid-hold, the conversation leaving mid-question, a hold that fails, and
a call answered by the gateway while it is asked (~300k states).

### What the model found

`_acquire` assumes the slot is empty once `_end(holder, why)` returns, and calls
`_set_holder(new)`. It is not, when the holder being ended is a **call with a
conversation on hold for it**: the call's `end` is `CallClaim._release` →
`release()` → `_resume_held()`, which gives the slot back to the held conversation and
starts its resume. Then:

```
c1 asks Talk; yes -> Talk on hold; c1 holds the slot (held = Talk, held_for = c1)
c1's pipeline stops; its claim is not released yet (SipCall: a teardown task in flight,
                     or a bring-up cancelled in its own teardown's settle)
c2 acquires          decide(c1, c2) = REPLACED -> _end(c1): release(c1) -> _resume_held:
                     holder = Talk, Talk's resume starts ("Sorry about that -- where were we?")
_end returns         _set_holder(c2): Talk is overwritten
both connect         Talk's resumed STT and c2's pipeline STT: the engine refuses the second
```

User-visible: the Talk user hears "where were we?" while the box picks up the second
call, and either the caller hears "I can't hear you right now" and is hung up, or the
Talk session says it cannot hear and ends. Afterwards the arbiter's holder is c2 (or
nobody) while Talk runs, so a third call is taken without asking. It needs the
`NoReplacedCall` overlap from `SipCall.tla` — narrow windows, so low likelihood.

**Decided (2026-10-15): the SIP claim fix, and no arbiter change.** `MODE = "redecide"`
(after `_end` returns, if the slot was taken back meanwhile, loop and decide again) closes
this trace too and is kept as a row, but it is not code. The trace needs a call claim
ended as REPLACED, and REPLACED is the only way the arbiter ends a call (`decide`: a call
holder against a Talk or room newcomer is REFUSED, and a call claim has no `gone`), so it
needs the next call to ask while the last one's claim is held — exactly what
`SipCall.tla`'s `SequentialClaims` rules out, and `sip_shipped` holds it. Under that
contract `sa_shipped` holds with the arbiter as written. Changing `_acquire`'s loop for a
case that can no longer arise would be a second guard on one contract.

`sa_roomWakes` is the accepted residual: a sleeping room is ended for a call (CALL_IN)
or a Talk session (TAKEN) by a decision taken while it was asleep; `_end_for` closes a
room silently for both whatever `asleep()` says by then, so a room that wakes in between
— one event-loop tick, decide to the ending task's first step — is cut off without a
word instead of being asked.

### Known limits of this model

- `Claim.gone` (REAPED), a client replacing its own Talk session (REPLACED), and
  `would_refuse`'s pre-check are left out.
- A failed hold has no partial effect here. `AgentSession.hold` can be cut part-way by
  `HOLD_WAIT_SECS` (muted, the gate held, possibly the STT let go); `_acquire` then ends
  the conversation — but if the call's acquire was cancelled meanwhile (the caller hung
  up), nothing does, and the conversation stays the holder, muted. Not modelled;
  `session_arbiter.py:410-416` / `agent_session.py:772-786`.
- Engine refusals are immediate: `stt.py`'s retry budget (`SttSlot.tla`) is what the
  SETTLE sleeps lean on, and is not modelled here.
- END_WAIT_SECS' timeout (a holder that never lets go) is not a step: past it the arbiter
  grants the slot anyway, and `SttOnlyHolder` would fall by design.

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
this module has the same environment with the facts the design rests on spelled out
(pipecat 1.8.1, `engine_tts.py`):

- A context opens (`TTSStartedFrame`) for any text with a non-blank character:
  `tts_service._push_tts_frames` creates the audio context and queues the started frame
  *before* `run_tts`, so text with nothing synthesizable — `"..."`, an emoji — opens a
  context that never yields audio (`UnspeakableOpens`). Such a context, and any whose
  last `run_tts` yielded no audio, is not closed at the End: it ends on the stop-frame
  timeout (`TTS_STOP_FRAME_TIMEOUT_S`, 15 s).
- Every context that opened ends with a `TTSStoppedFrame` naming it
  (`push_stop_frames=True`, `engine_tts.py:607`): appended behind its last chunk, or
  pushed on the stop-frame timeout — mid-reply too, when the LLM is silent that long. The
  ledger takes it as "this turn's synthesis is over" (`transcript_ledger.py:426-438`). The
  output transport queues it behind the audio and ends bot-speaking when it reaches the
  queue's head, if audio played since the last stop (`base_output.py`, `_handle_frame`):
  `BotStoppedSpeaking` comes at the end of EACH context, possibly with the next context's
  chunks still queued, and on the 3 s idle fallback when synthesis stalls. `MaxChunks = 2`
  makes that stall reachable.
- After the stop the TTS re-pushes the response's `LLMFullResponseEndFrame` — the same
  frame, same id (`tts_service._maybe_reset_word_timestamps`). Sighted a second time,
  below the TTS, it names the response the context spoke. It comes only if the End
  reached the TTS before the context ended; a context that ends on the timeout while the
  LLM still streams gets a fresh End the ledger ignores, and is re-created under the SAME
  id when the LLM's next sentence reaches the TTS (`Resume`).
- Every TTS push is tagged with its context (`engine_tts.py`); the transport's untagged
  rebuild of each chunk it played is recognised and ignored, and the thinking-sound bed
  (untagged, pushed INTO the transport) is told from it by the processor that sighted
  it first. The untagged legacy path is out of scope.

The ledger, in the model's terms: turns open only on the TTS's own frames, never on
`BotStartedSpeaking`; a turn claims the oldest expected context (the queue of ended
responses), else the response streaming now; several turns may be open at once — a
reply queued behind another at the transport; a turn's `audio_start` is its own chunk's
place in the transport's queue, laid out from the window's start; a window closing
credits the queued chunks that have played by then and keeps the rest queued, the window
open for them; it charts, oldest first, the turns whose audio has all played and whose
synthesis is over; a context starting proves every OTHER open context ended, since the
TTS drains one at a time; a context's stop frame ends its turn's synthesis; the drain
re-push confirms a turn's response or corrects it; an interruption charts every open
turn with its own played portion. The layout's seconds are taken to be right — at a
window close or a cut the ledger credits exactly what the transport has played;
`PlayoutPause.tla` checks that arithmetic, `test_ledger_playout.py` the rest on the real
ledger.

| row | property | LedgerPlayout |
|---|---|---|
| `lp_phantom` | `NoPhantomFullHeard` — a reply charted complete had every chunk PLAYED. Strict: `Ledger.tla`'s allowed "queued at the transport", the residual noted under its limits | ✓ (3.5k states) |
| `lp_unheard` | `NoUnheardWhenPlayed` — read live since the 2026-10-14 audit: a reply charted heard-nothing never plays, before the chart or after it | ✓ |
| `lp_once` | `ChartedAtMostOnce` | ✓ |
| `lp_wrongText` | `ChartedTextMatchesContext`, two replies | ✓ (300k states, ~20 s) |
| `lp_premature` | `NoPrematureFullChart`, with stalls and timeouts | ✓ |
| `lp_playedCharted` | `PlayedIsCharted` — audio the transport played is charted once its turn is over; nothing played vanishes | ✓ |
| `lp_fillerSet` | `FillerCtxRemembered` | ✓ |
| `lp_all` | every property above, two replies and a filler | ✓ (300k, ~25 s) |
| `lp_resume` | `NoPrematureFullChart` under `Resume` | ✗ (8) — below |
| `lp_wrongText_unspeakable` | `ChartedTextMatchesContext`, `Unspeakable = {r1}`, the first cut's premise (`UnspeakableOpens = FALSE`) without `has_speech` (`LedgerSpeakCheck = FALSE`) | ✗ (6) — below |
| `lp_wrongText_unspeakable_checked` | the same premise, with `has_speech`: why it shipped | ✓ |
| `lp_wrongText_unspeakable_opens` | `ChartedTextMatchesContext`, **as shipped**, with the context opening as pipecat opens it | ✗ (6) — **a bug in the code**, below |
| `lp_unheard_unspeakable_opens` | `NoUnheardWhenPlayed`, the same | ✗ (12) — below |
| `lp_unspeakable_queued` | every property, `has_speech` dropped from the queuing (**proposed, not code**) | ✓ (66k) |
| `lph_phantom`, `lph_once`, `lph_wrongText` | the same, hermetic wiring (`Drain = FALSE`) | ✓ (381k, ~15 s) |
| `lph_premature` | `NoPrematureFullChart`, hermetic wiring | ✗ (8) — below |

### The 2026-10-14 audit: what the model had drifted from

The module was written before PR #13 merged, and the merge switched the stop frames on.
Against the code today:

1. **No stop frame, and a window that closed only when the queue ran dry.** The model
   said `push_stop_frames` was False and `BotStoppedSpeaking` the transport's silence
   timeout. It is True (`engine_tts.py:607`, in the same merge as the model), so every
   context ends with a tagged `TTSStoppedFrame` the ledger acts on
   (`transcript_ledger.py:426-438`: the turn's synthesis is over, a filler's context is
   forgotten), and the transport ends bot-speaking on it with the NEXT context's audio
   possibly still queued. `_window_closed` (`:697-743`) credits only what has played by
   then and keeps the window open for the rest; the model credited every queued chunk
   and closed the window, which pipecat's `PlayStop` could not reach with audio queued.
   Now: stop markers in the transport's queue (`PlayMark`), the 3 s idle fallback
   (`PlayIdle`), the stop frame to the ledger (`TtsStop`), the drain a step of its own
   behind it (`TtsDrain`), and a credit of what has played.
2. **"An unspeakable reply opens no context."** False since the merge as well: the
   started frame is queued before `run_tts` (`tts_service._push_tts_frames`;
   `engine_tts.py:833-844` says as much). `UnspeakableOpens` is the switch; the first
   cut's premise stays as `FALSE` for its two rows. Reproduced on the real TTS: `"..."`
   emits a `TTSStartedFrame`, then nothing for the stop-frame timeout, then a stop.
3. **The drain's correction kept the misclaimed response claimed.** `_end_drained` lets
   it go (`:529-534`, `old["turn"] = None`), so if its End is still to come it is queued
   again. Modelled.
4. **A cut charted complete only on the previous window's credit.** `_finish` also
   counts the layout's portion up to the cut (`:845-848`). Modelled (`CutComplete`).
5. `NoUnheardWhenPlayed` checked "nothing had played when it was charted" — weaker than
   what `HeardContextCorrector` depends on: it deletes a reply charted heard-nothing, so
   the reply must not play *after* the chart either. Read live now.
6. `Resume = FALSE` had a context time out and never resume while its LLM went on
   streaming. Kept — a reply's text can end in a tool call streamed for 15 s with no more
   speech — but `Resume` is now what makes more audio possible, and "complete" in the
   chart means no more audio can come.

### The new finding: an unspeakable reply's context claims the next reply

`lp_wrongText_unspeakable_opens`, 6 states; `lp_unheard_unspeakable_opens`, 12:

```
LlmStart(r1) LlmEnd(r1)   r1 is "👍": has_speech says no, the ledger does not expect it
LlmStart(r2)              the next reply is streaming at the TTS
TtsStart(r1)              r1's context opens (late: the TTS was still on an earlier context,
                          or the reply hold held it) and claims what is streaming -- r2
TtsStop(r1)               no audio: the stop-frame timeout. r2 is charted heard-nothing
TtsDrain(r1) TtsStart(r2) r2's own context opens anonymous, claiming nothing
... PlayStart PlayChunk   r2 plays in full, charted nowhere
```

`has_speech` was the fix for `lp_wrongText_unspeakable`, which rests on the false premise
(item 2): pipecat opens the context, so leaving the response out of the queue is what
puts the ledger one context out of step. The caller hears r2 in full; the ledger says
they heard none of it, and at the next user turn `HeardContextCorrector` deletes r2 from
the context — an answer the model then forgets it gave. Reproduced on the real
`EngineTTSService` and `TranscriptLedger` (stop-frame timeout shortened to 1 s): replies
`"Great!!"`, `"👍"`, `"Hello there my friend."` back to back chart `'Hello there my
friend.' interrupted=True heard=0.00` while its audio plays under an anonymous turn. The
context ahead must still be open when r2 reaches the TTS — "Great!!" keeps its context
open for the full timeout (below) — so it is rare, and it needs an emoji- or
punctuation-only reply, which the guard does not stop (it speaks its recovery line only
for punctuation it strips). **Proposed fix, not code** (`lp_unspeakable_queued`, every
property holds): queue every response with text, as before `has_speech`. Its context
opens and ends with no audio, and is charted never-played under its own text.

`lp_wrongText_unspeakable` (6 states) is the first cut's counterexample, kept under its
premise:

```
LlmStart(r1) LlmEnd(r1)   r1 completes; the TTS opens no context for it; the ledger queues it
LlmStart(r2)              the next reply streams
TtsStart(r2)              its context opens and claims the queue's head -- r1's text
Interrupt                 barge-in before r2's End has reached the TTS: charted under r1's text
```

`lph_premature` fails in 8 steps, and only in the hermetic wiring: the response ends, the
first chunk plays, synthesis stalls, the window closes on the idle fallback — and "ended"
is taken as "synthesized", so the reply is charted complete with a chunk still to come.
Live, the stop frame and the drain are the completion signals and the turn stays open
(`lp_premature` holds). It means a stall is invisible to the test suite's ledger, not
that the shipped one mischarts it.

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
corrects the newest turn (`_end_drained`). The 2026-10-14 audit found the unspeakable
reply's misclaim above.

### Known limits of this model

- **`lp_resume`, the accepted residual.** 8 states now: the LLM streams, one chunk, then
  nothing reaches the context for the stop-frame timeout — its stop frame says
  "synthesized", the window closes on it, and the reply is charted complete while the
  LLM is still streaming; its next sentence re-creates the context under the same id
  and plays under an anonymous turn, charted nowhere. The first cut needed a narrator
  line in the gap; the stop frame does it alone. Still a 15 s LLM stall mid-reply
  (`TTS_STOP_FRAME_TIMEOUT_S`); "lead" pacing keeps the context alive through its waits.
- **The context left open on the timeout** (not a ledger property, so not here as a row):
  pipecat closes a context at its End only if the last `run_tts` yielded audio. A reply
  whose last piece the sentence splitter hands over alone and the engine cannot speak —
  `"Great!!"` (`"!"`), `"Really?!"`, `"Bye! 👋"` — keeps its context, and the TTS's
  queue, open for the full `TTS_STOP_FRAME_TIMEOUT_S`: bot-speaking ends on the 3 s idle
  fallback instead of the stop frame, and whatever the TTS says next (the answer after a
  tool call, a narrator line) waits up to 15 s. Measured on the real TTS with the timeout
  at 1 s: the stop frame 1.00 s after the last audio for those endings, 0.00 s for
  `"That is all."`.
- **Numbers are out of scope**, as in `Ledger.tla`: the layout's seconds, `heard_fraction`,
  the pts cut. The layout is taken to credit exactly what played.
- **Spoken notices** (a non-filler `TTSSpeakFrame`, queued as an expected context of its
  own, completed by its stop frame alone: no End re-push) are not modelled — "Worth
  modeling next", item 1. The hermetic fallback is `Drain = FALSE`, above.
- **A cancelled completion's End is assumed to precede the next run.** pipecat pushes it
  from the cancelled task's `finally`, at cancellation; the model had let it arrive after
  the next response began streaming, and the ledger — which cannot tell whose End it is
  and ends the stream it holds — closed that response on the stale frame. Excluded as an
  ordering fact (`LlmStart` waits for it) rather than modelled; real only if the event
  loop stalls between the cancel and the next run.
- **One interruption sighting.** The ledger runs `_interrupt` at every processor's
  sighting of the `InterruptionFrame`, and a TTS frame in flight between two of them
  opens an anonymous turn on a closed context, which the next sighting ends; the model's
  interruption is one step.
- **Scale.** One reply and one filler is 3.5k states; two replies 300k, about 20 s with
  two workers.

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
only interleaving the barge-in rows need (the commit overtaking its own interruption is
the other, for the context and the hold: `CutLands`, below).

| MODE | | `NoStrandedTurn` | `NoMissedBargeIn` |
|---|---|---|---|
| `asWritten` | stock pipecat — identical on 1.5.0, 1.7.0 and 1.8.1 | ✗ | ✗ |
| `retryOnQuiet` | `keep_barge_in_reachable`: a finalization refused **after its inference ran** is re-applied at the user's next quiet moment | ✓ | ✓ |

`NoStrandedTurn` is the state itself — a turn whose inference has already run, still open
once the user has fallen quiet. `NoMissedBargeIn` is what the user experiences, and its
counterexample is eight states with nothing exotic in it:

```
Interject    >= INTERRUPT_MIN_WORDS (their final, after the VAD stop): the turn opens
CutLands     the interruption reaches the transport: the bot is cut. Barge-in worked.
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

`speculate.py` (`TEAPORT_SPECULATIVE_REPLY`, off by default) asks the LLM once the caller's
words have settled on a turn the stop strategy has *not* concluded on — Smart Turn said
INCOMPLETE and `SMARTTURN_STOP_SECS` is running — against a snapshot of the context with
those words as the user message. At the commit the LLM service is offered that stream
instead of opening its own. It is a second machine riding on this one, so it is modelled
here rather than on its own: `SpecStart` reads `userTurn` and writes nothing the
controller's guards read, and whether that leaves barge-in intact is a question for TLC,
not a promise.

The words are the settled **interim**. The trigger was a final until 2026-10-09, which only
worked with the STT committing at the VAD stop before the verdict existed: under the
verdict commit (#43, `SttCommit.tla` below) the segment is held open through the ceiling,
no final lands before the commit, and the speculation was inert. The engine's interim now
equals its final (48 of 48 phone turns measured that day), so the strategy asks on the
interim once it has stopped changing for `TEAPORT_SPECULATE_SETTLE_MS`, and the vad-stop
setting is gone. Since 2026-10-10 an engine that reports how far it has transcribed
(`transcription.progress`) decides "stopped changing" instead of the window: the strategy
asks once the report passes the caller's speech end for the INCOMPLETE stop
(`SpeechDecodedFrame`). `SpecStart` is a free action whatever the trigger — it fires
wherever the strategy could — so the model checks a superset of all three, and a word
the report missed is `Retext`.

The property it adds is `NoStaleReply`: a speculated reply is spoken only for the context it
was asked against. Two things can move between the snapshot and the commit, and the model
has an action for each:

- **The context before the user message** (`CtxChange`, any writer at all).
  `HeardContextCorrector._reconcile` on the very `LLMContextFrame` that carries the commit
  was the first miss seen live, so `speculate.py` now runs it *before* the snapshot.
  `MemoryRecall`'s note is injected on the final, before the aggregator sees it — inside a
  snapshot taken on a final, but *after* one taken on the interim: with the interim
  trigger it is a real writer in the window, and a turn with a memory hit is a miss. The
  consult follow-up posts only with the turn free, and a speculation only exists inside an
  open turn. `CtxChange` stays free because nothing in the machine prevents the next writer.
- **The user message itself** (`Retext`). The commit carries the *final's* words, which the
  snapshot did not have: the decoder's lagging tail arriving after the settle window, or a
  final whose re-decode differs from the last interim by a comma or a capital — landing in
  the same step that commits, before the strategy could cancel on it. `Retext` leaves the
  speculation live; the strategy cancelling on a changed interim it does see in time is a
  refinement the rows do not rely on.

| SPEC | promotes when | `NoStrandedTurn` | `NoMissedBargeIn` | `NoStaleReply` |
|---|---|---|---|---|
| `off` | — (the four MODE rows above; 592 states since the cut became its own step, 436 before, 47 before the reply hold's variables) | | | |
| `byText` | the committed words are what was asked | ✓ | ✓ | ✗ (`CtxChange`) |
| `byHistory` | the context before the user message is what was asked | | | ✗ (`Retext`) |
| `byContext` | the whole context, user message included, equals the snapshot — `Speculator.take` | ✓ | ✓ | ✓ |

The `byText` counterexample is six states and needs no barge-in at all:

```
Interject    the turn opens (they have paused; Smart Turn says INCOMPLETE, the ceiling runs)
CutLands     its interruption lands (nothing to cut)
SpecStart    the interim settles, the stop does not fire: the LLM is asked now
CtxChange    something writes the context (MemoryRecall's note on the final; the
             corrector's truncation of the cut reply, before it was moved ahead of the
             snapshot)
Inference    the ceiling ends the turn; the words are what was asked, so byText
             adopts the stream -- a reply the model produced against a context
             the commit no longer has
```

The `byHistory` counterexample is the same six with `Retext` for `CtxChange`: the final
the ceiling's commit brings is "Tell me a story." where the interim was "tell me a story",
nothing else wrote the context, and the reply to the interim's words is adopted for the
final's. It is the hazard the interim snapshot adds over the final one, and the reason
`Speculator.take` compares the user message too, forgiving nothing but whitespace at its
ends (the interim carries the engine's leading space; a difference there is not a
difference in the words).

`byContext` closes both: `specGen = gen` and `specWords` together are the equality check on
`context.messages`. The adoption rule is one definition (`Adopt`) shared by all three
designs, and `staleReply` is raised whenever an adopted reply's generation is not the
commit's or its words moved. For `byContext` that is unreachable by construction —
`Adopt` requires exactly the two equalities `Stale` negates — so the row checks the rule
as written, not `Speculator.take`'s equality test (the tests pin that); what the model
does show is that each conjunct is needed: drop one and the row falls, as `byText` and
`byHistory` do. The two barge-in
rows hold with it on because nothing in `SpecStart`/`CtxChange`/`Retext` touches
`userTurn`, `userSpeaking` or `stopInFlight`. A resume (`VadStart`) and a new turn
(`Interject`) both close the speculation. The commit is `Inference` or `ForceStop` — the
watchdog's stop pushes the aggregation too, so it reaches `Speculator.take` and decides the
speculation the same way.

### The reply hold (`HOLD`, #85)

A commit's reply plays ~0.2 s after the commit, and a caller who was only pausing — the
`SMARTTURN_STOP_SECS` ceiling commits mid-sentence — resumes inside that gap. Their words
interrupt only once transcribed, so the reply starts over them about a fragment, and the
next commit then hands the model the fragment and its continuation as two user messages.
On a 2026-10-05 test call 10 of 14 INCOMPLETE verdicts were committed that way.
`ReplyHoldGate` (`reply_hold.py`) is armed by every commit until its reply's first audio;
the caller speaking while it is armed pauses its input queue (`held`, `paused`). Their
words — a new turn — drop the held reply and ask `HeardContextCorrector` to fold the next
commit into the unanswered one; no words release it.

The model's additions are one step each between awaits: the hold is taken inside the
VAD start (or the `Resume` inside the commit's own await), the release after the
resumed speech turned out to be wordless (`Wordless`, then `Release`), the drop where
the turn's interruption reaches the gate (`CutLands`, below), the merge at the commit
(`Commit`, shared by `Inference` and the watchdog's `ForceStop`), and `Hangup` — the
session's `EndFrame` reaching the gate's queue. `BotStart` now also plays the last
commit's reply (`reply`), not only the refused-finalize one, and waits while `paused`.
The gate is armed by the aggregator's `on_user_turn_message_added` (a commit with words
in it), which the model's `Commit` is.

These rows run with `FENCE = "inTime"` and `EARLY_FINAL = FALSE` — the cut lands before
the commit, and a refused finalize's retry commits nothing — so that each tells its own
designs apart. What the code does where those premises fail is in the two sections
after the pause's.

| HOLD | | `NoStaleReply` | `NoSplitTurn` | `NoSilencedBot` | `NoEndBehindHold` |
|---|---|---|---|---|---|
| `none` | before #85 | ✗ | ✗ | ✓ | ✓ |
| `gateNoMerge` | the hold without `TurnMerge` | ✓ | ✗ | ✓ | ✓ |
| `gateNoResume` | as first written: a drop left the pause to pipecat's reset | ✓ | ✓ | ✗ | ✓ |
| `gateEndWaits` | as first written: the End queued behind the hold | ✓ | ✓ | ✓ | ✗ |
| `gateNoEnding` | a hold may start after the End reached the gate (no `_ending`) | ✓ | ✓ | ✓ | ✗ |
| `gate`, `WORDS_IN_TIME` | `reply_hold.py` as shipped, words in time (the assumption) | ✓ | ✓ | ✓ | ✓ |
| `gate`, timers as they are | `reply_hold.py` as shipped | ✗ (accepted residual) | ✓ | ✓ | ✓ |

**The release is a timer, and the model says so** (round-2 review: the first version's
`Release` required `~resumed`, an oracle the code does not have). `Release` fires
`TEAPORT_REPLY_HOLD_RELEASE_S` after the caller goes quiet, and `Cap` ends a hold while
they may still be talking. `WORDS_IN_TIME` is the gate's assumption written down as a
constant: the caller's words — or the STT's close saying there were none (`Wordless`) —
arrive before either timer. Under it (`ut_hold_gate_all`) `gate` holds all four together
with `NoStrandedTurn` and `NoMissedBargeIn`. Without it (`ut_hold_gate_timed_*`) every
property but `NoStaleReply` still holds, and `NoStaleReply` falls — the accepted
residual, 9 states:

```
Interject CutLands Inference Finalize           the ceiling commits mid-sentence
VadStart                                        the caller resumes: held
Cap                                             the cap ends the hold, the caller still talking
BotStart Interject                              the reply plays; their late words cut it
```

That is the case the hold cannot fix without knowing the future (words slower than
`RELEASE_S`, or a caller talking past the cap with no words transcribed); it is no worse
than before #85.

The speculation and mode rows run with `WORDS_IN_TIME = TRUE` (and `FENCE = "inTime"`, `EARLY_FINAL = FALSE`, as every row predating them does); the speculation rows now run with `HOLD = "gate"`, the shipped
gate, and the `MODE` rows with `HOLD = "none"` (their verdicts and the counterexamples
above are unchanged; `NoStaleReply` grew a second cause, below, and `byText` still fails
it on the speculation's).

- `NoStaleReply` now also says: **a reply never starts over speech the caller began after
  its commit and that turned out to be words.** `none`, 8 states:

  ```
  Interject CutLands            the turn
  Inference Finalize            the ceiling commits it; the reply is on its way
  VadStart                      the caller was only pausing
  BotStart                      the reply's first audio plays over them
  Interject                     their words arrive and cut it: it was stale
  ```

- `NoSplitTurn`: **a commit never leaves two user messages with no reply between them.**
  `none` and `gateNoMerge` fail it the same way, 8 states: the commit, the caller's
  words (after their VAD stop) start a turn whose interruption drops the (unplayed or
  held) reply, the next commit appends their continuation after the unanswered fragment.
- `NoSilencedBot`: **the queue is paused only while a hold is in force.** `gateNoResume`,
  8 states: commit, resume (held), words (`Interject`) — pipecat's interruption reset
  cancels and re-creates the process task, which lifts the pause, but when the frame the
  paused task had already dequeued is an `UninterruptibleFrame` (a
  `FunctionCallInProgressFrame`), it only resets the queue and leaves the task waiting on
  the pause. The gate as first written did nothing more, and every later reply waited
  behind it forever. It now resumes the queue on every end of a hold.
- `NoEndBehindHold`: **the session's End never waits behind a hold.** `gateEndWaits`,
  6 states: commit, a resume inside the commit's await (held), hang-up — the `EndFrame`,
  a ControlFrame, queues behind the held reply (Talk's close took 5.7 s, and the held
  reply then played into the closing session). `queue_frame` now drops the hold and lifts
  the pause in the same step the End arrives. And no hold may start BEHIND the End:
  the caller can still speak after it reached the gate (`VadStart` is the one step not
  guarded by `~endQueued`), and `gateNoEnding` — the gate without its `_ending` flag —
  fails in 6 states: commit, hang-up, the caller's speech inside the commit's await holds
  a reply behind the End.

What the model does not say: the speaking-at-commit hold (`arm()` with the onset test
still hearing the caller) is not a step of its own, because `Inference` requires a quiet
VAD (see below) — but its states are reached: since the fence, the commit waits inside
the inference's await, a VAD start can land in that wait and the arm then holds at
once, which is `Inference` then `Resume` here; the onset test and the VAD are one
`userSpeaking`; how long the release window and the caps are (only that they are
timers). `test_reply_hold.py` covers those on the real gate.

**Follow-ups (`Followup.tla`) are unchanged by the hold, and the module is not
extended.** The gate is armed only by a user commit, never by the injector's
`LLMRunFrame`, so a follow-up's reply is not held. `FollowupTrigger` retires the trigger
at the first `LLMTextFrame` *below the LLM*, above the gate, so `NoSilentLoss` and
`NoRepeatRecital` do not see it. What the hold does to `FollowupGate`, which sits below
the transport: a held commit's `LLMFullResponseStartFrame` reaches it later, so
`TurnCommitMark`'s claim keeps the window shut longer (never an interjection), and a held
`FunctionCallInProgressFrame` keeps the narrator out for the hold — only while the
caller is talking or within the release window of it, which is not the dead air
`NoDeadAirDuringTool` is about.

### The barge-in pause (`PAUSE`, #86)

Barge-in waits for a transcript, and the STT returns none for speech over the bot until
the caller stops, so the bot talks on through the caller's whole interjection —
typically most of a second.
`BargeInPauser` (`barge_pause.py`) pauses playout at the transport on the caller's
speech; their words cancel the reply as before (`TEAPORT_INTERRUPT_MIN_WORDS`, or while
paused an utterance of stop words), no words resume it. A pause holds back the reply's
own end with its audio, so the bot counts as speaking until the resume — which is where
the review's must-fix came from.

The reply's state is two facts the code acts on, kept apart (round-2 review: one
boolean had merged them, and the band the answer-loss fix is about was unreachable):
`left` — how much of it is left against the two thresholds, `short` (≤ 0.5 s: the
transport does not pause there), `mid` (up to the ~1 s a final takes to land: a pause is
allowed, and the rest would have played out before the caller's final), `long` — and
`synthDone`, whether the brain knows it (the reply's `TTSStoppedFrame` has reached the
transport). New steps: the pause inside the VAD start (and, as shipped, at `BotStart`
when the caller is already talking), `Progress`, `SynthDone` (during a pause, as shipped,
the transport re-announces the pause with the tail it now knows), `TailElapsed` (ground
truth: paused, fully synthesized, less than a final's latency left — without the pause
the reply would be over), `ResumeP`, and two transcripts — `OneWord` ("Yes." after the
caller's VAD stop) and `StopWord` — both through the same `StartTurn` as `Interject`.

The monitors observe outcomes, not the design's own branch: `answerLost` compares what
the strategy did with a one-word final (the design: does it know a tail, `tailKnown`, and
has it elapsed) against the ground truth (`wouldEnd`); a missed stop is the bot
*resuming* after the caller said stop to it while paused, however that came about.

| PAUSE | | `NoLostAnswer` | `NoMissedBargeIn` | `NoStuckPause` |
|---|---|---|---|---|
| `asWritten` | #86 as first written: pauses anywhere; a paused bot is a speaking bot | ✗ | ✓ | ✓ |
| `aOnly` | only the near-end rule (no pause in the last 0.5 s) | ✗ | ✓ | ✓ |
| `tailAtPause` | round 1: plus the would-have-ended rule, tail known only if synthesized at the pause | ✗ | ✓ | ✓ |
| `noStopWord` | the shipped pause without the stop word | ✓ | ✗ | ✓ |
| `keepOnInterrupt` | a transport that keeps its pause through the interruption | ✓ | ✓ | ✗ |
| `shipped` | near-end rule + would-have-ended rule + the tail re-announced during the pause | ✓ | ✓ | ✓ |

`shipped` holds all three with `HOLD = "gate"` and every property above, under the
hold's `WORDS_IN_TIME` assumption (`ut_pause_shipped_all`, 1.5k states) and with the
timers as they are (`ut_pause_shipped_timed`, every property but the accepted
`NoStaleReply` residual, 3.5k states). Two **witness** rows prove it is not vacuous:
`ut_pause_shipped_reach_band` FAILS `NoPauseInTailBand` (a pause with 0.5–1 s left is
reachable) and `ut_pause_shipped_reach_elapsed` FAILS `NoTailElapsed` (a held-back tail
that would have ended is reachable).

- `NoLostAnswer`: **a one-word answer the caller would have had as a turn without the
  pause is one.** `aOnly` — the row that tells the shipped design from the near-end rule
  alone — 10 states:

  ```
  Interject CutLands
  Inference Finalize           a turn, committed
  BotStart                     its reply plays, fully synthesized, 0.5-1 s left
  VadStart                     "Yes" over it: past the near-end rule, so it pauses
  VadStop TailElapsed          the held-back rest would have played out by now
  OneWord                      "Yes." -- one word against a bot that still counts as
                               speaking: under the 2-word guard, dropped
  ```

  `asWritten` is the same with 0.5 s or less left. `tailAtPause`, 11 states: the pause
  comes before the reply's synthesis is over (`SynthDone` lands during it), so no tail
  was ever announced, and the same final is dropped. Shipped: the pause frame carries
  the tail, and the transport announces it again when the `TTSStoppedFrame` arrives
  during the pause; the strategy anchors it at the pause's start.
- `NoMissedBargeIn` (extended): **told stop while paused, the bot does not resume.**
  `noStopWord`, 10 states: a reply plays, the caller starts (paused), says "Stop." — one
  word, under the guard, dropped — and the pause resumes the reply.
- `NoStuckPause`: **playout is paused only in the middle of a reply.**
  `keepOnInterrupt`, 9 states: paused, then the caller's words cancel the reply — and a
  transport that kept its pause would block the next reply's first write until its 8 s
  guard.

The thinking-sound bed is not paused (`BargeInPauser` pauses only reply audio, which has
a TTS context; the bed has none): pausing it only delayed the consult answer queued
behind it. `Followup.tla` has no bed, and the fix removes the interaction instead of
modelling it.

### The cut and the commit (`FENCE`, `CutReplyFence`)

A barge-in puts two messages into the context from two ends of the pipeline: the reply
it cut, committed by the assistant aggregator (last) when the `InterruptionFrame`
reaches it, and the caller's turn, committed by the user aggregator.
`HeardContextCorrector` finds the cut reply by position, so it has to land first. Until
2026-10-14 the model took the turn start and its interruption as one step, ahead of the
commit — a premise the code did not keep until `endpointing.CutReplyFence` (2026-10-13):
pipecat's `broadcast_interruption()` only queues the frame, and a turn that opens on its
final after its own VAD stop (the late-start barge-in, the commonest shape) commits in
the same controller call. Now they are two steps: `StartTurn` (the controller: the turn
opens, the frame is queued — `cut`) and `CutLands` (the frame reaches the gate, the
transport and the assistant aggregator). `Interject` no longer requires the caller to be
speaking: the transcript that opens a barge-in usually lands after their VAD stop.

The property is `NoCutAfterCommit`: **nothing reads the context for a turn — its commit,
or a speculation's snapshot — before the reply that turn cut is in it.** The snapshot
counts because it runs the corrector first (`reconcile_for_snapshot`), which spends the
ledger's cut on a context without the reply, and the reply then lands whole.

| FENCE | | `NoCutAfterCommit` | `NoStaleReply` | `NoSplitTurn` |
|---|---|---|---|---|
| `none` | before 2026-10-13 | ✗ | ✗ | ✗ |
| `commitOnly` | 228298e: the commit waits, the snapshot does not | ✗ | | |
| `inTime` | `CutReplyFence` as shipped, the cut landing within its bound | ✓ | ✓ under the hold's premises | ✓ under the hold's premises |
| `timed` | the same with `CUT_COMMIT_WAIT_SECS` (0.5 s) as it is | ✗ (accepted residual) | | |

`none`, 8 states — the live 2026-10-13 call ("Okay, stop there." over a count):

```
Interject CutLands Inference Finalize   a turn, committed
BotStart                                its reply plays
Interject                               the caller's words open a turn (quiet: after their VAD stop)
Inference                               ... and commit in the same call, ahead of the interruption:
                                        the LLM is asked without the cut reply, which then lands
                                        whole after the caller's turn -- and the model counts again
```

A cut reply that had not started playing commits nothing, so it is not this property's;
it is the reply hold's, twice over. `NoSplitTurn` (`ut_fence_none_nosplitturn`, 7
states: the same with no `BotStart`): the first reply never played, but the merge the
gate asks for when the interruption reaches it comes after the commit it was for, so
the fragment and its continuation stay two messages and the merge lands on the turn
after. `NoStaleReply`
(`ut_fence_none_nostalereply`, 8 states, the reply hold's words-in-time premise and all):

```
Interject Inference                     a late-start turn commits; the commit arms the gate
CutLands                                its own interruption arrives: the gate reads it as the
                                        armed reply dropped -- disarms, asks for a false merge
Finalize VadStart                       the caller resumes before the reply plays: nothing holds it
BotStart Interject                      the reply starts over them and their words cut it
```

`commitOnly`, 8 states: the same, but the late-start barge-in's final, under an INCOMPLETE verdict,
asks a speculation (`SpecStart`) before the cut lands — `ecb4dcd` (live 2026-10-13: a
snapshot 15 ms after the interruption). `timed` is `none`'s trace with the commit's
wait running out: logged as a `CUT-FENCE` warning, and accepted, as the hold's release
timer is. As shipped the speculation is not started past the bound, so only the commit
can overtake.

### The refused finalize's second commit (`EARLY_FINAL`)

`retryOnQuiet` re-applies a refused finalize at the caller's next quiet moment, and the
model had that close the turn and nothing more. In the code it can commit: the refused
turn stays open with its stop strategy un-reset (pipecat keeps it open "so the next
inference re-evaluates"), the caller's resumed words join it, and whatever of them was
finalized by that VAD stop — the engine's own segmenter closes a segment on a shorter
silence than the VAD's floor — is pushed as a second user message, by the strategy's own
second stop or by the re-applied one, with no turn start and so no interruption.
`EARLY_FINAL = TRUE` lets that happen (`VadStop`); every row above runs with `FALSE`.

As shipped (`ut_shipped_nosplitturn`) it **fails `NoSplitTurn`**, 7 states:

```
Interject CutLands Inference   a turn commits; its reply is on its way (the gate armed)
Resume                         the caller resumes inside the commit's await: held
Finalize                       refused -- the caller is speaking; owed
VadStop                        they stop: a final of what they added is already aggregated,
                               and the retry commits it as a second message
```

No interruption drops the held reply, so it is released after `RELEASE_S` and the bot
answers the fragment, then answers again with both messages in the context.
`reply_hold.py` lists this among its known limits ("no worse than without the gate",
0 of 27 commits on 2026-10-05), but the formal rows had it hold. `CutReplyFence` widens
the entry: the commit's wait (~70 ms over a playing reply) is inside the inference's
await, the `Resume` gap. `MODE = "retryMerges"` is a proposed fix, not code: a second
commit on a turn whose first reply has not played (the gate still armed) interrupts
first — the unplayed reply is dropped and the words are folded into the unanswered
message — and with it everything holds (`ut_retryMerges_all`, 18k states; a probe
confirmed the drop-and-merge branch is reached with a held reply).

`ut_shipped_all` is the SIP front-end as shipped with everything on — `retryOnQuiet`,
`byContext`, `gate`, `shipped`, `inTime`, `EARLY_FINAL` — and holds every property but
`NoSplitTurn` (32k states, a few seconds). Talk and the room mic run the same turn
machine and fence, without the hold (`TEAPORT_REPLY_HOLD_TALK` defaults off) or the
pause (their transport cannot pause): `HOLD = "none"`, `PAUSE = "off"`, whose
`NoStaleReply` and `NoSplitTurn` fail as before #85. The room's own turn timing
(`ROOM_*`, #159) and `SIP_ENDPOINT_STOP_SECS` are durations, which the model does not have.

### Known limits of this model

- **`Inference` requires `~userSpeaking`**, which the controller does not: the guard is on
  the second await only. It is a fact about the *strategy* — both of its conclusion points
  run with VAD quiet. Without it TLC finds a two-step "the user simply never stopped"
  trace the implementation cannot produce, and the counterexample stops being a bug report.
  The cost is one excluded entry: `_timeout_handler` already running when the resume's
  `_discard_pending_end_of_turn()` tries to cancel it, which reaches the same state one
  step earlier.
- **`BotStart` waits for `~stopInFlight`** and for no cut in flight. An LLM round trip
  plus synthesis against two awaits, and an `InterruptionFrame` (a SystemFrame) that
  overtakes every reply frame queued behind it — ordering facts, stated rather than
  modelled, as with `LlmStart` in `LedgerPlayout.tla`.
- **One cut in flight at a time.** `StartTurn` waits for the last turn's interruption to
  land: a turn opens on a transcript, hundreds of ms after the last one, and the frame
  crosses in ~70 ms. Only `FENCE = "none"`/`"timed"` could otherwise put two in flight.
- **The commit's wait is atomic with the commit.** In the code a VAD start can land
  inside the fence's wait and the commit then goes ahead with the caller speaking; here
  the commit runs after the cut lands and `Resume` takes that start — the same states.
- **`ForceStop` commits only when the turn's words were not committed already** (a
  stranded turn's aggregation is empty). Words aggregated since are `EARLY_FINAL`'s on
  the retry path and not modelled on the watchdog's, which nothing reaches in 5 s.
- **Other producers of interruptions** (none in this brain but the turn start) and
  interruptions entering below the fence's head are not modelled.
- **A VAD frozen in SPEAKING is out of scope, and out of the fix's scope too.**
  `SIP_HALF_DUPLEX` left on, or a desynced gateway AEC feeding the bot's own audio back
  in, pins `userSpeaking` forever: no `VadStop` ever arrives, so nothing re-applies the
  finalization and nothing arms the watchdog. Those kill barge-in on their own, upstream
  of all of this, and both have their own kill switch. Modelling them here would only
  make this module look like it covered them.
- **Durations are out of scope**, as everywhere here. That the watchdog is 5 s and the
  retry fires within `ENDPOINT_STOP_SECS` is the reason one is unreachable and the other
  is not; the model only distinguishes reachable from absorbing. The same goes for the
  speculation: whether it is worth anything is the ceiling against when the interim
  settles and the LLM's latency (measured in `speculate.py`), and the model says nothing
  about that —
  only that a stale reply is never spoken and barge-in is as it was.
- **`CtxChange` is a free action** bounded by `MaxCtxWrites`, not a list of writers, and
  `Retext` fires at most once per speculation. The model does not say which component a
  miss will come from — `[SPEC] miss reason=ctx-changed` or `reason=text-differs` in the
  journal does — nor how often the interim and the final disagree; it checks the design
  that stays right whenever they do.
- **Scale.** Since the cut became its own step (2026-10-14): 592 distinct states with `SPEC = "off"` (436 before it, 47 before the reply hold's variables), 4.2k with it on, 338 for `HOLD = "gate"` with words in time and 647 with the timers as they are, 1.5k and 3.5k for the shipped pause with each, 32k for `ut_shipped_all`; a few seconds each.

## `PlayoutClock.tla` — a held reply's playout times at the transport's clock (#85)

`LedgerPlayout.tla` abstracts every number away — its limits say so: the layout's
seconds, `heard_fraction`, the pts cut — and this question is nothing but numbers in an
order: which frame the output transport's clock releases first. So it is a small module
of its own rather than a constant on that one, whose state space is already 300k states.

The TTS stamps its frames with a playout time as it synthesizes them: each word, and
also the response's `LLMFullResponseEndFrame`, re-pushed after the context's audio at
the last word's pts (`tts_service.py`). `BaseOutputTransport` puts every frame with a
pts on a priority queue keyed `(pts, arrival)` and releases each when its clock reaches
it; the assistant aggregator gathers the words it is handed and commits them at the End.
A reply `ReplyHoldGate` held for `hold` units plays from its release, so all of its pts
are that much early unless the gate moves them.

The engine opens a TTS context (`TTSStartedFrame`) up to its first-audio latency `LAT`
before the first audio, and anchors the words at that first audio. So the right move is
the first AUDIO's wait, not the context open's (round-2 review N1).

| SHIFT | | `CommittedIsPlayed` | `WordsOnTime` |
|---|---|---|---|
| `words` | as first written: only `TTSTextFrame` pts moved | ✗ | |
| `firstFrame` | round 1: every pts moved, by the context's first held frame — the `TTSStartedFrame` | ✓ | ✗ |
| `all` | as shipped: every pts moved, by the wait of the context's first held audio or word | ✓ | ✓ |

`pc_release_firstFrame` fails `WordsOnTime` in 3 states (`Wait`, `Release`): every word
lands `LAT` late — captions lag the voice, and a later barge-in's heard cut is short, so
the corrector trims the context below what was heard. `test_reply_hold.py::
test_the_shift_starts_at_the_first_audio_not_the_context_open` is the same on the gate.

`pc_release_words`, 6 states (`N = 3`, `MaxHold = 3`, `LAT = 1`; the trace is from
before `LAT`, the shape is the same):

```
Wait Wait Wait    the caller coughs over the reply's start: held 3 units
Release           words move to pts 4..6; the End keeps pts 3
Pop               the clock's head is the End -- released first; the aggregator
                  commits nothing, and the three words leak into the next message
```

The review's probe found the same on the real transport and aggregator: a cough-release
committed `'Sure here'` and left seven words uncommitted.
`test_reply_hold.py::test_a_released_reply_commits_every_word_through_the_real_transport`
is that probe, kept. The End has no `context_id`; the gate gives it the context it saw
last, which is the one whose audio it follows.

**A second context behind the held reply** (`SECOND`, 2026-10-14 audit). The model had
one context, and the gate's shift and the TTS's anchoring are per context: a context
that starts while the held reply is still to play — a narrator line, the answer after a
tool call — is anchored by the TTS at the held reply's end on ITS model
(`_prev_audio_end_ns`, or its own first audio if later; `engine_tts.start_word_timestamps`),
and the gate moves its pts by ITS OWN first held frame's wait (`reply_hold._restamp`),
while it really plays right behind the held reply, which plays from the release.

| | `SecondOnTime` |
|---|---|
| `pc_second_shipped` — as shipped | ✗ (5 states) |
| `pc_second_fix2` — **proposed, not code**: anchor a context that starts during a freeze on the frozen clock, move an ended context's anchor by the hold since its first audio, give every context held behind the first the first's shift | ✓ (169 states, with `CommittedIsPlayed` and `WordsOnTime`) |

Two ways, both from the code as written. Started *after* the release (TLC's shortest,
`Wait Wait Release Arrive2`): `_freeze_playout` moved the held reply's end by the whole
hold, `now - then`, though the hold began `LAT` before its first audio — the second
context's words land `LAT` late (`pp_hold_anchor` is the same arithmetic). Started
*during* the hold (`Wait Wait Wait Arrive2 Release`, held 3, first audio at 3): anchored
at `max(LAT + N, 3) = 4` on the pre-hold schedule, moved by its own wait, 0 — its words
2 units EARLY, so a cut credits words not yet heard, which no estimate corrects.
Found by the model from the code; not reproduced on the real gate. Rare: the hold is
about a second, and needs a second context to start inside it.

## `PlayoutPause.tla` — heard accounting across playout pauses, and the TTS's freeze (#86)

A sibling of `PlayoutClock.tla`, for the same reason: the question is the layout's
seconds and the pts cut, which `LedgerPlayout.tla` abstracts away.

**Part 1, the ledger.** A reply of `A` units plays a unit per tick from the window's
start; word *k* is scheduled at pts *k*. The transport can pause and resume (up to
`MaxPauses` times) and the caller can cut the reply at any tick; at the cut the ledger
charts what it believes played — the layout's seconds and the words its pts cut keeps —
and the truth is what the transport wrote.

| LEDGER | | `HeardIsPlayed` |
|---|---|---|
| `unaware` | the ledger before #86 | ✗ (4 states: pause, a tick, cut — one unit charted, none played) |
| `noShift` | freezes and re-anchors the layout, reads pts as scheduled | ✗ (5: pause, tick, resume, cut — seconds right, one word too many) |
| `shipped` | `_pause` credits and freezes, `_resume` re-anchors and adds the pause to the pts shift of the turns with audio already pushed | ✓, two pauses in one reply (1.6k states) |

The transport tells the ledger how long after its pause frame the audio really stops
(`lag_secs`: the written frame's 20 ms slot), which the model's tick abstracts.

**A clause synthesized after the pause** (`LATE`, 2026-10-14 audit). Part 1 had every
word scheduled before any pause. The TTS synthesizes clause by clause, and a clause that
comes after playout ran dry is placed after the gap `_note_playout` saw — on the TTS's
clock, which stands still while playout is stopped. A pause in that underrun (the pauser
needs bot-speaking and reply audio, not audio queued: `barge_pause._may_pause`) that ends
before the next clause is synthesized is then in the gap, and so in the clause's pts —
`_freeze_playout` moves `_play_end` by the stop only when it was still ahead of the stop
— and the ledger adds it again (`_resume`: every turn with audio pushed).

| `LATE = 2`, ledger `shipped` | | `HeardIsPlayed` |
|---|---|---|
| `pp_late_shipped_heard` | `TTSGAP = "shipped"`, as shipped | ✗ (9 states) |
| `pp_late_moved_heard` | `TTSGAP = "moved"` — **proposed, not code**: `_freeze_playout` always moves `_play_end` by the stop, so a gap never holds stop time and the ledger's shift is the only move | ✓ (20k states) |

```
Tick Tick      the first clause's two units play out; nothing more is synthesized yet
Pause Tick     the caller makes a sound: a pause with nothing to hold back, 1 unit
Resume         no words; the ledger's shift is now 1
Emit2          the next clause: the TTS sees a 1-unit gap (the pause) and places it after it
Tick Cut       unit 3 played (pts 4); the ledger reads the cut at 4 - 1 and drops it
```

The pause did not delay the clause, and its words carry it twice. The seconds are right
(the layout lays a chunk out no earlier than its push), and `_finish` never reports fewer
words than the played fraction implies, so the chart falls back to that estimate — off by
a word or so — instead of the exact cut. The same arithmetic on the real TTS:
`_note_playout` after a 1.0 s pause in an underrun puts the next clause at 3.0 s, where
it plays, and the ledger's cut is 1.0 s short of it.

**Part 2, the TTS.** A reply hold and a playout pause each stop playout; the two can
overlap. `engine_tts._freeze_playout` freezes the TTS's playout model through them and,
when the last one ends, moves the anchor of a reply queued behind (`_prev_audio_end_ns`)
by the time since the first began. The check is that arithmetic, against a ground-truth
count of the ticks playout was stopped (round-2 review: the first version only checked
"frozen while stopped", close to a tautology). Each part runs in its own rows (`PART`).

| SLOT | | `AnchorMovesByStop` (and `FrozenWhileStopped`) |
|---|---|---|
| `single` | #86 as first written: one frozen-since slot | ✗ (6 states: hold, pause, the hold ends — the slot clears with the pause still on; a tick of it is never added to the anchor) |
| `set` | one freeze per source (`engine_tts._freezes`) | ✓ (189 states) |

**A hold from before the first audio** (`FIRSTAUDIO = "any"`, 2026-10-14 audit). Those
rows had the stopped reply's audio queued before the stop began — a pause mid-reply. A
reply hold is armed at the commit and usually begins before the reply's first audio,
and the reply's short context often ends inside it. Ground truth is now the time stopped
*with the reply's audio waiting*; `_freeze_playout` moves an ended context's anchor
(`_prev_audio_end_ns`) by `now - then`, the whole stop, while the in-progress path
(`_ctx_held_ns`) already counts from `max(then, base)`.

| `SLOT = "set"`, `FIRSTAUDIO = "any"` | `AnchorMovesByStop` |
|---|---|
| `pp_hold_anchor` — `ANCHOR = "stop"`, as shipped | ✗ (6 states: the hold, a tick, the first audio, the context ends, the release — moved 1, stopped with audio 0) |
| `pp_hold_anchor_fixed` — `ANCHOR = "audio"`, **proposed, not code**: the ended context's anchor moves by the stop since its first audio | ✓ (1.1k states, with `FrozenWhileStopped`) |

A reply queued behind such a held reply — the answer after a tool call — is anchored by
however long the hold ran before the first audio, so its captions lag and its heard cut
comes short (the estimate floor again). `PlayoutClock.tla`'s `pc_second_shipped` is the
same, seen from the words.

The anchor's own value is tested on the real TTS in `test_reply_hold.py` and
`test_barge_pause.py`.

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
one. That is a new concurrent protocol: two processors, three FIFO queues (the VAD's
edges to the strategy, the edges and the verdicts up to the STT, the STT's transcripts
down — see THE ORDER below for why three), and two timer tasks — the hold's expiry, and the stranded backstop that now has to stand down while
a segment is held and stand back up when it is released. This module is that protocol,
with two faults in the environment: a verdict lost in flight (`LoseVerdict`) and a VAD stop
the VAD never reports (`VadStopMissed`, the fault the backstop exists for). A third
environment action closes the turn under a running ceiling on something other than the
ceiling (`TurnStop`: the controller re-applying a stop it refused earlier, or its stop
watchdog); what the strategy does then is `TURNSTOP`.

| MODE | | `NoSplitOnIncomplete` | `NoStrandedSegment` | `NoDoubleAnswer` | `NoStaleClose` | `NoOrphanedHold` |
|---|---|---|---|---|---|---|
| `vadStop` | commit at the raw VAD stop (until #43; since, only the no-verdict fallback) | ✗ | ✓ | ✓ | ✓ | — |
| `verdict`, `TURNSTOP = "silent"` | the first cut: nothing reported when the turn is closed under the ceiling | ✓ | ✓ | ✓ | ✓ | ✗ |
| `verdict`, `TURNSTOP = "verdict"` | the verdict commit, as shipped, the VAD one edge ahead of either side (`sc_verdict`) | ✓ | ✓ | ✓ | ✓ | ✓ |
| the same | the strategy three edges behind the VAD (`sc_verdict_stale`, THE ORDER) | ✗ | | | | |
| the same, `NUMBERED` | proposed: the verdict names its stop (`sc_verdict_numbered`) | ✓ | ✓ | ✓ | ✓ | ✓ |

`NoSplitOnIncomplete` is the bug, and `vadStop` fails it with nothing exotic:

```
VadStart     the caller speaks
VadStop      they pause
SttStop      the STT commits -- the segment is closed, unjudged
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
(`SttStart`'s `backstop' = backstop \/ (pending /\ segSpeech)`); drop that conjunct and the
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
are what was missing: with `TURNSTOP = "silent"` the row falls in eleven states (the
caller speaks, an interim opens the turn, the stop's verdict is INCOMPLETE, `TurnStop`,
the STT holds at its stop and the INCOMPLETE is delivered — pending, nothing in flight,
no ceiling, nothing lost), and with the strategy
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
| `stop` | `unmarked` | as shipped, against an engine without the reason field — before teagram-engine#36 (`sc_final_stop`) | ✗ | ✗ | |
| `stop` | `marked` | as shipped, against the engine that marks its dones — teagram-engine#36, what the box runs (`sc_final_stop_marked`, `sc_final_stop_stranded`) | ✗ | | ✗ |
| `receipts` | `unmarked` | proposed (THE ORDER), on the old engine (`sc_final_stop_residual`) | | ✓ | ✓ |
| `receipts` | `marked` | proposed (THE ORDER), on the engine the box runs (`sc_final_receipts`, `_backstop`; `_full` and `sc_voice_final_carry` under `--full`) | ✓ | | ✓ |

The `stop` rows held on the marked engine until 2026-10-14, when THE ORDER (below) put
the VAD where pipecat runs it; the `receipts` rows are the repair.

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

**The `stop` row on the unmarked wire fell in ten states, on one cause** — before THE
ORDER, which adds a second that TLC now reaches first (finding A below). The engine
cuts a breath; the VAD stop holds; the verdict commits *before* the engine's done for the
first half has landed; the STT, pairing dones with commits by order because the wire does
not say which is which, hands the commit's stamp to the engine's done; the strategy takes
the half as the stop's close and ends the turn while the commit's real answer — the rest
of the sentence — is still on its way. `sc_final_stop_residual` was the statement that
this was the *only* way the shipped design ended a turn stale, and `sc_final_stop_marked`
that a done which says whether it answers a client commit (`voxtral_websocket.c`'s
`worker_send_done` sends the same message for its VAD close and for a commit) closed it
entirely; both now say it of the `receipts` design, the shipped one having two more. Live, the mispairing needs the
engine's finish for the first half to outlast the rest of the sentence plus the VAD's
floor and the model's verdict — the GPU-contended finish — and its cost is one cut reply,
the shape the fix is for, at a small fraction of its old rate.

### The caller's voice (#140)

The VAD's start trails the caller's first word by 0.2–0.4 s, so after a stop the caller
can be talking again with no start sent. A complete verdict that commits then draws the
segment boundary inside their words, and the silence hint claims those words as
silence. In the environment, `voice` is that speech as the VAD analyzer's marks see it
(`VoiceOn`, `VoicePause`, bounded by `MaxVoice`): `on` since the stop, `paused` for the
stop's own window afterwards, or `none`. `VOICE` selects the STT's answer:

| `VOICE` | design | `NoCutVoice` | `NoOrphanedHold` | the rest |
|---|---|---|---|---|
| `blind` | before #140: a complete verdict commits over the voice (`sc_voice_blind`) | ✗ | | |
| `pause` | as shipped: the commit is held (`deferred`, in the hold's own timer slot) until `PauseCommit` (the voice paused), `VadStart` (the segment resumes) or `PauseCap` (the session's ceiling) (`sc_voice`) | ✓ | ✓ | ✓ |

The wait is a hold that no verdict will end, so `NoOrphanedHold` takes it as an escape.
`sc_voice_reach` keeps the property as it read before, `HeldOnlyForVerdict`, as the
witness that the wait is reached at all, and `sc_voice_reach_pause` and
`sc_voice_reach_resume` fall on the first trace in which the pause, or a VAD start,
ends one: both exits are reached, not only declared. The two bounds commit over voice
by design, and each has its own row to keep that visible rather than a property that
holds by construction. The cap (`PauseCap`, `capCut`, `sc_voice_cap`) can fire at any
point of the wait. The catch-up bound (`catchupCut`, `sc_voice_catchup`) is a verdict
that commits on what the analyzer has heard because it has not heard the rest in time.
`sc_voice_final` runs the final's protocol (its `receipts` repair) under the wait on the
marked engine: no stale turn end, no stranded turn. `MaxVoice` bounds only the voice
the VAD has not started on; since THE ORDER, a VAD start sets `voice` too (the
analyzer's own finding), so a verdict that reaches the STT after the caller has
resumed but before the STT has seen the start meets the voice and waits, as the code
does on the marks.

### The carry (#154)

After every commit the STT sends the audio from where the commit's silence claim starts
again, as the start of the next segment (`stt.py`, CARRY THE TAIL). No commit or done is
added or moved, so the protocol above is unchanged, but one premise is not: a commit the
caller was quiet for used to leave the next segment empty (`spoke' = FALSE`), and with the
carry it is fed. What a carry sends starts at or after the speech end, so it holds no
utterance's words by the VAD's count (`segAudio` is unchanged), but the engine can stream
an interim for it -- voice since the stop that the carry holds, or the margin's tail --
and the backstop can follow that interim, stamped with a stop already answered.

`CARRY` switches it: with it on, `CommitAt` leaves `spoke` set on every commit but the
backstop's -- `spoke' = speaking \/ (mayCarry /\ CARRY)`. That is a superset of the code
(a commit that claims nothing carries nothing): `spoke` only enables `Delta`, and no
action needs it unset, so feeding the next segment on every commit covers feeding it on
some, with no counter in the state. The backstop is the exception the code guarantees:
every commit is decided and sent under one lock (`stt.py`, THE WIRE), as each action here
is one step, so a speech end the backstop could see belongs to a stop that is holding,
and the backstop stands down for it.

`sc_carry_off` and `sc_carry_reach` are the pair that shows the carry reaches the space:
with the engine's own close off, a segment that holds no utterance's words never streams
an interim without a carry (`NoWordlessInterim` holds) and does with one (it falls). The
rows kept for a design that was rejected (`sc_verdict_silentStop`, `sc_voice_blind`, the
two clock rows) run without it.

**The default run and `--full`.** See THE ORDER's scale note below: since 2026-10-14
`--full` holds the five rows too large for the default run, the carry's three among
them.

### THE ORDER (2026-10-14): where the VAD runs

Until 2026-10-14 this model had the STT see every VAD edge first and atomically — "the
first processor after the transport" — with the strategy's copy queued behind the STT's
own frames. That was pipecat before 1.0. In 1.8.1 the VAD runs in the user aggregator,
*downstream* of the STT, on the aggregator's input task, where the stop strategy runs
too, and an edge it detects is queued twice (`_queued_broadcast_frame`): onto the
aggregator's own input queue, behind whatever audio is already waiting there, for the
strategy; and *upstream* through four processors' queues to the STT. Neither copy is
first by construction. The verdict the strategy pushes when it gets to a stop travels the
same upstream path, so it reaches the STT after that stop, and after any edge the VAD
found in the backlog before the strategy got there. The STT's interims and finals come
down on the aggregator's other task (the process queue, data frames), in no order with
the edges. So the model now has three FIFOs — `vadQ`, `toStt`, `toStrategy` — the STT
handles its copies in `SttStart`/`SttStop`, and the two sides count stops separately
(`sttStops`, `stratStops`). `MaxLag` bounds how many edges either side may be behind; one
already lets either side see a stop first. Every row was re-run; three findings, all
against the code as it is:

**A — a stale turn end on the marked engine** (`sc_final_stop_marked`, ten states):

```
VadStart      the caller speaks
EngineClose   the engine's endpointer cuts a breath inside the sentence
VadStop       they finish; the VAD queues the stop for the strategy and pushes it up
Done          the engine's done for the first half reaches the STT before the stop
              does: nothing asked for it and it has words, so it is stamped as the
              NEXT stop's -- stop 1, the one the VAD has just found
StratStop     the strategy counts stop 1 (owed) and the model says COMPLETE
StratFinal    the first half, stamped 1, pays stop 1's debt
EndTurn       the turn ends on half the sentence; the rest is in the open segment
```

The STT then sees stop 1, finds the segment closed unasked with nothing streamed since,
and commits the tail at once; its final, the rest of the sentence, opens a new turn, and
its interruption cuts the reply to the first half: PR #49's shape, a reply cut at 0% and
the LLM asked again. The stamp rule ("the next stop's") was right when the STT counted
first; the window now is the stop's trip up four queues, so it needs an engine cut whose
finish ends inside a few milliseconds (more under load) — rare, and not seen in a
journal yet.

**B — a turn stranded until the watchdog** (`sc_final_stop_stranded`, fourteen states),
the order the other way round: the engine cuts mid-sentence and its first half lands; the
STT sees the stop, finds the segment closed unasked and commits the tail at once; the
tail's close, stamped with that stop, reaches the strategy *before* the strategy has
counted the stop (its input queue backed up behind audio), so it pays nothing; then the
strategy counts the stop, and owes a close that has already gone by. The transcript is
complete and finalized, the verdict COMPLETE, and nothing can end the turn until the next
VAD stop or the aggregator's 5 s watchdog: the caller waits five seconds for the answer.
It needs the tail commit's round trip through the engine to beat the strategy to its own
queued stop; it is quickest in the appliance's common case, an engine cut at the end of
the speech, where the tail is silence and its close an empty done.

**C — a verdict for the stop before** (`sc_verdict_stale`, fifteen states, `MaxLag = 3`):
the strategy's queue holds a whole utterance — stop 1, the caller's next start and stop
2 — when it answers stop 1 COMPLETE, so that verdict goes up behind stop 2.
`TurnVerdictFrame` names no stop, the STT takes it for stop 2's and commits the held
segment; the model then calls stop 2 INCOMPLETE and the caller resumes inside the
ceiling: the #43 split. It needs the aggregator's input task stalled for a whole
utterance (≥ ~0.5 s with Talk's 0.2 s stop), which Smart Turn's inference on the
appliance's cores under load could do but has not been seen to.

**The repairs, modelled, not in the code:**

- `NUMBERED` — the strategy stamps each `TurnVerdictFrame` with the stop it answers (its
  own count; the ceiling's and the turn-stop's with the stop they override) and
  `_handle_verdict` ignores one that is not for `_vad_stops`. `sc_verdict_numbered`
  holds every commit property at `MaxLag = 3` (1.1 million states).
- `DESIGN = "receipts"` — `LateStartTurnStopStrategy` keeps the stamps of closes for
  stops it has not counted yet (`stop_n > _stops`) and, at the count, finds that stop's
  debt paid; and `_handle_message` stamps a done nothing asked for that carries words
  with no stop at all (a sentinel `_note_close` treats as paying nothing — not `None`,
  which pays as stock): the stop it lands near is always answered by its own commit, the
  tail's included. `sc_final_receipts`, `_backstop` and `sc_voice_final` hold every
  property in the default run, `sc_final_receipts_full` (the backstop, a lost verdict,
  the engine's cut and the carry together: 26.6 million states) and
  `sc_voice_final_carry` under `--full`; `sc_final_stop_residual` says that on an engine
  without the reason field the mispairing is again the only stale end left.

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
- **The analyzer's catch-up is abstracted (#140).** The STT waits for the VAD analyzer to
  have heard the audio sent at the verdict before it reads the voice; the model reads
  `voice` at the verdict as if that wait were instant. Voice in the last unscored chunk
  before a commit is below what either can see. The hold's expiry and the backstop are
  not charged with a cut over the voice: they are bounds with their own documented edges.
  Two session-wide policies are not modelled as switches, for the reason the no-verdict
  fallback is not. The wait stands down after three verdict commits running that hit
  the cap: any other verdict commit breaks the run, and so does a VAD start during a
  wait; commits that answer no verdict leave it alone. The marks go
  off when a catch-up that missed its bound has still not got there by the next
  verdict; slowness alone only commits on what was heard. Each is a policy on repeated
  faults; what runs while either is in force is the `blind` design, and
  `sc_voice_blind` is its cost. A disconnect during the wait is not
  modelled either: it drops the segment with the session, as it does a hold, and this
  model has no disconnect.
- **The over-bot path is not modelled.** A VAD stop while the bot is speaking commits at
  once (the flush is the barge-in, teagram-engine#7); it is the `vadStop` design applied to
  one stop, and the verdict that follows is ignored exactly as a stale one is.
- **The session-wide fallback is not modelled as a switch.** A session in which no verdict
  ever reaches the STT falls back to committing at the raw VAD stop at its first expiry,
  and back onto the verdict at the first verdict that arrives; that is a policy on
  repeated faults, and `MaxLost = 1` never reaches it. What it runs while in force is the
  `vadStop` design, whose two rows stay for that reason now that it is no longer an
  operator setting (`TEAPORT_STT_COMMIT_ON`, retired 2026-10-09): `sc_vadStop_sound` is
  what the fallback keeps, `sc_vadStop_split` what it costs.
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
  only one left under the `receipts` repair; the engine-side marker is the fix, and `DONES = "marked"` is that engine:
  teagram-engine's `transcription.done` carries `"reason": "commit" | "vad"` since the
  trailing-silence credit (its PR for issue #29), and `stt.py` places a done marked
  `vad` as the engine's own close whatever the queue holds. On that engine the marked
  rows are the shipped configuration; against an older one the field is absent and the
  queue pairs as before, so the unmarked rows still describe it.
- **THE ORDER is bounded** (`MaxLag`): the VAD finds a new edge only while neither side
  is `MaxLag` edges behind. One edge either way is every row's but `sc_verdict_stale`
  and `sc_verdict_numbered`'s three. The strategy's stop is one step here (count, model,
  verdict); in the code the inference is an await inside it, and a final the process
  task hands the strategy meanwhile sees the stop counted and no verdict yet — the same
  as landing just after the step, since the turn's end needs both.
- **Disconnects are not modelled** — the hold for a phone call (`stt.hold()`), a
  reconnect. `_disconnect_websocket` drops the segment, the queued stamps and the hold
  and pushes `SegmentResetFrame`, but the strategy's `_handle_segment_reset` leaves its
  owed close standing: a stop counted before the disconnect is owed a close that will
  never come, and a turn that needs it (one open at the hold, or a final with no stop in
  sight after the resume) waits for the next stop or the watchdog. A suspicion from
  reading, not a counterexample: the turn at a hold has usually just ended on the
  caller's answer.
- **One interim per segment.** `Delta` fires once per open segment: further deltas change
  nothing this model reads, and the engine streams none for a new segment until the
  previous finish is done (one worker per session), which is when the STT's buffer
  empties. Without it the queue to the strategy is unbounded. It also leaves out the
  committed segment's own last deltas: the engine's finish drains them before its done
  (`worker_drain_tokens`, then `worker_send_done`), and `stt.py` keeps the old buffer
  until the done, so such a delta re-arms the backstop once the commit has cleared
  `_commit_pending`. Harmless unless the done trails that delta by the backstop's 1.5 s
  — the GPU-stall edge `stt.py` already documents for interims — when the backstop would
  commit the *next* segment, the caller's next words with it.
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
- **Scale.** Measured 2026-10-14 on TLC 2.19, THE ORDER's model, two workers on a
  desktop shared with other checks (so slower than the figures from before it, which
  had the machine). Default run: `sc_verdict` 7.0 million distinct states (4 min 11 s),
  `sc_voice` 4.3 million (1 min 45 s; the missed stop moved to `sc_voice_missed`),
  `sc_vadStop_sound` 3.7 million (1 min 24 s), `sc_voice_final` 1.3 million (30 s),
  `sc_verdict_numbered` 1.1 million (47 s), the three `receipts` final rows 260,000 to
  480,000 each (under 20 s), `sc_carry_off` 260,000 (8 s); every row that falls finds its
  trace in 8 to 15 states, in a second or a few. `--full`: `sc_final_receipts_full`
  26.6 million (14 min), `sc_voice_missed` 27.9 million (12 min 30 s),
  `sc_vadStop_sound_carry` 5.6 million (3 min 36 s), `sc_voice_carry` 5.7 million
  (2 min 41 s), `sc_voice_final_carry` 4.1 million (1 min 28 s) -- about half an hour
  in all. The three queues cost most where the engine's own close and the backstop are
  both on (the final rows: 0.4 million with either, 8 million with both, at the same
  other bounds), which is why the default final rows take them one at a time.
  `check.sh` passes `-deadlock`: the bounds make terminal states ordinary. Before THE
  ORDER (2026-10-11/12, the whole desktop): `sc_verdict` 1.6 million, `sc_voice` 10.9
  million, `sc_voice_final` 13.6 million, `sc_final_stop_residual`/`_marked` 6.2 and
  5.7 million, the `--full` carry rows 20 to 30 million each.

## Worth modeling next

`UserTurn.tla` was never on this list, and that is the most useful thing this list has
told us. Everything below is a consequence a model already reaches; the barge-in
precondition was a *premise* five models shared, and a shared premise is invisible to
every one of them. Worth asking of each new entry: is this a step the machine takes, or
something the machine is assumed to be handed?

1. **Spoken notices in `LedgerPlayout.tla`** — now worth a constant. The call sites
   this item named have moved on (2026-10-14 audit): `llm_error_speaker.py:83` and the
   STT-busy lines (`agent_session.py:687`, `:805`) are fillers now. The notices committed
   to the context today are the prepared greeting (`agent_session.py:699`), the
   back-from-hold line (`:817`), `say_last_line(append_to_context=True)` as a
   conversation goes on hold (`:751`) and the take-the-call question
   (`call_prompt.py:92`). The ledger queues each as an expected context, and since the
   stop frames it is a third kind of context: pipecat re-pushes no End for a speak frame,
   so its stop frame is its only completion signal, and the ledger's stop handler charts
   only a turn that never played — a notice whose stop comes after the 3 s idle fallback
   has closed the window stays open until the next window closes. A `Notices` set beside `Replies`
   (queued at the TTS's sighting, no drain) would check the order against replies and
   that path.
2. **`HeardContextCorrector` against the ledger's chart.** The bounds this item asked
   about read sound: `_done` indexes `ledger.events`, which is never trimmed, and `_mark`
   is kept by `drop_messages`; `ClientNotes._fold` inserts below the tail without it
   (`client_notes.py:399-400`), but only system notes, which the walk skips. The
   tool-ack lines it named are `tools.py:561-568` and `:1193-1198`. What is worth a model
   is the join: `_truncate` finds the cut reply by POSITION, and every cut charted since
   the last reconcile is applied to the same tail — so the corrector is exactly as right
   as the ledger's chart, and `LedgerPlayout.tla`'s `lp_unheard_unspeakable_opens` is a
   heard reply deleted from the context. `NoUnheardWhenPlayed` is the ledger's half of
   that contract; the corrector's half (one cut, one anchor; a never-played chart with
   nothing committed touches nothing) is unchecked.
3. **`MemoryRecall`'s single-flight + turn generation tag** — small and already
   carefully written, so cheap regression insurance rather than a suspected bug.
