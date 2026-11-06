-------------------------------- MODULE Followup --------------------------------
(***************************************************************************)
(* teaport — a one-shot order in the context, and the gate's turn latch.   *)
(*                                                                          *)
(*   brain/teaport_brain/followup_gate.py  -- FollowupGate, FollowupTrigger *)
(*   brain/teaport_brain/client_notes.py   -- the reaction order            *)
(*                                                                          *)
(* An injector waits for a turn-free window -> posts a one-shot USER-role   *)
(* order ("react now ...") -> queues an LLMRunFrame -> the completion that  *)
(* answers from it retires it. Since #203 the one such order is ClientNotes'*)
(* reaction; this model was written for the async consult's follow-up       *)
(* trigger ("tell the user now"), which is gone: owed.py renders its note   *)
(* into each request and stores nothing, and Owed.tla checks it. The        *)
(* follow-up's delivery loop (MODE = "heard", #30) went with it.            *)
(*                                                                          *)
(* The order is a STANDING ORDER in the context:                            *)
(*   - retired too early  -> it is never acted on (silent loss)             *)
(*   - retired too late   -> a later turn re-executes it (repeat recital,   *)
(*                           the 2026-08-26 08:17 "shop list twice" bug)    *)
(*                                                                          *)
(* MODE selects the retirement design:                                      *)
(*   "asWritten"     -- gate.wait_until_delivered(): _busy/_idle, i.e. ANY   *)
(*                      activity. Violates NoSilentLoss.                    *)
(*   "gateOnOwn"     -- wait for OUR completion to start, then end.         *)
(*                      Violates NoRepeatRecital.                           *)
(*   "retireOnRead"  -- FollowupTrigger, as shipped: retire at the FIRST    *)
(*                      TEXT of a completion whose request carried the      *)
(*                      order (followup_gate.py, the first alphanumeric     *)
(*                      LLMTextFrame). Satisfies both. A completion that    *)
(*                      read the order and produced no text -- a bare tool  *)
(*                      call, a barge-in before its first word, an empty    *)
(*                      reply -- leaves it live: the same turn's answering  *)
(*                      completion, or the caller's next turn, reads it    *)
(*                      again and answers it once. ClientNotes withdraws    *)
(*                      an order nobody answered (FExpire: REACT_MAX_WAIT_S,*)
(*                      or a newer order posted over it).                   *)
(*   "retireOnStart" -- the MUTANT the shipped placement rules out: retire  *)
(*                      at LLMFullResponseStartFrame. pipecat pushes that   *)
(*                      frame BEFORE it serializes the context into the     *)
(*                      request, so the order is withdrawn before the model *)
(*                      sees it. Violates NoSilentLoss -- the proof that    *)
(*                      the property is not vacuous under retireOnRead.     *)
(*                                                                          *)
(* LATCH selects how the gate's _llm flag behaves across a TOOL CALL -- a   *)
(* turn that is two completions with the tool between them:                 *)
(*   "held"              -- pre-PR-#13: _llm latched until the answering     *)
(*                          completion ends. Violates NoDeadAirDuringTool.   *)
(*   "clearedOnToolCall" -- PR #13: FunctionCallInProgressFrame clears _llm. *)
(*                          Violates NoInterjectMidTurn -- and ONLY that.    *)
(*   "turnAware"         -- the fix: _llm released as above, plus _turn for  *)
(*                          the turn-free waiters alone (ClientNotes, and    *)
(*                          owed.py's run door: Owed.tla's NoRunMidTurn),    *)
(*                          and since #203 the commit's claim: a turn        *)
(*                          committed but not yet started (TurnCommitMark,   *)
(*                          FollowupGate.turn_committed) shuts the window    *)
(*                          too. Holds everything.                          *)
(*                                                                          *)
(* Checked designs and results are in README.md next to this file.          *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxTurns, MaxRuns,
          MaxOrders,    \* orders the injector may post (a newer one replaces a live one)
          MODE, LATCH

ASSUME MODE \in {"asWritten", "gateOnOwn", "retireOnRead", "retireOnStart"}
ASSUME LATCH \in {"held", "clearedOnToolCall", "turnAware"}
ASSUME MaxOrders >= 1

\* ClientNotes' order (its expiry and its replacement are its own; the consult
\* follow-up's designs had neither).
Notes == MODE \in {"retireOnRead", "retireOnStart"}

VARIABLES
    user, bot, llm, tool,      \* the gate's view: _user, _bot, _llm (+ a tool call in flight)
    run,          \* the completion in flight: none | read (request sent, no text yet) |
                  \*   text (first text produced) | tts (the TTS opened its context) |
                  \*   play (its audio is playing)
    q,            \* LLMRunFrames / committed turns queued, not yet started
    started, turns,
    fpc,          \* the injector's program counter
    trigger,      \* the order: absent | live | neutral (retired)
    orders,       \* orders posted so far
    sawLive,      \* the running completion's request carried the live order
    answered,     \* completions that answered from the latest order in text
    lost,         \* an order was retired with no answer, other than by expiry or a newer order
    interjected

vars == <<user, bot, llm, tool, run, q, started, turns, fpc, trigger, orders, sawLive,
          answered, lost, interjected>>

Idle  == (~user) /\ (~bot) /\ (~llm)                      \* what the narrator waits for
\* What a turn-free waiter waits for: wait_until_idle(turn_free=True). Under turnAware
\* a turn in flight (_turn) and a turn committed but not yet started (the commit's
\* claim, TurnCommitMark; q > 0 here) both shut the window.
Clear == /\ Idle
         /\ (LATCH # "turnAware" \/ (run = "none" /\ q = 0))

Init ==
    /\ user = FALSE /\ bot = FALSE /\ llm = FALSE /\ tool = FALSE
    /\ run = "none" /\ q = 0 /\ started = 0 /\ turns = 0
    /\ fpc = "waitIdle"
    /\ trigger = "absent" /\ orders = 0
    /\ sawLive = FALSE /\ answered = 0 /\ lost = FALSE /\ interjected = FALSE

(* =============== the injector, by retirement design =============== *)

FWaitIdle ==                                   \* await gate.wait_until_idle(turn_free=True)
    /\ fpc = "waitIdle" /\ Clear
    /\ fpc' = "queue"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,trigger,orders,sawLive,answered,
                   lost,interjected>>

\* Post the order and queue its LLMRunFrame (ClientNotes._post_order + queue_frames).
\* Did the gate's "quiet" reading hide a turn that is still in flight -- or, under
\* turnAware, one committed and not yet started?
Post ==
    /\ trigger' = "live" /\ orders' = orders + 1 /\ answered' = 0
    /\ q' = q + 1                              \* queue_frames() ENQUEUES; it does not cancel
    /\ sawLive' = FALSE                        \* no running request carries the new order
    /\ interjected' = (interjected \/ run # "none"
                       \/ (LATCH = "turnAware" /\ q > 0))

FQueue ==                                      \* add the order; queue LLMRunFrame
    /\ fpc = "queue"
    /\ Clear                                   \* no await between the gate returning and the append
    /\ Post
    /\ fpc' = "waitBusy"
    /\ UNCHANGED <<user,bot,llm,tool,run,started,turns,lost>>

\* A newer order posted at a later window while the last is still unread
\* (client_notes.py _post_order: "one live order at most" -- _retire_order drops the
\* old one unread; a host note folded into a user turn's request does the same at the
\* read). Not a silent loss: a newer order is one of the two ways an unread order
\* may go.
FReplace ==
    /\ Notes /\ fpc = "waitBusy" /\ trigger = "live"
    /\ Clear /\ orders < MaxOrders /\ q < MaxRuns
    /\ Post
    /\ UNCHANGED <<user,bot,llm,tool,run,started,turns,fpc,lost>>

\* An order no completion answered within REACT_MAX_WAIT_S is withdrawn before the
\* next request is built (client_notes.py _expire_order, at the LLMContextFrame on its
\* way to the LLM): the one the user's next turn asks for, or the tool's answering
\* completion. The 20 s are out of scope: it may expire at any such moment. Not a
\* silent loss either.
FExpire ==
    /\ Notes /\ trigger = "live" /\ (run = "none" \/ tool)
    /\ trigger' = "neutral"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,fpc,orders,sawLive,answered,
                   lost,interjected>>

FWaitBusy ==                                   \* wait_until_delivered() phase 1
    /\ fpc = "waitBusy"
    /\ CASE MODE = "asWritten" -> ~Idle          \* ANY activity sets _busy
         [] MODE = "gateOnOwn" -> llm /\ sawLive  \* our completion started
         [] Notes              -> trigger # "live" \* ClientNotes waits for nothing: the
                                                   \* order's own retirement ends its story
    /\ fpc' = "waitIdle2"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,trigger,orders,sawLive,answered,
                   lost,interjected>>

FWaitIdle2 ==                                  \* wait_until_delivered() phase 2
    /\ fpc = "waitIdle2"
    /\ CASE MODE = "asWritten" -> Idle
         [] MODE = "gateOnOwn" -> ~llm
         [] Notes              -> TRUE
    /\ fpc' = "neutralize"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,trigger,orders,sawLive,answered,
                   lost,interjected>>

\* The injector retires the one-shot itself (the consult follow-up's designs). Retiring
\* an order nobody answered is the loss; under the Notes designs the order is retired
\* already, and this is a no-op.
FNeutralize ==
    /\ fpc = "neutralize"
    /\ trigger' = "neutral"
    /\ lost' = (lost \/ (trigger = "live" /\ answered = 0))
    /\ fpc' = "done"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,orders,sawLive,answered,interjected>>

(* ---------------- the user ---------------- *)

\* The caller's turn starts: an InterruptionFrame. It cancels the completion in flight
\* and flushes queued frames. An armed order stays armed (FollowupTrigger): the caller's
\* own turn reads it.
UserStart ==
    /\ ~user /\ turns < MaxTurns
    /\ user' = TRUE
    /\ llm' = FALSE /\ bot' = FALSE /\ run' = "none" /\ tool' = FALSE
    /\ sawLive' = FALSE
    /\ q' = 0
    /\ UNCHANGED <<started,turns,fpc,trigger,orders,answered,lost,interjected>>

\* The caller's turn ends: their words are committed and a completion queued.
UserStop ==
    /\ user
    /\ user' = FALSE
    /\ turns' = turns + 1
    /\ q' = IF q < MaxRuns THEN q + 1 ELSE q
    /\ UNCHANGED <<bot,llm,tool,run,started,fpc,trigger,orders,sawLive,answered,lost,
                   interjected>>

(* ---------------- completion + playout ---------------- *)

\* LLMFullResponseStartFrame, and the request carrying the context: the read. Under
\* retireOnStart the order is withdrawn at the start frame, before the request is built,
\* so the request does not carry it.
RunStart ==
    /\ q > 0 /\ run = "none" /\ ~llm /\ ~bot
    /\ started < MaxRuns
    /\ q' = q - 1
    /\ run' = "read" /\ llm' = TRUE
    /\ sawLive' = (trigger = "live" /\ MODE # "retireOnStart")
    /\ trigger' = IF MODE = "retireOnStart" /\ trigger = "live" THEN "neutral" ELSE trigger
    /\ lost'    = (lost \/ (MODE = "retireOnStart" /\ trigger = "live" /\ answered = 0))
    /\ started' = started + 1
    /\ UNCHANGED <<user,bot,tool,turns,fpc,orders,answered,interjected>>

\* The first LLMTextFrame. A completion whose request carried the order answers from it
\* here; under retireOnRead FollowupTrigger fires its armed shot at this frame, whoever's
\* completion it is (followup_gate.py: no await between the check and the retirement).
FirstText ==
    /\ run = "read" /\ llm /\ ~tool
    /\ run' = "text"
    /\ answered' = IF sawLive THEN answered + 1 ELSE answered
    /\ trigger'  = IF MODE = "retireOnRead" /\ trigger = "live" THEN "neutral" ELSE trigger
    /\ lost'     = (lost \/ (MODE = "retireOnRead" /\ trigger = "live" /\ ~sawLive
                             /\ answered = 0))
    /\ UNCHANGED <<user,bot,llm,tool,q,started,turns,fpc,orders,sawLive,interjected>>

TtsOpen ==                                     \* the TTS opens its context
    /\ run = "text"
    /\ run' = "tts"
    /\ UNCHANGED <<user,bot,llm,tool,q,started,turns,fpc,trigger,orders,sawLive,answered,
                   lost,interjected>>

ToolCall ==                        \* FunctionCallInProgressFrame: a bare tool call
    /\ run = "read" /\ llm /\ ~tool
    /\ tool' = TRUE
    \* PR #13, followup_gate.py. Pre-PR the end frame was held at the TTS service
    \* (empty text creates no audio context), so _llm stayed latched.
    /\ llm' = IF LATCH \in {"clearedOnToolCall", "turnAware"} THEN FALSE ELSE llm
    /\ UNCHANGED <<user,bot,run,q,started,turns,fpc,trigger,orders,sawLive,answered,lost,
                   interjected>>

ToolEnd ==                         \* the async shape: the result comes back with run_llm=False
    /\ tool /\ run = "read"        \* and NO completion follows -- the turn is over
    /\ tool' = FALSE /\ run' = "none" /\ sawLive' = FALSE
    \* "held": the bare call's End frame never arrives, so _llm stays latched --
    \* the dead air. The other designs read idle here.
    /\ llm' = IF LATCH = "held" THEN llm ELSE FALSE
    /\ UNCHANGED <<user,bot,q,started,turns,fpc,trigger,orders,answered,lost,interjected>>

\* The answering completion -- ANOTHER context read, with the tool's result in view.
\* The order, still live after a completion that produced no text, is read again: the
\* same turn answers it (once, at its first text), which is no repeat recital.
ToolResult ==
    /\ tool /\ run = "read"
    /\ tool' = FALSE
    /\ llm' = TRUE
    /\ sawLive' = (trigger = "live")
    /\ UNCHANGED <<user,bot,run,q,started,turns,fpc,trigger,orders,answered,lost,interjected>>

LLMEnd ==                          \* the response ends; its audio starts (BotStartedSpeaking)
    /\ run = "tts" /\ llm /\ ~tool
    /\ llm' = FALSE /\ bot' = TRUE /\ run' = "play"
    /\ UNCHANGED <<user,tool,q,started,turns,fpc,trigger,orders,sawLive,answered,lost,
                   interjected>>

BotStop ==                         \* BotStoppedSpeaking: played to the end
    /\ bot /\ ~llm /\ run = "play"
    /\ bot' = FALSE /\ run' = "none" /\ sawLive' = FALSE
    /\ UNCHANGED <<user,llm,tool,q,started,turns,fpc,trigger,orders,answered,lost,
                   interjected>>

Next ==
    \/ FWaitIdle \/ FQueue \/ FReplace \/ FExpire \/ FWaitBusy \/ FWaitIdle2 \/ FNeutralize
    \/ UserStart \/ UserStop
    \/ RunStart \/ FirstText \/ TtsOpen \/ ToolCall \/ ToolResult \/ ToolEnd
    \/ LLMEnd \/ BotStop

Spec == Init /\ [][Next]_vars

(* ---------------- properties ---------------- *)

\* An order is retired with no completion having answered from it only by its expiry or
\* by a newer order. (Before the 2026-11-05 audit this read "(fpc = done) => reads >= 1",
\* which the injector's own wait implied under retireOnRead whatever the retirement
\* did; retireOnStart, with the same waits, fails this one.)
NoSilentLoss == ~lost

\* No order is answered from in text by two completions. A completion that read it and
\* produced no text (a bare tool call) does not count: its turn's answering completion
\* reads the order again and answers it once.
NoRepeatRecital == answered <= 1

\* The order never goes into a turn that is still in flight. NOT implied by the
\* read-count properties: an order appended during a tool call is answered exactly once,
\* by the tool's own answering completion, while the two turns collide.
NoInterjectMidTurn == ~interjected

\* The gate's `_llm` side of PR #13: during a tool call with nobody speaking the gate
\* reads idle. HISTORICAL since 9861f45 (#230): the reason was the consult narrator's
\* progress line, which waits on plain wait_until_idle() and needed the gate idle during
\* a synchronous consult's call (up to 45 s); it now skips its line while a call is out
\* (FollowupGate.calls_out), and the async consult runs no narrator at all. The rows
\* stay as the record of why `_llm` is released on FunctionCallInProgressFrame, which
\* the code still does; nothing that acts on the plain-idle reading does so during a
\* tool call any more (the other plain-idle waiter is the greeting's, gateway_server.py).
NoDeadAirDuringTool == (tool /\ ~user /\ ~bot) => ~llm

TypeOK ==
    /\ fpc \in {"waitIdle","queue","waitBusy","waitIdle2","neutralize","done"}
    /\ trigger \in {"absent","live","neutral"} /\ orders \in 0..MaxOrders
    /\ run \in {"none","read","text","tts","play"} /\ q \in 0..MaxRuns
    /\ tool \in BOOLEAN /\ interjected \in BOOLEAN /\ sawLive \in BOOLEAN /\ lost \in BOOLEAN
    /\ turns \in 0..MaxTurns /\ started \in 0..MaxRuns /\ answered \in 0..MaxRuns
=============================================================================
