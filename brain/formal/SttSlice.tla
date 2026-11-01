--------------------------------- MODULE SttSlice ---------------------------------
(***************************************************************************)
(* teaport -- sliced turn text: a turn is the words of ONE continuous      *)
(* engine transcript that end before its commit (stt.py TURN TEXT,         *)
(* TEAPORT_STT_TURN_TEXT=slice; the engine's preview stream, PROTOCOL3),   *)
(* a word past the cut that the commit's preview vouched for (THE CUT'S    *)
(* MARGIN), the final's timeout, and the preview asked at the VAD stop     *)
(* (THE PEEK, TEAPORT_STT_PREVIEW_AT_STOP). What each turn's final then    *)
(* delivers downstream, and how it is applied, is TurnText.tla's.          *)
(*                                                                          *)
(*   brain/teaport_brain/stt.py -- _take_turn / _turn_at (a turn's close),  *)
(*                                 _peek_at_stop, _drop_peek, _adopt_peek,  *)
(*                                 _push_turn (its reclaim), _vouch,        *)
(*                                 _slice_delta (a main delta),             *)
(*                                 _handle_preview, _finalize_turns         *)
(*                                                                          *)
(* The ENGINE decodes one stream. The caller's words start at S[1] < S[2]  *)
(* < ...; the main stream stamps each with end_ms E[i], from its start to  *)
(* WLEN steps after it, so a commit can fall inside a word (S < P <= E).   *)
(* It sends a word whole once its decoder is past E[i], or later -- but    *)
(* before it is D past the word's start (the premise of the final's        *)
(* timeout). Its progress report carries dec and two bounds: no delta sent *)
(* after it ends below end_through_ms, or starts below audio_through_ms;   *)
(* each as high as the engine may put it, or (FREEZE) held where it was,   *)
(* as 03e4223 froze the end bound through a pause. It answers every commit *)
(* at                                                                       *)
(* P with one preview, in order: the words not sent yet that began before  *)
(* P (it decodes the audio up to P) -- or (LOSSY) only some, or none when  *)
(* cut short ("complete": false, taken as no preview). The model           *)
(* does NOT assume what PROTOCOL3 also says, that no word ending after a   *)
(* commit is sent before its preview. One FIFO carries all of it.          *)
(*                                                                          *)
(* The BRAIN closes turns (Close: a verdict, the ceiling, a stop over the  *)
(* bot -- when is SttCommit.tla's business) and, with PEEKS, peeks at a    *)
(* VAD stop (Stop) with nothing in flight: a commit that closes nothing.   *)
(* The caller resuming (Speak) drops the peek. A Close with the stop's     *)
(* peek out adopts it: the turn closes at the peek's position, on its      *)
(* preview, with no second commit, takes back the open words the preview  *)
(* vouched for, and is made final at once if it can be. A turn takes the   *)
(* open words that end before its position; a delta goes to the oldest     *)
(* turn not yet final that it ends before -- or that it starts before (or  *)
(* ends less than M past) and whose preview vouched for it -- else to the  *)
(* open turn (shown as an interim); a preview makes its turn's provisional *)
(* text (the words in hand that end before it, then the preview's), or a   *)
(* peek's; a report makes every turn final, oldest first, whose preview    *)
(* has come and whose position the end bound has passed -- while its      *)
(* preview still vouches for a word, the end bound by M and the start      *)
(* bound too -- or the decoder D past it (the final's timeout).            *)
(*                                                                          *)
(* DESIGN = "shipped"     -- as above.                                      *)
(*          "arrival"     -- a turn is the words RECEIVED by its preview    *)
(*                           (fails RightTurn: a commit's last word, sent   *)
(*                           after the commit, lands in the next turn).     *)
(*          "interimAll"  -- every delta announced as an interim (fails     *)
(*                           NoLateInterim: a committed turn's own late     *)
(*                           word would start a turn and cut its reply).    *)
(*          "decoded"     -- final once decoded_through_ms passes the       *)
(*                           commit (fails FinalComplete).                  *)
(*          "noVouch"     -- sliced by end_ms alone, as before the          *)
(*                           2026-10-27 review (fails NoDropAtCut: the      *)
(*                           turn's last word, stamped past the cut, is in  *)
(*                           its provisional and not its final, and goes to *)
(*                           the next turn -- the backstop's own turn).     *)
(*          "endOnly"     -- vouched only if it ends less than M past the   *)
(*                           cut, as first written (fails NoDropAtCut: a    *)
(*                           word the cut falls inside goes to the next     *)
(*                           turn).                                         *)
(*          "noStartWait" -- vouched by start, but final on the end bound   *)
(*                           alone (fails FinalComplete: the word still to  *)
(*                           come).                                         *)
(*          "endMargin"   -- every word ending less than M past the cut is  *)
(*                           the turn's, vouched or not (fails RightTurn:   *)
(*                           the caller's next word is taken back).         *)
(*          "peekAsTurn"  -- the peek closes the turn at the stop (fails    *)
(*                           NoSplitOnResume: the caller resumes, and their *)
(*                           sentence is two turns).                        *)
(*          "noDrop"      -- the caller resuming does not drop the peek     *)
(*                           (fails NoSplitOnResume: a close adopts a stop  *)
(*                           the caller talked past).                       *)
(*          "noTimeout"   -- a final waits for the bound however long       *)
(*                           (fails NoStuckFinal: the bound froze).         *)
(*          "noTrim"      -- a preview's words an earlier turn's preview    *)
(*                           still vouches for are asked again (fails       *)
(*                           NoDoubleDeliver: a commit soon after another,  *)
(*                           the word not yet sent, is asked in both).      *)
(*          "noOrder"     -- a turn after one that put nothing with words   *)
(*                           on (an empty provisional, deferred) goes on at *)
(*                           its preview (fails NoDoubleDeliver: its        *)
(*                           preview has the earlier turn's unsent words,   *)
(*                           which that turn's final delivers again).       *)
(*          "noPeekOwed"  -- a peek adopted after the turn that owed words      *)
(*                           settled goes on at its preview (fails          *)
(*                           NoDoubleDeliver: that preview was taken while  *)
(*                           the word was owed, and holds it).              *)
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
          WLEN,        \* how many steps past its start a word's end_ms may be
          D,           \* the final's timeout (_FINAL_TIMEOUT_MS), in steps past a position
          FREEZE,      \* the engine may hold its bounds back (03e4223 froze the end bound)
          LOSSY,       \* a preview may miss words -- all of them when cut short
                       \* ("complete": false)
          DESIGN       \* "shipped" | "arrival" | "interimAll" | "decoded" | "noVouch"
                       \* | "endOnly" | "noStartWait" | "endMargin" | "peekAsTurn"
                       \* | "noDrop" | "noTimeout" | "noTrim" | "noOrder" | "noPeekOwed"

Words == 1..NWords
Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b
Increasing == {f \in [Words -> 0..(MaxPos - 1)] : \A i \in Words : i > 1 => f[i - 1] < f[i]}
\* The main stream's end_ms for words starting at s: from the start to WLEN past it.
Ends(s) == {f \in Increasing : \A i \in Words : s[i] <= f[i] /\ f[i] <= s[i] + WLEN}

\* THE CUT'S MARGIN (_VOUCH_MS), in steps.
M == 1
\* Whether the design vouches.
Vouch == DESIGN \notin {"arrival", "noVouch", "endMargin"}

VARIABLES
    S,           \* where each word starts (chosen at Init, strictly increasing)
    E,           \* the main stream's end_ms for it (chosen at Init)
    pos,         \* the audio sent: the session clock
    dec,         \* the engine's decoded_through_ms
    emitted,     \* the engine has sent words 1..emitted
    bound,       \* the engine's end_through_ms
    abound,      \* the engine's audio_through_ms
    answered,    \* commits the engine has answered with a preview
    chan,        \* engine -> brain, in order
    commits,     \* the brain's commits, in order: a turn's, or a peek
    turns,       \* the brain's closed turns, oldest first
    open,        \* words received that no turn has taken
    seen,        \* the brain's latest bounds and decoded position: [e, a, d]
    speaking,    \* the caller is speaking (the VAD's state)
    stops,       \* VAD stops so far
    peekIdx,     \* the stop's peek out (an index into commits), or 0
    atStop,      \* a turn closed at the latest stop, no verdict since ("peekAsTurn"), or 0
    split,       \* a turn was closed at a stop the caller talked past
    lateInterim  \* a closed turn's word was announced as an interim

engine == <<S, E, pos, dec, emitted, bound, abound, answered>>
brain == <<commits, turns, open, seen, speaking, stops, peekIdx, atStop, split,
           lateInterim>>
vars == <<engine, chan, brain>>

Turn == [at: 0..MaxPos, words: SUBSET Words, previewed: BOOLEAN, asked: SUBSET Words,
         expect: SUBSET Words, final: BOOLEAN, fwords: SUBSET Words, timedOut: BOOLEAN,
         deferred: BOOLEAN]
Commit == [peek: BOOLEAN, at: 0..MaxPos, turn: 0..MaxCommits, stop: 0..MaxStops,
           dropped: BOOLEAN, resumed: BOOLEAN, previewed: BOOLEAN, asked: SUBSET Words,
           pv: SUBSET Words, owed: BOOLEAN]

\* The position a turn's words start at: the turn before it, or the session's start.
Lo(ts, k) == IF k = 1 THEN 0 ELSE ts[k - 1].at
LastAt == IF turns = <<>> THEN 0 ELSE turns[Len(turns)].at
\* The words the brain has received.
Rcvd == open \cup UNION {turns[k].words : k \in 1..Len(turns)}

TypeOK ==
    /\ S \in Increasing /\ E \in Ends(S)
    /\ pos \in 0..MaxPos /\ dec \in 0..MaxPos /\ bound \in 0..MaxPos /\ abound \in 0..MaxPos
    /\ emitted \in 0..NWords /\ answered \in 0..MaxCommits
    /\ Len(chan) <= QLen
    /\ \A k \in 1..Len(commits) : commits[k] \in Commit
    /\ \A k \in 1..Len(turns) : turns[k] \in Turn
    /\ open \subseteq Words /\ seen \in [e: 0..MaxPos, a: 0..MaxPos, d: 0..MaxPos]
    /\ speaking \in BOOLEAN /\ stops \in 0..MaxStops /\ peekIdx \in 0..MaxCommits
    /\ atStop \in 0..MaxCommits /\ split \in BOOLEAN /\ lateInterim \in BOOLEAN

Init ==
    /\ S \in Increasing /\ E \in Ends(S)
    /\ pos = 0 /\ dec = 0 /\ emitted = 0 /\ bound = 0 /\ abound = 0 /\ answered = 0
    /\ chan = <<>> /\ commits = <<>> /\ turns = <<>> /\ open = {}
    /\ seen = [e |-> 0, a |-> 0, d |-> 0]
    /\ speaking = FALSE /\ stops = 0 /\ peekIdx = 0 /\ atStop = 0 /\ split = FALSE
    /\ lateInterim = FALSE

(* ---------------------------------------------------------------- the engine *)

Feed ==
    /\ pos < MaxPos
    /\ pos' = pos + 1
    /\ UNCHANGED <<S, E, dec, emitted, bound, abound, answered, chan, brain>>

\* Never D past the start of a word it has not sent.
Decode ==
    /\ dec < pos
    /\ \A i \in Words : i > emitted => dec + 1 < S[i] + D
    /\ dec' = dec + 1
    /\ UNCHANGED <<S, E, pos, emitted, bound, abound, answered, chan, brain>>

\* The next word, once the decoder is past its end -- or later (held).
Emit ==
    /\ emitted < NWords
    /\ E[emitted + 1] < dec
    /\ Len(chan) < QLen
    /\ emitted' = emitted + 1
    /\ chan' = Append(chan, <<"delta", emitted + 1, {}, [a |-> 0, d |-> 0]>>)
    /\ UNCHANGED <<S, E, pos, dec, bound, abound, answered, brain>>

\* The report: each bound as high as it may go -- the decoder's position, never past a
\* word still to be sent -- the most a brain could be misled by; or (FREEZE) both held
\* where they were, as 03e4223 held its end bound through a pause.
Next1(f) == IF emitted < NWords THEN f[emitted + 1] ELSE MaxPos
Up(b, f) == Max(b, Min(dec, Next1(f)))
Report ==
    /\ Len(chan) < QLen
    /\ \E frozen \in IF FREEZE THEN BOOLEAN ELSE {FALSE} :
          /\ bound' = IF frozen THEN bound ELSE Up(bound, E)
          /\ abound' = IF frozen THEN abound ELSE Up(abound, S)
    /\ chan' = Append(chan, <<"progress", IF DESIGN = "decoded" THEN dec ELSE bound', {},
                              [a |-> abound', d |-> dec]>>)
    /\ UNCHANGED <<S, E, pos, dec, emitted, answered, brain>>

\* The next commit's preview: the words not sent yet that began before it -- or
\* (LOSSY) any of them: a decode that missed some, or, cut short, all of them (taken as
\* no preview).
Preview ==
    /\ answered < Len(commits)
    /\ Len(chan) < QLen
    /\ answered' = answered + 1
    /\ LET full == {i \in Words : i > emitted /\ S[i] < commits[answered + 1].at}
       IN \E pv \in IF LOSSY THEN SUBSET full ELSE {full} :
             chan' = Append(chan, <<"preview", answered + 1, pv, [a |-> 0, d |-> 0]>>)
    /\ UNCHANGED <<S, E, pos, dec, emitted, bound, abound, brain>>

(* ----------------------------------------------------------------- the brain *)

NewTurn(at, mine) == [at |-> at, words |-> mine, previewed |-> FALSE, asked |-> {},
                      expect |-> {}, final |-> FALSE, fwords |-> {}, timedOut |-> FALSE,
                      deferred |-> FALSE]
TurnCommit(t, at) == [peek |-> FALSE, at |-> at, turn |-> t, stop |-> 0, dropped |-> FALSE,
                      resumed |-> FALSE, previewed |-> FALSE, asked |-> {}, pv |-> {},
                      owed |-> FALSE]
\* _turn_at: the open words that end before `at` ("arrival": all of them; "endMargin":
\* those ending less than M past it).
Mine(at) == CASE DESIGN = "arrival"   -> open
              [] DESIGN = "endMargin" -> {i \in open : E[i] < at + M}
              [] OTHER                -> {i \in open : E[i] < at}

\* _vouch: a word at or past the cut is the turn's if the preview vouched for it and it
\* starts before the cut or ends less than M past it ("endOnly": the latter only).
Vouched(t, i) ==
    /\ Vouch /\ i \in t.expect
    /\ (E[i] < t.at + M \/ (DESIGN /= "endOnly" /\ S[i] < t.at))

\* _finalize_turns: final once the end bound passes the position -- while the preview
\* still vouches for a word, by M, and the start bound too ("noStartWait": not that;
\* "endMargin": always by M) -- or (the timeout) once the decoder is D past it.
Bounded(t, b) ==
    CASE DESIGN = "endMargin" -> b.e >= t.at + M
      [] t.expect = {}        -> b.e >= t.at
      [] OTHER                -> b.e >= t.at + M /\ (DESIGN = "noStartWait" \/ b.a >= t.at)
Overdue(t, b) == DESIGN /= "noTimeout" /\ b.d >= t.at + D
Ready(ts, k, b) ==
    \A j \in 1..k : ts[j].final \/ (ts[j].previewed /\ (Bounded(ts[j], b) \/ Overdue(ts[j], b)))
Finalize(ts, b) ==
    [k \in 1..Len(ts) |->
        IF ~ts[k].final /\ Ready(ts, k, b)
        THEN [ts[k] EXCEPT !.final = TRUE, !.fwords = ts[k].words,
                           !.timedOut = ~Bounded(ts[k], b)]
        ELSE ts[k]]

\* _owed_before: a turn before `at`, previewed and not final, put nothing with words on
\* at its preview (an empty provisional, or deferred) and its final may still deliver
\* words; a turn after it then waits on its own final ("noOrder": does not).
OwedBefore(at) ==
    /\ DESIGN /= "noOrder"
    /\ \E j \in 1..Len(turns) : turns[j].previewed /\ ~turns[j].final /\ turns[j].at < at
                                /\ (turns[j].asked = {} \/ turns[j].deferred)

\* The caller speaks: a peek out is no one's now (_drop_peek) -- "noDrop": it stays out,
\* talked past.
Speak ==
    /\ ~speaking
    /\ speaking' = TRUE
    /\ IF peekIdx /= 0 /\ DESIGN /= "noDrop"
       THEN /\ commits' = [commits EXCEPT ![peekIdx].dropped = TRUE]
            /\ peekIdx' = 0
       ELSE /\ commits' = IF peekIdx /= 0 THEN [commits EXCEPT ![peekIdx].resumed = TRUE]
                          ELSE commits
            /\ UNCHANGED peekIdx
    /\ split' = (split \/ atStop /= 0)
    /\ atStop' = 0
    /\ UNCHANGED <<engine, chan, turns, open, seen, stops, lateInterim>>

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
                                                stop |-> stops + 1, dropped |-> FALSE,
                                                resumed |-> FALSE, previewed |-> FALSE,
                                                asked |-> {}, pv |-> {}, owed |-> FALSE])
                 /\ peekIdx' = Len(commits) + 1
                 /\ UNCHANGED <<turns, open, atStop>>
       ELSE UNCHANGED <<commits, turns, open, peekIdx, atStop>>
    /\ UNCHANGED <<engine, chan, seen, split, lateInterim>>

\* A turn closes: on the stop's peek if it is out and the stop's (_adopt_peek), else a
\* commit now (_take_turn) -- a verdict, a stop over the bot, the backstop with the VAD
\* stuck. An adopted peek's preview in hand: the turn takes back the open words it
\* vouched for (_push_turn's reclaim) and is made final at once if it can be; it waits
\* on its final if an earlier turn owed words when that preview came, or does now.
Close ==
    /\ IF peekIdx /= 0 /\ commits[peekIdx].stop = stops
       THEN LET c == commits[peekIdx]
                mine == Mine(c.at)
                t0 == [NewTurn(c.at, mine) EXCEPT
                          !.previewed = c.previewed, !.asked = c.asked,
                          !.expect = IF c.previewed /\ Vouch THEN c.pv \ mine ELSE {},
                          !.deferred = c.previewed /\ ((c.owed /\ DESIGN /= "noPeekOwed")
                                                        \/ OwedBefore(c.at))]
                back == IF c.previewed THEN {i \in open \ mine : Vouched(t0, i)} ELSE {}
                t == [t0 EXCEPT !.words = mine \cup back, !.expect = t0.expect \ back]
            IN /\ turns' = IF c.previewed THEN Finalize(Append(turns, t), seen)
                           ELSE Append(turns, t)
               /\ commits' = [commits EXCEPT ![peekIdx].turn = Len(turns) + 1]
               /\ open' = open \ (mine \cup back)
               /\ peekIdx' = 0
               /\ split' = (split \/ c.resumed)
       ELSE /\ Len(commits) < MaxCommits
            /\ pos > LastAt
            /\ turns' = Append(turns, NewTurn(pos, Mine(pos)))
            /\ commits' = Append(commits, TurnCommit(Len(turns) + 1, pos))
            /\ open' = open \ Mine(pos)
            /\ UNCHANGED <<peekIdx, split>>
    /\ atStop' = 0
    /\ UNCHANGED <<engine, chan, seen, speaking, stops, lateInterim>>

\* The turns a delta for word i may go to, oldest first: _slice_delta's rule -- ends
\* before it, or vouched for -- or ("arrival") any turn whose preview has not come.
Takers(i) == CASE DESIGN = "arrival"   -> {k \in 1..Len(turns) : ~turns[k].previewed}
               [] DESIGN = "endMargin" -> {k \in 1..Len(turns) :
                                             ~turns[k].final /\ E[i] < turns[k].at + M}
               [] OTHER -> {k \in 1..Len(turns) :
                              ~turns[k].final
                              /\ (E[i] < turns[k].at \/ Vouched(turns[k], i))}
MinOf(X) == CHOOSE k \in X : \A j \in X : k <= j

\* An interim of word i is late if a closed turn's position is past its end.
Late(i) == \E k \in 1..Len(turns) : E[i] < turns[k].at

RecvDelta ==
    /\ chan /= <<>> /\ Head(chan)[1] = "delta"
    /\ LET i == Head(chan)[2]
           K == Takers(i)
       IN IF K /= {}
          THEN /\ turns' = [turns EXCEPT ![MinOf(K)].words = @ \cup {i},
                                         ![MinOf(K)].expect = @ \ {i}]
               /\ open' = open
               /\ lateInterim' = (lateInterim \/ DESIGN = "interimAll")
          ELSE /\ turns' = turns
               /\ open' = open \cup {i}
               /\ lateInterim' = (lateInterim \/ Late(i))
    /\ chan' = Tail(chan)
    /\ UNCHANGED <<engine, commits, seen, speaking, stops, peekIdx, atStop, split>>

\* _past_earlier: the words a turn before `at`, previewed and not final, still expects --
\* a later preview's are that turn's ("noTrim": asked again). The code trims them in
\* their order within the preview's front run; words being unique here, that is this set
\* subtraction.
Owed(at) == IF DESIGN = "noTrim" THEN {}
            ELSE UNION {turns[j].expect : j \in {j \in 1..Len(turns) :
                            turns[j].previewed /\ ~turns[j].final /\ turns[j].at < at}}

\* _handle_preview: the provisional text is the words in hand that end before the
\* position, then the preview's, less what an earlier turn still expects. A turn's (or a
\* peek's adopted already) is pushed and vouches for the preview's words; a peek out
\* holds it; a dropped one, nothing.
RecvPreview ==
    /\ chan /= <<>> /\ Head(chan)[1] = "preview"
    /\ LET k == Head(chan)[2]
           c == commits[k]
           pv == Head(chan)[3] \ Owed(c.at)
       IN IF c.turn /= 0
          THEN /\ turns' = Finalize([turns EXCEPT
                                       ![c.turn].previewed = TRUE,
                                       ![c.turn].deferred = OwedBefore(turns[c.turn].at),
                                       ![c.turn].asked = turns[c.turn].words \cup pv,
                                       ![c.turn].expect = IF Vouch
                                                          THEN pv \ turns[c.turn].words
                                                          ELSE {}],
                                    seen)
               /\ commits' = [commits EXCEPT ![k].previewed = TRUE]
          ELSE /\ turns' = turns
               /\ commits' = IF c.dropped THEN commits
                             ELSE [commits EXCEPT ![k].previewed = TRUE, ![k].pv = pv,
                                                  ![k].asked = {i \in open : E[i] < c.at}
                                                               \cup pv,
                                                  ![k].owed = OwedBefore(c.at)]
    /\ chan' = Tail(chan)
    /\ UNCHANGED <<engine, open, seen, speaking, stops, peekIdx, atStop, split,
                   lateInterim>>

RecvProgress ==
    /\ chan /= <<>> /\ Head(chan)[1] = "progress"
    /\ LET m == Head(chan)
           b == [e |-> Max(seen.e, m[2]), a |-> Max(seen.a, m[4].a),
                 d |-> Max(seen.d, m[4].d)]
       IN /\ seen' = b
          /\ turns' = Finalize(turns, b)
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

\* A turn holds only words of its own stretch: none ending before the turn before it
\* closed, and none past its own close unless the word began before it -- never the
\* caller's next word.
RightTurn ==
    \A k \in 1..Len(turns) : \A i \in turns[k].words :
        /\ Lo(turns, k) <= E[i]
        /\ (E[i] < turns[k].at \/ S[i] < turns[k].at)

\* A turn made final has every word it will ever be given: none ending before it, and
\* none its preview still vouches for, can still come.
FinalComplete ==
    \A k \in 1..Len(turns) : turns[k].final =>
        /\ \A i \in Words : E[i] < turns[k].at => i \in Rcvd
        /\ turns[k].expect \subseteq Rcvd

\* A word the provisional had that began in the turn's stretch is in its final: a cut by
\* an end estimate, or inside a word, never moves the turn's own word to the next turn.
NoDropAtCut ==
    \A k \in 1..Len(turns) : turns[k].final =>
        \A i \in turns[k].asked : Lo(turns, k) <= S[i] => i \in turns[k].fwords

\* A closed turn's word is never announced as an interim.
NoLateInterim == ~lateInterim

\* No turn was closed at a stop the caller then talked past before its verdict: a peek
\* never splits a sentence.
NoSplitOnResume == ~split

\* What a turn delivers downstream: its provisional, pushed at its preview -- or, if it
\* waits on its final or its provisional had no words, its final's words once final (a
\* timed-out empty provisional settles as it went: nothing). No word delivered by two
\* turns: none asked twice, none delivered again after a later turn's.
Delivered(t) == CASE t.deferred   -> IF t.final THEN t.fwords ELSE {}
                  [] t.asked = {} -> IF t.final /\ ~t.timedOut THEN t.fwords ELSE {}
                  [] OTHER        -> t.asked
NoDoubleDeliver ==
    \A j, k \in 1..Len(turns) : j < k => Delivered(turns[j]) \cap Delivered(turns[k]) = {}

\* No turn waits on its final past the timeout: one the decoder is D past is final,
\* whatever the bounds did.
NoStuckFinal ==
    \A k \in 1..Len(turns) : turns[k].previewed /\ seen.d >= turns[k].at + D
                              => turns[k].final

\* Witnesses: that the shipped design reaches what the properties above are about.
\* A final with words its provisional lacked (they came after the preview) ...
NoLateWord == \A k \in 1..Len(turns) : turns[k].final => turns[k].fwords = turns[k].asked
\* ... an empty provisional whose final has words (TurnText.tla delivers them) ...
NoEmptyProvisional ==
    \A k \in 1..Len(turns) : turns[k].final => ~(turns[k].asked = {} /\ turns[k].fwords /= {})
\* ... a word the cut falls inside, vouched for, in its turn ...
NoWordAcrossCut ==
    \A k \in 1..Len(turns) : \A i \in turns[k].words : E[i] < turns[k].at + M
\* ... a final settled by its timeout ...
NoTimedOut == \A k \in 1..Len(turns) : ~turns[k].timedOut
\* ... a turn closed on a peek, and a peek the caller talked past.
NoAdoptedPeek == \A k \in 1..Len(commits) : ~(commits[k].peek /\ commits[k].turn /= 0)
NoDroppedPeek == \A k \in 1..Len(commits) : ~commits[k].dropped

=============================================================================
