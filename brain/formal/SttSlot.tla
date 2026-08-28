--------------------------------- MODULE SttSlot ---------------------------------
(***************************************************************************)
(* teaport — arbitration of the engine's SINGLE speech-to-text slot.       *)
(*                                                                          *)
(*   brain/teaport_brain/agent_session.py  -- acquire_slot(), greet()       *)
(*   brain/teaport_brain/sip_server.py     -- cancel_active_call()          *)
(*   brain/teaport_brain/stt.py            -- _connect_websocket()          *)
(*                                                                          *)
(* The engine serves one STT session. Two front-ends contend for it with    *)
(* SEPARATE arbiters and no shared lock: the OpenClaw path tracks           *)
(* `_active_session`, the SIP path tracks `active["call"]`. Each evicts its *)
(* OWN predecessor and neither can see the other's.                        *)
(*                                                                          *)
(* Both then rely on `await asyncio.sleep(0.3)` to let the engine process   *)
(* the close before the replacement connects. That sleep is a TIMING        *)
(* ASSUMPTION standing in for synchronization: nothing observes that the    *)
(* engine has actually freed the slot. `closing` below is that window.      *)
(*                                                                          *)
(* MODE = "fixedSettle"   -- as written: settle, connect once, and a        *)
(*                           refusal is final (stt.py sets _stt_available   *)
(*                           = False on the single attempt; greet() polls   *)
(*                           only until the tri-state RESOLVES, so a 503    *)
(*                           resolves to False in milliseconds).            *)
(* MODE = "retryWhileBusy" -- retry the connect instead of trusting a       *)
(*                           fixed sleep.                                   *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS GwSessions,    \* OpenClaw /talk sessions   (arbiter: _active_session)
          SipSessions,   \* SIP calls                 (arbiter: active["call"])
          MODE

Sessions == GwSessions \cup SipSessions

\* The two arbiters are separate variables in separate modules, so eviction only
\* ever reaches a session of the SAME front-end. That is the modelled gap.
SameFront(s, t) == (s \in GwSessions) = (t \in GwSessions)

VARIABLES
    pc,                  \* [Sessions -> state]
    holder,              \* the session holding the engine slot, or "none"
    closing,             \* a close is in flight; the engine has not freed the slot yet
    falseBusy            \* someone was told "busy" while NO session held the slot

vars == <<pc, holder, closing, falseBusy>>

NoOne == "none"

Init ==
    /\ pc = [s \in Sessions |-> "idle"]
    /\ holder = NoOne
    /\ closing = FALSE
    /\ falseBusy = FALSE

(***************************************************************************)
(* A new session arrives and runs its front-end's arbiter.                 *)
(***************************************************************************)

\* acquire_slot() / cancel_active_call(): evict the predecessor of the SAME
\* front-end. Note what is NOT here -- neither arbiter can see the other's
\* session, because they are different variables in different modules.
Arbitrate(s) ==
    /\ pc[s] = "idle"
    \* "settle" counts: acquire_slot registers `_active_session = (task, my_done)`
    \* INSIDE the lock, before the STT connects, so a same-front-end successor does
    \* see and evict a predecessor that has not connected yet.
    /\ LET victim == {t \in Sessions : t # s /\ SameFront(s, t)
                                       /\ pc[t] \in {"settle", "connect", "live"}}
       IN IF victim # {}
            THEN \E v \in victim :
                    pc' = [pc EXCEPT ![v] = "teardown", ![s] = "settle"]
            ELSE pc' = [pc EXCEPT ![s] = "settle"]
    /\ UNCHANGED <<holder, closing, falseBusy>>

\* The evicted pipeline finishes tearing down: STT _disconnect closes the socket.
\* The slot is NOT free yet -- the engine still has to process the close.
Teardown(s) ==
    /\ pc[s] = "teardown"
    /\ pc' = [pc EXCEPT ![s] = "done"]
    /\ holder' = IF holder = s THEN NoOne ELSE holder
    /\ closing' = (holder = s)
    /\ UNCHANGED falseBusy

\* The engine processes the close and frees the slot. Unbounded but finite:
\* how long this takes is exactly what the 0.3s sleep is guessing at.
EngineFreesSlot ==
    /\ closing
    /\ closing' = FALSE
    /\ UNCHANGED <<pc, holder, falseBusy>>

\* await asyncio.sleep(0.3) -- it does NOT observe `closing`. That is the point.
Settle(s) ==
    /\ pc[s] = "settle"
    /\ pc' = [pc EXCEPT ![s] = "connect"]
    /\ UNCHANGED <<holder, closing, falseBusy>>

\* stt.py _connect_websocket(): succeeds iff the engine has a free session.
ConnectOk(s) ==
    /\ pc[s] = "connect"
    /\ holder = NoOne /\ ~closing
    /\ pc' = [pc EXCEPT ![s] = "live"]
    /\ holder' = s
    /\ UNCHANGED <<closing, falseBusy>>

\* Refused (503). As written this is FINAL: one attempt, _stt_available = False,
\* greet() speaks the busy line and ends the session.
ConnectRefused(s) ==
    /\ pc[s] = "connect"
    /\ ~(holder = NoOne /\ ~closing)
    /\ CASE MODE = "fixedSettle" ->
              /\ pc' = [pc EXCEPT ![s] = "busy"]
              \* Told "busy" although NO session held the slot: the only thing in
              \* the way was a close the engine had not finished processing.
              /\ falseBusy' = (falseBusy \/ holder = NoOne)
         [] MODE = "retryWhileBusy" ->
              \* Retry while the refusal is only a close in flight; report busy
              \* only when another session genuinely holds the slot.
              /\ IF holder = NoOne
                   THEN /\ UNCHANGED pc          \* stay in "connect" and retry
                        /\ UNCHANGED falseBusy
                   ELSE /\ pc' = [pc EXCEPT ![s] = "busy"]
                        /\ UNCHANGED falseBusy
    /\ UNCHANGED <<holder, closing>>

\* The caller hangs up / the client disconnects.
Leave(s) ==
    /\ pc[s] = "live"
    /\ pc' = [pc EXCEPT ![s] = "teardown"]
    /\ UNCHANGED <<holder, closing, falseBusy>>

Next ==
    \/ \E s \in Sessions :
         Arbitrate(s) \/ Teardown(s) \/ Settle(s)
         \/ ConnectOk(s) \/ ConnectRefused(s) \/ Leave(s)
    \/ EngineFreesSlot

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Properties.                                                             *)
(***************************************************************************)

\* The engine enforces this itself, so it is a sanity check on the model rather
\* than a claim about the brain.
MutualExclusion == Cardinality({s \in Sessions : pc[s] = "live"}) <= 1

\* The user-visible failure: "Sorry, the voice assistant is busy with another
\* session right now" -- spoken, and on SIP followed by a hangup -- when in fact
\* nothing was using it and it would have worked a moment later.
NoFalseBusy == ~falseBusy

\* A session that is live really does hold the slot.
HolderAgrees == \A s \in Sessions : (pc[s] = "live") => (holder = s)

TypeOK ==
    /\ pc \in [Sessions -> {"idle","settle","connect","live","busy","teardown","done"}]
    /\ holder \in Sessions \cup {NoOne}
=============================================================================
