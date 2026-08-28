-------------------------------- MODULE SipCall --------------------------------
(***************************************************************************)
(* teaport — the SIP per-call lifecycle.                                   *)
(*                                                                          *)
(*   brain/teaport_brain/sip_server.py     on_call_state, cancel_active_call  *)
(*   brain/teaport_brain/sip_transport.py  SipConnection._receive_messages    *)
(*                                                                          *)
(* One AF_UNIX SEQPACKET connection carries call control AND caller audio.  *)
(* `_receive_messages` is the ONLY reader, and call-control handlers are    *)
(* registered `sync=True` so they run INLINE in that loop, in wire order.   *)
(* Wire order is load-bearing — a confirmed builds a pipeline and a         *)
(* following disconnected tears it down — but running inline means the loop *)
(* stops reading for as long as the handler takes, and on_call_state does   *)
(* teardown + model construction + greet()'s 12s STT poll + (on the busy    *)
(* branch) wait_until_delivered()'s 10s.                                    *)
(*                                                                          *)
(* Two findings meet here, and they compound:                               *)
(*   - teardown on `disconnected` ignores call_id; `active["call"]` never   *)
(*     records which call it belongs to                                     *)
(*   - a blocked loop lets control events queue up behind the block         *)
(*                                                                          *)
(* MODE:                                                                    *)
(*   "asWritten"      -- blocking handler, teardown ignores call_id         *)
(*   "callIdChecked"  -- teardown matches call_id; handler still blocks     *)
(*   "asyncSetup"     -- call_id matched AND the slow part is off the loop  *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Calls, MODE

VARIABLES
    wire,            \* control events the gateway has emitted, not yet dispatched
    gwLive,          \* calls the GATEWAY currently considers up
    gwDone,          \* calls already emitted, so the model stays finite
    active,          \* call id of the running per-call pipeline, or "none"
    setup,           \* call id currently being built + greeted, or "none"
    blocked,         \* the receive loop is stuck inside a handler
    wrongTeardown    \* a disconnected for one call tore down a DIFFERENT call

vars == <<wire, gwLive, gwDone, active, setup, blocked, wrongTeardown>>

NoCall == "none"

Init ==
    /\ wire = << >>
    /\ gwLive = {}
    /\ gwDone = {}
    /\ active = NoCall
    /\ setup = NoCall
    /\ blocked = FALSE
    /\ wrongTeardown = FALSE

(***************************************************************************)
(* The gateway. It may emit a confirmed for a new call BEFORE the           *)
(* disconnected for the previous one — re-INVITE, call waiting, or any      *)
(* out-of-order control emission. sip_server.py anticipates exactly that    *)
(* ("a new confirmed cancels any running call first"), so it is in scope.   *)
(***************************************************************************)

GwConfirm(c) ==
    /\ c \notin gwDone
    /\ gwLive' = gwLive \cup {c}
    /\ gwDone' = gwDone \cup {c}
    /\ wire' = Append(wire, <<"conf", c>>)
    /\ UNCHANGED <<active, setup, blocked, wrongTeardown>>

GwDisconnect(c) ==
    /\ c \in gwLive
    /\ gwLive' = gwLive \ {c}
    /\ wire' = Append(wire, <<"disc", c>>)
    /\ UNCHANGED <<gwDone, active, setup, blocked, wrongTeardown>>

(***************************************************************************)
(* The receive loop. While `blocked`, nothing is read: no control is seen   *)
(* and no caller audio is drained.                                          *)
(***************************************************************************)

\* asyncSetup keeps draining while a call is being built; the others do not.
LoopFree == (MODE = "asyncSetup") \/ ~blocked

DispatchConfirm ==
    /\ wire # << >>
    /\ Head(wire)[1] = "conf"
    /\ LoopFree
    /\ LET c == Head(wire)[2] IN
         \* A new confirmed supersedes any running call (single active call, v0).
         /\ active' = NoCall
         /\ setup' = c
         /\ wire' = Tail(wire)
    /\ blocked' = (MODE # "asyncSetup")
    /\ UNCHANGED <<gwLive, gwDone, wrongTeardown>>

DispatchDisconnect ==
    /\ wire # << >>
    /\ Head(wire)[1] = "disc"
    /\ LoopFree
    /\ LET c == Head(wire)[2] IN
         CASE MODE = "asWritten" ->
                \* cancel_active_call() with no idea which call it is cancelling.
                /\ wrongTeardown' = (wrongTeardown \/ (active # NoCall /\ active # c))
                /\ active' = NoCall
                /\ setup' = NoCall
           [] OTHER ->
                \* Only tear down the call this event is actually about.
                /\ wrongTeardown' = wrongTeardown
                /\ active' = IF active = c THEN NoCall ELSE active
                /\ setup'  = IF setup  = c THEN NoCall ELSE setup
    /\ wire' = Tail(wire)
    /\ UNCHANGED <<gwLive, gwDone, blocked>>

\* Building the pipeline and greeting finished. In the blocking modes this is what
\* finally releases the loop.
SetupDone ==
    /\ setup # NoCall
    /\ active' = setup
    /\ setup' = NoCall
    /\ blocked' = FALSE
    /\ UNCHANGED <<wire, gwLive, gwDone, wrongTeardown>>

Next ==
    \/ \E c \in Calls : GwConfirm(c) \/ GwDisconnect(c)
    \/ DispatchConfirm \/ DispatchDisconnect \/ SetupDone

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Properties.                                                             *)
(***************************************************************************)

\* Finding #4. The caller on the torn-down call is left connected to a gateway
\* with no brain: no audio, no hangup, and no further confirmed will arrive.
NoWrongTeardown == ~wrongTeardown

\* Finding #2, stated structurally. The receive loop is the ONLY reader of the
\* socket, so while it sits inside a handler nothing is drained: not the control
\* event already waiting here, and not the caller's audio either.
\*
\* This is not a discovery the way NoWrongTeardown is -- asyncSetup satisfies it by
\* construction, because not blocking is exactly what the fix does. It is here to
\* pin the structural difference, and to show WHY the two findings compound: the
\* backlog this permits is what lets a stale `disconnected` be dispatched after a
\* newer call has already been built, which is how NoWrongTeardown then fails.
\*
\* What it deliberately does NOT capture is the magnitude. "~17s" is a timing fact
\* (greet()'s 12s poll + wait_until_delivered()'s 10s) and no amount of model
\* checking establishes it; measure that, don't prove it.
NoBlockedWithPendingControl == ~(blocked /\ wire # << >>)

TypeOK ==
    /\ active \in Calls \cup {NoCall}
    /\ setup \in Calls \cup {NoCall}
    /\ blocked \in BOOLEAN
=============================================================================
