------------------------------ MODULE OwedAnswers ------------------------------
(***************************************************************************)
(* teaport -- "owed until heard" (#203/#204), the sync answers' BOOK with  *)
(* TWO answers to two different questions. Owed.tla holds one answer and   *)
(* the whole door (the gate, the runs, the lull, the settle's timing); it   *)
(* cannot say which answer a reply tells when two are about. This module    *)
(* is that question alone, on a reduced action set: the caller asks, a     *)
(* sync tool round answers, replies are rendered and end, and owed.py's    *)
(* book (Owed.noted / Owed._resolve / Owed._cut_answer) files each answer  *)
(* as fresh, told, or a record.                                            *)
(*                                                                          *)
(* The third #204 review found the book could not keep two questions       *)
(* apart. While an answer was owed, every new round was taken for its      *)
(* re-lookup (relookAll); a reply carried every still-fresh answer and the *)
(* post-tool position was applied to all of them, so a later round's reply *)
(* -- an action's confirmation, or an unrelated answer -- became an older  *)
(* question's record or "told" it (positionAll); and a reply carrying two  *)
(* questions' answers took its request from the first (oneRequest).       *)
(*                                                                          *)
(* The design (owed.py):                                                    *)
(*  - a round asks its own question unless its words ask nothing beyond an *)
(*    owed record's question (a re-lookup: "Hold on, hold on."; Owed       *)
(*    ._question). Here every round is a new question; relookAll files a   *)
(*    round started while a record is owed as that record's re-lookup.     *)
(*  - the post-tool reply that calls nothing tells by its POSITION only the *)
(*    answers new at its render (its own round's); any other answer it     *)
(*    carries, and every answer a caller's turn carries, is told only by a *)
(*    reply that tells THAT question (owed.tells: about its request, with  *)
(*    two terms of its own). The lexical premise: `tells` reads "tell q"   *)
(*    exactly (formal/README.md, the sync telling premise, has how it errs).*)
(*  - the fresh answers a reply carries are grouped by their question.      *)
(*                                                                          *)
(* MUT (one at a time): "relookAll", "positionAll", "oneRequest".           *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxTurns,    \* bound on the caller's turns that ask nothing new
          MUT

Mut(m) == m \in MUT
Qs == {1, 2}                                  \* the two questions
ACT == 3                                      \* an action tool's round (set_volume)

VARIABLES
    asked,     \* per question: "no" | "caller" (the caller's words) | "notice" (a
               \*   notice or a cue started its round: nobody asked, no record)
    ctx,       \* per question, the LLM's context: "none" | "fresh" (its result, untold)
               \*   | "told" (a telling heard in full) | "cut" (a telling's heard prefix)
    res,       \* per question, the BOOK: "none" | "fresh" | "told" | "rec" (a record was
               \*   made of it) | "held" (filed as another record's re-lookup) | "gone"
               \*   (resolved with nobody to owe: its round was a notice's)
    rec,       \* per question: "none" | "owed" | "closed"
    own,       \* per question: the record's outcome is a telling of ITS question
    heard,     \* ghost, per question: the caller heard a telling of it in full
    tail,      \* the round whose result is at the context's tail (0: none; ACT)
    act,       \* the action round happened (once)
    pending,   \* the next request: "none" | "post" | "caller"
    turns,     \* caller turns that asked nothing new
    req,       \* the request in flight: "none" | "post" | "caller"
    pos,       \* the post-tool request's round (its position), 0 for a caller's
    carried,   \* the questions fresh in the book when the request was rendered
    news,      \* ...of which new at this render (its own round's)
    say,       \* the reply, <<kind, question>>: kind "none" | "tell" | "look" (q's) |
               \*   "defer" | "other" | "confirm" (question 0)
    relook     \* ghost: the model looked an answer up again although it had it

vars == <<asked, ctx, res, rec, own, heard, tail, act, pending, turns, req, pos, carried,
          news, say, relook>>

None == <<"none", 0>>                         \* no reply yet

Init ==
    /\ asked = [q \in Qs |-> "no"] /\ ctx = [q \in Qs |-> "none"]
    /\ res = [q \in Qs |-> "none"] /\ rec = [q \in Qs |-> "none"]
    /\ own = [q \in Qs |-> TRUE] /\ heard = [q \in Qs |-> FALSE]
    /\ tail = 0 /\ act = FALSE /\ pending = "none" /\ turns = 0
    /\ req = "none" /\ pos = 0 /\ carried = {} /\ news = {} /\ say = None
    /\ relook = FALSE

Idle == req = "none" /\ pending = "none"
Owing == \E q \in Qs : rec[q] = "owed"

\* The caller asks question q (1, then 2) and its sync tool round answers it: the result is
\* at the tail, and the post-tool request is next. The book files it at the render
\* (Owed.noted): its own question, fresh -- or, under relookAll, the owed record's.
Round(q) ==
    /\ Idle /\ asked[q] = "no" /\ (q = 2 => asked[1] # "no")
    /\ \E who \in {"caller", "notice"} : asked' = [asked EXCEPT ![q] = who]
    /\ ctx' = [ctx EXCEPT ![q] = "fresh"]
    /\ res' = [res EXCEPT ![q] = IF Mut("relookAll") /\ Owing THEN "held" ELSE "fresh"]
    /\ tail' = q /\ pending' = "post"
    /\ UNCHANGED <<rec, own, heard, act, turns, req, pos, carried, news, say, relook>>

\* An action tool's round (set_volume): its post-tool reply confirms it.
ActionRound ==
    /\ Idle /\ ~act
    /\ act' = TRUE /\ tail' = ACT /\ pending' = "post"
    /\ UNCHANGED <<asked, ctx, res, rec, own, heard, turns, req, pos, carried, news, say, relook>>

\* The caller speaks before the post-tool reply goes (it is dropped: their turn reads the
\* fresh result), or takes a turn that asks nothing new ("Hold on.", "go on", "thanks").
CallerTurn ==
    /\ req = "none" /\ pending # "caller" /\ turns < MaxTurns
    /\ pending' = "caller" /\ turns' = turns + 1
    /\ UNCHANGED <<asked, ctx, res, rec, own, heard, tail, act, req, pos, carried, news, say,
                   relook>>

\* The request is rendered: it carries every answer fresh in the book; the post-tool one's
\* position is its round, and its round's answer is new at this render.
Issue ==
    /\ req = "none" /\ pending # "none"
    /\ req' = pending /\ pending' = "none"
    /\ pos' = IF pending = "post" THEN tail ELSE 0
    /\ carried' = {q \in Qs : res[q] = "fresh"}
    /\ news' = {q \in Qs : res[q] = "fresh" /\ q = tail /\ pending = "post"}
    /\ UNCHANGED <<asked, ctx, res, rec, own, heard, tail, act, turns, say, relook>>

\* What the reply says. The post-tool reply is its round's telling (or its confirmation);
\* a caller's turn tells any answer in its context, defers, says something else, or --
\* with a telling cut and no note for it (B0) -- looks it up again.
Says ==
    IF req = "post"
      THEN (IF pos = ACT THEN {<<"confirm", 0>>} ELSE {<<"tell", pos>>})
      ELSE {<<"defer", 0>>, <<"other", 0>>} \cup {<<"tell", q>> : q \in {q2 \in Qs : ctx[q2] # "none"}}
           \cup {<<"look", q>> : q \in {q2 \in Qs : ctx[q2] = "cut" /\ rec[q2] # "owed"
                                                   /\ asked[q2] = "caller"}}
Say ==
    /\ req # "none" /\ say = None
    /\ \E s \in Says : say' = s /\ relook' = (relook \/ s[1] = "look")
    /\ UNCHANGED <<asked, ctx, res, rec, own, heard, tail, act, pending, turns, req, pos,
                   carried, news>>

Tells(s, q) == s = <<"tell", q>>

\* The book's reading: does this reply tell question q? By position for the post-tool
\* reply's own round (positionAll: for every answer it carries); else by `tells`.
TellsQ(q) == Tells(say, q) \/ (pos # 0 /\ (q \in news \/ Mut("positionAll")))

\* The question a record would quote: q's own -- oneRequest: the first carried one's.
Asker(q) == IF Mut("oneRequest") THEN asked[CHOOSE p \in carried : \A p2 \in carried : p <= p2]
            ELSE asked[q]

\* The reply ends: played through (heard in full) or cut (a prefix). Then the settle
\* (Owed._resolve) files each carried answer still fresh that it tells: heard, TOLD; cut,
\* a record (of this reply's text: `own` iff it tells that question) -- unless nobody asked.
End ==
    /\ req # "none" /\ say # None /\ say[1] # "look"
    /\ \E cut \in BOOLEAN :
         /\ heard' = [q \in Qs |-> heard[q] \/ (Tells(say, q) /\ ~cut)]
         /\ ctx' = [q \in Qs |-> IF ~Tells(say, q) THEN ctx[q]
                                 ELSE IF ~cut THEN "told"
                                 ELSE IF ctx[q] = "told" THEN "told" ELSE "cut"]
         /\ LET filed(q) == q \in carried /\ res[q] = "fresh" /\ TellsQ(q) IN
            /\ res' = [q \in Qs |-> IF ~filed(q) THEN res[q]
                                    ELSE IF ~cut THEN "told"
                                    ELSE IF Asker(q) # "caller" THEN "gone"
                                    ELSE "rec"]
            /\ rec' = [q \in Qs |-> IF filed(q) /\ cut /\ Asker(q) = "caller" THEN "owed"
                                    ELSE IF rec[q] = "owed" /\ Tells(say, q) /\ ~cut THEN "closed"
                                    ELSE rec[q]]
            /\ own' = [q \in Qs |-> IF filed(q) /\ cut /\ Asker(q) = "caller"
                                    THEN Tells(say, q) ELSE own[q]]
    /\ req' = "none" /\ say' = None /\ pos' = 0 /\ carried' = {} /\ news' = {}
    /\ tail' = 0                                \* an assistant text follows the result
    /\ UNCHANGED <<asked, act, pending, turns, relook>>

Next == \/ \E q \in Qs : Round(q)
        \/ ActionRound \/ CallerTurn \/ Issue \/ Say \/ End

Spec == Init /\ [][Next]_vars

(* ---- properties ---- *)

\* An answer the book files as told was heard in full: a telling of another question or a
\* confirmation never tells it (positionAll; oneRequest's lost answer).
ToldOnHearing == \A q \in Qs : res[q] = "told" => heard[q]

\* A record's outcome is a telling of its own question, never another round's reply.
RecordIsOwn == \A q \in Qs : rec[q] # "none" => own[q]

\* The model never looks up again what it has (a telling cut with no record: B0).
NoRedundantLookup == ~relook

TypeOK ==
    /\ \A q \in Qs : /\ asked[q] \in {"no", "caller", "notice"}
                     /\ ctx[q] \in {"none", "fresh", "told", "cut"}
                     /\ res[q] \in {"none", "fresh", "told", "rec", "held", "gone"}
                     /\ rec[q] \in {"none", "owed", "closed"}
                     /\ own[q] \in BOOLEAN /\ heard[q] \in BOOLEAN
    /\ tail \in 0..ACT /\ act \in BOOLEAN /\ pending \in {"none", "post", "caller"}
    /\ turns \in 0..MaxTurns /\ req \in {"none", "post", "caller"} /\ pos \in 0..ACT
    /\ carried \subseteq Qs /\ news \subseteq carried /\ say[2] \in 0..2
    /\ MUT \subseteq {"relookAll", "positionAll", "oneRequest"}
=============================================================================
