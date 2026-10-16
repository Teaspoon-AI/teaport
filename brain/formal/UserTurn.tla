-------------------------------- MODULE UserTurn --------------------------------
(***************************************************************************)
(* teaport — whether the user can interrupt the bot AT ALL.                 *)
(*                                                                          *)
(*   brain/teaport_brain/endpointing.py    -- keep_barge_in_reachable()     *)
(*   brain/teaport_brain/agent_session.py  -- UserTurnStrategies wiring     *)
(*   pipecat/turns/user_turn_controller.py -- the machine modelled here     *)
(*                                                                          *)
(* Every other model in this directory takes barge-in as a FREE ENVIRONMENT *)
(* ACTION. `Ledger.tla` and `LedgerPlayout.tla` both write                  *)
(*                                                                          *)
(*     Interrupt ==                                                         *)
(*         /\ interrupts < MaxInterrupts                                    *)
(*         /\ window \/ Synthesizing \/ Streaming                           *)
(*                                                                          *)
(* -- a bounding counter and "there is something to interrupt", nothing     *)
(* else -- and `Followup.tla`'s UserStart is the same shape. They model     *)
(* what happens AFTER an interruption and assume one is always available.   *)
(* Nothing modelled the machinery that decides whether the user gets one,   *)
(* which is where it actually failed. This module is that assumption's      *)
(* discharge: it is the only place `Interrupt`'s enabling condition is a    *)
(* conclusion rather than an axiom.                                         *)
(*                                                                          *)
(* The machine. Barge-in in this brain is a user turn START and nothing     *)
(* else: MinWordsUserTurnStartStrategy counts the words, the aggregator     *)
(* broadcasts the InterruptionFrame from on_user_turn_started. So the       *)
(* controller's two guards decide it between them:                          *)
(*                                                                          *)
(*   _trigger_user_turn_start  -- "Prevent two consecutive user turn        *)
(*                                starts": refuses while a turn is open.    *)
(*   _trigger_user_turn_stop   -- "Never finalize while the user is         *)
(*                                audibly speaking": refuses while VAD      *)
(*                                says speaking.                            *)
(*                                                                          *)
(* and trigger_user_turn_stopped() is TWO events across the second guard:   *)
(*                                                                          *)
(*     await self.trigger_user_turn_inference_triggered()  \* LLM runs      *)
(*     await self.trigger_user_turn_finalized(...)         \* refusable     *)
(*                                                                          *)
(* The first awaits through push_aggregation() -> push_context_frame() ->   *)
(* push_frame() across the whole pipeline; when the trigger came from the   *)
(* stop strategy's own _timeout_handler TASK, the aggregator's input task   *)
(* runs a queued VADUserStartedSpeakingFrame in that gap. `Resume` below    *)
(* is that gap, and it is the only interleaving the barge-in rows need    *)
(* (the commit overtaking its own interruption, `CutLands`, is the other,   *)
(* for the context and the hold).                                           *)
(*                                                                          *)
(* MODE = "asWritten"    -- stock pipecat (identical on 1.5.0/1.7.0/1.8.1). *)
(* MODE = "retryOnQuiet" -- keep_barge_in_reachable: a finalization refused *)
(*                          after its inference ran is re-applied at the    *)
(*                          user's next quiet moment -- and, words aggregated *)
(*                          since, committed there as a second message      *)
(*                          (the code until 2026-10-15).                    *)
(* MODE = "retryMerges"  -- as shipped (CutReplyFence.hold_commits'         *)
(*                          unplayed_reply): a commit while the gate is     *)
(*                          still armed -- that second commit -- first      *)
(*                          drops the unplayed reply and merges into the    *)
(*                          unanswered message.                             *)
(* MODE = "retryMergesAtEnd" -- its first cut (e62e67a): the session-end    *)
(*                          commit of leftover words did it too, dropping   *)
(*                          the queued reply the End was behind.            *)
(*                                                                          *)
(* The speculative reply (speculate.py, TEAPORT_SPECULATIVE_REPLY). When   *)
(* the caller's words have settled on a turn the stop strategy has not      *)
(* concluded on -- a VAD stop answered INCOMPLETE, the ceiling running, and  *)
(* (since #43) the STT holding the segment open through it -- the LLM is    *)
(* asked NOW, out of band, against a snapshot of the context with those     *)
(* words as the user message: the settled INTERIM (or a final, where one     *)
(* lands before the commit). At the commit the LLM service is offered that  *)
(* stream instead of opening its own. The controller's state is never       *)
(* touched: `SpecStart` reads userTurn and writes nothing the guards read,  *)
(* which is why the two barge-in properties are re-checked with it on       *)
(* rather than assumed.                                                     *)
(*                                                                          *)
(* What the speculation must not do is speak a reply generated against a    *)
(* context the commit no longer has. Two things can move under the         *)
(* snapshot. The context before the user message: other writers --         *)
(* HeardContextCorrector's truncation of the cut reply on the               *)
(* LLMContextFrame itself was one, and the first miss seen live, until      *)
(* speculate.py moved it ahead of the snapshot; MemoryRecall's note,        *)
(* injected on the final, is one for a snapshot taken on the interim.       *)
(* `CtxChange` is any writer at all: what the code prevents today is not    *)
(* what the machine prevents. And the user message itself: the words the   *)
(* commit carries are the FINAL's, which the snapshot did not have -- a     *)
(* lagging delta past the settle window, or a final whose re-decode differs *)
(* from the last interim by a comma or a capital, landing in the same step  *)
(* that commits. `Retext` is that, and it leaves the speculation live: the  *)
(* strategy cancelling on a changed interim it sees in time is a            *)
(* refinement the properties do not rely on.                                *)
(*                                                                          *)
(* SPEC = "off"       -- no speculation; the four MODE rows are unchanged.  *)
(* SPEC = "byText"    -- promote when the committed TEXT is what was asked. *)
(*                       Rejected: NoStaleReply falls to CtxChange.         *)
(* SPEC = "byHistory" -- promote when the context BEFORE the user message   *)
(*                       is what was asked. Rejected: NoStaleReply falls to *)
(*                       Retext -- the hazard the interim snapshot adds.    *)
(* SPEC = "byContext" -- promote only when the WHOLE context, user message  *)
(*                       included, equals the snapshot (what speculate.py   *)
(*                       does, forgiving only whitespace at the message's   *)
(*                       ends, which is not a difference in its words).     *)
(*                                                                          *)
(* The reply hold (reply_hold.py, #85). A commit's reply plays ~0.2 s after *)
(* the commit, and a caller who was only pausing resumes inside that gap:   *)
(* their words interrupt only once transcribed, so the reply starts over    *)
(* them about a fragment. ReplyHoldGate, armed by every commit until the    *)
(* reply's first audio, pauses its queue when the caller speaks; their      *)
(* words (a new turn) drop the held reply and ask for the next commit to be *)
(* merged into the unanswered one; no words release it.                     *)
(*                                                                          *)
(* HOLD = "none"          -- before #85: nothing looks at the line.         *)
(* HOLD = "gate"          -- reply_hold.py as shipped.                      *)
(* HOLD = "gateNoMerge"   -- the hold without TurnMerge.                    *)
(* HOLD = "gateNoResume"  -- the gate as first written: a drop left the     *)
(*                           queue to pipecat's reset, which does not lift  *)
(*                           the pause when the frame in hand is            *)
(*                           Uninterruptible.                               *)
(* HOLD = "gateEndWaits"  -- the gate as first written: the session's End   *)
(*                           queued behind the held reply.                  *)
(* HOLD = "gateNoEnding"  -- a hold may start after the End reached the     *)
(*                           gate (no _ending flag).                        *)
(* HOLD = "gateKeepsTool" -- the gate until 2026-10-15: a dropped hold left  *)
(*                           the reply's function-call frames (pipecat's    *)
(*                           UninterruptibleFrames) in its queue, to land   *)
(*                           in the context after the turn that dropped it. *)
(* HOLD = "gateDropsResult" -- c1c329f: a drop also dropped a held result  *)
(*                           whose IN_PROGRESS had already passed: the call *)
(*                           stayed in the context unsettled.               *)
(* HOLD = "gateNoFoldCancelled" -- the corrector until 2026-10-15: no merge *)
(*                           across the tool round an interruption          *)
(*                           cancelled (only system messages between).      *)
(*                                                                          *)
(* The release is a TIMER (TEAPORT_REPLY_HOLD_RELEASE_S after the caller    *)
(* goes quiet; a cap while they may still talk). WORDS_IN_TIME = TRUE       *)
(* states the gate's assumption -- the caller's words, or the STT's close   *)
(* saying there were none, come before it -- under which it is sound;        *)
(* FALSE is the timer as it is, and the stale reply it still lets through   *)
(* is the accepted residual.                                                *)
(*                                                                          *)
(* The barge-in pause (barge_pause.py, #86). With the bot audible, caller   *)
(* speech PAUSES playout at the transport; their words then cancel the      *)
(* reply as before (min words, or while paused a stop word), and no words   *)
(* resume it. A pause holds back the reply's own end, so the bot counts as  *)
(* speaking until the resume.                                               *)
(*                                                                          *)
(* PAUSE = "off"             -- before #86.                                 *)
(* PAUSE = "asWritten"       -- #86 as first written: pauses at any point,  *)
(*                              a paused bot is a speaking bot.             *)
(* PAUSE = "aOnly"           -- only the near-end rule: no pause in a       *)
(*                              reply's last half second.                   *)
(* PAUSE = "tailAtPause"     -- plus the would-have-ended rule, with the    *)
(*                              tail known only if synthesis was over at    *)
(*                              the pause (the round-1 fix).                *)
(* PAUSE = "shipped"         -- plus a tail learned DURING the pause (the   *)
(*                              transport re-announces the pause when the   *)
(*                              reply's synthesis completes).               *)
(* PAUSE = "noStopWord"      -- the shipped pause without the stop word.    *)
(* PAUSE = "keepOnInterrupt" -- a transport that keeps its pause when the   *)
(*                              interruption cancels the reply.             *)
(*                                                                          *)
(* The cut and the commit (endpointing.CutReplyFence, 2026-10-13). A turn   *)
(* start and its interruption are two events: the controller opens the turn *)
(* and the aggregator only QUEUES the InterruptionFrame, which reaches the  *)
(* gate, the transport and -- last -- the assistant aggregator (which       *)
(* commits the reply it cut) ~70 ms later. `StartTurn` is the first,        *)
(* `CutLands` the second. A turn that opens on its final after its own VAD  *)
(* stop (the late-start barge-in, the commonest shape) commits in the same  *)
(* controller call, so without a fence the commit overtakes its own cut:    *)
(* the cut reply lands after the caller's turn, uncorrected, and the gate   *)
(* the commit armed reads the interruption as that commit's reply dropped   *)
(* (a false TurnMerge, and a reply left unchecked).                         *)
(*                                                                          *)
(* FENCE = "none"       -- before 2026-10-13: nothing orders them.          *)
(* FENCE = "commitOnly" -- 228298e: the commit waits for the cut; the       *)
(*                         speculation's snapshot does not.                 *)
(* FENCE = "inTime"     -- CutReplyFence as shipped (commit AND snapshot    *)
(*                         wait), ASSUMING the cut lands within its bound.  *)
(* FENCE = "timed"      -- the same with the bound as it is: past           *)
(*                         CUT_COMMIT_WAIT_SECS the commit goes ahead (the  *)
(*                         speculation is not started).                     *)
(*                                                                          *)
(* A tool call overtaken by its own cancel (#185). The LLM broadcasts a      *)
(* call's FunctionCallInProgressFrame (a ControlFrame) from the call's task; *)
(* below the LLM it queues behind whatever non-system frames are ahead of   *)
(* it -- the output transport parks it behind the reply audio still to play *)
(* -- and the interruption's reset keeps it (uninterruptible). The cancel   *)
(* the LLM broadcasts on the interruption is a SystemFrame: every processor *)
(* handles it ahead of its queue, the assistant aggregator's own included,  *)
(* so it can reach the aggregator before the call. pipecat's cancel handler *)
(* finds no call to settle there and returns; the call then lands as        *)
(* IN_PROGRESS with no task left to settle it. `lateCall` is that frame,    *)
(* still on its way, and where the cancel overtook it: "above" the          *)
(* processor directly over the aggregator, or "atAgg", past it, inside the *)
(* aggregator's own input queue (a priority queue: the SystemFrame goes     *)
(* first there too).                                                        *)
(*                                                                          *)
(* CALLS = "asWritten"        -- pipecat's aggregator as written: the late   *)
(*                               call is written IN_PROGRESS, for good.     *)
(* CALLS = "dropAbove"        -- a processor directly above the aggregator  *)
(*                               drops an InProgress whose cancel it has    *)
(*                               seen. One the cancel overtakes inside the  *)
(*                               aggregator's own queue gets past it.       *)
(* CALLS = "settleAtCommit"   -- the corrector settles any such call        *)
(*                               CANCELLED at the next commit (and          *)
(*                               snapshot). The LLM reads it unsettled      *)
(*                               before that: a sync tool's result asks it. *)
(* CALLS = "dropAtAggregator" -- as shipped (drop_overtaken_tool_calls): the *)
(*                               aggregator's own frame handling, in the    *)
(*                               order it handles them, drops the           *)
(*                               InProgress of a call whose cancel it       *)
(*                               handled first.                             *)
(*                                                                          *)
(* The answer after the tool (#17): POSTTOOL, see its section below.        *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxTurns,        \* bound on turns opened, to keep the state space finite
          MODE,
          SPEC,
          MaxCtxWrites,    \* bound on context writes under a live speculation
          HOLD,            \* the reply hold's design (see the header)
          WORDS_IN_TIME,   \* ASSUMPTION, for the gate's holding row: a resumed caller's
                           \* words (or the STT's word-less close) arrive before the
                           \* release timer or the cap. FALSE: the timers as they are
          PAUSE,           \* the barge-in pause's design (see the header)
          FENCE,           \* the cut-before-commit order (see the header)
          EARLY_FINAL,     \* a final of the caller's resumed words can be aggregated
                           \* before the VAD stop that re-applies a refused finalize
                           \* (TRUE: as it is). FALSE: the premise, see VadStop
          CALLS,           \* a call overtaken by its own cancel (see the header)
          POSTTOOL         \* the answer to a tool's result, under the caller's speech (#17)

VARIABLES
    \* --- pipecat's UserTurnController ---
    userTurn,        \* _user_turn:     a user turn is open
    userSpeaking,    \* _user_speaking: VAD says the user is audibly speaking
    watchdog,        \* the 5s user_turn_stop_timeout is armed and un-re-armed
    \* --- the rest of the session ---
    botSpeaking,     \* the transport's bot-speaking window is open
    inference,       \* this turn's words are in the context and the LLM was asked
    stopInFlight,    \* between trigger_user_turn_stopped()'s two awaits
    owed,            \* retryOnQuiet: a refused finalization waiting for quiet
    turns,
    \* --- the speculative reply ---
    spec,            \* a speculation is live (opened, not yet adopted or closed)
    specGen,         \* the context generation it was asked against
    specWords,       \* the words it was asked on are still the ones the commit
                     \* will carry (FALSE once Retext has moved them)
    gen,             \* the context generation: bumped by every other writer
    \* --- monitors ---
    missedBargeIn,   \* the user spoke over the bot and got no interruption, at a
                     \* moment when they had already had a quiet one since the
                     \* inference: the wedge, not the ordinary in-turn delay
    staleReply,      \* a reply asked against a context the commit no longer has
                     \* was handed to the pipeline
    \* --- the reply hold (reply_hold.py) ---
    reply,           \* the last commit's reply exists and has not started playing
    armed,           \* ReplyHoldGate is armed: commit -> that reply's first audio
    held,            \* the gate is holding the reply
    paused,          \* the gate's input queue is paused (pause_processing_frames)
    resumed,         \* the caller spoke after the commit, before the reply started,
                     \* and that speech has not yet turned out to be wordless
    mergePending,    \* TurnMerge: fold the next commit into the unanswered one
    unanswered,      \* user messages in the context since a reply last played
    endQueued,       \* the session's EndFrame has reached the gate (hang-up)
    \* --- monitors ---
    replyOverResume, \* a reply started over the caller's resumed speech, and that
                     \* speech was words: the stale reply the tester heard
    splitTurn        \* a commit left two user messages with no reply between them

holdVars == <<reply, armed, held, paused, resumed, mergePending, unanswered, endQueued,
              replyOverResume, splitTurn>>
VARIABLES
    \* --- the barge-in pause (barge_pause.py) ---
    pausedP,         \* the output transport has stopped writing the bot's audio
    left,            \* how much of the playing reply is left, against the two
                     \* thresholds that matter: "short" (<= 0.5 s: no pause there),
                     \* "mid" (0.5 s up to the ~1 s a final takes to land: a pause is
                     \* allowed, and the rest would have played out before the
                     \* caller's final arrives), "long"
    synthDone,       \* the reply's synthesis is over (its TTSStoppedFrame is at the
                     \* transport): only then is `left` known to the brain
    tailKnown,       \* paused, and the start strategy has been told the held-back tail
    wouldEnd,        \* ground truth: paused, and without the pause the reply would
                     \* have ended by now
    stopHeard,       \* a stop utterance landed while the bot was paused for it
    answerLost       \* monitor: a one-word answer that, without the pause, would have
                     \* been a turn (the reply would have ended), started none
pauseVars == <<pausedP, left, synthDone, tailKnown, wouldEnd, stopHeard, answerLost>>
VARIABLES
    \* --- the cut and the commit (endpointing.CutReplyFence) ---
    cut,             \* the last turn start's InterruptionFrame is still on its way to
                     \* the assistant aggregator: "none", "empty" (no reply was playing),
                     \* or "reply" (it cuts one the caller was hearing)
    overtaken,       \* a commit ran while that frame was still on its way
    cutAfterCommit   \* monitor: something read the context for a turn (its commit, or
                     \* a speculation's snapshot, which spends the ledger's cut) before
                     \* the reply that turn cut was in it
fenceVars == <<cut, overtaken, cutAfterCommit>>
VARIABLES
    \* --- the reply's tool round (the LLM called a function for the last commit) ---
    tool,            \* where that round is: "none"; "held" (its FunctionCallInProgress /
                     \* Result frames wait in the gate's paused queue); "transit" (its
                     \* InProgress is past the gate, on its way to the assistant
                     \* aggregator: the transport parks it behind unplayed audio);
                     \* "ctxRunning" (the
                     \* call and its IN_PROGRESS result are in the context); "ctxDone" (a
                     \* result is: a sync tool's, or the consult's placeholder, the
                     \* consult running on); "resultHeld" (the call is in the context, its
                     \* result -- a DataFrame -- waits in the paused queue: a sync tool's
                     \* seconds of work straddle the hold's start); "ctxCancelled" (an
                     \* interruption cancelled it: the result reads CANCELLED);
                     \* "ctxOrphan" (the call is in the context and nothing will ever
                     \* settle it)
    lateTool,        \* monitor: a dropped reply's tool round was left to land in the
                     \* context after the turn that dropped it
    goodbyeLost,     \* monitor: the session-end commit dropped a reply the End was
                     \* queued behind (it would have played out first)
    orphanCall,      \* monitor: a tool call was left in the context with no result,
                     \* ever (strict OpenAI-compatible servers refuse such a context)
    lateCall,        \* the InProgress of a call its cancel overtook, still on its way:
                     \* "none", "above" (the processor directly over the aggregator), or
                     \* "atAgg" (past it, in the aggregator's own queue). Two at once
                     \* (FIFO between them) are one, at the later position
    unsettled,       \* a call written IN_PROGRESS after its cancel is in the context: no
                     \* task will ever settle it
    readUnsettled    \* monitor: the LLM was asked over a context holding such a call
toolVars == <<tool, lateTool, goodbyeLost, orphanCall, lateCall, unsettled, readUnsettled>>
VARIABLES
    \* --- the answer to a tool's result (#17; see "The answer after the tool") ---
    answer,          \* "none"; "due" (a sync result is in the context and asks for the
                     \* answer: the assistant aggregator's push, or its deferral to the
                     \* bot's stop); "held" (FollowupGate holds the push); "asked" (the
                     \* LLM is answering, the TTS holding its first clause while the VAD
                     \* hears the caller); "playing"; "dropped" (discarded while the
                     \* caller spoke: only a turn of theirs will ever read the result)
    pend,            \* ground truth: caller speech has begun whose words have not
                     \* resolved -- no turn start, no commit carrying them and no
                     \* word-less close since its VAD start
    gpend,           \* FollowupGate's view of it (gate): from a VAD start until a turn
                     \* start, or its release timer after the VAD stop
    asUnder,         \* the answer was asked while such speech was pending
    ansOver,         \* monitor: the answer started playing before the words of speech it
                     \* was asked under, which then started their turn
    ansLost,         \* monitor: the answer was discarded and no turn of the caller's read
                     \* the result instead (their speech was word-less)
    skipLost,        \* monitor: pipecat skipped the push for an open turn whose commit
                     \* had already read the context without the result (#196)
    gstale           \* ghost: the gate's view is pending on speech whose words a commit
                     \* has already taken (gateNoCommit)
postVars == <<answer, pend, gpend, asUnder, ansOver, ansLost, skipLost, gstale>>

NearEndRule  == PAUSE \notin {"off", "asWritten"}
Counterfact  == PAUSE \notin {"off", "asWritten", "aOnly"}
LateTail     == PAUSE \in {"shipped", "noStopWord", "keepOnInterrupt"}
\* May a commit run with a cut on its way? The fence holds it, unless its bound runs out.
CommitMayOvertake == FENCE \in {"none", "timed"}
\* May a speculation's snapshot? Only where the fence does not cover it; past the bound
\* the speculation is not started at all.
SnapshotMayOvertake == FENCE \in {"none", "commitOnly"}
vars == <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
          stopInFlight, owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply,
          holdVars, pauseVars, fenceVars, toolVars, postVars>>

Gated == HOLD /= "none"
\* FollowupGate's designs for the answer after a tool (#17, POSTTOOL).
Gates == {"gate", "gateNoCommit"}

\* What a drop (an interruption reaching the gate) does to the reply's tool round. A
\* call still running in the context is cancelled (pipecat's cancel_on_interruption, the
\* default every tool here is registered with: CANCELLED is written over IN_PROGRESS).
\* One still behind the hold is dropped with the held reply as shipped (reply_hold.py
\* discards the held function-call frames); until 2026-10-15 pipecat's queue reset kept
\* them, UninterruptibleFrames, and they landed after the commit that dropped them --
\* a cancelled call as an IN_PROGRESS nothing ever settles (the cancel reached the
\* assistant aggregator first and found no call to settle), a finished one with its
\* result and, for a sync tool, a second inference.
Dropped(t) == CASE t = "ctxRunning" -> "ctxCancelled"
                [] t = "held"       -> "none"
                [] t = "transit"    -> "none"     \* cancelled before it landed: `Overtakes`
                [] t = "resultHeld" -> IF HOLD = "gateDropsResult" THEN "ctxOrphan"
                                       ELSE "ctxDone"
                [] OTHER            -> t
LateOnDrop == tool \in {"held", "resultHeld"} /\ HOLD = "gateKeepsTool"
\* A call whose IN_PROGRESS passed before the hold and whose result waited behind it
\* (`resultHeld`): the call has finished, so the interruption cancels nothing, and the
\* result is all that can settle it. As shipped the gate lets it through without the
\* inference it would ask for (the commit that follows asks); c1c329f's gate
\* (`gateDropsResult`) dropped it with the held reply and left the call unsettled.
OrphanOnDrop == tool = "resultHeld" /\ HOLD = "gateDropsResult"
\* The interruption reaches the LLM, which cancels a call whose InProgress is still in
\* transit, and the cancel overtakes it: above the processor over the aggregator, or
\* inside the aggregator's own queue (#185). A second late frame behind a first is
\* behind it (FIFO): the two are one at the later position.
Overtakes ==
    \E w \in {"above", "atAgg"} :
        lateCall' = IF tool = "transit"
                      THEN (IF lateCall = "atAgg" THEN "atAgg" ELSE w)
                      ELSE lateCall
\* A commit reads the context for the LLM. settleAtCommit settles an unsettled call
\* CANCELLED first (on the commit's LLMContextFrame, as the corrector would).
CommitReads ==
    /\ readUnsettled' = (readUnsettled \/ (unsettled /\ CALLS /= "settleAtCommit"))
    /\ unsettled' = (unsettled /\ CALLS /= "settleAtCommit")

Init ==
    /\ userTurn = FALSE
    /\ userSpeaking = FALSE
    /\ watchdog = FALSE
    /\ botSpeaking = FALSE
    /\ inference = FALSE
    /\ stopInFlight = FALSE
    /\ owed = FALSE
    /\ turns = 0
    /\ spec = FALSE
    /\ specGen = 0
    /\ specWords = FALSE
    /\ gen = 0
    /\ missedBargeIn = FALSE
    /\ staleReply = FALSE
    /\ reply = FALSE /\ armed = FALSE /\ held = FALSE /\ paused = FALSE
    /\ resumed = FALSE /\ mergePending = FALSE /\ unanswered = 0 /\ endQueued = FALSE
    /\ replyOverResume = FALSE /\ splitTurn = FALSE
    /\ pausedP = FALSE /\ left = "long" /\ synthDone = FALSE /\ tailKnown = FALSE
    /\ wouldEnd = FALSE /\ stopHeard = FALSE /\ answerLost = FALSE
    /\ cut = "none" /\ overtaken = FALSE /\ cutAfterCommit = FALSE
    /\ tool = "none" /\ lateTool = FALSE /\ goodbyeLost = FALSE /\ orphanCall = FALSE
    /\ lateCall = "none" /\ unsettled = FALSE /\ readUnsettled = FALSE
    /\ answer = "none" /\ pend = FALSE /\ gpend = FALSE /\ asUnder = FALSE /\ ansOver = FALSE
    /\ ansLost = FALSE /\ skipLost = FALSE /\ gstale = FALSE

\* Caller speech while the bot is audible pauses playout -- not, with the near-end
\* rule, when the reply is fully synthesized and in its last half second. The pause
\* frame carries the tail when synthesis is over.
MayPause == PAUSE /= "off" /\ botSpeaking /\ ~pausedP
            /\ ~(NearEndRule /\ synthDone /\ left = "short")
TakePause ==
    /\ pausedP' = TRUE
    /\ tailKnown' = (Counterfact /\ synthDone)
    /\ UNCHANGED <<left, synthDone, wouldEnd, stopHeard, answerLost>>
PauseOnSpeech ==
    IF MayPause THEN TakePause ELSE UNCHANGED pauseVars

\* The caller starts speaking: the gate holds an armed reply (the VAD start, or the
\* faster onset test -- one event here), and the speech counts as a resumption if
\* the last commit's reply has not started.
HoldOnSpeech ==
    /\ resumed' = (resumed \/ reply)
    \* _ending: once the session's End has reached the gate, no new hold starts.
    /\ IF Gated /\ armed /\ ~held /\ (~endQueued \/ HOLD = "gateNoEnding")
         THEN held' = TRUE /\ paused' = TRUE
         ELSE UNCHANGED <<held, paused>>

\* Its words are not known yet: they come with the STT's final, after the VAD stop.
PendOnSpeech ==
    /\ pend' = (pend \/ POSTTOOL /= "off")
    /\ gpend' = (gpend \/ POSTTOOL /= "off")
    /\ gstale' = FALSE                         \* what the gate waits for is real again
    /\ UNCHANGED <<answer, asUnder, ansOver, ansLost, skipLost>>

(***************************************************************************)
(* VAD. Both edges re-arm the stop watchdog, which is the whole reason it   *)
(* cannot save anyone: _handle_vad_user_started_speaking,                   *)
(* _handle_vad_user_stopped_speaking and _handle_transcription all do       *)
(* `self._user_turn_stop_timeout_event.set()`.                              *)
(***************************************************************************)

\* ~stopInFlight only so that a resume landing INSIDE trigger_user_turn_stopped()
\* has to be `Resume` below. The two together enable exactly what this one alone
\* would; splitting them costs no behaviours and makes the counterexample name its
\* own mechanism instead of leaving the reader to notice which step it landed on.
VadStart ==
    \* Not guarded by ~endQueued: the caller can still speak after the session's End
    \* reached the gate, and the VAD's frames still flow -- that is the gap _ending
    \* closes.
    /\ ~userSpeaking /\ ~stopInFlight
    /\ userSpeaking' = TRUE
    /\ watchdog' = FALSE                       \* the inactivity timer is reset
    /\ spec' = FALSE                           \* the caller resumed: the text will differ
    /\ HoldOnSpeech
    /\ PauseOnSpeech
    /\ PendOnSpeech
    /\ UNCHANGED <<userTurn, botSpeaking, inference, stopInFlight, owed, turns,
                   specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<reply, armed, mergePending, unanswered, endQueued, replyOverResume,
                   splitTurn>>
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars

\* The user falls quiet: `VadStop`, below the commit it may re-apply.

(***************************************************************************)
(* The turn.                                                               *)
(***************************************************************************)

\* A user turn starts. One definition for every transcript that starts a turn
\* (Interject, and the pause's one-word answer and stop word below). This is the
\* CONTROLLER's half: the turn is open and the InterruptionFrame is queued (`cut`). What
\* the interruption does below the aggregator -- the gate, the transport, the assistant
\* aggregator's commit of the reply it cut -- is `CutLands`, a step of its own: pipecat's
\* broadcast_interruption() only queues the frame, and a turn that opens on its final
\* after its own VAD stop commits inside the same controller call (endpointing.py,
\* LateStartTurnStopStrategy), so the commit can run first unless something holds it
\* (FENCE). The model had them as one step until 2026-10-14 -- the premise the code did
\* not keep before CutReplyFence.
\*
\* `cut = "none"`: a turn opens on a transcript, hundreds of ms after any earlier turn's
\* start, and an interruption crosses in ~70 ms -- two cuts in flight at once are not
\* modelled (stated, not checked).
StartTurn ==
    /\ cut = "none"
    /\ userTurn' = TRUE
    /\ inference' = FALSE
    /\ turns' = turns + 1
    /\ spec' = FALSE               \* a new turn: whatever was asked is not this
    \* The words were for speech that began after the commit, and a reply is
    \* playing that started over it: the stale reply, cut only now.
    /\ replyOverResume' = (replyOverResume \/ (botSpeaking /\ resumed))
    /\ resumed' = FALSE
    \* The strategy's turn start: a stop it heard was honoured.
    /\ stopHeard' = FALSE
    \* What the interruption will cut into the context: a reply the caller was hearing.
    \* One not yet playing commits nothing (the assistant aggregator takes the TTS text
    \* as it plays), though the gate still drops it and asks for the merge.
    /\ cut' = IF botSpeaking THEN "reply" ELSE "empty"
    /\ overtaken' = FALSE
    /\ UNCHANGED <<botSpeaking, reply, armed, held, paused, mergePending, unanswered,
                   endQueued, splitTurn>>
    /\ UNCHANGED <<pausedP, left, synthDone, tailKnown, wouldEnd, answerLost,
                   cutAfterCommit>>
    \* The pending speech was words. An answer playing that was asked under it is the
    \* stale answer, cut only now. One discarded under it is read by this turn.
    /\ pend' = FALSE /\ gpend' = FALSE /\ gstale' = FALSE
    /\ ansOver' = (ansOver \/ (answer = "playing" /\ asUnder))
    /\ answer' = IF answer = "dropped" THEN "none" ELSE answer
    /\ UNCHANGED <<asUnder, ansLost, skipLost>>

\* The interruption reaches the gate, the transport and the assistant aggregator, which
\* commits the reply it cut. Its effects are the old StartTurn's.
CutLands ==
    /\ ~endQueued                             \* the session has not ended
    /\ cut /= "none"
    /\ cut' = "none" /\ overtaken' = FALSE
    /\ botSpeaking' = FALSE
    \* The interruption cancels the reply. A held one is dropped: pipecat's reset
    \* empties the queue; the gate lifts the pause itself -- except as first written,
    \* where an Uninterruptible frame in hand left it paused.
    \*
    \* A commit that overtook the frame armed the gate for ITS reply, which the LLM is
    \* producing behind the interruption: that reply is not dropped (none of it has
    \* reached the gate), but the gate cannot tell -- it disarms and asks for a merge
    \* (the false TurnMerge), so the reply plays unchecked and the merge lands on the
    \* next commit.
    /\ IF overtaken
         THEN UNCHANGED <<reply, resumed>>
         ELSE reply' = FALSE /\ resumed' = FALSE
    /\ armed' = FALSE /\ held' = FALSE
    /\ paused' = (HOLD = "gateNoResume" /\ held)
    \* Armed: none of the last commit's reply was played: the next commit finishes it.
    /\ mergePending' = (mergePending \/ (armed /\ HOLD \notin {"none", "gateNoMerge"}))
    /\ UNCHANGED <<unanswered, endQueued, splitTurn, replyOverResume>>
    /\ tool' = Dropped(tool) /\ lateTool' = (lateTool \/ LateOnDrop)
    /\ orphanCall' = (orphanCall \/ OrphanOnDrop)
    /\ Overtakes
    /\ UNCHANGED <<goodbyeLost, unsettled, readUnsettled>>
    \* The interruption reaches the transport: the queued audio is dropped and, with
    \* it, the pause -- except in a transport that keeps it.
    /\ pausedP' = (PAUSE = "keepOnInterrupt" /\ pausedP)
    /\ tailKnown' = FALSE /\ wouldEnd' = FALSE
    \* The answer goes with it, and the turn's commit reads the result: a push still due
    \* is skipped (pipecat's reset clears a deferral, and the turn is open at the
    \* aggregator), a held one is dropped by FollowupGate, a completion is cancelled or
    \* -- its LLMContextFrame still queued -- dropped by the reset (test_pipecat_premises),
    \* one being synthesized is discarded by the TTS, one playing is cut.
    /\ answer' = IF answer \in {"due", "held", "asked", "playing"} THEN "none" ELSE answer
    /\ asUnder' = (asUnder /\ answer \notin {"due", "held", "asked", "playing"})
    /\ UNCHANGED <<pend, gpend, ansOver, ansLost, skipLost, gstale>>
    /\ UNCHANGED <<left, synthDone, answerLost, stopHeard, cutAfterCommit>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, inference, stopInFlight, owed, turns,
                   spec, specGen, specWords, gen, missedBargeIn, staleReply>>

\* MinWordsUserTurnStartStrategy fired: >= INTERRUPT_MIN_WORDS of transcript.
\* This is the ONLY producer of an interruption in the brain.
\* Not guarded by `userSpeaking`: the transcript that opens a barge-in usually lands
\* AFTER the caller's VAD stop (the engine returns nothing over the bot until the stop
\* flushes the segment -- LateStartTurnStopStrategy's note), and that late start is the
\* shape whose commit can overtake its cut.
Interject ==
    /\ ~endQueued                             \* the session has not ended
    /\ turns < MaxTurns
    /\ IF userTurn
         THEN \* _trigger_user_turn_start returns early. The strategy logged
              \* should_trigger=True and nothing came of it. Count it as a MISSED
              \* barge-in only once the user has had a quiet moment since the
              \* inference ran:
              \* before that, an open turn is the ordinary state of someone still
              \* talking, and refusing a second start is correct. Nor while this
              \* turn's own interruption is still on its way to the transport.
              /\ missedBargeIn' = (missedBargeIn \/ (inference /\ ~owed /\ botSpeaking
                                                     /\ cut = "none"))
              /\ UNCHANGED <<userTurn, botSpeaking, inference, turns, spec>>
              /\ UNCHANGED holdVars
              /\ UNCHANGED pauseVars
              /\ UNCHANGED fenceVars
              /\ UNCHANGED postVars
         ELSE /\ StartTurn
              /\ UNCHANGED missedBargeIn
    /\ watchdog' = FALSE
    /\ UNCHANGED <<userSpeaking, stopInFlight, owed, specGen, specWords, gen, staleReply>>
    /\ UNCHANGED toolVars

(***************************************************************************)
(* The barge-in pause.                                                     *)
(***************************************************************************)

\* The reply plays on: its rest shrinks past the two thresholds.
Progress ==
    /\ ~endQueued
    /\ PAUSE /= "off" /\ botSpeaking /\ ~pausedP /\ left /= "short"
    /\ left' = IF left = "long" THEN "mid" ELSE "short"
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED <<pausedP, synthDone, tailKnown, wouldEnd, stopHeard, answerLost>>
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars
    /\ UNCHANGED postVars

\* The reply's synthesis completes (its TTSStoppedFrame reaches the transport). During
\* a pause, as shipped, the transport re-announces the pause with the tail it now
\* knows, and the strategy learns it.
SynthDone ==
    /\ ~endQueued
    /\ PAUSE /= "off" /\ botSpeaking /\ ~synthDone
    /\ synthDone' = TRUE
    /\ tailKnown' = (tailKnown \/ (pausedP /\ LateTail))
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED <<pausedP, left, wouldEnd, stopHeard, answerLost>>
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars
    /\ UNCHANGED postVars

\* Ground truth: paused with the whole reply synthesized and less than a final's
\* latency of it left, the held-back rest would have played out by now. (With a long
\* rest, or more still to be synthesized, it would not.)
TailElapsed ==
    /\ ~endQueued
    /\ pausedP /\ synthDone /\ left /= "long" /\ ~wouldEnd
    /\ wouldEnd' = TRUE
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED <<pausedP, left, synthDone, tailKnown, stopHeard, answerLost>>
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars
    /\ UNCHANGED postVars

\* No words: the line is quiet and playout resumes from where it stopped. A bot that
\* resumes after the caller said stop to it has not been stopped: that OUTCOME is
\* the missed barge-in, whatever path got there.
ResumeP ==
    /\ ~endQueued
    /\ pausedP /\ ~userSpeaking
    /\ pausedP' = FALSE /\ wouldEnd' = FALSE /\ tailKnown' = FALSE /\ stopHeard' = FALSE
    /\ missedBargeIn' = (missedBargeIn \/ stopHeard)
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED <<left, synthDone, answerLost>>
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars
    /\ UNCHANGED postVars

\* A one-word final (not a stop word) lands after the caller's VAD stop -- "Yes.".
\* What the strategy does with it (`Starts`) is the design; what it SHOULD have done
\* is the ground truth -- without the pause, would the bot have been silent? -- and
\* the monitor compares the outcome with that, not with the design's own branch.
OneWordStarts ==
    \/ ~botSpeaking
    \/ (Counterfact /\ pausedP /\ tailKnown /\ wouldEnd)
OneWord ==
    /\ ~endQueued
    /\ PAUSE /= "off"
    /\ turns < MaxTurns /\ ~userTurn /\ ~userSpeaking /\ ~stopInFlight
    /\ IF OneWordStarts
         THEN /\ StartTurn
              /\ UNCHANGED missedBargeIn
         ELSE /\ answerLost' = (answerLost \/ (pausedP /\ wouldEnd))
              /\ UNCHANGED <<userTurn, botSpeaking, inference, turns, spec, missedBargeIn>>
              /\ UNCHANGED holdVars
              /\ UNCHANGED <<pausedP, left, synthDone, tailKnown, wouldEnd, stopHeard>>
              /\ UNCHANGED fenceVars
              /\ UNCHANGED postVars
    /\ UNCHANGED <<userSpeaking, watchdog, stopInFlight, owed, specGen, specWords, gen, staleReply>>
    /\ UNCHANGED toolVars

\* A transcript made only of stop words ("Stop.") over the speaking bot. Under the
\* 2-word guard; as shipped, a stop utterance to a PAUSED bot starts the turn. If it
\* does not, the caller said stop to a bot that went quiet for them, and ResumeP
\* records what happens next.
StopWord ==
    /\ ~endQueued
    /\ PAUSE /= "off"
    /\ turns < MaxTurns /\ ~userTurn /\ botSpeaking /\ ~stopInFlight
    /\ IF pausedP /\ PAUSE /= "noStopWord"
         THEN /\ StartTurn
              /\ UNCHANGED missedBargeIn
         ELSE /\ stopHeard' = (stopHeard \/ pausedP)
              /\ UNCHANGED <<userTurn, botSpeaking, inference, turns, spec, missedBargeIn>>
              /\ UNCHANGED holdVars
              /\ UNCHANGED <<pausedP, left, synthDone, tailKnown, wouldEnd, answerLost>>
              /\ UNCHANGED fenceVars
              /\ UNCHANGED postVars
    /\ UNCHANGED <<userSpeaking, watchdog, stopInFlight, owed, specGen, specWords, gen, staleReply>>
    /\ UNCHANGED toolVars

(***************************************************************************)
(* The speculative reply.                                                  *)
(***************************************************************************)

\* The interim settled under an INCOMPLETE verdict (or a final landed that the stop
\* strategy did not conclude on): the LLM is asked now, against the context as it is
\* and the words as they are. Enabled exactly where the strategy's trigger fires --
\* an open turn, VAD quiet, no inference yet -- and touches nothing the controller's
\* guards read. Single-flight: a changed interim or a new final supersedes, which is
\* this same step from a state where `spec` was already TRUE (the old one is closed).
SpecStart ==
    /\ ~endQueued                             \* the session has not ended
    /\ SPEC /= "off"
    /\ userTurn /\ ~userSpeaking /\ ~inference /\ ~stopInFlight
    \* Speculator.start waits at the cut fence (as shipped): a snapshot is taken only
    \* once the reply a barge-in cut is in the context, and not at all past the bound.
    /\ cut = "none" \/ SnapshotMayOvertake
    /\ spec' = TRUE
    /\ specGen' = gen
    /\ specWords' = TRUE
    \* The snapshot runs the heard-context corrector first (reconcile_for_snapshot),
    \* which spends the ledger's cut: on a context without the cut reply it has nothing
    \* to rewrite, and the reply then lands whole (live 2026-10-13, ecb4dcd).
    /\ cutAfterCommit' = (cutAfterCommit \/ cut = "reply")
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED <<cut, overtaken>>
    /\ UNCHANGED toolVars
    /\ UNCHANGED postVars

\* The words the commit will carry move after the snapshot, with the caller quiet:
\* the decoder's lagging tail past the settle window, or the final's re-decode
\* differing from the interim it follows. Free, and it leaves the speculation live
\* (see the header). Bounded by itself: it fires once per speculation.
Retext ==
    /\ ~endQueued                             \* the session has not ended
    /\ spec /\ specWords /\ ~inference
    /\ specWords' = FALSE
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, spec, specGen, gen, missedBargeIn,
                   staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars
    /\ UNCHANGED postVars

\* Something else writes the context while a speculation is live. A free action,
\* deliberately: HeardContextCorrector._reconcile on the very LLMContextFrame that
\* carries the commit was such a writer until it was moved ahead of the snapshot, and
\* nothing in the pipeline stops the next one. Bounded only to keep the state finite.
CtxChange ==
    /\ ~endQueued                             \* the session has not ended
    /\ spec /\ gen < MaxCtxWrites
    /\ gen' = gen + 1
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, spec, specGen, specWords, missedBargeIn,
                   staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars
    /\ UNCHANGED postVars

\* trigger_user_turn_stopped(), first await: the stop strategy has decided, the
\* aggregation is pushed and the LLM is asked. The controller gates this on the turn
\* being open and NOTHING else -- the `_user_speaking` guard is on the second await
\* only. ~userSpeaking here is not that guard but a fact about the strategy: both of
\* its conclusion points run with VAD quiet (_handle_vad_user_stopped_speaking, and
\* the analyzer's silence backstop in _handle_input_audio, which only accumulates
\* silence while _vad_user_speaking is False). Without it TLC finds a two-step
\* "the user simply never stopped" trace that the implementation cannot produce, and
\* the counterexample stops being a bug report. See README, Known limits.
\*
\* With a speculation live this is also the commit that decides its fate: the LLM
\* service is offered the stream (Speculator.take). byText adopts it whenever the
\* words are what was asked (specWords), so a context written in between is adopted
\* with it. byHistory adopts whenever nothing else wrote the context (specGen = gen),
\* so words that moved after the snapshot are adopted with it. byContext adopts only
\* when both hold; the speculation is otherwise closed and the ordinary request made,
\* the same state minus the monitor. Either way the speculation is over.
\*
\* The adoption rule is written once so that every row checks the same thing: a
\* reply is stale when it is ADOPTED against a moved context or moved words. For
\* byContext that is unreachable by construction -- Adopt requires exactly the two
\* equalities Stale negates -- so its row checks the rule as written here, not the
\* code's equality test (Speculator.take is the tests' to pin); what it does show is
\* that dropping either conjunct makes the row fall, which byText and byHistory do.
Adopt == spec /\ (SPEC \in {"byText", "byContext"} => specWords)
              /\ (SPEC \in {"byHistory", "byContext"} => specGen = gen)
Stale == Adopt /\ (specGen /= gen \/ ~specWords)

\* The commit: push_aggregation(), on every path that has words to commit -- the stop
\* strategy's inference and the watchdog's stop alike (CutReplyFence.hold_commits wraps
\* it, so both wait). The LLM service is offered the speculation (Speculator.take), so
\* this is where a live one is decided. A reply is on its way, and the aggregator's
\* on_user_turn_message_added arms the gate. The aggregator appended this turn's user
\* message -- which the corrector folds into the unanswered one when a merge is pending
\* AND there is one to fold into (_merge_tail: nothing but system messages between
\* them); a merge pending over a reply that did play is taken and leaves the turns apart.
\*
\* The fence: the commit waits until the reply a barge-in cut is in the context. Where
\* it may run ahead of its cut, it reads a context without that reply (cutAfterCommit),
\* and the gate it arms is the one the interruption will then find armed (overtaken).
\* In the code the wait sits inside trigger_user_turn_inference_triggered's await, so a
\* VAD start can land during it and the arm then holds at once ("the caller is speaking
\* at the commit"); here the commit runs after the cut lands and `Resume` takes that
\* start -- the same states.
CommitReady == cut = "none" \/ CommitMayOvertake

\* The user message the commit appends, against the one before it. It is folded into
\* that one when a merge is pending and the corrector can (_merge_tail): nothing but
\* system messages between -- or, as shipped, a tool round an interruption cancelled,
\* which it removes. A finished tool round between them (a sync result, the async
\* consult's placeholder) means the turn was acted on: the new message is a turn of its
\* own, which is no split (the model sees the round, and the consult goes on). One
\* still in the context unsettled is the same. Anything else after an unanswered
\* message is the split: two user messages with neither a reply nor a round between.
Folds(mp, t) == mp /\ unanswered >= 1
                /\ (t \in {"none", "held"}
                    \/ (t = "ctxCancelled" /\ HOLD /= "gateNoFoldCancelled"))
Acted(t) == t \in {"transit", "ctxRunning", "resultHeld", "ctxDone"}
Appended(mp, t) ==
    /\ unanswered' = IF Folds(mp, t) THEN unanswered
                     ELSE IF Acted(t) THEN 1
                     ELSE (IF unanswered < 2 THEN unanswered + 1 ELSE 2)
    /\ splitTurn' = (splitTurn \/ (~Folds(mp, t) /\ unanswered >= 1 /\ ~Acted(t)))
    /\ tool' = "none"
    /\ UNCHANGED goodbyeLost

\* Speech that began in the open turn has its words in this commit: none are pending.
\* FollowupGate learns it from TurnCommitMark (every commit of an open turn), and lets go
\* of speech that began before it -- ordered by frame id, not by arrival, since the
\* commit and the caller's VAD frames reach it by different paths. gateNoCommit is the
\* gate as first written (cb5fbb8): only a turn start or its timer ended its view, so a
\* turn of two segments ("what's the time ... in Paris") held its tool's answer a
\* release window with nobody talking.
PendCommitted ==
    /\ pend' = FALSE
    /\ IF POSTTOOL = "gateNoCommit"
         THEN gstale' = (gstale \/ gpend) /\ UNCHANGED gpend
         ELSE gpend' = FALSE /\ gstale' = FALSE
    /\ UNCHANGED <<answer, asUnder, ansOver, ansLost, skipLost>>

CommitCore ==
    /\ spec' = FALSE
    /\ staleReply' = (staleReply \/ Stale)
    /\ reply' = TRUE /\ armed' = Gated
    /\ Appended(mergePending, tool)
    /\ mergePending' = FALSE
    /\ CommitReads
    /\ UNCHANGED <<lateTool, orphanCall, lateCall>>
    /\ PendCommitted
Commit ==
    /\ CommitCore
    /\ overtaken' = (cut /= "none")
    /\ cutAfterCommit' = (cutAfterCommit \/ cut = "reply")
    /\ UNCHANGED cut

\* MODE = "retryMerges", as shipped since 2026-10-15 (CutReplyFence.hold_commits, its
\* `unplayed_reply`): a commit that finds the gate still ARMED once the fence has let it
\* through -- the reply to the last commit has not started playing, and no turn start's
\* interruption has reached the gate since -- continues that turn. It first drops the
\* unplayed reply: an InterruptionFrame pushed down from the aggregator (no turn start,
\* nothing upstream), which the gate reads as a dropped reply (it ends any hold and asks
\* for the merge) and the fence waits for, so the commit's words are folded into the
\* unanswered message (TurnMerge) -- across a tool round the drop cancelled, not across
\* one that finished (`Appended`). One step here: the commit's wait is atomic with it,
\* as for every commit -- under FENCE = "inTime". Only the refused finalize's second
\* commit reaches it (`VadStop`): every other commit runs on a turn whose start's
\* interruption disarmed the gate, and past the fence's bound (cut /= "none") the code
\* does not look. ForceStop's and Inference's commits take the same step anyway, as the
\* code's one wrapper does. Not the session-end commit: see Hangup.
Continues == MODE \in {"retryMerges", "retryMergesAtEnd"} /\ armed /\ cut = "none"
ContinueCommit ==
    /\ spec' = FALSE
    /\ staleReply' = (staleReply \/ Stale)
    /\ reply' = TRUE /\ armed' = Gated
    /\ held' = FALSE /\ paused' = FALSE /\ resumed' = FALSE
    /\ Appended(HOLD /= "gateNoMerge", Dropped(tool))
    /\ lateTool' = (lateTool \/ LateOnDrop)
    /\ orphanCall' = (orphanCall \/ OrphanOnDrop)
    /\ Overtakes                   \* its interruption reaches the LLM too
    /\ CommitReads
    /\ PendCommitted
    /\ mergePending' = FALSE
    /\ UNCHANGED <<endQueued, replyOverResume>>
    /\ UNCHANGED fenceVars
\* FENCE = "timed": the fence's second wait, for the continuation's own interruption,
\* runs out (CUT_COMMIT_WAIT_SECS) and the commit goes ahead with the frame still on its
\* way: no merge has been asked for yet (the gate has not seen it), the commit arms the
\* gate, and the frame then lands as `CutLands` does for any overtaken cut -- it drops
\* the held reply and asks for a merge for the NEXT commit (the false TurnMerge). The
\* same accepted residual as every commit past the bound.
ContinueLate ==
    /\ CommitCore
    /\ cut' = "empty" /\ overtaken' = TRUE
    /\ UNCHANGED <<cutAfterCommit, held, paused, resumed, endQueued, replyOverResume>>
CommitStep ==
    IF Continues
      THEN IF FENCE = "timed" THEN ContinueCommit \/ ContinueLate ELSE ContinueCommit
      ELSE /\ Commit
           /\ UNCHANGED <<held, paused, resumed, endQueued, replyOverResume>>

Inference ==
    /\ ~endQueued                             \* the session has not ended
    /\ userTurn /\ ~stopInFlight /\ ~inference /\ ~userSpeaking
    /\ CommitReady
    /\ inference' = TRUE
    /\ stopInFlight' = TRUE
    /\ watchdog' = FALSE                       \* _trigger_user_turn_inference_triggered
    /\ CommitStep
    /\ UNCHANGED <<userTurn, userSpeaking, botSpeaking, owed, turns, specGen, specWords, gen,
                   missedBargeIn>>
    /\ UNCHANGED pauseVars

\* The user falls quiet. In retryOnQuiet the owed finalization is applied HERE,
\* inside the same process_frame() call that cleared _user_speaking -- so it is
\* atomic with respect to every other coroutine, exactly as the wrapper is.
\*
\* And it is a COMMIT when the aggregation is not empty. The refused finalize left the
\* turn open with its stop strategy un-reset (pipecat: "keep the turn open so the next
\* inference re-evaluates"), so the words the caller resumed with join it, and whatever
\* of them was finalized by this VAD stop (the engine's own segmenter closes a segment
\* on a shorter silence than the VAD's floor) is committed here -- by the strategy's own
\* second stop on a COMPLETE verdict, or by the re-applied stop: either way in this
\* frame, as a second user message, with no turn start and so no interruption. Until
\* 2026-10-15 (retryOnQuiet) the first commit's reply, held by the gate, was then
\* released and played, then the answer to the rest. `early` is that final, possible
\* only with EARLY_FINAL. Until 2026-10-14 the model had the retry commit nothing, which
\* is EARLY_FINAL = FALSE: the premise the reply hold's and the pause's rows are checked
\* under, so that each still tells its own designs apart -- and under which retryOnQuiet
\* and retryMerges are one machine (no commit ever finds the gate armed).
\*
\* As shipped (retryMerges) a second commit on a turn whose first reply has not played
\* (the gate still armed) is a continuation of it: `CommitStep` drops the unplayed reply
\* and folds the words into the unanswered message.
Retry == MODE \in {"retryOnQuiet", "retryMerges", "retryMergesAtEnd"}
VadStop ==
    /\ ~endQueued                             \* the session has not ended
    /\ userSpeaking
    /\ userSpeaking' = FALSE
    /\ watchdog' = FALSE
    /\ IF Retry /\ owed /\ userTurn
         THEN /\ userTurn' = FALSE
              /\ owed' = FALSE
              /\ inference' = FALSE
              /\ \/ /\ UNCHANGED <<spec, staleReply>>     \* nothing new: commits nothing
                    /\ UNCHANGED holdVars
                    /\ UNCHANGED fenceVars
                    /\ UNCHANGED toolVars
                    /\ UNCHANGED postVars
                 \/ /\ EARLY_FINAL /\ CommitReady        \* `early`: a second commit
                    /\ CommitStep
         ELSE /\ UNCHANGED <<userTurn, owed, inference, spec, staleReply>>
              /\ UNCHANGED holdVars
              /\ UNCHANGED fenceVars
              /\ UNCHANGED toolVars
              /\ UNCHANGED postVars
    /\ UNCHANGED <<botSpeaking, stopInFlight, turns, specGen, specWords, gen,
                   missedBargeIn>>
    /\ UNCHANGED pauseVars

\* The gap between the two awaits: push_aggregation()'s trip across the pipeline,
\* during which the aggregator's input task can run a queued VAD start. This is
\* the entire entry into the failure.
Resume ==
    /\ stopInFlight
    /\ ~userSpeaking
    /\ userSpeaking' = TRUE
    /\ watchdog' = FALSE
    /\ HoldOnSpeech
    /\ PendOnSpeech
    /\ UNCHANGED <<userTurn, botSpeaking, inference, stopInFlight, owed, turns,
                   spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<reply, armed, mergePending, unanswered, endQueued, replyOverResume,
                   splitTurn>>
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars

\* trigger_user_turn_stopped(), second await.
Finalize ==
    /\ ~endQueued                             \* the session has not ended
    /\ stopInFlight
    /\ stopInFlight' = FALSE
    /\ IF userSpeaking
         THEN \* "Never finalize while the user is audibly speaking." The turn stays
              \* open -- with the inference already spent.
              /\ UNCHANGED <<userTurn, inference>>
              /\ owed' = Retry
         ELSE /\ userTurn' = FALSE
              /\ inference' = FALSE
              /\ owed' = FALSE
    /\ UNCHANGED <<userSpeaking, watchdog, botSpeaking, turns, spec, specGen, specWords, gen,
                   missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars
    /\ UNCHANGED postVars

\* The reply the inference produced reaches the transport. It cannot overtake the
\* finalize in practice -- an LLM round trip plus synthesis against two awaits --
\* so ~stopInFlight is an ordering FACT, stated rather than modelled. See the
\* limits note in README.md.
BotStart ==
    /\ ~endQueued                             \* the session has not ended
    /\ (inference \/ reply) /\ ~stopInFlight /\ ~botSpeaking
    /\ ~paused                                 \* the gate holds it
    \* An interruption on its way is a SystemFrame: it overtakes every reply frame
    \* queued behind it, and the reply to a commit that overtook IT is produced behind
    \* it. An ordering fact, as ~stopInFlight is.
    /\ cut = "none"
    /\ botSpeaking' = TRUE
    \* Its first audio goes through: the gate disarms, and the caller has a reply.
    /\ reply' = FALSE /\ armed' = FALSE /\ unanswered' = 0
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<held, paused, resumed, mergePending, endQueued, replyOverResume,
                   splitTurn>>
    \* A new reply: any length, synthesized or not yet. As shipped, a caller already
    \* talking as the bot starts is paused at the start (with what is known of it).
    /\ \E l \in {"long", "mid", "short"}, d \in BOOLEAN :
         /\ left' = IF PAUSE = "off" THEN "long" ELSE l
         /\ synthDone' = (PAUSE /= "off" /\ d)
         /\ pausedP' = (PAUSE = "shipped" /\ userSpeaking
                        /\ ~(synthDone' /\ left' = "short"))
         /\ tailKnown' = (pausedP' /\ synthDone')
    /\ UNCHANGED <<wouldEnd, stopHeard, answerLost>>
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars
    /\ UNCHANGED postVars

(***************************************************************************)
(* The reply hold.                                                         *)
(***************************************************************************)

\* The resumed speech ended and its segment closed with no words (a cough, a breath).
Wordless ==
    /\ ~endQueued                             \* the session has not ended
    /\ (resumed \/ pend) /\ ~userSpeaking /\ ~stopInFlight
    /\ resumed' = FALSE
    \* No turn will come of it: an answer discarded under it is lost.
    \* (FollowupGate does not see the close: its own timer is GateQuiet.)
    /\ pend' = FALSE /\ asUnder' = FALSE
    /\ ansLost' = (ansLost \/ answer = "dropped")
    /\ answer' = IF answer = "dropped" THEN "none" ELSE answer
    /\ UNCHANGED <<gpend, ansOver, skipLost, gstale>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<reply, armed, held, paused, mergePending, unanswered, endQueued,
                   replyOverResume, splitTurn>>
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars

\* The release timer: TEAPORT_REPLY_HOLD_RELEASE_S after the caller goes quiet, the
\* held reply plays. A timer knows nothing of the words: under WORDS_IN_TIME they have
\* resolved first (a turn, or `Wordless`); without it, it fires regardless.
Release ==
    /\ ~endQueued                             \* the session has not ended
    /\ held /\ ~userSpeaking /\ (~WORDS_IN_TIME \/ ~resumed)
    /\ held' = FALSE /\ paused' = FALSE
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<reply, armed, resumed, mergePending, unanswered, endQueued,
                   replyOverResume, splitTurn>>
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    \* A tool round queued behind the hold reaches the context: still running, or done
    \* -- or its InProgress goes on behind the released reply's audio (`transit`).
    /\ \E t \in (CASE tool = "held" -> {"transit", "ctxRunning", "ctxDone"}
                   [] tool = "resultHeld" -> {"ctxDone"}
                   [] OTHER -> {tool}) : tool' = t
    /\ UNCHANGED <<lateTool, goodbyeLost, orphanCall, lateCall, unsettled, readUnsettled>>
    /\ UNCHANGED postVars

\* The hold cap (TEAPORT_REPLY_HOLD_MAX_S): ends a hold whatever the caller is doing.
Cap ==
    /\ ~endQueued                             \* the session has not ended
    /\ held /\ ~WORDS_IN_TIME                 \* the cap: the caller may still be talking
    /\ held' = FALSE /\ paused' = FALSE
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<reply, armed, resumed, mergePending, unanswered, endQueued,
                   replyOverResume, splitTurn>>
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    \* A tool round queued behind the hold reaches the context: still running, or done
    \* -- or its InProgress goes on behind the released reply's audio (`transit`).
    /\ \E t \in (CASE tool = "held" -> {"transit", "ctxRunning", "ctxDone"}
                   [] tool = "resultHeld" -> {"ctxDone"}
                   [] OTHER -> {tool}) : tool' = t
    /\ UNCHANGED <<lateTool, goodbyeLost, orphanCall, lateCall, unsettled, readUnsettled>>
    /\ UNCHANGED postVars

\* The session ends (a hang-up, Talk's close): its EndFrame reaches the gate's queue.
\* Every other action but the caller's speech is guarded by ~endQueued: what TLC
\* checks after it is whether a paused queue holds the End back.
\* As shipped, queue_frame drops the held reply and lifts the pause in the same step;
\* as first written the End waited behind the hold.
\*
\* The user aggregator pushes the End on and then commits whatever words an open turn
\* still holds (pipecat's _maybe_emit_user_turn_stopped(on_session_end=True)), through
\* the same push_aggregation. That commit's context is never read -- nothing answers
\* after the End -- so only what it may do to the reply ahead of the End is modelled:
\* as first cut (retryMergesAtEnd) the gate still armed made it a continuation, whose
\* interruption overtook the End and dropped an unheld reply -- the goodbye, say -- that
\* would have played out before it. As shipped the session-end commit never continues.
Hangup ==
    /\ ~endQueued
    /\ endQueued' = TRUE
    /\ IF Gated /\ HOLD /= "gateEndWaits" /\ held
         THEN held' = FALSE /\ paused' = FALSE /\ reply' = FALSE
         ELSE UNCHANGED <<held, paused, reply>>
    /\ \E words \in (IF userTurn THEN BOOLEAN ELSE {FALSE}) :
         goodbyeLost' = (goodbyeLost \/ (words /\ MODE = "retryMergesAtEnd" /\ armed
                                          /\ cut = "none" /\ reply /\ ~held))
    /\ UNCHANGED <<tool, lateTool, orphanCall, lateCall, unsettled, readUnsettled>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<armed, resumed, mergePending, unanswered, replyOverResume, splitTurn>>
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED postVars

\* The reply to the last commit calls a function (unconditionally here: the model
\* decides). Its FunctionCallInProgressFrame goes on through the gate towards the
\* context (`CallLands`) -- or waits in its queue while it holds.
ToolCall ==
    /\ ~endQueued
    /\ (Gated \/ POSTTOOL /= "off") /\ reply /\ (armed \/ ~Gated) /\ tool = "none"
    /\ cut = "none"
    /\ tool' = IF paused THEN "held" ELSE "transit"
    /\ UNCHANGED <<lateTool, goodbyeLost, orphanCall, lateCall, unsettled, readUnsettled>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED postVars

\* The InProgress reaches the assistant aggregator first: the call and its IN_PROGRESS
\* result are in the context. Behind a late frame still on its way, never ahead of it:
\* control frames on one path keep their order.
CallLands ==
    /\ ~endQueued
    /\ tool = "transit" /\ lateCall = "none"
    /\ tool' = "ctxRunning"
    /\ UNCHANGED <<lateTool, goodbyeLost, orphanCall, lateCall, unsettled, readUnsettled>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED postVars

\* The InProgress its cancel overtook reaches the aggregator (#185). As written it is
\* the call's first news there: the call and IN_PROGRESS go in, the call's task is
\* gone, and nothing will settle it. The drop above the aggregator catches it only if
\* the cancel overtook it there; the drop at the aggregator always does. settleAtCommit
\* writes it, for the next commit to settle.
LateLands ==
    /\ ~endQueued
    /\ lateCall /= "none"
    /\ lateCall' = "none"
    /\ LET dropped == \/ CALLS = "dropAtAggregator"
                      \/ CALLS = "dropAbove" /\ lateCall = "above"
       IN /\ unsettled' = (unsettled \/ ~dropped)
          /\ orphanCall' = (orphanCall \/ (~dropped /\ CALLS /= "settleAtCommit"))
    /\ UNCHANGED <<tool, lateTool, goodbyeLost, readUnsettled>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED postVars

\* Its result lands: a sync tool's (the LLM then answers from it -- the reply goes on),
\* or the consult's placeholder (run_llm=False; the consult runs on). Through the
\* gate: while it holds, the result waits in its queue.
ToolDone ==
    /\ ~endQueued
    /\ tool = "ctxRunning"
    /\ tool' = IF paused THEN "resultHeld" ELSE "ctxDone"
    \* A sync tool's result asks the LLM again: a read of the context, not at a commit.
    /\ readUnsettled' = (readUnsettled \/ unsettled)
    \* POSTTOOL: a sync result passing the gate makes the answer due (a held result's
    \* answer is the reply gate's: not modelled here, see the README).
    /\ \E sync \in (IF POSTTOOL = "off" \/ paused THEN {FALSE} ELSE BOOLEAN) :
         answer' = IF sync THEN "due" ELSE answer
    /\ UNCHANGED <<pend, gpend, asUnder, ansOver, ansLost, skipLost, gstale>>
    /\ UNCHANGED <<lateTool, goodbyeLost, orphanCall, lateCall, unsettled>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars

BotStop ==
    /\ ~endQueued                             \* the session has not ended
    /\ botSpeaking /\ ~pausedP                  \* a paused reply cannot finish
    /\ botSpeaking' = FALSE
    /\ answer' = IF answer = "playing" THEN "none" ELSE answer
    /\ asUnder' = (asUnder /\ answer /= "playing")
    /\ UNCHANGED <<pend, gpend, ansOver, ansLost, skipLost, gstale>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars


(***************************************************************************)
(* The answer after the tool (#17, POSTTOOL).                              *)
(*                                                                          *)
(* A sync tool's result lands; the assistant aggregator asks the LLM for    *)
(* the answer (_maybe_push_context_after_function_result, pushing an        *)
(* LLMContextFrame UPSTREAM), deferring it to BotStoppedSpeaking while the  *)
(* bot is audible -- and, either way, only `if not self._user_speaking`,    *)
(* which is the user TURN (UserStartedSpeakingFrame), not the VAD. The      *)
(* result frame waits behind the filler's audio at the transport, so the    *)
(* push is after the filler either way: one step, `PushAnswer`, guarded by  *)
(* ~botSpeaking. The TTS holds a clause while the VAD hears the caller      *)
(* (engine_tts._hold_for_user_speech), so the answer plays at their VAD     *)
(* stop at the earliest (`AnswerStart`), or at the hold's cap (`TtsCap`).   *)
(*                                                                          *)
(* POSTTOOL = "off"        -- no answer modelled (every row before #17).    *)
(* POSTTOOL = "asWritten"  -- pipecat's push, gated on the turn alone.      *)
(* POSTTOOL = "vadDrop"    -- the issue's filter in front of the LLM: drop  *)
(*                            the push while the VAD hears the caller; the  *)
(*                            caller's own turn will read the result.       *)
(* POSTTOOL = "capDiscard" -- the TTS discards the answer when its speech   *)
(*                            hold ends on the cap, the caller still going. *)
(* POSTTOOL = "lullAtStop" -- FollowupGate holds the push while the VAD     *)
(*                            hears the caller and lets it go at the VAD    *)
(*                            stop (a queue that delivers in the next lull, *)
(*                            "speaking" being the VAD).                    *)
(* POSTTOOL = "gate"       -- FollowupGate holds the push while caller      *)
(*                            speech is pending: from its VAD start until   *)
(*                            its words start a turn (the push is then      *)
(*                            dropped: that turn's commit reads the result) *)
(*                            or TEAPORT_REPLY_HOLD_RELEASE_S after it went *)
(*                            quiet with none (the push goes on). The       *)
(*                            release is a timer: WORDS_IN_TIME, as for the *)
(*                            reply hold.                                   *)
(***************************************************************************)

\* The push, at the aggregator, once the filler's audio is past (or with none). pipecat
\* skips it for an open turn: that turn's commit reads the result -- unless the commit
\* has already run (`inference`: a finalize refused, or in flight), and then nothing
\* ever does (skipLost).
PushAnswer ==
    /\ ~endQueued
    /\ answer = "due" /\ ~botSpeaking /\ cut = "none"
    \* A result that asks for an answer comes of a reply, a round trip after its commit:
    \* never inside the commit's two awaits (the ordering fact BotStart states).
    /\ ~stopInFlight
    /\ IF userTurn
         THEN /\ answer' = "none" /\ asUnder' = FALSE
              /\ skipLost' = (skipLost \/ inference)
         ELSE /\ LET holdIt == \/ POSTTOOL \in Gates /\ (userSpeaking \/ gpend)
                               \/ POSTTOOL = "lullAtStop" /\ userSpeaking
                 IN  IF POSTTOOL = "vadDrop" /\ userSpeaking
                       THEN answer' = "dropped" /\ asUnder' = FALSE
                     ELSE IF holdIt
                       THEN answer' = "held" /\ asUnder' = FALSE
                     ELSE answer' = "asked" /\ asUnder' = pend
              /\ UNCHANGED skipLost
    /\ UNCHANGED <<pend, gpend, ansOver, ansLost, gstale>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars

\* FollowupGate lets the held push go: at the VAD stop (lullAtStop), or (gate) with
\* nothing pending in its view -- a VAD that stopped after its timer already ran.
ReleaseA ==
    /\ ~endQueued
    /\ answer = "held" /\ ~userSpeaking
    /\ POSTTOOL = "lullAtStop" \/ ~gpend
    /\ answer' = "asked" /\ asUnder' = pend
    /\ UNCHANGED <<pend, gpend, ansOver, ansLost, skipLost, gstale>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars

\* FollowupGate's release timer (TEAPORT_REPLY_HOLD_RELEASE_S after the VAD stop, no
\* turn start): the speech is taken as word-less, and a held push goes on. A timer knows
\* nothing of the words: under WORDS_IN_TIME they have resolved first (a turn start, a
\* commit, or the STT's word-less close); without it, it fires regardless.
GateQuiet ==
    /\ ~endQueued
    /\ gpend /\ ~userSpeaking /\ (~WORDS_IN_TIME \/ ~pend)
    /\ gpend' = FALSE /\ gstale' = FALSE
    /\ IF POSTTOOL \in Gates /\ answer = "held"
         THEN answer' = "asked" /\ asUnder' = pend
         ELSE UNCHANGED <<answer, asUnder>>
    /\ UNCHANGED <<pend, ansOver, ansLost, skipLost>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars

\* The gate's cap (TEAPORT_REPLY_HOLD_MAX_S): the caller may still be talking.
CapA ==
    /\ ~endQueued
    /\ answer = "held" /\ ~WORDS_IN_TIME
    \* A turn open by then (its start reached the gate, its interruption not yet):
    \* that turn's commit reads the result, so the cap drops the answer rather than
    \* ask for a second completion.
    /\ IF userTurn THEN answer' = "none" /\ asUnder' = FALSE
                   ELSE answer' = "asked" /\ asUnder' = pend
    /\ UNCHANGED <<pend, gpend, ansOver, ansLost, skipLost, gstale>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars

\* The answer's first audio reaches the transport, through the reply gate (it holds an
\* armed reply: a tool round with no filler) -- as BotStart, and like it the gate is
\* disarmed. As shipped, a caller already talking (only past the TTS cap) is paused at
\* its start.
StartAnswer ==
    /\ answer' = "playing" /\ botSpeaking' = TRUE
    /\ reply' = FALSE /\ armed' = FALSE /\ unanswered' = 0
    /\ UNCHANGED <<pend, gpend, asUnder, ansOver, ansLost, skipLost, gstale>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<held, paused, resumed, mergePending, endQueued, replyOverResume,
                   splitTurn>>
    /\ \E l \in {"long", "mid", "short"}, d \in BOOLEAN :
         /\ left' = IF PAUSE = "off" THEN "long" ELSE l
         /\ synthDone' = (PAUSE /= "off" /\ d)
         /\ pausedP' = (PAUSE = "shipped" /\ userSpeaking
                        /\ ~(synthDone' /\ left' = "short"))
         /\ tailKnown' = (pausedP' /\ synthDone')
    /\ UNCHANGED <<wouldEnd, stopHeard, answerLost>>
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars
AnswerReady == ~endQueued /\ answer = "asked" /\ ~botSpeaking /\ ~paused /\ cut = "none"
AnswerStart ==
    /\ AnswerReady /\ ~userSpeaking            \* the TTS's speech hold
    /\ StartAnswer

\* The TTS's speech hold runs out (TTS_USER_SPEECH_HOLD_MAX_S) with the caller still
\* talking: the answer plays -- or, capDiscard, is discarded.
TtsCap ==
    /\ AnswerReady /\ userSpeaking /\ ~WORDS_IN_TIME
    /\ IF POSTTOOL = "capDiscard"
         THEN /\ answer' = "dropped" /\ asUnder' = FALSE
              /\ UNCHANGED <<pend, gpend, ansOver, ansLost, skipLost, gstale>>
              /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                             stopInFlight, owed, turns, spec, specGen, specWords, gen,
                             missedBargeIn, staleReply>>
              /\ UNCHANGED holdVars
              /\ UNCHANGED pauseVars
              /\ UNCHANGED fenceVars
              /\ UNCHANGED toolVars
         ELSE StartAnswer

(***************************************************************************)
(* The 5s force-stop. An INACTIVITY timer: every user action above clears   *)
(* `watchdog`, so reaching it needs a sustained quiet stretch -- which is   *)
(* exactly what a user still trying to be heard does not produce.          *)
(***************************************************************************)

Arm ==
    /\ ~endQueued                             \* the session has not ended
    /\ ~watchdog /\ ~userSpeaking
    /\ watchdog' = TRUE
    /\ UNCHANGED <<userTurn, userSpeaking, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars
    /\ UNCHANGED fenceVars
    /\ UNCHANGED toolVars
    /\ UNCHANGED postVars

\* The watchdog's stop is a commit too: _trigger_user_turn_stop pushes the
\* aggregation, which reaches get_chat_completions and Speculator.take, so a live
\* speculation is decided here exactly as at Inference.
ForceStop ==
    /\ ~endQueued                             \* the session has not ended
    /\ watchdog /\ userTurn /\ ~userSpeaking
    /\ userTurn' = FALSE
    /\ inference' = FALSE
    /\ owed' = FALSE
    /\ watchdog' = FALSE
    \* A commit, unless this turn's words were committed already (a stranded turn: the
    \* aggregation is empty, and pipecat pushes nothing).
    /\ IF ~inference
         THEN CommitReady /\ CommitStep
         ELSE /\ spec' = FALSE
              /\ UNCHANGED <<staleReply, reply, armed, unanswered, mergePending, splitTurn>>
              /\ UNCHANGED <<held, paused, resumed, endQueued, replyOverResume>>
              /\ UNCHANGED fenceVars
              /\ UNCHANGED toolVars
              /\ UNCHANGED postVars
    /\ UNCHANGED <<userSpeaking, botSpeaking, stopInFlight, turns, specGen, specWords, gen,
                   missedBargeIn>>
    /\ UNCHANGED pauseVars

Next ==
    \/ VadStart \/ Resume \/ Interject \/ OneWord \/ StopWord \/ CutLands
    \/ Wordless \/ ToolDone \/ BotStop
    \/ PushAnswer \/ ReleaseA \/ GateQuiet \/ CapA \/ AnswerStart \/ TtsCap
    \/ VadStop \/ Inference \/ Finalize \/ BotStart \/ Arm \/ ForceStop
    \/ SpecStart \/ CtxChange \/ Retext \/ Release \/ Cap \/ Hangup
    \/ ToolCall \/ CallLands \/ LateLands
    \/ Progress \/ SynthDone \/ TailElapsed \/ ResumeP

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Properties.                                                             *)
(***************************************************************************)

\* THE property, and the one no model in this directory had. A user turn whose
\* inference has already run -- the words are in the context, the LLM is answering
\* them -- must never still be open once the user has fallen quiet. That state is
\* absorbing: an open turn refuses every barge-in, and the only exit is a watchdog
\* the user's own voice keeps re-arming.
NoStrandedTurn == ~(userTurn /\ inference /\ ~userSpeaking /\ ~stopInFlight)

\* The user-visible failure the above causes: speaking over the bot, with enough
\* words, and getting nothing -- after a quiet moment has already passed, so this
\* is the wedge rather than the ordinary wait for a turn to close.
NoMissedBargeIn == ~missedBargeIn

\* A speculated reply is only ever spoken for the context it was asked against. The
\* text being right is not enough: a memory note or a truncated previous reply that
\* landed after the snapshot is context the model never saw. Nor is the rest of the
\* context being right: a final that differs from the interim the snapshot took is
\* words the model never saw.
NoStaleReply == ~staleReply /\ ~replyOverResume

\* The reply hold's merge: a commit never leaves two user messages with no reply
\* between them -- the model answers the caller's whole turn, not a fragment and then
\* its continuation.
NoSplitTurn == ~splitTurn

\* The gate's queue is paused only while a hold is in force: never a bot that has
\* gone silent with nothing holding it (every later reply would wait forever).
NoSilencedBot == ~(paused /\ ~held)

\* The session's End never waits behind a hold.
NoEndBehindHold == ~(endQueued /\ paused)

\* The barge-in pause never loses an answer: a one-word final the caller would have
\* had as a turn without the pause (the reply would already have ended) is one.
NoLostAnswer == ~answerLost

\* A pause is never left with nothing to resume: playout is paused only while the
\* bot is in the middle of a reply.
NoStuckPause == ~(pausedP /\ ~botSpeaking)

\* The reply a barge-in cut is in the context, corrected to what was heard, before
\* anything reads the context for the turn that cut it: its commit, or a speculation's
\* snapshot (which spends the ledger's cut). Otherwise the model is asked with the cut
\* reply missing and then reads it back whole after the caller's words -- live
\* 2026-10-13: "Okay, stop there." over a count, and the model counted again.
NoCutAfterCommit == ~cutAfterCommit

\* Nothing of a reply dropped before it played reaches the context after the turn that
\* dropped it: not its tool round either.
NoLateToolRound == ~lateTool

\* The session-end commit never drops a reply the End was queued behind.
NoLostGoodbye == ~goodbyeLost

\* Every tool call in the context gets a result: none is left unsettled for good.
NoOrphanCall == ~orphanCall

\* The LLM is never asked over a context holding a call nothing is running for and
\* nothing has settled (strict servers refuse it): settling it later is too late.
NoUnsettledRead == ~readUnsettled

\* Reachability witnesses: rows that expect these to FAIL prove the window the
\* answer-loss fix is about is reachable in the design they run -- a pause with the
\* rest of the reply in the 0.5-1 s band, and a tail that elapses during it.
NoPauseInTailBand == ~(pausedP /\ left = "mid")

\* #17. The answer to a tool's result is never asked while the caller is talking and
\* then played before their words: the stale answer over (or just after) them, cut
\* by those words, and their turn answered again.
NoAnswerUnderSpeech == ~ansOver

\* Nothing discards the answer for good: a result that asked for one gets it, or the
\* caller's own turn reads it.
NoLostToolAnswer == ~ansLost

\* pipecat's skip for an open turn never strands a result: the turn's commit is still
\* to come. Fails as pipecat is (#196): not #17's, and not fixed by its gate.
NoSkippedAnswer == ~skipLost

\* The gate never holds the answer on speech whose words a commit has already taken:
\* with nobody talking it is not waiting for anything.
NoHoldOnCommittedSpeech == ~(answer = "held" /\ gstale)
NoTailElapsed == ~wouldEnd

TypeOK ==
    /\ userTurn \in BOOLEAN /\ userSpeaking \in BOOLEAN /\ watchdog \in BOOLEAN
    /\ botSpeaking \in BOOLEAN /\ inference \in BOOLEAN /\ stopInFlight \in BOOLEAN
    /\ owed \in BOOLEAN /\ missedBargeIn \in BOOLEAN
    /\ turns \in 0..MaxTurns
    /\ spec \in BOOLEAN /\ staleReply \in BOOLEAN
    /\ specGen \in 0..MaxCtxWrites /\ gen \in 0..MaxCtxWrites /\ specWords \in BOOLEAN
    /\ SPEC \in {"off", "byText", "byHistory", "byContext"}
    /\ (SPEC = "off" => ~spec)
    /\ HOLD \in {"none", "gate", "gateNoMerge", "gateNoResume", "gateEndWaits",
                 "gateNoEnding", "gateKeepsTool", "gateNoFoldCancelled",
                 "gateDropsResult"}
    /\ WORDS_IN_TIME \in BOOLEAN
    /\ reply \in BOOLEAN /\ armed \in BOOLEAN /\ held \in BOOLEAN /\ paused \in BOOLEAN
    /\ resumed \in BOOLEAN /\ mergePending \in BOOLEAN /\ unanswered \in 0..2
    /\ endQueued \in BOOLEAN /\ replyOverResume \in BOOLEAN /\ splitTurn \in BOOLEAN
    /\ (~Gated => ~armed /\ ~held /\ ~paused)
    /\ PAUSE \in {"off", "asWritten", "aOnly", "tailAtPause", "shipped", "noStopWord",
                  "keepOnInterrupt"}
    /\ pausedP \in BOOLEAN /\ left \in {"long", "mid", "short"} /\ synthDone \in BOOLEAN
    /\ tailKnown \in BOOLEAN /\ wouldEnd \in BOOLEAN /\ stopHeard \in BOOLEAN
    /\ answerLost \in BOOLEAN
    /\ (PAUSE = "off" => ~pausedP)
    /\ MODE \in {"asWritten", "retryOnQuiet", "retryMerges", "retryMergesAtEnd"}
    /\ EARLY_FINAL \in BOOLEAN
    /\ FENCE \in {"none", "commitOnly", "inTime", "timed"}
    /\ cut \in {"none", "empty", "reply"} /\ overtaken \in BOOLEAN
    /\ cutAfterCommit \in BOOLEAN
    /\ (overtaken => cut /= "none")
    /\ tool \in {"none", "held", "transit", "ctxRunning", "resultHeld", "ctxDone",
                 "ctxCancelled", "ctxOrphan"}
    /\ lateTool \in BOOLEAN /\ goodbyeLost \in BOOLEAN /\ orphanCall \in BOOLEAN
    /\ CALLS \in {"asWritten", "dropAbove", "settleAtCommit", "dropAtAggregator"}
    /\ lateCall \in {"none", "above", "atAgg"}
    /\ unsettled \in BOOLEAN /\ readUnsettled \in BOOLEAN
    /\ POSTTOOL \in {"off", "asWritten", "vadDrop", "capDiscard", "lullAtStop", "gate",
                     "gateNoCommit"}
    /\ answer \in {"none", "due", "held", "asked", "playing", "dropped"}
    /\ pend \in BOOLEAN /\ gpend \in BOOLEAN /\ asUnder \in BOOLEAN /\ ansOver \in BOOLEAN
    /\ ansLost \in BOOLEAN /\ skipLost \in BOOLEAN /\ gstale \in BOOLEAN
    /\ (POSTTOOL = "off" => answer = "none" /\ ~pend /\ ~gpend)
=============================================================================
