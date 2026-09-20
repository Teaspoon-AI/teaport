-------------------------------- MODULE SttCommit --------------------------------
(***************************************************************************)
(* teaport -- when the STT closes the engine's transcript segment (#43),    *)
(* and when the turn may end on what comes back (PR #49).                   *)
(*                                                                          *)
(*   brain/teaport_brain/stt.py         -- process_frame, _handle_verdict,  *)
(*                                         the hold's expiry, the backstop; *)
(*                                         _send_commit's queue of stamps,  *)
(*                                         _handle_message's pairing and    *)
(*                                         stamping of a done               *)
(*   brain/teaport_brain/endpointing.py -- TurnVerdictFrame, the pushes in  *)
(*                                         LateStartTurnStopStrategy; the   *)
(*                                         owed close and the gate on       *)
(*                                         _maybe_trigger_user_turn_stopped *)
(*   the engine's voxtral_websocket.c   -- one worker, dones in order, a    *)
(*                                         VAD close and a commit's answer  *)
(*                                         the same message on the wire     *)
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
(*                                                                          *)
(* THE FINAL (PR #49). What the engine returns for a closed segment travels *)
(* back down the same queue as the VAD edges, and the strategy ends the     *)
(* turn on it. Two things make that a protocol of its own: the engine       *)
(* closes segments ON ITS OWN too (its endpointer, on a shorter silence     *)
(* than the VAD's floor -- or a breath inside a sentence), and a done on    *)
(* the wire does not say whether it answers a commit of ours or a close of  *)
(* the engine's. So a final can be the PREVIOUS utterance's, landing        *)
(* inside the next one (the engine's finish takes ~0.7 s; a caller who      *)
(* speaks in bursts starts the next utterance inside it), and the stock     *)
(* strategy ended the turn on it: 38 of 431 replies cut at 0% since         *)
(* 2026-09-16. Two designs for telling whose a final is, selected by DESIGN:*)
(*                                                                          *)
(*   "clock" -- the first cut (PR #49 as opened): the STT stamps each final *)
(*              with the clock time of the commit it answers, or its own    *)
(*              arrival when nothing asked; the strategy takes a final      *)
(*              stamped before its latest VAD start as an earlier           *)
(*              utterance's and holds it back from `_text`. Here, a stamp   *)
(*              is the strategy's count of VAD starts at that instant --    *)
(*              the same order the clock readings encode -- and an unasked  *)
(*              done released a held segment.                               *)
(*   "stop"  -- as shipped: the STT numbers VAD stops as it sees them and   *)
(*              stamps a final with the stop its commit answered (commits   *)
(*              and dones pair up in order); a done nothing asked for is    *)
(*              the NEXT stop's if it has words, the last commit's if it    *)
(*              has none, and releases nothing. The strategy counts the     *)
(*              same frames and, once the VAD has reported a stop, ends the *)
(*              turn only on a close stamped with that stop that arrived    *)
(*              with the caller quiet since. Every path to the turn's end   *)
(*              -- the verdict, the ceiling, the p99 net -- waits for it.   *)
(*                                                                          *)
(* DONES says what the wire carries: "unmarked" is the engine as it is (a   *)
(* done answering a commit and one from the engine's own close are the      *)
(* same message, so the STT pairs by order and the engine's close can take  *)
(* a commit's place in the queue); "marked" is the one-field engine change  *)
(* that says which. The environment's engine close is bounded by MaxCloses. *)
(*                                                                          *)
(* NoStaleTurnEnd is the property: the turn never ends on VAD stop k while  *)
(* words of an utterance up to k are still in the engine's open segment, in *)
(* a done on its way, or in a final queued to the strategy. NoStrandedTurn  *)
(* is its dual: a turn the caller has stopped for always has a way to end.  *)
(***************************************************************************)
EXTENDS Naturals, Sequences

CONSTANTS MODE,        \* "vadStop" | "verdict"
          MaxStops,    \* bound on VAD stops the caller makes
          MaxLost,     \* bound on verdict frames the fault drops
          MaxMissed,   \* bound on VAD stops the VAD fails to report
          TURNSTOP,    \* "silent" | "verdict": the strategy at a turn closed under its ceiling
          DESIGN,      \* "clock" | "stop": whose a final is (see the header)
          DONES,       \* "unmarked" | "marked": whether a done says it answers a commit
          MaxCloses,   \* bound on segments the engine closes on its own
          MaxBackstops \* bound on backstop commits (each opens a segment the caller can refill)

\* Utterances are numbered by VAD start; a done's words are the utterances whose
\* audio was in the segment it closed. NONE is "no stop": owed's rest value.
Utts == 1..(MaxStops + MaxMissed + 1)
NONE == 0

VARIABLES
    \* --- the VAD, as the transport reports it ---
    userSpeaking,
    utt,            \* the caller's latest utterance: VAD starts so far
    \* --- frames in flight ---
    toStrategy,     \* frames the STT has seen or pushed and the strategy has not:
                    \* VAD edges, interims, finals and wordless closes, in order
    toStt,          \* verdicts the strategy has pushed and the STT has not seen
    fromEngine,     \* dones the engine has produced and the STT has not received
    \* --- LateStartTurnStopStrategy + BaseSmartTurn ---
    stratSpeaking,  \* _vad_user_speaking
    ceilingArmed,   \* an INCOMPLETE verdict's silence clock is running
    stratStops,     \* VAD stops the strategy has processed (numbers its verdicts)
    stratStarts,    \* VAD starts the strategy has processed (the clock design's stamp)
    stopUtt,        \* the utterance the latest processed stop ended
    turnOpen,       \* the controller has a user turn open
    text,           \* _text is non-empty
    finalized,      \* _transcript_finalized
    turnComplete,   \* _turn_complete
    vadStopped,     \* _vad_stopped: a VAD stop seen this turn
    netArmed,       \* the p99 safety-net timer is running
    netFired,       \* _timeout_expired: it fired
    owed,           \* the latest stop counted here whose close has not landed, or NONE
                    \* (DESIGN = "stop"; not per turn -- see CanEnd)
    earlier,        \* _earlier_final is held (DESIGN = "clock")
    \* --- TeaportSTTService and the engine's open segment ---
    spoke,          \* audio has been fed since the last close: its deltas may still come
    segAudio,       \* utterances whose audio is in the engine's open segment
    segSpeech,      \* the STT's buffer holds words: the engine streamed interims
    pending,        \* _commit_pending: a VAD stop seen, its commit deferred
    hold,           \* the hold's expiry timer is armed
    backstop,       \* the stranded backstop is armed
    closedUnasked,  \* _closed_unasked: a done nothing asked for since the last start
    commits,        \* _commits: stamps of commits sent and not yet answered, in order
    lastCommitN,    \* _last_commit_n
    \* --- bounds ---
    stops, lost, missed, closes, backstops,
    lostHere,       \* a verdict for the CURRENT stop was lost in flight
    \* --- monitors ---
    closedUnjudged, \* the segment was closed for a stop the strategy has not called done
    answered,       \* a commit has answered the current VAD stop
    split,          \* a stop the model called INCOMPLETE, the caller proved right,
                    \* and the segment was already closed under it
    doubleAnswer,   \* two commits answered one VAD stop
    staleClose,     \* a verdict closed the segment after the caller had resumed
    staleEnd,       \* the turn ended with words of its utterances still to come
    mispaired       \* a done of the engine's own took a commit's place in the queue

vars == <<userSpeaking, utt, toStrategy, toStt, fromEngine,
          stratSpeaking, ceilingArmed, stratStops, stratStarts, stopUtt, turnOpen, text,
          finalized, turnComplete, vadStopped, netArmed, netFired, owed, earlier,
          spoke, segAudio, segSpeech, pending, hold, backstop, closedUnasked, commits,
          lastCommitN, stops, lost, missed, closes, backstops, lostHere,
          closedUnjudged, answered, split, doubleAnswer, staleClose, staleEnd, mispaired>>

\* The strategy's per-turn state, as _reset() + analyzer.clear() leave it.
stratVars == <<turnOpen, text, finalized, turnComplete, vadStopped, netArmed, netFired,
               owed, earlier, ceilingArmed>>

\* A frame on its way to the strategy. VAD edges carry the utterance a stop ended
\* (n, for the stale monitor); a final or a wordless close carries its stamp under
\* both designs (n: the stop it answers; c: the strategy's start count at the commit)
\* and the utterances whose words it holds.
Frame(k, n, c, w) == [kind |-> k, n |-> n, c |-> c, words |-> w]


\* Words of the turn's utterances not yet in the strategy's hands: in the engine's
\* open segment, in a done on its way, or in a final queued behind other frames.
Outstanding ==
    segAudio
      \cup UNION {fromEngine[i].words : i \in 1..Len(fromEngine)}
      \cup UNION {toStrategy[i].words : i \in 1..Len(toStrategy)}

StopInFlight == \E i \in 1..Len(toStrategy): toStrategy[i].kind = "stop"

Init ==
    /\ userSpeaking = FALSE /\ utt = 0
    /\ toStrategy = <<>> /\ toStt = <<>> /\ fromEngine = <<>>
    /\ stratSpeaking = FALSE /\ ceilingArmed = FALSE /\ stratStops = 0 /\ stratStarts = 0
    /\ stopUtt = 0 /\ turnOpen = FALSE /\ text = FALSE /\ finalized = FALSE
    /\ turnComplete = FALSE /\ vadStopped = FALSE /\ netArmed = FALSE /\ netFired = FALSE
    /\ owed = NONE /\ earlier = FALSE
    /\ spoke = FALSE /\ segAudio = {} /\ segSpeech = FALSE
    /\ pending = FALSE /\ hold = FALSE /\ backstop = FALSE /\ closedUnasked = FALSE
    /\ commits = <<>> /\ lastCommitN = 0
    /\ stops = 0 /\ lost = 0 /\ missed = 0 /\ closes = 0 /\ backstops = 0
    /\ lostHere = FALSE
    /\ closedUnjudged = FALSE /\ answered = FALSE
    /\ split = FALSE /\ doubleAnswer = FALSE /\ staleClose = FALSE
    /\ staleEnd = FALSE /\ mispaired = FALSE

(***************************************************************************)
(* What every commit does to the STT -- _send_commit and the engine's       *)
(* reset. The segment closes into a done the engine will answer with (one   *)
(* per final commit, in order); its stamp -- the VAD stop count n, and the  *)
(* strategy's start count c for the clock design -- joins the queue of      *)
(* commits awaiting an answer; the timers that were closing it are done; a  *)
(* caller still talking keeps feeding the next segment.                     *)
(***************************************************************************)
CommitAt(n, speaking) ==
    /\ fromEngine' = Append(fromEngine, [asked |-> TRUE, words |-> segAudio])
    /\ segAudio' = IF speaking THEN {utt} ELSE {}
    /\ spoke' = speaking
    \* Only the design's own stamp is kept: the other is dead state.
    /\ commits' = Append(commits, [n |-> IF DESIGN = "stop" THEN n ELSE 0,
                                   c |-> IF DESIGN = "clock" THEN stratStarts ELSE 0])
    /\ lastCommitN' = n
    /\ segSpeech' = FALSE
    /\ pending' = FALSE
    /\ hold' = FALSE
    /\ backstop' = FALSE

Commit == CommitAt(stops, userSpeaking)

\* The engine closes the segment on its own: its endpointer, on a silence shorter
\* than the VAD's floor or a breath inside a sentence. The words go into a done the
\* wire cannot tell from a commit's answer; a caller mid-word keeps feeding the next
\* segment. The STT learns of it only when the done arrives (Done).
EngineClose ==
    /\ segAudio /= {} /\ closes < MaxCloses
    /\ closes' = closes + 1
    /\ fromEngine' = Append(fromEngine, [asked |-> FALSE, words |-> segAudio])
    /\ segAudio' = IF userSpeaking THEN {utt} ELSE {}
    /\ UNCHANGED <<userSpeaking, utt, toStrategy, toStt, stratSpeaking, stratStops,
                   stratStarts, stopUtt, stratVars, spoke, segSpeech, pending, hold, backstop,
                   closedUnasked, commits, lastCommitN, stops, lost, missed, backstops,
                   lostHere, closedUnjudged, answered, split, doubleAnswer, staleClose,
                   staleEnd, mispaired>>

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
    /\ ~userSpeaking /\ utt < MaxStops + MaxMissed + 1
    /\ userSpeaking' = TRUE
    /\ utt' = utt + 1
    /\ spoke' = TRUE
    /\ segAudio' = segAudio \cup {utt + 1}
    /\ toStrategy' = Append(toStrategy, Frame("start", 0, 0, {}))
    /\ backstop' = (backstop \/ (pending /\ segSpeech))
    /\ pending' = FALSE
    /\ hold' = FALSE
    /\ closedUnasked' = FALSE
    /\ split' = (split \/ (closedUnjudged /\ ceilingArmed))
    /\ UNCHANGED <<toStt, fromEngine, stratSpeaking, stratStops, stratStarts, stopUtt,
                   stratVars, segSpeech, commits, lastCommitN, stops, lost, missed, closes, backstops,
                   lostHere, closedUnjudged, answered, doubleAnswer, staleClose, staleEnd,
                   mispaired>>

\* The caller falls quiet and the VAD says so. The backstop's premise -- that this
\* frame never comes -- is gone whichever path commits. vadStop commits here, before
\* any verdict exists; verdict puts the segment on hold and arms the hold's expiry --
\* unless the engine has closed this utterance's segment on its own already and
\* nothing has streamed since: then what is open is the silent tail after its cut,
\* and the stop commits it at once rather than hold it (stt.py, "tail").
VadStop ==
    /\ userSpeaking /\ stops < MaxStops
    /\ userSpeaking' = FALSE
    /\ stops' = stops + 1
    /\ lostHere' = FALSE
    /\ toStrategy' = Append(toStrategy, Frame("stop", utt, 0, {}))
    /\ IF MODE = "vadStop" \/ (closedUnasked /\ ~segSpeech)
         THEN /\ CommitAt(stops + 1, FALSE)
              /\ closedUnjudged' = (MODE = "vadStop")
              /\ answered' = TRUE
         ELSE /\ pending' = TRUE
              /\ hold' = TRUE
              /\ backstop' = FALSE
              /\ closedUnjudged' = FALSE
              /\ answered' = FALSE
              /\ UNCHANGED <<fromEngine, spoke, segAudio, segSpeech, commits, lastCommitN>>
    /\ UNCHANGED <<utt, toStt, stratSpeaking, stratStops, stratStarts, stopUtt, stratVars,
                   closedUnasked, lost, missed, closes, backstops, split, doubleAnswer, staleClose,
                   staleEnd, mispaired>>

\* The caller falls quiet and the VAD does NOT say so. Nobody sees it: not the STT,
\* not the strategy. The stop the caller made is unanswerable by design; what the
\* properties ask is that the words already in the segment are still closed.
VadStopMissed ==
    /\ userSpeaking /\ missed < MaxMissed
    /\ userSpeaking' = FALSE
    /\ missed' = missed + 1
    /\ UNCHANGED <<utt, toStrategy, toStt, fromEngine, stratSpeaking, stratStops,
                   stratStarts, stopUtt, stratVars, spoke, segAudio, segSpeech, pending, hold,
                   backstop, closedUnasked, commits, lastCommitN, stops, lost, closes, backstops,
                   lostHere, closedUnjudged, answered, split, doubleAnswer, staleClose,
                   staleEnd, mispaired>>

\* The engine streams an interim for the audio it was fed -- during the speech or,
\* since its deltas lag, after the stop. Each one re-arms the backstop, UNLESS the
\* segment is held: then the stop the backstop guards against missing has come, and
\* a backstop commit would land inside the ceiling's wait (_arm_stranded_commit).
\* The interim goes on to the strategy too: an interim means more transcription is
\* still to come, so a final in hand no longer covers the caller's speech. One per
\* segment: further deltas for the same segment change nothing here, and the engine
\* streams none for a new segment until the previous one's finish is done (one
\* worker per session), which is when the STT's buffer empties (Done).
Delta ==
    /\ spoke /\ ~segSpeech
    /\ segSpeech' = TRUE
    /\ backstop' = (backstop \/ ~pending)
    /\ toStrategy' = Append(toStrategy, Frame("delta", 0, 0, {}))
    /\ UNCHANGED <<userSpeaking, utt, toStt, fromEngine, stratSpeaking, stratStops,
                   stratStarts, stopUtt, stratVars, spoke, segAudio, pending, hold, closedUnasked,
                   commits, lastCommitN, stops, lost, missed, closes, backstops, lostHere,
                   closedUnjudged, answered, split, doubleAnswer, staleClose, staleEnd,
                   mispaired>>

(***************************************************************************)
(* The stop strategy.                                                      *)
(***************************************************************************)

\* _reset() + analyzer.clear(): what a turn boundary leaves. The owed close is not
\* the turn's to forget: it is a fact about the STT's segment.
ResetStrategy ==
    /\ turnOpen' = FALSE /\ text' = FALSE /\ finalized' = FALSE /\ turnComplete' = FALSE
    /\ vadStopped' = FALSE /\ netArmed' = FALSE /\ netFired' = FALSE
    /\ earlier' = FALSE /\ ceilingArmed' = FALSE
    /\ UNCHANGED owed

\* _handle_vad_user_started_speaking: _discard_pending_end_of_turn, and the
\* analyzer's silence clock resets on speech. _earlier_final survives (the first cut
\* cleared it only at turn boundaries); so does the owed close.
StratStart ==
    /\ toStrategy /= <<>> /\ Head(toStrategy).kind = "start"
    /\ toStrategy' = Tail(toStrategy)
    /\ stratSpeaking' = TRUE
    /\ stratStarts' = stratStarts + 1
    /\ ceilingArmed' = FALSE
    /\ turnComplete' = FALSE /\ finalized' = FALSE /\ vadStopped' = FALSE
    /\ netArmed' = FALSE /\ netFired' = FALSE
    /\ UNCHANGED <<userSpeaking, utt, toStt, fromEngine, stratStops, stopUtt, turnOpen,
                   text, owed, earlier, spoke, segAudio, segSpeech, pending, hold, backstop,
                   closedUnasked, commits, lastCommitN, stops, lost, missed, closes, backstops,
                   lostHere, closedUnjudged, answered, split, doubleAnswer, staleClose,
                   staleEnd, mispaired>>

\* _handle_vad_user_stopped_speaking: the model runs and answers -- the answer is the
\* environment's choice -- and the verdict is pushed upstream, numbered with the stop
\* it answers. INCOMPLETE leaves the analyzer's speech buffer live, so its silence
\* ceiling starts counting; COMPLETE clears it and calls the turn complete. The p99
\* safety-net timer is armed either way. A COMPLETE verdict also vindicates a
\* segment already closed under this stop: the model agrees the caller was done.
\* The stop is counted (stratStops) and owed its close from here: the STT commits
\* for every stop it sees, or holds and then commits, and a close stamped with this
\* stop cannot have come before it (the STT counts the same frame first).
StratStop ==
    /\ toStrategy /= <<>> /\ Head(toStrategy).kind = "stop"
    /\ toStrategy' = Tail(toStrategy)
    /\ stratSpeaking' = FALSE
    /\ stratStops' = stratStops + 1
    /\ stopUtt' = Head(toStrategy).n
    /\ owed' = stratStops + 1
    /\ vadStopped' = TRUE
    /\ netArmed' = TRUE
    /\ \E v \in {"complete", "incomplete"}:
         /\ toStt' = Append(toStt, [v |-> v, n |-> stratStops + 1])
         /\ ceilingArmed' = (v = "incomplete")
         /\ turnComplete' = (v = "complete")
         /\ closedUnjudged' = (closedUnjudged /\ v = "incomplete")
    /\ UNCHANGED <<userSpeaking, utt, fromEngine, stratStarts, turnOpen, text, finalized,
                   netFired, earlier, spoke, segAudio, segSpeech, pending, hold,
                   backstop, closedUnasked, commits, lastCommitN, stops, lost, missed,
                   closes, backstops, lostHere, answered, split, doubleAnswer, staleClose, staleEnd,
                   mispaired>>

\* handle_user_turn_started, when a frame opens the turn (MinWordsUserTurnStartStrategy
\* on an interim or a final): pipecat's reset -- unless the turn opens after its own
\* VAD stop with the caller quiet, when the verdict and the stop in hand are kept and
\* only _text is cleared (the late-start rule this class exists for). `finalized` is
\* the opening frame's to set.
OpenTurn ==
    /\ turnOpen' = TRUE
    /\ earlier' = FALSE
    /\ IF vadStopped /\ ~stratSpeaking
         THEN UNCHANGED <<turnComplete, vadStopped, netArmed, netFired>>
         ELSE /\ turnComplete' = FALSE /\ vadStopped' = FALSE
              /\ netArmed' = FALSE /\ netFired' = FALSE

\* An interim reaches the strategy: more transcription is coming, so a final in hand
\* no longer covers the caller's speech (pipecat's own rule). It opens the turn if
\* none is.
StratDelta ==
    /\ toStrategy /= <<>> /\ Head(toStrategy).kind = "delta"
    /\ toStrategy' = Tail(toStrategy)
    /\ finalized' = FALSE
    /\ IF turnOpen
         THEN UNCHANGED <<turnOpen, text, earlier, turnComplete, vadStopped, netArmed,
                          netFired>>
         ELSE OpenTurn /\ text' = FALSE
    /\ UNCHANGED <<userSpeaking, utt, toStt, fromEngine, stratSpeaking, ceilingArmed,
                   stratStops, stratStarts, stopUtt, owed, spoke, segAudio, segSpeech, pending, hold,
                   backstop, closedUnasked, commits, lastCommitN, stops, lost, missed,
                   closes, backstops, lostHere, closedUnjudged, answered, split, doubleAnswer,
                   staleClose, staleEnd, mispaired>>

\* A final reaches the strategy (_handle_transcription). It opens the turn if none is
\* (handle_user_turn_started runs first). Then, by design:
\*   "clock": stamped before the strategy's latest VAD start -> an earlier utterance's:
\*            held in _earlier_final, nothing else touched. Otherwise stock: it is
\*            _text, finalized.
\*   "stop":  stock for _text and finalized, always; and its stamp pays the owed
\*            close if it names it -- the caller quiet or not.
\* pipecat's fallback for a transcript with no VAD stop in sight (the caller quiet,
\* no stop this turn) calls the turn complete and arms the net.
StratFinal ==
    /\ toStrategy /= <<>> /\ Head(toStrategy).kind = "final"
    /\ LET f == Head(toStrategy)
           reset == ~turnOpen /\ ~(vadStopped /\ ~stratSpeaking)
           tc0 == IF reset THEN FALSE ELSE turnComplete
           vs0 == IF reset THEN FALSE ELSE vadStopped
           na0 == IF reset THEN FALSE ELSE netArmed
           fz0 == IF reset THEN FALSE ELSE finalized
           tx0 == IF turnOpen THEN text ELSE FALSE
           isEarlier == DESIGN = "clock" /\ f.c < stratStarts
           pays == DESIGN = "stop" /\ f.n = owed
           fallback == ~stratSpeaking /\ ~vs0
       IN /\ toStrategy' = Tail(toStrategy)
          /\ turnOpen' = TRUE
          /\ earlier' = isEarlier
          /\ text' = IF isEarlier THEN tx0 ELSE TRUE
          /\ finalized' = IF isEarlier THEN fz0 ELSE TRUE
          /\ owed' = IF pays THEN NONE ELSE owed
          /\ turnComplete' = IF isEarlier THEN tc0 ELSE (tc0 \/ fallback)
          /\ netArmed' = IF isEarlier THEN na0 ELSE (na0 \/ fallback)
          /\ vadStopped' = vs0
          /\ netFired' = IF reset THEN FALSE ELSE netFired
    /\ UNCHANGED <<userSpeaking, utt, toStt, fromEngine, stratSpeaking, ceilingArmed,
                   stratStops, stratStarts, stopUtt, spoke, segAudio, segSpeech, pending, hold,
                   backstop, closedUnasked, commits, lastCommitN, stops, lost, missed,
                   closes, backstops, lostHere, closedUnjudged, answered, split, doubleAnswer,
                   staleClose, staleEnd, mispaired>>

\* A wordless close reaches the strategy (_handle_segment_done). Opens no turn.
\*   "clock": stamped from before the latest start -> ignored; else, with an earlier
\*            final held, no _text and the VAD stopped, the earlier final is adopted
\*            as the transcript.
\*   "stop":  pays the owed close on the same terms as a final, and then finalizes
\*            whatever _text holds.
StratDone ==
    /\ toStrategy /= <<>> /\ Head(toStrategy).kind = "done"
    /\ LET f == Head(toStrategy)
           pays == DESIGN = "stop" /\ f.n = owed
           adopt == DESIGN = "clock" /\ f.c >= stratStarts /\ earlier /\ ~text /\ vadStopped
       IN /\ toStrategy' = Tail(toStrategy)
          /\ text' = (text \/ adopt)
          /\ earlier' = (earlier /\ ~adopt)
          /\ finalized' = (finalized \/ pays \/ adopt)
          /\ owed' = IF pays THEN NONE ELSE owed
    /\ UNCHANGED <<userSpeaking, utt, toStt, fromEngine, stratSpeaking, ceilingArmed,
                   stratStops, stratStarts, stopUtt, turnOpen, turnComplete, vadStopped,
                   netArmed, netFired, spoke, segAudio, segSpeech, pending, hold, backstop,
                   closedUnasked, commits, lastCommitN, stops, lost, missed, closes, backstops,
                   lostHere, closedUnjudged, answered, split, doubleAnswer, staleClose,
                   staleEnd, mispaired>>

\* BaseSmartTurn.append_audio: SMARTTURN_STOP_SECS of silence since the stop overrides
\* the INCOMPLETE verdict; _handle_input_audio sees _turn_complete flip and pushes the
\* ceiling's verdict. The caller had the whole ceiling and did not resume, so a
\* segment closed under this stop is not a split.
Ceiling ==
    /\ ceilingArmed /\ ~stratSpeaking
    /\ ceilingArmed' = FALSE
    /\ turnComplete' = TRUE
    /\ toStt' = Append(toStt, [v |-> "ceiling", n |-> stratStops])
    /\ closedUnjudged' = FALSE
    /\ UNCHANGED <<userSpeaking, utt, toStrategy, fromEngine, stratSpeaking, stratStops,
                   stratStarts, stopUtt, turnOpen, text, finalized, vadStopped, netArmed,
                   netFired, owed, earlier, spoke, segAudio, segSpeech, pending, hold,
                   backstop, closedUnasked, commits, lastCommitN, stops, lost, missed,
                   closes, backstops, lostHere, answered, split, doubleAnswer, staleClose, staleEnd,
                   mispaired>>

\* _timeout_handler: the STT p99 safety net, armed at every VAD stop, cancelled by a
\* VAD start or a turn boundary. Durations are out of scope, so it can fire at any
\* point after the stop -- including, as live, before any done can exist.
Net ==
    /\ netArmed
    /\ netArmed' = FALSE /\ netFired' = TRUE
    /\ UNCHANGED <<userSpeaking, utt, toStrategy, toStt, fromEngine, stratSpeaking,
                   ceilingArmed, stratStops, stratStarts, stopUtt, turnOpen, text,
                   finalized, turnComplete, vadStopped, owed, earlier, spoke, segAudio,
                   segSpeech, pending, hold, backstop, closedUnasked, commits, lastCommitN,
                   stops, lost, missed, closes, backstops, lostHere, closedUnjudged, answered, split,
                   doubleAnswer, staleClose, staleEnd, mispaired>>

\* _maybe_trigger_user_turn_stopped, and the controller's refusal to finalize while
\* the user is audibly speaking: the turn ends when the model has called it complete
\* and there is a transcript, finalized or past the net. Under DESIGN = "stop", not
\* while the latest VAD stop counted is owed its close -- on any path, the no-stop
\* fallback included, and across turn boundaries: this model's first two finds were
\* the half of a sentence the backstop (MaxCloses = 0) or the engine's own cut had
\* returned, landing after the controller closed the turn under the ceiling, opening
\* a new turn and ending it through the fallback with the held half still on its
\* way. Evaluated inline wherever the code calls it (see Next): the end is
\* immediate.
CanEnd ==
    /\ turnOpen /\ turnComplete /\ text /\ (finalized \/ netFired) /\ ~stratSpeaking
    /\ (DESIGN = "clock" \/ owed = NONE)

\* The turn ends. The monitor: words of the utterances up to the one this stop ended
\* still in the engine's segment, in a done on its way, or in a final still queued --
\* the final that lands next opens a NEW turn on them, and its interruption cuts the
\* reply to this one before a word of it plays.
EndTurn ==
    /\ CanEnd
    /\ staleEnd' = (staleEnd \/ (Outstanding \cap (1..stopUtt) /= {}))
    /\ ResetStrategy
    /\ UNCHANGED <<userSpeaking, utt, toStrategy, toStt, fromEngine, stratSpeaking,
                   stratStops, stratStarts, stopUtt, spoke, segAudio, segSpeech, pending, hold,
                   backstop, closedUnasked, commits, lastCommitN, stops, lost, missed,
                   closes, backstops, lostHere, closedUnjudged, answered, split, doubleAnswer,
                   staleClose, mispaired>>

\* The controller closes the turn under a running ceiling on something other than the
\* ceiling: keep_barge_in_reachable re-applying a stop it refused earlier, or the stop
\* watchdog. handle_user_turn_stopped -> _reset() + analyzer.clear(): the ceiling stops
\* counting and its verdict will never come. TURNSTOP = "verdict" is the strategy
\* reporting the close first, so a held segment closes with the turn; "silent" is the
\* first cut, which reported nothing. The split this can cause (a caller who resumes
\* after it) is the one keep_barge_in_reachable accepts, and the monitor is disarmed
\* with the ceiling here on purpose; the stale-end monitor is not consulted either --
\* the close is accepted, not checked.
TurnStop ==
    /\ turnOpen /\ ceilingArmed /\ ~stratSpeaking
    /\ toStt' = IF TURNSTOP = "verdict"
                  THEN Append(toStt, [v |-> "stopped", n |-> stratStops])
                  ELSE toStt
    /\ ResetStrategy
    /\ UNCHANGED <<userSpeaking, utt, toStrategy, fromEngine, stratSpeaking, stratStops,
                   stratStarts, stopUtt, spoke, segAudio, segSpeech, pending, hold, backstop,
                   closedUnasked, commits, lastCommitN, stops, lost, missed, closes, backstops,
                   lostHere, closedUnjudged, answered, split, doubleAnswer, staleClose,
                   staleEnd, mispaired>>

\* The fault: a verdict that never reaches the STT. Noted against the stop it was for,
\* if that is the current one -- an earlier stop's, still in flight, excuses nothing.
LoseVerdict ==
    /\ toStt /= <<>> /\ lost < MaxLost
    /\ toStt' = Tail(toStt)
    /\ lost' = lost + 1
    /\ lostHere' = (lostHere \/ Head(toStt).n = stops)
    /\ UNCHANGED <<userSpeaking, utt, toStrategy, fromEngine, stratSpeaking, stratStops,
                   stratStarts, stopUtt, stratVars, spoke, segAudio, segSpeech, pending, hold,
                   backstop, closedUnasked, commits, lastCommitN, stops, missed, closes, backstops,
                   closedUnjudged, answered, split, doubleAnswer, staleClose, staleEnd,
                   mispaired>>

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
         THEN UNCHANGED <<fromEngine, spoke, segAudio, segSpeech, pending, hold, backstop,
                          commits, lastCommitN, answered, doubleAnswer, staleClose>>
         ELSE /\ Commit
              /\ answered' = TRUE
              /\ doubleAnswer' = (doubleAnswer \/ answered)
              /\ staleClose' = (staleClose \/ userSpeaking)
    /\ UNCHANGED <<userSpeaking, utt, toStrategy, stratSpeaking, stratStops, stratStarts,
                   stopUtt, stratVars, closedUnasked, stops, lost, missed, closes, backstops,
                   lostHere, closedUnjudged, split, staleEnd, mispaired>>

\* _handle_message on a transcription.done. The done at the head of the wire is paired
\* with the commit at the head of the queue -- exactly when the wire says which dones
\* answer commits (DONES = "marked"), by order when it does not, and then a close of
\* the engine's own takes the commit's place (mispaired). The stamp under "stop": the
\* commit's for its answer; for a done nothing asked, the last commit's if it carries
\* no words (the silent tail behind a mispairing), the next stop's if it does. Under
\* "clock": the commit's start count, or the strategy's start count now. The STT's
\* buffer and the backstop are cleared; a done nothing asked for marks the segment as
\* closed unasked, and -- in the first cut only -- released a hold it landed under.
Done ==
    /\ fromEngine /= <<>>
    /\ LET d == Head(fromEngine)
           paired == IF DONES = "marked" THEN d.asked ELSE commits /= <<>>
           n == IF DESIGN /= "stop" THEN 0
                ELSE IF paired THEN Head(commits).n
                ELSE IF d.words = {} THEN lastCommitN ELSE stops + 1
           c == IF DESIGN /= "clock" THEN 0
                ELSE IF paired THEN Head(commits).c ELSE stratStarts
           kind == IF d.words = {} THEN "done" ELSE "final"
       IN /\ fromEngine' = Tail(fromEngine)
          /\ commits' = IF paired THEN Tail(commits) ELSE commits
          /\ toStrategy' = Append(toStrategy, Frame(kind, n, c, d.words))
          /\ segSpeech' = FALSE
          /\ backstop' = FALSE
          /\ closedUnasked' = (closedUnasked \/ ~paired)
          /\ mispaired' = (mispaired \/ (paired /\ ~d.asked))
          /\ IF DESIGN = "clock" /\ ~paired /\ pending
               THEN pending' = FALSE /\ hold' = FALSE
               ELSE UNCHANGED <<pending, hold>>
    /\ UNCHANGED <<userSpeaking, utt, toStt, stratSpeaking, stratStops, stratStarts,
                   stopUtt, stratVars, spoke, segAudio, lastCommitN, stops, lost, missed, closes, backstops,
                   lostHere, closedUnjudged, answered, split, doubleAnswer, staleClose,
                   staleEnd>>

\* _commit_when_hold_expires. TIMING FACT, stated: the expiry outlasts every verdict
\* that is still coming -- one in flight, one for a stop the strategy has not
\* processed yet, or the ceiling's, which fires SMARTTURN_STOP_SECS after the stop
\* and the expiry is that plus slack. So it is enabled only once none of those can
\* arrive, i.e. when a verdict was LOST. The strategy has judged the stop by then
\* (the ceiling fired, or the model said done), so the closure is not unjudged.
HoldExpire ==
    /\ hold /\ pending
    /\ toStt = <<>> /\ ~StopInFlight
    /\ ~(ceilingArmed /\ ~stratSpeaking)
    /\ Commit
    /\ answered' = TRUE
    /\ doubleAnswer' = (doubleAnswer \/ answered)
    \* Unjudged only if the strategy still holds the stop as INCOMPLETE and counting --
    \* which the timing fact above rules out. Written this way rather than as FALSE so
    \* that dropping the fact makes the row fall instead of the monitor lie.
    /\ closedUnjudged' = ceilingArmed
    /\ UNCHANGED <<userSpeaking, utt, toStrategy, toStt, stratSpeaking, stratStops,
                   stratStarts, stopUtt, stratVars, closedUnasked, stops, lost, missed,
                   closes, backstops, lostHere, split, staleClose, staleEnd, mispaired>>

\* _stranded_commit_after_quiet: 1.5 s after the last interim, the segment still
\* holding words and no VAD stop having claimed them. It answers no stop -- it is for
\* the stop that never came -- so the stop monitors are untouched.
BackstopReady == backstop /\ segSpeech /\ ~pending

Backstop ==
    /\ BackstopReady /\ backstops < MaxBackstops
    /\ backstops' = backstops + 1
    /\ Commit
    \* A backstop that fired into a HELD segment would have closed it under a stop
    \* nobody has judged. The guard above (stt.py's fire-time check) makes this a
    \* no-op; it is here so that removing the guard makes NoSplitOnIncomplete fall.
    /\ closedUnjudged' = (closedUnjudged \/ pending)
    /\ UNCHANGED <<userSpeaking, utt, toStrategy, toStt, stratSpeaking, stratStops,
                   stratStarts, stopUtt, stratVars, closedUnasked, stops, lost, missed,
                   closes, lostHere, answered, split, doubleAnswer, staleClose, staleEnd,
                   mispaired>>

\* The turn's end has priority: the code ends the turn inside the handler that made
\* it possible, so no other step can come between.
Next ==
    \/ EndTurn
    \/ /\ ~CanEnd
       /\ \/ VadStart \/ VadStop \/ VadStopMissed \/ Delta \/ EngineClose
          \/ StratStart \/ StratStop \/ StratDelta \/ StratFinal \/ StratDone
          \/ Ceiling \/ Net \/ TurnStop \/ LoseVerdict
          \/ Verdict \/ Done \/ HoldExpire \/ Backstop

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
\* sense that the state after it is delivered (or lost) is checked too. A done on its
\* way from the engine is one: the buffer's words are in it.
\* (BackstopReady rather than ENABLED Backstop: the bound on backstop firings is a
\* bound on the state space, not a fact about the timer.)
WayOut ==
    \/ BackstopReady
    \/ ENABLED HoldExpire
    \/ toStt /= <<>> \/ StopInFlight \/ fromEngine /= <<>>
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
VerdictComing == toStt /= <<>> \/ StopInFlight \/ (ceilingArmed /\ ~stratSpeaking)

NoOrphanedHold == pending => (VerdictComing \/ lostHere)

\* The bug of PR #49: the turn ended on VAD stop k with words of an utterance up to k
\* still to come. What comes next opens a new turn on them and cuts the reply.
NoStaleTurnEnd == ~staleEnd

\* The same, minus the one cause the STT cannot see: a done of the engine's own that
\* took a commit's place in the queue. With this holding on the unmarked wire, every
\* stale end there IS a mispairing -- the residual is exactly that, and nothing else.
StaleOnlyByMispairing == staleEnd => mispaired

\* A turn the caller has stopped for, with the caller quiet on both sides, always has
\* a way to end: a frame or a done in flight, a ceiling or the net counting, a timer
\* that can fire, or the end itself enabled. Its absorbing state is the aggregator's
\* 5 s watchdog -- which is how a turn that waits for a close that never comes would
\* look live, and what gating the end on the close must not cost.
TurnWayOut ==
    \/ toStrategy /= <<>> \/ toStt /= <<>> \/ fromEngine /= <<>>
    \/ (ceilingArmed /\ ~stratSpeaking) \/ netArmed
    \/ BackstopReady \/ ENABLED HoldExpire
    \/ CanEnd

NoStrandedTurn == (turnOpen /\ vadStopped /\ ~stratSpeaking /\ ~userSpeaking) => TurnWayOut

MaxUtt == MaxStops + MaxMissed + 1

TypeOK ==
    /\ userSpeaking \in BOOLEAN /\ utt \in 0..MaxUtt
    /\ stratSpeaking \in BOOLEAN /\ ceilingArmed \in BOOLEAN
    /\ stratStops \in 0..MaxStops /\ stratStarts \in 0..MaxUtt /\ stopUtt \in 0..MaxUtt
    /\ turnOpen \in BOOLEAN /\ text \in BOOLEAN /\ finalized \in BOOLEAN
    /\ turnComplete \in BOOLEAN /\ vadStopped \in BOOLEAN
    /\ netArmed \in BOOLEAN /\ netFired \in BOOLEAN
    /\ owed \in 0..MaxStops /\ earlier \in BOOLEAN
    /\ spoke \in BOOLEAN /\ segAudio \subseteq Utts /\ segSpeech \in BOOLEAN
    /\ pending \in BOOLEAN /\ hold \in BOOLEAN /\ backstop \in BOOLEAN
    /\ closedUnasked \in BOOLEAN
    /\ \A i \in 1..Len(commits): commits[i] \in [n: 0..MaxStops, c: 0..MaxUtt]
    /\ lastCommitN \in 0..MaxStops
    /\ stops \in 0..MaxStops /\ lost \in 0..MaxLost /\ missed \in 0..MaxMissed
    /\ closes \in 0..MaxCloses /\ backstops \in 0..MaxBackstops /\ lostHere \in BOOLEAN
    /\ closedUnjudged \in BOOLEAN /\ answered \in BOOLEAN
    /\ split \in BOOLEAN /\ doubleAnswer \in BOOLEAN /\ staleClose \in BOOLEAN
    /\ staleEnd \in BOOLEAN /\ mispaired \in BOOLEAN
    /\ \A i \in 1..Len(toStrategy):
         toStrategy[i] \in [kind: {"start", "stop", "delta", "final", "done"},
                            n: 0..(MaxStops + 1), c: 0..MaxUtt, words: SUBSET Utts]
    /\ \A i \in 1..Len(toStt):
         toStt[i] \in [v: {"complete", "incomplete", "ceiling", "stopped"}, n: 1..MaxStops]
    /\ \A i \in 1..Len(fromEngine):
         fromEngine[i] \in [asked: BOOLEAN, words: SUBSET Utts]
    /\ MODE \in {"vadStop", "verdict"} /\ TURNSTOP \in {"silent", "verdict"}
    /\ DESIGN \in {"clock", "stop"} /\ DONES \in {"unmarked", "marked"}
    /\ (MODE = "vadStop" => ~pending /\ ~hold)
    /\ (DONES = "marked" => ~mispaired)
=============================================================================
