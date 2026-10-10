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
(* is that gap, and it is the only interleaving this module needs.          *)
(*                                                                          *)
(* MODE = "asWritten"    -- stock pipecat (identical on 1.5.0/1.7.0/1.8.1). *)
(* MODE = "retryOnQuiet" -- keep_barge_in_reachable: a finalization refused *)
(*                          after its inference ran is re-applied at the    *)
(*                          user's next quiet moment.                       *)
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
          PAUSE            \* the barge-in pause's design (see the header)

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

NearEndRule  == PAUSE \notin {"off", "asWritten"}
Counterfact  == PAUSE \notin {"off", "asWritten", "aOnly"}
LateTail     == PAUSE \in {"shipped", "noStopWord", "keepOnInterrupt"}
vars == <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
          stopInFlight, owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply,
          holdVars, pauseVars>>

Gated == HOLD /= "none"

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
    /\ UNCHANGED <<userTurn, botSpeaking, inference, stopInFlight, owed, turns,
                   specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<reply, armed, mergePending, unanswered, endQueued, replyOverResume,
                   splitTurn>>

\* The user falls quiet. In retryOnQuiet the owed finalization is applied HERE,
\* inside the same process_frame() call that cleared _user_speaking -- so it is
\* atomic with respect to every other coroutine, exactly as the wrapper is.
VadStop ==
    /\ ~endQueued                             \* the session has not ended
    /\ userSpeaking
    /\ userSpeaking' = FALSE
    /\ watchdog' = FALSE
    /\ IF MODE = "retryOnQuiet" /\ owed /\ userTurn
         THEN /\ userTurn' = FALSE
              /\ owed' = FALSE
              /\ inference' = FALSE
         ELSE UNCHANGED <<userTurn, owed, inference>>
    /\ UNCHANGED <<botSpeaking, stopInFlight, turns, spec, specGen, specWords, gen,
                   missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars

(***************************************************************************)
(* The turn.                                                               *)
(***************************************************************************)

\* MinWordsUserTurnStartStrategy fired: >= INTERRUPT_MIN_WORDS of transcript.
\* This is the ONLY producer of an interruption in the brain.
\* A user turn starts, and with it the interruption: the bot is cut. One definition
\* for every transcript that starts a turn (Interject, and the pause's one-word answer
\* and stop word below).
StartTurn ==
    /\ userTurn' = TRUE
    /\ botSpeaking' = FALSE
    /\ inference' = FALSE
    /\ turns' = turns + 1
    /\ spec' = FALSE               \* a new turn: whatever was asked is not this
    \* The words were for speech that began after the commit, and a reply is
    \* playing that started over it: the stale reply, cut only now.
    /\ replyOverResume' = (replyOverResume \/ (botSpeaking /\ resumed))
    \* The interruption cancels the reply. A held one is dropped: pipecat's
    \* reset empties the queue; the gate lifts the pause itself -- except as
    \* first written, where an Uninterruptible frame in hand left it paused.
    /\ reply' = FALSE /\ armed' = FALSE /\ held' = FALSE /\ resumed' = FALSE
    /\ paused' = (HOLD = "gateNoResume" /\ held)
    \* None of the last commit's reply was played: the next commit finishes it.
    /\ mergePending' = (mergePending \/ (armed /\ HOLD \notin {"none", "gateNoMerge"}))
    /\ UNCHANGED <<unanswered, endQueued, splitTurn>>
    \* The interruption reaches the transport: the queued audio is dropped and, with
    \* it, the pause -- except in a transport that keeps it.
    /\ pausedP' = (PAUSE = "keepOnInterrupt" /\ pausedP)
    /\ tailKnown' = FALSE /\ wouldEnd' = FALSE /\ stopHeard' = FALSE
    /\ UNCHANGED <<left, synthDone, answerLost>>

\* MinWordsUserTurnStartStrategy fired: >= INTERRUPT_MIN_WORDS of transcript.
\* This is the ONLY producer of an interruption in the brain.
Interject ==
    /\ ~endQueued                             \* the session has not ended
    /\ turns < MaxTurns
    /\ userSpeaking
    /\ IF userTurn
         THEN \* _trigger_user_turn_start returns early. The strategy logged
              \* should_trigger=True and nothing came of it. Count it as a MISSED
              \* barge-in only once the user has had a quiet moment since the
              \* inference ran:
              \* before that, an open turn is the ordinary state of someone still
              \* talking, and refusing a second start is correct.
              /\ missedBargeIn' = (missedBargeIn \/ (inference /\ ~owed /\ botSpeaking))
              /\ UNCHANGED <<userTurn, botSpeaking, inference, turns, spec>>
              /\ UNCHANGED holdVars
              /\ UNCHANGED pauseVars
         ELSE /\ StartTurn
              /\ UNCHANGED missedBargeIn
    /\ watchdog' = FALSE
    /\ UNCHANGED <<userSpeaking, stopInFlight, owed, specGen, specWords, gen, staleReply>>

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
    /\ UNCHANGED <<userSpeaking, watchdog, stopInFlight, owed, specGen, specWords, gen, staleReply>>

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
    /\ UNCHANGED <<userSpeaking, watchdog, stopInFlight, owed, specGen, specWords, gen, staleReply>>

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
    /\ spec' = TRUE
    /\ specGen' = gen
    /\ specWords' = TRUE
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars

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

Inference ==
    /\ ~endQueued                             \* the session has not ended
    /\ userTurn /\ ~stopInFlight /\ ~inference /\ ~userSpeaking
    /\ inference' = TRUE
    /\ stopInFlight' = TRUE
    /\ watchdog' = FALSE                       \* _trigger_user_turn_inference_triggered
    /\ spec' = FALSE
    /\ staleReply' = (staleReply \/ Stale)
    \* The commit: a reply is on its way, and on_user_turn_inference_triggered arms
    \* the gate. The aggregator appended this turn's user message -- which the
    \* corrector folds into the unanswered one when a merge is pending.
    /\ reply' = TRUE /\ armed' = Gated
    /\ unanswered' = IF mergePending THEN unanswered ELSE (IF unanswered < 2 THEN unanswered + 1 ELSE 2)
    /\ mergePending' = FALSE
    /\ splitTurn' = (splitTurn \/ (~mergePending /\ unanswered >= 1))
    /\ UNCHANGED <<userTurn, userSpeaking, botSpeaking, owed, turns, specGen, specWords, gen,
                   missedBargeIn>>
    /\ UNCHANGED <<held, paused, resumed, endQueued, replyOverResume>>
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
    /\ UNCHANGED <<userTurn, botSpeaking, inference, stopInFlight, owed, turns,
                   spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<reply, armed, mergePending, unanswered, endQueued, replyOverResume,
                   splitTurn>>
    /\ UNCHANGED pauseVars

\* trigger_user_turn_stopped(), second await.
Finalize ==
    /\ ~endQueued                             \* the session has not ended
    /\ stopInFlight
    /\ stopInFlight' = FALSE
    /\ IF userSpeaking
         THEN \* "Never finalize while the user is audibly speaking." The turn stays
              \* open -- with the inference already spent.
              /\ UNCHANGED <<userTurn, inference>>
              /\ owed' = (MODE = "retryOnQuiet")
         ELSE /\ userTurn' = FALSE
              /\ inference' = FALSE
              /\ owed' = FALSE
    /\ UNCHANGED <<userSpeaking, watchdog, botSpeaking, turns, spec, specGen, specWords, gen,
                   missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars

\* The reply the inference produced reaches the transport. It cannot overtake the
\* finalize in practice -- an LLM round trip plus synthesis against two awaits --
\* so ~stopInFlight is an ordering FACT, stated rather than modelled. See the
\* limits note in README.md.
BotStart ==
    /\ ~endQueued                             \* the session has not ended
    /\ (inference \/ reply) /\ ~stopInFlight /\ ~botSpeaking
    /\ ~paused                                 \* the gate holds it
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

(***************************************************************************)
(* The reply hold.                                                         *)
(***************************************************************************)

\* The resumed speech ended and its segment closed with no words (a cough, a breath).
Wordless ==
    /\ ~endQueued                             \* the session has not ended
    /\ resumed /\ ~userSpeaking /\ ~stopInFlight
    /\ resumed' = FALSE
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<reply, armed, held, paused, mergePending, unanswered, endQueued,
                   replyOverResume, splitTurn>>
    /\ UNCHANGED pauseVars

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

\* The session ends (a hang-up, Talk's close): its EndFrame reaches the gate's queue.
\* Every other action but the caller's speech is guarded by ~endQueued: what TLC
\* checks after it is whether a paused queue holds the End back.
\* As shipped, queue_frame drops the held reply and lifts the pause in the same step;
\* as first written the End waited behind the hold.
Hangup ==
    /\ ~endQueued
    /\ endQueued' = TRUE
    /\ IF Gated /\ HOLD /= "gateEndWaits" /\ held
         THEN held' = FALSE /\ paused' = FALSE /\ reply' = FALSE
         ELSE UNCHANGED <<held, paused, reply>>
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED <<armed, resumed, mergePending, unanswered, replyOverResume, splitTurn>>
    /\ UNCHANGED pauseVars

BotStop ==
    /\ ~endQueued                             \* the session has not ended
    /\ botSpeaking /\ ~pausedP                  \* a paused reply cannot finish
    /\ botSpeaking' = FALSE
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, inference, stopInFlight,
                   owed, turns, spec, specGen, specWords, gen, missedBargeIn, staleReply>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars

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
    /\ spec' = FALSE
    /\ staleReply' = (staleReply \/ Stale)
    /\ UNCHANGED <<userSpeaking, botSpeaking, stopInFlight, turns, specGen, specWords, gen,
                   missedBargeIn>>
    /\ UNCHANGED holdVars
    /\ UNCHANGED pauseVars

Next ==
    \/ VadStart \/ VadStop \/ Interject
    \/ Inference \/ Resume \/ Finalize
    \/ BotStart \/ BotStop
    \/ Arm \/ ForceStop
    \/ SpecStart \/ CtxChange \/ Retext
    \/ Wordless \/ Release \/ Cap \/ Hangup
    \/ Progress \/ SynthDone \/ TailElapsed \/ ResumeP \/ OneWord \/ StopWord

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

\* Reachability witnesses: rows that expect these to FAIL prove the window the
\* answer-loss fix is about is reachable in the design they run -- a pause with the
\* rest of the reply in the 0.5-1 s band, and a tail that elapses during it.
NoPauseInTailBand == ~(pausedP /\ left = "mid")
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
                 "gateNoEnding"}
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
=============================================================================
