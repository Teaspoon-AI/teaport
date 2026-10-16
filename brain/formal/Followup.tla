-------------------------------- MODULE Followup --------------------------------
(***************************************************************************)
(* teaport — async ask_openclaw follow-up delivery.                        *)
(*                                                                          *)
(*   brain/teaport_brain/followup_gate.py  -- FollowupGate, FollowupTrigger *)
(*   brain/teaport_brain/agent_session.py  -- _make_consult_followup        *)
(*   brain/teaport_brain/transcript_ledger.py -- next_assistant (heard)     *)
(*                                                                          *)
(* speak_followup(): wait for a turn-free window -> post a one-shot         *)
(* USER-role trigger ("tell the user now") -> queue an LLMRunFrame -> the   *)
(* completion that answers from it retires it -> ask the transport and the  *)
(* ledger whether the CALLER heard the answer, and if not, post it again.   *)
(*                                                                          *)
(* The trigger is a STANDING ORDER in the context:                          *)
(*   - retired too early  -> the answer is never spoken (silent loss)       *)
(*   - retired too late   -> a later turn re-executes it (repeat recital,   *)
(*                           the 2026-08-26 08:17 "shop list twice" bug)    *)
(*                                                                          *)
(* MODE selects the design. The first three are the RETIREMENT designs and  *)
(* take "delivered" to mean "a completion read it":                         *)
(*   "asWritten"    -- gate.wait_until_delivered(): _busy/_idle, i.e. ANY    *)
(*                     activity. Violates NoSilentLoss.                     *)
(*   "gateOnOwn"    -- wait for OUR completion to start, then end.          *)
(*                     Violates NoRepeatRecital.                            *)
(*   "retireOnRead" -- retire AT the read. Satisfies both (FollowupTrigger). *)
(*                                                                          *)
(* MODE = "heard" is the DELIVERY LOOP (#33, issue #30), which takes        *)
(* "delivered" to mean "the caller HEARD it" -- at least _MIN_HEARD of it.  *)
(* The read point is the completion's first text (FollowupTrigger), the     *)
(* heard outcome is HEARD | CUT_ABOVE | CUT_BELOW, and only CUT_BELOW (or a *)
(* turn flushed before anything answered) re-arms, up to Attempts times.    *)
(*                                                                          *)
(* REPOST: where a retry puts the trigger back.                             *)
(*   "inPlace" -- the first version: its content restored where it was.     *)
(*                Live 2026-09-10 17:36 -- by then the caller's barge-in is *)
(*                the tail, a buried trigger is not the turn the model      *)
(*                answers, and the retry re-answered the barge-in.          *)
(*   "tail"    -- as shipped (42d0a2b): re-posted at the tail after the     *)
(*                window, the earlier copy retired.                         *)
(* FIX: the parts of the fix for what "tail" still allows (a set; {} is the *)
(* code as shipped):                                                        *)
(*   "void"          -- an interruption VOIDS an armed shot and retires the *)
(*                      copy untold, instead of leaving it for whichever    *)
(*                      completion's text comes next to fire.              *)
(*   "retireUnarmed" -- an attempt abandoned unanswered retires the copy    *)
(*                      untold before it waits for the next window, rather  *)
(*                      than leaving it live with no shot armed.            *)
(*   "unspoken"      -- a reply none of whose audio played (cut before the  *)
(*                      TTS opened, or text that never became audio) is     *)
(*                      heard~0% (Delivery.spoke), not a question for the   *)
(*                      ledger, which charts no turn its TTS never opened.  *)
(*   "ownAudio"      -- Delivery.spoke is our reply's AUDIO -- audio after  *)
(*                      its own TTSStartedFrame -- not a filler line's.     *)
(*   "ownStart"      -- (rejected) spoke at the reply's TTSStartedFrame     *)
(*                      alone: a context that opens and never plays (an     *)
(*                      emoji-only reply, every clause failing) "spoke".    *)
(*   "supersede"     -- a re-post leaves the earlier copy saying only that  *)
(*                      it was superseded, not "already given".             *)
(*                                                                          *)
(* LATCH selects how the gate's _llm flag behaves across a TOOL CALL -- a   *)
(* turn that is two completions with the tool between them:                 *)
(*   "held"              -- pre-PR-#13: _llm latched until the answering     *)
(*                          completion ends. Violates NoDeadAirDuringTool.   *)
(*   "clearedOnToolCall" -- PR #13: FunctionCallInProgressFrame clears _llm. *)
(*                          Violates NoInterjectMidTurn -- and ONLY that.    *)
(*   "turnAware"         -- the fix: _llm released as above, plus _turn for  *)
(*                          the follow-up injector alone. Holds everything.  *)
(*                                                                          *)
(* TIMED switches on the exits that only a timer takes: the gate's max_wait *)
(* give-up (followed by wait_out_claim's bounded wait), a 20 s start timeout*)
(* while a completion is still on its way, the 45 s heard timeout. Off, the *)
(* model assumes each wait ends on the event it waits for, as UserTurn.tla's*)
(* untimed rows do.                                                         *)
(*                                                                          *)
(* Checked designs and results are in README.md next to this file.          *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxTurns, MaxRuns, MODE, LATCH, Attempts, TIMED, REPOST, FIX

Heard == MODE = "heard"
Fixes == {"void", "retireUnarmed", "unspoken", "ownAudio", "ownStart", "supersede"}

VARIABLES
    user, bot, llm, tool,      \* the gate's view: _user, _bot, _llm (+ a tool call in flight)
    run,          \* the completion in flight: none | read (request sent, no text yet) |
                  \*   text (first text produced) | tts (the TTS opened its context: the
                  \*   ledger has a turn) | play (its audio is playing)
    q,            \* LLMRunFrames / committed turns queued, not yet started
    started, turns,
    fpc,          \* speak_followup's program counter
    trigger,      \* the current copy: absent | live | neutral (old modes) |
                  \*   spent (_SPENT, "already given") | untold (_UNTOLD)
    buried,       \* a user message was committed after the current copy: not the tail
    sawLive, sawTail,  \* what the running completion's request carried
    recite,       \* the running completion is answering from the trigger
    reads,        \* old modes: completions that read the live trigger
    interjected,
    claimed,      \* a turn-free window is claimed (FollowupGate._claim, TurnCommitMark)
    attempts,     \* posts so far (the _DELIVERY_ATTEMPTS loop)
    armed, fired, voided,  \* our OneShot in FollowupTrigger
    watch, wspoke, wcut,   \* our Delivery: none | armed | done; .spoke; .interrupted
    lw, lheard, lrec,      \* ledger.next_assistant(): none | waiting | open | played |
                           \*   resolved; the fraction it charted; whether that
                           \*   reply was a recital of the answer
    outcome,      \* how the loop ended: none | spent | untold
    heardOK,      \* ghost: some recital was heard to the end or cut at >= _MIN_HEARD
    repeat,       \* ghost: a recital began after the caller had heard one
    filler,       \* a filler line is playing (tool-ack, narrator: append_to_context=False)
    stale         \* the strongest claim an EARLIER copy makes: none | spent | untold

vars == <<user, bot, llm, tool, run, q, started, turns, fpc, trigger, buried, sawLive,
          sawTail, recite, reads, interjected, claimed, attempts, armed, fired, voided,
          watch, wspoke, wcut, lw, lheard, lrec, outcome, heardOK, repeat, filler, stale>>
loopVars == <<attempts, armed, fired, voided, watch, wspoke, wcut, lw, lheard, lrec,
              outcome, filler, stale>>

Idle  == (~user) /\ (~bot) /\ (~llm)                      \* what the narrator waits for
\* What the follow-up injector waits for: wait_until_idle(turn_free=True). A claimed
\* window (ours, or a committed turn's -- TurnCommitMark) is not turn-free; the old
\* modes predate the claim. The claim is ONE flag in the gate, and the first completion
\* to start releases it: with two queued (a frame of ours behind a barge-in, and the
\* caller's commit) the second runs unclaimed. It starts milliseconds after the first
\* ends, inside the 0.7 s debounce, so untimed no window opens while one is queued;
\* TIMED drops that assumption.
Clear == /\ Idle
         /\ (LATCH # "turnAware" \/ run = "none")
         /\ (~Heard \/ ~claimed)
         /\ (~Heard \/ TIMED \/ q = 0)

Init ==
    /\ user = FALSE /\ bot = FALSE /\ llm = FALSE /\ tool = FALSE
    /\ run = "none" /\ q = 0 /\ started = 0 /\ turns = 0
    /\ fpc = "waitIdle"
    /\ trigger = "absent" /\ buried = FALSE
    /\ sawLive = FALSE /\ sawTail = FALSE /\ recite = FALSE
    /\ reads = 0 /\ interjected = FALSE /\ claimed = FALSE
    /\ attempts = 0 /\ armed = FALSE /\ fired = FALSE /\ voided = FALSE
    /\ watch = "none" /\ wspoke = FALSE /\ wcut = FALSE
    /\ lw = "none" /\ lheard = "none" /\ lrec = FALSE
    /\ outcome = "none" /\ heardOK = FALSE /\ repeat = FALSE
    /\ filler = FALSE /\ stale = "none"

(* =============== speak_followup, the retirement designs (old modes) =============== *)

FWaitIdle ==                                   \* await gate.wait_until_idle(turn_free=True)
    /\ ~Heard
    /\ fpc = "waitIdle" /\ Clear
    /\ fpc' = "queue"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,trigger,buried,sawLive,sawTail,
                   recite,reads,interjected,claimed,heardOK,repeat>>
    /\ UNCHANGED loopVars

FQueue ==                                      \* add trigger; queue LLMRunFrame
    /\ ~Heard
    /\ fpc = "queue"
    /\ Clear                                   \* no await between the gate returning and the append
    /\ trigger' = "live"
    /\ q' = q + 1                              \* queue_frames() ENQUEUES; it does not cancel
    /\ fpc' = "waitBusy"
    \* Did the gate's "quiet" reading hide a turn that is still in flight?
    /\ interjected' = (interjected \/ run # "none")
    /\ UNCHANGED <<user,bot,llm,tool,run,started,turns,buried,sawLive,sawTail,recite,
                   reads,claimed,heardOK,repeat>>
    /\ UNCHANGED loopVars

FWaitBusy ==                                   \* wait_until_delivered() phase 1
    /\ ~Heard
    /\ fpc = "waitBusy"
    /\ CASE MODE = "asWritten"    -> ~Idle          \* ANY activity sets _busy
         [] MODE = "gateOnOwn"    -> llm /\ sawLive  \* our completion started
         [] MODE = "retireOnRead" -> reads >= 1     \* the trigger was consumed
    /\ fpc' = "waitIdle2"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,trigger,buried,sawLive,sawTail,
                   recite,reads,interjected,claimed,heardOK,repeat>>
    /\ UNCHANGED loopVars

FWaitIdle2 ==                                  \* wait_until_delivered() phase 2
    /\ ~Heard
    /\ fpc = "waitIdle2"
    /\ CASE MODE = "asWritten"    -> Idle
         [] MODE = "gateOnOwn"    -> ~llm
         [] MODE = "retireOnRead" -> TRUE
    /\ fpc' = "neutralize"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,trigger,buried,sawLive,sawTail,
                   recite,reads,interjected,claimed,heardOK,repeat>>
    /\ UNCHANGED loopVars

FNeutralize ==                                 \* retire the one-shot
    /\ ~Heard
    /\ fpc = "neutralize"
    /\ trigger' = "neutral"
    /\ fpc' = "done"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,buried,sawLive,sawTail,recite,
                   reads,interjected,claimed,heardOK,repeat>>
    /\ UNCHANGED loopVars

FRequeue ==                                    \* barged away -> try again
    /\ MODE \in {"gateOnOwn", "retireOnRead"}
    /\ fpc = "waitBusy" /\ trigger = "live" /\ run = "none" /\ q = 0 /\ Idle
    /\ started < MaxRuns
    /\ q' = 1
    /\ UNCHANGED <<user,bot,llm,tool,run,started,turns,fpc,trigger,buried,sawLive,sawTail,
                   recite,reads,interjected,claimed,heardOK,repeat>>
    /\ UNCHANGED loopVars

(* =============== speak_followup, the delivery loop (heard modes) =============== *)

\* The window, the post and the arming, as one step: wait_until_idle(turn_free=True)
\* claims the window and returns, and from there to `await task.queue_frames` the code
\* runs without an await -- _post_trigger (retire the earlier copy, append the new one),
\* the loop's top, ledger.next_assistant(), gate.watch_delivery(), retirer.arm().
Post(viaWindow) ==
    /\ attempts < Attempts
    /\ claimed' = (viaWindow \/ claimed)    \* a True window is claimed; a give-up claims nothing
    /\ interjected' = (interjected \/ run # "none")
    /\ trigger' = "live"
    \* _post_trigger retires the earlier copy first -- with _retire()'s default, _SPENT
    \* ("already given to the user"), whatever it said before; FIX "supersede" leaves a
    \* note that only points at the newer copy.
    /\ stale' = IF trigger \in {"live", "spent", "untold"} /\ "supersede" \notin FIX
                  THEN "spent" ELSE stale
    \* Re-posted at the tail -- except the first version of the retry, which restored
    \* the copy where it was, below whatever the caller said meanwhile.
    /\ buried' = IF REPOST = "inPlace" /\ attempts > 0 THEN buried ELSE FALSE
    /\ attempts' = attempts + 1
    /\ armed' = TRUE /\ fired' = FALSE /\ voided' = FALSE
    /\ watch' = "armed" /\ wspoke' = FALSE /\ wcut' = FALSE
    /\ lw' = "waiting" /\ lheard' = "none" /\ lrec' = FALSE
    /\ fpc' = "queue"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,sawLive,sawTail,recite,reads,
                   outcome,heardOK,repeat,filler>>

FWindow ==
    /\ Heard /\ fpc \in {"waitIdle", "retry"} /\ Clear
    /\ Post(TRUE)

\* TIMED: wait_until_idle ran out (max_wait) and wait_out_claim's bounded wait ran out
\* too: the injector goes ahead into whatever is in flight. By design ("so a relentlessly
\* chatty conversation can't strand the answer").
FGiveUp ==
    /\ Heard /\ TIMED /\ fpc \in {"waitIdle", "retry"} /\ ~Clear
    /\ Post(FALSE)

\* await task.queue_frames([LLMRunFrame()]). Its context frame claims the window at
\* TurnCommitMark, which the model takes as immediate -- as it does a caller's commit
\* (UserStop): a frame reaches the LLM's door in milliseconds, against a 0.7 s debounce.
\* That matters after a barge-in: a frame queued just BEHIND the interruption is not
\* flushed, and it runs -- but before any later window can open, not after.
FQueueRun ==
    /\ Heard /\ fpc = "queue"
    /\ q' = IF q < MaxRuns THEN q + 1 ELSE q
    /\ claimed' = TRUE
    /\ fpc' = "await"
    /\ UNCHANGED <<user,bot,llm,tool,run,started,turns,trigger,buried,sawLive,sawTail,
                   recite,reads,interjected,heardOK,repeat>>
    /\ UNCHANGED loopVars

FFired ==                                      \* shot.fired: a completion answered from it
    /\ Heard /\ fpc = "await" /\ fired
    /\ fpc' = "heard"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,trigger,buried,sawLive,sawTail,
                   recite,reads,interjected,claimed,heardOK,repeat>>
    /\ UNCHANGED loopVars

\* Give up on this attempt before anything answered: disarm, drop the watch, cancel the
\* ledger waiter; retry at the next window, or -- out of attempts -- retire it untold.
\* As shipped the copy stays LIVE while it waits for that window ("still live and
\* still accurate"); FIX "retireUnarmed" retires it untold first, so no completion reads a live
\* trigger that no shot will retire.
Abandon ==
    /\ armed' = FALSE /\ watch' = "none" /\ lw' = "none"
    /\ IF attempts < Attempts
         THEN /\ fpc' = "retry"
              /\ trigger' = IF "retireUnarmed" \in FIX /\ trigger = "live" THEN "untold" ELSE trigger
              /\ UNCHANGED outcome
         ELSE /\ fpc' = "done" /\ outcome' = "untold"
              /\ trigger' = IF trigger = "live" THEN "untold" ELSE trigger
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,buried,sawLive,sawTail,recite,
                   reads,interjected,claimed,attempts,fired,voided,wspoke,wcut,lheard,lrec,
                   heardOK,repeat,filler,stale>>

\* The 20 s start timeout. Untimed it fires only when nothing can still answer: no
\* completion running or queued and the caller not mid-turn (whose commit may queue
\* one). TIMED, it can also overtake a completion that is merely slow.
FStartTimeout ==
    /\ Heard /\ fpc = "await" /\ ~fired /\ ~voided
    /\ TIMED \/ (q = 0 /\ run = "none" /\ ~user)
    /\ Abandon

FVoided ==                                     \* FIX "void": the interruption voided the shot
    /\ Heard /\ fpc = "await" /\ voided
    /\ Abandon

Finish(o) ==
    /\ fpc' = "done" /\ outcome' = o
    /\ trigger' = IF o = "untold" THEN "untold" ELSE trigger
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,buried,sawLive,sawTail,recite,
                   reads,interjected,claimed,attempts,armed,fired,voided,watch,wspoke,wcut,
                   lw,lheard,lrec,heardOK,repeat,filler,stale>>

\* Below _MIN_HEARD: post it again at the next window, or, on the last attempt, leave the
\* note that says it never arrived. The copy stays as the read retired it (_SPENT) while
\* the loop waits.
Below ==
    IF attempts < Attempts
      THEN /\ fpc' = "retry"
           /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,trigger,buried,sawLive,
                          sawTail,recite,reads,interjected,claimed,heardOK,repeat>>
           /\ UNCHANGED loopVars
      ELSE Finish("untold")

FPlayed ==                                     \* not watch.interrupted: played to the end
    /\ Heard /\ fpc = "heard" /\ watch = "done" /\ ~wcut
    /\ Finish("spent")

FCut ==                                        \* watch.interrupted: ask the ledger
    /\ Heard /\ fpc = "heard" /\ watch = "done" /\ wcut
    /\ IF "unspoken" \in FIX /\ ~wspoke
         THEN Below                            \* no audio of it ever played: heard~0%
         ELSE /\ lw = "resolved"
              /\ IF lheard \in {"full", "above"} THEN Finish("spent") ELSE Below

FChartTimeout ==                               \* _DELIVERY_CHART_TIMEOUT: "leaving it as delivered"
    /\ Heard /\ fpc = "heard" /\ watch = "done" /\ wcut
    /\ ~("unspoken" \in FIX /\ ~wspoke)
    /\ lw # "resolved"
    /\ Finish("spent")

\* _DELIVERY_HEARD_TIMEOUT. Untimed it fires only when nothing can settle the watch any
\* more: a completion that produced text and then no audio (RunQuiet) leaves it waiting
\* for a BotStopped or an interruption that never comes. FIX "unspoken": with nothing of
\* ours played that is heard~0%, not "leaving it as delivered".
FHeardTimeout ==
    /\ Heard /\ fpc = "heard" /\ watch = "armed"
    /\ TIMED \/ (run = "none" /\ q = 0 /\ ~user /\ ~bot)
    /\ IF "unspoken" \in FIX /\ ~wspoke THEN Below ELSE Finish("spent")

(* ---------------- the user ---------------- *)

\* The caller's turn starts: an InterruptionFrame. It cancels the completion in flight
\* and flushes queued frames; the gate settles every watched Delivery as interrupted
\* (spoken or not), releases the claim; the ledger charts the cut reply on the spot.
UserStart ==
    /\ ~user /\ turns < MaxTurns
    /\ user' = TRUE
    /\ llm' = FALSE /\ bot' = FALSE /\ run' = "none" /\ tool' = FALSE
    /\ sawLive' = FALSE /\ sawTail' = FALSE /\ recite' = FALSE /\ filler' = FALSE
    /\ q' = 0
    /\ claimed' = (claimed /\ ~Heard)
    /\ IF Heard /\ watch = "armed"
         THEN watch' = "done" /\ wcut' = TRUE
         ELSE UNCHANGED <<watch, wcut>>
    \* FIX "void": FollowupTrigger voids the armed shot and the copy is retired untold.
    /\ IF Heard /\ "void" \in FIX /\ armed
         THEN /\ armed' = FALSE /\ voided' = TRUE
              /\ trigger' = IF trigger = "live" THEN "untold" ELSE trigger
         ELSE UNCHANGED <<armed, voided, trigger>>
    \* How much of the cut reply played: nothing before its audio started, either side
    \* of _MIN_HEARD after.
    /\ \E frac \in (IF run = "play" THEN {"above", "below"} ELSE {"below"}):
         /\ heardOK' = (heardOK \/ (Heard /\ recite /\ run = "play" /\ frac = "above"))
         /\ IF lw = "open"
              THEN lw' = "resolved" /\ lheard' = frac
              ELSE UNCHANGED <<lw, lheard>>
    /\ UNCHANGED <<started,turns,fpc,buried,reads,interjected,attempts,fired,wspoke,lrec,
                   outcome,repeat,stale>>

\* The caller's turn ends. With words, they are committed: appended to the context (so
\* whatever copy is there is no longer the tail), a completion queued and the window
\* claimed (TurnCommitMark). An empty final -- a 0-char decode, noise -- commits nothing:
\* push_aggregation returns "" and no context frame goes down. The old modes always
\* commit.
UserStop ==
    /\ user
    /\ user' = FALSE
    /\ turns' = turns + 1
    /\ \E words \in (IF Heard THEN BOOLEAN ELSE {TRUE}):
         /\ q' = IF words /\ q < MaxRuns THEN q + 1 ELSE q
         /\ buried' = (buried \/ (Heard /\ words /\ trigger # "absent"))
         /\ claimed' = (claimed \/ (Heard /\ words))
    /\ UNCHANGED <<bot,llm,tool,run,started,fpc,trigger,sawLive,sawTail,recite,reads,
                   interjected,heardOK,repeat>>
    /\ UNCHANGED loopVars

(* ---------------- completion + playout ---------------- *)

\* LLMFullResponseStartFrame, and the request carrying the context. The old modes count
\* the read and retire here; the delivery loop retires at the first text.
RunStart ==
    /\ q > 0 /\ run = "none" /\ ~llm /\ ~bot
    /\ started < MaxRuns
    /\ q' = q - 1
    /\ run' = "read" /\ llm' = TRUE
    /\ sawLive' = (trigger = "live")
    /\ sawTail' = ~buried
    /\ reads'   = IF ~Heard /\ trigger = "live" THEN reads + 1 ELSE reads
    /\ trigger' = IF MODE = "retireOnRead" /\ trigger = "live" THEN "neutral" ELSE trigger
    /\ started' = started + 1
    /\ claimed' = (claimed /\ ~Heard)          \* the claimant's completion has started
    /\ UNCHANGED <<user,bot,tool,turns,fpc,buried,recite,interjected,heardOK,repeat>>
    /\ UNCHANGED loopVars

\* The first LLMTextFrame. FollowupTrigger fires EVERY armed shot, whichever completion
\* this is. What the completion says: a trigger at the tail is the turn it answers; one
\* buried under the caller's words may be answered or not (both happened live -- 08:17
\* recited a buried one, 17:36 answered the caller instead); none, no recital.
FirstText ==
    /\ run = "read" /\ llm /\ ~tool
    /\ run' = "text"
    /\ \E r \in (IF sawLive THEN (IF sawTail THEN {TRUE} ELSE BOOLEAN) ELSE {FALSE}):
         /\ recite' = r
         /\ repeat' = (repeat \/ (r /\ heardOK))
    /\ IF Heard /\ armed
         THEN /\ armed' = FALSE /\ fired' = TRUE
              /\ trigger' = IF trigger = "live" THEN "spent" ELSE trigger
         ELSE UNCHANGED <<armed, fired, trigger>>
    /\ UNCHANGED <<user,bot,llm,tool,q,started,turns,fpc,buried,sawLive,sawTail,reads,
                   interjected,claimed,heardOK,attempts,voided,watch,wspoke,wcut,lw,lheard,
                   lrec,outcome,filler,stale>>

TtsOpen ==                                     \* the TTS opens its context: a ledger turn
    /\ run = "text"
    /\ run' = "tts"
    /\ IF lw = "waiting" THEN lw' = "open" /\ lrec' = recite ELSE UNCHANGED <<lw, lrec>>
    \* FIX "ownStart": the reply's TTSStartedFrame passing the gate marks it spoke.
    /\ wspoke' = (wspoke \/ (watch = "armed" /\ "ownStart" \in FIX))
    /\ UNCHANGED <<user,bot,llm,tool,q,started,turns,fpc,trigger,buried,sawLive,sawTail,
                   recite,reads,interjected,claimed,heardOK,repeat,attempts,armed,fired,
                   voided,watch,wcut,lheard,outcome,filler,stale>>

ToolCall ==                        \* FunctionCallInProgressFrame: a bare tool call
    /\ run = "read" /\ llm /\ ~tool
    /\ tool' = TRUE
    \* PR #13, followup_gate.py. Pre-PR the end frame was held at the TTS service
    \* (empty text creates no audio context), so _llm stayed latched.
    /\ llm' = IF LATCH \in {"clearedOnToolCall", "turnAware"} THEN FALSE ELSE llm
    /\ UNCHANGED <<user,bot,run,q,started,turns,fpc,trigger,buried,sawLive,sawTail,recite,
                   reads,interjected,claimed,heardOK,repeat>>
    /\ UNCHANGED loopVars

ToolEnd ==                         \* the async shape: the result comes back with run_llm=False
    /\ tool /\ run = "read"        \* and NO completion follows -- the turn is over
    /\ tool' = FALSE /\ run' = "none" /\ sawLive' = FALSE /\ sawTail' = FALSE
    \* "held": the bare call's End frame never arrives, so _llm stays latched --
    \* the dead air. The other designs read idle here.
    /\ llm' = IF LATCH = "held" THEN llm ELSE FALSE
    /\ UNCHANGED <<user,bot,q,started,turns,fpc,trigger,buried,recite,reads,interjected,
                   claimed,heardOK,repeat>>
    /\ UNCHANGED loopVars

ToolResult ==                      \* the answering completion -- ANOTHER context read
    /\ tool /\ run = "read"
    /\ tool' = FALSE
    /\ llm' = TRUE
    /\ sawLive' = (trigger = "live")
    /\ sawTail' = ~buried          \* the call and its result are this turn's, not a burial
    /\ reads'   = IF ~Heard /\ trigger = "live" THEN reads + 1 ELSE reads
    /\ trigger' = IF MODE = "retireOnRead" /\ trigger = "live" THEN "neutral" ELSE trigger
    /\ UNCHANGED <<user,bot,run,q,started,turns,fpc,buried,recite,interjected,claimed,
                   heardOK,repeat>>
    /\ UNCHANGED loopVars

LLMEnd ==                          \* the response ends; its audio starts (BotStartedSpeaking)
    /\ run = "tts" /\ llm /\ ~tool /\ ~filler
    /\ llm' = FALSE /\ bot' = TRUE /\ run' = "play"
    /\ wspoke' = (wspoke \/ watch = "armed")
    /\ UNCHANGED <<user,tool,q,started,turns,fpc,trigger,buried,sawLive,sawTail,recite,
                   reads,interjected,claimed,heardOK,repeat,attempts,armed,fired,voided,
                   watch,wcut,lw,lheard,lrec,outcome,filler,stale>>

BotStop ==                         \* BotStoppedSpeaking: played to the end
    /\ bot /\ ~llm /\ run = "play"
    /\ bot' = FALSE /\ run' = "none" /\ sawLive' = FALSE /\ sawTail' = FALSE
    /\ recite' = FALSE
    /\ heardOK' = (heardOK \/ (Heard /\ recite))
    \* The gate settles a watched reply that had started; the ledger charts a reply
    \* that simply finished only LATER (ChartLate).
    /\ IF watch = "armed" /\ wspoke THEN watch' = "done" /\ wcut' = FALSE
                                    ELSE UNCHANGED <<watch, wcut>>
    /\ IF lw = "open" THEN lw' = "played" ELSE UNCHANGED lw
    /\ UNCHANGED <<user,llm,tool,q,started,turns,fpc,trigger,buried,reads,interjected,
                   claimed,repeat,attempts,armed,fired,voided,wspoke,lheard,lrec,outcome,
                   filler,stale>>

ChartLate ==                       \* the ledger charts a finished reply at the next bot activity
    /\ lw = "played"
    /\ lw' = "resolved" /\ lheard' = "full"
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,fpc,trigger,buried,sawLive,
                   sawTail,recite,reads,interjected,claimed,heardOK,repeat,attempts,armed,
                   fired,voided,watch,wspoke,wcut,lrec,outcome,filler,stale>>

\* A completion whose text never becomes audio: the guard emptied it (#70), it has no
\* speech (has_speech), or the LLM failed after its first token. The TTS opens no
\* context and the turn ends.
RunQuiet ==
    /\ Heard /\ run = "text" /\ llm /\ ~tool
    /\ run' = "none" /\ llm' = FALSE /\ recite' = FALSE
    /\ sawLive' = FALSE /\ sawTail' = FALSE
    /\ UNCHANGED <<user,bot,tool,q,started,turns,fpc,trigger,buried,reads,interjected,
                   claimed,heardOK,repeat>>
    /\ UNCHANGED loopVars

\* The TTS opened a context for the reply and no audio ever came of it: the text was
\* emoji or punctuation only (pipecat opens a context for any non-blank text), or the
\* engine failed every clause. The ledger charts it heard nothing, or not at all.
TtsQuiet ==
    /\ Heard /\ run = "tts" /\ llm /\ ~tool
    /\ run' = "none" /\ llm' = FALSE /\ recite' = FALSE
    /\ sawLive' = FALSE /\ sawTail' = FALSE
    /\ IF lw = "open" THEN \/ lw' = "resolved" /\ lheard' = "below"
                           \/ UNCHANGED <<lw, lheard>>
                      ELSE UNCHANGED <<lw, lheard>>
    /\ UNCHANGED <<user,bot,tool,q,started,turns,fpc,trigger,buried,reads,interjected,
                   claimed,heardOK,repeat,attempts,armed,fired,voided,watch,wspoke,wcut,
                   lrec,outcome,filler,stale>>

\* FollowupTrigger fires on ANY alnum LLMTextFrame while a shot is armed, not only a
\* completion's first. A shot armed while a completion is mid-stream (only a give-up
\* posts then) is fired by that completion's next token, which never read it.
MoreText ==
    /\ Heard /\ run \in {"text", "tts"} /\ llm /\ armed
    /\ armed' = FALSE /\ fired' = TRUE
    /\ trigger' = IF trigger = "live" THEN "spent" ELSE trigger
    /\ UNCHANGED <<user,bot,llm,tool,run,q,started,turns,fpc,buried,sawLive,sawTail,recite,
                   reads,interjected,claimed,heardOK,repeat,attempts,voided,watch,wspoke,
                   wcut,lw,lheard,lrec,outcome,filler,stale>>

\* A filler line plays while a tool runs: the tool-ack, or the narrator filling the
\* silence (append_to_context=False). The transport reports it like any other audio:
\* BotStartedSpeaking, then BotStoppedSpeaking. As shipped the gate marks every watched
\* Delivery spoke on the first, and settles it as PLAYED on the second, before the
\* answer has a sample of audio. FIX "ownAudio": only our reply's own context
\* (TTSStartedFrame without append_to_context=False) marks it spoke.
FillerStart ==
    /\ Heard /\ tool /\ ~bot /\ ~filler
    /\ bot' = TRUE /\ filler' = TRUE
    /\ wspoke' = (wspoke \/ (watch = "armed" /\ "ownAudio" \notin FIX /\ "ownStart" \notin FIX))
    /\ UNCHANGED <<user,llm,tool,run,q,started,turns,fpc,trigger,buried,sawLive,sawTail,
                   recite,reads,interjected,claimed,heardOK,repeat,attempts,armed,fired,
                   voided,watch,wcut,lw,lheard,lrec,outcome,stale>>

FillerStop ==
    /\ filler /\ bot
    /\ bot' = FALSE /\ filler' = FALSE
    /\ IF watch = "armed" /\ wspoke THEN watch' = "done" /\ wcut' = FALSE
                                    ELSE UNCHANGED <<watch, wcut>>
    /\ UNCHANGED <<user,llm,tool,run,q,started,turns,fpc,trigger,buried,sawLive,sawTail,
                   recite,reads,interjected,claimed,heardOK,repeat,attempts,armed,fired,
                   voided,wspoke,lw,lheard,lrec,outcome,stale>>

Next ==
    \/ RunQuiet \/ TtsQuiet \/ MoreText \/ FillerStart \/ FillerStop
    \/ FWaitIdle \/ FQueue \/ FWaitBusy \/ FWaitIdle2 \/ FNeutralize \/ FRequeue
    \/ FWindow \/ FGiveUp \/ FQueueRun \/ FFired \/ FStartTimeout \/ FVoided
    \/ FPlayed \/ FCut \/ FChartTimeout \/ FHeardTimeout
    \/ UserStart \/ UserStop
    \/ RunStart \/ FirstText \/ TtsOpen \/ ToolCall \/ ToolResult \/ ToolEnd
    \/ LLMEnd \/ BotStop \/ ChartLate

Spec == Init /\ [][Next]_vars

\* Fair version, for the liveness check: the answer must actually get out.
FairSpec == Spec /\ WF_vars(Next)

(* ---------------- properties ---------------- *)

\* The trigger is never retired as delivered when the answer did not get through. For the
\* retirement designs "delivered" means a completion read it; for the delivery loop it
\* means the caller heard it (to the end, or cut at >= _MIN_HEARD). Running out of
\* attempts is not a loss in this sense: the note left behind says it never arrived, and
\* the answer survives in the rewritten tool result.
NoSilentLoss ==
    IF Heard THEN (fpc = "done" /\ (outcome = "spent" \/ stale = "spent")) => heardOK
             ELSE (fpc = "done") => (reads >= 1)

\* The answer is never recited again once it landed. For the retirement designs: no
\* completion reads the trigger twice. For the delivery loop, which retries on purpose:
\* no recital begins after the caller has heard one.
NoRepeatRecital ==
    IF Heard THEN ~repeat ELSE reads <= 1

\* The loop never ends with a standing order in the context: past the attempt budget the
\* trigger is retired regardless.
NoStandingOrder == (Heard /\ fpc = "done") => trigger # "live"

\* A live trigger is always one a shot will retire: whatever completion reads it next
\* takes it out. (Between a post and its queue_frames the shot is already armed.) The
\* mechanism NoRepeatRecital rests on, stated directly.
LiveIsArmed == (Heard /\ trigger = "live") => armed

\* The follow-up never interjects into a turn that is still in flight. NOT implied by the
\* read-count properties: a trigger appended during a tool call is read exactly once, by
\* the tool's own answering completion, while the two turns collide.
NoInterjectMidTurn == ~interjected

\* The narrator's side: during a tool call with nobody speaking the gate reads idle, so
\* a progress line can fill the silence. "held" fails this -- the pre-PR dead air.
NoDeadAirDuringTool == (tool /\ ~user /\ ~bot) => ~llm

\* Liveness: the consult answer is eventually read by some completion.
EventuallyDelivered == <>(reads >= 1)

TypeOK ==
    /\ fpc \in {"waitIdle","queue","waitBusy","waitIdle2","neutralize","await","heard",
                "retry","done"}
    /\ trigger \in {"absent","live","neutral","spent","untold"}
    /\ run \in {"none","read","text","tts","play"} /\ q \in 0..MaxRuns
    /\ tool \in BOOLEAN /\ interjected \in BOOLEAN /\ buried \in BOOLEAN
    /\ turns \in 0..MaxTurns /\ started \in 0..MaxRuns /\ reads \in 0..MaxRuns
    /\ attempts \in 0..Attempts
    /\ watch \in {"none","armed","done"}
    /\ lw \in {"none","waiting","open","played","resolved"}
    /\ lheard \in {"none","full","above","below"}
    /\ outcome \in {"none","spent","untold"}
    /\ filler \in BOOLEAN /\ stale \in {"none","spent","untold"}
=============================================================================
