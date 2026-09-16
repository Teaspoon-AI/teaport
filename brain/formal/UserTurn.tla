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
(* The speculative reply (speculate.py, TEAPORT_SPECULATIVE_REPLY). When a  *)
(* final lands and the stop strategy does not conclude on it -- the verdict  *)
(* was INCOMPLETE and the ceiling is running -- the LLM is asked NOW, out of *)
(* band, against a snapshot of the context with that text as the user       *)
(* message. At the commit the LLM service is offered that stream instead of *)
(* opening its own. The controller's state is never touched: `SpecStart`    *)
(* reads userTurn and writes nothing the guards read, which is why the two  *)
(* barge-in properties are re-checked with it on rather than assumed.       *)
(*                                                                          *)
(* What the speculation must not do is speak a reply generated against a    *)
(* context the commit no longer has. Between the snapshot and the commit    *)
(* the context can have other writers -- HeardContextCorrector's truncation *)
(* of the cut reply on the LLMContextFrame itself was one, and the first    *)
(* miss seen live, until speculate.py moved it ahead of the snapshot; the   *)
(* consult follow-up's trigger and MemoryRecall's note are NOT ones (the    *)
(* follow-up posts only with the turn free, the note is injected before the *)
(* aggregator sees the final). `CtxChange` is any writer at all: what the   *)
(* code prevents today is not what the machine prevents.                    *)
(*                                                                          *)
(* SPEC = "off"       -- no speculation; the four MODE rows are unchanged.  *)
(* SPEC = "byText"    -- promote when the committed TEXT is what was asked. *)
(*                       Rejected: NoStaleReply falls.                      *)
(* SPEC = "byContext" -- promote only when the WHOLE context equals the     *)
(*                       snapshot (what speculate.py does).                 *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxTurns,        \* bound on turns opened, to keep the state space finite
          MODE,
          SPEC,
          MaxCtxWrites     \* bound on context writes under a live speculation

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
    gen,             \* the context generation: bumped by every other writer
    \* --- monitors ---
    missedBargeIn,   \* the user spoke over the bot and got no interruption, at a
                     \* moment when they had already had a quiet one since the
                     \* inference: the wedge, not the ordinary in-turn delay
    staleReply       \* a reply asked against a context the commit no longer has
                     \* was handed to the pipeline

vars == <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
          stopInFlight, owed, turns, spec, specGen, gen, missedBargeIn, staleReply>>

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
    /\ gen = 0
    /\ missedBargeIn = FALSE
    /\ staleReply = FALSE

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
    /\ spec' = FALSE                           \* the caller resumed: the text will differ
    /\ UNCHANGED <<userTurn, botSpeaking, inference, stopInFlight, owed, turns,
                   specGen, gen, missedBargeIn, staleReply>>

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
    /\ UNCHANGED <<botSpeaking, stopInFlight, turns, spec, specGen, gen,
                   missedBargeIn, staleReply>>

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
              /\ UNCHANGED <<userTurn, botSpeaking, inference, turns, spec>>
         ELSE \* A turn starts, and with it the interruption: the bot is cut.
              /\ userTurn' = TRUE
              /\ botSpeaking' = FALSE
              /\ inference' = FALSE
              /\ turns' = turns + 1
              /\ spec' = FALSE               \* a new turn: whatever was asked is not this
              /\ UNCHANGED missedBargeIn
    /\ watchdog' = FALSE
    /\ UNCHANGED <<userSpeaking, stopInFlight, owed, specGen, gen, staleReply>>

(***************************************************************************)
(* The speculative reply.                                                  *)
(***************************************************************************)

\* A final landed and the stop strategy did not conclude on it: the LLM is asked now,
\* against the context as it is. Enabled exactly where the strategy hook fires --
\* an open turn, VAD quiet, no inference yet -- and touches nothing the controller's
\* guards read. Single-flight: a second final supersedes, which is this same step
\* from a state where `spec` was already TRUE (the old one is closed).
SpecStart ==
    /\ SPEC /= "off"
    /\ userTurn /\ ~userSpeaking /\ ~inference /\ ~stopInFlight
    /\ spec' = TRUE
    /\ specGen' = gen
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, gen, missedBargeIn, staleReply>>

\* Something else writes the context while a speculation is live. A free action,
\* deliberately: HeardContextCorrector._reconcile on the very LLMContextFrame that
\* carries the commit was such a writer until it was moved ahead of the snapshot, and
\* nothing in the pipeline stops the next one. Bounded only to keep the state finite.
CtxChange ==
    /\ spec /\ gen < MaxCtxWrites
    /\ gen' = gen + 1
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, botSpeaking, inference,
                   stopInFlight, owed, turns, spec, specGen, missedBargeIn, staleReply>>

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
\* text is what was asked -- which every live speculation's is, since a resume or a
\* new turn already closed it -- so a context written in between is adopted with it.
\* byContext adopts only when nothing else wrote the context (specGen = gen); the
\* speculation is otherwise closed and the ordinary request made, the same state
\* minus the monitor. Either way the speculation is over.
Inference ==
    /\ userTurn /\ ~stopInFlight /\ ~inference /\ ~userSpeaking
    /\ inference' = TRUE
    /\ stopInFlight' = TRUE
    /\ watchdog' = FALSE                       \* _trigger_user_turn_inference_triggered
    /\ spec' = FALSE
    /\ staleReply' = (staleReply \/ (spec /\ SPEC = "byText" /\ specGen /= gen))
    /\ UNCHANGED <<userTurn, userSpeaking, botSpeaking, owed, turns, specGen, gen,
                   missedBargeIn>>

\* The gap between the two awaits: push_aggregation()'s trip across the pipeline,
\* during which the aggregator's input task can run a queued VAD start. This is
\* the entire entry into the failure.
Resume ==
    /\ stopInFlight
    /\ ~userSpeaking
    /\ userSpeaking' = TRUE
    /\ watchdog' = FALSE
    /\ UNCHANGED <<userTurn, botSpeaking, inference, stopInFlight, owed, turns,
                   spec, specGen, gen, missedBargeIn, staleReply>>

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
    /\ UNCHANGED <<userSpeaking, watchdog, botSpeaking, turns, spec, specGen, gen,
                   missedBargeIn, staleReply>>

\* The reply the inference produced reaches the transport. It cannot overtake the
\* finalize in practice -- an LLM round trip plus synthesis against two awaits --
\* so ~stopInFlight is an ordering FACT, stated rather than modelled. See the
\* limits note in README.md.
BotStart ==
    /\ inference /\ ~stopInFlight /\ ~botSpeaking
    /\ botSpeaking' = TRUE
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, inference, stopInFlight,
                   owed, turns, spec, specGen, gen, missedBargeIn, staleReply>>

BotStop ==
    /\ botSpeaking
    /\ botSpeaking' = FALSE
    /\ UNCHANGED <<userTurn, userSpeaking, watchdog, inference, stopInFlight,
                   owed, turns, spec, specGen, gen, missedBargeIn, staleReply>>

(***************************************************************************)
(* The 5s force-stop. An INACTIVITY timer: every user action above clears   *)
(* `watchdog`, so reaching it needs a sustained quiet stretch -- which is   *)
(* exactly what a user still trying to be heard does not produce.          *)
(***************************************************************************)

Arm ==
    /\ ~watchdog /\ ~userSpeaking
    /\ watchdog' = TRUE
    /\ UNCHANGED <<userTurn, userSpeaking, botSpeaking, inference, stopInFlight,
                   owed, turns, spec, specGen, gen, missedBargeIn, staleReply>>

\* The watchdog's stop is a commit too: _trigger_user_turn_stop pushes the
\* aggregation, which reaches get_chat_completions and Speculator.take, so a live
\* speculation is decided here exactly as at Inference.
ForceStop ==
    /\ watchdog /\ userTurn /\ ~userSpeaking
    /\ userTurn' = FALSE
    /\ inference' = FALSE
    /\ owed' = FALSE
    /\ watchdog' = FALSE
    /\ spec' = FALSE
    /\ staleReply' = (staleReply \/ (spec /\ SPEC = "byText" /\ specGen /= gen))
    /\ UNCHANGED <<userSpeaking, botSpeaking, stopInFlight, turns, specGen, gen,
                   missedBargeIn>>

Next ==
    \/ VadStart \/ VadStop \/ Interject
    \/ Inference \/ Resume \/ Finalize
    \/ BotStart \/ BotStop
    \/ Arm \/ ForceStop
    \/ SpecStart \/ CtxChange

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
\* landed after the snapshot is context the model never saw.
NoStaleReply == ~staleReply

TypeOK ==
    /\ userTurn \in BOOLEAN /\ userSpeaking \in BOOLEAN /\ watchdog \in BOOLEAN
    /\ botSpeaking \in BOOLEAN /\ inference \in BOOLEAN /\ stopInFlight \in BOOLEAN
    /\ owed \in BOOLEAN /\ missedBargeIn \in BOOLEAN
    /\ turns \in 0..MaxTurns
    /\ spec \in BOOLEAN /\ staleReply \in BOOLEAN
    /\ specGen \in 0..MaxCtxWrites /\ gen \in 0..MaxCtxWrites
    /\ SPEC \in {"off", "byText", "byContext"}
    /\ (SPEC = "off" => ~spec)
=============================================================================
