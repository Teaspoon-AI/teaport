---------------------------- MODULE SessionArbiter ----------------------------
(***************************************************************************)
(* teaport -- the session arbiter: one conversation at a time on the       *)
(* engine's single STT slot (issues #58, #111).                            *)
(*                                                                          *)
(*   brain/teaport_brain/session_arbiter.py  decide, SessionArbiter.acquire/*)
(*       _acquire/_ask/_hold/_holding/_end/_ending/_resume_held/release     *)
(*   brain/teaport_brain/gateway_server.py   run_relay_bot (Talk + room     *)
(*       claims), _end_for                                                  *)
(*   brain/teaport_brain/sip_server.py       CallClaim (a call's claim)     *)
(*   brain/teaport_brain/agent_session.py    AgentSession.hold/resume       *)
(*   brain/teaport_brain/call_prompt.py      CallPrompt.ask/hold/resume     *)
(*                                                                          *)
(* Claims: a Talk session ("talk"), the room mic ("room", asleep or awake), *)
(* and phone calls (Calls, the same "sip" client). acquire() runs under one *)
(* asyncio.Lock and awaits: the lock, an ending/holding/resume in flight,   *)
(* the question put to the holder (PROMPT), the hold, the end. release() is *)
(* synchronous and runs from any session, outside the lock. The hold, the   *)
(* end and the resume run as their own (shielded) tasks.                    *)
(*                                                                          *)
(* Time is not modelled: every bounded wait (END_WAIT_SECS, HOLD_WAIT_SECS, *)
(* the prompt's timeout) is a step that may happen; the SETTLE sleeps are   *)
(* steps others interleave with. The engine refuses an STT connect while    *)
(* another session's STT is connected (stt.py's retries are not modelled:  *)
(* a refusal here is one the retry budget would have to absorb).           *)
(*                                                                          *)
(* SIP:  "sequential" -- the SIP front-end's contract: a call's claim is    *)
(*                       released before the next call asks (start_call     *)
(*                       tears the old call down first)                     *)
(*       "overlap"    -- what SipCall.tla shows sip_server.py can do: the   *)
(*                       next call asks while the previous call's claim is  *)
(*                       still held (its pipeline already stopped), so the  *)
(*                       arbiter REPLACES it                                *)
(* MODE: "shipped"    -- session_arbiter.py as written                      *)
(*       "redecide"   -- proposed: after ending a holder, a newcomer that   *)
(*                       finds the slot taken again (a conversation resumed *)
(*                       by that end) decides again instead of overwriting  *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Convs,     \* subset of {"talk", "room"}
          Calls,     \* subset of {"c1", "c2"}
          ASKABLE,   \* the conversations can be asked (answer_phone_call is on)
          AUTO,      \* a call may get answered by the gateway while it asks
          SIP, MODE

None == "none"
C1 == "c1"
C2 == "c2"
Claims == Convs \cup Calls
Kind(s) == IF s \in Calls THEN "call" ELSE s

VARIABLES
    \* --- the sessions (front-end side)
    st,        \* "off" | "acq" (in acquire) | "run" (granted, pipeline up)
               \*   | "stopping" (a call: pipeline stopped, claim not yet let go) | "out"
    stt,       \* its STT is connected to the engine
    heldF,     \* AgentSession.held: on hold (muted, STT let go)
    conn,      \* has connected its STT once (the pipeline started)
    asleep,    \* the room mic is asleep (wake gate / keep-alive idle)
    ans,       \* calls answered by the gateway (Claim.answered set)
    \* --- acquire(), per claim
    apc,       \* "none" | "lock" | "loop" | "ask" | "holdw" | "endw1" | "post" | "endw"
    tgt,       \* the holder this acquire is asking / holding / ending
    lockOwner,
    \* --- the arbiter
    holder, held, heldFor,
    rel,       \* claims released (Claim.released)
    epc, ewhy, \* the ending task per holder: "none" | "call" | "wait" | "settle"
    hpc, hfor, hres,  \* the holding task per holder: "none" | "hold" | "settle"; result
    rpc,       \* the resume task per conversation: "none" | "run"
    \* --- findings
    clobber,       \* _set_holder(new) overwrote a different live holder
    engineRefused, \* a session the arbiter let in found the engine taken
    silentEnd      \* an AWAKE room closed without a word (CALL_IN / TAKEN)

vars == <<st, stt, heldF, conn, asleep, ans, apc, tgt, lockOwner, holder, held, heldFor,
          rel, epc, ewhy, hpc, hfor, hres, rpc, clobber, engineRefused, silentEnd>>

Init ==
    /\ st = [s \in Claims |-> "off"]
    /\ stt = [s \in Claims |-> FALSE]
    /\ heldF = [s \in Claims |-> FALSE]
    /\ conn = {} /\ asleep = TRUE /\ ans = {}
    /\ apc = [s \in Claims |-> "none"]
    /\ tgt = [s \in Claims |-> None]
    /\ lockOwner = None
    /\ holder = None /\ held = None /\ heldFor = None /\ rel = {}
    /\ epc = [s \in Claims |-> "none"] /\ ewhy = [s \in Claims |-> None]
    /\ hpc = [s \in Claims |-> "none"] /\ hfor = [s \in Claims |-> None]
    /\ hres = [s \in Claims |-> FALSE]
    /\ rpc = [s \in Claims |-> "none"]
    /\ clobber = FALSE /\ engineRefused = FALSE /\ silentEnd = FALSE

(***************************************************************************)
(* decide() (session_arbiter.py:175). Claim.gone (REAPED) is left out: no  *)
(* client vanishes silently here (a session's end is its release).         *)
(***************************************************************************)
Why(h, n) ==
    IF h = None THEN "grant"
    ELSE IF Kind(h) = "call" /\ Kind(n) = "call" THEN "REPLACED"   \* same client "sip"
    ELSE IF h = "room" /\ asleep
         THEN IF Kind(n) = "call" THEN "CALL_IN"
              ELSE IF n = "talk" THEN "TAKEN" ELSE "REFUSED"
    ELSE IF Kind(n) = "call" /\ h \in {"talk", "room"}
         THEN IF ASKABLE THEN "PROMPT"
              ELSE IF h = "talk" THEN "CALL_IN" ELSE "REFUSED"
    ELSE "REFUSED"

\* A holder with an end, a hold or a resume in flight: acquire waits it out.
Busy(h) == h # None /\ (epc[h] # "none" \/ hpc[h] # "none" \/ rpc[h] # "none")

(***************************************************************************)
(* release() (session_arbiter.py:477) and _resume_held (:456), as records  *)
(* of the arbiter's next values; `res` is the conversation whose resume    *)
(* task starts (None: none).                                               *)
(***************************************************************************)
RelRec(x) ==
    LET rel1 == rel \cup {x}
        held1 == IF held = x THEN None ELSE held
        hf1 == IF held = x THEN None ELSE heldFor
        doRes == holder = x \/ (x = hf1 /\ holder = None)
        r == IF doRes /\ held1 # None /\ held1 \notin rel1 THEN held1 ELSE None
    IN [rel |-> rel1,
        holder |-> IF doRes THEN r ELSE holder,
        held |-> IF doRes THEN None ELSE held1,
        heldFor |-> IF doRes THEN None ELSE hf1,
        res |-> r]

\* _resume_held() on its own (from _acquire / _holding).
ResRec ==
    LET r == IF held # None /\ held \notin rel THEN held ELSE None
    IN [rel |-> rel, holder |-> IF r # None THEN r ELSE holder,
        held |-> None, heldFor |-> None, res |-> r]

ApplyArb(r) ==
    /\ rel' = r.rel /\ holder' = r.holder /\ held' = r.held /\ heldFor' = r.heldFor
    /\ rpc' = IF r.res # None THEN [rpc EXCEPT ![r.res] = "run"] ELSE rpc

EngineBusy(s) == \E o \in Claims \ {s} : stt[o]

(***************************************************************************)
(* The sessions.                                                           *)
(***************************************************************************)
StartOK(s) ==
    CASE s = C2 -> IF SIP = "sequential" THEN st[C1] = "out"
                   ELSE st[C1] \in {"stopping", "out"}
      [] OTHER  -> TRUE

Start(s) ==
    /\ st[s] = "off" /\ StartOK(s)
    /\ st' = [st EXCEPT ![s] = "acq"]
    /\ apc' = [apc EXCEPT ![s] = "lock"]
    /\ UNCHANGED <<stt, heldF, conn, asleep, ans, tgt, lockOwner, holder, held, heldFor,
                   rel, epc, ewhy, hpc, hfor, hres, rpc, clobber, engineRefused, silentEnd>>

\* The pipeline starts and its STT connects (or the engine refuses it, and the
\* session says it cannot hear and ends -- releasing its claim).
Connect(s) ==
    /\ st[s] = "run" /\ s \notin conn /\ ~heldF[s]
    /\ conn' = conn \cup {s}
    /\ IF EngineBusy(s)
       THEN /\ engineRefused' = TRUE
            /\ st' = [st EXCEPT ![s] = "out"]
            /\ ApplyArb(RelRec(s))
            /\ UNCHANGED stt
       ELSE /\ stt' = [stt EXCEPT ![s] = TRUE]
            /\ UNCHANGED <<engineRefused, st, rel, holder, held, heldFor, rpc>>
    /\ UNCHANGED <<heldF, asleep, ans, apc, tgt, lockOwner, epc, ewhy, hpc, hfor, hres,
                   clobber, silentEnd>>

\* A conversation ends (its client left, or the arbiter's end closed it): the
\* pipeline stops, then `finally: ARBITER.release(claim)` -- no await between.
StopConv(s) ==
    /\ s \in Convs /\ st[s] = "run"
    /\ st' = [st EXCEPT ![s] = "out"]
    /\ stt' = [stt EXCEPT ![s] = FALSE]
    /\ heldF' = [heldF EXCEPT ![s] = FALSE]
    /\ ApplyArb(RelRec(s))
    /\ UNCHANGED <<conn, asleep, ans, apc, tgt, lockOwner, epc, ewhy, hpc, hfor, hres,
                   clobber, engineRefused, silentEnd>>

\* A call ends: cancel_active_call stops the pipeline first ...
StopCall(c) ==
    /\ c \in Calls /\ st[c] = "run"
    /\ st' = [st EXCEPT ![c] = "stopping"]
    /\ stt' = [stt EXCEPT ![c] = FALSE]
    /\ UNCHANGED <<heldF, conn, asleep, ans, apc, tgt, lockOwner, holder, held, heldFor,
                   rel, epc, ewhy, hpc, hfor, hres, rpc, clobber, engineRefused, silentEnd>>

\* ... and lets the claim go after its 0.3 s settle (presence.end()).
ReleaseCall(c) ==
    /\ st[c] = "stopping"
    /\ st' = [st EXCEPT ![c] = "out"]
    /\ ApplyArb(RelRec(c))
    /\ UNCHANGED <<stt, heldF, conn, asleep, ans, apc, tgt, lockOwner, epc, ewhy, hpc,
                   hfor, hres, clobber, engineRefused, silentEnd>>

\* The caller hangs up while the call asks: the bring-up is cancelled, and
\* CallClaim.start's except releases the claim. Shielded tasks run on.
CancelCall(c) ==
    /\ c \in Calls /\ st[c] = "acq"
    /\ st' = [st EXCEPT ![c] = "out"]
    /\ apc' = [apc EXCEPT ![c] = "none"]
    /\ lockOwner' = IF lockOwner = c THEN None ELSE lockOwner
    /\ ApplyArb(RelRec(c))
    /\ UNCHANGED <<stt, heldF, conn, asleep, ans, tgt, epc, ewhy, hpc, hfor, hres,
                   clobber, engineRefused, silentEnd>>

\* The gateway answers the call while it rings (auto_answer, a replay).
Answered(c) ==
    /\ AUTO /\ c \in Calls /\ st[c] = "acq" /\ c \notin ans
    /\ ans' = ans \cup {c}
    /\ UNCHANGED <<st, stt, heldF, conn, asleep, apc, tgt, lockOwner, holder, held,
                   heldFor, rel, epc, ewhy, hpc, hfor, hres, rpc, clobber, engineRefused,
                   silentEnd>>

\* The room wakes (a wake word; someone speaks) or falls asleep (keep-alive).
RoomFlip ==
    /\ "room" \in Convs /\ st["room"] \in {"acq", "run"} /\ ~heldF["room"]
    /\ asleep' = ~asleep
    /\ UNCHANGED <<st, stt, heldF, conn, ans, apc, tgt, lockOwner, holder, held, heldFor,
                   rel, epc, ewhy, hpc, hfor, hres, rpc, clobber, engineRefused, silentEnd>>

(***************************************************************************)
(* acquire() (session_arbiter.py:283/298).                                 *)
(***************************************************************************)
Unchanged_Sess == UNCHANGED <<stt, heldF, conn, asleep, ans>>

TakeLock(s) ==
    /\ apc[s] = "lock" /\ lockOwner = None
    /\ lockOwner' = s
    /\ apc' = [apc EXCEPT ![s] = "loop"]
    /\ Unchanged_Sess
    /\ UNCHANGED <<st, tgt, holder, held, heldFor, rel, epc, ewhy, hpc, hfor, hres, rpc,
                   clobber, engineRefused, silentEnd>>

\* Start the holder's end (_end: one ending per holder, shared).
SpawnEnd(h, why) ==
    /\ epc' = IF epc[h] = "none" THEN [epc EXCEPT ![h] = "call"] ELSE epc
    /\ ewhy' = IF epc[h] = "none" THEN [ewhy EXCEPT ![h] = why] ELSE ewhy

\* The end of _acquire: `if new.released: _resume_held(); return None`, else
\* `_set_holder(new)`. "redecide" goes back to the loop instead of overwriting a
\* holder that appeared while the old one was being ended.
Final(s) ==
    IF s \in rel
    THEN /\ ApplyArb(ResRec)
         /\ clobber' = (clobber \/ (ResRec.res # None /\ holder # None /\ holder # ResRec.res))
         /\ st' = [st EXCEPT ![s] = "run"]
         /\ apc' = [apc EXCEPT ![s] = "none"]
         /\ lockOwner' = None
    ELSE IF MODE = "redecide" /\ holder # None /\ holder # s
    THEN /\ apc' = [apc EXCEPT ![s] = "loop"]
         /\ UNCHANGED <<st, lockOwner, rel, holder, held, heldFor, rpc, clobber>>
    ELSE /\ holder' = s
         /\ clobber' = (clobber \/ (holder # None /\ holder # s))
         /\ st' = [st EXCEPT ![s] = "run"]
         /\ apc' = [apc EXCEPT ![s] = "none"]
         /\ lockOwner' = None
         /\ UNCHANGED <<rel, held, heldFor, rpc>>

Refuse(s) ==
    /\ st' = [st EXCEPT ![s] = "out"]
    /\ apc' = [apc EXCEPT ![s] = "none"]
    /\ lockOwner' = None

\* The while loop: wait out an end/hold/resume of the holder, then decide.
Decide(s) ==
    /\ apc[s] = "loop" /\ lockOwner = s /\ ~Busy(holder)
    /\ LET h == holder
           why == Why(h, s)
       IN CASE why = "grant" ->
                 /\ Final(s)
                 /\ UNCHANGED <<tgt, epc, ewhy, hpc, hfor, hres, silentEnd>>
            [] why = "PROMPT" ->
                 /\ tgt' = [tgt EXCEPT ![s] = h]
                 /\ IF s \in ans
                    THEN /\ hpc' = [hpc EXCEPT ![h] = "hold"]
                         /\ hfor' = [hfor EXCEPT ![h] = s]
                         /\ apc' = [apc EXCEPT ![s] = "holdw"]
                    ELSE /\ apc' = [apc EXCEPT ![s] = "ask"]
                         /\ UNCHANGED <<hpc, hfor>>
                 /\ UNCHANGED <<st, lockOwner, holder, held, heldFor, rel, epc, ewhy, hres,
                                rpc, clobber, silentEnd>>
            [] why = "REFUSED" ->
                 /\ Refuse(s)
                 /\ UNCHANGED <<tgt, holder, held, heldFor, rel, epc, ewhy, hpc, hfor,
                                hres, rpc, clobber, silentEnd>>
            [] OTHER ->
                 /\ SpawnEnd(h, why)
                 /\ tgt' = [tgt EXCEPT ![s] = h]
                 /\ apc' = [apc EXCEPT ![s] = "endw"]
                 /\ UNCHANGED <<st, lockOwner, holder, held, heldFor, rel, hpc, hfor, hres,
                                rpc, clobber, silentEnd>>
    /\ Unchanged_Sess /\ UNCHANGED engineRefused

\* _ask (session_arbiter.py:358): the user's answer (or the prompt's default),
\* or the question withdrawn (the holder left, or the call got answered).
Ask(s) ==
    /\ apc[s] = "ask"
    /\ LET h == tgt[s] IN
       \E out \in {"yes", "no", "withdrawn"} :
         /\ (out = "withdrawn") => (h \in rel \/ s \in ans)
         /\ LET take == IF s \in ans /\ h \notin rel THEN "yes" ELSE out IN
              CASE take = "no" ->      \* declined: the call is left ringing
                     /\ Refuse(s)
                     /\ UNCHANGED <<hpc, hfor>>
                [] take = "yes" ->
                     /\ hpc' = [hpc EXCEPT ![h] = "hold"]
                     /\ hfor' = [hfor EXCEPT ![h] = s]
                     /\ apc' = [apc EXCEPT ![s] = "holdw"]
                     /\ UNCHANGED <<st, lockOwner>>
                [] OTHER ->
                     /\ apc' = [apc EXCEPT ![s] = "post"]
                     /\ UNCHANGED <<st, lockOwner, hpc, hfor>>
    /\ Unchanged_Sess
    /\ UNCHANGED <<tgt, holder, held, heldFor, rel, epc, ewhy, hres, rpc, clobber,
                   engineRefused, silentEnd>>

\* The hold is over: put on hold, or (it would not let go) ended instead.
HoldDone(s) ==
    /\ apc[s] = "holdw" /\ hpc[tgt[s]] = "none"
    /\ IF hres[tgt[s]]
       THEN /\ apc' = [apc EXCEPT ![s] = "post"]
            /\ UNCHANGED <<epc, ewhy>>
       ELSE /\ SpawnEnd(tgt[s], "CALL_IN")
            /\ apc' = [apc EXCEPT ![s] = "endw1"]
    /\ Unchanged_Sess
    /\ UNCHANGED <<st, tgt, lockOwner, holder, held, heldFor, rel, hpc, hfor, hres, rpc,
                   clobber, engineRefused, silentEnd>>

EndDone1(s) ==
    /\ apc[s] = "endw1" /\ epc[tgt[s]] = "none"
    /\ apc' = [apc EXCEPT ![s] = "post"]
    /\ Unchanged_Sess
    /\ UNCHANGED <<st, tgt, lockOwner, holder, held, heldFor, rel, epc, ewhy, hpc, hfor,
                   hres, rpc, clobber, engineRefused, silentEnd>>

\* After the question: `if new.released` -> resume the held one, return; else
\* decide again against whoever holds it now (a PROMPT now counts as REFUSED).
Post(s) ==
    /\ apc[s] = "post"
    /\ IF s \in rel
       THEN /\ Final(s)
            /\ UNCHANGED <<tgt, epc, ewhy>>
       ELSE LET h == holder
                w0 == Why(h, s)
                why == IF w0 = "PROMPT" THEN "REFUSED" ELSE w0
            IN CASE why = "grant" ->
                      /\ Final(s)
                      /\ UNCHANGED <<tgt, epc, ewhy>>
                 [] why = "REFUSED" ->
                      /\ Refuse(s)
                      /\ UNCHANGED <<tgt, holder, held, heldFor, rel, epc, ewhy, rpc,
                                     clobber>>
                 [] OTHER ->
                      /\ SpawnEnd(h, why)
                      /\ tgt' = [tgt EXCEPT ![s] = h]
                      /\ apc' = [apc EXCEPT ![s] = "endw"]
                      /\ UNCHANGED <<st, lockOwner, holder, held, heldFor, rel, rpc,
                                     clobber>>
    /\ Unchanged_Sess
    /\ UNCHANGED <<hpc, hfor, hres, engineRefused, silentEnd>>

\* The holder ended (_end returned): the end of _acquire.
EndDone(s) ==
    /\ apc[s] = "endw" /\ epc[tgt[s]] = "none"
    /\ Final(s)
    /\ Unchanged_Sess
    /\ UNCHANGED <<tgt, epc, ewhy, hpc, hfor, hres, engineRefused, silentEnd>>

(***************************************************************************)
(* The shielded tasks.                                                     *)
(***************************************************************************)
\* _ending, first step: holder.end(why). A conversation is closed (its session
\* stops on its own step: StopConv); a call's end is CallClaim._release, which
\* releases it at once. A room is closed WITHOUT a word for CALL_IN and TAKEN
\* (gateway_server._end_for), whatever asleep() says by now.
EndCall(h) ==
    /\ epc[h] = "call"
    /\ epc' = [epc EXCEPT ![h] = "wait"]
    /\ silentEnd' = (silentEnd \/ (h = "room" /\ ~asleep /\ ewhy[h] \in {"CALL_IN", "TAKEN"}))
    /\ IF Kind(h) = "call"
       THEN /\ ApplyArb(RelRec(h))
            /\ st' = IF st[h] = "stopping" THEN [st EXCEPT ![h] = "out"] ELSE st
       ELSE UNCHANGED <<rel, holder, held, heldFor, rpc, st>>
    /\ Unchanged_Sess
    /\ UNCHANGED <<apc, tgt, lockOwner, ewhy, hpc, hfor, hres, clobber, engineRefused>>

\* ... the claim is released (or END_WAIT_SECS -- not modelled): holder cleared.
EndWait(h) ==
    /\ epc[h] = "wait" /\ h \in rel
    /\ epc' = [epc EXCEPT ![h] = "settle"]
    /\ holder' = IF holder = h THEN None ELSE holder
    /\ Unchanged_Sess
    /\ UNCHANGED <<st, apc, tgt, lockOwner, held, heldFor, rel, ewhy, hpc, hfor, hres, rpc,
                   clobber, engineRefused, silentEnd>>

EndSettle(h) ==
    /\ epc[h] = "settle"
    /\ epc' = [epc EXCEPT ![h] = "none"]
    /\ Unchanged_Sess
    /\ UNCHANGED <<st, apc, tgt, lockOwner, holder, held, heldFor, rel, ewhy, hpc, hfor,
                   hres, rpc, clobber, engineRefused, silentEnd>>

\* _holding: hold() (AgentSession.hold: last line, mute, STT let go), then the
\* bookkeeping, synchronously.
HoldOk(h) ==
    /\ hpc[h] = "hold"
    /\ LET n == hfor[h]
           live == st[h] = "run"
       IN /\ stt' = IF live THEN [stt EXCEPT ![h] = FALSE] ELSE stt
          /\ heldF' = IF live THEN [heldF EXCEPT ![h] = TRUE] ELSE heldF
          /\ IF h \in rel
             THEN /\ holder' = IF holder = h THEN None ELSE holder
                  /\ hpc' = [hpc EXCEPT ![h] = "none"]
                  /\ hres' = [hres EXCEPT ![h] = TRUE]
                  /\ UNCHANGED <<held, heldFor, rpc>>
             ELSE IF n \in rel
             THEN \* the call is gone already: held, and straight back
                  /\ held' = None /\ heldFor' = None
                  /\ holder' = h
                  /\ rpc' = [rpc EXCEPT ![h] = "run"]
                  /\ hpc' = [hpc EXCEPT ![h] = "none"]
                  /\ hres' = [hres EXCEPT ![h] = TRUE]
             ELSE /\ held' = h /\ heldFor' = n
                  /\ holder' = IF holder = h THEN None ELSE holder
                  /\ hpc' = [hpc EXCEPT ![h] = "settle"]
                  /\ UNCHANGED <<hres, rpc>>
    /\ UNCHANGED <<st, conn, asleep, ans, apc, tgt, lockOwner, rel, epc, ewhy, hfor,
                   clobber, engineRefused, silentEnd>>

\* hold() raised or ran past HOLD_WAIT_SECS (no partial effect modelled).
HoldFail(h) ==
    /\ hpc[h] = "hold"
    /\ hpc' = [hpc EXCEPT ![h] = "none"]
    /\ hres' = [hres EXCEPT ![h] = FALSE]
    /\ Unchanged_Sess
    /\ UNCHANGED <<st, apc, tgt, lockOwner, holder, held, heldFor, rel, epc, ewhy, hfor,
                   rpc, clobber, engineRefused, silentEnd>>

HoldSettle(h) ==
    /\ hpc[h] = "settle"
    /\ hpc' = [hpc EXCEPT ![h] = "none"]
    /\ hres' = [hres EXCEPT ![h] = TRUE]
    /\ Unchanged_Sess
    /\ UNCHANGED <<st, apc, tgt, lockOwner, holder, held, heldFor, rel, epc, ewhy, hfor,
                   rpc, clobber, engineRefused, silentEnd>>

\* _resuming: AgentSession.resume -- the STT reconnects (or cannot: the session
\* says so and ends), the mic opens.
ResumeRun(h) ==
    /\ rpc[h] = "run"
    /\ rpc' = [rpc EXCEPT ![h] = "none"]
    /\ IF st[h] # "run"
       THEN UNCHANGED <<st, stt, heldF, conn, rel, holder, held, heldFor, engineRefused>>
       ELSE IF EngineBusy(h)
       THEN /\ engineRefused' = TRUE
            /\ st' = [st EXCEPT ![h] = "out"]
            /\ heldF' = [heldF EXCEPT ![h] = FALSE]
            /\ conn' = conn \cup {h}
            /\ LET r == RelRec(h) IN
                 /\ rel' = r.rel /\ holder' = r.holder /\ held' = r.held
                 /\ heldFor' = r.heldFor
            /\ UNCHANGED stt
       ELSE /\ stt' = [stt EXCEPT ![h] = TRUE]
            /\ heldF' = [heldF EXCEPT ![h] = FALSE]
            /\ conn' = conn \cup {h}
            /\ UNCHANGED <<st, rel, holder, held, heldFor, engineRefused>>
    /\ UNCHANGED <<asleep, ans, apc, tgt, lockOwner, epc, ewhy, hpc, hfor, hres, clobber,
                   silentEnd>>

Next ==
    \/ \E s \in Claims :
         \/ Start(s) \/ Connect(s) \/ StopConv(s) \/ StopCall(s) \/ ReleaseCall(s)
         \/ CancelCall(s) \/ Answered(s)
         \/ TakeLock(s) \/ Decide(s) \/ Ask(s) \/ HoldDone(s) \/ EndDone1(s) \/ Post(s)
         \/ EndDone(s)
         \/ EndCall(s) \/ EndWait(s) \/ EndSettle(s)
         \/ HoldOk(s) \/ HoldFail(s) \/ HoldSettle(s) \/ ResumeRun(s)
    \/ RoomFlip

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Properties.                                                             *)
(***************************************************************************)
\* What every front-end relies on when acquire() returns None: its STT finds the
\* engine free, because only the holder's STT is ever connected.
SttOnlyHolder == \A s \in Claims : stt[s] => holder = s

\* One conversation at a time: never two sessions live (granted, not on hold).
OneConversation ==
    Cardinality({s \in Claims : st[s] = "run" /\ ~heldF[s]}) <= 1

\* No session the arbiter let in is turned away by the engine: the caller or
\* user would hear "I can't hear you right now" and the session ends.
NoEngineRefusal == ~engineRefused

\* _set_holder(new) never overwrites a different holder.
NoClobber == ~clobber

\* A conversation on hold is the arbiter's held one, or being resumed: never
\* muted with nobody left to bring it back.
NoStrandedHold ==
    \A s \in Convs : (heldF[s] /\ st[s] = "run") => (held = s \/ rpc[s] # "none")

\* A hold outlives its call only until the call's release brings it back.
NoOrphanHold == held # None => heldFor \notin rel

\* An awake room is asked (PROMPT), never closed without a word.
NoSilentLiveEnd == ~silentEnd

\* A ringing call (or any newcomer) is never stranded inside acquire(): wherever
\* nothing more can happen, every acquire, end, hold and resume has finished and
\* every session has ended. The bounded waits are steps here, so this is the
\* untimed half of the claim; the timeouts are what bound it in time.
NoStuckAcquire ==
    (~ENABLED Next) =>
        \A s \in Claims : /\ apc[s] = "none" /\ epc[s] = "none" /\ hpc[s] = "none"
                         /\ rpc[s] = "none" /\ st[s] \in {"off", "out"}

TypeOK ==
    /\ holder \in Claims \cup {None}
    /\ held \in Convs \cup {None}
    /\ heldFor \in Calls \cup {None}
    /\ lockOwner \in Claims \cup {None}
    /\ (holder # None) => holder \notin rel
=============================================================================
