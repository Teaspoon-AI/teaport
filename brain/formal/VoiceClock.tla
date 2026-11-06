----------------------------- MODULE VoiceClock -----------------------------
(***************************************************************************)
(* teaport -- one clock for "when does this word play" across a playout     *)
(* pause and a reply hold (#255).                                           *)
(*                                                                          *)
(*   brain/teaport_brain/playout_pause.py -- VoiceClock, PausablePlayout    *)
(*   brain/teaport_brain/engine_tts.py    -- the word baseline on the clock *)
(*   brain/teaport_brain/reply_hold.py    -- ReplyHoldGate._restamp         *)
(*   brain/teaport_brain/transcript_ledger.py -- the pts cut                *)
(*                                                                          *)
(* The TTS stamps every word with a playout time (pts) as it synthesizes    *)
(* it, and pipecat's output transport releases each word downstream when    *)
(* its clock reaches that pts. What reads a released word as "played now"   *)
(* -- the room's echo guard, the Talk captions, the assistant's running     *)
(* text -- and the ledger's cut, which reads the pts against the clock, are *)
(* right only if the clock runs exactly while the listener hears the bot.   *)
(*                                                                          *)
(* A playout pause stops the transport writing; what it had sent plays on   *)
(* (the lag), then the listener hears nothing until the resume. The VOICE   *)
(* CLOCK is the pipeline clock less every tick the voice stood still: it    *)
(* stops when the lag has played and runs again at the resume, and the     *)
(* transport alone moves it.                                                *)
(*                                                                          *)
(* The stream. A reply R: its first clause (N units, a word each) emitted   *)
(* at 0; optionally a second clause (LATE units) the TTS emits at any tick  *)
(* after (an underrun, if the first has played out by then); and optionally *)
(* a second context Q (QN units: a filler, the next reply) queued behind R. *)
(* The TTS places a clause at the end of what it emitted before, or at the  *)
(* clock's now if that has played out (_note_playout's gap, the baseline's  *)
(* max(now, previous end)). With HOLD, ReplyHoldGate holds R from 0 until a *)
(* release at any tick and moves R's every pts by how long it held it       *)
(* (_restamp); the TTS's model of R stands still through the hold           *)
(* (_freeze_playout "hold") and moves by the same amount at its end.        *)
(* Pauses (up to MaxPauses) come and go at any tick, during the hold too (a *)
(* filler playing over a held reply is paused).                             *)
(*                                                                          *)
(* RELEASE = "wall"  -- before #255: pts are on the pipeline clock and the  *)
(*                      transport releases on it. The TTS's freeze keeps    *)
(*                      its own stamps right (they are the voice clock's    *)
(*                      plus the stop so far), but a word stamped before a  *)
(*                      pause is released the pause early.                  *)
(* RELEASE = "voice" -- as shipped: pts are on the voice clock and the      *)
(*                      transport releases a word when the voice clock      *)
(*                      reaches it.                                         *)
(* HOLDCLOCK = "wall"  -- the gate measures a hold on the pipeline clock: a *)
(*                        pause during the hold counts twice.               *)
(* HOLDCLOCK = "voice" -- as shipped: on the voice clock.                   *)
(* STAMP = "wall"  -- the TTS places a clause that follows a gap at the     *)
(*                    pipeline clock's now (a read the voice clock missed). *)
(* STAMP = "voice" -- as shipped: at the voice clock's now.                 *)
(*                                                                          *)
(* The checks. WordsOnVoice: every unit, when the listener starts hearing   *)
(* it, has its word released at that very tick -- no earlier, no later.     *)
(* ChartIsHeard: at a cut, the ledger's pts cut (words placed before the   *)
(* voice clock's now) is exactly what the listener heard.                   *)
(*                                                                          *)
(* Not modelled: the caption lead (every pts is moved early by the same     *)
(* constant: the ledger takes it back; the transport holds a frame whose    *)
(* audio is past a stop until the start, and the echo guard dates a word   *)
(* by its audio, less the lead -- tested); the lag's measurement (what the  *)
(* transport sent and is still playing, _heard_until: tested); the one     *)
(* frame of lead a resumed write keeps; a unit is one word and one tick.    *)
(* Nor the other half of the ledger's cut: its heard_text is the LONGER of  *)
(* the pts prefix checked here and an estimate from its layout's seconds    *)
(* (read on the pipeline clock, clamped at the pause: _heard, _play_t), so  *)
(* the estimate can over-report what the pts cut got right. Owed.tla's      *)
(* ow_ledgerMax is that over-report's consequence; PlayoutPause.tla part 1  *)
(* checks the layout half on its own.                                       *)
(***************************************************************************)
EXTENDS Integers

CONSTANTS N,          \* units of R's first clause
          LATE,       \* units of R's second clause (0: none)
          QN,         \* units of the context queued behind R (0: none)
          HOLD,       \* the gate holds R from 0
          MaxHold,
          MaxPauses,
          LAG,        \* units the transport has sent ahead when it pauses (at most)
          RELEASE,
          HOLDCLOCK,
          STAMP

ASSUME /\ N >= 1 /\ LATE >= 0 /\ QN >= 0 /\ HOLD \in BOOLEAN /\ LAG \in 0..1
       /\ RELEASE \in {"wall", "voice"} /\ HOLDCLOCK \in {"wall", "voice"}
       /\ STAMP \in {"wall", "voice"}

Total == N + LATE + QN
Bound == Total + MaxHold + 2 * MaxPauses + 3

VARIABLES
    now,       \* the pipeline clock
    v,         \* the voice clock
    stood,     \* ticks the voice clock has stood still (now = v + stood)
    paused, stopAt, pauses,
    held,      \* the gate holds R
    shift,     \* the gate's shift of R's pts (fixed at the release)
    e2, e3,    \* when the TTS emitted R's second clause, Q (-1: not yet)
    b2, b3,    \* their voice-clock bases, before the gate's shift
    s2, s3,    \* ticks stood still when each was stamped (s1 = 0)
    heard,     \* units the listener has heard (started)
    offbeat,   \* a unit started at a tick its word was not released at
    cut, chart, truth

vars == <<now, v, stood, paused, stopAt, pauses, held, shift, e2, e3, b2, b3, s2, s3,
          heard, offbeat, cut, chart, truth>>

Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b

Init ==
    /\ now = 0 /\ v = 0 /\ stood = 0 /\ paused = FALSE /\ stopAt = 0 /\ pauses = 0
    /\ held = HOLD /\ shift = 0
    /\ e2 = -1 /\ e3 = -1 /\ b2 = 0 /\ b3 = 0 /\ s2 = 0 /\ s3 = 0
    /\ heard = 0 /\ offbeat = FALSE /\ cut = FALSE /\ chart = -1 /\ truth = -1

\* Units at the transport, in stream order: R once the gate lets it through (its second
\* clause once emitted), Q once emitted.
Avail == (IF held THEN 0 ELSE N + (IF e2 >= 0 THEN LATE ELSE 0))
         + (IF e3 >= 0 THEN QN ELSE 0)

\* Unit u's voice-clock pts (the gate's shift on R's), and the stop the TTS had seen when
\* it stamped it.
InR(u) == u < N + LATE
VPts(u) == IF u < N THEN u + shift
           ELSE IF u < N + LATE THEN b2 + (u - N) + shift
           ELSE b3 + (u - N - LATE)
StoodAt(u) == IF u < N THEN 0 ELSE IF u < N + LATE THEN s2 ELSE s3
\* On the pipeline clock (RELEASE = "wall"): the TTS's frozen model places it the stop
\* so far later than the voice clock does. The gate's shift is in R's VPts already.
WallPts(u) == VPts(u) + StoodAt(u)

\* The voice runs unless a pause has stopped it, once what was sent has played.
Running == ~(paused /\ now >= stopAt)

Live == ~cut /\ now < Bound

Tick ==
    /\ Live
    /\ now' = now + 1
    /\ IF Running
         THEN /\ v' = v + 1 /\ stood' = stood
              /\ IF heard < Avail
                   THEN \* the listener starts the next unit: was its word released now?
                        /\ heard' = heard + 1
                        /\ offbeat' = (offbeat \/
                              (IF RELEASE = "voice" THEN VPts(heard) /= v
                                                   ELSE WallPts(heard) /= now))
                   ELSE UNCHANGED <<heard, offbeat>>
         ELSE /\ v' = v /\ stood' = stood + 1 /\ UNCHANGED <<heard, offbeat>>
    /\ UNCHANGED <<paused, stopAt, pauses, held, shift, e2, e3, b2, b3, s2, s3,
                   cut, chart, truth>>

Pause ==
    /\ Live /\ ~paused /\ pauses < MaxPauses
    /\ paused' = TRUE /\ pauses' = pauses + 1
    \* What the transport had sent plays on: at most LAG units of what is there.
    /\ stopAt' = now + Min(LAG, Avail - heard)
    /\ UNCHANGED <<now, v, stood, held, shift, e2, e3, b2, b3, s2, s3, heard, offbeat,
                   cut, chart, truth>>

Resume ==
    /\ Live /\ paused
    /\ paused' = FALSE
    /\ UNCHANGED <<now, v, stood, stopAt, pauses, held, shift, e2, e3, b2, b3, s2, s3,
                   heard, offbeat, cut, chart, truth>>

\* The gate lets R through: its every pts moves by how long it held it, measured on the
\* voice clock or (HOLDCLOCK = "wall") on the pipeline clock. Held from 0, where both
\* clocks read 0.
Release ==
    /\ Live /\ held /\ now <= MaxHold
    /\ held' = FALSE
    /\ shift' = IF HOLDCLOCK = "voice" THEN v ELSE now
    /\ UNCHANGED <<now, v, stood, paused, stopAt, pauses, e2, e3, b2, b3, s2, s3, heard,
                   offbeat, cut, chart, truth>>

\* The TTS's model of R, on the voice clock: its first clause ends at N, plus the hold
\* once over (_freeze_playout "hold" moves _play_end by it); through the hold the model
\* stands still at 0.
TtsNow == IF held THEN 0 ELSE IF STAMP = "voice" THEN v ELSE now
REnd1 == N + (IF held THEN 0 ELSE shift)

\* R's second clause: right after the first, or after the gap the TTS saw. The gate
\* moves it by the shift too (every pts of a held context), so it is placed less it.
Emit2 ==
    /\ Live /\ LATE > 0 /\ e2 = -1
    /\ e2' = now /\ s2' = stood
    /\ b2' = N + Max(0, TtsNow - REnd1)
    /\ UNCHANGED <<now, v, stood, paused, stopAt, pauses, held, shift, e3, b3, s3, heard,
                   offbeat, cut, chart, truth>>

\* Q, after R's context has ended (the TTS drains contexts in order) and past the gate:
\* at R's end on its model (_prev_audio_end_ns), or now if that has played out.
Emit3 ==
    /\ Live /\ QN > 0 /\ e3 = -1 /\ (LATE = 0 \/ e2 >= 0) /\ ~held
    /\ e3' = now /\ s3' = stood
    /\ b3' = Max(TtsNow, IF LATE > 0 THEN b2 + LATE + shift ELSE N + shift)
    /\ UNCHANGED <<now, v, stood, paused, stopAt, pauses, held, shift, e2, b2, s2, heard,
                   offbeat, cut, chart, truth>>

\* The ledger's cut: the words placed before the voice clock's now (only what came to
\* the transport has words downstream), against what the listener heard.
RECURSIVE Count(_)
Count(k) == IF k = 0 THEN 0 ELSE Count(k - 1) + (IF VPts(k - 1) < v THEN 1 ELSE 0)

Cut ==
    /\ Live
    /\ cut' = TRUE /\ chart' = Count(Avail) /\ truth' = heard
    /\ UNCHANGED <<now, v, stood, paused, stopAt, pauses, held, shift, e2, e3, b2, b3, s2,
                   s3, heard, offbeat>>

Next == Tick \/ Pause \/ Resume \/ Release \/ Emit2 \/ Emit3 \/ Cut

Spec == Init /\ [][Next]_vars

WordsOnVoice == ~offbeat
ChartIsHeard == cut => chart = truth

\* The voice clock is the pipeline clock less the time it stood still.
ClockSplit == now = v + stood

TypeOK ==
    /\ now \in 0..Bound /\ v \in 0..Bound /\ stood \in 0..Bound
    /\ pauses \in 0..MaxPauses /\ heard \in 0..Total /\ shift \in 0..Bound
    /\ e2 \in -1..Bound /\ e3 \in -1..Bound
=============================================================================
