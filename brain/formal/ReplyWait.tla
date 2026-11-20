----------------------------- MODULE ReplyWait -----------------------------
(***************************************************************************)
(* teaport -- the reply's wait, and the bare stop (barge_pause.py,          *)
(* PauseAwareMinWordsStrategy: WORDS NOBODY SPOKE, #22; A BARE STOP).       *)
(*                                                                          *)
(* What a caller should get:                                                *)
(*                                                                          *)
(*   - Words the STT returns with no speech under them, after a turn was    *)
(*     committed and before its reply is audible, do not cut that reply     *)
(*     before a word of it plays: they join the committed turn (a TAIL).    *)
(*   - That wait is for a reply still to come, and for nothing else. Once   *)
(*     the reply has been audible, or has ended with nothing to play, a     *)
(*     word is a turn again -- the caller's "Yes." to the question the bot  *)
(*     just asked is answered, not folded into the turn before.             *)
(*   - "Okay, wait." stops the reply and asks nothing (the turn is HELD) -- *)
(*     but only a turn that cut something, and only when the stop is all    *)
(*     the turn said. "Wait." to a box that has finished speaking is the    *)
(*     model's to answer; so is a stop that follows, in the same turn,      *)
(*     words the model was already asked on.                                *)
(*                                                                          *)
(* The strategy cannot see the reply. It sees the turn's start and stop     *)
(* (the controller), each commit (the cut fence asks it whether to hold     *)
(* one), the bot's audio starting and stopping, and the reply ending        *)
(* unspoken (UnspokenReplyTap). The model keeps the reply as ground truth   *)
(* (pending, playing) beside what the strategy keeps (the s- variables),    *)
(* and asks whether the strategy's wait is ever out of step with it.        *)
(*                                                                          *)
(* The step that makes it hard is pipecat's refused finalize                *)
(* (UserTurn.tla, Finalize): a stop strategy that concludes while the VAD   *)
(* still hears the caller commits the turn -- the model is asked -- and     *)
(* the turn stays OPEN until their VAD stop. The reply can start, play and  *)
(* even end before the turn "stops".                                        *)
(*                                                                          *)
(* Not modelled: the onset test (THE ONSET: speech heard is the VAD's       *)
(* here), fillers and the box's own lines (audio that is no reply), the     *)
(* incoming-call question (never_holds), a reply that ends unspoken where   *)
(* the tap does not see it (a failed synthesis: the 10 s bound, Expire, is  *)
(* what ends that wait), the barge-in pause and the judge (UserTurn.tla),   *)
(* and which stop closes which words (_closed, by the STT's stamps): here   *)
(* words either are the heard speech's or nobody's.                         *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS
    WAIT,    \* when a stopped turn's reply is awaited (the strategy's _reply_due):
             \*   "commit" -- as shipped: the turn committed words, and nothing has
             \*     answered its last asked commit yet (bot audio, or the reply ending
             \*     unspoken, while the turn was still open);
             \*   "stop" -- before the 2026-11-19 audit: the turn committed words;
             \*   "never" -- before #22: no wait at all
    HOLD,    \* which commits may be held as a bare stop:
             \*   "turn" -- as shipped: one the fence dropped an unplayed reply for, or
             \*     the first words a turn that cut something has the model asked on;
             \*   "commit" -- before the audit: any commit of a turn that cut something
    FENCE    \* BOOLEAN: a later commit of a turn drops that turn's reply if none of it
             \*   has played (endpointing.CutReplyFence with the reply hold: SIP). FALSE
             \*   is Talk and the room as shipped: both replies stay

ASSUME WAIT \in {"commit", "stop", "never"} /\ HOLD \in {"turn", "commit"} /\ FENCE \in BOOLEAN

VARIABLES
    \* --- ground truth ---
    vad,        \* the VAD hears the caller
    turn,       \* a user turn is open (the controller)
    owed,       \* its finalize was refused: owed at the VAD stop
    agg,        \* the open turn's words not yet committed: "none", "bare" (nothing but
                \*   a stop: "Okay, wait."), "plain" (anything else)
    pending,    \* replies asked for and not yet audible, ended or cut (0..2)
    playing,    \* the bot is audible
    multi,      \* two replies of one turn are alive (a later commit dropped nothing)
    cutSome,    \* the open (or last) turn cut something at its start: a reply in flight
                \*   or audible, or its speech began over one
    \* --- the strategy (barge_pause.PauseAwareMinWordsStrategy) ---
    sWords,     \* _turn_words: a final with words came in the turn, and was not held
    sDue,       \* _reply_due
    sLate,      \* REPLY_WAIT_S has passed since it was due
    sAudible,   \* _audible
    sHeard,     \* _heard_at: speech heard whose words have not come yet
    sOver,      \* that speech began before the bot went quiet (_speech_at < _quiet_at)
    sCut,       \* _turn_cut: read at the turn's start
    sAsked,     \* the turn has a commit the model was asked on
    sAnswered,  \* ... and its latest has been answered while the turn was open
    \* --- what went wrong (ghosts) ---
    tailCut,    \* words nobody spoke cut a reply in flight that the wait was for
    heldUncut   \* a commit was held as a bare stop with nothing cut

vars == <<vad, turn, owed, agg, pending, playing, multi, cutSome,
          sWords, sDue, sLate, sAudible, sHeard, sOver, sCut, sAsked, sAnswered,
          tailCut, heldUncut>>

Init ==
    /\ vad = FALSE /\ turn = FALSE /\ owed = FALSE /\ agg = "none"
    /\ pending = 0 /\ playing = FALSE /\ multi = FALSE /\ cutSome = FALSE
    /\ sWords = FALSE /\ sDue = FALSE /\ sLate = FALSE /\ sAudible = FALSE
    /\ sHeard = FALSE /\ sOver = FALSE /\ sCut = FALSE /\ sAsked = FALSE
    /\ sAnswered = FALSE
    /\ tailCut = FALSE /\ heldUncut = FALSE

\* THE WAIT (_waiting): the reply to the last turn is still to come.
Waiting == sDue /\ ~sAudible /\ ~sLate

\* Words of class c join the words in hand: a bare stop stays one only among its like.
Join(a, c) == IF c = "stop" /\ a \in {"none", "bare"} THEN "bare" ELSE "plain"

(* ---------------- the caller ---------------- *)

VadStart ==
    /\ ~vad
    /\ vad' = TRUE /\ sHeard' = TRUE
    /\ sOver' = playing                         \* _spoke: the latest start wins
    /\ UNCHANGED <<turn, owed, agg, pending, playing, multi, cutSome,
                   sWords, sDue, sLate, sAudible, sCut, sAsked, sAnswered, tailCut, heldUncut>>

\* The turn stops (handle_user_turn_stopped): from here its reply is awaited, or not.
Stopped ==
    /\ turn' = FALSE /\ owed' = FALSE
    /\ sDue' = CASE WAIT = "never" -> FALSE
                 [] WAIT = "stop" -> sWords'
                 [] OTHER -> sWords' /\ ~sAnswered'
    /\ sLate' = FALSE

\* A commit of the words in hand (the stop strategy concludes, or a re-applied
\* finalize with words since): the fence may first drop the turn's unplayed reply,
\* then asks the strategy whether to hold it (hold_bare_stop), else the model is asked.
Drops == FENCE /\ sAsked /\ pending > 0 /\ ~playing
Holds == /\ agg = "bare"
         /\ IF HOLD = "commit" THEN Drops \/ sCut
                               ELSE Drops \/ (sCut /\ ~sAsked)
CommitWords ==
    LET left == IF Drops THEN 0 ELSE pending IN
    /\ agg' = "none"
    /\ IF Holds
         THEN /\ pending' = left
              /\ sWords' = FALSE                \* its words are gone, as a taken final's
              /\ heldUncut' = (heldUncut \/ ~(Drops \/ (cutSome /\ ~sAsked)))
              /\ UNCHANGED <<multi, sAsked, sAnswered>>
         ELSE /\ pending' = left + 1
              /\ multi' = (multi \/ left > 0 \/ (playing /\ sAsked))
              /\ sAsked' = TRUE /\ sAnswered' = FALSE
              /\ UNCHANGED <<sWords, heldUncut>>

\* The stop strategy concludes on the words in hand. While the VAD still hears the
\* caller the finalize is refused: committed, and the turn stays open.
Conclude ==
    /\ turn /\ agg # "none" /\ pending < 2
    /\ CommitWords
    /\ IF vad THEN /\ owed' = TRUE
                   /\ UNCHANGED <<turn, sDue, sLate>>
              ELSE Stopped
    /\ UNCHANGED <<vad, playing, cutSome, sAudible, sHeard, sOver, sCut, tailCut>>

\* The VAD stop: an owed finalize is re-applied, with a commit of the words since.
VadStop ==
    /\ vad /\ vad' = FALSE
    /\ IF owed
         THEN /\ IF agg # "none" /\ pending < 2
                   THEN CommitWords
                   ELSE UNCHANGED <<agg, pending, multi, sWords, sAsked, sAnswered, heldUncut>>
              /\ Stopped
         ELSE UNCHANGED <<turn, owed, agg, pending, multi, sWords, sDue, sLate,
                          sAsked, sAnswered, heldUncut>>
    /\ UNCHANGED <<playing, cutSome, sAudible, sHeard, sOver, sCut, tailCut>>

\* A transcript: words of class c ("plain", "stop": nothing but a stop, "mixed": a stop
\* word among others); sp: they are the words of the speech the VAD heard, else
\* nobody spoke them (the engine's own close on noise, a tail decoded twice).
\*
\* With a turn open they are its own. With none: a TAIL if nobody spoke them, they hold
\* no stop word, and the reply is awaited -- no turn, nothing cut. Else a turn starts,
\* and its interruption cuts whatever the bot was saying or about to (over the audible
\* bot one plain word may be too few: MinWords, a no-op here).
Tail(c, sp) == ~turn /\ ~sp /\ ~sHeard /\ c = "plain" /\ Waiting
\* The requirement the wait exists for: these words should have been a tail.
TailDue(c, sp) == /\ ~turn /\ ~sp /\ ~sHeard /\ c = "plain"
                  /\ pending = 1 /\ ~playing /\ ~multi /\ ~sLate
Say(c, sp) ==
    /\ sp => sHeard
    /\ sHeard' = (sHeard /\ ~sp)
    /\ IF turn THEN
          /\ agg' = Join(agg, c) /\ sWords' = TRUE
          /\ UNCHANGED <<turn, pending, playing, multi, cutSome, sDue, sLate, sAudible,
                         sOver, sCut, sAsked, sAnswered, tailCut>>
       ELSE IF Tail(c, sp) THEN
          UNCHANGED <<turn, agg, pending, playing, multi, cutSome, sWords, sDue, sLate,
                      sAudible, sOver, sCut, sAsked, sAnswered, tailCut>>
       ELSE
          \/ /\ playing /\ c = "plain"          \* too few words over the bot: no turn
             /\ UNCHANGED <<turn, agg, pending, playing, multi, cutSome, sWords, sDue,
                            sLate, sAudible, sOver, sCut, sAsked, sAnswered, tailCut>>
          \/ /\ turn' = TRUE /\ agg' = Join("none", c) /\ sWords' = TRUE
             /\ cutSome' = (pending > 0 \/ playing \/ sOver)
             /\ sCut' = (sAudible \/ Waiting \/ sOver)
             /\ tailCut' = (tailCut \/ TailDue(c, sp))
             /\ pending' = 0 /\ playing' = FALSE /\ multi' = FALSE   \* the interruption
             /\ sAudible' = FALSE /\ sDue' = FALSE /\ sOver' = FALSE
             /\ sAsked' = FALSE /\ sAnswered' = FALSE
             /\ UNCHANGED sLate
    /\ UNCHANGED <<vad, owed, heldUncut>>

(* ---------------- the reply ---------------- *)

\* What answers the open turn's latest asked commit, as the strategy sees it.
Answers == sAnswered \/ (turn /\ sAsked)

BotStart ==                                     \* BotStartedSpeakingFrame
    /\ pending > 0 /\ ~playing
    /\ pending' = pending - 1 /\ playing' = TRUE
    /\ sAudible' = TRUE /\ sDue' = FALSE /\ sOver' = FALSE
    /\ sAnswered' = Answers
    /\ UNCHANGED <<vad, turn, owed, agg, multi, cutSome, sWords, sLate, sHeard, sCut,
                   sAsked, tailCut, heldUncut>>

BotStop ==                                      \* BotStoppedSpeakingFrame
    /\ playing
    /\ playing' = FALSE /\ sAudible' = FALSE
    /\ multi' = (multi /\ pending > 0)
    /\ UNCHANGED <<vad, turn, owed, agg, pending, cutSome, sWords, sDue, sLate, sHeard,
                   sOver, sCut, sAsked, sAnswered, tailCut, heldUncut>>

\* The reply ends with nothing of it to play (UnspokenReplyTap: an empty answer, a tool
\* round that asks for none).
Unspoken ==
    /\ pending > 0 /\ ~playing
    /\ pending' = pending - 1
    /\ multi' = (multi /\ pending > 1)
    /\ sDue' = FALSE
    /\ sAnswered' = Answers
    /\ UNCHANGED <<vad, turn, owed, agg, playing, cutSome, sWords, sLate, sAudible,
                   sHeard, sOver, sCut, sAsked, tailCut, heldUncut>>

\* REPLY_WAIT_S after the stop: the wait is over, whatever became of the reply.
Expire ==
    /\ sDue /\ ~sLate /\ sLate' = TRUE
    /\ UNCHANGED <<vad, turn, owed, agg, pending, playing, multi, cutSome, sWords, sDue,
                   sAudible, sHeard, sOver, sCut, sAsked, sAnswered, tailCut, heldUncut>>

Next ==
    \/ VadStart \/ VadStop \/ Conclude
    \/ \E c \in {"plain", "stop", "mixed"}, sp \in BOOLEAN : Say(c, sp)
    \/ BotStart \/ BotStop \/ Unspoken \/ Expire

Spec == Init /\ [][Next]_vars

(* ---------------- properties ---------------- *)

\* The wait is for a reply still to come: the strategy never awaits one that has been
\* audible, has ended unspoken, or was never asked for. (Awaiting one that is not there,
\* it takes the caller's next unheard word for a tail of a turn already answered, and
\* reads a turn begun in that "wait" as one that cut something.)
WaitOnlyInFlight == Waiting => pending > 0

\* A commit is held as a bare stop only if its turn cut something and the stop is all
\* the turn has had the model asked on, or the fence dropped an unplayed reply for it.
NoHeldWithNothingCut == ~heldUncut

\* Words nobody spoke do not cut the reply to the turn before them while it is still to
\* come (#22): with one reply awaited, inside the bound, they are a tail.
TailJoins == ~tailCut

TypeOK ==
    /\ vad \in BOOLEAN /\ turn \in BOOLEAN /\ owed \in BOOLEAN
    /\ agg \in {"none", "bare", "plain"} /\ pending \in 0..2 /\ playing \in BOOLEAN
    /\ multi \in BOOLEAN /\ cutSome \in BOOLEAN
    /\ sWords \in BOOLEAN /\ sDue \in BOOLEAN /\ sLate \in BOOLEAN /\ sAudible \in BOOLEAN
    /\ sHeard \in BOOLEAN /\ sOver \in BOOLEAN /\ sCut \in BOOLEAN
    /\ sAsked \in BOOLEAN /\ sAnswered \in BOOLEAN
    /\ tailCut \in BOOLEAN /\ heldUncut \in BOOLEAN
    /\ owed => turn
=============================================================================
