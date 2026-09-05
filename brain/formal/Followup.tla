-------------------------------- MODULE Followup --------------------------------
(***************************************************************************)
(* teaport — async ask_openclaw follow-up delivery.                        *)
(*                                                                          *)
(*   brain/teaport_brain/followup_gate.py  -- FollowupGate                  *)
(*   brain/teaport_brain/agent_session.py  -- _make_consult_followup        *)
(*                                                                          *)
(* speak_followup(): wait_until_idle() -> rewrite the tool result, append a *)
(* one-shot USER-role trigger ("tell the user now"), queue an LLMRunFrame   *)
(* -> wait_until_delivered() -> neutralise the trigger.                     *)
(*                                                                          *)
(* The trigger is a STANDING ORDER in the context. It must be retired after *)
(* exactly one completion has read it:                                      *)
(*   - retired too early  -> the answer is never spoken (silent loss)       *)
(*   - retired too late   -> a later turn re-executes it (repeat recital,   *)
(*                           the 2026-08-26 08:17 "shop list twice" bug)    *)
(*                                                                          *)
(* MODE selects the design:                                                 *)
(*   "asWritten"    -- gate.wait_until_delivered(): _busy/_idle, i.e. ANY    *)
(*                     activity. Violates NoSilentLoss.                     *)
(*   "gateOnOwn"    -- wait for OUR completion to start, then end.          *)
(*                     Violates NoRepeatRecital.                            *)
(*   "retireOnRead" -- retire AT the read. Satisfies both; this is what      *)
(*                     FollowupTrigger implements (followup_gate.py), armed  *)
(*                     before queue_frames and fired on the first            *)
(*                     LLMTextFrame of the answering completion.             *)
(*                                                                          *)
(* LATCH selects how the gate's _llm flag behaves across a TOOL CALL -- a   *)
(* turn that is two completions with the tool between them:                 *)
(*   "held"              -- pre-PR-#13: the bare call's LLMFullResponseEnd   *)
(*                          is held at the TTS service (empty text makes no  *)
(*                          audio context), so _llm stays latched until the  *)
(*                          answering completion ends.                       *)
(*   "clearedOnToolCall" -- PR #13: FunctionCallInProgressFrame clears _llm. *)
(*                          The gate reads IDLE for the whole tool call and  *)
(*                          the follow-up can be appended into a turn whose  *)
(*                          answering completion has not started. Violates  *)
(*                          NoInterjectMidTurn -- and ONLY that: the trigger  *)
(*                          is read once, by the tool's own answer, so the   *)
(*                          two read-count properties are blind to it.       *)
(*   "turnAware"         -- the fix. _llm is released as above (the narrator *)
(*                          must fill a synchronous consult's silence), and  *)
(*                          a second flag, _turn, stays set from the         *)
(*                          response's start until its answering completion *)
(*                          ends, a result with run_llm=False closes it, or  *)
(*                          an interruption. Only the follow-up injector's   *)
(*                          wait requires ~_turn. Holds everything.          *)
(*                                                                          *)
(* NoDeadAirDuringTool is the narrator's side of the trade: during a tool    *)
(* call with nobody speaking, the gate must read idle. "held" violates it -- *)
(* the pre-PR dead air -- which is why the latch was released in the first  *)
(* place.                                                                    *)
(*                                                                          *)
(* Checked designs and results are in README.md next to this file.          *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxTurns, MaxRuns, MODE, LATCH

\* MODE  \in {"asWritten", "gateOnOwn", "retireOnRead"}
\* LATCH \in {"held", "clearedOnToolCall", "turnAware"}   -- how the gate behaves across a tool call

VARIABLES user, bot, llm, tool, fpc, trigger, run, q, sawLive, reads, started,
          turns, interjected

vars == <<user, bot, llm, tool, fpc, trigger, run, q, sawLive, reads, started,
          turns, interjected>>

Idle  == (~user) /\ (~bot) /\ (~llm)                      \* what the narrator waits for
Clear == Idle /\ (LATCH # "turnAware" \/ run = "none")   \* what the follow-up injector waits for

Init ==
    /\ user = FALSE /\ bot = FALSE /\ llm = FALSE /\ tool = FALSE
    /\ interjected = FALSE
    /\ fpc = "waitIdle"
    /\ trigger = "absent"
    /\ run = "none"
    /\ q = 0
    /\ sawLive = FALSE
    /\ reads = 0
    /\ started = 0
    /\ turns = 0

(* ---------------- speak_followup ---------------- *)

FWaitIdle ==                                   \* await gate.wait_until_idle(turn_free=True)
    /\ fpc = "waitIdle" /\ Clear
    /\ fpc' = "queue"
    /\ UNCHANGED <<user,bot,llm,tool,trigger,run,q,sawLive,reads,started,turns,interjected>>

FQueue ==                                      \* add trigger; queue LLMRunFrame
    /\ fpc = "queue"
    /\ Clear                                   \* no await between the gate returning and the append
    /\ trigger' = "live"
    /\ q' = q + 1                              \* queue_frames() ENQUEUES; it does not cancel
    /\ fpc' = "waitBusy"
    \* Did the gate's "quiet" reading hide a turn that is still in flight?
    /\ interjected' = (interjected \/ run # "none")
    /\ UNCHANGED <<user,bot,llm,tool,run,sawLive,reads,started,turns>>

FWaitBusy ==                                   \* wait_until_delivered() phase 1
    /\ fpc = "waitBusy"
    /\ CASE MODE = "asWritten"    -> ~Idle          \* ANY activity sets _busy
         [] MODE = "gateOnOwn"    -> llm /\ sawLive  \* our completion started
         [] MODE = "retireOnRead" -> reads >= 1     \* the trigger was consumed
    /\ fpc' = "waitIdle2"
    /\ UNCHANGED <<user,bot,llm,tool,trigger,run,q,sawLive,reads,started,turns,interjected>>

FWaitIdle2 ==                                  \* wait_until_delivered() phase 2
    /\ fpc = "waitIdle2"
    /\ CASE MODE = "asWritten"    -> Idle
         [] MODE = "gateOnOwn"    -> ~llm
         [] MODE = "retireOnRead" -> TRUE
    /\ fpc' = "neutralize"
    /\ UNCHANGED <<user,bot,llm,tool,trigger,run,q,sawLive,reads,started,turns,interjected>>

FNeutralize ==                                 \* retire the one-shot
    /\ fpc = "neutralize"
    /\ trigger' = "neutral"
    /\ fpc' = "done"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,sawLive,reads,started,turns,interjected>>

FRequeue ==                                    \* FIX only: barged away -> try again
    /\ MODE \in {"gateOnOwn", "retireOnRead"}
    /\ fpc = "waitBusy" /\ trigger = "live" /\ run = "none" /\ q = 0 /\ Idle
    /\ started < MaxRuns
    /\ q' = 1
    /\ UNCHANGED <<user,bot,llm,tool,fpc,trigger,run,sawLive,reads,started,turns,interjected>>

(* ---------------- the user ---------------- *)

UserStart ==                                   \* barge-in cancels an in-flight turn
    /\ ~user /\ turns < MaxTurns
    /\ user' = TRUE
    /\ IF llm \/ bot \/ tool
         THEN /\ llm' = FALSE /\ bot' = FALSE /\ run' = "none" /\ sawLive' = FALSE
         ELSE UNCHANGED <<llm,bot,run,sawLive>>
    /\ q' = 0                                  \* the interruption flushes queued frames
    /\ tool' = FALSE
    /\ UNCHANGED <<fpc,trigger,reads,started,turns,interjected>>

UserStop ==
    /\ user
    /\ user' = FALSE
    /\ turns' = turns + 1
    /\ q' = IF q < MaxRuns THEN q + 1 ELSE q
    /\ UNCHANGED <<bot,llm,tool,fpc,trigger,run,sawLive,reads,started,interjected>>

(* ---------------- completion + playout ---------------- *)

RunStart ==                                    \* the context is READ here
    /\ q > 0 /\ run = "none" /\ ~llm /\ ~bot
    /\ started < MaxRuns
    /\ q' = q - 1
    /\ run' = "running" /\ llm' = TRUE
    /\ sawLive' = (trigger = "live")
    /\ reads'   = IF trigger = "live" THEN reads + 1 ELSE reads
    /\ trigger' = IF MODE = "retireOnRead" /\ trigger = "live" THEN "neutral" ELSE trigger
    /\ started' = started + 1
    /\ UNCHANGED <<user,bot,tool,fpc,turns,interjected>>

ToolCall ==                        \* FunctionCallInProgressFrame: a bare tool call
    /\ run = "running" /\ llm /\ ~tool
    /\ tool' = TRUE
    \* PR #13, followup_gate.py:159. Pre-PR the end frame was held at the TTS
    \* service (empty text creates no audio context), so _llm stayed latched.
    /\ llm' = IF LATCH \in {"clearedOnToolCall", "turnAware"} THEN FALSE ELSE llm
    /\ UNCHANGED <<user,bot,fpc,trigger,run,q,sawLive,reads,started,turns,interjected>>

ToolEnd ==                         \* the async shape: the result comes back with run_llm=False
    /\ tool /\ run = "running"     \* and NO completion follows -- the turn is over
    /\ tool' = FALSE /\ run' = "none" /\ sawLive' = FALSE
    \* "held": the bare call's End frame never arrives, so _llm stays latched --
    \* the dead air. The other designs read idle here.
    /\ llm' = IF LATCH = "held" THEN llm ELSE FALSE
    /\ UNCHANGED <<user,bot,fpc,trigger,q,reads,started,turns,interjected>>

ToolResult ==                      \* the answering completion -- ANOTHER context read
    /\ tool /\ run = "running"
    /\ tool' = FALSE
    /\ llm' = TRUE
    /\ sawLive' = (trigger = "live")
    /\ reads'   = IF trigger = "live" THEN reads + 1 ELSE reads
    /\ trigger' = IF MODE = "retireOnRead" /\ trigger = "live" THEN "neutral" ELSE trigger
    /\ UNCHANGED <<user,bot,fpc,run,q,started,turns,interjected>>

LLMEnd ==
    /\ run = "running" /\ llm /\ ~tool
    /\ llm' = FALSE /\ bot' = TRUE
    /\ UNCHANGED <<user,tool,fpc,trigger,run,q,sawLive,reads,started,turns,interjected>>

BotStop ==
    /\ bot /\ ~llm /\ run = "running"
    /\ bot' = FALSE /\ run' = "none" /\ sawLive' = FALSE
    /\ UNCHANGED <<user,llm,tool,fpc,trigger,q,reads,started,turns,interjected>>

Next ==
    \/ FWaitIdle \/ FQueue \/ FWaitBusy \/ FWaitIdle2 \/ FNeutralize \/ FRequeue
    \/ UserStart \/ UserStop
    \/ RunStart  \/ ToolCall \/ ToolResult \/ ToolEnd \/ LLMEnd \/ BotStop

Spec == Init /\ [][Next]_vars

\* Fair version, for the liveness check: the answer must actually get out.
FairSpec == Spec /\ WF_vars(Next)

(* ---------------- properties ---------------- *)

\* The trigger is never retired before a completion has actually read it.
NoSilentLoss == (fpc = "done") => (reads >= 1)

\* No completion ever reads the trigger twice -- a live trigger is a standing
\* order, and a second reading is the bot repeating an answer it already gave.
NoRepeatRecital == reads <= 1

\* The follow-up never interjects into a turn that is still in flight. This is the
\* gate's stated job ("don't step on ... the assistant mid-answer about something
\* else") and it is NOT implied by either property above: a trigger appended during
\* a tool call is read exactly once, by the tool's own answering completion, so the
\* read-count properties are satisfied while the two turns collide.
NoInterjectMidTurn == ~interjected

\* The narrator's side: during a tool call with nobody speaking the gate reads
\* idle, so a progress line can fill the silence. "held" fails this -- the
\* pre-PR dead air -- and is why the latch was released at all.
NoDeadAirDuringTool == (tool /\ ~user /\ ~bot) => ~llm

\* Liveness: the consult answer is eventually read by some completion.
EventuallyDelivered == <>(reads >= 1)

TypeOK ==
    /\ fpc \in {"waitIdle","queue","waitBusy","waitIdle2","neutralize","done"}
    /\ trigger \in {"absent","live","neutral"}
    /\ run \in {"none","running"} /\ q \in 0..MaxRuns
    /\ tool \in BOOLEAN /\ interjected \in BOOLEAN
    /\ turns \in 0..MaxTurns /\ started \in 0..MaxRuns /\ reads \in 0..MaxRuns
=============================================================================
