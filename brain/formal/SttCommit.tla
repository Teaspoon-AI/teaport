-------------------------------- MODULE SttCommit --------------------------------
(***************************************************************************)
(* teaport -- when the STT closes the engine's transcript segment (#43).    *)
(*                                                                          *)
(*   brain/teaport_brain/stt.py         -- process_frame, _handle_verdict,  *)
(*                                         the hold's expiry, the backstop  *)
(*   brain/teaport_brain/endpointing.py -- TurnVerdictFrame, the pushes in  *)
(*                                         LateStartTurnStopStrategy        *)
(*                                                                          *)
(* A commit is irreversible on the engine side: finish() right-pads and     *)
(* redecodes the buffer, then resets the stream. So WHEN the STT sends it   *)
(* decides whether a caller's pause splits their sentence into two decodes  *)
(* that never see each other. Two designs, selected by MODE:                *)
(*                                                                          *)
(*   "vadStop"  -- commit on VADUserStoppedSpeakingFrame, before Smart Turn *)
(*                 has answered. What stt.py did until #43.                 *)
(*   "verdict"  -- the stop strategy answers every VAD stop with a          *)
(*                 TurnVerdictFrame pushed UPSTREAM: the model's verdict,   *)
(*                 and the silence ceiling's when an INCOMPLETE one falls   *)
(*                 through. The STT commits on a complete one and HOLDS the *)
(*                 segment open on an incomplete one; a VAD start releases  *)
(*                 the hold (the words join the segment); a timer expires   *)
(*                 the hold if no verdict ever comes.                       *)
(*                                                                          *)
(* TURNSTOP selects what the strategy does when the turn is closed under a  *)
(* running ceiling by something other than the ceiling -- the controller    *)
(* re-applying a stop it refused earlier (keep_barge_in_reachable), or its  *)
(* stop watchdog. The controller resets the strategy and clears the         *)
(* analyzer either way, so the ceiling stops counting and its verdict never *)
(* comes:                                                                   *)
(*                                                                          *)
(*   "silent"   -- nothing pushed. The first cut of #43: the STT held the   *)
(*                 segment to its expiry and logged a lost frame.           *)
(*   "verdict"  -- a completing verdict pushed at the close                 *)
(*                 (handle_user_turn_stopped), so the held segment closes   *)
(*                 with the turn. As shipped.                               *)
(*                                                                          *)
(* The machine is two processors talking through pipeline queues in         *)
(* opposite directions. VAD edges reach the STT first (it is the first      *)
(* processor after the transport) and the strategy later, through the       *)
(* aggregator's queue; verdicts travel back up the same way. Each queue is  *)
(* FIFO, the two are independent, so a verdict pushed for a stop can arrive *)
(* after the VAD START that followed it. Add the stranded backstop (armed   *)
(* off interims, for a VAD stop that never comes) and the hold's expiry,    *)
(* both timer TASKS, and the question is whether any interleaving leaves    *)
(* words in a segment nothing will ever close, or closes one twice.         *)
(*                                                                          *)
(* Two faults are in the environment, bounded: a verdict frame lost in      *)
(* flight (LoseVerdict -- the model raising before the push, a processor    *)
(* dropping it), and a VAD stop the VAD never reports (VadStopMissed -- the *)
(* fault the backstop exists for). The properties must hold with both.      *)
(*                                                                          *)
(* Durations are out of scope, as everywhere in this directory. Two timing  *)
(* facts are STATED as enabling conditions rather than modelled: the hold's *)
(* expiry (SMARTTURN_STOP_SECS + slack) outlasts any verdict still on its   *)
(* way, and the backstop (1.5 s of interim quiet) can fire whenever the     *)
(* segment has words and no stop has claimed them -- including while the    *)
(* caller is still talking, which is the known edge stt.py documents.      *)
(***************************************************************************)
EXTENDS Naturals, Sequences

CONSTANTS MODE,        \* "vadStop" | "verdict"
          MaxStops,    \* bound on VAD stops the caller makes
          MaxLost,     \* bound on verdict frames the fault drops
          MaxMissed,   \* bound on VAD stops the VAD fails to report
          TURNSTOP     \* "silent" | "verdict": the strategy at a turn closed under its ceiling

VARIABLES
    \* --- the VAD, as the transport reports it ---
    userSpeaking,
    \* --- frames in flight ---
    toStrategy,     \* VAD edges the STT has seen and the strategy has not
    toStt,          \* verdicts the strategy has pushed and the STT has not seen
    \* --- LateStartTurnStopStrategy + BaseSmartTurn ---
    stratSpeaking,  \* _vad_user_speaking
    ceilingArmed,   \* an INCOMPLETE verdict's silence clock is running
    stratStops,     \* VAD stops the strategy has processed (numbers its verdicts)
    \* --- TeaportSTTService and the engine's open segment ---
    spoke,          \* audio has been fed since the last commit
    segSpeech,      \* the segment holds words: the engine streamed interims for it
    pending,        \* _commit_pending: a VAD stop seen, its commit deferred
    hold,           \* the hold's expiry timer is armed
    backstop,       \* the stranded backstop is armed
    \* --- bounds ---
    stops, lost, missed,
    lostHere,       \* a verdict for the CURRENT stop was lost in flight
    \* --- monitors ---
    closedUnjudged, \* the segment was closed for a stop the strategy has not called done
    answered,       \* a commit has answered the current VAD stop
    split,          \* a stop the model called INCOMPLETE, the caller proved right,
                    \* and the segment was already closed under it
    doubleAnswer,   \* two commits answered one VAD stop
    staleClose      \* a verdict closed the segment after the caller had resumed

vars == <<userSpeaking, toStrategy, toStt, stratSpeaking, ceilingArmed, stratStops,
          spoke, segSpeech, pending, hold, backstop, stops, lost, missed, lostHere,
          closedUnjudged, answered, split, doubleAnswer, staleClose>>

Init ==
    /\ userSpeaking = FALSE
    /\ toStrategy = <<>> /\ toStt = <<>>
    /\ stratSpeaking = FALSE /\ ceilingArmed = FALSE /\ stratStops = 0
    /\ spoke = FALSE /\ segSpeech = FALSE
    /\ pending = FALSE /\ hold = FALSE /\ backstop = FALSE
    /\ stops = 0 /\ lost = 0 /\ missed = 0 /\ lostHere = FALSE
    /\ closedUnjudged = FALSE /\ answered = FALSE
    /\ split = FALSE /\ doubleAnswer = FALSE /\ staleClose = FALSE

(***************************************************************************)
(* What every commit does to the STT -- _send_commit and the engine's       *)
(* reset. The segment closes; the timers that were closing it are done; a   *)
(* caller still talking keeps feeding the next segment.                     *)
(***************************************************************************)
Commit ==
    /\ segSpeech' = FALSE
    /\ spoke' = userSpeaking
    /\ pending' = FALSE
    /\ hold' = FALSE
    /\ backstop' = FALSE

(***************************************************************************)
(* The VAD. The STT is the first processor after the transport, so its      *)
(* handling of an edge is atomic with the edge here; the strategy's copy    *)
(* goes into toStrategy.                                                    *)
(***************************************************************************)

\* The caller starts, or resumes. A held segment is released -- the caller's next
\* words join it -- and, if it holds words, the backstop takes them back into its
\* care (their own deltas would re-arm it; this covers a resume the engine decodes
\* nothing for, followed by a stop the VAD misses). The monitor: if the strategy
\* had called the stop INCOMPLETE and the segment was already closed under it, the
\* caller has just proved the model right and the sentence is in two decodes.
VadStart ==
    /\ ~userSpeaking
    /\ userSpeaking' = TRUE
    /\ spoke' = TRUE
    /\ toStrategy' = Append(toStrategy, "start")
    /\ backstop' = (backstop \/ (pending /\ segSpeech))
    /\ pending' = FALSE
    /\ hold' = FALSE
    /\ split' = (split \/ (closedUnjudged /\ ceilingArmed))
    /\ UNCHANGED <<toStt, stratSpeaking, ceilingArmed, stratStops, segSpeech, stops,
                   lost, missed, lostHere, closedUnjudged, answered, doubleAnswer,
                   staleClose>>

\* The caller falls quiet and the VAD says so. The backstop's premise -- that this
\* frame never comes -- is gone whichever path commits. vadStop commits here, before
\* any verdict exists; verdict puts the segment on hold and arms the hold's expiry.
VadStop ==
    /\ userSpeaking /\ stops < MaxStops
    /\ userSpeaking' = FALSE
    /\ stops' = stops + 1
    /\ lostHere' = FALSE
    /\ toStrategy' = Append(toStrategy, "stop")
    /\ IF MODE = "vadStop"
         THEN /\ Commit
              /\ closedUnjudged' = TRUE
              /\ answered' = TRUE
         ELSE /\ pending' = TRUE
              /\ hold' = TRUE
              /\ backstop' = FALSE
              /\ closedUnjudged' = FALSE
              /\ answered' = FALSE
              /\ UNCHANGED <<spoke, segSpeech>>
    /\ UNCHANGED <<toStt, stratSpeaking, ceilingArmed, stratStops, lost, missed, split,
                   doubleAnswer, staleClose>>

\* The caller falls quiet and the VAD does NOT say so. Nobody sees it: not the STT,
\* not the strategy. The stop the caller made is unanswerable by design; what the
\* properties ask is that the words already in the segment are still closed.
VadStopMissed ==
    /\ userSpeaking /\ missed < MaxMissed
    /\ userSpeaking' = FALSE
    /\ missed' = missed + 1
    /\ UNCHANGED <<toStrategy, toStt, stratSpeaking, ceilingArmed, stratStops, spoke,
                   segSpeech, pending, hold, backstop, stops, lost, lostHere,
                   closedUnjudged, answered, split, doubleAnswer, staleClose>>

\* The engine streams an interim for the audio it was fed -- during the speech or,
\* since its deltas lag, after the stop. Each one re-arms the backstop, UNLESS the
\* segment is held: then the stop the backstop guards against missing has come, and
\* a backstop commit would land inside the ceiling's wait (_arm_stranded_commit).
Delta ==
    /\ spoke
    /\ segSpeech' = TRUE
    /\ backstop' = (backstop \/ ~pending)
    /\ UNCHANGED <<userSpeaking, toStrategy, toStt, stratSpeaking, ceilingArmed,
                   stratStops, spoke, pending, hold, stops, lost, missed, lostHere,
                   closedUnjudged, answered, split, doubleAnswer, staleClose>>

(***************************************************************************)
(* The stop strategy.                                                      *)
(***************************************************************************)

\* _handle_vad_user_started_speaking: _discard_pending_end_of_turn, and the
\* analyzer's silence clock resets on speech.
StratStart ==
    /\ toStrategy /= <<>> /\ Head(toStrategy) = "start"
    /\ toStrategy' = Tail(toStrategy)
    /\ stratSpeaking' = TRUE
    /\ ceilingArmed' = FALSE
    /\ UNCHANGED <<userSpeaking, toStt, stratStops, spoke, segSpeech, pending, hold,
                   backstop, stops, lost, missed, lostHere, closedUnjudged, answered,
                   split, doubleAnswer, staleClose>>

\* _handle_vad_user_stopped_speaking: the model runs and answers -- the answer is the
\* environment's choice -- and the verdict is pushed upstream, numbered with the stop
\* it answers. INCOMPLETE leaves the analyzer's speech buffer live, so its silence
\* ceiling starts counting; COMPLETE clears it. A COMPLETE verdict also vindicates a
\* segment already closed under this stop: the model agrees the caller was done.
StratStop ==
    /\ toStrategy /= <<>> /\ Head(toStrategy) = "stop"
    /\ toStrategy' = Tail(toStrategy)
    /\ stratSpeaking' = FALSE
    /\ stratStops' = stratStops + 1
    /\ \E v \in {"complete", "incomplete"}:
         /\ toStt' = Append(toStt, [v |-> v, n |-> stratStops + 1])
         /\ ceilingArmed' = (v = "incomplete")
         /\ closedUnjudged' = (closedUnjudged /\ v = "incomplete")
    /\ UNCHANGED <<userSpeaking, spoke, segSpeech, pending, hold, backstop, stops,
                   lost, missed, lostHere, answered, split, doubleAnswer, staleClose>>

\* BaseSmartTurn.append_audio: SMARTTURN_STOP_SECS of silence since the stop overrides
\* the INCOMPLETE verdict; _handle_input_audio sees _turn_complete flip and pushes the
\* ceiling's verdict. The caller had the whole ceiling and did not resume, so a
\* segment closed under this stop is not a split.
Ceiling ==
    /\ ceilingArmed /\ ~stratSpeaking
    /\ ceilingArmed' = FALSE
    /\ toStt' = Append(toStt, [v |-> "ceiling", n |-> stratStops])
    /\ closedUnjudged' = FALSE
    /\ UNCHANGED <<userSpeaking, toStrategy, stratSpeaking, stratStops, spoke, segSpeech,
                   pending, hold, backstop, stops, lost, missed, lostHere, answered,
                   split, doubleAnswer, staleClose>>

\* The controller closes the turn under a running ceiling on something other than the
\* ceiling: keep_barge_in_reachable re-applying a stop it refused earlier, or the stop
\* watchdog. handle_user_turn_stopped -> _reset() + analyzer.clear(): the ceiling stops
\* counting and its verdict will never come. TURNSTOP = "verdict" is the strategy
\* reporting the close first, so a held segment closes with the turn; "silent" is the
\* first cut, which reported nothing. The split this can cause (a caller who resumes
\* after it) is the one keep_barge_in_reachable accepts, and the monitor is disarmed
\* with the ceiling here on purpose.
TurnStop ==
    /\ ceilingArmed /\ ~stratSpeaking
    /\ ceilingArmed' = FALSE
    /\ toStt' = IF TURNSTOP = "verdict"
                  THEN Append(toStt, [v |-> "stopped", n |-> stratStops])
                  ELSE toStt
    /\ UNCHANGED <<userSpeaking, toStrategy, stratSpeaking, stratStops, spoke, segSpeech,
                   pending, hold, backstop, stops, lost, missed, lostHere, closedUnjudged,
                   answered, split, doubleAnswer, staleClose>>

\* The fault: a verdict that never reaches the STT. Noted against the stop it was for,
\* if that is the current one -- an earlier stop's, still in flight, excuses nothing.
LoseVerdict ==
    /\ toStt /= <<>> /\ lost < MaxLost
    /\ toStt' = Tail(toStt)
    /\ lost' = lost + 1
    /\ lostHere' = (lostHere \/ Head(toStt).n = stops)
    /\ UNCHANGED <<userSpeaking, toStrategy, stratSpeaking, ceilingArmed, stratStops,
                   spoke, segSpeech, pending, hold, backstop, stops, missed,
                   closedUnjudged, answered, split, doubleAnswer, staleClose>>

(***************************************************************************)
(* The STT.                                                                *)
(***************************************************************************)

\* _handle_verdict. With no commit pending the verdict answers a stop this segment
\* is not waiting on -- vadStop mode, or a caller who resumed before it landed -- and
\* is ignored. A complete verdict (the model's or the ceiling's) commits; an
\* incomplete one keeps the hold.
Verdict ==
    /\ toStt /= <<>>
    /\ toStt' = Tail(toStt)
    /\ IF ~pending \/ Head(toStt).v = "incomplete"
         THEN UNCHANGED <<spoke, segSpeech, pending, hold, backstop, answered,
                          doubleAnswer, staleClose>>
         ELSE /\ Commit
              /\ answered' = TRUE
              /\ doubleAnswer' = (doubleAnswer \/ answered)
              /\ staleClose' = (staleClose \/ userSpeaking)
    /\ UNCHANGED <<userSpeaking, toStrategy, stratSpeaking, ceilingArmed, stratStops,
                   stops, lost, missed, lostHere, closedUnjudged, split>>

\* _commit_when_hold_expires. TIMING FACT, stated: the expiry outlasts every verdict
\* that is still coming -- one in flight, one for a stop the strategy has not
\* processed yet, or the ceiling's, which fires SMARTTURN_STOP_SECS after the stop
\* and the expiry is that plus slack. So it is enabled only once none of those can
\* arrive, i.e. when a verdict was LOST. The strategy has judged the stop by then
\* (the ceiling fired, or the model said done), so the closure is not unjudged.
HoldExpire ==
    /\ hold /\ pending
    /\ toStt = <<>> /\ toStrategy = <<>>
    /\ ~(ceilingArmed /\ ~stratSpeaking)
    /\ Commit
    /\ answered' = TRUE
    /\ doubleAnswer' = (doubleAnswer \/ answered)
    \* Unjudged only if the strategy still holds the stop as INCOMPLETE and counting --
    \* which the timing fact above rules out. Written this way rather than as FALSE so
    \* that dropping the fact makes the row fall instead of the monitor lie.
    /\ closedUnjudged' = ceilingArmed
    /\ UNCHANGED <<userSpeaking, toStrategy, toStt, stratSpeaking, ceilingArmed,
                   stratStops, stops, lost, missed, lostHere, split, staleClose>>

\* _stranded_commit_after_quiet: 1.5 s after the last interim, the segment still
\* holding words and no VAD stop having claimed them. It answers no stop -- it is for
\* the stop that never came -- so the stop monitors are untouched.
Backstop ==
    /\ backstop /\ segSpeech /\ ~pending
    /\ Commit
    \* A backstop that fired into a HELD segment would have closed it under a stop
    \* nobody has judged. The guard above (stt.py's fire-time check) makes this a
    \* no-op; it is here so that removing the guard makes NoSplitOnIncomplete fall.
    /\ closedUnjudged' = (closedUnjudged \/ pending)
    /\ UNCHANGED <<userSpeaking, toStrategy, toStt, stratSpeaking, ceilingArmed,
                   stratStops, stops, lost, missed, lostHere, answered, split,
                   doubleAnswer, staleClose>>

Next ==
    \/ VadStart \/ VadStop \/ VadStopMissed \/ Delta
    \/ StratStart \/ StratStop \/ Ceiling \/ TurnStop \/ LoseVerdict
    \/ Verdict \/ HoldExpire \/ Backstop

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Properties.                                                             *)
(***************************************************************************)

\* Words in the segment, the caller quiet, and nothing left that will close it: no
\* timer that can fire, no frame in flight either way, no ceiling counting. That state
\* is absorbing -- the words wait for the engine's 15 s auto-segment or the caller's
\* NEXT utterance, and the turn they belong to is never answered. Checked with both
\* faults on, because the hold is what stands the backstop down.
\*
\* ENABLED, not the armed flags: a timer counts as a way out only if its action can
\* actually fire from here. `hold` alone would let a model whose expiry never fires
\* pass with the segment held forever. A frame in flight is a way out only in the
\* sense that the state after it is delivered (or lost) is checked too.
WayOut ==
    \/ ENABLED Backstop
    \/ ENABLED HoldExpire
    \/ toStt /= <<>> \/ toStrategy /= <<>>
    \/ (ceilingArmed /\ ~stratSpeaking)

NoStrandedSegment == (segSpeech /\ ~userSpeaking) => WayOut

\* The bug of #43: the segment closed under a stop the model called INCOMPLETE, and
\* the caller then resumed inside the ceiling -- their sentence is two decodes.
NoSplitOnIncomplete == ~split

\* One VAD stop, at most one commit answering it -- the second closes an empty
\* segment, costs the engine's fixed finish() and logs an EMPTY final.
NoDoubleAnswer == ~doubleAnswer

\* A verdict answers the stop it was asked at. Once the caller has audibly resumed,
\* closing the segment on it would cut their sentence at a pause the STT already
\* knows was not the end -- the words belong in this segment, and the next stop asks
\* again. The VAD start reaches the STT before any verdict pushed after it, so this
\* holds by the order of the queues plus one cleared flag; it is here so that the
\* flag cannot quietly stop being cleared.
NoStaleClose == ~staleClose

\* The hold and its expiry are one thing: a held segment always has a way out, and
\* the expiry never fires into a segment nothing holds.
HoldIffPending == hold = pending

\* A held segment always has a verdict on its way -- one in flight, a stop the strategy
\* has yet to process, or a ceiling counting -- unless a verdict for THIS stop was lost.
\* The expiry is the backstop for that loss and for nothing else: a design that leaves
\* a hold with no verdict coming has made the expiry its normal way out, and none of
\* the properties above can see it (the expiry keeps the segment from stranding and
\* answers the stop exactly once). It is also what makes HoldExpire's timing fact a
\* checked one: with this holding, the expiry is reachable only after a loss.
VerdictComing == toStt /= <<>> \/ toStrategy /= <<>> \/ (ceilingArmed /\ ~stratSpeaking)

NoOrphanedHold == pending => (VerdictComing \/ lostHere)

TypeOK ==
    /\ userSpeaking \in BOOLEAN /\ stratSpeaking \in BOOLEAN
    /\ ceilingArmed \in BOOLEAN /\ spoke \in BOOLEAN /\ segSpeech \in BOOLEAN
    /\ stratStops \in 0..MaxStops /\ lostHere \in BOOLEAN
    /\ pending \in BOOLEAN /\ hold \in BOOLEAN /\ backstop \in BOOLEAN
    /\ closedUnjudged \in BOOLEAN /\ answered \in BOOLEAN
    /\ split \in BOOLEAN /\ doubleAnswer \in BOOLEAN /\ staleClose \in BOOLEAN
    /\ stops \in 0..MaxStops /\ lost \in 0..MaxLost /\ missed \in 0..MaxMissed
    /\ \A i \in 1..Len(toStrategy): toStrategy[i] \in {"start", "stop"}
    /\ \A i \in 1..Len(toStt):
         toStt[i] \in [v: {"complete", "incomplete", "ceiling", "stopped"}, n: 1..MaxStops]
    /\ MODE \in {"vadStop", "verdict"} /\ TURNSTOP \in {"silent", "verdict"}
    /\ (MODE = "vadStop" => ~pending /\ ~hold)
=============================================================================
