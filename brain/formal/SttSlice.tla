--------------------------------- MODULE SttSlice ---------------------------------
(***************************************************************************)
(* teaport -- sliced turn text: a turn is the words of ONE continuous      *)
(* engine transcript that end before its commit (stt.py TURN TEXT,         *)
(* TEAPORT_STT_TURN_TEXT=slice; the engine's preview stream, PROTOCOL3),   *)
(* and the preview asked at the VAD stop (THE PEEK,                        *)
(* TEAPORT_STT_PREVIEW_AT_STOP).                                           *)
(*                                                                          *)
(*   brain/teaport_brain/stt.py -- _take_turn / _turn_at (a turn's close),  *)
(*                                 _peek_at_stop, _drop_peek, _adopt_peek,  *)
(*                                 _slice_delta (a main delta),             *)
(*                                 _handle_preview, _finalize_turns /       *)
(*                                 _settle_turn (end_through_ms)            *)
(*                                                                          *)
(* The ENGINE decodes one stream. Its words end at positions E[1] < E[2]   *)
(* < ... on the session clock (the audio sent); it sends each one only     *)
(* once its decoder is past it (dec), and may hold it longer (a word is    *)
(* sent whole, PROTOCOL3 section 2). Its progress report carries dec and   *)
(* the bound end_through_ms -- no delta sent after it ends below it --     *)
(* which it keeps by construction. It answers every commit with one        *)
(* preview, in order, and the model does NOT assume what PROTOCOL3 also    *)
(* says, that no word ending after a commit is sent before its preview:    *)
(* the brain must not need it. One FIFO carries all of it to the brain.    *)
(*                                                                          *)
(* The BRAIN closes turns (Close: a verdict, the ceiling, a stop over the  *)
(* bot -- when is SttCommit.tla's business) and, with PEEKS, peeks at a    *)
(* VAD stop (Stop) with nothing in flight: a commit that closes nothing.   *)
(* The caller resuming (Speak) drops the peek. A Close with the stop's     *)
(* peek out adopts it: the turn closes at the peek's position, on its      *)
(* preview, with no second commit. A turn takes the open words that end    *)
(* before its position; a delta goes to the oldest turn not yet final that *)
(* it ends before, else to the open turn (shown as an interim); a preview  *)
(* pushes its turn's provisional words, or holds a peek's; a report makes  *)
(* every turn final, oldest first, whose preview has come and whose        *)
(* position the bound has passed, and delivers what the final adds.        *)
(*                                                                          *)
(* DESIGN = "shipped"    -- as above.                                       *)
(*          "arrival"    -- a turn is the words RECEIVED by its preview;    *)
(*                          later ones go to the next turn (fails           *)
(*                          RightTurn: a commit's last word, sent after the *)
(*                          commit, lands in the next turn).                *)
(*          "interimAll" -- the shipped slicing, but every delta announced  *)
(*                          as an interim (fails NoLateInterim: a committed *)
(*                          turn's own late word would start a turn and cut *)
(*                          the reply to it).                               *)
(*          "decoded"    -- final once decoded_through_ms passes the commit *)
(*                          (fails FinalComplete: a held word is still to   *)
(*                          come).                                          *)
(*          "noNewWords" -- an empty provisional's final is not delivered   *)
(*                          (fails NoLostWords).                            *)
(*          "peekAsTurn" -- the peek closes the turn at the stop, as a      *)
(*                          commit at the stop would (fails                 *)
(*                          NoSplitOnResume: the caller resumes, and their  *)
(*                          sentence is two turns).                         *)
(*                                                                          *)
(* Every queue-appending action is bounded: Len(chan) < QLen, commits <=    *)
(* MaxCommits, pos <= MaxPos, stops <= MaxStops.                            *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS MaxPos,      \* the audio the client sends, in steps
          NWords,      \* the caller's words
          MaxCommits,  \* the brain's commits, peeks included
          MaxStops,    \* the caller's VAD stops
          QLen,        \* the engine -> brain channel's bound
          PEEKS,       \* the preview is asked at the VAD stop (THE PEEK)
          DESIGN       \* "shipped" | "arrival" | "interimAll" | "decoded" | "noNewWords"
                       \* | "peekAsTurn"

Words == 1..NWords
Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b
Ends == {f \in [Words -> 0..(MaxPos - 1)] : \A i \in Words : i > 1 => f[i - 1] < f[i]}

VARIABLES
    E,           \* where each word ends (chosen at Init, strictly increasing)
    pos,         \* the audio sent: the session clock
    dec,         \* the engine's decoded_through_ms
    emitted,     \* the engine has sent words 1..emitted
    bound,       \* the engine's end_through_ms
    answered,    \* commits the engine has answered with a preview
    chan,        \* engine -> brain, in order
    commits,     \* the brain's commits, in order: a turn's, or a peek
    turns,       \* the brain's closed turns, oldest first
    open,        \* words received that no turn has taken
    endThrough,  \* the brain's latest bound (or decoded position, "decoded")
    speaking,    \* the caller is speaking (the VAD's state)
    stops,       \* VAD stops so far
    peekIdx,     \* the stop's peek out (an index into commits), or 0
    atStop,      \* a turn closed at the latest stop, no verdict since ("peekAsTurn"), or 0
    split,       \* the caller talked past a stop a turn had been closed at
    lateInterim  \* a closed turn's word was announced as an interim

engine == <<E, pos, dec, emitted, bound, answered>>
brain == <<commits, turns, open, endThrough, speaking, stops, peekIdx, atStop, split,
           lateInterim>>
vars == <<engine, chan, brain>>

Turn == [at: 0..MaxPos, words: SUBSET Words, previewed: BOOLEAN, asked: SUBSET Words,
         final: BOOLEAN, fwords: SUBSET Words, sent: SUBSET Words]
Commit == [peek: BOOLEAN, at: 0..MaxPos, turn: 0..MaxCommits, dropped: BOOLEAN,
           previewed: BOOLEAN, asked: SUBSET Words]

\* The position a turn's words start at: the turn before it, or the session's start.
Lo(ts, k) == IF k = 1 THEN 0 ELSE ts[k - 1].at
\* The words that belong to turn k: those that end in [Lo, at).
Belongs(ts, k) == {i \in Words : Lo(ts, k) <= E[i] /\ E[i] < ts[k].at}
LastAt == IF turns = <<>> THEN 0 ELSE turns[Len(turns)].at

TypeOK ==
    /\ E \in Ends
    /\ pos \in 0..MaxPos /\ dec \in 0..MaxPos /\ bound \in 0..MaxPos
    /\ emitted \in 0..NWords /\ answered \in 0..MaxCommits
    /\ Len(chan) <= QLen
    /\ \A k \in 1..Len(commits) : commits[k] \in Commit
    /\ \A k \in 1..Len(turns) : turns[k] \in Turn
    /\ open \subseteq Words /\ endThrough \in 0..MaxPos
    /\ speaking \in BOOLEAN /\ stops \in 0..MaxStops /\ peekIdx \in 0..MaxCommits
    /\ atStop \in 0..MaxCommits /\ split \in BOOLEAN /\ lateInterim \in BOOLEAN

Init ==
    /\ E \in Ends
    /\ pos = 0 /\ dec = 0 /\ emitted = 0 /\ bound = 0 /\ answered = 0
    /\ chan = <<>> /\ commits = <<>> /\ turns = <<>> /\ open = {} /\ endThrough = 0
    /\ speaking = FALSE /\ stops = 0 /\ peekIdx = 0 /\ atStop = 0 /\ split = FALSE
    /\ lateInterim = FALSE

(* ---------------------------------------------------------------- the engine *)

Feed ==
    /\ pos < MaxPos
    /\ pos' = pos + 1
    /\ UNCHANGED <<E, dec, emitted, bound, answered, chan, brain>>

Decode ==
    /\ dec < pos
    /\ dec' = dec + 1
    /\ UNCHANGED <<E, pos, emitted, bound, answered, chan, brain>>

\* The next word, once the decoder is past its end -- or later (held).
Emit ==
    /\ emitted < NWords
    /\ E[emitted + 1] < dec
    /\ Len(chan) < QLen
    /\ emitted' = emitted + 1
    /\ chan' = Append(chan, <<"delta", emitted + 1, 0>>)
    /\ UNCHANGED <<E, pos, dec, bound, answered, brain>>

\* The report: the bound as high as it may go -- the decoder's position, but never
\* past a word still to be sent -- which is the most a brain could be misled by.
NextEnd == IF emitted < NWords THEN E[emitted + 1] ELSE MaxPos
Report ==
    /\ Len(chan) < QLen
    /\ bound' = Max(bound, Min(dec, NextEnd))
    /\ chan' = Append(chan, <<"progress", bound', dec>>)
    /\ UNCHANGED <<E, pos, dec, emitted, answered, brain>>

Preview ==
    /\ answered < Len(commits)
    /\ Len(chan) < QLen
    /\ answered' = answered + 1
    /\ chan' = Append(chan, <<"preview", answered + 1, 0>>)
    /\ UNCHANGED <<E, pos, dec, emitted, bound, brain>>

(* ----------------------------------------------------------------- the brain *)

NewTurn(at, mine) == [at |-> at, words |-> mine, previewed |-> FALSE, asked |-> {},
                      final |-> FALSE, fwords |-> {}, sent |-> {}]
TurnCommit(t, at) == [peek |-> FALSE, at |-> at, turn |-> t, dropped |-> FALSE,
                      previewed |-> FALSE, asked |-> {}]
\* _turn_at: the open words that end before `at` ("arrival": all of them).
Mine(at) == IF DESIGN = "arrival" THEN open ELSE {i \in open : E[i] < at}

\* The caller speaks: a peek out is no one's now (_drop_peek).
Speak ==
    /\ ~speaking
    /\ speaking' = TRUE
    /\ commits' = IF peekIdx /= 0 THEN [commits EXCEPT ![peekIdx].dropped = TRUE]
                  ELSE commits
    /\ peekIdx' = 0
    /\ split' = (split \/ atStop /= 0)
    /\ atStop' = 0
    /\ UNCHANGED <<engine, chan, turns, open, endThrough, stops, lateInterim>>

\* The VAD stop: with PEEKS and nothing in flight, the peek (_peek_at_stop) --
\* ("peekAsTurn") a commit that closes the turn at the stop.
Stop ==
    /\ speaking /\ stops < MaxStops
    /\ speaking' = FALSE
    /\ stops' = stops + 1
    /\ IF PEEKS /\ answered = Len(commits) /\ Len(commits) < MaxCommits /\ pos > LastAt
       THEN IF DESIGN = "peekAsTurn"
            THEN /\ turns' = Append(turns, NewTurn(pos, Mine(pos)))
                 /\ commits' = Append(commits, TurnCommit(Len(turns) + 1, pos))
                 /\ open' = open \ Mine(pos)
                 /\ peekIdx' = 0
                 /\ atStop' = Len(turns) + 1
            ELSE /\ commits' = Append(commits, [peek |-> TRUE, at |-> pos, turn |-> 0,
                                                dropped |-> FALSE, previewed |-> FALSE,
                                                asked |-> {}])
                 /\ peekIdx' = Len(commits) + 1
                 /\ UNCHANGED <<turns, open, atStop>>
       ELSE UNCHANGED <<commits, turns, open, peekIdx, atStop>>
    /\ UNCHANGED <<engine, chan, endThrough, split, lateInterim>>

\* A turn closes: on the stop's peek if it is out (_adopt_peek; the caller is quiet then,
\* or Speak would have dropped it), else a commit now (_take_turn) -- a verdict, a stop
\* over the bot, the backstop with the VAD stuck.
Close ==
    /\ IF peekIdx /= 0
       THEN LET c == commits[peekIdx]
                t == Len(turns) + 1
            IN /\ turns' = Append(turns, [NewTurn(c.at, Mine(c.at)) EXCEPT
                                          !.previewed = c.previewed,
                                          !.asked = c.asked, !.sent = c.asked])
               /\ commits' = [commits EXCEPT ![peekIdx].turn = t]
               /\ open' = open \ Mine(c.at)
               /\ peekIdx' = 0
       ELSE /\ Len(commits) < MaxCommits
            /\ pos > LastAt
            /\ turns' = Append(turns, NewTurn(pos, Mine(pos)))
            /\ commits' = Append(commits, TurnCommit(Len(turns) + 1, pos))
            /\ open' = open \ Mine(pos)
            /\ UNCHANGED peekIdx
    /\ atStop' = 0
    /\ UNCHANGED <<engine, chan, endThrough, speaking, stops, split, lateInterim>>

\* The turns a delta for word i may go to, oldest first: _slice_delta's rule, or
\* ("arrival") any turn whose preview has not come.
Takers(i) == IF DESIGN = "arrival"
             THEN {k \in 1..Len(turns) : ~turns[k].previewed}
             ELSE {k \in 1..Len(turns) : ~turns[k].final /\ E[i] < turns[k].at}
MinOf(S) == CHOOSE k \in S : \A j \in S : k <= j

\* An interim of word i is late if a closed turn's position is past its end.
Late(i) == \E k \in 1..Len(turns) : E[i] < turns[k].at

RecvDelta ==
    /\ chan /= <<>> /\ Head(chan)[1] = "delta"
    /\ LET i == Head(chan)[2]
           K == Takers(i)
       IN IF K /= {}
          THEN /\ turns' = [turns EXCEPT ![MinOf(K)].words = @ \cup {i}]
               /\ open' = open
               /\ lateInterim' = (lateInterim \/ (DESIGN = "interimAll" /\ Late(i)))
          ELSE /\ turns' = turns
               /\ open' = open \cup {i}
               /\ lateInterim' = (lateInterim \/ Late(i))
    /\ chan' = Tail(chan)
    /\ UNCHANGED <<engine, commits, endThrough, speaking, stops, peekIdx, atStop, split>>

\* What a turn made final delivers downstream (_settle_turn): its provisional words were
\* pushed at the preview; a final that differs goes down as a correction carrying the
\* final; an empty provisional's final words go on as new words ("noNewWords": dropped).
Sent(t) == IF t.asked = {} /\ t.words /= {} /\ DESIGN = "noNewWords" THEN {}
           ELSE IF t.words /= t.asked THEN t.words ELSE t.asked

\* _finalize_turns: oldest first, every turn previewed and passed by the bound `et`.
Ready(ts, k, et) == \A j \in 1..k : ts[j].final \/ (ts[j].previewed /\ ts[j].at <= et)
Finalize(ts, et) ==
    [k \in 1..Len(ts) |->
        IF ~ts[k].final /\ Ready(ts, k, et)
        THEN [ts[k] EXCEPT !.final = TRUE, !.fwords = ts[k].words, !.sent = Sent(ts[k])]
        ELSE ts[k]]

\* _handle_preview: a turn's pushes its words in hand; a peek adopted already, the same;
\* a peek out holds the words in hand that end before it; a dropped one, nothing.
Pushed(ts, t) == [ts EXCEPT ![t].previewed = TRUE, ![t].asked = ts[t].words,
                            ![t].sent = ts[t].words]
RecvPreview ==
    /\ chan /= <<>> /\ Head(chan)[1] = "preview"
    /\ LET k == Head(chan)[2]
           c == commits[k]
       IN IF c.turn /= 0
          THEN /\ turns' = Finalize(Pushed(turns, c.turn), endThrough)
               /\ commits' = [commits EXCEPT ![k].previewed = TRUE]
          ELSE /\ turns' = turns
               /\ commits' = IF c.dropped THEN commits
                             ELSE [commits EXCEPT ![k].previewed = TRUE,
                                                  ![k].asked = {i \in open : E[i] < c.at}]
    /\ chan' = Tail(chan)
    /\ UNCHANGED <<engine, open, endThrough, speaking, stops, peekIdx, atStop, split,
                   lateInterim>>

RecvProgress ==
    /\ chan /= <<>> /\ Head(chan)[1] = "progress"
    /\ LET et == Max(endThrough, IF DESIGN = "decoded" THEN Head(chan)[3] ELSE Head(chan)[2])
       IN /\ endThrough' = et
          /\ turns' = Finalize(turns, et)
    /\ chan' = Tail(chan)
    /\ UNCHANGED <<engine, commits, open, speaking, stops, peekIdx, atStop, split,
                   lateInterim>>

Next == Feed \/ Decode \/ Emit \/ Report \/ Preview
        \/ Speak \/ Stop \/ Close \/ RecvDelta \/ RecvPreview \/ RecvProgress

Spec == Init /\ [][Next]_vars

(* --------------------------------------------------------------- properties *)

\* No word is in two places: the open turn and the closed ones.
NoWordTwice ==
    \A i \in Words : (IF i \in open THEN 1 ELSE 0)
                     + Cardinality({k \in 1..Len(turns) : i \in turns[k].words}) <= 1

\* A turn holds only words that end in its own stretch: none of the next turn's, none of
\* the last one's.
RightTurn ==
    \A k \in 1..Len(turns) : turns[k].words \subseteq Belongs(turns, k)

\* A turn made final has every word of its stretch: none can still come.
FinalComplete ==
    \A k \in 1..Len(turns) : turns[k].final => turns[k].fwords = Belongs(turns, k)

\* A closed turn's word is never announced as an interim.
NoLateInterim == ~lateInterim

\* Every word of a final went downstream: in the provisional, a correction or new words.
NoLostWords ==
    \A k \in 1..Len(turns) : turns[k].final => turns[k].fwords \subseteq turns[k].sent

\* No turn was closed at a stop the caller then talked past: a peek never splits a
\* sentence.
NoSplitOnResume == ~split

\* Witnesses: that the shipped design reaches what the properties above are about.
\* A final with words its provisional lacked (they came after the preview) ...
NoLateWord == \A k \in 1..Len(turns) : turns[k].final => turns[k].fwords = turns[k].asked
\* ... an empty provisional whose final has words (they go on as new words) ...
NoEmptyProvisional ==
    \A k \in 1..Len(turns) : turns[k].final => ~(turns[k].asked = {} /\ turns[k].fwords /= {})
\* ... a turn closed on a peek, and a peek the caller talked past.
NoAdoptedPeek == \A k \in 1..Len(commits) : ~(commits[k].peek /\ commits[k].turn /= 0)
NoDroppedPeek == \A k \in 1..Len(commits) : ~commits[k].dropped

=============================================================================
