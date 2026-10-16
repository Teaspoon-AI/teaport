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
(* The context opens (TTSStartedFrame) LAT units before its first audio,   *)
(* and the TTS anchors the words at that first audio: word k at LAT + k.     *)
(* The correct move is the first AUDIO's wait, release - LAT.               *)
(*                                                                          *)
(* SHIFT = "words"      -- as first written: only TTSTextFrame pts move, by  *)
(*                         the longest wait (the context's open).           *)
(* SHIFT = "firstFrame" -- every pts moves, by the context's first held      *)
(*                         frame's wait -- the TTSStartedFrame: LAT too far. *)
(* SHIFT = "all"        -- as shipped: every pts moves, by the wait of the   *)
(*                         context's first held audio or word.              *)
(*                                                                          *)
(* SECOND: a second TTS context (a filler, a tool's answer) whose first     *)
(* audio comes at a2, during the hold or after it, while the held reply     *)
(* still plays.                                                             *)
(* DESIGN2 = "asWritten" -- the code until 2026-10-15: the TTS anchored it  *)
(*   at the held reply's end on ITS model (_prev_audio_end_ns, moved at the *)
(*   release by the whole hold, `now - then`) or at a2 if later, and the    *)
(*   gate moved its pts by ITS OWN first held frame's wait.                 *)
(* DESIGN2 = "shipped" -- a context that starts while the freeze holds      *)
(*   anchored audio back is anchored on the frozen schedule (the held       *)
(*   reply's end, or the instant the freeze began holding audio back --     *)
(*   here the held reply's first audio, LAT -- never the real clock); an    *)
(*   ended context's anchor, and the second context's own end, move by the  *)
(*   hold since that instant; every context held behind the first takes the *)
(*   first's shift.                                                         *)
(* DESIGN2 = "realClock" -- the same, but anchored on the real clock: a2 if *)
(*   later than the held reply's end (why the TTS pins the clock).          *)
(***************************************************************************)
EXTENDS Integers

CONSTANTS N,        \* words in the reply
          MaxHold,  \* the longest hold modelled
          LAT,      \* the context's open to its first audio
          SHIFT,
          SECOND,   \* BOOLEAN: a second context queued behind the held reply
          DESIGN2   \* its anchoring (see the header)

ASSUME SHIFT \in {"words", "firstFrame", "all"} /\ N >= 1 /\ LAT <= MaxHold
ASSUME SECOND \in BOOLEAN /\ DESIGN2 \in {"asWritten", "realClock", "shipped"}
       /\ (SECOND => SHIFT = "all")

Bound == LAT + N + MaxHold + 1      \* the clock never needs to run past this

VARIABLES
    now,         \* the transport's clock
    phase,       \* "held" (in the gate's queue) | "queued" (on the clock queue) | "done"
    hold,        \* the release time (the context opened at 0): fixed at the release
    wordShift,   \* how far the release moved the words' pts
    q,           \* the clock queue: a set of [kind, i, pts, seq]
    aggregated,  \* words the assistant aggregator holds, not yet committed
    committed,   \* -1 until the End is released; then the words committed with it
    a2,          \* -1, or when the second context's first audio reached the gate
    a2Held       \* it arrived while the reply was held

vars == <<now, phase, hold, wordShift, q, aggregated, committed, a2, a2Held>>

Init ==
    /\ now = 0 /\ phase = "held" /\ hold = 0 /\ wordShift = 0
    /\ q = {} /\ aggregated = 0 /\ committed = -1 /\ a2 = -1 /\ a2Held = FALSE

\* Time passes with the reply in the gate's paused queue.
Wait ==
    /\ phase = "held" /\ now < MaxHold
    /\ now' = now + 1
    /\ UNCHANGED <<phase, hold, wordShift, q, aggregated, committed, a2, a2Held>>

\* The gate releases: every held frame reaches the transport in arrival order (the
\* words, then the End) with its pts moved by the wait -- or, as first written, only
\* the words' pts.
Release ==
    /\ phase = "held" /\ now >= LAT              \* the first audio is in the queue
    /\ phase' = "queued" /\ hold' = now
    /\ LET ws == IF SHIFT = "all" THEN now - LAT ELSE now   \* words' move
            es == CASE SHIFT = "words" -> 0
                    [] SHIFT = "firstFrame" -> now
                    [] OTHER -> now - LAT                    \* the End's move
        IN /\ wordShift' = ws
           /\ q' = {[kind |-> "word", i |-> k, pts |-> LAT + k + ws, seq |-> k] : k \in 1..N}
                  \cup {[kind |-> "end", i |-> 0, pts |-> LAT + N + es, seq |-> N + 1]}
    /\ UNCHANGED <<now, aggregated, committed, a2, a2Held>>

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
    /\ UNCHANGED <<now, hold, wordShift, a2, a2Held>>

Tick ==
    /\ phase = "queued" /\ now < Bound
    /\ q /= {} /\ First.pts > now
    /\ now' = now + 1
    /\ UNCHANGED <<phase, hold, wordShift, q, aggregated, committed, a2, a2Held>>

\* The second context's first audio reaches the gate (its TTS context started once the
\* held reply's had ended, so not before LAT); the clock runs on after the reply.
Arrive2 ==
    /\ SECOND /\ a2 = -1 /\ now >= LAT
    /\ a2' = now /\ a2Held' = (phase = "held")
    /\ UNCHANGED <<now, phase, hold, wordShift, q, aggregated, committed>>
Idle ==
    /\ SECOND /\ a2 = -1 /\ phase = "done" /\ now < Bound
    /\ now' = now + 1
    /\ UNCHANGED <<phase, hold, wordShift, q, aggregated, committed, a2, a2Held>>

Next == Wait \/ Release \/ Pop \/ Tick \/ Arrive2 \/ Idle

Spec == Init /\ [][Next]_vars

\* What the context is told the bot said is what it said: the End commits the whole
\* reply -- every word was played before the reply's End (nothing was cut here).
CommittedIsPlayed == committed /= -1 => committed = N

\* Each word is released when its audio plays: the audio plays from the release, so
\* word k at release + k, and its pts must say so (the captions, and the ledger's
\* heard cut, read it).
WordsOnTime == phase /= "held" => LAT + 1 + wordShift = hold + 1

\* The second context's first word: where the TTS anchored it, moved by the gate, against
\* where it plays -- right behind the held reply, which plays from the release.
Max(a, b) == IF a > b THEN a ELSE b
AsWritten == DESIGN2 = "asWritten"
Prev2  == IF a2Held THEN LAT + N                                 \* the held reply's end, pre-hold
          ELSE LAT + N + (IF AsWritten THEN hold ELSE hold - LAT) \* moved at the release
Base2  == IF a2Held /\ DESIGN2 = "shipped" THEN Max(Prev2, LAT)  \* the frozen schedule
          ELSE Max(Prev2, a2)                                    \* the real clock
Shift2 == IF ~a2Held THEN 0 ELSE IF AsWritten THEN hold - a2 ELSE hold - LAT
True2  == IF hold + N > a2 THEN hold + N ELSE a2
SecondOnTime == (a2 >= 0 /\ phase /= "held") => Base2 + Shift2 = True2

\* Where the TTS puts the second context's END (less its own audio): its anchor plus
\* the hold it adds at the release (_ctx_held_ns) -- what a third context queued
\* behind it is anchored at. As written: the hold since max(then, its anchor), then = 0.
Held2 == IF ~a2Held THEN 0 ELSE IF AsWritten THEN Max(0, hold - Base2) ELSE hold - LAT
SecondEndOnTime == (a2 >= 0 /\ phase /= "held") => Base2 + Held2 = True2

TypeOK ==
    /\ now \in 0..Bound /\ hold \in 0..MaxHold /\ wordShift \in 0..MaxHold
    /\ phase \in {"held", "queued", "done"}
    /\ aggregated \in 0..N /\ committed \in -1..N /\ a2 \in -1..Bound
=============================================================================
