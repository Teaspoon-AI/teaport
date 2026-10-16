-------------------------------- MODULE SipCall --------------------------------
(***************************************************************************)
(* teaport -- the SIP per-call lifecycle, and the call's claim on the      *)
(* session arbiter.                                                         *)
(*                                                                          *)
(*   brain/teaport_brain/sip_server.py     run_connection: on_call_incoming,*)
(*       on_call_state, start_call, cancel_setup, cancel_active_call,       *)
(*       bring_up_call/_bring_up, on_pipeline_finished, CallClaim            *)
(*   brain/teaport_brain/sip_transport.py  SipConnection._receive_messages  *)
(*   brain/teaport_brain/session_arbiter.py  acquire/_end/release (calls)  *)
(*                                                                          *)
(* One AF_UNIX SEQPACKET connection carries call control AND caller audio.  *)
(* `_receive_messages` is the ONLY reader, and the control handlers are     *)
(* registered sync=True: they run INLINE in that loop, in wire order, so    *)
(* whatever a handler awaits is time the socket is not read.                *)
(*                                                                          *)
(* Since #111 the brain answers the call (auto_answer=false): call.incoming *)
(* starts a bring-up TASK (start_call) that asks the session arbiter for    *)
(* the engine (CallClaim.start -> ARBITER.acquire), builds and PUBLISHES    *)
(* the pipeline (active["call"]), sends call.answer, and greets once the    *)
(* gateway's `confirmed` arrives. Teardown (cancel_active_call) stays       *)
(* INLINE in the reader when a handler runs it, and ends with               *)
(* presence.end(), which releases whatever claim CallClaim holds NOW.       *)
(* A pipeline that ends on its own tears itself down from a separate TASK   *)
(* (on_pipeline_finished), which runs concurrently with the reader.         *)
(*                                                                          *)
(* MODE:                                                                    *)
(*   "asWritten"     -- 2026-08: bring-up inline, teardown ignores call_id  *)
(*   "callIdChecked" -- 2026-08: teardown matches call_id; still inline     *)
(*   "shipped"       -- sip_server.py today (bring-up a task, ring+answer,  *)
(*                      CallClaim.end() releases the claim it holds now)    *)
(*   "claimByCall"   -- proposed: a teardown releases ITS call's claim      *)
(*                      (also when cancelled mid-way), and start_call waits *)
(*                      out the teardown tasks still in flight              *)
(*                                                                          *)
(* The 2026-08 modes keep the 2026-08 gateway: it confirms by itself and    *)
(* may confirm a second call before the first's `disconnected`. The         *)
(* shipped modes use teaport-sip >= 0.6.0: one call at a time (a second     *)
(* INVITE while one is ringing or up gets 486), except that an INVITE may   *)
(* land while a call the BRAIN hung up is still on its way out             *)
(* (teaport-sip call.cpp onIncomingCall), and auto_answer=false unless      *)
(* AUTO.                                                                    *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Calls, MODE, AUTO

None == "none"
Inline   == MODE \in {"asWritten", "callIdChecked"}   \* bring-up runs in the reader
Guarded  == MODE # "asWritten"                         \* teardown matches call_id
RingGw   == MODE \in {"shipped", "claimByCall"}        \* #111 ring + 0.6.0 gateway
ClaimFix == MODE = "claimByCall"

VARIABLES
    \* --- the gateway
    wire,        \* control events emitted to the brain, not yet dispatched
    gw,          \* per call: "idle" | "ringing" | "up" | "ending" | "gone"
    hang,        \* calls the brain sent call.hangup for
    ansSent,     \* calls the brain sent call.answer for
    \* --- the reader (SipConnection._receive_messages) and its inline handler
    rd,          \* "free" | "ca" (cancel_active_call next) | "wait" (inline teardown)
                 \*   | "rest" (rest of the handler) | "bringUp" (inline bring-up)
    rdKind,      \* "start" (start_call) | "disc" (the disconnected branch)
    rdCall,      \* the call the handler is about
    rdAns,       \* start_call's `answered`
    heard,       \* calls whose incoming/confirmed the reader has dispatched
    \* --- sip_server's per-connection state
    active,      \* active["call"]'s call id, or None
    pipe,        \* calls whose pipeline is running (STT connected)
    setup,       \* setup["call_id"]: the bring-up task's call, or None
    bpc,         \* that task's pc: "none" | "new" | "settle" | "ring" | "greet" | "td"
    td,          \* teardowns in flight: <<call, owner>>, owner "reader"|"bringup"|"task"
    ring,        \* the call in `rings`, or None
    ringAns,     \* its `answered` event
    presence,    \* CallClaim._claim: whose claim it holds, or None
    \* --- the session arbiter, as far as calls see it
    holder,      \* the claim holding the engine (a call), or None
    rel,         \* calls whose claim has been released (Claim.released)
    \* --- findings
    wrongTeardown, \* a disconnected for one call tore down a DIFFERENT call
    replaced       \* a call's acquire found another call's claim still held

vars == <<wire, gw, hang, ansSent, rd, rdKind, rdCall, rdAns, heard, active, pipe,
          setup, bpc, td, ring, ringAns, presence, holder, rel, wrongTeardown, replaced>>

gwVars   == <<wire, gw, hang, ansSent>>
rdVars   == <<rd, rdKind, rdCall, rdAns, heard>>
arbVars  == <<presence, holder, rel>>

Init ==
    /\ wire = << >>
    /\ gw = [c \in Calls |-> "idle"]
    /\ hang = {} /\ ansSent = {}
    /\ rd = "free" /\ rdKind = "start" /\ rdCall = None /\ rdAns = FALSE
    /\ heard = {}
    /\ active = None /\ pipe = {} /\ setup = None /\ bpc = "none" /\ td = {}
    /\ ring = None /\ ringAns = FALSE
    /\ presence = None /\ holder = None /\ rel = {}
    /\ wrongTeardown = FALSE /\ replaced = FALSE

(***************************************************************************)
(* The claim helpers. presence.end() at the end of a teardown for call c:  *)
(* as shipped it releases whatever claim CallClaim holds at that moment    *)
(* (CallClaim.end, sip_server.py:379) -- by then possibly a NEWER call's;  *)
(* claimByCall releases c's own. release() is SessionArbiter.release      *)
(* restricted to calls: the claim is released, and the holder slot freed   *)
(* if it held the slot.                                                    *)
(***************************************************************************)
EndClaim(c) ==
    IF ClaimFix
    THEN [rel |-> rel \cup {c},
          holder |-> IF holder = c THEN None ELSE holder,
          presence |-> IF presence = c THEN None ELSE presence]
    ELSE IF presence = None
         THEN [rel |-> rel, holder |-> holder, presence |-> None]
         ELSE [rel |-> rel \cup {presence},
               holder |-> IF holder = presence THEN None ELSE holder,
               presence |-> None]

ApplyClaim(r) == rel' = r.rel /\ holder' = r.holder /\ presence' = r.presence

(***************************************************************************)
(* cancel_setup(call_id), and the await of the cancelled task, as one step *)
(* (sip_server.py:644). What the cancellation does depends on where the    *)
(* bring-up is:                                                            *)
(*   new    -- never ran: nothing (no claim was taken)                     *)
(*   settle -- inside ARBITER.acquire: CallClaim.start's except releases   *)
(*             the claim (sip_server.py:356)                               *)
(*   ring/greet -- the pipeline stays published in active["call"]; the     *)
(*             cancel_active_call after this tears it down                 *)
(*   td     -- inside its OWN cancel_active_call ("never confirmed", "STT  *)
(*             unavailable"). In wait_for(runner_task) the CancelledError  *)
(*             is swallowed (sip_server.py:566) and the teardown completes *)
(*             with presence.end(); in the 0.3 s settle it propagates and  *)
(*             presence.end() never runs. claimByCall releases in a        *)
(*             finally either way.                                         *)
(* Returned as the set of possible outcomes.                               *)
(***************************************************************************)
CancelOutcomes ==
    LET s == setup
        keep == [rel |-> rel, holder |-> holder, presence |-> presence,
                 pipe |-> pipe, td |-> td]
    IN CASE bpc = "settle" ->
              {[keep EXCEPT !.rel = rel \cup {s},
                            !.holder = IF holder = s THEN None ELSE holder,
                            !.presence = IF presence = s THEN None ELSE presence]}
         [] bpc = "td" ->
              LET e == EndClaim(s) IN
              { \* swallowed: the teardown runs to its end
                [rel |-> e.rel, holder |-> e.holder, presence |-> e.presence,
                 pipe |-> pipe \ {s}, td |-> td \ {<<s, "bringup">>}],
                \* propagated from the settle: no presence.end()
                IF ClaimFix
                THEN [rel |-> e.rel, holder |-> e.holder, presence |-> e.presence,
                      pipe |-> pipe \ {s}, td |-> td \ {<<s, "bringup">>}]
                ELSE [keep EXCEPT !.pipe = pipe \ {s}, !.td = td \ {<<s, "bringup">>}] }
         [] OTHER -> {keep}

\* Does cancel_setup(guard) touch the in-flight bring-up? g = None: any.
CancelHits(g) == setup # None /\ (g = None \/ setup = g)

CancelSetup(g) ==
    IF CancelHits(g)
    THEN \E o \in CancelOutcomes :
            /\ rel' = o.rel /\ holder' = o.holder /\ presence' = o.presence
            /\ pipe' = o.pipe /\ td' = o.td
            /\ setup' = None /\ bpc' = "none"
    ELSE UNCHANGED <<rel, holder, presence, pipe, td, setup, bpc>>

(***************************************************************************)
(* The gateway.                                                            *)
(***************************************************************************)
OtherLive(c) == \E d \in Calls \ {c} : gw[d] \in {"ringing", "up"}

\* 2026-08: confirms by itself, a second call possibly before the first's end.
OldConfirm(c) ==
    /\ ~RingGw /\ gw[c] = "idle"
    /\ gw' = [gw EXCEPT ![c] = "up"]
    /\ wire' = Append(wire, <<"conf", c>>)
    /\ UNCHANGED <<hang, ansSent>>

\* teaport-sip >= 0.6.0: one call ringing or up at a time.
GwIncoming(c) ==
    /\ RingGw /\ gw[c] = "idle" /\ ~OtherLive(c)
    /\ gw' = [gw EXCEPT ![c] = "ringing"]
    /\ wire' = Append(wire, <<"inc", c>>)
    /\ UNCHANGED <<hang, ansSent>>

\* Answered: by our call.answer, or by the gateway itself (auto_answer=true).
GwAnswer(c) ==
    /\ RingGw /\ gw[c] = "ringing" /\ (c \in ansSent \/ AUTO)
    /\ gw' = [gw EXCEPT ![c] = "up"]
    /\ wire' = Append(wire, <<"conf", c>>)
    /\ UNCHANGED <<hang, ansSent>>

\* Our call.hangup reaches pjsua: the call is on its way out (and no longer
\* counts against a new INVITE), its `disconnected` not sent yet.
GwBrainHangup(c) ==
    /\ c \in hang /\ gw[c] \in {"ringing", "up"}
    /\ gw' = [gw EXCEPT ![c] = "ending"]
    /\ UNCHANGED <<wire, hang, ansSent>>

\* The call ends: the caller hung up, or our hangup completes.
GwDisconnect(c) ==
    /\ gw[c] \in {"ringing", "up", "ending"}
    /\ gw' = [gw EXCEPT ![c] = "gone"]
    /\ wire' = Append(wire, <<"disc", c>>)
    /\ UNCHANGED <<hang, ansSent>>

GwStep ==
    /\ \E c \in Calls : OldConfirm(c) \/ GwIncoming(c) \/ GwAnswer(c)
                        \/ GwBrainHangup(c) \/ GwDisconnect(c)
    /\ UNCHANGED <<rdVars, active, pipe, setup, bpc, td, ring, ringAns, arbVars,
                   wrongTeardown, replaced>>

(***************************************************************************)
(* The reader: dispatch one control event, inline.                         *)
(***************************************************************************)
Head1 == Head(wire)

\* call.incoming (on_call_incoming, sip_server.py:609). Over a call that is up
\* (answered) it is left ringing; otherwise start_call(c, answered=False).
DispIncoming ==
    /\ rd = "free" /\ wire # << >> /\ Head1[1] = "inc"
    /\ LET c == Head1[2] IN
         /\ wire' = Tail(wire)
         /\ heard' = heard \cup {c}
         /\ IF active # None /\ active # c /\ ring = active /\ ringAns
            THEN /\ UNCHANGED <<rd, rdKind, rdCall, rdAns>>
                 /\ UNCHANGED <<setup, bpc, rel, holder, presence, pipe, td>>
            ELSE /\ CancelSetup(None)
                 /\ rd' = "ca" /\ rdKind' = "start" /\ rdCall' = c /\ rdAns' = FALSE
    /\ UNCHANGED <<gw, hang, ansSent, active, ring, ringAns, wrongTeardown, replaced>>

\* call.state=confirmed (sip_server.py:886). The ring's own: mark it answered.
\* Otherwise (no ring before it): start_call(c, answered=True).
DispConfirm ==
    /\ rd = "free" /\ wire # << >> /\ Head1[1] = "conf"
    /\ LET c == Head1[2] IN
         /\ wire' = Tail(wire)
         /\ heard' = heard \cup {c}
         /\ IF RingGw /\ ring = c
            THEN /\ ringAns' = TRUE
                 /\ UNCHANGED <<rd, rdKind, rdCall, rdAns>>
                 /\ UNCHANGED <<setup, bpc, rel, holder, presence, pipe, td>>
            ELSE /\ CancelSetup(None)
                 /\ rd' = "ca" /\ rdKind' = "start" /\ rdCall' = c /\ rdAns' = TRUE
                 /\ UNCHANGED ringAns
    /\ UNCHANGED <<gw, hang, ansSent, active, ring, wrongTeardown, replaced>>

\* call.state=disconnected (sip_server.py:914): cancel_setup(c), then
\* cancel_active_call(c) -- both guarded by call_id, except in asWritten.
DispDisconnect ==
    /\ rd = "free" /\ wire # << >> /\ Head1[1] = "disc"
    /\ LET c == Head1[2] IN
         /\ wire' = Tail(wire)
         /\ CancelSetup(IF Guarded THEN c ELSE None)
         /\ wrongTeardown' = (wrongTeardown \/
                              (~Guarded /\ setup # None /\ setup # c))
         /\ rd' = "ca" /\ rdKind' = "disc" /\ rdCall' = c
    /\ UNCHANGED <<gw, hang, ansSent, rdAns, heard, active, ring, ringAns, replaced>>

\* The handler's cancel_active_call: tear down the running pipeline (guarded by
\* call_id on a disconnected; start_call evicts whatever runs).
ReaderCancelActive ==
    /\ rd = "ca"
    /\ LET g == IF rdKind = "disc" /\ Guarded THEN rdCall ELSE None
           hit == active # None /\ (g = None \/ active = g)
       IN IF hit
          THEN /\ td' = td \cup {<<active, "reader">>}
               /\ active' = None
               /\ rd' = "wait"
               /\ wrongTeardown' = (wrongTeardown \/
                                    (rdKind = "disc" /\ active # rdCall))
          ELSE /\ rd' = "rest"
               /\ UNCHANGED <<td, active, wrongTeardown>>
    /\ UNCHANGED <<gwVars, rdKind, rdCall, rdAns, heard, pipe, setup, bpc, ring, ringAns,
                   arbVars, replaced>>

\* claimByCall: start_call also awaits the teardown TASKS still in flight (the
\* `teardowns` set), so a superseded call's claim is gone before the next asks.
DrainWait == ClaimFix /\ rdKind = "start" /\ \E t \in td : t[2] = "task"

\* The rest of the handler, after its teardown.
ReaderRest ==
    /\ rd = "rest" /\ ~DrainWait
    /\ IF rdKind = "start"
       THEN \* start_call: rings = {c}; setup = a NEW bring-up task (inline: run it here).
            /\ ring' = rdCall /\ ringAns' = rdAns
            /\ setup' = rdCall /\ bpc' = "new"
            /\ rd' = IF Inline THEN "bringUp" ELSE "free"
            /\ UNCHANGED arbVars
       ELSE \* disconnected: no pipeline and no bring-up left -> presence.end().
            /\ IF active = None /\ setup = None
               THEN ApplyClaim(EndClaim(rdCall))
               ELSE UNCHANGED arbVars
            /\ ring' = IF ring = rdCall THEN None ELSE ring
            /\ ringAns' = IF ring = rdCall THEN FALSE ELSE ringAns
            /\ rd' = "free"
            /\ UNCHANGED <<setup, bpc>>
    /\ UNCHANGED <<gwVars, rdKind, rdCall, rdAns, heard, active, pipe, td,
                   wrongTeardown, replaced>>

(***************************************************************************)
(* A teardown (cancel_active_call, sip_server.py:532) finishing: the       *)
(* pipeline has stopped (wait_for runner_task), the 0.3 s settle is over,  *)
(* and presence.end() runs. Then whoever ran it carries on.                *)
(***************************************************************************)
BringUpOver ==
    /\ setup' = None /\ bpc' = "none"

TdEnd(t) ==
    /\ t \in td
    /\ LET c == t[1] IN
         /\ pipe' = pipe \ {c}
         /\ td' = td \ {t}
         /\ ApplyClaim(EndClaim(c))
         /\ CASE t[2] = "reader"  -> /\ rd' = "rest"
                                    /\ UNCHANGED <<setup, bpc>>
              [] t[2] = "bringup" -> /\ BringUpOver
                                    /\ rd' = IF rd = "bringUp" THEN "free" ELSE rd
              [] OTHER            -> UNCHANGED <<rd, setup, bpc>>
    /\ UNCHANGED <<gwVars, rdKind, rdCall, rdAns, heard, active, ring, ringAns,
                   wrongTeardown, replaced>>

(***************************************************************************)
(* The bring-up task (bring_up_call/_bring_up, sip_server.py:665/745).      *)
(***************************************************************************)
\* acquire granted, and the pipeline published -- synchronous from the grant on
\* (sip_server.py:751-842). A claim released while it was asked for comes back
\* "granted" (session_arbiter._acquire: `if new.released ... return None`) but
\* installs no holder; _bring_up builds the call anyway.
GrantPublish(s) ==
    /\ holder' = IF s \in rel THEN holder ELSE s
    /\ active' = s
    /\ pipe' = pipe \cup {s}
    /\ IF ringAns
       THEN /\ bpc' = "greet" /\ UNCHANGED ansSent
       ELSE /\ bpc' = "ring"  /\ ansSent' = ansSent \cup {s}

\* The task's first run: presence.start() (CallClaim._claim = this claim), then
\* ARBITER.acquire. The holder is a call (the same "sip" client): REPLACED, its
\* end() is CallClaim._release -> release(it); then the 0.3 s settle.
BuRun ==
    /\ bpc = "new"
    /\ LET s == setup IN
         IF holder = None \/ holder = s
         THEN /\ presence' = s
              /\ GrantPublish(s)
              /\ UNCHANGED <<rel, replaced>>
         ELSE /\ presence' = s
              /\ rel' = rel \cup {holder}
              /\ holder' = None
              /\ replaced' = TRUE
              /\ bpc' = "settle"
              /\ UNCHANGED <<active, pipe, ansSent>>
    /\ UNCHANGED <<wire, gw, hang, rdVars, setup, td, ring, ringAns, wrongTeardown>>

BuSettled ==
    /\ bpc = "settle"
    /\ GrantPublish(setup)
    /\ UNCHANGED <<wire, gw, hang, rdVars, setup, td, ring, ringAns, presence, rel,
                   wrongTeardown, replaced>>

\* `confirmed` arrived (ring.answered set by the reader): greet.
BuConfirmed ==
    /\ bpc = "ring" /\ ringAns /\ ring = setup
    /\ bpc' = "greet"
    /\ UNCHANGED <<gwVars, rdVars, active, pipe, setup, td, ring, ringAns, arbVars,
                   wrongTeardown, replaced>>

\* A failed bring-up hangs up and tears its own call down, guarded by call_id:
\* "never confirmed" (no `confirmed` within 10 s of call.answer) or "STT
\* unavailable" (greet() could not reach the engine).
BuFail ==
    /\ \/ bpc = "ring" /\ ~ringAns
       \/ bpc = "greet"
    /\ LET s == setup IN
         /\ hang' = hang \cup {s}
         /\ IF active = s
            THEN /\ td' = td \cup {<<s, "bringup">>}
                 /\ active' = None
                 /\ bpc' = "td"
                 /\ UNCHANGED <<setup, rd>>
            ELSE /\ BringUpOver
                 /\ rd' = IF rd = "bringUp" THEN "free" ELSE rd
                 /\ UNCHANGED <<td, active>>
    /\ UNCHANGED <<wire, gw, ansSent, rdKind, rdCall, rdAns, heard, pipe, ring, ringAns,
                   arbVars, wrongTeardown, replaced>>

BuGreeted ==
    /\ bpc = "greet"
    /\ BringUpOver
    /\ rd' = IF rd = "bringUp" THEN "free" ELSE rd
    /\ UNCHANGED <<gwVars, rdKind, rdCall, rdAns, heard, active, pipe, td, ring, ringAns,
                   arbVars, wrongTeardown, replaced>>

(***************************************************************************)
(* A pipeline that ends on its own (on_pipeline_finished, sip_server.py:    *)
(* 798): the STT, TTS or transport was written off, or a timeout fired.    *)
(* The pipeline has stopped; it hangs the caller up and starts a TEARDOWN  *)
(* TASK -- cancel_active_call(call_id), concurrent with the reader.        *)
(***************************************************************************)
PipelineEnds(c) ==
    /\ c \in pipe /\ active = c
    /\ pipe' = pipe \ {c}
    /\ hang' = hang \cup {c}
    /\ active' = None
    /\ td' = td \cup {<<c, "task">>}
    /\ UNCHANGED <<wire, gw, ansSent, rdVars, setup, bpc, ring, ringAns, arbVars,
                   wrongTeardown, replaced>>

Next ==
    \/ GwStep
    \/ DispIncoming \/ DispConfirm \/ DispDisconnect
    \/ ReaderCancelActive \/ ReaderRest
    \/ \E t \in td : TdEnd(t)
    \/ BuRun \/ BuSettled \/ BuConfirmed \/ BuFail \/ BuGreeted
    \/ \E c \in Calls : PipelineEnds(c)

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Properties.                                                             *)
(***************************************************************************)

\* 2026-08 finding #4: a disconnected for one call tore down another. The caller
\* on the torn-down call is left connected to a gateway with no brain.
NoWrongTeardown == ~wrongTeardown

\* 2026-08 finding #2, stated structurally: the reader is inside a handler while
\* control waits behind it. Strict: ANY inline wait counts, the teardown too.
NoBlockedWithPendingControl ==
    ~((rd \in {"wait", "bringUp"} \/ (rd = "rest" /\ DrainWait)) /\ wire # << >>)

\* What the 2026-08 fix did: the BRING-UP (the arbiter's question, up to ~30 s;
\* greet()'s 12 s STT poll) never blocks the reader. The inline teardown still
\* does -- bounded by cancel_active_call's 5 s wait + 0.3 s settle (+ reclaim).
NoBringUpBlocksReader == ~(rd = "bringUp" /\ wire # << >>)

\* The arbiter's view matches the call's: a call whose pipeline runs (its STT on
\* the engine) holds the slot. Violated, the arbiter reads the box as free while
\* a call is live: a room mic or a Talk client is granted the engine under the
\* call (and the busy lamp and the face's "call" go dark).
NoUnclaimedCall == \A c \in pipe : holder = c

\* What CallClaim._release's comment relies on: the SIP front-end lets a call's
\* claim go before the next call asks, so the arbiter never REPLACES one call with
\* another. (SessionArbiter.tla shows what REPLACED does to a conversation held for
\* the replaced call.)
NoReplacedCall == ~replaced

\* An answered call is never abandoned without a hangup: the caller is never left
\* on a line with no brain behind it.
NoDeadLine ==
    \A c \in Calls :
        (gw[c] = "up" /\ c \notin hang /\ c \in heard)
            => (active = c \/ setup = c \/ (rd # "free" /\ rdCall = c)
                \/ (\E t \in td : t[1] = c))

\* A ringing call the brain has heard about is being brought up, or let go.
NoStrandedRing ==
    \A c \in Calls :
        (gw[c] = "ringing" /\ c \in heard /\ c \notin hang)
            => (active = c \/ setup = c \/ (rd # "free" /\ rdCall = c)
                \/ (\E t \in td : t[1] = c))

TypeOK ==
    /\ gw \in [Calls -> {"idle", "ringing", "up", "ending", "gone"}]
    /\ rd \in {"free", "ca", "wait", "rest", "bringUp"}
    /\ active \in Calls \cup {None}
    /\ setup \in Calls \cup {None}
    /\ presence \in Calls \cup {None}
    /\ holder \in Calls \cup {None}
    /\ pipe \subseteq Calls /\ rel \subseteq Calls
    /\ bpc \in {"none", "new", "settle", "ring", "greet", "td"}
    /\ (setup = None) <=> (bpc = "none")
=============================================================================
