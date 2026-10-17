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
(*   "asWritten"    -- gate.wait_until_delivered(): _busy/_idle, i.e. ANY    *)
(*                     activity. Violates NoSilentLoss.                     *)
(*   "gateOnOwn"    -- wait for OUR completion to start, then end.          *)
(*                     Violates NoRepeatRecital.                            *)
(*   "retireOnRead" -- retire AT the read. Satisfies both (FollowupTrigger). *)
(*                                                                          *)
(* LATCH selects how the gate's _llm flag behaves across a TOOL CALL -- a   *)
(* turn that is two completions with the tool between them:                 *)
(*   "held"              -- pre-PR-#13: _llm latched until the answering     *)
(*                          completion ends. Violates NoDeadAirDuringTool.   *)
(*   "clearedOnToolCall" -- PR #13: FunctionCallInProgressFrame clears _llm. *)
(*                          Violates NoInterjectMidTurn -- and ONLY that.    *)
(*   "turnAware"         -- the fix: _llm released as above, plus _turn for  *)
(*                          the turn-free waiters alone (ClientNotes, and    *)
(*                          owed.py's run door: Owed.tla's NoRunMidTurn).    *)
(*                          Holds everything.                               *)
(*                                                                          *)
(* Checked designs and results are in README.md next to this file.          *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxTurns, MaxRuns, MODE, LATCH

VARIABLES
    user, bot, llm, tool,      \* the gate's view: _user, _bot, _llm (+ a tool call in flight)
    run,          \* the completion in flight: none | read (request sent, no text yet) |
                  \*   text (first text produced) | tts (the TTS opened its context) |
                  \*   play (its audio is playing)
    q,            \* LLMRunFrames / committed turns queued, not yet started
    started, turns,
    fpc,          \* the injector's program counter
    trigger,      \* the order: absent | live | neutral (retired)
    sawLive,      \* the running completion's request carried the live order
    reads,        \* completions that read the live order
    interjected

vars == <<user, bot, llm, tool, run, q, started, turns, fpc, trigger, sawLive, reads,
          interjected>>

Idle  == (~user) /\ (~bot) /\ (~llm)                      \* what the narrator waits for
\* What a turn-free waiter waits for: wait_until_idle(turn_free=True).
Clear == /\ Idle
         /\ (LATCH # "turnAware" \/ run = "none")

Init ==
    /\ user = FALSE /\ bot = FALSE /\ llm = FALSE /\ tool = FALSE
    /\ run = "none" /\ q = 0 /\ started = 0 /\ turns = 0
    /\ fpc = "waitIdle"
    /\ trigger = "absent"
    /\ sawLive = FALSE /\ reads = 0 /\ interjected = FALSE

(* =============== the injector, by retirement design =============== *)

FWaitIdle ==                                   \* await gate.wait_until_idle(turn_free=True)
    /\ fpc = "waitIdle" /\ Clear
    /\ fpc' = "queue"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,trigger,sawLive,reads,interjected>>

FQueue ==                                      \* add the order; queue LLMRunFrame
    /\ fpc = "queue"
    /\ Clear                                   \* no await between the gate returning and the append
    /\ trigger' = "live"
    /\ q' = q + 1                              \* queue_frames() ENQUEUES; it does not cancel
    /\ fpc' = "waitBusy"
    \* Did the gate's "quiet" reading hide a turn that is still in flight?
    /\ interjected' = (interjected \/ run # "none")
    /\ UNCHANGED <<user,bot,llm,tool,run,started,turns,sawLive,reads>>

FWaitBusy ==                                   \* wait_until_delivered() phase 1
    /\ fpc = "waitBusy"
    /\ CASE MODE = "asWritten"    -> ~Idle          \* ANY activity sets _busy
         [] MODE = "gateOnOwn"    -> llm /\ sawLive  \* our completion started
         [] MODE = "retireOnRead" -> reads >= 1     \* the order was consumed
    /\ fpc' = "waitIdle2"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,trigger,sawLive,reads,interjected>>

FWaitIdle2 ==                                  \* wait_until_delivered() phase 2
    /\ fpc = "waitIdle2"
    /\ CASE MODE = "asWritten"    -> Idle
         [] MODE = "gateOnOwn"    -> ~llm
         [] MODE = "retireOnRead" -> TRUE
    /\ fpc' = "neutralize"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,trigger,sawLive,reads,interjected>>

FNeutralize ==                                 \* retire the one-shot
    /\ fpc = "neutralize"
    /\ trigger' = "neutral"
    /\ fpc' = "done"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,sawLive,reads,interjected>>

FRequeue ==                                    \* barged away -> try again
    /\ MODE \in {"gateOnOwn", "retireOnRead"}
    /\ fpc = "waitBusy" /\ trigger = "live" /\ run = "none" /\ q = 0 /\ Idle
    /\ started < MaxRuns
    /\ q' = 1
    /\ UNCHANGED <<user,bot,llm,tool,run,started,turns,fpc,trigger,sawLive,reads,interjected>>

(* ---------------- the user ---------------- *)

\* The caller's turn starts: an InterruptionFrame. It cancels the completion in flight
\* and flushes queued frames.
UserStart ==
    /\ ~user /\ turns < MaxTurns
    /\ user' = TRUE
    /\ llm' = FALSE /\ bot' = FALSE /\ run' = "none" /\ tool' = FALSE
    /\ sawLive' = FALSE
    /\ q' = 0
    /\ UNCHANGED <<started,turns,fpc,trigger,reads,interjected>>

\* The caller's turn ends: their words are committed and a completion queued.
UserStop ==
    /\ user
    /\ user' = FALSE
    /\ turns' = turns + 1
    /\ q' = IF q < MaxRuns THEN q + 1 ELSE q
    /\ UNCHANGED <<bot,llm,tool,run,started,fpc,trigger,sawLive,reads,interjected>>

(* ---------------- completion + playout ---------------- *)

\* LLMFullResponseStartFrame, and the request carrying the context: the read.
RunStart ==
    /\ q > 0 /\ run = "none" /\ ~llm /\ ~bot
    /\ started < MaxRuns
    /\ q' = q - 1
    /\ run' = "read" /\ llm' = TRUE
    /\ sawLive' = (trigger = "live")
    /\ reads'   = IF trigger = "live" THEN reads + 1 ELSE reads
    /\ trigger' = IF MODE = "retireOnRead" /\ trigger = "live" THEN "neutral" ELSE trigger
    /\ started' = started + 1
    /\ UNCHANGED <<user,bot,tool,turns,fpc,interjected>>

FirstText ==                                   \* the first LLMTextFrame
    /\ run = "read" /\ llm /\ ~tool
    /\ run' = "text"
    /\ UNCHANGED <<user,bot,llm,tool,q,started,turns,fpc,trigger,sawLive,reads,interjected>>

TtsOpen ==                                     \* the TTS opens its context
    /\ run = "text"
    /\ run' = "tts"
    /\ UNCHANGED <<user,bot,llm,tool,q,started,turns,fpc,trigger,sawLive,reads,interjected>>

ToolCall ==                        \* FunctionCallInProgressFrame: a bare tool call
    /\ run = "read" /\ llm /\ ~tool
    /\ tool' = TRUE
    \* PR #13, followup_gate.py. Pre-PR the end frame was held at the TTS service
    \* (empty text creates no audio context), so _llm stayed latched.
    /\ llm' = IF LATCH \in {"clearedOnToolCall", "turnAware"} THEN FALSE ELSE llm
    /\ UNCHANGED <<user,bot,run,q,started,turns,fpc,trigger,sawLive,reads,interjected>>

ToolEnd ==                         \* the async shape: the result comes back with run_llm=False
    /\ tool /\ run = "read"        \* and NO completion follows -- the turn is over
    /\ tool' = FALSE /\ run' = "none" /\ sawLive' = FALSE
    \* "held": the bare call's End frame never arrives, so _llm stays latched --
    \* the dead air. The other designs read idle here.
    /\ llm' = IF LATCH = "held" THEN llm ELSE FALSE
    /\ UNCHANGED <<user,bot,q,started,turns,fpc,trigger,reads,interjected>>

ToolResult ==                      \* the answering completion -- ANOTHER context read
    /\ tool /\ run = "read"
    /\ tool' = FALSE
    /\ llm' = TRUE
    /\ sawLive' = (trigger = "live")
    /\ reads'   = IF trigger = "live" THEN reads + 1 ELSE reads
    /\ trigger' = IF MODE = "retireOnRead" /\ trigger = "live" THEN "neutral" ELSE trigger
    /\ UNCHANGED <<user,bot,run,q,started,turns,fpc,interjected>>

LLMEnd ==                          \* the response ends; its audio starts (BotStartedSpeaking)
    /\ run = "tts" /\ llm /\ ~tool
    /\ llm' = FALSE /\ bot' = TRUE /\ run' = "play"
    /\ UNCHANGED <<user,tool,q,started,turns,fpc,trigger,sawLive,reads,interjected>>

BotStop ==                         \* BotStoppedSpeaking: played to the end
    /\ bot /\ ~llm /\ run = "play"
    /\ bot' = FALSE /\ run' = "none" /\ sawLive' = FALSE
    /\ UNCHANGED <<user,llm,tool,q,started,turns,fpc,trigger,reads,interjected>>

Next ==
    \/ FWaitIdle \/ FQueue \/ FWaitBusy \/ FWaitIdle2 \/ FNeutralize \/ FRequeue
    \/ UserStart \/ UserStop
    \/ RunStart \/ FirstText \/ TtsOpen \/ ToolCall \/ ToolResult \/ ToolEnd
    \/ LLMEnd \/ BotStop

Spec == Init /\ [][Next]_vars

(* ---------------- properties ---------------- *)

\* The order is never retired before a completion read it.
NoSilentLoss == (fpc = "done") => (reads >= 1)

\* No completion reads the order twice.
NoRepeatRecital == reads <= 1

\* The order never goes into a turn that is still in flight. NOT implied by the
\* read-count properties: an order appended during a tool call is read exactly once, by
\* the tool's own answering completion, while the two turns collide.
NoInterjectMidTurn == ~interjected

\* The narrator's side: during a tool call with nobody speaking the gate reads idle, so
\* a progress line can fill the silence. "held" fails this -- the pre-PR dead air.
NoDeadAirDuringTool == (tool /\ ~user /\ ~bot) => ~llm

TypeOK ==
    /\ fpc \in {"waitIdle","queue","waitBusy","waitIdle2","neutralize","done"}
    /\ trigger \in {"absent","live","neutral"}
    /\ run \in {"none","read","text","tts","play"} /\ q \in 0..MaxRuns
    /\ tool \in BOOLEAN /\ interjected \in BOOLEAN /\ sawLive \in BOOLEAN
    /\ turns \in 0..MaxTurns /\ started \in 0..MaxRuns /\ reads \in 0..MaxRuns
=============================================================================
