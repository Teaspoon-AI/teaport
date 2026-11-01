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

## `Followup.tla` — a one-shot order in the context, and the gate's turn latch

> Until #203 this was the model of the async `openclaw_agent_consult` follow-up: when a background
> consult finished, a one-shot USER-role trigger ("tell the user now…") was appended to
> the context and an `LLMRunFrame` queued, and a delivery loop (`MODE = "heard"`, #30)
> posted it again until the caller had heard a third of its words. #203 broke that
> loop's premise and `Owed.tla` replaced it: `owed.py` renders its note into each
> request and stores nothing, so there is no trigger to retire. The loop's rows
> (`fu_heard_*`) and its code (`_make_consult_followup`, `OneShot.void`,
> `Delivery.replies_only`, `TranscriptLedger.next_assistant`, `_MIN_HEARD`) went in the
> same change. What stays is what still describes live code: retiring a one-shot order
> AT its read (`FollowupTrigger`, which now retires `ClientNotes`' reaction order), and
> `FollowupGate`'s turn latch (`LATCH`), which `ClientNotes` and the run's door wait on.
> The history below keeps the follow-up's words where they are the record of a live
> incident.

Models `FollowupGate` (`followup_gate.py`) together with an injector that appends a
one-shot USER-role order to the context and queues an `LLMRunFrame` (the consult
follow-up's trigger then, `ClientNotes`' reaction now).

Retiring that order is a **two-sided** constraint, and each side has already cost a
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

The injector arms the one-shot **before** `queue_frames` (the completion can start
while `queue_frames` is still awaiting): `ClientNotes._post_order` does. The read point's
two facts are `brain/tests/test_followup_trigger.py`
(`test_the_response_start_alone_does_not_retire_it`, `test_someone_elses_activity_does_not_retire_it`).

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
placeholder nothing answers from) once no other call is out, by a run that says nothing
(`RunSilentFrame`, #203), or by an interruption. Since #203 the gate also keeps the calls
out by id (`_calls_out`; `Owed.tla`'s `endClearsTool` and `staleFlag` rows): a turn whose
line ended with its call still out stays shut. `wait_until_idle(turn_free=True)` waits on idle *and* `~_turn`;
`ClientNotes` passes it (the follow-up injector did), the narrator does not, and the
run's door (`FollowupGate.ask`) waits on the same window. The gate sits before the
assistant aggregator, so it sees the result frame. `test_followup_gate.py` covers the
synchronous shape (narrator idle, a turn-free wait held until the answering completion
ends), the no-inference result, and the interruption.

### Known limits of this model

- **Durations are out of scope.** `_QUIET_SECS` and the claim's expiry are timing
  assumptions TLA+ cannot judge.
- **One injector.** `ClientNotes`' reaction competes with `owed.py`'s run for the same
  claimed windows; the claim keeps the two apart, and `Owed.tla` models the run's side
  (`NoRunMidTurn`, `NoRunUnderSpeech`). A reaction stays armed through a barge-in on
  purpose, so the caller's turn folds the reaction into its reply. Nothing checks or
  retries a reaction, so the liveness question the follow-up had (`EventuallyDelivered`,
  never established here) is not asked of it; the consult's is `Owed.tla`'s
  `OwedResolves`.
- **What the model says** is reduced to "reads the order or not".

## `Owed.tla` — owed until heard (#203)

This model was written before the code it describes, and led it: it is the delivery
design for issue #203, and it replaced `Followup.tla`'s delivery loop (`MODE = "heard"`),
whose rows and code went in the same change. The code below is the model's actions,
one for one; nothing in it forced a change to the model (see "What the code adds" for
what it does that the model abstracts).

#203 broke `Followup.tla`'s premise. The loop counted a reply as delivered when the caller
heard at least `_MIN_HEARD` (a third) of its words. On 2026-10-16 the caller cut in 1.1 s
into "The top story on Hacker News right now is…": 31% of the words and none of the
answer. The trigger was retired as "already given", and "I'm ready." got a second
49-second lookup. The new design has three concepts and two states:

| | what it is | code |
|---|---|---|
| the **record** | what the caller is still owed, outside the conversation: `none` (consult running) → `owed` → `closed` (or `dropped`, evicted unheard: not modelled) | `owed.Record`, kept by `owed.Owed` (one per session) |
| the **note** | rendered into every request from the record while it is owed, never stored | `owed.render(messages, records, purpose)`, called by the render hook in `BoundedOpenAILLMService._open_stream` through `Owed.view` |
| the **evidence** | the caller heard the **gist** -- the answer to what they asked (#210); a failure, by a reply about it heard to its end. A reply about the record played to its end closes it if the gist judge, shown the caller's words, the result and what was heard of the record, answers HEARD -- or gives no verdict; the second such reply closes it whatever the verdict (the backstop). A cut closes it only if the gist judge passes the reply's whole text and the cut judge, shown the question, the heard prefix and the unheard rest, answers HEARD. Both judges read what was heard of the record so far, not the one reply | `Owed.settle`, `Owed._judge` (the cut judge), the gist judge beside it, fed by the settle feed |

A sync tool's answer (web_search, web_fetch) joins the same book as a record when a
telling of it is cut before the caller heard it (#204); how it is filed is
`OwedAnswers.tla`, below. Its record's life from there is this model's.

The brain speaks unprompted at most twice per record. It asks one **run** when the answer
lands, and one **re-offer** at a long lull (`TEAPORT_REOFFER_AFTER_QUIET_S`, 10 s) once
the re-offer is armed. That is §4 (a), the user's choice on 2026-10-16.

### Actions and the code they become

| action | code |
|---|---|
| `Land` | `tools._consult_and_land` → `Owed.land` (the tools hold the session's `Owed`; `Owed.dispatch` registered the consult and folds a duplicate into it): the placeholder tool result is rewritten once to the cleaned outcome (`owed.clean_outcome`; the rewrite moved from the old `speak_followup`), and the landing run is asked unless `FollowupGate.caller_request_pending` (their turn is open, `_user`, or a commit passed `TurnCommitMark` and the LLM has not rendered it, `request_rendered`); then the re-offer is armed instead |
| `Ask` | `FollowupGate.ask(keys, purpose, context)`: the run's own slot (`_run`), beside the post-tool answer's `_held_answer`. A re-offer counts at its ask (`Record.reoffers`), so a held-then-dropped one is one |
| `RunGo` | `FollowupGate._run_door`: past the hold, a window that is turn-free (`_clear`: no turn, no request or reply in flight, no tool round out, nothing claimed) and VAD-free (no `_vad`, no `_speech_pending`), debounced; the gate claims the window and pushes a `RunFrame` UP. Whether its records are still owed is asked where it renders (`Owed.view`, after `settled()`): in the quiet window nothing lies between the go and the render, so that is the model's check at the go. A run whose records were heard renders nothing (`_NothingToSay`) and its empty completion ends the gate's turn (`RunSilentFrame`) |
| `Cap` | `_run_door`'s hold: a run held under speech for `TEAPORT_REPLY_HOLD_MAX_S` is **dropped** (`on_run_dropped` → `Owed.run_dropped` arms the re-offer), never sent. The post-tool answer keeps `_answer_capped`'s release |
| `GateQuiet` | `_speech_over`: no words within the release window |
| `ToolResult` | pipecat's `_maybe_push_context_after_function_result`, as in `UserTurn.tla`'s POSTTOOL. The gate keeps the ids of the calls out (`_calls_out`, `gtool`): an id is added by its `FunctionCallInProgressFrame` unless it has already settled or been cancelled (`Mark`: the frame is uninterruptible and can come after the barge-in, `staleFlag`), and removed by its result or its `FunctionCallCancelFrame`. A call out keeps the window shut although a line before it ended `_turn` (`tend`, `endClearsTool`) |
| `Issue` | the LLM takes a caller commit or a post-tool answer; `_open_stream` asks `Owed.view` (which waits for every pending settle) and sends the view. Only then is a commit no longer "still to be rendered" (`request_rendered`), so a consult landing during that wait leaves the note to it. The commit passed `HeardContextCorrector` first; `sp` is the speculative reply, opened through `_open_stream` directly (`speculate.py`), its noted keys carried on the stream to the commit that adopts it |
| `Say`, `Played` | the completion and its playout. `stay_silent` is offered only in the re-offer's view, and `run_function_calls` consumes it: no call, no result in the context, a `RunSilentFrame` to end the gate's turn (only with no other call out) |
| `TurnStart` | `UserStartedSpeakingFrame` at the gate (the waiting run dropped, `on_caller_turn` → `Owed.caller_turn` arms the re-offer) and the `InterruptionFrame` (the ledger charts the cut, the gate drops the held answer, the reset drops what is queued, a sync call is cancelled) |
| `Finish` → `Settle` | the **settle feed**: `TranscriptLedger.on_reply_end` → `Owed.settle`, at the reply's own end, once per reply. A cut: at `_interrupt`, with the pts-exact prefix (`exact`, never `heard_text`); a reply cut before its TTS opened, heard nothing. Played through: at the ledger's chart (`_close_ready`), once the reply's synthesis is done (its own `TTSStoppedFrame`) and all of its audio has played -- the window close its last chunk played out in -- with the whole text; a `BotStoppedSpeakingFrame` mid-reply (the transport's 3 s fallback on a TTS stall) or at a filler's end settles nothing. No text or no audio: at its End, or where the ledger drops it. The reply is the `owed.Reply` the LLM hangs on its `LLMFullResponseStartFrame`. A reply about a record (`Record.about`) is judged on what was heard of the record so far (`Record.heard`): played through, by the gist judge (`Owed._played` -> `services.BoundedOpenAILLMService.judge_gist`, asked as its stream ended: `Owed.reply_text`, fed by `TranscriptLedger.on_reply_text`; `PtVerdicts`), or the backstop at its second (`Record.told`); cut with a heard prefix and its whole text, by both judges (`Owed._cut` -> `judge_gist` + `judge_cut`, `CutVerdicts`): the settle completes when the verdicts land (`Owed._apply`, on the live book), the reply pending till then |
| `Lull` | `Owed._lull_loop`: every reply settled, and `FollowupGate.quiet_for() >= TEAPORT_REOFFER_AFTER_QUIET_S` (no caller speech pending, no turn, no request or reply, nothing claimed, held or waiting at the door) |
| `Commit` | the user aggregator's commit (an empty final commits nothing) |

### What the code adds

Each is outside what the model checks, and each errs toward owed or toward silence:

- **Several records.** At most `MAX_RENDERED` (3) owed records render; a fourth evicts the
  oldest as `dropped` (not heard, its tool result kept, no note): never `closed`, which
  would break `ClosedOnEvidence`. One run renders every owed record, so a second ask
  joins the waiting run. Consult and sync records share the book (the model holds one
  of either kind); every record is made through `Owed._owe` (land, a cut sync answer, a
  wake's restore).
- **The cut judge's wait.** A cut that needs a verdict keeps its reply pending until the
  verdict lands -- measured p50 0.34 s, p90 0.58 s, max 2 s -- each call bounded by
  `SETTLE_WAIT_S` (2 s), past which it is no verdict and errs owed. The call starts at the
  interruption and mostly overlaps the caller's own speech; a render that comes before it
  lands (the caller's next commit, a run, the lull) waits for it, inside the same bound.
  The rest it is shown is the ledger's own (`transcript_ledger._unheard`, aligned by
  characters to the heard prefix; none if it is not one -- no verdict). A session that
  ends with a verdict still to come (`Owed.close_session`, the room's sleep before its
  snapshot) applies it as none at once: the cut answer is owed and saved. Not modelled
  (`Owed.tla`'s `SessionEnd` has no verdict in flight); `test_owed.py` pins it.
- **The bounded settle wait: an accepted residual.** The render waits `SETTLE_WAIT_S`
  (2 s) for pending settles; past it the readers go on without the replies still
  pending, with a warning (one lost reply does not hold every later render). Each still
  settles when its end arrives, so its evidence still counts and a cut sync answer still
  makes its record (errs owed; the #204 review's finding 7); only a reply the ledger
  never saw (unclaimed: never played) is settled there as heard nothing. The model's
  readers wait unboundedly. If the bound is ever hit by a reply that the caller did
  hear, a run rendered right after can tell it again: `ow_lagSettle`'s repeat, in
  practice, behind a 2 s timer. The lull does not take the bound: it waits for the
  settle itself. A reply that put a call out holds no reader unless its request carried
  the note (`Owed.called`): its round's answer is a request of its own after it, and a
  cut of it cancels the call.
- **The session's end** (`Owed.close_session`, from the gate's cleanup and the room's
  sleep): a consult still running has its placeholder rewritten to its failure record,
  owed, with no run. **Room sleep and wake** save and restore the owed records with the
  conversation (`wake_gate`).
- **A run that says nothing** (`SILENT`, `stay`): the model ends its turn there. The
  code tells the gate (`RunSilentFrame`) when the run's completion gave the TTS nothing it
  would speak -- counted with the TTS's own test (`tts_text.has_speech`), so a reply of
  zero-width spaces (#70) is silent -- and called nothing. What it cannot see: text the
  LLM pushed that a processor below it then swallowed without a word in its place. The
  guard speaks its recovery line for anything it empties; `MoodTag` alone could take a
  reply that is only a mood tag, and that run's turn would stay set until the next
  interruption.
- **The note's placement and texts** (`render`): the measured texts; the run line first,
  the re-offer line last (where rounds 4 and 5 measured it).
- **The heard quote.** A cut reply that carried the note and closed nothing has its
  pts-exact heard text quoted ("They heard only …") when it was a run of ours or shares
  a non-common token with the record (`Owed._about`: the #203 session would otherwise
  have quoted a clarifying question, `test_owed_corpus.py`). The re-offer's note never
  quotes (the confirmation round: 9/40 stay silent after "hold on" with the quote, 18/40
  without). The status line is not in the model.

### What the model may say

The model's reply is nondeterministic, bounded by the replay rounds' measurements (20
trials each, gpt-oss-120b on Groq, one session's contexts):

| request | choices | evidence |
|---|---|---|
| carries the note | tell, tell part, defer, other (+ a sync tool call on a caller turn, bare or after a line: "Let me check." + the call) | X1-I 18/20 answer, W 18-20/20, HOLD 60/60 defer |
| carries the note, `FLAKY` | + re-look-up | measured: 1/20 in X1-P and X2-I (round 1); 0 in round 2's "I'm ready"/"go ahead" arms |
| no note, after landing, not yet heard (mutations only) | anything, re-lookup included | B0 19/20 re-lookups |
| no note, before landing | nothing to tell | — |
| no note, closed (the telling is in the history) | mention it or not, no lookup | K-M-ready: 15 ok, 5 mention, 0 lookups |
| the landing run | tell; `PARTIAL`: tell some items; `SILENT`: no audio (#70's guard, an LLM error) | D-first 20/20, round 5 C 20/20 |
| the re-offer run | tell, or `stay_silent` | round 5: 10/20 silent after "hold on", 2/20 after an unrelated exchange |

The outcome is abstracted to **two items with terms** (a two-story list). That makes "one
item of two" and "a filler that shares common words" expressible. The caller hears
`nothing | lead | filler | some | all` of a reply. A telling may open with a lead-in that
restates the question, and a cut can land before, inside or after the items. The ghost
`heard` means that some reply's heard text carried every item. §3's lexical premise is
that the rule reads that exactly (the corpus test before the PR). So the model asks
whether the **rule** closes on it.

### Properties

| property | meaning |
|---|---|
| `ClosedOnEvidence` | `st = closed ⇒ heard` |
| `NoteWhileOwed` | every request rendered while owed carries the note |
| `NoStoredNote` | no request carries the note for a closed record. A type invariant over the render would be vacuous, so the `storeNote` row carries it |
| `NoStaleNote` | no request tells the model "They have NOT heard this yet" after they have |
| `NoRunAfterHeard` | `req ∈ {run, reoffer} ⇒ ¬heard`, ghost-based: a run decided on a record the feed has not caught up with is a counterexample |
| `NoRepeatRecital` | `¬repeat`: no unprompted telling begins after `heard` (08-26 08:17) |
| `NoRunUnderSpeech` | a run never goes while caller speech is pending (#198's hold, #197) |
| `NoRunMidTurn` | a run never goes inside a caller's turn (words in, unanswered) or a tool round |
| `HoldNoLoss` | neither a run nor a held post-tool answer is displaced by the other |
| `OneReoffer` | at most one re-offer per record. A held-then-dropped re-offer counts (it is counted at the ask) |
| `NoRedundantLookup` | the model never re-looks-up what the outcome answers |
| `OwedResolves` | `owed ~> (closed ∨ sessionEnd ∨ reoffered)`, where `reoffered` is a terminal for unprompted speech only: the note keeps rendering, so a caller who asks gets the answer |

### Rows

Every mutation row is the design with exactly one change. Partial and silent runs are
off in those rows (they have rows of their own), so each counterexample shows its own
mechanism. Counterexample lengths vary with TLC's worker count.

| row | what | verdict | the counterexample, or the state count |
|---|---|---|---|
| `ow_design` | the design as the model corrected it (`anyMiss`, `about`), with partial and silent runs, `MaxSpeech = 6` | holds, all 12 safety properties | 56,128 states (`MENTION` on) |
| `ow_design_live` | the same under `FairSpec`, `MaxSpeech = 5` | holds `OwedResolves` | 43,824 states |
| `ow_asWritten_closed` | DESIGN §3 as written (`RUNCLOSE = "uncut"`) | ✗ `ClosedOnEvidence` | the landing run tells one item of two and plays through; "uncut run" closes it |
| `ow_asWritten_live` | DESIGN §4 as written (`REOFFER = "cutOrTurn"`) | ✗ `OwedResolves` | the landing run produces no audio; with no cut and no caller turn, nothing ever arms the re-offer (the hold cap's drop does the same) |
| `ow_fraction` | today's ≥ 1/3 of the words | ✗ `ClosedOnEvidence` | the run is cut after a lead-in or partway into the items, and the fraction closes it (#203) |
| `ow_countSettle` | two uncut noted replies close (X2) | ✗ `ClosedOnEvidence` | the caller's turn is open at landing; a noted reply tells one item, another defers; both play through and the count closes |
| `ow_singleWord` | no common-word list, no 2-term minimum | ✗ `ClosedOnEvidence` | a noted reply defers ("Sure, take your time.") and its common words close it |
| `ow_oneItem` | one item's terms close | ✗ `ClosedOnEvidence` | the run is cut after the first item |
| `ow_ledgerMax` | the feed reads the ledger's `heard_text` as it is | ✗ `ClosedOnEvidence` | the run is cut after the first item, and the estimate reads both |
| `ow_storeNote` | the note stored once at landing | ✗ `NoStoredNote` | the run is heard to the end, closed; the caller's next turn still carries the stored note |
| `ow_renderInGet` | the hook in `get_chat_completions` | ✗ `NoteWhileOwed` | the answer lands mid-turn; the commit adopts a speculative stream that never passed the hook |
| `ow_runAfterCut` | no bound on runs after a cut or a turn (§4 (c), today's loop) | ✗ `OneReoffer` | a re-offer held and dropped by a caller turn, then a second at the next lull |
| `ow_lagSettle` | the readers do not wait for the settle feed | ✗ `NoRunAfterHeard` | the run is cut after its last item (heard); the close is still in the feed; the lull reads owed and asks a re-offer |
| `ow_lagSettle_repeat` | the same | ✗ `NoRepeatRecital` | a noted caller reply tells it all; the lull, ahead of the settle, re-offers, and the run recites it again |
| `ow_askOnly` | the run validated only at `gate.ask` | ✗ `NoRunAfterHeard` | a sync tool is out, so the landing run waits for its window; the post-tool answer goes first, carries the note, tells it; the run goes anyway |
| `ow_oneSlot` | the hold's single slot | ✗ `HoldNoLoss` | a post-tool answer is held under speech; the landing run is asked into the same slot and displaces it (or the other way round) |
| `ow_turnLevel` | a turn-level window (today's `wait_until_idle`) | ✗ `NoRunUnderSpeech` | the caller starts talking, the answer lands, the run goes before their words make a turn (#197) |
| `ow_vadLevel` | a VAD-level window | ✗ `NoRunMidTurn` | the caller's reply is a sync tool call; the run goes between the call and its answer |
| `ow_endClearsTool` | the gate's turn half ends at a completion's End even with its call out (`FollowupGate` before the review round: `_turn` cleared by the `LLMFullResponseEndFrame`) | ✗ `NoRunMidTurn` | a caller request in flight without the note; the answer lands, the run waits; the reply is "Let me check." + a sync tool call, its line plays, its End clears the turn, and the run goes before the tool's answer (10 steps) |
| `ow_staleFlag` | the gate's tool round as ONE flag, set by a call's `FunctionCallInProgressFrame` (`FollowupGate` before the second review round). The frame is uninterruptible: it waits in the TTS's and the transport's queues and reaches the gate after the barge-in that cancelled the call (`Mark` after `TurnStart`) | ✗ `OwedResolves` | the answer lands, the run waits; the caller's reply is a sync call, the caller barges in (the call cancelled, the run dropped, the re-offer armed), the stale in-progress frame sets the flag, the turn commits no words: no completion clears the flag, the door and the lull stay shut for good (13 steps) |
| `ow_adoptDropsKeys` | the commit that adopts a speculative reply registers none of the keys its note carried (`BoundedOpenAILLMService.get_chat_completions` read `owed_noted` off `speculate._AdoptedStream`, which never had it: Fable's final review) | ✗ `NoRepeatRecital` | the caller's turn tells it all through an adopted stream; the record stays owed, the lull re-offers, and the run tells it again (12 steps) |
| `ow_failure` | `OUTCOME = "failure"` (one term-less item), the design (`RUNCLOSE = "about"`): a noted reply about the record, heard to its end, closes it | holds, every safety property | 28,310 states |
| `ow_failure_live` | the same under `FairSpec` | holds `OwedResolves` | 22,400 states |
| `ow_failure_runOnly` | a failure closed only by an uncut run (`RUNCLOSE = "terms"`, the code before Fable's review) | ✗ `NoRepeatRecital` | the landing run tells it and is cut after its last word (or the caller's own turn tells it in full); it stays owed, and the lull's re-offer tells it again (11 steps) |
| `ow_flaky` | `FLAKY` | ✗ `NoRedundantLookup` | the caller's turn carries the note, and the model looks it up again anyway |
| `ow_flaky_rest` | `FLAKY`, every other property, `MaxSpeech = 6` | holds | 85,042 states |
| `ow_judgeWrong` | the cut judge answers HEARD with part of the answer unheard | ✗ `ClosedOnEvidence` | the run is cut after a lead-in; HEARD closes it (7 steps). The measured residual: 1/145 false closes by majority vote on 241 live cuts |
| `ow_prefixLexical` | a cut decided by the lexical rule on the heard prefix (a8fa2ba) | ✗ `ClosedOnEvidence` | the run is cut after each item's content words, before its facts ("words"): two terms an item close it (10 steps). Measured: 89/145 false closes on the same cuts |
| `ow_judgeOwes` | `OWES`: the judge answers OWED, or not within `SETTLE_WAIT_S`, though all was heard | ✗ `NoRunAfterHeard` | the run is cut after its last fact; OWED keeps the record owed and the lull re-offers it (10 steps). The residual: 14/96 false owes, at the trailing edge |
| `ow_judgeOwes_rest` | `OWES`, every other property but the repeat and the stale note | holds | 154,228 states |
| `ow_gist_lexical` | `CLOSE = "lexical"` with `LEXMISS`: the coverage rule as #204 shipped it, on a full telling it reads as not covering | ✗ `RetellOnce` | the run tells it all and plays through; the record stays owed, the re-offer tells it again and is cut, the caller's next turn tells it a third time (14 steps). Live: a one-fact answer told in the caller's words is one term of seven (2026-10-23 14:06, five tellings) |
| `ow_gist_noBackstop` | the gist judge with `PTOWES` and no backstop | ✗ `RetellOnce` | the run tells it all, judged OWED; the caller's turn tells it again, judged OWED; the re-offer tells it a third time (15 steps) |
| `ow_gist_oneReply` | the judges read the one reply, not what was heard of the record | ✗ `RetellOnce` | the run tells it all, judged OWED (`PTOWES`); the re-offer re-tells it and the caller cuts in before its facts (they heard it already); that cut is OWED on its own prefix, and the next turn tells it a third time (14 steps) |
| `ow_gist_ptOwes` | `PTOWES`: the gist judge answers OWED on a full telling | ✗ `NoStaleNote` | the run tells it all and is judged OWED; the re-offer's request says NOT heard (7 steps). Measured: 2-4/29 false owes by majority of 10 |
| `ow_gist_ptOwes_rest` | `PTOWES`, with partial and silent runs and `MENTION`: every property but the one re-telling and its stale note | holds, `RetellOnce` included | 101,510 states |
| `ow_gist_noVerdict` | `NOVERDICT`: no verdict on a reply about the record played through closes it (the user's choice, 2026-10-23) | ✗ `ClosedOnEvidence` | the caller's turn carries the note and tells part of it; no verdict, closed (10 steps). The accepted cost: the note stops; the result stays in the context |
| `ow_gist_noVerdict_rest` | `NOVERDICT`, every other property | holds | 77,076 states |
| `ow_gist_wrong` | the gist judge answers HEARD on a part or a mention | ✗ `ClosedOnEvidence` | the run tells one item of two; HEARD closes it (6 steps). Measured: 0/25 false closes by majority |
| `ow_gist_twice` | `TWICE`: the reply after one that gave no answer gives none again | ✗ `ClosedOnEvidence` | the run tells part; the re-offer tells part again; the backstop closes it (10 steps). The note was in view both times (X1: 18/20 answer) |

`NoRedundantLookup` is a rate the model shows, not something the design guarantees.
gpt-oss re-looked-up with the note in view 1/20 in round 1. The design's claim is the
`ow_flaky_rest` row: a re-lookup costs the caller time and a second consult, but it never
loses or repeats an answer. (A second consult's own record is outside this model.)

`check.sh` gates the liveness rows too. TLC does not name the temporal property that
fell, so a liveness row's cfg checks exactly one `PROPERTY`, and the gate takes that one
as the name.

### A cut is judged, coverage is lexical (#204, 2026-10-20; coverage replaced by the gist, #210)

On 241 cuts of 12 live tellings (each cut after every word, labelled heard once every fact
that answers the question has been spoken), the lexical rule on the heard prefix closed
89 of 145 that were not heard: two content words of an item close it, so "a high around
seventy-five" closed the forecast before the low, and "the Orin Nano developer kit at two
hundred" the price (2026-10-19 23:56, the caller's "Oren" leaving "orin" a term). An LLM
judge shown the question, the heard prefix and the unheard rest closed 1 of 145 and kept
14 of 96 heard ones owed, at the trailing edge (gpt-oss-120b, majority of 10; p50 0.34 s,
p90 0.58 s). On whole tellings the lexical rule had no false owes and a whole-result judge
over-owed 9 of 16. So the split: **a reply played to the end closes by the lexical rule
(coverage); a cut closes only if its whole text covers the record by that rule and the
judge answers HEARD** -- the cheap check decides with no call when the reply was not a
telling of it. The sync record's "a cut after the answer was heard makes nothing" is the
judge's verdict too. No verdict (no full text, no pts, a timeout at `SETTLE_WAIT_S`, an
error, an unparsable reply) errs owed. `ow_prefixLexical`, `oa_prefixLexical`,
`ow_judgeWrong` and `ow_judgeOwes` above. The settle now waits for the verdict: the reply
stays pending, so the next render waits up to the call's latency (bounded by
`SETTLE_WAIT_S`, past which the readers go on and the verdict applies on the live book --
`OwedAnswers.tla`'s `LATE`).

### The gist close (#210, 2026-10-23)

On 2026-10-23 at 14:06 a room session's one-fact answer (a founding year, with the day
and the place) was told as "<year> is the year <the question's subject> was founded." and
played through. Coverage needs min(2, |terms|) of an item's terms, the request had taken
every word but the facts, and the year was one term of seven: the record stayed owed,
every request said "They have NOT heard this yet", and the model told it five times. The same
rule closes a forecast told without its low (#210's first case). The user chose the
**gist**: a record is heard once a reply gave the caller the answer to what they asked.

`tests/test_gist_judge_eval.py` measured it on 58 judged cases (every live
record/played-through pair of 10-18 and 10-22/23, the corpus relabelled, probes):
the lexical rule 2/25 false closes and 11/29 false owes; an LLM judge shown the
caller's words, the result and the reply 0/25 and 2-4/29 (majority of 10), p50 0.56 s.
Shown the model's own request instead it over-owes (9-12/29: "... and give its title and
URL"); shown no result it closes a wrong year. So:

- a reply **about** the record, played to its end, closes it on the gist judge's HEARD,
  or on **no verdict** (the user's choice: a repeat is worse than a detail lost, and the
  result stays in the context) -- `ow_gist_noVerdict` is that cost;
- the **backstop**: the second such reply closes it whatever the verdict, so a judge that
  answers OWED on a full telling (`PTOWES`) costs one re-telling, not a loop
  (`ow_gist_noBackstop`); its own cost is `ow_gist_twice`;
- both judges read **what was heard of the record so far** -- every noted reply about it,
  whole or the cut's prefix -- or a re-telling the caller cuts because they heard it
  already is OWED on its own prefix and told a third time (`ow_gist_oneReply`, the
  counterexample that added it);
- a cut keeps the cut judge; the gate in front of it (the reply's whole text gives the
  answer) is the gist judge, so its misses are `OWES` and `judgeWrong`.

`RetellOnce` is the new property: a heard answer is re-told at most once by a request that
carried the note.

The model has one record. With several owed, a run of ours renders only the records it
was asked for (`Owed.view`): a landing run whose note listed an older owed answer first
told that one in its place (2026-10-24 03:02). A caller's own turn carries every owed
record.

### What the model changed in the design

The model was written to lead the design, and it found these:

1. **An uncut run proves nothing about items it did not say** (`ow_asWritten_closed`).
   §3 closes on "a run that played to the end uncut". That is only sound if the run told
   every item. The run line asks for "one or two short spoken sentences", which a
   five-story list will not fit into, and §3 itself says naming one story does not close
   a five-story result. Fix (`RUNCLOSE = "terms"`): an uncut run closes only items with
   no terms (a bare "done"). Items with terms are closed by their terms, from a run as
   from any other reply.
2. **§4's re-offer arming leaves records owed with nothing further said**
   (`ow_asWritten_live`). "After an audible cut or a caller turn" misses a landing run
   dropped at the hold cap (room TV), a run that produced no audio (#70's guard, an LLM
   error after the first token), and, with fix 1, a run that played through with an
   item untold. In each case no cut and no turn ever comes. Fix (`REOFFER = "anyMiss"`):
   any run that ended without closing the record also arms the re-offer.
3. **The settle feed must be the reply's own end** (`ow_lagSettle`,
   `ow_lagSettle_repeat`). §6 lists "a ledger feed of settled replies". The ledger of
   `next_assistant`'s docstring charted a reply that played through only at the next bot
   activity (the 2026-09-11 14:23 loss), so the lull, the run and the render would read
   a stale record. Fable's lagging-settle trace is exactly this. The feed has to be the
   reply's own end: the interruption (ahead of the commit, by `CutReplyFence`), or the
   end of its own audio. Every reader waits for it. As built, both come from the
   ledger, whose playout design (`LedgerPlayout.tla`) charts at exactly those ends: the
   cut at `_interrupt`; a reply that played through once its synthesis is done and all
   of its audio has played (`_close_ready`), which is the window close its own last
   chunk played out in -- never a mid-reply `BotStoppedSpeakingFrame` (the transport's
   3 s fallback on a TTS stall) or a filler's end, which a first version that watched
   one TTS context at the gate took for the reply's (review round, `test_owed.py`).
4. **The ledger's heard text over-reports on a cut** (`ow_ledgerMax`). §3 says "the
   ledger's word-exact heard prefix". It is not word-exact. `transcript_ledger.py:902`
   keeps the **longer** of the pts prefix and `_prefix(intended, frac)`, an estimate that
   rounds to the nearest word and divides by `max(audio, words × 0.36 s)`. That bias is
   right for the corrector, which must never delete words that were heard, and wrong for
   closing a record. The close rule must read the pts-exact prefix only. A reply without
   per-word pts can close only by playing through.
5. **The run must be re-validated where it goes, not where it is asked** (`ow_askOnly`).
   Between `gate.ask` and the run reaching the LLM, a post-tool answer released from the
   same hold can go first, carry the note, and tell. §4 states only the landing-time
   condition. `owed.py` must be asked again at the gate's release (`RunGo`), and a run
   for a record that has closed is dropped there. It must not be rendered as a bare "tell
   them now" with no results.
6. **The run's window is both VAD-level and turn-level** (`ow_turnLevel`,
   `ow_vadLevel`). #198's branch gives the VAD half. The turn half (no turn open, no
   committed words unanswered, no tool round out: today's `_turn` and the claim) must
   still gate the run. Otherwise it goes between a sync tool call and its answer.
7. **§4's landing condition, made precise.** "A run is not started if any caller turn has
   opened since the consult was dispatched and nobody has answered it" becomes: skip the
   landing run if and only if a caller request is still to be **rendered** (their turn is
   open, or committed and not yet asked). That request will carry the note. A caller
   request already in flight was rendered without the note, so it does not stop the run,
   which waits behind it. A skipped landing run arms the re-offer.
8. **Fable's three model points hold.** The repeat side (`NoRunAfterHeard`, `repeat`)
   catches the lagging settle. A held-then-dropped run counts as an attempt (the
   `ow_runAfterCut` trace drops one), and the settle is its own step.
9. **A failure told on any path but an uncut run was re-offered** (`ow_failure_runOnly`,
   Fable's final review). A failure is one item with no terms, so finding 1 left only an
   uncut run to close it. When the landing run was skipped (a caller request still to be
   rendered) or cut after its last word, the caller heard it in full on another path,
   and the lull told it again. Fix (`RUNCLOSE = "about"`, `OUTCOME = "failure"`): a
   term-less item also closes on any noted reply heard to its end that is about the
   record (`owed._about`: a non-common word shared with the request). The premise, the
   corpus test's to check: a failure is one statement, told whole or not at all, and the
   telling is the reply. An unrelated reply shares nothing with the request and closes
   nothing.
10. **An adopted speculation's keys must reach the evidence** (`ow_adoptDropsKeys`). The
   speculation renders its note in its own task, and the adopting commit is the reply
   whose ending is the evidence: if it registers none of the keys, a full telling never
   closes the record, and the lull tells it again. The code read the keys off the wrong
   object (the speculation's wrapper, not its stream) until Fable's final review.

Not a finding, but a simplification the model allows: speculation's `take()` comparing a
records version (§2) is redundant here. `Land` rewrites the placeholder, which is a
context write the existing compare already sees, and the settle precedes every render
(finding 3). The version compare is cheap insurance, and only something that closes a
record **without** a context write between snapshot and commit would need it. §1's
eviction of a fourth record happens only at a landing, which writes the context, so it
is covered too; the compare was not built.

### Fairness

`FairSpec` makes weakly fair every step the brain or pipecat takes on its own: the LLM
answers what it is asked, audio plays out, the settle feed runs, a waiting run goes once
its window is open, and the release timer fires. The caller's VAD stop and commit are
fair too, since nobody talks forever and `MaxSpeech` bounds them anyway. `Lull` is fair
because it is time passing on a quiet line. These are **not** fair: the consult landing,
the caller starting to speak or opening a turn, the hold cap, and the session ending. So
`OwedResolves` holds without assuming the caller ever speaks, or that a session ever
ends.

### Known limits of this model

- **One record.** §1 renders at most the 3 newest owed records. Evicting an older one is
  not a close on evidence, so `owed.py` has a third terminal state, `dropped`, distinct
  from closed (no note, no run, its tool result kept); a model with several records
  would have `ClosedOnEvidence` and the liveness property name it. Two records
  interacting (one run telling both, a run for B while A's re-offer waits) are not
  modelled.
- **The lexical premise** (§3; the corpus test is its check): "every item's terms in the
  pts-exact heard text" is taken to mean the caller heard every item. Two partial
  tellings that together cover the items count as not heard. The rule errs the same way:
  towards owed, which costs a mention.
- **One request and reply at a time.** pipecat's LLM runs ahead of playout. The
  serialization is sound for runs, because a run's window waits for the bot to be quiet.
  Caller and post-tool requests follow a cut or a tool call with no audio, so no telling
  is still playing when they are rendered.
- **`WORDS_IN_TIME`** is inherited from `UserTurn.tla`'s gate: words later than the
  release timer are #198's accepted residual. The barge-in pause and the reply hold's
  timers are not modelled.
- **A cut before the TTS opened** (#79) is taken as heard-nothing. The feed reads a cut
  with no charted turn that way (`TranscriptLedger._interrupt` settles a reply still
  streaming or queued for its context as heard nothing), never "leave it as delivered".
- **Room sleep and wake** persistence (`wake_gate.py`) and the session end's rewrite of
  a running consult's placeholder are not modelled (`test_owed.py` covers both).
- **Durations.** The settle wait's bound (`SETTLE_WAIT_S`), the debounce before a run
  goes, the lull's length: timing the model leaves out ("What the code adds").
- **pipecat premises**, checked in `test_pipecat_premises.py`: the LLM answers an
  `LLMContextFrame` whatever its direction, so a run pushed up from the gate is answered
  (`check_an_upstream_context_frame_is_answered_by_the_llm`); the post-tool answer asks
  on the live context, so its one slot loses nothing (`check_the_answer_to_a_result_asks_on_the_live_context`);
  the TTS holds a text-less completion's End, which is why a silent run tells the gate
  itself (`check_a_text_less_completions_end_is_held_by_the_tts`).

## `OwedAnswers.tla` — how a sync tool's answer becomes owed (#204)

A consult's answer lands into a record (`Owed.land`). A sync answer tool's result
(`tools.Tool.answers`: web_search, web_fetch, search_memory, get_current_time, ...) is
answered in the reply after it, and becomes a record only when a telling of it is cut
before the caller heard it (#204: on 2026-10-18 the weather and toy-store answers were
cut at "Tomorrow in Austin looks" and "There's a Toy Joy store at", and the next turns
re-fetched or sat silent). This module is that filing alone (`Owed.noted`,
`Owed._resolve`, `Owed._cut_answer`), with two questions, an action tool's round,
chains, the late settle and the stream cut, on a reduced action set: no gate, no runs.

Four review rounds tried to read *which* reply tells an answer from its words (against
the result, then the reply's own terms, then whether it was about the question); each
reading missed tellings or took the wrong ones, and the rules piled up. The rule now is
position only:

- **The answer's first reply settles it.** The first reply rendered with a result, of
  whatever kind -- the post-tool reply, or the caller's turn that dropped it -- settles
  it if it calls nothing, plays audio and says something of its own (a term outside the
  question and the common words: "Sure, take your time." says nothing, settles nothing):
  heard in full, told; cut after its stream
  ended, a record of its whole text (unless the heard prefix already carries the
  answer), to the caller's words that started the round ("" when a notice or a cue
  did: nobody asked, no record).
- **One more reply, then history.** A first reply that did not settle it -- no audio,
  cut mid-stream, a line before another call, a reply with nothing of its own -- leaves
  it one more. Then it is history, as before #204; a new round (another question, an
  action), or a caller's turn in words of their own ("Never mind. What's the capital of
  Australia?"), ends an answer some render already carried at once. The result stays in
  the context either way.
- **The re-lookup, narrowly.** A round is an owed record's re-lookup -- no answer of its
  own -- only when it starts in the caller's turn right after that record's cut, before
  any reply played through, and the caller's words carry no terms of their own ("Hold
  on, hold on." and web_fetch, 2026-10-18 17:26) -- or continues such a round's chain (a
  re-lookup's reply that calls again passes it on). Every other round is its own
  question.

"Terms of its own" is one test, `terms_of(words, question)` empty or not (the close
rule's reading), used three times: a reply with none beyond the answer's question settles
nothing, a caller turn with some beyond an open answer's question moves on (restating
it -- "Wait, Austin tomorrow?" -- does not), a round started right after a cut with none
at all is the re-lookup.

A cut telling is read once more, by meaning: whether it **answers at all**
(`Owed._is_answer`, 2026-10-24). An apology and an offer -- "I can't open that page right
now. If you'd like, I can try another source." -- has terms of its own, and was owed as
the answer for 23 minutes: said again unasked, and looked up again for the caller's next
question. The gist judge, shown the telling's whole text as its own result, finds no
answer in it; the cut then spends a reply, as the model's `"defer"` does here, and makes
no record (`oa_fillerSettles` is that record). Measured on the session's 18 cut tellings:
0/42 trials took a non-answer for an answer, 7/84 an answer for none -- that error spends
a reply and leaves the answer one more, the `LOSE` side. No verdict makes the record.

| property | meaning |
|---|---|
| `ToldOnHearing` | an answer filed as told was heard in full |
| `RecordIsOwn` | a record's outcome is a whole telling of its own question |
| `NoRedundantLookup` | no answer the model had is looked up again (a cut telling with no record: B0) |
| `NoLaterRoundFiling` | a newer round's post-tool reply never files an older question |
| `OwedUnheard` | no record is owed for an answer the caller heard (made after they did) |
| `NoConfirmationOwed` | an action's confirmation is never owed |

`LOSE` is the caller moving on before the answer was settled: both replies unheard or
saying nothing of their own, a new round or a turn in words of their own over it, vague
words right after a cut that ask something new. The design rows assume it away; with it
(`oa_lose`), every property but `NoRedundantLookup` still holds -- the answer is lost to
history, never misfiled. `ASIDE` is the one case that misfiles: a caller turn in words
of their own drops the post-tool request (the answer is new at its render, so it is not
ended), and its reply answers the caller's words instead -- taken for the telling
(`oa_lose_aside`).

| row | what | verdict | the counterexample, or the state count |
|---|---|---|---|
| `oa_design` | the rule; vague consent ("Sure, go ahead.") to a new round while a record is owed is its own question; deferrals | holds, every property | 9,730 states |
| `oa_lose` | `LOSE` | holds every property but `NoRedundantLookup` | 41,213 states |
| `oa_lose_lookup` | the same | ✗ `NoRedundantLookup` | what remains of before #204: the answer the caller moved on from is history; a telling of it later, cut, leaves no record (11 steps) |
| `oa_lose_aside` | `LOSE`, `ASIDE` | ✗ `RecordIsOwn` | the result lands under the caller's own new question; their turn's reply answers that question and is cut: the record of the dropped answer is its text (6 steps) |
| `oa_fillerSettles` | a reply with no terms of its own settles (74c3509) | ✗ `RecordIsOwn` | "Sure, take your time." in the answer's position, cut: its record (6 steps: the fifth review's J1) |
| `oa_ownWordsKeep` | a caller turn in words of their own leaves an older answer open (74c3509) | ✗ `RecordIsOwn` | the post-tool reply unheard; "Never mind. What's the capital of Australia?" answered and cut: the weather's record (9 steps: J2) |
| `oa_streamCut_rest` | `STREAMCUT`: a telling cut mid-stream spends a reply | holds, all but `NoRedundantLookup` | 13,742 states |
| `oa_streamCut` | the same | ✗ `NoRedundantLookup` | the residual: both replies end unsettled (unheard, then cut mid-stream), no record, the B0 shape (12 steps) |

"Mid-stream" is read at the TTS's door (#211, 2026-10-24): the ledger takes a reply's
stream as ended when its `LLMFullResponseEndFrame` is pushed to the TTS
(`TranscriptLedger.on_push_frame`), not when the TTS reads it. The TTS reads its input in
order behind its synthesis, so a reply could be whole at its door a second before the TTS
reached its End: a one-sentence answer was cut at its first word 0.5 s after its End
was pushed, read as `STREAMCUT`, and the answer lost (7
`STREAMCUT` lines against 5 records on the box, 10-22..24). The residual is now a cut
while the model is still writing.
| `oa_late` | `LATE`: the bounded settle wait runs out; the settle re-reads the live book | holds, every property | 19,192 states |
| `oa_noCutRecord` | no record from a cut answer (before #204's extension) | ✗ `NoRedundantLookup` | the telling is cut; the next turn looks it up again (9 steps; W-goahead 20/20) |
| `oa_postOnly` | only the post-tool request carries the answer (5327336) | ✗ `NoRedundantLookup` | the caller's turn that dropped it tells it and is cut: no record (9 steps) |
| `oa_noSecondChance` | the first reply only | ✗ `NoRedundantLookup` | the post-tool reply is unheard; the caller's turn tells it, cut: no record (12 steps) |
| `oa_partialRecord` | a record from a telling cut mid-stream (5327336) | ✗ `RecordIsOwn` | its fragment is the record (5 steps) |
| `oa_callRecords` | a reply that calls again settles | ✗ `RecordIsOwn` | "Let me get their hours." + a call, cut: a record of the line (5 steps) |
| `oa_actionOwed` | an action's confirmation is owed (5327336) | ✗ `NoConfirmationOwed` | "Okay, I've turned the volume down" cut (5 steps) |
| `oa_keepOpen` | a new round leaves an older answer open (ab7dd29's lingering) | ✗ `NoLaterRoundFiling` | question 1's post-tool request is dropped by the caller's next question; question 2's post-tool reply files both (7 steps: the third review's cases A, C) |
| `oa_relookAll` | every round under the note is the record's re-lookup (ab7dd29) | ✗ `NoRedundantLookup` | a new question while a record is owed: its cut telling, no record (13 steps) |
| `oa_vagueConsent` | words with no terms are a re-lookup whenever a record is owed (9b602f8) | ✗ `NoRedundantLookup` | a record is owed; "Sure, go ahead." starts a new round, filed as a re-lookup; its cut telling, no record (13 steps: the fourth review's trace) |
| `oa_lateReplace` | the late settle makes its record whatever the book holds (c8e046a) | ✗ `OwedUnheard` | the caller's turn told it in full meanwhile (12 steps) |
| `oa_lateNoEvidence` | a reply rendered with the open answer is no evidence for the record made of it since | ✗ `OwedUnheard` | (12 steps) |
| `oa_prefixLexical` | a sync cut judged by the lexical rule on the heard prefix (a8fa2ba) | ✗ `ToldOnHearing` | the telling is cut after its content words, before the price; taken for heard, no record (5 steps: 2026-10-19 23:56, "the developer kit at two hundred") |

**What is accepted** (`test_owed.py` pins each). As before #204, no record: both replies
unheard, said nothing of their own, or cut mid-stream; a telling that also calls again
("There's a Toy Joy at 5501 Burnet. Let me get their hours." + web_fetch), cut; an answer
the caller moved on from in words of their own. Unlike before #204, misfiled: `ASIDE`
(above); a deferral that says something of its own ("Got it, I'll keep the Austin
forecast handy.") is taken for the telling when heard in full -- told (the lexical
reading: the model's "defer" is a reply with nothing of its own); cut, it is one only if
the answer gate finds no answer in it (above), else recorded as it stands; vague
words right after a cut that start a new question are taken for the re-lookup (one
turn). The re-lookup window opens only if no caller turn has rendered since the cut (a
late settle opens none); a re-lookup's chain is not modelled (`test_owed.py`).

Two lexical residuals of that test, kept as they are. **A short negative answer says
nothing of its own** ("No, it's closed tonight.", "No, it won't rain tomorrow."): "no"
is a common word -- it is the Spanish and Portuguese lists' (Portuguese "no" is "em o",
a function word), and the lists are one set -- while "yes" is a term, as the list's
charter says of polarity words. So a "yes" answer settles its answer and a bare "no"
answer spends a reply (heard, the answer is not told; cut, no record). Making "no" a term
would make a Portuguese function word one, the unsafe side for the close rule; making
"yes" common would leave a yes/no answer term-less. **The deferral fillers weaken a
contrived close**: "continue", "pause", "understand(ood)" are common words (they keep
"Understood -- let me know when you'd like me to continue." a deferral), so a consult
outcome like "No. The strike will continue through Friday." is left one term, "friday",
and any heard "Friday" closes it.

**What neither model covers.** A re-lookup round's own answer against the old record
(its telling is evidence for the record, never a record of its own; whether that
answer and the old one agree is the model's, not the book's). Two records under the
gate's timing (`Owed.tla` holds one; this module has no gate, runs or re-offers).
`STREAMCUT` with two answers in flight (here one reply is in flight at a time). The
`LATE` settle's re-offer counter (`Owed.tla`'s `OneReoffer` with one record).

## `SttSlice.tla` — sliced turn text: the words of one continuous transcript, by where they end

With `TEAPORT_STT_TURN_TEXT=slice` (stt.py, TURN TEXT) the engine decodes one stream per
session and a commit changes nothing in it: the engine answers it with a preview, and the
brain forms the turn itself. A turn is the main words that end before its position and
after the turn before, plus a word past the cut that its preview vouched for (THE CUT'S
MARGIN); it goes downstream at the preview as the words in hand plus the preview's text,
and is final once the engine's `end_through_ms` has passed its position -- while the
preview still vouches for a word, by the margin, and `audio_through_ms` too -- or once its
final has timed out. With `TEAPORT_STT_PREVIEW_AT_STOP` (THE PEEK, `PEEKS`) the preview is
asked at the VAD stop with nothing in flight: a commit that closes nothing. The caller
resuming drops it; the turn's close (a verdict, the ceiling, the hold's expiry) adopts it,
at its position, on its preview, with no second commit, taking back the open words its
preview vouched for. That is a new protocol over one ordered channel (deltas, previews,
progress), and the brain's half of it is `_take_turn`/`_turn_at`, `_peek_at_stop`,
`_drop_peek`, `_adopt_peek`, `_push_turn`, `_vouch`, `_slice_delta`, `_handle_preview` and
`_finalize_turns`. What a final then delivers is `TurnText.tla`'s.

The engine is modelled weaker than it promises (teaport-engine #108): a word's `end_ms`
runs from its start to `WLEN` steps past it, so a commit can fall inside a word; a word is
sent once the
decoder is past its `end_ms` or any time later -- but before the decoder is `D` past its
start, the premise of the final's timeout; both bounds are as high as the engine may put
them, or (`FREEZE`) held back, as 03e4223 froze the end bound through a pause; a preview
holds the unsent words begun before its commit, or (`LOSSY`) any of them, none when it is
cut short (`"complete": false`, which the brain takes as no preview); and a word that ends
after a commit may come before that commit's preview. The brain closes turns at any point
(when is SttCommit.tla's business); its VAD speaks and stops.

| DESIGN | | `RightTurn` | `FinalComplete` | `NoDropAtCut` | `NoLateInterim` | `NoSplitOnResume` | `NoStuckFinal` | `NoDoubleDeliver` |
|---|---|---|---|---|---|---|---|---|
| `shipped`, `PEEKS` FALSE | the preview at the verdict (`ss_shipped`; frozen bounds and lossy previews `ss_shipped_freeze`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `shipped`, `PEEKS` TRUE | the same, the preview at the VAD stop (`ss_peek`; `--full`: `ss_peek_full`, `ss_peek_freeze`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `arrival` | a turn is the words received by its preview | ✗ | | | | | | |
| `endMargin` | every word ending less than the margin past the cut is the turn's | ✗ | | | | | | |
| `decoded` | final once `decoded_through_ms` passes the commit | | ✗ | | | | | |
| `noStartWait` | vouched by start, final on the end bound alone | | ✗ | | | | | |
| `noVouch` | sliced by `end_ms` alone (before the 2026-10-27 review) | | | ✗ | | | | |
| `endOnly` | vouched only if it ends less than the margin past the cut (as first written) | | | ✗ | | | | |
| `interimAll` | every delta announced as an interim | | | | ✗ | | | |
| `peekAsTurn` | the commit at the stop closes the turn there | | | | | ✗ | | |
| `noDrop` | the caller resuming does not drop the peek | | | | | ✗ | | |
| `noTimeout` | a final waits for the bound however long | | | | | | ✗ | |
| `noTrim` | a preview's words an earlier turn's preview still vouches for are asked again | | | | | | | ✗ |
| `noOrder` | a turn after one that put nothing with words on goes on at its preview | | | | | | | ✗ |
| `noPeekOwed` | a peek adopted after the turn that owed words settled goes on at its preview | | | | | | | ✗ |

`NoWordTwice` (no word in two places) is checked with the shipped rows. `NoDoubleDeliver`
counts what each turn delivers downstream -- its provisional, or, when it waits on its
final (deferred) or its provisional had no words, its final's words -- and no word may be
delivered by two turns. A commit soon after another (the backstop's, then the VAD stop's,
half a second apart) is answered with the turn before's words the main stream has not sent:
`noTrim` asks a word the turn before vouched for in both (seen twice in 301 turns on
377508d, before `_past_earlier`); `noOrder` lets a turn go on at its preview while the turn
before, which put nothing with words on, still has its final to come, whose words then go
on again, after it (the re-review's S2); `noPeekOwed` adopts a peek whose preview was
taken while a turn owed words, after that turn settled (`ss_peek_freeze` found it; the
peek now remembers, `_Peek.owed`). The code trims an earlier turn's owed words from the
preview's front run (as many words as are owed, and two more): each word there that is
an owed word still ahead of the pointer is dropped, and the pointer moves past it, so an
owed word the later preview lacks is skipped. With words unique and in time order that
is the model's set subtraction (`Owed`) over the front run; the model's three words never
pass the run's bound, which is not modelled.

- **`RightTurn`** — a turn holds only words of its stretch: none ending before the turn
  before it closed, and none past its own close unless the word began before it -- never
  the caller's next word. `arrival` fails it the way the commit-and-flush days never could
  (the caller's last word is decoded ~0.5 s after the commit, and by arrival it is the next
  turn's first); `endMargin` takes the caller's next word back.
- **`FinalComplete`** — a turn made final has every word it will ever be given: none
  ending before it, and none its preview vouches for, can still come. `decoded` fails it
  (`decoded_through_ms` passes a word's end before the engine sends the word), and so does
  `noStartWait` (a word the cut falls inside can end far past it: only the start bound says
  it has come).
- **`NoDropAtCut`** — a word the provisional had that began in the turn's stretch is in
  its final: an end estimate past the cut, or a cut inside a word (the backstop, a stop over
  the bot), never moves the turn's own word to the next turn -- where the backstop would
  commit it as a turn of its own. `noVouch` is the design before the review; `endOnly` the
  first fix, which vouched by `end_ms` alone and missed a word the cut falls inside.
- **`NoLateInterim`** — a closed turn's word is never announced as an interim: it would
  start a user turn and interrupt the reply to its own turn. A peek's words before its
  adoption are interims, as any open turn's are: nothing is closed on a peek.
- **`NoSplitOnResume`** — no turn is closed at a stop the caller then talked past before
  its verdict. `peekAsTurn` closes it at the stop; `noDrop` adopts a peek the caller talked
  past. (Before the review this property held vacuously for the shipped peek: nothing in
  it could set the ghost. It now records the adoption of a talked-past peek, which `noDrop`
  shows the shipped code avoids only by dropping the peek.)
- **`NoStuckFinal`** — no turn waits on its final past the timeout, whatever the bounds
  did. `noTimeout` fails it on a frozen bound.
- Witnesses: the shipped design reaches a final with words that came after its preview
  (`ss_shipped_late`), an empty provisional whose final has words (`ss_shipped_empty`), a
  word the cut falls inside taken by its turn (`ss_shipped_across`), a final settled by its
  timeout (`ss_shipped_timeout`), a turn closed on a peek (`ss_peek_adopted`) and a peek
  the caller talked past (`ss_peek_dropped`).

Run (TLC 2.19, six cores): `ss_shipped` ~2.2M distinct states (two words, audio to 4, a
word's end up to two steps past its start), ~40 s; `ss_peek` ~1.9M (two words, audio to 3),
~20 s; `ss_shipped_freeze` ~9.4M (the bounds frozen or moving, previews lossy), ~1-3 min; the
witnesses and rejected rows under 1.2M each. `--full` adds three words: `ss_shipped_full`
~20M (audio to 5), `ss_peek_full` ~16M (audio to 4), and `ss_peek_freeze` ~81M (two words, audio to 4, frozen bounds and lossy previews: ~18 min
on five cores).

### Known limits of this model

- Text is sets of word ids. A preview that has a different word than the main stream (a
  preview's "light" where the main stream has "lighthouse") is not expressible; the brain matches by word text
  (the main word's first piece a prefix of the preview's), unit-tested in
  `test_stt_turn_text.py`. Pieces are not modelled: a word arrives whole, as its pieces do
  (the engine sends a word's pieces together, each with the word's positions); that its
  later pieces follow it is unit-tested.
- The margin is one step and `end_ms` the only estimate: a start estimate's error is not
  modelled (the code's `end_ms < P + 200` clause covers a start stamped at or past P).
- The final's timeout is modelled on `decoded_through_ms`; its wall-clock half (for an
  engine that stops reporting) and the timeout's settle as the provisional are the code's,
  unit-tested. The code also waits the margin longer while a preview still vouches (a
  vouched word's start estimate may be up to the margin past the cut; the model's starts
  are exact). What a timed-out or deferred turn delivers is TurnText.tla's.
- A cut-short preview is a `LOSSY` preview with no words here; the code also makes its
  turn wait on its final, which is what an empty provisional's `Delivered` counts.
- The peek waits for nothing in flight; the stop that finds a commit in flight peeks when
  its answer lands (`_peek_wanted`), which the model leaves out (that stop's turn closes
  with a commit of its own, as `PEEKS` FALSE does). `_adopt_peek` settles at once when it
  can, as modelled; a peek or a commit needs audio sent since the last turn (`pos >
  LastAt`), as a VAD stop does.
- The speculation on the peek's preview (endpointing.LateStartTurnStopStrategy, the
  Speculator) is not modelled here: UserTurn.tla's SPEC rows hold for any text it asks on,
  and `test_turn_text_peek.py` drives it over a real pipeline; who stands it down (taken
  words, a run of ours) is unit-tested in `test_turn_text_taken.py`.
- A reconnect (the turns not yet final are dropped, their provisional standing) is not
  modelled.
- The bounds are trusted. A word below a settled turn's commit (the engine breaking a
  bound, or a final that timed out) goes to the open turn with a warning; the model's
  engine sends none.

## `TurnText.tla` — a sliced turn's final, from the STT to the context

What the STT delivers when a turn's final is in (`_push_turn`, `_settle_turn`), through
the processor that may hold the STT's frames above the aggregator (run control's claim,
the barge-in judge), the user aggregator, and `turn_text.TurnTextCorrector` below it. One
turn; its provisional and final words are any two sets (which words they are is
SttSlice.tla's). The STT pushes the provisional at the preview -- the words (stamped with
the stop), a wordless close, or nothing, the turn closing on its final, when the preview
was cut short or had none of the caller's words over the bot's reply -- and the wake gate
or echo guard may have taken it (asleep: nothing went on; echo: the words were the bot's;
woke on a phrase the final lacks). At the final it delivers the deferred close, nothing
(as asked, or a branch that leaves it as it went), the final's words as new words where
nothing with words went on, or a correction. The holder, while a consult runs or the reply
is paused, holds the first final with words and every STT frame after it, then decides
once -- take it, judge it a backchannel, let it go -- and the state it decided under is
over. The aggregator adds words as a part and commits on its own task at any time.

| DESIGN | | `NoLostWords` | `BargeStamped` | `DecidedOnce` | `CorrectionApplied` | `BargePaused` |
|---|---|---|---|---|---|---|
| `shipped` | as above | ✓ | ✓ | ✓ | ✓ | ✓ |
| `noNewWords` | an empty provisional's final words dropped | ✗ | | | | |
| `emptyCloses` | over the bot an empty preview closes the turn wordless | | ✗ | | | |
| `echoCloses` | over the bot only an empty provisional waits, not the bot's own words | | ✗ | | | |
| `repush` | a correction placed nowhere pushed back as new words (before the review) | | | ✗ | | |
| `overtake` | the holder holds the words, not their correction (before the review) | | | | ✗ | |
| `byFrame` | commits recorded when their context frame passes the corrector (before the review) | | | | ✗ | |
| `noHold` | the pause may end while a barge-in turn waits on its final (before the re-review) | | | | | ✗ |

`NoDuplicate` (the turn's words in one part or one message) is checked with `shipped`.

- **`NoLostWords`** — every word of the final goes downstream (provisional, the close on
  the final, the correction, or new words) unless a branch leaves it by design: the gate
  slept, the words were the bot's own, the final lacks the phrase that woke the gate.
- **`BargeStamped`** — a barge-in's words go downstream stamped with its stop, never as
  new words decided after the pause: `emptyCloses` is the design before the review,
  `echoCloses` the first fix, which this model found (a provisional that is only the bot's
  reply heard back closed the turn wordless).
- **`DecidedOnce`** — words the holder took or judged a backchannel never reach the model:
  `repush` decided them again under another state.
- **`CorrectionApplied`** — a correction sent for words that went on is applied: once all
  is quiet, the turn's words downstream are the final's. `overtake` lets it pass the
  holder ahead of the words it corrects; `byFrame` misses a commit the aggregator made on
  its own task while the correction was on its way.
- **`BargePaused`** — a barge-in's words reach the judging holder while the reply's pause
  holds. The pause ends on its own once its window is out and no final is on its way; a
  barge-in turn that waits on its final holds it (`stt.barge_hold`), and `noHold`, where it
  does not, delivers a lone "Stop." after the reply resumed -- to the unpaused two-word
  guard (the re-review's S1). The window is assumed to outlast the preview.

Run: each row is a few thousand states, ~1 s.

### Known limits of this model

- One turn, and the holder decides on its first final with words: two turns' corrections
  crossing, and the reply hold's merge (the newest-message fallback), are unit-tested
  (`test_turn_text_correction.py`), not modelled. A commit's record is atomic with its
  write; the code's handler runs as its own task, which the newest-message fallback covers.
- The wake gate asleep decides an empty provisional's re-pushed words as excused (it may
  wake on them, unit-tested); the echo guard's strip is abstracted as "the provisional's
  words were the bot's".
- The speculation the taken words stand down is not modelled (`test_turn_text_taken.py`).

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
  `disconnected` for the new call cancels the wait like any bring-up. It waits for the
  teardown tasks only — what `DrainWait` waits for. A declined call's busy line
  (`answer_busy`, for a call the gateway answered anyway) holds no claim and is kept in
  `busy_lines`, apart: it first sat in `teardowns`, and the next caller's greeting waited
  out the whole busy line (~3 s, review of #175). Its caller's `disconnected` now stops it
  (`stop_busy_line`), as `answer_busy`'s docstring always said, rather than its audio
  going on under the next call.

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
`sip_server` connection loop, `CallClaim` and `SessionArbiter`. Both hold the teardown's
0.3 s settle on an event (`_SettleGate`) until the next call has got as far as it can, so
the interleaving is forced, not raced: each fails on the code before the fix on every run.
`test_a_busy_line_does_not_keep_the_next_caller_from_their_greeting` is the busy line's.

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
  the declined call answered anyway (`answer_busy` — a task of its own that holds no
  claim, which no bring-up waits for; its caller's `disconnected` stops it), DTMF and
  audio routing.
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
`finally`, after the `InterruptionFrame` as the ledger sees it, and the ledger of that
time took the partial text as a new `_pending_gen` -- or the ledger never sights it
(`LlmCancelLost`): it reads the stream where the TTS processes it, and the End reaches
the TTS ahead of the interruption, a queued frame the reset may flush.

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
cancelled completion's `finally` pushes the End frame after the interruption (as the ledger, an observer acting on first sighting, sees it), the partial
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
  context that never yields audio (`UnspeakableOpens`). pipecat closes a context at its
  End only if its last `run_tts` yielded audio (`on_turn_context_completed`, on
  `tts_process_generator`'s flag); `EngineTTSService` closes every one at its End
  (`CloseAtEnd`, below), so only a context the LLM leaves open mid-reply ends on the
  stop-frame timeout (`TTS_STOP_FRAME_TIMEOUT_S`, 15 s).
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
  rebuild of each chunk it played is recognised and ignored. The thinking sound's
  typing is no TTS audio at all (a plain `OutputAudioRawFrame`, never bot speech), so
  the ledger never charts it. The untagged legacy path is out of scope.

The ledger, in the model's terms: turns open only on the TTS's own frames, never on
`BotStartedSpeaking`; a turn claims the oldest expected context (the queue of ended
responses — every one with text, synthesizable or not), else the response streaming now;
several turns may be open at once — a reply queued behind another at the transport; a
turn's `audio_start` is its own chunk's place in the transport's queue, laid out from the
window's start; a window closing credits the queued chunks that have played by then and
keeps the rest queued, the window open for them; it charts, oldest first, the turns whose
audio has all played and whose synthesis is over; a context starting proves every OTHER
open context ended, since the TTS drains one at a time; a context's stop frame ends its
turn's synthesis; the drain re-push confirms a turn's response or corrects it; an
interruption charts every open turn with its own played portion. The layout's seconds are
taken to be right — at a window close or a cut the ledger credits exactly what the
transport has played; `PlayoutPause.tla` checks that arithmetic, `test_ledger_playout.py`
the rest on the real ledger.

| row | property | LedgerPlayout |
|---|---|---|
| `lp_phantom` | `NoPhantomFullHeard` — a reply charted complete had every chunk PLAYED. Strict: `Ledger.tla`'s allowed "queued at the transport", the residual noted under its limits | ✓ (3.5k states) |
| `lp_unheard` | `NoUnheardWhenPlayed` — read live since the 2026-10-14 audit: a reply charted heard-nothing never plays, before the chart or after it | ✓ |
| `lp_once` | `ChartedAtMostOnce` | ✓ |
| `lp_wrongText` | `ChartedTextMatchesContext`, two replies | ✓ (300k states, ~20 s) |
| `lp_premature` | `NoPrematureFullChart`, with stalls and timeouts | ✓ |
| `lp_playedCharted` | `PlayedIsCharted` — audio the transport played is charted once its turn is over; nothing played vanishes | ✓ |
| `lp_fillerSet` | `FillerCtxRemembered` | ✓ |
| `lp_all` | every property above, `NoUnheardAheadOfPlayed`, `NoPlayedReplyDeleted`, `UnheardSaidIsCorrected`, `CutAnchorsOwn`, `HeardPrefixOnOwn` and `NoSweepAfterEnd`, two replies and a filler | ✓ (405k, ~25 s) |
| `lp_resume` | `NoPrematureFullChart` under `Resume` | ✗ (8) — below |
| `lp_unspeakable` | every property, `Unspeakable = {r1}`, **as shipped** (`SpeakCheck = "chart"`, `Corrector = "seq"`): every response with text queued; one with no audio and nothing synthesizable charted nothing; its committed text never taken for another reply's | ✓ (83k) |
| `lp_wrongText_unspeakable` | `ChartedTextMatchesContext`, the first cut's premise (`UnspeakableOpens = FALSE`) without `has_speech` (`SpeakCheck = "none"`) | ✗ (6) — below |
| `lp_wrongText_unspeakable_checked` | the same premise, `has_speech` at the queue (`"queue"`): why it shipped | ✓ |
| `lp_wrongText_unspeakable_opens` | `ChartedTextMatchesContext`, **before this fix** (`"queue"`), with the context opening as pipecat opens it | ✗ (6) — below |
| `lp_unheard_unspeakable_opens` | `NoUnheardWhenPlayed`, the same | ✗ (12) — below |
| `lp_unspeakable_charted` | every property, the first proposal: queued and charted (`"none"`) | ✗ (12) `NoUnheardAheadOfPlayed` — below |
| `lp_unspeakable_noOpen` | `ChartedTextMatchesContext`, the shipped ledger on the first cut's premise: the fix rests on pipecat opening the context | ✗ (7) |
| `lp_endings` | `NoSweepAfterEnd`, `TrailingMute = {r1}` ("Great!!"), pipecat's default close (`CloseAtEnd = FALSE`): **before this fix** | ✗ (6) — below |
| `lp_endings_closed` | every property and `NoSweepAfterEnd`, a trailing-mute reply and an unspeakable one, **as shipped** (`CloseAtEnd`) | ✓ (83k, ~7 s) |
| `lp_failed_charted` | `NoUnheardAheadOfPlayed`, `Failed = {r1}`: a reply the engine fails on every clause | ✗ (12) — below |
| `lp_failed_position` | `NoPlayedReplyDeleted`, `Failed = {r1}`, no barge-in, the corrector **before this fix** (`Corrector = "position"`) | ✗ (13) — below |
| `lp_position` | `NoPlayedReplyDeleted`, no failure, the corrector **before this fix**: a barge-in before a queued reply's first audio | ✗ (11) — below |
| `lp_guarded` | `HeardPrefixOnOwn`, the corrector of the first fix (`"guarded"`: still the newest message, cut by cut) | ✗ (14) — below |
| `lp_unspeakable_tail` | `HeardPrefixOnOwn`, `Unspeakable = {r1}`, the `"seq"` corrector before the emoji fix (`"seqTail"`: with no newer reply, the newest message, speech or not) | ✗ (13) — below |
| `lp_failed` | every property but `NoUnheardAheadOfPlayed`, with the corrector's three, `Failed = {r1}`, **as shipped** (`Corrector = "seq"`) | ✓ (85k, ~10 s) |
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
for punctuation it strips). Fixed, below.

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

### What was changed (2026-10-15)

`has_speech` moves from the queue to the chart (`SpeakCheck = "chart"`). `_llm_side` queues
every response with text, and every non-filler spoken notice, as pre-#13 — pipecat opens a
context for any non-blank text, so each context claims its own entry and the queue stays
in step. `_finish` charts nothing for a turn with no audio whose text has nothing
synthesizable, unless a barge-in cut its response while it still streamed (`Silent` in
the model): nothing of it was heard. (pipecat does commit its text — the TTS
force-completes a context's text at its stop — which the section on failed replies below
found; an emoji left in the context is harmless.)

Queuing alone — the first proposal, `lp_unspeakable_queued` before this change, now
`lp_unspeakable_charted` — held every property then checked, but charts the unspeakable
reply heard-nothing, and `HeardContextCorrector` anchors a cut by position: the newest
assistant message since its last reconcile. The reply that played after the unspeakable
one committed the newer message, so the cut lands on it and deletes it — the same lost answer
by another route. Checked on the real corrector with the ledger's events (`"Great!!"`,
`"👍"` cut at 0%, the answer heard in full): it removed the answer. `NoUnheardAheadOfPlayed`
is that requirement as an invariant (a reply charted heard-nothing is not followed in the
chart by one that played with no user turn between them; the model's user turns are its
barge-ins, `epoch` in the chart). `lp_unspeakable_charted`'s counterexample, 12 states:

```
LlmStart(r1) TtsStart(r1) r1 ("👍") streams; its context opens and claims it live
TtsStop(r1)               nothing for the stop-frame timeout: charted heard-nothing
LlmEnd(r1) LlmStart(r2) TtsStart(r2) TtsAudio(r2) ... PlayChunk
Interrupt                 r2 cut partway, charted with r1 in one reconcile window
```

The barge-in exception keeps a cut of a still-streaming response charted, as before:
what it would have said is unknown, and the consult follow-up loop reads that cut to
decide whether to say its answer again. Nothing charted after a barge-in shares its
window: it comes after the user's turn.

Removing `has_speech` from the queue does not reopen `lp_wrongText_unspeakable`: that
counterexample needs the unspeakable response to open no context, and pipecat 1.8.1
opens one for any text with a non-blank character (`_push_tts_frames`; the brain's TTS
has no text filters or skipped aggregation types that could empty it first).
`lp_unspeakable_noOpen` keeps the dependence visible — the shipped ledger on the old
premise fails the way `lp_wrongText_unspeakable` did — and
`test_tts_stop_frame.py`'s `test_an_unspeakable_reply_opens_a_context_and_the_next_reply_keeps_its_text`
pins the premise on the real `EngineTTSService`, so a pipecat upgrade that stopped
opening the context fails a test rather than the transcript. Regressions:
`test_ledger_playout.py` case k (the probe's frame order, through the ledger and the
corrector) and that test; both fail on the code before this change.

The trailing-`"!!"` hold is not the ledger's: `"Great!!"` has speech and was always
queued. It is the next section.

### A reply ending in `"!!"` held the TTS for 15 s (2026-10-15)

pipecat closes a context at its End only if the *last* `run_tts` yielded audio
(`on_turn_context_completed` reads the flag `tts_process_generator` sets from that one
call: a service that yields none may be streaming on a socket of its own). The sentence
aggregator hands a reply's trailing piece to `run_tts` on its own — `"Great!!"` reaches it
as `"Great!"` and `"!"`, `"Really?!"` as `"Really?"` and `"!"`, `"Bye! 👋"` as `"Bye!"`
and `"👋"` — and the engine has nothing to synthesize in it. So the context, and the TTS's
queue behind it, stayed open for the full `TTS_STOP_FRAME_TIMEOUT_S`: bot-speaking ended
on the transport's 3 s idle fallback instead of the stop frame, and whatever the TTS said
next — the answer after a tool call, a narrator line — waited up to 15 s. A reply with
nothing synthesizable at all (`"👍"`), and one whose every clause the engine fails, the
same. Measured on the real TTS with the timeout at 1 s: the stop frame 1.00 s after the
last audio for those endings, 0.00 s for `"That is all."`.

Modelled as a property of the environment the ledger reads, not of the ledger:
`TrailingMute` (replies with audio whose last piece has none), `MutedAtEnd` (those and
`Unspeakable`), and `swept` — contexts the timeout closed after their End reached the TTS.
`NoSweepAfterEnd` says none is. `lp_endings`, 6 states:

```
LlmStart(r1) TtsStart(r1)  "Great!!" streams; its context opens
LlmEnd(r1)                 the End reaches the TTS: the last piece, "!", yielded no audio,
                           so the End does not close the context
TtsAudio(r1) TtsStop(r1)   "Great!" plays; the stop frame comes on the timeout, 15 s on
```

**What was changed.** `EngineTTSService.tts_process_generator` runs pipecat's and then
sets the flag: this service yields every frame of a `run_tts` synchronously, so a call with
no audio is one with none to give, and the End closes the context whatever the last piece
was (`CloseAtEnd = TRUE`). `_require_close_at_end_flag` fails the session build if a
pipecat bump stops reading that flag in `on_turn_context_completed`. Dropping the
unspeakable piece before `run_tts` (a text filter) was the other way in, and is wrong
here: a reply with nothing synthesizable at all would then open no context, which is the
premise `lp_unspeakable_noOpen` shows the ledger cannot take. `lp_endings_closed` holds
with every property — the ledger's charts do not depend on when the stop comes, only that
it does. Regression: `test_tts_stop_frame.py`'s
`test_a_reply_whose_last_piece_is_unspeakable_closes_at_its_end` (real
`EngineTTSService`, timeout 2 s: `"Great!!"`, `"Really?!"`, `"Bye! 👋"`, `"👍"`, each
followed by a reply), which fails before it (`'Great!!': its context closed 2.00s after
its last audio`).

### A failed reply's cut deleted the reply that played after it (2026-10-15)

A reply the engine fails on every clause (CUDA OOM, an engine restart) opens a context,
yields no audio, and is charted heard 0 by design (`test_ledger_playout.py` case c): it has
speech, so it is a cut, and `HeardContextCorrector` removes it. But pipecat commits its
text all the same — at the stop the TTS force-completes the context's remaining text
(`_apply_force_complete`) as a word frame, which the assistant aggregator appends;
measured on the real `EngineTTSService`: `TTSStopped`, then `TTSText 'Hello there my
friend.' append_to_context=True`, then the End (an emoji the same, `'👍'`). The corrector
anchors a cut on the newest assistant message of its window, so with a reply that played
after the failed one and no user turn between — the answer after a tool call, the next
response — the failed reply's cut deleted THAT one: an answer the caller heard, while the
text they never heard stayed. `Failed` (replies with speech that yield no audio) reaches
`NoUnheardAheadOfPlayed` (`lp_failed_charted`, 12 states).

Modelling the corrector found a second way in. `Chart` records `said` — pipecat committed
a message: words of it played, or its context ended — and `Anchor(i)` is the newest
committed message of the cut's window, newest by the order the contexts opened, which is
the order pipecat commits them (not always the charts' order: a failed reply is charted at
its stop while the reply ahead of it still plays). `NoPlayedReplyDeleted`: the corrector
never rewrites or deletes ANOTHER reply that played. `lp_failed_position`, 13 states:

```
LlmStart(r1) TtsStart(r1) TtsStop(r1)   r1 fails on every clause: charted heard 0, committed
LlmEnd(r1) LlmStart(r2) TtsStart(r2) PlayMark TtsAudio(r2) PlayStart TtsStop(r2)
PlayChunk PlayMark
                                        r2 plays in full, committed after r1: the newest
                                        message -- the corrector's anchor for r1's cut
```

**The premise under `said`.** The model takes `said` as the truth, and the code computes it:
`Utterance.committed = not interrupted or never or synth_done or heard > 0`. It holds
because each arm names a context whose end force-completes the reply's text
(`tts_service._apply_force_complete` runs in `_handle_audio_context` before the stop frame
is pushed, whether the context ends at its End or on the timeout), or words of it that
reached the aggregator:

- `never` — the ledger's three no-audio closes: its own `TTSStoppedFrame` (the stop was
  pushed after the force-complete), its End re-pushed below the TTS (pushed by
  `_maybe_reset_word_timestamps`, after the same force-complete), and another context
  starting (`_context_started`: pipecat drains contexts one at a time, so the earlier
  one's `_handle_audio_context`, force-complete included, has finished).
- `synth_done` — set by the same three sightings for a turn with audio.
- `heard > 0` — words are pushed on their pts, ahead of their audio by the caption lead,
  so some of a reply that played have reached the assistant aggregator, which commits
  them at the interruption.
- `not interrupted` — charted complete: its stop and drain were seen.
- A spoken notice (`TTSSpeakFrame`, `append_to_context`) opens a context of its own
  through `_push_tts_frames`, and is committed by the same force-complete at its stop.

Only a reply cut before its context ended, with nothing played, falls through all of
them — and pipecat committed nothing of it. Checked by review against every `_finish`
caller; no counterexample. A pipecat bump that commits on another frame, or stops
force-completing, breaks it.

`lp_position` (11 states) needs no failure: a reply plays, the next one's context opens,
and a barge-in lands before its first audio. That reply committed nothing (its context had
not ended), so the newest message is the one the caller heard, and the corrector deleted
it. A tool message between the two stops the walk, which may be why it has not been seen.

**The first fix, and what it left.** `_anchor_elsewhere` let a cut that heard nothing
be when its message could not be the newest (`Corrector = "guarded"`): a reply newer than
it in its window was heard, or an older one was and it committed nothing. It held
`NoPlayedReplyDeleted`, but every cut it did reconcile was still anchored on the newest
message left, cut by cut in the batch's order — and a reply cut partway with a reply
committed after it lost its correction. `HeardPrefixOnOwn` (a cut that heard something
has its heard prefix written on its OWN message) is that; `lp_guarded`, 14 states:

```
LlmStart(r1) TtsStart(r1) TtsAudio(r1) TtsAudio(r1) TtsStop(r1) LlmEnd(r1)
LlmStart(r2) TtsStart(r2) TtsAudio(r2) TtsStop(r2)
                          r2 synthesized whole behind r1: committed (force-completed)
PlayStart PlayChunk       r1's first chunk plays
Interrupt                 r1 cut partway, r2 heard nothing: r1's prefix lands on r2's
                          message (the newest), r2's cut then removes it, and r1 keeps
                          its FULL text -- the context says the caller heard all of it
```

The model's first run of this (with anchors taken all at once rather than cut by cut)
showed the same with a failed reply in r2's place; the real corrector, cut by cut, gets
that one right — the failed reply is charted first, at its stop, and its removal leaves
r1's message the newest. The queued reply is charted after r1, at the same barge-in.

**What was changed.** `HeardContextCorrector._resolve` finds each cut's OWN message,
all of them before any is rewritten: pipecat commits the replies' messages in the order
their contexts opened, so the cut's is as many spoken messages back from the newest as
its batch has committed replies newer than it. The ledger puts what that needs on each
`Utterance`: `turn_seq` (the order its context opened) and `committed`
(`not interrupted or never or synth_done or heard > 0`). A cut that committed nothing has
nothing to correct. Messages with nothing synthesizable (an emoji reply, charted nothing)
are not counted; a message carrying a tool call is cleared rather than removed, so the
tool result keeps its call. `Target` is that rule (`Corrector = "seq"`). `lp_failed`
holds with every property but `NoUnheardAheadOfPlayed` — the charts are unchanged; the
corrector no longer reads them by position alone — and with the corrector's three:
`NoPlayedReplyDeleted`, `CutAnchorsOwn` (every cut it reconciles, on its own message) and
`UnheardSaidIsCorrected` (every cut heard nothing that committed a message is removed: the
failed reply ahead of one that played too, which the first fix had to leave). Charting a
failed reply nothing, as `Silent` does an unspeakable one, was another way, and is worse:
its text is in the context, and the corrector removes it. Regression:
`test_ledger_playout.py` cases l (a failed reply then a played one; a played one then a
failed one charted first; a played one then a barge-in before the next one's audio) and m
(a reply cut partway with a queued reply, and with a failed one, after it), through the
real ledger and corrector. Before the first fix, l's first and third fail (`the reply the
caller heard was deleted`); before this one, l's first (the failed reply's text kept) and
m (`['The bakery opens at eight and closes at six.']`: the cut reply's full text).

**An emoji reply after the cut one (review, 2026-10-15).** The model's messages were the
charts' alone, and an unspeakable reply is charted nothing — but committed: its context
closes at its End (promptly since `lp_endings`' fix) and the TTS force-completes its text.
Behind a reply the caller then barges into, it is the newest message, and `_resolve`, with
no committed reply newer than the cut, took the newest message as it found it: the emoji's,
overwritten with the cut reply's heard prefix, while the cut reply kept its full text.
Reproduced on the real ledger and corrector (`['The bakery opens at eight and closes at
six.', 'The bakery opens at']`). `Muted` puts those messages in the model, with
`stopEpoch` (the window a context's stop falls in); `lp_unspeakable_tail` (`"seqTail"`, the
code before this fix), 13 states:

```
LlmStart(r2) TtsStart(r2) TtsAudio(r2) TtsAudio(r2) TtsStop(r2) LlmEnd(r2)
LlmStart(r1) TtsStart(r1) TtsStop(r1)   r1 ("👍"): charted nothing, committed after r2
PlayStart PlayChunk Interrupt           r2 cut partway: its prefix lands on r1's message
```

The walk back from the newest now skips an assistant message with text and nothing
synthesizable, as the count of newer replies already did — one predicate for both,
`_is_reply` (the ledger's own `has_speech`). A tool call riding on the emoji does not make
it a reply: the first cut of this fix kept such a message as a stop while the count skipped
it, so with emoji text on a follow-up's tool call the walk stopped there and its text took
the cut reply's prefix (review of the integrated stack). A message with no text at all — a
bare tool call — still stops the walk, for the tool-result insert path. Tool calls are not
in the model: to it both are `Muted` messages, which the corrector never counts or takes.
`lp_unspeakable` (`"seq"`) holds with the corrector's three; regression:
`test_ledger_playout.py` case n (an emoji reply, and the same with a tool call), which
fails on the code before each fix with the review's output.

### What the model found

Writing the drain step found a hole in the code as first written: the re-push for a
response whose turn was ALREADY charted (closed on other evidence — a newer context, or
the hermetic fallback) reassigned the newest open turn to that response. Fixed before
the tests ran: a charted turn's re-push is a no-op, and only an UNCLAIMED response's drain
corrects the newest turn (`_end_drained`). The 2026-10-14 audit found the unspeakable
reply's misclaim above, and its fix found the corrector's positional anchor
(`NoUnheardAheadOfPlayed`).

### Known limits of this model

- **`lp_resume`, the accepted residual.** 8 states now: the LLM streams, one chunk, then
  nothing reaches the context for the stop-frame timeout — its stop frame says
  "synthesized", the window closes on it, and the reply is charted complete while the
  LLM is still streaming; its next sentence re-creates the context under the same id
  and plays under an anonymous turn, charted nowhere. The first cut needed a narrator
  line in the gap; the stop frame does it alone. Still a 15 s LLM stall mid-reply
  (`TTS_STOP_FRAME_TIMEOUT_S`); "lead" pacing keeps the context alive through its waits.
- **Numbers are out of scope**, as in `Ledger.tla`: the layout's seconds, `heard_fraction`,
  the pts cut. The layout is taken to credit exactly what played.
- **Spoken notices** (a non-filler `TTSSpeakFrame`, queued as an expected context of its
  own — synthesizable or not, as a response — completed by its stop frame alone: no End
  re-push) are not modelled — "Worth modeling next", item 1. The hermetic fallback is `Drain = FALSE`, above.
- **A cancelled completion's End is assumed to precede the next run, if it comes at
  all.** pipecat pushes it from the cancelled task's `finally`, at cancellation; the model
  had let it arrive after the next response began streaming, and the ledger — which cannot
  tell whose End it is and ends the stream it holds — closed that response on the stale
  frame. Excluded as an ordering fact (`LlmStart` waits for `LlmCancelEnd` or
  `LlmCancelLost`) rather than modelled; real only if the event loop stalls between the
  cancel and the next run. The End may also never reach the ledger (`LlmCancelLost`, added
  2026-10-15: it reaches the TTS ahead of the interruption, whose reset may flush it);
  every row's verdict is unchanged with it, and every holding row's state count too (a
  lost End and an ignored one leave the same state).
- **One interruption sighting.** The ledger runs `_interrupt` at every processor's
  sighting of the `InterruptionFrame`, and a TTS frame in flight between two of them
  opens an anonymous turn on a closed context, which the next sighting ends; the model's
  interruption is one step.
- **The corrector's messages are the charts' and the muted ones.** The model's context is
  the committed charts of the window, and the unspeakable replies charted nothing whose
  context ended (`Muted`), in the order their contexts opened; the code counts spoken
  messages back from the newest. An uncharted commit WITH speech, or a reply pipecat
  dropped (the tool-result insert path), would put the count off by one: the code then
  finds too few messages and leaves the cut alone, or lands one message off. None is
  known.
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
| `retryMerges` | the same, and the commit that re-applied stop can make continues the unplayed turn (shipped 2026-10-15; see [the refused finalize's second commit](#the-refused-finalizes-second-commit-early_final)) | ✓ | ✓ |

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

### The reply's tool round (`tool`, 2026-10-15)

Until 2026-10-15 the model's replies were all speech, and every merge folded. A reply
can be a tool round instead, or start with one: the LLM calls a function for the
commit (`ToolCall`), its `FunctionCallInProgressFrame` travels to the assistant
aggregator (`tool = "transit"`) and puts the call and an `IN_PROGRESS` result in the
context (`CallLands`), and the result lands (`ToolDone`) — a sync tool's,
the LLM answering from it, or the consult's placeholder (`run_llm=False`, the consult
running on in the brain's own background task: `openclaw_agent_consult` is an ordinary pipecat
call, registered with the default `cancel_on_interruption=True`). Both frames are
uninterruptible (pipecat's `UninterruptibleFrame` on 1.8, `interruptible=False` since
1.12) and reach the context through the gate, so a round started while the gate holds waits
in its queue (`tool = "held"`) and lands when the hold is released — and so does a
result alone, when a sync tool's seconds of work straddle the hold's start: the call
passed before it and is in the context, the result waits (`tool = "resultHeld"`). What a drop does
to it (`Dropped`): a round in the context still running is cancelled (pipecat writes
`CANCELLED` over `IN_PROGRESS`; every tool here is registered `cancel_on_interruption`),
one finished stays. `NoSplitTurn` now reads "neither a played reply **nor a tool round
that ran** between them" (`Appended`): a finished round is the turn acted on — the model
sees it, and the consult goes on and is delivered as its own follow-up — so the
next message is a turn of its own; a cancelled round did nothing.

| HOLD | | `NoSplitTurn` | `NoLateToolRound` |
|---|---|---|---|
| `gateNoFoldCancelled` | the corrector until 2026-10-15: `_merge_tail` folded across system messages only | ✗ | ✓ |
| `gateKeepsTool` | the gate until 2026-10-15: a dropped hold left the reply's function-call frames to pipecat's reset, which keeps them | ✓ | ✗ |
| `gateDropsResult` | c1c329f: the gate dropped every function-call frame held past a drop, a result whose call had passed included | (✗) | ✓ |
| `gate` | as shipped: the corrector drops a cancelled round with the merge; the gate drops a dropped hold's held calls with their results, and lets through, settling its call without an answer, a held result whose call had passed | ✓ | ✓ |

`ut_hold_gateNoFoldCancelled_nosplitturn`, 10 states: a turn commits, its reply calls a
tool (in the context, unheld), the caller's words start a turn whose interruption drops
the reply and cancels the call, and the next commit is left apart from the fragment
around the cancelled round — what the turn-start drop always did, and what the
continuation (below) did too. `ut_hold_gateKeepsTool_nolatetool`, 9 states: the same
with the caller talking when the tool is called, so its frames wait behind the hold;
the drop keeps them and they land after the turn that dropped them —
`NoLateToolRound`: **nothing of a reply dropped before it played reaches the context
afterwards, its tool round included.** In the code a cancelled sync call landed as an
`IN_PROGRESS` nothing settles (its `FunctionCallCancelFrame`, a SystemFrame, passed the
paused gate and reached the assistant aggregator before the call had, and found nothing
to settle), and a finished one with its result and, for a sync tool, a second
inference. `reply_hold.py` now drops the function-call frames queued before a drop
(`tests/test_reply_hold.py::test_a_dropped_hold_drops_its_tool_round_too`), and
`HeardContextCorrector._merge_tail` folds across a round whose every call reads
`CANCELLED`, removing it (`test_merge_folds_across_a_tool_round_the_drop_cancelled`).

The first cut of that gate fix (c1c329f) dropped every function-call frame held past a
drop, without asking whether its call had already passed. A sync tool's result held
while its call sat in the context was then the only thing that could ever settle that
call — a finished call is not cancelled by the interruption, and pipecat's aggregator
writes nothing for one it was not told about — so the call stayed `IN_PROGRESS` for
good: the merge could not fold across it (a split again), and strict OpenAI-compatible
servers refuse a context with an unanswered tool call. `NoOrphanCall`: **every tool call
in the context gets a result.** `ut_hold_gateDropsResult_noorphancall` fails it, 11 states: a turn
commits, its reply calls a tool (unheld), the caller resumes (held), the result lands
behind the hold, their words drop it. As shipped the gate drops a held call and, by its
`tool_call_id`, that call's result; a held result whose call passed goes through with
`run_llm=False` — it settles the call, the round is the turn acted on, and the inference
it would have asked for is the dropped reply's, the commit that follows asking instead
(`test_a_held_result_whose_call_passed_settles_it_without_an_answer`, on the gate and
pipecat's assistant aggregator).

### A call overtaken by its own cancel (`CALLS`, #185)

pipecat's own race, on any barge-in, with or without the gate. The LLM broadcasts a
call's `FunctionCallInProgressFrame` from the call's task. It is a ControlFrame, so below
the LLM it queues behind the non-system frames ahead of it — the output transport parks
it in its audio queue behind the reply audio still to play — and the interruption's reset
keeps it (uninterruptible). The `FunctionCallCancelFrame` the LLM broadcasts on the same
interruption is a SystemFrame: every processor handles it ahead of its queue, the
assistant aggregator's own priority queue included. So the cancel can reach the
aggregator first. The aggregator knows the call only from `FunctionCallsStartedFrame`,
and `_handle_function_call_cancel` returns at `if not function_call:` without settling
anything. The in-progress frame then writes the `tool_calls` message and an `IN_PROGRESS`
result, and the call's task is already cancelled: nothing ever settles it. Confirmed on
1.8.1 and 1.12.0 alike (`tests/test_pipecat_function_call_race.py`, which drives it
through the real LLM service, output transport and aggregator).

In the model the cut (`CutLands`, and the continuation's own interruption in
`ContinueCommit`) cancels a call still `transit`, and its frame goes on as `lateCall`: overtaken
`"above"` the processor directly over the aggregator, or `"atAgg"`, inside the
aggregator's own queue, past that processor. `LateLands` is its arrival. `NoOrphanCall`
fails as written; a sharper property tells the third design apart: `NoUnsettledRead`,
**the LLM is never asked over a context holding a call nothing is running for and
nothing has settled** (strict OpenAI-compatible servers return 400 on it).

| CALLS | | `NoOrphanCall` | `NoUnsettledRead` |
|---|---|---|---|
| `asWritten` | pipecat's aggregator as written | ✗ | (✗) |
| `dropAbove` | a processor directly above the aggregator drops an in-progress frame whose cancel it has seen | ✗ | (✗) |
| `settleAtCommit` | the corrector settles such a call `CANCELLED` at the next commit (and snapshot) | ✓ | ✗ |
| `dropAtAggregator` | as shipped: the aggregator's own frame handling drops it | ✓ | ✓ |

`ut_calls_asWritten_noorphancall`, 9 states: a barge-in, a commit, its reply calls a tool
(in transit), a second barge-in cancels it and overtakes the frame, the frame lands.
`ut_calls_dropAbove_noorphancall`, 9 states: the same with the cancel overtaking inside
the aggregator's queue, after the processor above it let the frame through — a
processor anywhere above the aggregator, `followup_gate` included, has that window.
`ut_calls_settleAtCommit_nounsettledread`, 12 states: the frame lands after the next
commit (nothing orders the two), that commit's reply calls a tool, and the sync result
asks the LLM before any commit has settled the stale call. The window is sub-second —
the late frame has to land after the continuation's commit has read the context and
before the sync result's inference reads it — but nothing closes it, which is why that
design was rejected.

The fix is `heard_context.drop_overtaken_tool_calls`, which `agent_session` applies to the
one assistant aggregator every front-end (SIP, Talk, the room) builds: it wraps that
instance's `process_frame`, which pipecat calls for every frame in the order the
aggregator handles them. A cancel that finds no in-progress frame on record (read from
the aggregator's own map: exactly when pipecat's handler is about to return early) takes
the call's empty `FunctionCallsStartedFrame` entry off that map, as the handler deletes a
call it does have, and marks the call so that its in-progress frame is dropped if it ever
comes. The aggregator then ends as an interruption's cancel leaves it in the ordinary
order: the call off the map, and a result already behind the dropped frame (the handler
reported, then was cancelled while unwinding: cancel → in-progress → result) dropped by
pipecat as not running. Only the context differs: the call is absent, not `CANCELLED`.
The same pop covers the reply gate's drop of a held call (its cancel passes the paused
gate and finds nothing on record; the frame, dropped at the gate, never comes). That
holds for interruption cancels, which carry `run_llm=False`; the tool timeout's cancel
asks for an inference in the ordinary order and the drop never would, but it cannot
overtake a frame in practice (only `openclaw_agent_consult` has a timeout, at least 5 s, and its
frame would have to wait that long behind the reply audio ahead of it). A call no cancel
overtook always passes; a new call reusing the id clears the mark with its own
`FunctionCallsStartedFrame`, and a dropped frame takes its mark with it. Tests: in
`tests/test_pipecat_function_call_race.py`,
`test_a_barge_in_that_overtakes_the_in_progress_frame_leaves_no_call_unsettled`,
`test_a_call_that_was_not_cancelled_still_lands_and_settles`,
`test_a_result_behind_the_overtaken_frame_does_not_bring_the_call_back`,
`test_a_new_call_after_the_race_lands_and_settles`,
`test_an_overtaken_calls_id_reused_by_a_new_call_lands_and_settles`, and
`test_pipecat_alone_still_leaves_the_overtaken_call_in_progress`, which fails the day
pipecat fixes the race itself; in `tests/test_reply_hold.py`,
`test_a_dropped_held_call_leaves_the_aggregator_with_no_call_running`.

### The answer after the tool (`POSTTOOL`, #17)

Seen live 2026-09-04 on pipecat 1.7 (Talk): a `web_search`, its "Looking up ..." filler,
the result landing; the caller started asking again, the answer to the result played
over the end of their sentence, their words cut it, and their turn was answered a second
time. Re-established on main (`ac49796`, pipecat 1.12.0) by
`tests/test_post_tool_under_speech.py`, which runs `build_agent_session`'s own processors
(SIP with the reply gate and the pauser, Talk, the room) under a scripted LLM stream,
engine and caller. Without the fix:

| front-end | shape | the answer asked under speech played before the words? | the result answered |
|---|---|---|---|
| SIP | caller starts mid-filler (paused) | no | once |
| SIP | filler ends under the speech (its last 0.5 s, no pause) | **yes** | **twice** |
| SIP | filler over, then the caller starts, then the result | **yes** | **twice** |
| Talk, room | filler ends under the speech / after it | **yes** | **twice** |
| any | words while still speaking (a turn before the VAD stop) | no (cancelled unheard) | asked twice, heard once |

The mechanism on 1.12, which is not quite the issue's:

- pipecat asks for the answer from `LLMAssistantAggregator._maybe_push_context_after_function_result`
  (`llm_response_universal.py:2026`): `if run_llm and not self._user_speaking`, and
  `_user_speaking` is the user TURN (`UserStartedSpeakingFrame`, `:1786`), which here
  starts on transcribed words. The push is an `LLMContextFrame` UPSTREAM (`:2068`).
- The deferral to `BotStoppedSpeakingFrame` (`:1797`) the issue saw no longer happens in
  these shapes: the result frame waits behind the filler's audio in the output transport
  (`handle_sync_frame`), so it reaches the aggregator after the filler's stop and the push
  is immediate. Same gate, same hole.
- The 1.7 survival of a queued `LLMContextFrame` past the interruption does not hold on
  1.12: the interruption's reset drops it (`test_pipecat_premises.py`, check 9 now
  queues one). A turn start that comes while the caller is still talking therefore
  cancels the answer unheard.
- What lets it through is the VAD stop: the TTS holds a clause only while the VAD hears
  the caller (`engine_tts._hold_for_user_speech`), and speech that began over the bot is
  transcribed only once its stop flushes the segment, ~0.5-0.7 s later. The answer plays
  in that gap; on SIP the reply gate is not armed (the filler's audio disarmed it, its
  known limit) and the pauser does not pause a reply that starts with the caller quiet.

`POSTTOOL` adds the answer to `UserTurn.tla`: a sync tool's result makes it `due`;
`PushAnswer` is the aggregator's push (after the filler, never inside a commit's two
awaits; skipped for an open turn); `AnswerStart` is its first audio, through the TTS's
speech hold and the reply gate; `TtsCap` the hold's cap; a turn's interruption cancels it
(`CutLands`). `pend` is the ground truth (speech begun whose words have not resolved: no
turn start with the caller quiet, no commit carrying them, no word-less close); `gpend` is
FollowupGate's view of it, which a turn start with the VAD quiet, a commit made after the
speech's VAD stop (`PendCommitted`) or its own release timer (`GateQuiet`) ends.
`InferenceUnderSpeech` is the commit made while the caller still talks: pipecat guards
only the finalize on their speaking, not the inference, so it is possible, if narrow (the
stock strategy discards its conclusion at every VAD start, a premise check; a commit
waiting at the fence can still be overtaken by one). It takes none of the words still
being said, and a turn started on an interim mid-speech has not had them all either. The
rows before POSTTOOL keep `Inference`'s ordering fact. New properties, the last for a
pipecat behaviour the model found on the way:

- `NoAnswerUnderSpeech`: an answer asked while the caller's speech was pending never
  plays before the words of that speech (which then start a turn: heard over the caller,
  cut, and answered again).
- `NoLostToolAnswer`: nothing discards the answer for good -- it plays, or a turn of the
  caller's reads the result.
- `NoHoldOnCommittedSpeech`: the gate never holds the answer on speech whose words a
  commit has already taken -- with nobody talking it is waiting for nothing (`gstale`,
  a ghost). No property measures latency; this is the hold that had no reason.
- `NoSkippedAnswer`: pipecat's skip for an open turn never strands a result.

| POSTTOOL | | `NoAnswerUnderSpeech` | `NoLostToolAnswer` |
|---|---|---|---|
| `asWritten` | pipecat's push, gated on the turn (Talk; SIP with the gate and the pause) | ✗ | ✓ |
| `vadDrop` | the issue's filter in front of the LLM: drop while the VAD hears the caller | | ✗ |
| `capDiscard` | the TTS discards the answer when its speech hold ends on the cap | ✗ | |
| `lullAtStop` | a queue that delivers at the next lull, the VAD stop | ✗ | |
| `gateNoCommit` | the gate as first written (cb5fbb8): only a turn start or its timer ends its view | ✓ | ✓ |
| `gateById` | 0db54f6: a commit of the open turn ends the view on speech whose VAD START is older | ✗ | |
| `gateOwedBlind` | ff033c9: any commit after the speech's VAD STOP ends it, the re-applied stop's included | ✗ | |
| `gate` | as shipped: FollowupGate holds the push while speech is pending | ✓ | ✓ |
| `gate`, timers as they are | | ✗ (accepted residual) | ✓ |

`gateNoCommit` is safe and still wrong: it fails `NoHoldOnCommittedSpeech`
(`ut_post_gateNoCommit_noholdoncommittedspeech`). A turn of two segments, "what's the
time (300 ms) in Paris": the turn starts on the first, the second VAD start comes inside
the open turn and meets no turn start of its own, the commit takes its words, and a tool
whose result lands within the release window has its answer held a second with nobody
talking.

`gateById`, the first fix for that, fails `NoAnswerUnderSpeech`
(`ut_post_gateById_noanswerunderspeech`). A VAD start older than the commit does not
mean the words are in it. The caller starts, a turn opens on their first words, and the
commit is made while they are still talking. Their finalize is refused and re-applied at
their VAD stop; the tool's result lands; the gate, emptied at the commit, lets the answer
go, and the rest of their words come after it. `gate` lets go only of speech whose VAD
STOP is older than the commit: a segment's words are final once its stop flushes it,
and the commit carries them (SttCommit.tla's receipts). It keeps waiting past a turn
start made while the VAD still hears the caller.

`gateOwedBlind`, that rule as first written, fails `NoAnswerUnderSpeech`
(`ut_post_gateOwedBlind_noanswerunderspeech`). A commit after the stop does not always
carry what the stop flushed. `keep_barge_in_reachable` re-applies a refused finalize
inside the VAD stop's own handling, before the engine's flush of that stop lands
(~0.5 s), and commits whatever is in hand: a segment the engine closed mid-span (B1),
not the rest (B2). The caller resumed inside the first commit and went on. Their stop
re-applied the finalize and committed B1, and the gate let go. The answer played, then
B2 started a turn. `gate` does not count a commit made while the stop strategy still
owes that stop its close (`LateStartTurnStopStrategy.close_owed`, the receipts'
bookkeeping, which its own trigger waits on). The model's `owedClose` is that commit:
VadStop's. The watchdog's commit comes 5 s into a quiet line, its close long landed: a
timing fact. `gate` holds `NoHoldOnCommittedSpeech` with every other property.

The counterexamples (`ut_post_*`; their lengths vary with TLC's worker count, so none
are given):

- `asWritten`, SIP -- the issue's shape on main: the turn, its reply a tool
  call; `BotStart BotStop` (the filler, which disarms the gate); `VadStart` (the caller
  starts again); `CallLands ToolDone PushAnswer` (the result, the push: no turn yet);
  `VadStop AnswerStart` (the TTS's hold ends, the answer plays); `Interject` (their
  words). Talk: the same with the push landing after the VAD stop and before the words.
- `vadDrop`: dropped under the VAD, the speech was a cough (`Wordless`), and no
  turn ever reads the result. The "the caller's commit will re-run it" premise holds only
  for speech that has words.
- `capDiscard`: the cap is not where it happens -- the hold ends at the VAD
  stop and the answer plays before the words.
- `lullAtStop`: the VAD stop is not a lull; the words are still to come. A
  push just after it plays at once.

The design is TextQueue's (the livekit-menubar queue the user pointed at): deliver in a
lull, judged by the VAD and not the turn; if the caller takes the turn instead, fold the
answer into it. The fold is free here, since the result is already in the context their
commit sends. What the model adds is that the lull has to wait for the words, not
just the VAD stop. FollowupGate is the one door: it sits directly above the assistant
aggregator, so every answer's push passes it, and it is the processor that already
decides "is this a good moment to speak" for the injectors.

**What was changed.** `followup_gate.py`: an upstream `LLMContextFrame` is held from the
caller's VAD start until a turn starts -- then dropped at the interruption, the turn's
commit reading the result -- or until `TEAPORT_REPLY_HOLD_RELEASE_S` after the VAD stop
with no turn (it then goes on), capped at `TEAPORT_REPLY_HOLD_MAX_S`. Those two settings
are the reply hold's, because they state the same fact: how long after the caller goes
quiet their words may still come. A commit of the caller's words ends the wait for speech
whose VAD stop came before it. `CutReplyFence.hold_commits`, the wrapper every such commit
passes (the stop strategy's, the turn's stop, the watchdog's), tells the gate at the
commit, in the aggregator's own task, with a marker numbered off pipecat's frame counter.
The gate compares frame numbers, not arrival order: the commit and the VAD frames reach
it by different paths. This replaces 0db54f6's `TurnCommitMark` signal, which also
needed the turn open at the gate. A commit made by the turn's stop is made after its
`UserStoppedSpeakingFrame`, and that frame could reach the gate first. The fence says
nothing while the stop strategy owes the latest stop its close (`close_owed`): that
commit has none of what the stop flushed. A turn start while the VAD still hears the
caller does not end the wait. A push that arrives with a
turn open at the gate is dropped (pipecat's own rule, applied where the turn start can
have overtaken the push), and so is one still held at the cap once a turn has opened (a
release there would be a second completion). The answer is held only on speech whose
words have not arrived; with nobody talking it is asked at once. A VAD stop made before
the commit that has not yet reached the gate is not counted: the gate holds on until its
timer, a delay and never a loss.

Only the assistant aggregator sends a context frame up past the gate. It would also send
one for an `LLMRunFrame` or `LLMMessages*Frame(run_llm)` that reached it, but every one the
brain sends enters at the top, where the user aggregator takes it: a premise check
(`check_run_frames_stop_at_the_user_aggregator`) and a structural check of the brain's
senders (`test_followup_gate.py`, `test_every_run_frame_the_brain_sends_enters_above_the_user_aggregator`:
`task.queue_frame(s)` on the PipelineTask, or the STT's own push, and nothing else).

`ut_post_gate_sip_all` holds the new properties with every property `ut_shipped_all`
checks (1.1M states), `ut_post_gate_talk_all` without the reply gate and the pause.
Tests: `test_post_tool_under_speech.py`. Every shape above fails without the fix,
including a result landing between the VAD stop and the words. With nobody talking the
answer is asked at once, and after a cough it plays once, a release window later.
Nothing is left held at the end of any scenario, and a commit made while the caller
still talks leaves the rest of their words pending (fails on 0db54f6). Both of those
shapes start from a gate with a VAD stop in its history, as every turn but the first
does live. `test_followup_gate.py` adds the re-applied stop's commit through the real
fence with the strategy owing the close: nothing is cleared until the close is paid.
`test_followup_gate.py` covers the gate alone: the gap after the VAD stop, the
two-segment turn, speech begun after the commit and still going at it, the cap both
ways, and the session's End.

The gate does not change what FollowupGate calls the user speaking (`_user`, the turn):
`_idle`/`_clear` feed the narrator, the injectors' windows, `room_idle` and
`wait_until_delivered`, and a VAD-level `_user` would have changed each (a TV keeping a
room awake; a caller's breath ending the SIP busy line's wait). `Followup.tla` is
therefore unchanged. The consult follow-up has the same exposure as the answer did, since
its window is turn-level: #197.

**A blip costs a release window.** Any VAD start holds the answer until the release timer
runs out, including a run the barge-in pauser calls a blip (under `_BLIP_S`, 250 ms: echo
residue, a click) and resumes from after `_BLIP_RESUME_S`. Reusing that shorter window here
was considered and not taken. For a pause, a blip that was a word is caught anyway: the
word still cancels the resumed reply through barge-in. Here, playing the answer before
that word is the harm this gate exists to stop. A short "no" or "wait" is a short VAD
run, since the VAD's edge lags speech onset by its start time. The cost is a delay of at
most `TEAPORT_REPLY_HOLD_RELEASE_S` after a blip, and nothing is lost.

**Accepted residuals** (`ut_post_gate_timed_noanswerunderspeech`): words
later than the release window, or a caller talking past both caps (the gate's 6 s, then
the TTS's 3 s). The same assumption as the reply hold's `WORDS_IN_TIME`. Not checked
either: the faster onset test (the gate listens to the VAD only, ~0.2 s later), and an
answer asked BEFORE the caller starts and played after their stop. That is the reply
hold's #85 case: off on Talk, and on SIP not armed after a filler.

**Found on the way: a result nothing answers** (#196; `ut_post_asWritten_noskippedanswer`,
pipecat's own, every POSTTOOL): a commit whose finalize is refused (the caller
resumed inside it) leaves the turn open, so the aggregator's `_user_speaking` stays set;
that commit's reply calls a tool whose result lands before the caller's next quiet
moment; pipecat skips the push for the open turn, and the turn's commit has already run.
Unless the re-applied stop commits new words, nothing asks for the answer. FollowupGate
never sees the push, so #17's fix does not cover it: #196.

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

### The barge-in judge (`JUDGE`, #224)

`BargeInJudgeGate` (`barge_pause.py`, `TEAPORT_BARGE_JUDGE`) sits directly above the user
aggregator. Words that would start a turn over a paused reply are held there while a
one-word judge decides them:
- **CONTINUE:** the held words are dropped. Their final becomes the segment's wordless
  close, as a cough's does, and the reply resumes.
- **STOP, ANSWER or no verdict:** the held words go on as they came, and the turn
  starts as it would have.

Stop words, a reply that would be over by now, and an open user turn are never judged. A
stop word, or the reply's would-be end passing, while the judge decides lets everything
held on at once. The verdict itself is the environment: the model checks the gate around
it, with any verdict at any point.

The new steps:
- `Interject`, `OneWord` and `StopWord` go through the gate.
- `JudgeContinue`: CONTINUE, asked again on words that grew since the judge was asked.
- `JudgeLetOn`: any other verdict, the deadline, or a pause that ended first.
- `ResumeP` is the pause's quiet resume, which the gate holds while it waits.

The new monitor is `resumeOverWords`: the reply resumed while words were held, and those
words then started a turn. That is a question talked over while it was being judged.

| JUDGE | | `NoResumeOverWords` | `NoMissedBargeIn` |
|---|---|---|---|
| `noHold` | the gate without `hold_resume`: the pause's 0.8 s quiet resume can come before the verdict | ✗ | ✓ |
| `noReask` | a stop said while it decides is held with the rest, and a CONTINUE drops all of it | ✓ | ✗ |
| `shipped` | the resume held while a verdict is awaited; a stop word releases at once; a CONTINUE on grown words is asked again | ✓ | ✓ |

`shipped` holds every property the shipped pause holds:
- `ut_judge_shipped_all`: under the hold's `WORDS_IN_TIME` assumption, 12k states.
- `ut_judge_shipped_timed`: the timers as they are, everything but the accepted
  `NoStaleReply` residual, 34k states.

The witness `ut_judge_shipped_reach` FAILS `NoJudging`: a hold is reachable.

- `NoResumeOverWords`: **a question asked into the pause is not talked over while it is
  judged.** `noHold`, 11 states:

  ```
  ... BotStart                 a reply plays
  VadStart                     the caller speaks over it: paused
  Interject                    "Wait, what's a turbine?" -- held for the judge
  VadStop ResumeP              0.8 s of quiet: the pause resumes, the verdict not in
  Interject                    their next words: the turn starts on all of it,
                               after the reply has played on over the question
  ```

  The live judge's p90 is 0.45 s, so the quiet resume came first mainly on the slow
  verdicts. A wait of 1.2 s made that most of them.
- `NoMissedBargeIn` (the pause's, unchanged): **told stop while paused, the bot does not
  resume.** `noReask`, 12 states:

  ```
  ... BotStart VadStart        a reply plays; the caller speaks: paused
  Interject                    "Okay" -- held for the judge
  StopWord                     "Stop." -- held behind it
  VadStop JudgeContinue        CONTINUE on "Okay": both dropped
  ResumeP                      the reply resumes after "Stop."
  ```

  As shipped, the stop word lets everything held on at once. That is also faster: as
  first written, "Stop." waited for the verdict on "Okay" and then for a judge of its own
  (the PR #232 review).

What is not modelled:
- The judge's latency: there is no clock here, and its timeout (1.2 s per request) is a verdict.
- The 8 s pause cap: a caller still talking keeps the pause; the cap ends it as a pause end.
- What the CONTINUE'd final does to the stop strategy's close count. It becomes the
  `SegmentDoneFrame` the stop strategy already counts, which `test_barge_judge.py`
  checks.

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

Until 2026-10-15 (`retryOnQuiet`, `ut_retryOnQuiet_early_nosplitturn`) it **failed
`NoSplitTurn`**, 7 states:

```
Interject CutLands Inference   a turn commits; its reply is on its way (the gate armed)
Resume                         the caller resumes inside the commit's await: held
Finalize                       refused -- the caller is speaking; owed
VadStop                        they stop: a final of what they added is already aggregated,
                               and the retry commits it as a second message
```

No interruption dropped the held reply, so it was released after `RELEASE_S` and the bot
answered the fragment, then answered again with both messages in the context.
`reply_hold.py` listed this among its known limits ("no worse than without the gate",
0 of 27 commits on 2026-10-05), but the formal rows had it hold. `CutReplyFence` widens
the entry: the commit's wait (~70 ms over a playing reply) is inside the inference's
await, the `Resume` gap. The row is kept as the regression row for the old code, and
`tests/test_reply_hold.py::test_without_the_continuation_the_refused_finalize_splits_the_turn`
replays that trace through the real aggregator, controller, fence, gate and corrector.

As shipped (`MODE = "retryMerges"`, `CommitStep`) a commit that finds the gate still
armed once the fence has let it through continues the turn whose reply has not played:
`CutReplyFence.hold_commits` asks the gate (`unplayed_reply`, the gate's `armed`, set by
`agent_session` where there is a gate) and, if so, pushes an `InterruptionFrame` down
from the aggregator — no turn start, nothing upstream — and waits for it like any cut.
The gate reads it as a turn start's: it drops the held reply (ending the hold) and asks
for the `TurnMerge`; the LLM cancels a reply still being written; the corrector folds
the commit into the unanswered message. Armed after the fence's wait means no turn
start's interruption has reached the gate since the last commit, so only the refused
finalize's second commit takes the branch; the model applies `CommitStep` at every commit
as the code's one wrapper does, and before tool rounds were modelled `ut_shipped_all`
had the same 18k states the proposed-fix row (`ut_retryMerges_all`, now folded into it)
had with the branch at `VadStop` alone. Past the fence's bound the code does not ask, as
the model's `Continues` requires `cut = "none"`.

The 2026-10-15 review of the first cut (e62e67a) found four places the model was
narrower than the code:

- **Tool rounds.** A reply that called a tool before the continuation dropped it left
  the round between the two user messages, and `_merge_tail` refused the merge: the
  code dropped the reply *and* split the turn while `ContinueCommit` always folded. In
  the refused-finalize shape the gate holds from the commit on, so the round is
  normally still in its queue, where the drop used to leave it to land late. Both are
  the turn-start drop's as much as the continuation's, and both are fixed for both
  (the tool-round section above): the continuation folds across a round it cancelled,
  drops a held one with the reply, and leaves a finished one (the turn acted on) apart.
- **The session's end** (`NoLostGoodbye`). pipecat's user aggregator pushes the
  `EndFrame` on and then commits an open turn's leftover words
  (`_maybe_emit_user_turn_stopped(on_session_end=True)`) through the same
  `push_aggregation`; with the gate armed the first cut sent the continuation's
  interruption there, which overtook the End and dropped the unheld reply queued ahead
  of it — a goodbye — for words nothing will answer (`ut_retryMergesAtEnd_nolostgoodbye`,
  `MODE = "retryMergesAtEnd"`, 5 states: a commit, the hang-up during its await with
  words in hand). `hold_commits` now never continues once that session-end commit has
  begun (`test_the_session_end_commit_never_drops_the_reply_ahead_of_the_end`).
- **The fence's bound** (`ContinueLate`). The second wait, for the continuation's own
  frame, is bounded like every other: past `CUT_COMMIT_WAIT_SECS` the commit goes ahead
  unmerged (the gate has not asked for the merge yet) and the frame, landing late, is
  the overtaken cut of `CutLands` — it drops the held reply and asks for a merge the next
  commit gets. That is `NoSplitTurn` failing under `FENCE = "timed"`, the same accepted
  residual as `NoCutAfterCommit` there (`ut_fence_timed_nosplitturn`; its first trace is
  a turn start's commit past the bound, and a probe with only the continuation able to
  overtake fails it too, at the continuation's `VadStop`). Nothing is playing when the
  continuation fires — the gate is armed — so its frame's crossing is the cheap one (a
  few ms, against the ~70 ms of a cut that stops audio), and a timeout is logged
  (`CUT-FENCE`).
- **Follow-ups** (not modelled further; the argument). Below the aggregator the
  continuation's `InterruptionFrame` is the frame a turn start sends, and it does what one
  does to `Followup.tla`'s machinery: `FollowupTrigger` voids an armed shot,
  `FollowupGate` settles the deliveries it watches as interrupted and releases a claim,
  an agent-first consult's typing stops (a background consult's goes on, unheard while
  the caller talks). The caller did keep talking, so that is right, and it is a
  sequence `Followup.tla`'s environment already produces: on the re-applied stop's path
  the aggregator broadcasts `UserStoppedSpeakingFrame`, then the continuation's
  interruption, then the commit — `UserStop` without words, `UserStart`, `UserStop` with
  them, each a free action there, so `NoSilentLoss`, `NoRepeatRecital` and the rest are
  checked against it; on the strategy's own second stop the interruption comes while the
  turn is still open (`user` set), when no window is open for a follow-up to have
  claimed, and it only meets the reply it was sent for. A follow-up queued in the gap
  between that `UserStoppedSpeakingFrame` and the commit is voided and re-delivered
  exactly as one a new turn's start would void.

`ut_shipped_all` is the SIP front-end as shipped with everything on — `retryMerges`,
`byContext`, `gate`, `shipped`, `inTime`, `EARLY_FINAL`, tool rounds, the in-transit
call and its drop at the aggregator (`dropAtAggregator`) — and holds every property
(172k states, a few seconds). `ut_shipped_timed` is the same with the hold's
timers as they are (`WORDS_IN_TIME = FALSE`: `Release` and `Cap` end holds whatever the
caller does next) and holds every property but the accepted `NoStaleReply` residual
(632k states, ~10 s): the timeout paths leave no queue paused (`NoSilencedBot`), a held
tool round lands when they release it, and a reply released by a timer but not yet
started is still the one a continuation drops. With
`EARLY_FINAL = FALSE` (every row above) no commit finds the gate armed, so
`retryOnQuiet` and `retryMerges` are one machine and those rows keep `retryOnQuiet`.
Talk and the room mic run the same turn machine and fence, without the hold
(`TEAPORT_REPLY_HOLD_TALK` defaults off) or the pause (their transport cannot pause):
`HOLD = "none"`, `PAUSE = "off"`, whose `NoStaleReply` and `NoSplitTurn` fail as before
#85 — and without a gate nothing knows the first reply has not played, so the second
commit stays a second message there. The room's own turn timing (`ROOM_*`, #159) and
`SIP_ENDPOINT_STOP_SECS` are durations, which the model does not have.

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
- **One tool round per reply, all-or-nothing.** `tool` is the last commit's reply's
  one round: a chain of calls, or a parallel batch partly settled, is not modelled (the
  corrector folds only when EVERY call of a round reads `CANCELLED`, which the tests
  pin). pipecat's own race outside a hold — an unheld `FunctionCallInProgressFrame`
  still in transit below the LLM when a cancel overtakes it — is modelled since
  2026-10-15 (`CALLS`, `lateCall`) and fixed in
  `heard_context.drop_overtaken_tool_calls` (see
  [a call overtaken by its own cancel](#a-call-overtaken-by-its-own-cancel-calls-185)).
  Two late frames at once land as one, at the later position (FIFO between them). The
  tool rounds need the gate (`ToolCall` takes an armed reply), so the race's rows run
  with it, though the race does not need it. The split a late `IN_PROGRESS` causes
  (`_merge_tail` cannot fold across it) is not monitored apart: `NoOrphanCall` already
  falls there. A speculation's snapshot is not counted as a read for `NoUnsettledRead`:
  `settleAtCommit` is given the settle there too, the generous version of it.
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
- **Scale.** Since the cut became its own step (2026-10-14): 592 distinct states with `SPEC = "off"` (436 before it, 47 before the reply hold's variables), 4.2k with it on, 338 for `HOLD = "gate"` with words in time and 647 with the timers as they are, 1.5k and 3.5k for the shipped pause with each; with tool rounds (2026-10-15) 1.2k and 2.2k for `HOLD = "gate"` with words in time and without, 5.1k and 12k for the shipped pause, 63k for `ut_shipped_all` and 237k for `ut_shipped_timed`; with the in-transit call (#185) 2.3k and 5.3k, 11k and 32k, 172k and 632k; seconds each.

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

**A second context behind the held reply** (`SECOND`, 2026-10-14 audit; fixed
2026-10-15). The model had one context, and the gate's shift and the TTS's anchoring are
per context: a context that starts while the held reply is still to play — a narrator
line, the answer after a tool call — was anchored by the TTS at the held reply's end on
ITS model (`_prev_audio_end_ns`, or its own first audio if later;
`engine_tts.start_word_timestamps`), and the gate moved its pts by ITS OWN first held
frame's wait (`reply_hold._restamp`), while it really plays right behind the held reply,
which plays from the release. `DESIGN2` says which anchoring.

| `DESIGN2` | | `SecondOnTime` | `SecondEndOnTime` |
|---|---|---|---|
| `asWritten` | `pc_second_asWritten`, the code until 2026-10-15 | ✗ (5 states) | |
| `realClock` | `pc_second_realClock`: the fix below, but anchored on the real clock | ✗ (7 states) | |
| `shipped` | `pc_second_fix2`, **as shipped**: a context that starts while the freeze holds anchored audio back is anchored on the frozen schedule (`_frozen_from`); an ended context's anchor and the second's own end move by the hold since that instant; every context held behind the first takes the first's shift | ✓ (183 states, with `CommittedIsPlayed` and `WordsOnTime`) | ✓ |

Two ways, both from the code as written. Started *after* the release (TLC's shortest,
`Wait Wait Release Arrive2`): `_freeze_playout` moved the held reply's end by the whole
hold, `now - then`, though the hold began `LAT` before its first audio — the second
context's words land `LAT` late (`pp_hold_anchor` is the same arithmetic). Started
*during* the hold (`Wait Wait Wait Arrive2 Release`, held 3, first audio at 3): anchored
at `max(LAT + N, 3) = 4` on the pre-hold schedule, moved by its own wait, 0 — its words
2 units EARLY, so a cut credits words not yet heard, which no estimate corrects.

The fix, three parts that only hold together. The TTS records where the freeze began
holding anchored audio back, `_frozen_from`: the freeze's start if anything anchored was
still to play, else the first audio anchored during it (`_stop_holds_from`). An ended
context's `_prev_audio_end_ns` and the in-progress context's `_ctx_held_ns` both move by
the hold since that instant. A context whose first audio comes while the freeze holds
audio back is anchored on the frozen schedule — `max(_prev_audio_end_ns, _frozen_from)`,
pipecat's baseline reading `_frozen_from` instead of the clock (`EngineTTSService.get_clock`
pins one read) — and the gate gives every context it held the shift the first one fixed
(`_release_shift`). Both halves are needed: with the gate's shared shift but the real
clock (`pc_second_realClock`, `Wait`×4 `Arrive2 Release`, `N = 2`: the second context's
first audio at 4, after the held reply's pre-hold end at 3) its words land a unit late.
`SecondEndOnTime` checks the second context's own end — where a third would be
anchored — and it needs the hold since `_frozen_from` too, not since its own anchor.
`pc_second_fix2` runs at `N = 2`, `MaxHold = 4` so a second context can start after
the held reply's pre-hold end. The tests, each failing before the fix:
`test_reply_hold.py::test_a_reply_queued_behind_a_held_one_is_anchored_on_the_frozen_schedule`
(the TTS), `::test_the_contexts_held_behind_the_first_move_with_it` (the gate: 0.45 s
against the first's 0.75 s), `::test_a_reply_ending_inside_a_hold_moves_by_the_hold_since_its_first_audio`.
Rare on a call: the hold is about a second, and needs a second context to start inside it.

Not modelled, and accepted: the TTS hears of the release one upstream SystemFrame after
the gate acts, so a context first anchored in that window is anchored frozen but not
shifted (well under a millisecond, on the input task). And the freeze treats everything
the TTS has anchored as held, though audio already past the gate when the hold began (a
line still playing at the commit) plays on — as before the fix.

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
before the next clause is synthesized was then in the gap, and so in the clause's pts —
`_freeze_playout` moved `_play_end` by the stop only when it was still ahead of the stop
— and the ledger adds it again (`_resume`: every turn with audio pushed).

| `LATE = 2`, ledger `shipped` | | `HeardIsPlayed` |
|---|---|---|
| `pp_late_ahead_heard` | `TTSGAP = "ahead"`, before the fix | ✗ (9 states) |
| `pp_late_moved_heard` | `TTSGAP = "moved"`, **as shipped** (2026-10-15): `_freeze_playout` always moves `_play_end` by the stop, so a gap never holds stop time and the ledger's shift is the only move | ✓ (20k states) |

```
Tick Tick      the first clause's two units play out; nothing more is synthesized yet
Pause Tick     the caller makes a sound: a pause with nothing to hold back, 1 unit
Resume         no words; the ledger's shift is now 1
Emit2          the next clause: the TTS sees a 1-unit gap (the pause) and places it after it
Tick Cut       unit 3 played (pts 4); the ledger reads the cut at 4 - 1 and drops it
```

The pause did not delay the clause, and its words carried it twice. The seconds were
right (the layout lays a chunk out no earlier than its push), and `_finish` never reports
fewer words than the played fraction implies, so the chart fell back to that estimate —
off by a word or so — instead of the exact cut. The fix moves `_play_end` by every stop,
even one that began after playout ran dry (a model already in the past stays in the past,
so the pacing is unchanged). `test_barge_pause.py::
test_a_pause_in_an_underrun_is_not_a_playout_gap_as_well` is the trace on the real TTS:
before the fix, the next clause after a 0.3 s pause started 0.3 s late.

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
(`_ctx_held_ns`) already counted from `max(then, base)`.

| `SLOT = "set"`, `FIRSTAUDIO = "any"` | `AnchorMovesByStop` |
|---|---|
| `pp_hold_anchor` — `ANCHOR = "stop"`, before the fix | ✗ (6 states: the hold, a tick, the first audio, the context ends, the release — moved 1, stopped with audio 0) |
| `pp_hold_anchor_fixed` — `ANCHOR = "audio"`, **as shipped** (2026-10-15): the ended context's anchor moves by the stop since its first audio (`_frozen_from`) | ✓ (1.1k states, with `FrozenWhileStopped`) |

A reply queued behind such a held reply — the answer after a tool call — was anchored
late by however long the hold ran before the first audio, so its captions lagged and its
heard cut came short (the estimate floor again). `PlayoutClock.tla`'s
`pc_second_asWritten` is the same, seen from the words, and its fix carries this one.
`test_reply_hold.py::test_a_reply_ending_inside_a_hold_moves_by_the_hold_since_its_first_audio`
is the trace: a reply anchored 0.3 s into a 0.7 s hold ended at 12.0 s instead of 11.7 s.

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
| the same, before `NUMBERED` | the strategy three edges behind the VAD, a verdict naming no stop (`sc_verdict_stale`, THE ORDER; until 2026-10-15) | ✗ | | | | |
| the same, `NUMBERED` | as shipped: the verdict names its stop, three edges behind (`sc_verdict_numbered`) | ✓ | ✓ | ✓ | ✓ | ✓ |

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
| `stop` | `unmarked` | shipped until 2026-10-15, against an engine without the reason field — before teagram-engine#36 (`sc_final_stop`) | ✗ | ✗ | |
| `stop` | `marked` | shipped until 2026-10-15, against the engine that marks its dones — teagram-engine#36, what the box runs (`sc_final_stop_marked`, `sc_final_stop_stranded`) | ✗ | | ✗ |
| `receipts` | `unmarked` | as shipped since 2026-10-15, on the old engine (`sc_final_stop_residual`) | | ✓ | ✓ |
| `receipts` | `marked` | as shipped since 2026-10-15, on the engine the box runs (`sc_final_receipts`, `_backstop`; `_full` and `sc_voice_final_carry` under `--full`) | ✓ | | ✓ |

The `stop` rows held on the marked engine until 2026-10-14, when THE ORDER (below) put
the VAD where pipecat runs it; the `receipts` rows are the repair, in the code since
2026-10-15 (THE ORDER, What was changed).

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
shape. THE ORDER (below) amended both halves of it: a close for a stop the strategy has
not counted yet is a receipt, and a done nothing asked for that carries words is
stamped with no stop at all.

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
entirely; both now say it of the `receipts` design, shipped since 2026-10-15 (the
`stop` design before it had two more). Live, the mispairing needs the
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
`sc_voice_final` runs the final's protocol (as shipped, `receipts`) under the wait on the
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
`--full` holds the six rows too large for the default run, the carry's three among
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
against the code as it then was. All three are fixed (What was changed, below).

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
`TurnVerdictFrame` named no stop, so the STT took it for stop 2's and committed the
held segment; the model then calls stop 2 INCOMPLETE and the caller resumes inside the
ceiling: the #43 split. It needs the aggregator's input task stalled for a whole
utterance (≥ ~0.5 s with Talk's 0.2 s stop), which Smart Turn's inference on the
appliance's cores under load could do but has not been seen to. Its repair, `NUMBERED`,
is the code (What was changed, below); `sc_verdict_stale` stays as the row for the code
before it.

### What was changed (2026-10-15): numbered verdicts, for C

The model's repair, `NUMBERED = TRUE`, is the code.

- **The verdict names its stop** (`endpointing.py`). `TurnVerdictFrame` carries
  `stop_n`, and `LateStartTurnStopStrategy._push_verdict` stamps it with `_stops`, the
  strategy's count of VAD stops — the count #173's receipts already keep, of the same
  frames the STT counts in `_vad_stops`, so no second counter. Every verdict answers
  the latest stop counted: the model's at that stop, the ceiling's and the turn-stop's
  under it (a VAD start since stops the ceiling and releases the hold). That is
  `StratStop`'s `Up("verdict", v, stratStops + 1)` and `Ceiling`'s and `TurnStop`'s
  `stratStops`. The default, `NO_STOP`, answers no stop.
- **The STT acts only on its own stop's verdict** (`stt.py`, `_handle_verdict`). One
  whose `stop_n` is not `_vad_stops` is ignored before anything else reads it — the
  hold, the late-after-expiry note — and the stop's own verdict, still on its way
  behind it, decides. That is `Verdict`'s `NUMBERED /\ other`. A stale verdict still
  counts as a verdict for the session-wide fallback: it proves the wiring answers.
- **The rows follow the code.** Every row that runs `DESIGN = "receipts"` now runs
  `NUMBERED = TRUE`, the code, except `sc_verdict_stale`, which stays as the row for
  the code before (`NoSplitOnIncomplete` falls, fifteen states);
  `sc_verdict_numbered` is the shipped design at `MaxLag = 3` (1.1 million states,
  every commit property). The rows of an older design keep `FALSE`.
- **Test.** `tests/test_stt_commit_on_verdict.py`,
  `test_a_verdict_for_the_stop_before_does_not_close_the_held_segment`, drives the real
  strategy and the real service through `sc_verdict_stale`'s trace: the STT sees stop
  1, the resume and stop 2 before the strategy answers stop 1 COMPLETE. On the old
  `_handle_verdict` that verdict commits stop 2's held segment; now it is ignored, the
  model's INCOMPLETE for stop 2 holds, and the resumed words join the one segment. The
  tests that hand the STT a verdict directly stamp it with the stop the STT is on.

### THE RESET (2026-10-15): a lost session pays the stops it takes

`_disconnect_websocket` — a reconnect after the receive task fails, or `hold()` for a
phone call — drops the open segment, the stamps of the commits still out and the hold,
and pushes `SegmentResetFrame` for the strategy. Until this change the strategy only
dropped its interim there, and the owed close stayed owed. The model had no disconnect;
`SttReset` is one now (bounded by `MaxResets`, the next session up at once), with
`RESET` for what the strategy does at the frame.

**D — a turn stranded by a reset** (`sc_reset_owed`, `RESET = "keep"`, twenty states):

```
VadStart, VadStop    stop 1, COMPLETE; the STT commits
VadStart             the caller starts again before stop 1's final lands (the burst)
VadStop              stop 2, COMPLETE; its commit goes out; the strategy owes stop 2
Done -> StratFinal   stop 1's final: the turn's transcript, finalized; it pays stop 1
SttReset             the session goes, and stop 2's done with it
StratReset           the strategy drops its interim; stop 2 is still owed
```

A turn with its transcript in hand and the verdict COMPLETE, and nothing that will ever
pay stop 2: it waits for the caller's next stop or the aggregator's 5 s watchdog
(`NoStrandedTurn`). Not seen live; it needs the session to go inside a turn's last
second.

**The repair, `RESET = "pays"`, is the code.** `SegmentResetFrame` carries `stop_n`,
the STT's `_vad_stops` at the reset, and `LateStartTurnStopStrategy._note_reset` takes
the reset as the close of every stop up to it, since none of them will get another:
it pays the owed close if it is among them, keeps a receipt for each the strategy has
not counted yet (the STT saw it first), and, having paid, finalizes whatever `_text`
holds, as a wordless close does (`_finalize_on_wordless_close`, shared with
`_handle_segment_done`). That is `StratReset`. `sc_reset_pays` holds every property
with one reset anywhere in the run (134,569 states); `sc_reset_pays_closes`
(`--full`) adds the engine's own close (3,139,486 states, 1 min 35 s). By hand, the
same design also held with `MaxLag = 2` (278,747) and with a lost verdict, the
backstop and the unmarked wire (6,465,751, without `NoStaleTurnEnd`, the mispairing
residual's). The next turn is not mis-closed under either design: the strategy's next
count replaces the debt.

`NoStrandedTurn` now leaves out a turn a reset has left with no text (Known limits, the
disconnect); with resets off it reads as before. Every other row runs `MaxResets = 0`, and
the receipts rows `RESET = "pays"`, the code.

**Test.** `tests/test_earlier_final_does_not_end_the_turn.py`,
`test_a_reset_with_a_stops_commit_out_is_that_stops_close`, drives the real service and
the real strategy through the trace above and disconnects the service; on the code
before, the turn never ends.

### What was changed (2026-10-15): receipts, for A and B

The model's repair, `DESIGN = "receipts"`, is the code; the model led it.

- **The strategy keeps receipts** (`endpointing.py`, `LateStartTurnStopStrategy`).
  `_note_close` keeps the stamp of a close for a stop it has not counted yet
  (`stop_n > _stops`) in `_receipts`; `_handle_vad_user_stopped_speaking` counts the
  stop and owes its close only if no receipt for it is held, then drops the receipts
  the count has passed. That is `StratStop`'s `owed'`/`early'` and `Receipt(f)`. B's
  close, landing first, is now the receipt the count finds.
- **An unasked done with words pays no stop** (`stt.py`, `_handle_message`). It is
  stamped `NO_STOP` (`endpointing.py`, `0`: stops count from 1, so it matches no debt
  and is never a receipt), where it was stamped `_vad_stops + 1`, the next stop's.
  `None` would not do: it is another STT's unstamped final, which pays whatever is
  owed, as stock. That is `Done`'s `NONE`. A's first half no longer pays the stop the
  strategy has just counted. The stop is answered by its own commit, the tail's
  included. A wordless unasked done keeps the last commit's stamp, as before.
- **The rows follow the code.** Every row that describes the code as shipped now runs
  `DESIGN = "receipts"`: the commit rows (`sc_verdict`, `_stale`, `_numbered`), the
  voice rows and their witnesses, the carry pair, and the fallback's `sc_vadStop_*`.
  The `receipts` final rows ran with `NUMBERED = FALSE`, the code at the time, where
  they first held with `TRUE`; since the numbered verdicts (above) they run `TRUE`
  again. At `MaxLag = 1` the verdict's stop never mattered: C needs three.
  The `stop` rows stay as the design before receipts, each a FAILS row named for its
  finding: `sc_final_stop_marked` (A), `sc_final_stop_stranded` (B), and
  `sc_final_stop` (the old engine). The design rows of their own time,
  `sc_verdict_silentStop` and `sc_voice_blind`, keep `stop`.
- **Tests.** `tests/test_earlier_final_does_not_end_the_turn.py` drives the real
  service and the real strategy through each trace, handing the strategy what the
  service pushed in the order TLC found it.
  `test_the_engines_cut_landing_between_the_two_counts_does_not_end_the_turn` is A.
  `test_a_stops_close_landing_before_the_strategy_counts_it_is_a_receipt` is B. Both
  fail on the code before the change: the turn ends on the first half, and the turn
  never ends. The stamp tests there and in `test_stt_commit_on_verdict.py` expect
  `NO_STOP` where they expected the next stop.

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
- **A disconnect is one step, and the next session is up at once** (`SttReset`, THE
  RESET below). A VAD stop that reaches the STT while there is no session — during a
  `hold()`, before `resume()` — holds and then commits into no socket: that is a commit
  whose send fails (below), and the turn it strands ends on the watchdog. The words a
  reset takes are lost, not outstanding, so no stale-end is charged for them; and a turn
  that a reset leaves with no words at all (an interim opened it, and the segment it
  streamed from is gone) is left out of `NoStrandedTurn`: the watchdog closes it, and
  there is nothing to answer.
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
- **Scale.** Measured 2026-10-15 on TLC 2.19 with the receipts rows as shipped, two
  workers on a desktop shared with other checks. Default run: `sc_verdict` 7.0 million
  distinct states (4 min 4 s), `sc_voice` 4.3 million (2 min 32 s; the missed stop moved
  to `sc_voice_missed`), `sc_vadStop_sound` 3.7 million (1 min 31 s), `sc_voice_final`
  1.3 million (36 s), `sc_verdict_numbered` 1.1 million (37 s), the three `receipts`
  final rows 260,000 to 480,000 each (under 20 s), `sc_carry_off` 260,000 (8 s); every
  row that falls finds its trace in 8 to 15 states, in a second or a few. `--full`:
  `sc_final_receipts_full` 26.6 million (15 min), `sc_voice_missed` 27.9 million
  (14 min), `sc_vadStop_sound_carry` 5.6 million (3 min 16 s), `sc_voice_carry` 5.7
  million (3 min 7 s), `sc_voice_final_carry` 4.1 million (2 min 12 s) -- about 40
  minutes in all. Switching the shipped rows from `stop` to `receipts` (and the final
  rows' `NUMBERED` to FALSE) left every count above where it was, to the figures given,
  and so did setting `NUMBERED` back to TRUE on every shipped row (the numbered
  verdicts, re-measured the same day: `sc_verdict` 6,953,899, `sc_voice` 4,318,220). The three queues
  cost most where the engine's own close and the backstop are
  both on (the final rows: 0.4 million with either, 8 million with both, at the same
  other bounds), which is why the default final rows take them one at a time.
  `check.sh` passes `-deadlock`: the bounds make terminal states ordinary. Before THE
  ORDER (2026-10-11/12, the whole desktop): `sc_verdict` 1.6 million, `sc_voice` 10.9
  million, `sc_voice_final` 13.6 million, `sc_final_stop_residual`/`_marked` 6.2 and
  5.7 million, the `--full` carry rows 20 to 30 million each.

## pipecat premises

Every model here takes some of pipecat's behaviour as given, and a fix the model checked
is only as good as those premises: a pipecat upgrade can drop one without a line of the
brain changing (the 1.12 bump moved the user aggregator's commit off the method
`CutReplyFence` wrapped). `brain/tests/test_pipecat_premises.py` checks each premise below
on REAL pipecat objects -- the turn controller, the aggregators, the frame processor's
queues, the TTS base under `EngineTTSService`, the output transport -- and fails if
pipecat stops doing it. A failure there means a model has drifted from pipecat: re-read
the model's premise, not just the check. Where another test already pins a premise on
the real code, the table names it instead. Sources are pipecat 1.8.1, the pin the table
was first written against, under `pipecat/` (`llm_response_universal.py` is
`processors/aggregators/`); where 1.12 (main's pin since #184) moved a premise, its row
says so. The 1.8.1 column stays as the record of the previous pin, so the next upgrade
has two columns to compare against.

The brain's own behaviour (strategies, gate, ledger, corrector, fence) and the engine's
are not premises in this sense and are not listed. `SipCall.tla`, `SessionArbiter.tla`
and `SttSlot.tla` rest on none: their machines are the brain's. `PlayoutPause.tla`'s
Part 1 is the ledger's arithmetic, but its Part 2 (`ANCHOR = "audio"`) is
`_prev_audio_end_ns` and `_ctx_held_ns`, kept in `EngineTTSService`'s override of
pipecat's `_maybe_reset_word_timestamps(context_id)` -- the context-end hook row below.

| premise | model(s) | check | source (1.8.1) | 1.8.1 (the previous pin) | 1.12 (main's pin) |
|---|---|---|---|---|---|
| A second user turn start is refused while one is open; a start calls each stop strategy's `handle_user_turn_started` (the late-start rule) | UserTurn (`StartTurn`; `retryOnQuiet`), SttCommit (`OpenTurn`) | `check_a_second_turn_start_is_refused_while_one_is_open`; the composite wedge: `test_barge_in_survives_an_open_turn.py::test_the_stock_controller_still_wedges` | `turns/user_turn_controller.py:332-351` | ✓ | ✓ |
| A finalize is refused while the user speaks, leaving the turn open and the stop strategy un-reset; an accepted stop resets it (`handle_user_turn_stopped`) | UserTurn (`Finalize`, `EARLY_FINAL`), SttCommit (`TURNSTOP = "verdict"`) | `check_a_finalize_is_refused_while_the_user_speaks` | `turns/user_turn_controller.py:367-396` | ✓ | ✓ |
| The stop watchdog is an inactivity timer every VAD edge and transcript re-arms, fires only while the user is quiet, and its stop resets the strategies | UserTurn (`Arm`, `ForceStop`), SttCommit (`TurnStop`) | `check_the_stop_watchdog_is_starved_by_speech_and_resets_the_strategy` | `turns/user_turn_controller.py:266-296, 398-415` | ✓ | ✓ |
| `MinWordsUserTurnStartStrategy` asks `min_words` while the bot speaks and one word otherwise | UserTurn (PAUSE: `OneWord`, `NoLostAnswer`) | `check_min_words_relaxes_to_one_word_only_while_the_bot_is_silent` | `turns/user_start/min_words_user_turn_start_strategy.py:77-111` | ✓ | ✓ |
| A user turn start broadcasts an `InterruptionFrame` both ways (barge-in is a turn start) | UserTurn (`StartTurn`/`CutLands`); every `Interrupt` (Ledger, LedgerPlayout, Followup) | `check_a_turn_start_broadcasts_the_interruption` | `llm_response_universal.py:1275-1292`; `processors/frame_processor.py:1017-1022` | ✓ | ✓ |
| The interruption is only queued: the commit in the same controller call can overtake it | UserTurn (`CutLands`, `FENCE`) | `test_barge_commit_order.py::test_without_the_fence_the_turn_overtakes_the_cut_reply` | `processors/frame_processor.py:1017-1022` | ✓ (that test) | ✓ (main's copy) |
| `trigger_user_turn_stopped` is two awaits, and the context write (the LLM asked) is in the first: a VAD start inside it has the finalize refused after the commit | UserTurn (`Inference`/`Resume`/`Finalize`) | `check_the_commit_runs_before_the_finalize_it_may_lose` | `turns/user_stop/base_user_turn_stop_strategy.py:204-222`; `llm_response_universal.py:1296-1330` | ✓ | ✓ |
| Every write of the caller's words goes through the one writer `CutReplyFence.hold_commits` wraps: inference, a finalize with words since, the watchdog, the session's end | UserTurn (`CommitStep`, `NoCutAfterCommit`; the hold's arming) | `check_every_commit_passes_the_fenced_writer` (through the brain's own `hold_commits`; fails on 1.12 with main's fence: `three`, `four`, `five` bypass it) | `llm_response_universal.py:869-886, 1296-1330, 1371-1404` (1.12: `_push_aggregation`, `:900`) | ✓ | ✓ |
| Cascade mode, session end by EndFrame: the End is pushed on, THEN the leftover words are committed through `_maybe_emit_user_turn_stopped(on_session_end=True)` (`_stop`) | UserTurn (`Hangup`, `NoLostGoodbye`) | `check_the_session_end_commit_follows_the_end_frame` (real aggregator); the fence's half: `test_barge_commit_order.py::test_the_session_end_commit_never_drops_the_reply_ahead_of_the_end` | `llm_response_universal.py:809-813, 891-900` | ✓ | ✓ |
| The same by CancelFrame (`PipelineTask.cancel`, a hang-up): the same call (`_cancel`), but BEFORE the CancelFrame is pushed on | UserTurn (`Hangup`, `NoLostGoodbye`) | `check_the_session_end_commit_runs_on_a_cancel_too` | `llm_response_universal.py:815-817, 903-910` | ✓ | ✓ |
| An interruption's queue reset keeps only uninterruptible frames (function-call frames, the brain's `FinalTranscriptionFrame` and segment frames) and drops the rest (a queued `LLMRunFrame` or `LLMContextFrame` included); with an interruptible frame in hand it re-creates the task, lifting a pause | UserTurn (`gateKeepsTool`, `Dropped`; POSTTOOL's `CutLands` drops a queued answer), SttCommit (`toStrategy` loses no final), Followup (`UserStart` flushes the run) | `check_the_interruption_reset_keeps_only_uninterruptible_frames` | `processors/frame_processor.py:1130-1155, 1222-1242`; `utils/frame_queue.py:84` | ✓ | ✓ |
| With an uninterruptible frame in hand the reset leaves the task waiting on the pause | UserTurn (`gateNoResume`; the shipped gate does not depend on it) | `check_an_uninterruptible_frame_in_hand_keeps_the_pause` | `processors/frame_processor.py:1133-1139` | ✓ | ✓ |
| A SystemFrame (the interruption) passes a paused queue; the EndFrame waits in it; every frame enters through `queue_frame` | UserTurn (`BotStart`; `gateEndWaits`, `NoEndBehindHold`) | `check_a_system_frame_passes_a_paused_queue_and_the_end_waits_in_it` | `processors/frame_processor.py:700-728, 1300-1330` | ✓ | ✓ |
| An interruption cancels a running `cancel_on_interruption` call, and the assistant aggregator writes `CANCELLED` over `IN_PROGRESS` (the shape `_merge_tail` folds across) | UserTurn (`Dropped`, `gateNoFoldCancelled`) | `check_an_interruption_cancels_a_running_call_as_CANCELLED` | `services/llm_service.py:688-689, 758-761`; `llm_response_universal.py:1772-1806, 1956-1968` | ✓ | ✓ |
| A cancel that overtakes an `IN_PROGRESS` still in transit leaves the call unsettled (#185: pipecat as written; the brain fixes it) | UserTurn (`CALLS`, `NoOrphanCall`, `NoUnsettledRead`; see "A call overtaken by its own cancel") | `test_pipecat_function_call_race.py::test_pipecat_alone_still_leaves_the_overtaken_call_in_progress` (fails once pipecat fixes it: the fix is then dead weight) | `llm_response_universal.py:1956-1962` `_handle_function_call_cancel` returns early with no in-progress frame on record (1.12: 2110-2116) | ✓ | ✓ |
| `run_llm` after a result: `properties.run_llm`, else `frame.run_llm`, else yes | UserTurn (`ToolDone`, `NoOrphanCall`), Followup (`ToolEnd`) | `check_run_llm_resolves_properties_then_frame_then_yes`; the gate's frame: `test_reply_hold.py::test_a_held_result_whose_call_passed_settles_it_without_an_answer` | `llm_response_universal.py:1836-1872` | ✓ | ✓ |
| The answer to a tool's result is an `LLMContextFrame` pushed UPSTREAM by the assistant aggregator (past `FollowupGate`, above it), deferred to `BotStoppedSpeakingFrame` while the bot is audible, and skipped for an open user TURN, whatever the VAD says | UserTurn (POSTTOOL: `PushAnswer`) | `check_the_answer_to_a_result_is_pushed_up_unless_a_turn_is_open`; through the session's processors: `test_post_tool_under_speech.py` | 1.12 `llm_response_universal.py:1786, 1797, 2026, 2057-2068` | -- | ✓ |
| An `LLMContextFrame` is answered by the LLM whatever its direction, on its own context, and pushed no further: a run the gate pushes UP (`gate.ask`) is a completion like the post-tool answer | Owed (`RunGo`) | `check_an_upstream_context_frame_is_answered_by_the_llm` (a subclass of the frame, as `RunFrame` is) | 1.12 `services/openai/base_llm.py:643-657` | -- | ✓ |
| Each answer the assistant aggregator pushes up for a tool's result asks on its own live context, every result in it: the gate's hold keeps one, a newer replacing the older, beside the run's own slot | Owed (`HoldNoLoss`), UserTurn (POSTTOOL) | `check_the_answer_to_a_result_asks_on_the_live_context` | 1.12 `llm_response_universal.py:2057-2068` | -- | ✓ |
| The TTS holds the End of a completion with no text (no context opens; it is re-pushed only when a context of its turn's id ends): a run that says nothing (stay_silent, empty) tells the gate itself (`RunSilentFrame`) | Owed (`Say`'s silent and stay outcomes) | `check_a_text_less_completions_end_is_held_by_the_tts` | 1.12 `services/tts_service.py:933-945, 1785-1803` | -- | ✓ |
| A VAD start discards the stop strategy's conclusion (`_turn_complete`), so the brain's strategy does not commit while the caller speaks; pipecat itself guards only the finalize on it | UserTurn (`Inference`'s ordering fact; POSTTOOL's `InferenceUnderSpeech` drops it) | `check_a_vad_start_discards_the_stop_strategys_conclusion` (stock and the brain's subclass, with a control) | 1.12 `turns/user_stop/turn_analyzer_user_turn_stop_strategy.py:129-141, 218-223, 328-340` | -- | ✓ |
| An `LLMRunFrame` or `LLMMessagesAppendFrame(run_llm)` queued at the top is taken by the user aggregator (its context frame goes down, the run frame no further), so none reaches the assistant aggregator to be sent up past `FollowupGate`; frame ids increase in creation order | UserTurn (POSTTOOL: only a tool's answer comes up; `PendCommitted` by id) | `check_run_frames_stop_at_the_user_aggregator`; the brain's senders: `test_followup_gate.py::test_every_run_frame_the_brain_sends_enters_above_the_user_aggregator` | 1.12 `llm_response_universal.py` (`LLMUserAggregator._handle_llm_run`, `_handle_llm_messages_append`); `utils/utils.py:23` (`obj_id`) | -- | ✓ |
| A tool result is broadcast by the LLM and passes the TTS and the output transport to `FollowupGate` | Followup (`ToolEnd`, `turnAware`) | `check_a_tool_result_reaches_the_gate_below_the_transport` | `services/llm_service.py:1618-1630` (`result_callback`'s broadcast); `transports/base_output.py:896-923` (`_audio_task_handler` pushes it on) | ✓ | ✓ |
| `FunctionCallsStartedFrame` is a SystemFrame the LLM broadcasts from inside `_process_context`, before the completion's End | UserTurn (`ToolCall`/`ToolDone` leave the gate `armed`; only `BotStart`/`CutLands` disarm) | `check_a_tool_calls_started_frame_is_a_system_frame_ahead_of_the_end` (the real `_process_context` on a scripted tool-call stream) | `services/llm_service.py:1456`; `services/openai/base_llm.py:560-600, 603-616` | ✓ | ✓ |
| A cancelled completion's End (pushed from a `finally`) is sighted by the ledger after the interruption, or never: the ledger reads it where the TTS processes it, and it reaches the TTS ahead of the interruption, a queued frame the reset may flush. Either way the ledger has interrupted first and queues no partial text | Ledger, LedgerPlayout (`LlmCancelEnd`, `LlmCancelLost`) | `check_a_cancelled_completion_never_reaches_the_ledger_as_a_finished_one` (the real `TranscriptLedger` built with the TTS) | `services/openai/base_llm.py:603-616`; `processors/frame_processor.py:839-841`; the brain's reading point `transcript_ledger.py:318-327` | ✓ | ✓ |
| The VAD runs in the user aggregator and each edge is also pushed UPSTREAM, to the STT | SttCommit (THE ORDER; the receipts, numbered verdicts) | `check_vad_edges_are_pushed_upstream_to_the_stt` | `llm_response_universal.py:1237-1270` | ✓ | ✓ |
| A context opens (`TTSStartedFrame`) for any non-blank text, before `run_tts` | LedgerPlayout (`UnspeakableOpens`) | `test_tts_stop_frame.py::test_an_unspeakable_reply_opens_a_context_and_the_next_reply_keeps_its_text` | `services/tts_service.py:1147-1282` | ✓ (that test) | ✓ (main's suite) |
| A context closes at its End only if the last `run_tts` yielded audio (the flag `on_turn_context_completed` reads) | LedgerPlayout (`CloseAtEnd`, `lp_endings`) | guard `engine_tts._require_close_at_end_flag` (source); `test_tts_stop_frame.py::test_a_reply_whose_last_piece_is_unspeakable_closes_at_its_end` | `services/tts_service.py:722-746, 1348-1375` | ✓ (that test) | ✓ (main's suite) |
| Every opened context ends with a `TTSStoppedFrame` naming it; the transport ends bot-speaking on it (3 s idle fallback without) | LedgerPlayout (`TtsStop`, `PlayMark`, `PlayIdle`) | `test_tts_stop_frame.py::test_stop_frame_ends_bot_speaking_at_audio_end`, `::test_without_stop_frame_transport_waits_out_the_fallback` | `services/tts_service.py:1711-1782`; `transports/base_output.py:396` | ✓ (those tests) | ✓ (main's suite) |
| After the stop the TTS re-pushes the response's OWN End (same id) at the last word's pts, before the next context starts; the context-end hook `_maybe_reset_word_timestamps(context_id)` that `EngineTTSService` overrides runs there | LedgerPlayout (`TtsDrain`, `CanStart`), PlayoutClock (the End's pts), PlayoutPause (Part 2: the anchor the override keeps) | `check_the_tts_repushes_each_responses_own_end_after_its_context`; the hook: guard `engine_tts._require_context_end_hook` | `services/tts_service.py:1659-1691` | ✓ | ✓ |
| A context that ends without audio has its text force-completed as word frames ahead of the End, and the assistant aggregator commits it | LedgerPlayout (`said`, `Failed`, `Muted`) | `check_a_reply_with_no_audio_still_commits_its_text` | `services/tts_service.py:1693-1709, 1752-1778` | ✓ | ✓ |
| A context that timed out mid-reply is re-created under the SAME id | LedgerPlayout (`TtsRestart`; `ledger_split` settled) | `check_a_timed_out_context_resumes_under_its_own_id` | `services/tts_service.py:1277-1282` (`_turn_context_id`) | ✓ | ✓ |
| `start_word_timestamps` takes its baseline from its first `get_clock()` read | PlayoutClock (`pc_second_realClock`) | guard `engine_tts._require_baseline_clock_read` (behavioural); `test_tts_streaming.py::test_a_moved_baseline_clock_read_fails_the_session_build` | `services/tts_service.py:1381-1398` | ✓ (that test) | ✓ (main's suite) |
| The output transport releases timed frames by (pts, arrival), not arrival order | PlayoutClock (`Pop`, `pc_release_words`) | `check_the_transport_releases_timed_frames_by_pts_then_arrival` | `transports/base_output.py:640-649, 1079-1100` | ✓ | ✓ |
| What the transport pushes on after writing a chunk carries no `context_id` | Ledger, LedgerPlayout (the untagged rebuild) | `check_the_transport_repushes_played_audio_untagged` | `transports/base_output.py:896-923` | ✓ | ✓ |
| The assistant aggregator commits a cut reply at the interruption and a reply at its End; words after the End go to the next message | UserTurn (`CutLands`), PlayoutClock (`Pop`) | `check_the_assistant_aggregator_commits_at_the_interruption_and_the_end` | `llm_response_universal.py:1748-1750, 2053-2061, 2076-2097` | ✓ | ✓ |
| The stop strategy's own rules the brain subclasses: an interim un-finalizes, the fallback for a transcript with no VAD stop, `_maybe_trigger_user_turn_stopped` as every path's gate | SttCommit (`StratDelta`, `StratFinal`, the owed close) | `test_endpointing.py`, `test_earlier_final_does_not_end_the_turn.py` (real strategy) | `turns/user_stop/turn_analyzer_user_turn_stop_strategy.py` | ✓ (those tests) | ✓ (main's suite) |
| **1.12 only.** A speculative inference runs on a provisional context copy and commits nothing (no user message, no `on_user_turn_message_added`, the aggregation kept) | UserTurn (the fence, the hold's arming) | `check_a_speculative_inference_commits_nothing` | 1.12 `llm_response_universal.py:1412-1433` | skip | ✓ |
| **1.12 only.** A commit with `run_llm=False` (a stop with `confirms_speculation`) confirms an already-generated reply: the context write, no inference | UserTurn (`CommitStep`, `NoLostGoodbye`; 0aa019b's fence) | `check_a_confirming_stop_commits_without_inference` | 1.12 `llm_response_universal.py:900-930, 1463` | skip | ✓ |
| **1.12 only.** The brain uses no speculating stop strategy (pipecat's only one is `EagerUserTurnStopStrategy`): the two rows above are latent | -- | `check_the_brain_never_speculates` (structural) | 1.12 `turns/user_stop/eager_user_turn_stop_strategy.py` | skip | ✓ |

Not checked behaviourally, because no fix rests on them (each holds whichever way pipecat
goes): `Followup.tla`'s placement facts -- `LLMFullResponseStartFrame` is pushed before the
request serializes the context, the TTS consumes `LLMTextFrame` (a bare tool call's End
held at the TTS, the pre-#13 latch, is now checked as the text-less End above). Also not
checked separately: an interruption
dropping a completed reply whose synthesis has not begun (the TTS's own queue reset,
`tts_service.py:1031-1067`; the processor reset rule above is the general case), and
"a cancelled completion's End, if the ledger sights it at all, does so before the next
run" (an ordering fact the models state: `LlmStart` waits for `LlmCancelEnd` or
`LlmCancelLost`; the End is pushed at the cancel, ahead of anything the next commit
sends).

**Guards.** Three premises fail the session build if pipecat moves them
(`engine_tts._require_context_end_hook`, `_require_close_at_end_flag` -- both read the
source -- and `_require_baseline_clock_read`, behavioural); #184 adds signature checks
in `endpointing` for the controller's inference hook and the fence's stop hook. No new
guard was added: the box installs the pipecat in `uv.lock` (since #56), the same one CI
runs these checks against, so a guard catches only a box off the lock. Moving the guards
into one module is left until #184 merges, which adds to them.

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
