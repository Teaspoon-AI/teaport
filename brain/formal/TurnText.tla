--------------------------------- MODULE TurnText ---------------------------------
(***************************************************************************)
(* teaport -- a sliced turn's final, from the STT to the context: what the *)
(* STT delivers when the final is in (stt.py _push_turn / _settle_turn),   *)
(* through the processor that may hold the STT's frames above the          *)
(* aggregator (run_control.RunControlClaims, barge_pause.BargeInJudgeGate),*)
(* the user aggregator, and turn_text.TurnTextCorrector below it. Which   *)
(* words a turn's provisional and final hold is SttSlice.tla's; here they  *)
(* are any two sets, one turn.                                             *)
(*                                                                          *)
(* The STT pushes the provisional at the preview: the turn's words (F,     *)
(* stamped with its stop), or a wordless close (D) -- or nothing, the      *)
(* turn closing on its final, when the preview came back cut short or had  *)
(* none of the caller's words over the bot's reply. The wake gate or echo  *)
(* guard may have taken it ("asleep": nothing went on; "echo": the words   *)
(* were the bot's own;                                                     *)
(* "wokeLost": it woke the gate, and the final lacks the phrase). At the   *)
(* final it delivers: the deferred close on the final; nothing if they     *)
(* agree, or for the branches that leave it as it went; the final's words  *)
(* as new words (N, no stop) where nothing with words went on; else a      *)
(* correction (C) from what was pushed to the final.                       *)
(*                                                                          *)
(* The HOLDER, while a consult runs ("claim") or the reply is paused        *)
(* ("judge"), holds the first final with words and every STT frame after   *)
(* it (endpointing.STT_FRAMES), then decides once -- take it, judge it a   *)
(* backchannel, or let it go -- and the state it decided under is over.   *)
(* The reply's pause also ends on its own, its resume window out, once no  *)
(* final is on its way to the holder -- but never while a barge-in turn    *)
(* waits to close on its final (stt.py barge_hold). Not modelled: the      *)
(* pause's cap, which ends it regardless, closes such a turn on the words  *)
(* in hand first (stt.close_held) and resumes 0.15 s after, so the close   *)
(* reaches the holder paused (a timing premise, as the window's is); and   *)
(* the window is assumed to outlast the preview (0.8 s against 0.15-0.46   *)
(* s measured; a slower preview finds the pause already resumed).          *)
(* The AGGREGATOR adds a final's words as a part, commits them as a user   *)
(* message on its own task at any time, and forwards the rest. The         *)
(* CORRECTOR rewrites the part (amended) or the committed message          *)
(* (corrected); a correction placed nowhere is logged.                     *)
(*                                                                          *)
(* DESIGN = "shipped"     -- as above.                                      *)
(*          "noNewWords"  -- an empty provisional's final words dropped     *)
(*                           (fails NoLostWords).                           *)
(*          "emptyCloses" -- over the bot an empty preview closes the turn  *)
(*                           wordless, its final's words going on as new   *)
(*                           ones (fails BargeStamped: the barge-in's words *)
(*                           are no stop's, decided after the pause).      *)
(*          "echoCloses"  -- only an empty one waits: a provisional that is *)
(*                           the bot's own words heard back closes wordless *)
(*                           (fails BargeStamped, the same way; this model  *)
(*                           found it in the first fix).                    *)
(*          "overtake"    -- the holder holds the words, not the correction *)
(*                           (fails CorrectionApplied: it overtakes them,  *)
(*                           finds them nowhere, and the turn keeps the    *)
(*                           provisional words).                            *)
(*          "repush"      -- a correction placed nowhere is pushed back as  *)
(*                           new words, as before the 2026-10-27 review     *)
(*                           (fails DecidedOnce: words the holder took or  *)
(*                           judged reach the model, decided again under    *)
(*                           another state).                                *)
(*          "noHold"      -- the pause may end while a barge-in turn waits   *)
(*                           on its final, as before the re-review (fails  *)
(*                           BargePaused: a lone "Stop." then meets the     *)
(*                           unpaused guard).                               *)
(*          "byFrame"     -- commits recorded when their context frame      *)
(*                           passes the corrector (fails CorrectionApplied:*)
(*                           a commit of the aggregator's own task trails   *)
(*                           the correction it is needed for).              *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS DESIGN   \* "shipped" | "noNewWords" | "emptyCloses" | "echoCloses" | "overtake"
                   \* | "repush" | "byFrame" | "noHold"

Words == {1, 2}
Gates == {"open", "asleep", "echo", "wokeLost"}

VARIABLES
    asked,      \* the provisional's words (chosen at Init)
    final,      \* the final's words (chosen at Init)
    cut,        \* the preview came back cut short ("complete": false)
    barge,      \* the turn's commit was a stop over the bot's reply
    gate,       \* what the wake gate / echo guard made of the provisional
    holder,     \* "none" | "claim" (a consult runs) | "judge" (the reply is paused)
    phase,      \* the STT: "preview" -> "final" -> "done"
    deferred,   \* nothing went on at the preview: the turn closes on its final
    pushed,     \* the words pushed at the preview ({} for a wordless close)
    none,       \* nothing at all went on at the preview (the gate slept)
    sentF,      \* words the STT pushed stamped with the stop (F), or carried by C
    sentN,      \* words it pushed as new words (N)
    cFrom, cTo, \* the correction, if one was sent
    cSent,
    q1,         \* STT -> holder
    held,       \* the holder's held frames
    active,     \* the holder will decide on the next final with words
    fate,       \* the held final: "none" | "taken" | "judged" | "released" -- or
                \* "unpaused": a barge-in's words reached the holder after the pause
    q2,         \* holder -> aggregator
    agg,        \* the aggregator's parts (sets of words), in order
    ctx,        \* the user messages committed (sets of words), in order
    recorded,   \* the commits the corrector knows of (indexes into ctx)
    q3,         \* aggregator -> corrector
    up          \* corrector -> STT ("repush")

vars == <<asked, final, cut, barge, gate, holder, phase, deferred, pushed, none, sentF,
          sentN, cFrom, cTo, cSent, q1, held, active, fate, q2, agg, ctx, recorded, q3, up>>

Frame(k, w) == [k |-> k, w |-> w, from |-> {}]

Init ==
    /\ asked \in SUBSET Words /\ final \in SUBSET Words
    /\ cut \in BOOLEAN /\ barge \in BOOLEAN /\ gate \in Gates
    /\ holder \in {"none", "claim", "judge"}
    \* The echo guard and the wake gate act on words; a woken gate saw the provisional.
    /\ gate \in {"echo", "wokeLost"} => (asked /= {} /\ ~cut)
    /\ phase = "preview" /\ deferred = FALSE /\ pushed = {} /\ none = FALSE
    /\ sentF = {} /\ sentN = {} /\ cFrom = {} /\ cTo = {} /\ cSent = FALSE
    /\ q1 = <<>> /\ held = <<>> /\ active = (holder /= "none") /\ fate = "none"
    /\ q2 = <<>> /\ agg = <<>> /\ ctx = <<>> /\ recorded = {} /\ q3 = <<>> /\ up = <<>>

(* ------------------------------------------------------------------- the STT *)

\* A close of `w` stamped with the stop: the final, or wordless.
Close(w) == IF w /= {} THEN Frame("F", w) ELSE Frame("D", {})

\* _push_turn: the provisional goes on -- or, cut short, or over the bot with none of the
\* caller's words (none, or the bot's own heard back), nothing.
Provisional ==
    /\ phase = "preview"
    /\ phase' = "final"
    /\ LET none_callers == asked = {} \/ (gate = "echo" /\ DESIGN /= "echoCloses")
           d == cut \/ (barge /\ none_callers /\ DESIGN /= "emptyCloses")
       IN /\ deferred' = d
          /\ IF d THEN UNCHANGED <<q1, pushed, none, sentF>>
             ELSE CASE gate = "asleep" -> /\ q1' = Append(q1, Frame("D", {}))
                                          /\ none' = TRUE
                                          /\ UNCHANGED <<pushed, sentF>>
                    [] gate = "echo"   -> /\ q1' = Append(q1, Frame("D", {}))
                                          /\ UNCHANGED <<pushed, none, sentF>>
                    [] OTHER           -> /\ q1' = Append(q1, Close(asked))
                                          /\ pushed' = asked
                                          /\ sentF' = asked
                                          /\ UNCHANGED none
    /\ UNCHANGED <<asked, final, cut, barge, gate, holder, sentN, cFrom, cTo, cSent, held,
                   active, fate, q2, agg, ctx, recorded, q3, up>>

\* The final's words as the echo guard leaves them: the bot's own (the provisional's,
\* "echo") taken off.
Corrected == IF gate = "echo" THEN final \ asked ELSE final

\* _settle_turn.
Settle ==
    /\ phase = "final"
    /\ phase' = "done"
    /\ CASE deferred ->
              \* The close on the final, through the gate (asleep: it decides, wordless).
              LET w == IF gate = "asleep" THEN {} ELSE Corrected
              IN /\ q1' = Append(q1, Close(w))
                 /\ sentF' = sentF \cup w
                 /\ UNCHANGED <<sentN, cFrom, cTo, cSent>>
         [] final = asked \/ (none /\ asked /= {}) \/ gate = "wokeLost" ->
              \* As asked; or the gate slept through words, or the final lacks the
              \* phrase the provisional woke on: left as it went.
              UNCHANGED <<q1, sentF, sentN, cFrom, cTo, cSent>>
         [] pushed = {} ->
              \* Nothing with words went on: the final's words as new words, through the
              \* gate ("noNewWords": dropped).
              IF Corrected = {} \/ DESIGN = "noNewWords" \/ gate = "asleep"
              THEN UNCHANGED <<q1, sentF, sentN, cFrom, cTo, cSent>>
              ELSE /\ q1' = Append(q1, Frame("N", Corrected))
                   /\ sentN' = sentN \cup Corrected
                   /\ UNCHANGED <<sentF, cFrom, cTo, cSent>>
         [] OTHER ->
              IF Corrected = pushed
              THEN UNCHANGED <<q1, sentF, sentN, cFrom, cTo, cSent>>
              ELSE /\ q1' = Append(q1, [k |-> "C", w |-> Corrected, from |-> pushed])
                   /\ sentF' = sentF \cup Corrected
                   /\ cFrom' = pushed /\ cTo' = Corrected /\ cSent' = TRUE
                   /\ UNCHANGED sentN
    /\ UNCHANGED <<asked, final, cut, barge, gate, holder, deferred, pushed, none, held,
                   active, fate, q2, agg, ctx, recorded, q3, up>>

\* "repush": what the corrector sent back up goes down again as new words.
Repush ==
    /\ up /= <<>>
    /\ q1' = Append(q1, Frame("N", Head(up)))
    /\ up' = Tail(up)
    /\ UNCHANGED <<asked, final, cut, barge, gate, holder, phase, deferred, pushed, none,
                   sentF, sentN, cFrom, cTo, cSent, held, active, fate, q2, agg, ctx,
                   recorded, q3>>

(* ---------------------------------------------------------------- the holder *)

HolderRecv ==
    /\ q1 /= <<>>
    /\ LET f == Head(q1)
       IN IF held /= <<>>
          THEN IF DESIGN = "overtake" /\ f.k = "C"
               THEN /\ q2' = Append(q2, f) /\ UNCHANGED held
               ELSE /\ held' = Append(held, f) /\ UNCHANGED q2
          ELSE IF active /\ f.k \in {"F", "N"}
               THEN /\ held' = <<f>> /\ UNCHANGED q2
               ELSE /\ q2' = Append(q2, f) /\ UNCHANGED held
    /\ fate' = IF held = <<>> /\ ~active /\ barge /\ holder = "judge" /\ fate = "none"
                  /\ Head(q1).k \in {"F", "N"} /\ Head(q1).w /= {}
               THEN "unpaused" ELSE fate
    /\ q1' = Tail(q1)
    /\ UNCHANGED <<asked, final, cut, barge, gate, holder, phase, deferred, pushed, none,
                   sentF, sentN, cFrom, cTo, cSent, active, agg, ctx, recorded, q3, up>>

\* The reply's pause ends on its own: its window out, no final on its way to the
\* holder, and no barge-in turn waiting on its final ("noHold": regardless of that).
PauseEnds ==
    /\ holder = "judge" /\ active /\ held = <<>> /\ phase /= "preview"
    /\ \A i \in 1..Len(q1) : q1[i].k \notin {"F", "N"}
    /\ (DESIGN = "noHold" \/ ~(deferred /\ phase = "final"))
    /\ active' = FALSE
    /\ UNCHANGED <<asked, final, cut, barge, gate, holder, phase, deferred, pushed, none,
                   sentF, sentN, cFrom, cTo, cSent, q1, held, fate, q2, agg, ctx, recorded,
                   q3, up>>

\* Taken (run control) or judged a backchannel: the final goes on wordless; or let go.
\* Its frames held behind it go on as they came.
HolderDecide ==
    /\ held /= <<>>
    /\ \E out \in (IF holder = "claim" THEN {"taken", "released"}
                   ELSE {"judged", "released"}) :
          /\ fate' = out
          /\ q2' = q2 \o (IF out = "released" THEN held
                          ELSE <<Frame("D", {})>> \o Tail(held))
    /\ held' = <<>>
    /\ active' = FALSE
    /\ UNCHANGED <<asked, final, cut, barge, gate, holder, phase, deferred, pushed, none,
                   sentF, sentN, cFrom, cTo, cSent, q1, agg, ctx, recorded, q3, up>>

(* ------------------------------------------------------------ the aggregator *)

AggRecv ==
    /\ q2 /= <<>>
    /\ LET f == Head(q2)
       IN CASE f.k \in {"F", "N"} -> /\ agg' = Append(agg, f.w) /\ UNCHANGED q3
            [] f.k = "C"          -> /\ q3' = Append(q3, f) /\ UNCHANGED agg
            [] OTHER              -> UNCHANGED <<agg, q3>>
    /\ q2' = Tail(q2)
    /\ UNCHANGED <<asked, final, cut, barge, gate, holder, phase, deferred, pushed, none,
                   sentF, sentN, cFrom, cTo, cSent, q1, held, active, fate, ctx, recorded,
                   up>>

Union(s) == UNION {s[i] : i \in 1..Len(s)}

\* A commit, on the aggregator's own task: its event records it at once ("byFrame": its
\* context frame does, when it reaches the corrector).
Commit ==
    /\ agg /= <<>> /\ Len(ctx) < 2
    /\ ctx' = Append(ctx, Union(agg))
    /\ agg' = <<>>
    /\ IF DESIGN = "byFrame"
       THEN /\ q3' = Append(q3, [k |-> "CTX", w |-> {}, from |-> {}, i |-> Len(ctx) + 1])
            /\ UNCHANGED recorded
       ELSE /\ recorded' = recorded \cup {Len(ctx) + 1}
            /\ UNCHANGED q3
    /\ UNCHANGED <<asked, final, cut, barge, gate, holder, phase, deferred, pushed, none,
                   sentF, sentN, cFrom, cTo, cSent, q1, held, active, fate, q2, up>>

(* ------------------------------------------------------------- the corrector *)

\* Where the pushed words are: a part of the aggregation, or a committed message on
\* record (the turn's only words, so the message holding them is the turn's).
PartAt(w) == {i \in 1..Len(agg) : agg[i] = w}
MsgAt(w) == {i \in recorded : w \subseteq ctx[i]}
DropAt(s, i) == [j \in 1..(Len(s) - 1) |-> IF j < i THEN s[j] ELSE s[j + 1]]

CorrRecv ==
    /\ q3 /= <<>>
    /\ LET f == Head(q3)
       IN IF f.k = "CTX"
          THEN /\ recorded' = recorded \cup {f.i}
               /\ UNCHANGED <<agg, ctx, up>>
          ELSE IF f.from /= {} /\ PartAt(f.from) /= {}
          THEN LET i == CHOOSE i \in PartAt(f.from) : TRUE
               IN /\ agg' = IF f.w /= {} THEN [agg EXCEPT ![i] = f.w] ELSE DropAt(agg, i)
                  /\ UNCHANGED <<ctx, recorded, up>>
          ELSE IF f.from /= {} /\ MsgAt(f.from) /= {}
          THEN LET i == CHOOSE i \in MsgAt(f.from) : TRUE
                   new == (ctx[i] \ f.from) \cup f.w
               IN /\ ctx' = IF new /= {} THEN [ctx EXCEPT ![i] = new] ELSE ctx
                  /\ UNCHANGED <<agg, recorded, up>>
          ELSE \* Unplaced: logged ("repush": a final that adds words goes back up).
               /\ up' = IF DESIGN = "repush" /\ f.w \ f.from /= {} THEN Append(up, f.w)
                        ELSE up
               /\ UNCHANGED <<agg, ctx, recorded>>
    /\ q3' = Tail(q3)
    /\ UNCHANGED <<asked, final, cut, barge, gate, holder, phase, deferred, pushed, none,
                   sentF, sentN, cFrom, cTo, cSent, q1, held, active, fate, q2>>

Next == Provisional \/ Settle \/ Repush \/ HolderRecv \/ HolderDecide \/ PauseEnds
        \/ AggRecv \/ Commit \/ CorrRecv

Spec == Init /\ [][Next]_vars

(* --------------------------------------------------------------- properties *)

TypeOK ==
    /\ asked \subseteq Words /\ final \subseteq Words /\ cut \in BOOLEAN /\ barge \in BOOLEAN
    /\ gate \in Gates /\ holder \in {"none", "claim", "judge"}
    /\ phase \in {"preview", "final", "done"} /\ deferred \in BOOLEAN /\ none \in BOOLEAN
    /\ pushed \subseteq Words /\ sentF \subseteq Words /\ sentN \subseteq Words
    /\ cFrom \subseteq Words /\ cTo \subseteq Words /\ cSent \in BOOLEAN /\ active \in BOOLEAN
    /\ fate \in {"none", "taken", "judged", "released", "unpaused"}
    /\ recorded \subseteq 1..2 /\ Len(ctx) <= 2

\* A barge-in's words are decided with the reply paused: they reach the judging holder
\* while the pause holds.
BargePaused == fate /= "unpaused"

\* The final's words a branch leaves as they went, by design: the gate slept through
\* them, they were the bot's own, or the final lacks the phrase that woke the gate.
Excused == CASE gate = "asleep"   -> final
             [] gate = "echo"     -> asked
             [] gate = "wokeLost" -> final \ pushed
             [] OTHER             -> {}

\* Every word of the final the branches do not excuse went downstream: in the
\* provisional, the close on the final, the correction, or as new words.
NoLostWords == phase = "done" => final \ Excused \subseteq sentF \cup sentN

\* A barge-in's words go downstream stamped with its stop -- in its close or its
\* correction -- never as new words decided after the pause.
BargeStamped == (phase = "done" /\ barge) => final \ Excused \subseteq sentF

Quiet == phase = "done" /\ q1 = <<>> /\ held = <<>> /\ q2 = <<>> /\ q3 = <<>> /\ up = <<>>
Placed == Union(agg) \cup Union(ctx)

\* Words the holder took or judged a backchannel never reach the model.
DecidedOnce == fate \in {"taken", "judged"} => agg = <<>> /\ ctx = <<>>

\* A correction sent for words that went on is applied: once all is quiet, the turn's
\* words downstream are the final's (one with no words never empties a message).
CorrectionApplied ==
    (Quiet /\ cSent /\ fate \notin {"taken", "judged"} /\ Placed /= {})
        => (Placed = cTo \/ (cTo = {} /\ Placed \subseteq cFrom))

\* The turn's words are in one place: one part or one message.
NoDuplicate == Len(agg) + Len(ctx) <= 1

=============================================================================
