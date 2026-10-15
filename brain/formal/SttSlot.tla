--------------------------------- MODULE SttSlot ---------------------------------
(***************************************************************************)
(* teaport — arbitration of the engine's SINGLE speech-to-text slot.       *)
(*                                                                          *)
(*   brain/teaport_brain/session_arbiter.py -- acquire, _ending, _holding,  *)
(*                                             release, _resume_held        *)
(*   brain/teaport_brain/gateway_server.py  -- a /talk session's claim; its *)
(*                                             release after the pipeline   *)
(*   brain/teaport_brain/sip_server.py      -- CallClaim; the call's        *)
(*                                             teardown, settle, release    *)
(*   brain/teaport_brain/stt.py             -- _connect_websocket, hold()   *)
(*                                                                          *)
(* The engine serves one STT session. Who may hold it is the brain's        *)
(* decision, and how that decision was made is ARBITER:                     *)
(*                                                                          *)
(*   "split" -- before #58 step 2 (8ef333f): two front-ends, two arbiters,  *)
(*              no shared lock. The OpenClaw path tracked `_active_session`,*)
(*              the SIP path `active["call"]`; each evicted its OWN         *)
(*              predecessor, slept 0.3 s, and connected. Kept so the        *)
(*              finding below stays reproducible.                           *)
(*   "one"   -- as shipped: one SessionArbiter for every front-end of the   *)
(*              process (talk, room, call; SIP moved in-process in #58 step *)
(*              3). A newcomer asks under one lock; a holder that has to go *)
(*              is ended (end(), then wait for its release(), bounded by    *)
(*              END_WAIT_SECS) or, for a call, put on hold (hold(): its STT *)
(*              disconnects); then SETTLE_SECS, then the grant. A session   *)
(*              that ends on its own release()s after its pipeline has      *)
(*              stopped -- a /talk one at once, a call after its own 0.3 s  *)
(*              sleep -- and a conversation held for that call gets the     *)
(*              slot back at once and reconnects (resume()).                *)
(*                                                                          *)
(* In both, nothing observes that the ENGINE has freed the slot: every wait *)
(* is a sleep or a release, and a release says the brain's socket closed,   *)
(* not that the engine has processed the close. `closing` is that window.   *)
(*                                                                          *)
(* MODE = "fixedSettle"   -- connect once, and a refusal is final (stt.py   *)
(*                           set _stt_available = False on the single       *)
(*                           attempt; greet() polls only until the          *)
(*                           tri-state RESOLVES, so a 503 resolved to False *)
(*                           in milliseconds).                              *)
(* MODE = "retryWhileBusy" -- as shipped since: retry a rejection           *)
(*                           (_CONNECT_ATTEMPTS x _CONNECT_RETRY_S).        *)
(*                                                                          *)
(* ENDWAIT says what the arbiter's wait for an ended holder's release is:   *)
(* "untimed" states the timing fact that a pipeline tears down within       *)
(* END_WAIT_SECS; "timeout" lets the wait give up, as _ending does (5 s),   *)
(* with the holder still tearing down.                                      *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS GwSessions,    \* /talk sessions: the OpenClaw app, a Talk client, the room mic
          SipSessions,   \* phone calls
          MODE,          \* "fixedSettle" | "retryWhileBusy"
          ARBITER,       \* "split" | "one"
          ENDWAIT        \* "untimed" | "timeout" (ARBITER = "one")

Sessions == GwSessions \cup SipSessions

NoOne == "none"

\* Which arbiter a session asks. "split": its front-end's; "one": the one.
Arb(s) == IF ARBITER = "one" THEN "one" ELSE IF s \in GwSessions THEN "gw" ELSE "sip"
Arbs == IF ARBITER = "one" THEN {"one"} ELSE {"gw", "sip"}

VARIABLES
    pc,          \* [Sessions -> state]
    engine,      \* the session whose socket holds the engine slot, or NoOne
    closing,     \* a close is in flight; the engine has not freed the slot yet
    arbHolder,   \* [Arbs -> the session the arbiter has the slot granted to, or NoOne]
    lock,        \* [Arbs -> the session inside acquire, or NoOne] ("one")
    waitFor,     \* [Sessions -> the holder a newcomer is waiting out, or NoOne]
    held,        \* the conversation on hold for a call, or NoOne ("one")
    heldFor,     \* the call it is held for, or NoOne
    falseBusy,   \* someone was told "busy" while NO session held the slot
    grantedBusy  \* a session the arbiter granted was refused: another still held it

vars == <<pc, engine, closing, arbHolder, lock, waitFor, held, heldFor, falseBusy,
          grantedBusy>>

States == {"idle", "acq", "settle", "connect", "live", "ending", "holding", "held",
           "leaving", "relSettle", "busy", "done"}

Init ==
    /\ pc = [s \in Sessions |-> "idle"]
    /\ engine = NoOne
    /\ closing = FALSE
    /\ arbHolder = [a \in Arbs |-> NoOne]
    /\ lock = [a \in Arbs |-> NoOne]
    /\ waitFor = [s \in Sessions |-> NoOne]
    /\ held = NoOne /\ heldFor = NoOne
    /\ falseBusy = FALSE /\ grantedBusy = FALSE

\* The brain closes s's engine socket (if it has one). The slot is NOT free yet --
\* the engine still has to process the close. A session that never held the slot
\* (ended while it settled) has nothing for the engine to free, and must not clear a
\* close already in flight for whoever did.
CloseSocket(s) ==
    /\ engine' = IF engine = s THEN NoOne ELSE engine
    /\ closing' = (closing \/ engine = s)

(***************************************************************************)
(* A newcomer asks for the slot.                                           *)
(***************************************************************************)

\* "split": acquire_slot() / cancel_active_call(): evict the predecessor of the SAME
\* front-end -- neither arbiter can see the other's session -- and settle.
\* "one": SessionArbiter.acquire, under its lock. Nobody holds it: granted, and the
\* STT connects as the pipeline starts. Somebody does: it is ended (end()) or, for a
\* call against a conversation, put on hold (hold() -- the PROMPT answered yes), and
\* the newcomer waits it out. Which (and a refusal, which touches nothing) is policy,
\* not this model's: both are possible here -- except ending a CALL: under the SIP
\* front-end's contract a call's claim is released before the next call asks, and
\* nothing else ends a call. What happens when that contract is broken, and every
\* other question of who may take the slot from whom, is SessionArbiter.tla's; this
\* module asks only whether the engine has let go when the arbiter thinks it has.
Arrive(s) ==
    /\ pc[s] = "idle"
    /\ LET a == Arb(s)
           h == arbHolder[a]
       IN IF ARBITER = "split"
            THEN /\ pc' = IF h = NoOne THEN [pc EXCEPT ![s] = "settle"]
                          ELSE [pc EXCEPT ![h] = "ending", ![s] = "settle"]
                 /\ arbHolder' = [arbHolder EXCEPT ![a] = s]
                 /\ UNCHANGED <<lock, waitFor, held, heldFor>>
            ELSE /\ lock[a] = NoOne
                 /\ IF h = NoOne
                      THEN /\ pc' = [pc EXCEPT ![s] = "connect"]
                           /\ arbHolder' = [arbHolder EXCEPT ![a] = s]
                           /\ UNCHANGED <<lock, waitFor>>
                      ELSE /\ lock' = [lock EXCEPT ![a] = s]
                           /\ waitFor' = [waitFor EXCEPT ![s] = h]
                           /\ UNCHANGED arbHolder
                           /\ \/ /\ h \notin SipSessions
                                 /\ pc' = [pc EXCEPT ![h] = "ending", ![s] = "acq"]
                              \/ /\ s \in SipSessions /\ h \in GwSessions
                                 /\ pc[h] = "live" /\ held = NoOne
                                 /\ pc' = [pc EXCEPT ![h] = "holding", ![s] = "acq"]
                 /\ UNCHANGED <<held, heldFor>>
    /\ UNCHANGED <<engine, closing, falseBusy, grantedBusy>>


\* A conversation going on hold for a call (AgentSession.hold): its last line, then
\* stt.hold() closes the socket; the arbiter keeps it beside the slot.
Holding(h) ==
    /\ pc[h] = "holding"
    /\ \E s \in SipSessions: waitFor[s] = h /\ pc[s] = "acq"
         /\ heldFor' = s
    /\ pc' = [pc EXCEPT ![h] = "held"]
    /\ held' = h
    /\ CloseSocket(h)
    /\ UNCHANGED <<arbHolder, lock, waitFor, falseBusy, grantedBusy>>

\* _ending / _holding go on: the holder has let go (released, or held) -- or, with
\* ENDWAIT = "timeout", END_WAIT_SECS have passed and it has not, and the arbiter
\* proceeds anyway. The slot is nobody's, then SETTLE_SECS.
AcqContinue(s) ==
    /\ pc[s] = "acq"
    /\ LET v == waitFor[s]
       IN \/ pc[v] \in {"done", "held"}
          \/ ENDWAIT = "timeout" /\ pc[v] = "ending"
    /\ pc' = [pc EXCEPT ![s] = "settle"]
    /\ arbHolder' = IF arbHolder[Arb(s)] = waitFor[s]
                     THEN [arbHolder EXCEPT ![Arb(s)] = NoOne] ELSE arbHolder
    /\ waitFor' = [waitFor EXCEPT ![s] = NoOne]
    /\ UNCHANGED <<engine, closing, lock, held, heldFor, falseBusy, grantedBusy>>

\* await asyncio.sleep(SETTLE_SECS) -- it does NOT observe `closing`. That is the
\* point. "one": then the grant, and the lock is let go.
Settle(s) ==
    /\ pc[s] = "settle"
    /\ pc' = [pc EXCEPT ![s] = "connect"]
    /\ IF ARBITER = "one"
         THEN /\ arbHolder' = [arbHolder EXCEPT ![Arb(s)] = s]
              /\ lock' = [lock EXCEPT ![Arb(s)] = NoOne]
         ELSE UNCHANGED <<arbHolder, lock>>
    /\ UNCHANGED <<engine, closing, waitFor, held, heldFor, falseBusy, grantedBusy>>

\* The engine processes the close and frees the slot. Unbounded but finite:
\* how long this takes is exactly what every sleep here is guessing at.
EngineFreesSlot ==
    /\ closing
    /\ closing' = FALSE
    /\ UNCHANGED <<pc, engine, arbHolder, lock, waitFor, held, heldFor, falseBusy,
                   grantedBusy>>

\* stt.py _connect_websocket(): succeeds iff the engine has a free session.
ConnectOk(s) ==
    /\ pc[s] = "connect"
    /\ engine = NoOne /\ ~closing
    /\ pc' = [pc EXCEPT ![s] = "live"]
    /\ engine' = s
    /\ UNCHANGED <<closing, arbHolder, lock, waitFor, held, heldFor, falseBusy, grantedBusy>>

\* Refused (503). fixedSettle: final -- one attempt, _stt_available = False, greet()
\* speaks the busy line and ends the session. retryWhileBusy: retried while the
\* refusal is only a close in flight (TIMING FACT: the engine frees a closed slot
\* within the retry budget); busy only when a session genuinely holds the slot.
\* Either way, under "one" every session that connects was granted the slot: busy
\* then means the brain's arbiter and the engine disagree about who has it.
ConnectRefused(s) ==
    /\ pc[s] = "connect"
    /\ ~(engine = NoOne /\ ~closing)
    /\ IF MODE = "retryWhileBusy" /\ engine = NoOne
         THEN UNCHANGED <<pc, falseBusy, grantedBusy>>        \* stay, and retry
         ELSE /\ pc' = [pc EXCEPT ![s] = "busy"]
              \* Told "busy" although NO session held the slot: the only thing in
              \* the way was a close the engine had not finished processing.
              /\ falseBusy' = (falseBusy \/ engine = NoOne)
              /\ grantedBusy' = (grantedBusy \/ (ARBITER = "one" /\ engine /= NoOne))
    /\ UNCHANGED <<engine, closing, arbHolder, lock, waitFor, held, heldFor>>

\* The caller hangs up / the client disconnects; a held conversation's client may
\* leave too (it is forgotten).
Leave(s) ==
    /\ pc[s] \in {"live", "held"}
    /\ pc' = [pc EXCEPT ![s] = "leaving"]
    /\ UNCHANGED <<engine, closing, arbHolder, lock, waitFor, held, heldFor, falseBusy,
                   grantedBusy>>

\* SessionArbiter.release: the newer holder is left alone by a late release; a held
\* one is forgotten; the end of the call a conversation was held for brings it back at
\* once -- the holder again, and its STT reconnects (resume()), with no settle.
Release(s) ==
    IF ARBITER = "split"
      THEN /\ arbHolder' = IF arbHolder[Arb(s)] = s
                             THEN [arbHolder EXCEPT ![Arb(s)] = NoOne] ELSE arbHolder
           /\ UNCHANGED <<held, heldFor>>
           /\ pc' = [pc EXCEPT ![s] = "done"]
      ELSE IF held = s
      THEN /\ held' = NoOne /\ heldFor' = NoOne
           /\ UNCHANGED arbHolder
           /\ pc' = [pc EXCEPT ![s] = "done"]
      ELSE IF arbHolder["one"] = s /\ held /= NoOne /\ heldFor = s
      THEN /\ arbHolder' = [arbHolder EXCEPT !["one"] = held]
           /\ held' = NoOne /\ heldFor' = NoOne
           /\ pc' = [pc EXCEPT ![s] = "done", ![held] = "connect"]
      ELSE /\ arbHolder' = IF arbHolder["one"] = s
                             THEN [arbHolder EXCEPT !["one"] = NoOne] ELSE arbHolder
           /\ UNCHANGED <<held, heldFor>>
           /\ pc' = [pc EXCEPT ![s] = "done"]

\* A holder told to end tears its pipeline down: the STT's _disconnect closes the
\* socket, and its front-end releases the claim (gateway_server's finally; a call's
\* end() releases at once) -- with all release() does, a held conversation's resume
\* included. "split" had no release to speak of: the newcomer was already the holder.
Ending(h) ==
    /\ pc[h] = "ending"
    /\ CloseSocket(h)
    /\ IF ARBITER = "one"
         THEN Release(h)
         ELSE /\ pc' = [pc EXCEPT ![h] = "done"]
              /\ UNCHANGED <<arbHolder, held, heldFor>>
    /\ UNCHANGED <<lock, waitFor, falseBusy, grantedBusy>>

\* A session ending on its own tears down: its socket closes. A /talk session then
\* releases (gateway_server's finally, after the pipeline); a call first sleeps 0.3 s
\* (sip_server's teardown) -- under "one"; "split" released at once.
Leaving(s) ==
    /\ pc[s] = "leaving"
    /\ CloseSocket(s)
    /\ IF ARBITER = "one" /\ s \in SipSessions
         THEN /\ pc' = [pc EXCEPT ![s] = "relSettle"]
              /\ UNCHANGED <<arbHolder, held, heldFor>>
         ELSE Release(s)
    /\ UNCHANGED <<lock, waitFor, falseBusy, grantedBusy>>

RelSettle(s) ==
    /\ pc[s] = "relSettle"
    /\ Release(s)
    /\ UNCHANGED <<engine, closing, lock, waitFor, falseBusy, grantedBusy>>

Next ==
    \/ \E s \in Sessions :
         \/ Arrive(s) \/ Ending(s) \/ Holding(s) \/ AcqContinue(s) \/ Settle(s)
         \/ ConnectOk(s) \/ ConnectRefused(s) \/ Leave(s) \/ Leaving(s) \/ RelSettle(s)
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

\* The same line, when the arbiter HAD granted the slot: the session it gave way to
\* (told it was ending, or put on hold) still held the engine. Both lose -- the one
\* the arbiter ended, and the one it let in.
NoGrantedBusy == ~grantedBusy

\* A session that is live really does hold the slot.
HolderAgrees == \A s \in Sessions : (pc[s] = "live") => (engine = s)

TypeOK ==
    /\ pc \in [Sessions -> States]
    /\ engine \in Sessions \cup {NoOne}
    /\ closing \in BOOLEAN
    /\ arbHolder \in [Arbs -> Sessions \cup {NoOne}]
    /\ lock \in [Arbs -> Sessions \cup {NoOne}]
    /\ waitFor \in [Sessions -> Sessions \cup {NoOne}]
    /\ held \in GwSessions \cup {NoOne} /\ heldFor \in SipSessions \cup {NoOne}
    /\ MODE \in {"fixedSettle", "retryWhileBusy"}
    /\ ARBITER \in {"split", "one"} /\ ENDWAIT \in {"untimed", "timeout"}
=============================================================================
