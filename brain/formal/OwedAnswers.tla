------------------------------ MODULE OwedAnswers ------------------------------
(***************************************************************************)
(* teaport -- "owed until heard" (#203/#204): how a SYNC tool's answer     *)
(* becomes owed. Owed.tla is a record's life (the note, the evidence, the  *)
(* runs and the door) for a consult; a sync answer tool's result           *)
(* (web_search, web_fetch: tools.Tool.answers) joins it as a record when a *)
(* telling of it is cut before the caller heard it. This module is that    *)
(* filing alone -- owed.py's Owed.noted / Owed._resolve / Owed._cut_answer *)
(* -- with two questions, an action tool's round, chains, the late settle  *)
(* and the stream cut, on a reduced action set (no gate, no runs).         *)
(*                                                                          *)
(* The rule (the fourth #204 review's cut, replacing four rounds of        *)
(* lexical rules):                                                          *)
(*  - POSITION. A result's first reply -- the first rendered with it, of   *)
(*    whatever kind -- settles it if it calls nothing and plays audio:     *)
(*    heard in full, told; cut after its stream ended, a record (unless    *)
(*    its heard prefix already carries the answer). A reply that did not   *)
(*    settle it (no audio, a cut mid-stream, a line before a call) leaves  *)
(*    it ONE more reply, then it is history, as before #204; a new round   *)
(*    (another question, an action) ends it at once.                       *)
(*  - A round is an owed record's RE-LOOKUP only when it starts in the     *)
(*    caller's turn right after that record's cut, before any reply played *)
(*    through, and the caller's words carry no terms of their own ("Hold   *)
(*    on, hold on." and web_fetch, 2026-10-18 17:26). Every other round is *)
(*    its own question -- "Sure, go ahead.", "When do they close?" asked   *)
(*    while a record is owed included.                                      *)
(*                                                                          *)
(* LOSE: the caller moves on before an answer's position settled it -- a   *)
(*   deferral or another question's answer heard in its place, both its    *)
(*   replies unheard, a new round started over it, vague words right after  *)
(*   a cut that ask something new. The design then degrades to before #204 *)
(*   (oa_lose_told: a heard deferral counts as told); the design rows      *)
(*   assume it away and check what the rule guarantees, oa_lose what it    *)
(*   keeps anyway.                                                          *)
(* STREAMCUT: a telling is cut before its stream ended (a fragment, no     *)
(*   record: the next reply has the answer, and the B0 shape).              *)
(* LATE: the readers' bounded settle wait runs out before a cut telling's  *)
(*   settle (Owed.settled); the settle re-reads the live book.              *)
(*                                                                          *)
(* MUT, one at a time -- each the code before a fix:                        *)
(*   noCutRecord    -- no record from a cut answer (before #204's sync      *)
(*                     extension).                                          *)
(*   postOnly       -- only the post-tool request carries the answer (the   *)
(*                     frame's direction): the caller's turn that dropped   *)
(*                     it reads the result, tells it, is cut: no record.    *)
(*   noSecondChance -- the first reply only: a post-tool reply cut before  *)
(*                     its audio leaves the telling after it no record.     *)
(*   partialRecord  -- a record from a telling cut mid-stream: a fragment.  *)
(*   callRecords    -- a reply that calls again settles: its line ("Let me  *)
(*                     get their hours.") cut is a record.                  *)
(*   actionOwed     -- an action's confirmation is a record too.            *)
(*   keepOpen       -- a new round leaves an older answer open: the newer   *)
(*                     round's reply files the older question.             *)
(*   relookAll      -- every round started while a record is owed is its    *)
(*                     re-lookup.                                           *)
(*   vagueConsent   -- a round whose words carry no terms is a re-lookup    *)
(*                     whenever a record is owed (9b602f8's _question: the *)
(*                     fourth review's 12-step trace).                      *)
(*   lateReplace    -- the late settle makes its record whatever the book   *)
(*                     holds (after a full telling).                       *)
(*   lateNoEvidence -- a reply rendered with the open answer is no evidence *)
(*                     for the record made of it since.                    *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS MaxTurns, MUT, LOSE, LATE, STREAMCUT

Mut(m) == m \in MUT
Qs  == {1, 2}                                  \* the two questions
ACT == 3                                       \* an action tool's round (set_volume)

VARIABLES
    asked,     \* per question: asked yet
    ctx,       \* per question, the LLM's context: "none" | "fresh" (its result, untold)
               \*   | "told" (a telling heard in full) | "cut" (a telling's heard prefix)
    res,       \* per question, the BOOK: "none" | "open" | "told" | "rec" (a record) |
               \*   "held" (filed as a record's re-lookup) | "past" (history)
    ch,        \* per question: replies left to settle it
    rec,       \* per question: "none" | "owed" | "closed"
    own,       \* per question: its record's outcome is its own telling
    heard,     \* ghost, per question: the caller heard a telling of it in full
    tail,      \* the round whose post-tool reply is next (0 none, ACT)
    act,       \* the action round happened (once)
    chain,     \* a line before a call put a chain round out (once)
    pending,   \* the next request: "none" | "post" | "caller"
    turns,     \* caller turns that started no round
    win,       \* the record whose re-lookup window is open (its cut's next turn), 0 none
    req,       \* the request in flight: "none" | "post" | "caller"
    pos,       \* the post-tool request's round, 0 for a caller's
    carried,   \* the questions open in the book at the render
    noted,     \* the questions with a record owed at the render (the note carries them)
    say,       \* the reply, <<kind, q>>: "none" | "tell" | "look" | "mute" (no audio) |
               \*   "line" (a line before another call) | "defer" | "other" | "confirm"
    lq,        \* LATE: the question whose cut telling's settle is still to come, 0 none
    lown,      \* ...and whether that telling was its own
    gone,      \* LATE: the readers' wait for it ran out
    relook,    \* ghost: an answer looked up again although the model had it (B0)
    cross,     \* ghost: a newer round's post-tool reply filed an older question
    actRec     \* ghost: an action's confirmation made a record

vars == <<asked, ctx, res, ch, rec, own, heard, tail, act, chain, pending, turns, win,
          req, pos, carried, noted, say, lq, lown, gone, relook, cross, actRec>>

None == <<"none", 0>>

Init ==
    /\ asked = [q \in Qs |-> FALSE] /\ ctx = [q \in Qs |-> "none"]
    /\ res = [q \in Qs |-> "none"] /\ ch = [q \in Qs |-> 0]
    /\ rec = [q \in Qs |-> "none"] /\ own = [q \in Qs |-> TRUE]
    /\ heard = [q \in Qs |-> FALSE]
    /\ tail = 0 /\ act = FALSE /\ chain = 0 /\ pending = "none" /\ turns = 0 /\ win = 0
    /\ req = "none" /\ pos = 0 /\ carried = {} /\ noted = {} /\ say = None
    /\ lq = 0 /\ lown = TRUE /\ gone = FALSE
    /\ relook = FALSE /\ cross = FALSE /\ actRec = FALSE

Open  == {q \in Qs : res[q] = "open"}
Owing == \E q \in Qs : rec[q] = "owed"

\* A new round: the reply that put its call out, and its result filed. An older answer
\* still open is ended (keepOpen: it stays open, one reply spent).
Older == [q \in Qs |-> IF res[q] # "open" THEN res[q]
                       ELSE IF ~Mut("keepOpen") \/ ch[q] = 1 THEN "past" ELSE "open"]
OlderCh == [q \in Qs |-> IF res[q] = "open" /\ Mut("keepOpen") THEN ch[q] - 1 ELSE ch[q]]

\* The caller asks question q (1, then 2) -- in a turn of its own, or the turn already
\* waiting (a dropped post-tool request's: its reply is the one that calls) -- and its
\* sync tool round answers it. Its words
\* may carry no terms ("Sure, go ahead." to an offer): the round is still its own unless it
\* starts in the turn right after a record's cut (win). LOSE alone lets such vague words
\* start a NEW question there, or a round start over an open answer.
Round(q) ==
    /\ req = "none" /\ pending \in {"none", "caller"} /\ ~asked[q] /\ (q = 2 => asked[1])
    /\ LOSE \/ Open = {}
    /\ \E vague \in BOOLEAN :
         /\ (vague /\ win # 0) => LOSE
         /\ LET held == \/ Mut("relookAll") /\ Owing
                        \/ Mut("vagueConsent") /\ vague /\ Owing
                        \/ vague /\ win # 0 IN
            /\ res' = [Older EXCEPT ![q] = IF held THEN "held" ELSE "open"]
            /\ ch' = [OlderCh EXCEPT ![q] = IF Mut("noSecondChance") THEN 1 ELSE 2]
    /\ asked' = [asked EXCEPT ![q] = TRUE]
    /\ ctx' = [ctx EXCEPT ![q] = "fresh"]
    /\ tail' = q /\ pending' = "post" /\ win' = 0
    /\ UNCHANGED <<rec, own, heard, act, chain, turns, req, pos, carried, noted, say, lq, lown,
                   gone, relook, cross, actRec>>

\* An action tool's round (set_volume): its post-tool reply confirms it.
ActionRound ==
    /\ req = "none" /\ pending \in {"none", "caller"} /\ ~act /\ (LOSE \/ Open = {})
    /\ act' = TRUE /\ res' = Older /\ ch' = OlderCh
    /\ tail' = ACT /\ pending' = "post" /\ win' = 0
    /\ UNCHANGED <<asked, ctx, rec, own, heard, chain, turns, req, pos, carried, noted, say,
                   lq, lown, gone, relook, cross, actRec>>

\* The caller takes a turn that starts no round ("Hold on.", "go on", "thanks"); before the
\* post-tool reply went, it drops it, and the turn reads the fresh result.
CallerTurn ==
    /\ req = "none" /\ pending # "caller" /\ turns < MaxTurns
    /\ pending' = "caller" /\ turns' = turns + 1 /\ win' = 0
    /\ UNCHANGED <<asked, ctx, res, ch, rec, own, heard, tail, act, chain, req, pos, carried,
                   noted, say, lq, lown, gone, relook, cross, actRec>>

\* The request is rendered: it carries every open answer (postOnly: only the post-tool
\* request does), and the note for every owed record. The readers wait for a pending
\* settle (LATE: unless that wait ran out).
Issue ==
    /\ req = "none" /\ pending # "none" /\ (lq = 0 \/ gone)
    /\ req' = pending /\ pending' = "none"
    /\ pos' = IF pending = "post" THEN tail ELSE 0
    /\ carried' = IF Mut("postOnly") /\ pending # "post" THEN {} ELSE Open
    /\ noted' = {q \in Qs : rec[q] = "owed"}
    /\ UNCHANGED <<asked, ctx, res, ch, rec, own, heard, tail, act, chain, turns, win, say, lq,
                   lown, gone, relook, cross, actRec>>

\* What the reply says. The post-tool reply tells its round's answer, or says nothing a
\* caller hears, or puts a line before another call of its chain. A caller's turn: anything
\* -- but without LOSE, a reply rendered with an open answer tells it (the position
\* premise), unheard at most once. With a telling cut and no note for it (B0), the model
\* may look it up again.
Look == {<<"look", q>> : q \in {q2 \in Qs : ctx[q2] = "cut" /\ rec[q2] # "owed"}}
\* The answer's first reply: the one that may go unheard (or put a line before a call).
First == \A q \in carried : ch[q] = (IF Mut("noSecondChance") THEN 1 ELSE 2)
Says ==
    IF req = "post"
      THEN IF pos = ACT THEN {<<"confirm", 0>>, <<"mute", 0>>}
           ELSE {<<"tell", pos>>}
                \cup (IF LOSE \/ First THEN {<<"mute", 0>>} \cup
                         (IF chain = 0 THEN {<<"line", pos>>} ELSE {}) ELSE {})
      ELSE IF carried # {} /\ ~LOSE
             THEN {<<"tell", q>> : q \in carried} \cup (IF First THEN {<<"mute", 0>>} ELSE {})
           ELSE {<<"defer", 0>>, <<"other", 0>>, <<"mute", 0>>} \cup Look
                \cup {<<"tell", q>> : q \in {q2 \in Qs : ctx[q2] # "none"}}
Say ==
    /\ req # "none" /\ say = None
    /\ \E s \in Says : say' = s /\ relook' = (relook \/ s[1] = "look")
    /\ UNCHANGED <<asked, ctx, res, ch, rec, own, heard, tail, act, chain, pending, turns, win,
                   req, pos, carried, noted, lq, lown, gone, cross, actRec>>

\* The reply ends: played through (heard in full) or cut (a prefix). Then its settle
\* (Owed._resolve) files every answer it carried that is still open in the LIVE book: a
\* reply that calls nothing and played audio settles it -- heard, told; cut after its
\* stream ended, a record (made at once, or LATE at a settle still to come); any other
\* reply spends one of its replies. A heard telling closes an owed record its request
\* noted, or one made of an answer it carried since (the late settle).
End ==
    /\ req # "none" /\ say # None /\ say[1] # "look"
    /\ \E cut \in BOOLEAN, sd \in (IF STREAMCUT THEN BOOLEAN ELSE {TRUE}),
          late \in (IF LATE /\ lq = 0 THEN BOOLEAN ELSE {FALSE}) :
       LET k == say[1]
           Tq(q) == say = <<"tell", q>>
           audio == k # "mute"
           settles == audio /\ (k # "line" \/ Mut("callRecords"))
                      /\ (~cut \/ sd \/ Mut("partialRecord"))
           filed(q) == q \in carried /\ res[q] = "open"
           makes(q) == filed(q) /\ settles /\ cut /\ ~Mut("noCutRecord")
           ownq(q) == Tq(q) /\ sd
           mk == {q \in Qs : makes(q)}
           lateq == late /\ Cardinality(mk) = 1
           now(q) == makes(q) /\ ~lateq
           ev(q) == q \in noted \/ (q \in carried /\ ~Mut("lateNoEvidence"))
       IN
       /\ heard' = [q \in Qs |-> heard[q] \/ (Tq(q) /\ ~cut)]
       /\ ctx' = [q \in Qs |-> IF ~Tq(q) THEN ctx[q] ELSE IF ~cut THEN "told"
                               ELSE IF ctx[q] = "told" THEN "told" ELSE "cut"]
       /\ res' = [q \in Qs |->
                    IF k = "line" /\ ~cut /\ q = pos /\ req = "post" /\ res[q] # "held"
                      THEN "open"                       \* its chain's next result
                    ELSE IF ~filed(q) THEN res[q]
                    ELSE IF settles /\ ~cut THEN "told"
                    ELSE IF settles THEN (IF makes(q) /\ lateq THEN "open"
                                          ELSE IF makes(q) THEN "rec" ELSE "past")
                    ELSE IF ch[q] > 1 THEN "open" ELSE "past"]
       /\ ch' = [q \in Qs |->
                    IF k = "line" /\ ~cut /\ q = pos /\ req = "post" THEN 2
                    ELSE IF filed(q) /\ ~settles THEN ch[q] - 1 ELSE ch[q]]
       /\ rec' = [q \in Qs |-> IF now(q) THEN "owed"
                               ELSE IF rec[q] = "owed" /\ Tq(q) /\ ~cut /\ ev(q) THEN "closed"
                               ELSE rec[q]]
       /\ own' = [q \in Qs |-> IF now(q) THEN ownq(q) ELSE own[q]]
       /\ lq' = IF lateq THEN CHOOSE q \in mk : TRUE ELSE lq
       /\ lown' = IF lateq THEN ownq(CHOOSE q \in mk : TRUE) ELSE lown
       /\ win' = IF \E q \in Qs : now(q) THEN CHOOSE q \in Qs : now(q)
                 ELSE IF audio /\ ~cut THEN 0 ELSE win
       /\ cross' = (cross \/ \E q \in Qs : filed(q) /\ settles /\ pos \notin {0, q})
       /\ actRec' = (actRec \/ (pos = ACT /\ cut /\ audio /\ Mut("actionOwed")))
       /\ pending' = IF k = "line" /\ ~cut /\ req = "post" THEN "post" ELSE pending
       /\ chain' = IF k = "line" /\ ~cut /\ req = "post" THEN 1 ELSE chain
       /\ tail' = IF k = "line" /\ ~cut /\ req = "post" THEN tail ELSE 0
    /\ req' = "none" /\ say' = None /\ pos' = 0 /\ carried' = {} /\ noted' = {}
    /\ UNCHANGED <<asked, act, turns, gone, relook>>

\* The cut telling's late settle re-reads the live book: an answer told meanwhile, or
\* recorded by another reply's cut, makes no record (the prefix, short of the answer, is
\* only evidence); lateReplace makes it whatever the book holds.
LateSettle ==
    /\ lq # 0
    /\ LET q == lq
           mk == Mut("lateReplace") \/ res[q] = "open" IN
       /\ res' = IF mk THEN [res EXCEPT ![q] = "rec"] ELSE res
       /\ rec' = IF mk THEN [rec EXCEPT ![q] = "owed"] ELSE rec
       /\ own' = IF mk THEN [own EXCEPT ![q] = lown] ELSE own
       /\ win' = IF mk THEN q ELSE win
    /\ lq' = 0 /\ gone' = FALSE
    /\ UNCHANGED <<asked, ctx, ch, heard, tail, act, chain, pending, turns, req, pos, carried,
                   noted, say, lown, relook, cross, actRec>>

\* SETTLE_WAIT_S passes with that settle still to come: the readers go on (a timer).
GiveUp ==
    /\ LATE /\ lq # 0 /\ ~gone
    /\ gone' = TRUE
    /\ UNCHANGED <<asked, ctx, res, ch, rec, own, heard, tail, act, chain, pending, turns, win,
                   req, pos, carried, noted, say, lq, lown, relook, cross, actRec>>

Next == \/ \E q \in Qs : Round(q)
        \/ ActionRound \/ CallerTurn \/ Issue \/ Say \/ End \/ LateSettle \/ GiveUp

Spec == Init /\ [][Next]_vars

(* ---- properties ---- *)

\* An answer filed as told was heard in full.
ToldOnHearing == \A q \in Qs : res[q] = "told" => heard[q]

\* A record's outcome is a whole telling of its own question.
RecordIsOwn == \A q \in Qs : rec[q] # "none" => own[q]

\* The model never looks an answer up again that it had (a cut telling with no record: B0).
NoRedundantLookup == ~relook

\* A newer round's post-tool reply never files an older question.
NoLaterRoundFiling == ~cross

\* No record is owed for an answer the caller already heard (a record made after it was).
OwedUnheard == \A q \in Qs : rec[q] = "owed" => ~heard[q]

\* An action's confirmation is never owed.
NoConfirmationOwed == ~actRec

TypeOK ==
    /\ \A q \in Qs : /\ asked[q] \in BOOLEAN
                     /\ ctx[q] \in {"none", "fresh", "told", "cut"}
                     /\ res[q] \in {"none", "open", "told", "rec", "held", "past"}
                     /\ ch[q] \in 0..2 /\ rec[q] \in {"none", "owed", "closed"}
                     /\ own[q] \in BOOLEAN /\ heard[q] \in BOOLEAN
    /\ tail \in 0..ACT /\ act \in BOOLEAN /\ chain \in 0..1
    /\ pending \in {"none", "post", "caller"} /\ turns \in 0..MaxTurns /\ win \in 0..2
    /\ req \in {"none", "post", "caller"} /\ pos \in 0..ACT
    /\ carried \subseteq Qs /\ noted \subseteq Qs /\ say[2] \in 0..2
    /\ lq \in 0..2 /\ lown \in BOOLEAN /\ gone \in BOOLEAN /\ (gone => lq # 0)
    /\ MUT \subseteq {"noCutRecord", "postOnly", "noSecondChance", "partialRecord",
                      "callRecords", "actionOwed", "keepOpen", "relookAll", "vagueConsent",
                      "lateReplace", "lateNoEvidence"}
=============================================================================
