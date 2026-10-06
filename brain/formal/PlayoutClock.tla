---------------------------- MODULE PlayoutClock ----------------------------
(***************************************************************************)
(* teaport -- what a HELD reply's playout times do at the output transport.  *)
(*                                                                          *)
(*   brain/teaport_brain/reply_hold.py   -- ReplyHoldGate._restamp          *)
(*   pipecat/transports/base_output.py   -- MediaSender's clock queue       *)
(*   pipecat/services/tts_service.py     -- the End re-pushed with a pts    *)
(*                                                                          *)
(* LedgerPlayout.tla abstracts every number away (its "Known limits": the   *)
(* layout's seconds, heard_fraction, the pts cut), and this question is     *)
(* nothing but numbers in an order: which frame the transport's clock       *)
(* releases first. So it is its own small module rather than a constant on  *)
(* that one, whose state space is already 258k states.                      *)
(*                                                                          *)
(* The mechanism. The TTS stamps its frames with a playout time (pts) as it *)
(* synthesizes them: word i at i (a unit per word), and the response's End, *)
(* re-pushed after the context's audio, at the LAST word's pts. The output   *)
(* transport puts every frame that carries a pts on a priority queue keyed  *)
(* (pts, arrival) and releases each when the clock reaches it; the          *)
(* assistant aggregator downstream gathers the words it is handed and       *)
(* commits them to the context at the End.                                  *)
(*                                                                          *)
(* ReplyHoldGate holds the reply for `hold` units (the caller coughed over  *)
(* its start) and then lets it all through at once. The audio plays from    *)
(* the release, so every pts is `hold` early -- unless the gate moves it.    *)
(*                                                                          *)
(* SHIFT = "words" -- as first written: only TTSTextFrame pts move.         *)
(* SHIFT = "all"   -- as shipped: every frame with a pts moves, the End     *)
(*                    included (it belongs to the context last seen).       *)
(***************************************************************************)
EXTENDS Integers

CONSTANTS N,        \* words in the reply
          MaxHold,  \* the longest hold modelled
          SHIFT

ASSUME SHIFT \in {"words", "all"} /\ N >= 1

Bound == N + MaxHold + 1      \* the clock never needs to run past this

VARIABLES
    now,         \* the transport's clock
    phase,       \* "held" (in the gate's queue) | "queued" (on the clock queue) | "done"
    hold,        \* how long the reply was held: fixed at the release
    q,           \* the clock queue: a set of [kind, i, pts, seq]
    aggregated,  \* words the assistant aggregator holds, not yet committed
    committed    \* -1 until the End is released; then the words committed with it

vars == <<now, phase, hold, q, aggregated, committed>>

Init ==
    /\ now = 0 /\ phase = "held" /\ hold = 0
    /\ q = {} /\ aggregated = 0 /\ committed = -1

\* Time passes with the reply in the gate's paused queue.
Wait ==
    /\ phase = "held" /\ now < MaxHold
    /\ now' = now + 1
    /\ UNCHANGED <<phase, hold, q, aggregated, committed>>

\* The gate releases: every held frame reaches the transport in arrival order (the
\* words, then the End) with its pts moved by the wait -- or, as first written, only
\* the words' pts.
Release ==
    /\ phase = "held"
    /\ phase' = "queued" /\ hold' = now
    /\ q' = {[kind |-> "word", i |-> k, pts |-> k + now, seq |-> k] : k \in 1..N}
            \cup {[kind |-> "end", i |-> 0,
                   pts |-> N + (IF SHIFT = "all" THEN now ELSE 0), seq |-> N + 1]}
    /\ UNCHANGED <<now, aggregated, committed>>

\* The clock queue's head: the lowest (pts, seq).
Before(a, b) == a.pts < b.pts \/ (a.pts = b.pts /\ a.seq < b.seq)
First == CHOOSE x \in q : \A y \in q : x = y \/ Before(x, y)

\* MediaSender._clock_task_handler: take the head, wait for its pts, push it on.
Pop ==
    /\ phase = "queued" /\ q /= {}
    /\ First.pts <= now
    /\ q' = q \ {First}
    /\ IF First.kind = "word"
         THEN aggregated' = aggregated + 1 /\ UNCHANGED committed
         ELSE committed' = aggregated /\ aggregated' = 0
    /\ phase' = IF q' = {} THEN "done" ELSE "queued"
    /\ UNCHANGED <<now, hold>>

Tick ==
    /\ phase = "queued" /\ now < Bound
    /\ q /= {} /\ First.pts > now
    /\ now' = now + 1
    /\ UNCHANGED <<phase, hold, q, aggregated, committed>>

Next == Wait \/ Release \/ Pop \/ Tick

Spec == Init /\ [][Next]_vars

\* What the context is told the bot said is what it said: the End commits the whole
\* reply -- every word was played before the reply's End (nothing was cut here).
CommittedIsPlayed == committed /= -1 => committed = N

TypeOK ==
    /\ now \in 0..Bound /\ hold \in 0..MaxHold
    /\ phase \in {"held", "queued", "done"}
    /\ aggregated \in 0..N /\ committed \in -1..N
=============================================================================
