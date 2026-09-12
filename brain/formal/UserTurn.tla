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
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxTurns,        \* bound on turns opened, to keep the state space finite
          MODE

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
    \* --- monitor ---
    missedBargeIn    \* the user spoke over the bot and got no interruption, at a
                     \* moment when they had already had a quiet one since the
                     \* inference: the wedge, not the ordinary in-turn delay

vars == <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
          stopInFlight, owed, turns, missedBargeIn>>

Init ==
    /\ userTurn = FALSE
    /\ userSpeaking = FALSE
    /\ watchdog = FALSE
    /\ botSpeaking = FALSE
    /\ inference = FALSE
    /\ stopInFlight = FALSE
    /\ owed = FALSE
    /\ turns = 0
    /\ missedBargeIn = FALSE

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
    /\ ~userSpeaking /\ ~stopInFlight
    /\ userSpeaking' = TRUE
    /\ watchdog' = FALSE                       \* the inactivity timer is reset
    /\ UNCHANGED <<userTurn, botSpeaking, inference, stopInFlight, owed, turns, missedBargeIn>>

\* The user falls quiet. In retryOnQuiet the owed finalization is applied HERE,
\* inside the same process_frame() call that cleared _user_speaking -- so it is
\* atomic with respect to every other coroutine, exactly as the wrapper is.
VadStop ==
    /\ userSpeaking
    /\ userSpeaking' = FALSE
    /\ watchdog' = FALSE
    /\ IF MODE = "retryOnQuiet" /\ owed /\ userTurn
         THEN /\ userTurn' = FALSE
              /\ owed' = FALSE
              /\ inference' = FALSE
         ELSE UNCHANGED <<userTurn, owed, inference>>
    /\ UNCHANGED <<botSpeaking, stopInFlight, turns, missedBargeIn>>

(***************************************************************************)
(* The turn.                                                               *)
(***************************************************************************)

\* MinWordsUserTurnStartStrategy fired: >= INTERRUPT_MIN_WORDS of transcript.
\* This is the ONLY producer of an interruption in the brain.
Interject ==
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
              /\ UNCHANGED <<userTurn, botSpeaking, inference, turns>>
         ELSE \* A turn starts, and with it the interruption: the bot is cut.
              /\ userTurn' = TRUE
              /\ botSpeaking' = FALSE
              /\ inference' = FALSE
              /\ turns' = turns + 1
              /\ UNCHANGED missedBargeIn
    /\ watchdog' = FALSE
    /\ UNCHANGED <<userSpeaking, stopInFlight, owed>>

\* trigger_user_turn_stopped(), first await: the stop strategy has decided, the
\* aggregation is pushed and the LLM is asked. The controller gates this on the turn
\* being open and NOTHING else -- the `_user_speaking` guard is on the second await
\* only. ~userSpeaking here is not that guard but a fact about the strategy: both of
\* its conclusion points run with VAD quiet (_handle_vad_user_stopped_speaking, and
\* the analyzer's silence backstop in _handle_input_audio, which only accumulates
\* silence while _vad_user_speaking is False). Without it TLC finds a two-step
\* "the user simply never stopped" trace that the implementation cannot produce, and
\* the counterexample stops being a bug report. See README, Known limits.
Inference ==
    /\ userTurn /\ ~stopInFlight /\ ~inference /\ ~userSpeaking
    /\ inference' = TRUE
    /\ stopInFlight' = TRUE
    /\ watchdog' = FALSE                       \* _trigger_user_turn_inference_triggered
    /\ UNCHANGED <<userTurn, userSpeaking, botSpeaking, owed, turns, missedBargeIn>>

\* The gap between the two awaits: push_aggregation()'s trip across the pipeline,
\* during which the aggregator's input task can run a queued VAD start. This is
\* the entire entry into the failure.
Resume ==
    /\ stopInFlight
    /\ ~userSpeaking
    /\ userSpeaking' = TRUE
    /\ watchdog' = FALSE
    /\ UNCHANGED <<userTurn, botSpeaking, inference, stopInFlight, owed, turns, missedBargeIn>>

\* trigger_user_turn_stopped(), second await.
Finalize ==
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
    /\ UNCHANGED <<userSpeaking, watchdog, botSpeaking, turns, missedBargeIn>>

\* The reply the inference produced reaches the transport. It cannot overtake the
\* finalize in practice -- an LLM round trip plus synthesis against two awaits --
\* so ~stopInFlight is an ordering FACT, stated rather than modelled. See the
\* limits note in README.md.
BotStart ==
    /\ inference /\ ~stopInFlight /\ ~botSpeaking
    /\ botSpeaking' = TRUE
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, inference, stopInFlight,
                   owed, turns, missedBargeIn>>

BotStop ==
    /\ botSpeaking
    /\ botSpeaking' = FALSE
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, inference, stopInFlight,
                   owed, turns, missedBargeIn>>

(***************************************************************************)
(* The 5s force-stop. An INACTIVITY timer: every user action above clears   *)
(* `watchdog`, so reaching it needs a sustained quiet stretch -- which is   *)
(* exactly what a user still trying to be heard does not produce.          *)
(***************************************************************************)

Arm ==
    /\ ~watchdog /\ ~userSpeaking
    /\ watchdog' = TRUE
    /\ UNCHANGED <<userTurn, userSpeaking, botSpeaking, inference, stopInFlight,
                   owed, turns, missedBargeIn>>

ForceStop ==
    /\ watchdog /\ userTurn /\ ~userSpeaking
    /\ userTurn' = FALSE
    /\ inference' = FALSE
    /\ owed' = FALSE
    /\ watchdog' = FALSE
    /\ UNCHANGED <<userSpeaking, botSpeaking, stopInFlight, turns, missedBargeIn>>

Next ==
    \/ VadStart \/ VadStop \/ Interject
    \/ Inference \/ Resume \/ Finalize
    \/ BotStart \/ BotStop
    \/ Arm \/ ForceStop

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

TypeOK ==
    /\ userTurn \in BOOLEAN /\ userSpeaking \in BOOLEAN /\ watchdog \in BOOLEAN
    /\ botSpeaking \in BOOLEAN /\ inference \in BOOLEAN /\ stopInFlight \in BOOLEAN
    /\ owed \in BOOLEAN /\ missedBargeIn \in BOOLEAN
    /\ turns \in 0..MaxTurns
=============================================================================
