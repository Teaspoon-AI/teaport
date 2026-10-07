---------------------------- MODULE PlayoutPause ----------------------------
(***************************************************************************)
(* teaport -- the ledger's heard accounting across a playout PAUSE (#86),   *)
(* and the TTS's playout model when two things stop playout at once.        *)
(*                                                                          *)
(*   brain/teaport_brain/barge_pause.py      -- the pause                   *)
(*   brain/teaport_brain/sip_transport.py    -- set_playout_paused          *)
(*   brain/teaport_brain/transcript_ledger.py -- _pause / _resume / cut     *)
(*   brain/teaport_brain/engine_tts.py       -- _freeze_playout             *)
(*                                                                          *)
(* A sibling of PlayoutClock.tla, for the same reason: LedgerPlayout.tla    *)
(* abstracts the layout's seconds and the pts cut away, and both are the    *)
(* whole question here.                                                     *)
(*                                                                          *)
(* Part 1, the ledger. A reply of A units of audio plays a unit per tick    *)
(* from the window's start; word k is scheduled (pts) at k. The transport   *)
(* can pause at any tick and resume later, up to MaxPauses times, and the   *)
(* caller can cut the reply at any tick. At the cut the ledger charts what   *)
(* it believes played -- seconds from its layout, words from the pts cut -- *)
(* and the truth is what the transport wrote.                                *)
(*                                                                          *)
(* LEDGER = "unaware"  -- the ledger before #86: the layout runs on through  *)
(*                        a pause.                                          *)
(* LEDGER = "noShift"  -- freezes and re-anchors the layout, but reads the   *)
(*                        words' pts as scheduled.                          *)
(* LEDGER = "shipped"  -- freezes at the pause, re-anchors at the resume,    *)
(*                        and moves the pts cut by the pausing it has had.  *)
(*                                                                          *)
(* Part 2, the TTS. A reply hold (reply_hold.py) and a playout pause each   *)
(* stop playout; the two can overlap. engine_tts._freeze_playout freezes its *)
(* playout model through them and, when the LAST one ends, moves the anchor *)
(* of a reply queued behind (_prev_audio_end_ns) by the time since the FIRST *)
(* began. The check is that arithmetic: once nothing stops playout, the     *)
(* anchor has moved by exactly the time playout was stopped.                *)
(*                                                                          *)
(* SLOT = "single" -- #86 as first written: one frozen-since slot, which any *)
(*                    end of a stop clears.                                 *)
(* SLOT = "set"    -- one freeze per source (engine_tts._freezes).          *)
(***************************************************************************)
EXTENDS Integers

CONSTANTS PART,       \* 1: the ledger; 2: the TTS's freeze (checked separately)
          A,          \* units of audio in the reply (word k at pts k, k in 1..A)
          MaxPauses,
          LEDGER,
          SLOT

ASSUME LEDGER \in {"unaware", "noShift", "shipped"} /\ SLOT \in {"single", "set"}

Bound == A + 2 * MaxPauses + 1

VARIABLES
    now,         \* the clock
    written,     \* units the transport has written: the truth
    paused,      \* the transport is not writing
    pauses,
    \* --- the ledger ---
    credited,    \* units credited at the last pause (re-timed layout, as _credit)
    winT0,       \* where the layout's remaining audio starts playing
    pausedAt,    \* -1, or when the pause in force began
    shift,       \* pausing the reply has had: added to the pts cut
    cut,         \* the reply was cut
    chartSecs,   \* at the cut: units the ledger says played
    chartWords,  \* at the cut: words the ledger says were heard
    truth,       \* at the cut: units the transport had written
    \* --- part 2 ---
    clk,               \* part 2's own clock
    holdOn, pauseOn,   \* the two sources currently stopping playout
    sources,           \* engine_tts._freezes (set design)
    frozenAt,          \* -1, or _frozen_clock: when the freeze began
    stopped,           \* ground truth: ticks during which something stopped playout
    moved              \* how far _freeze_playout has moved the queued reply's anchor

vars == <<now, written, paused, pauses, credited, winT0, pausedAt, shift, cut,
          chartSecs, chartWords, truth, clk, holdOn, pauseOn, sources, frozenAt,
          stopped, moved>>
part2 == <<clk, holdOn, pauseOn, sources, frozenAt, stopped, moved>>

Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b

Init ==
    /\ now = 0 /\ written = 0 /\ paused = FALSE /\ pauses = 0
    /\ credited = 0 /\ winT0 = 0 /\ pausedAt = -1 /\ shift = 0
    /\ cut = FALSE /\ chartSecs = -1 /\ chartWords = -1 /\ truth = -1
    /\ clk = 0 /\ holdOn = FALSE /\ pauseOn = FALSE /\ sources = {}
    /\ frozenAt = -1 /\ stopped = 0 /\ moved = 0

\* The ledger's playout clock at `t`: frozen at a pause in force (as designed).
PlayT(t) == IF LEDGER /= "unaware" /\ pausedAt >= 0 THEN Min(t, pausedAt) ELSE t
\* What its layout says has played by `t`: credited, then the rest laid out from winT0.
LayoutPlayed(t) == Min(A, credited + Max(0, PlayT(t) - winT0))
\* Words whose pts the cut reaches (shipped: less the pausing the reply has had).
CutWords(t) == Max(0, Min(A, PlayT(t) - (IF LEDGER = "shipped" THEN shift ELSE 0)))

Live == ~cut /\ now < Bound

\* A tick: the transport writes a unit unless paused or done.
Tick ==
    /\ Live
    /\ now' = now + 1
    /\ written' = IF ~paused /\ written < A THEN written + 1 ELSE written
    /\ UNCHANGED <<paused, pauses, credited, winT0, pausedAt, shift, cut,
                   chartSecs, chartWords, truth, part2>>

Pause ==
    /\ Live /\ ~paused /\ written < A /\ pauses < MaxPauses
    /\ paused' = TRUE /\ pauses' = pauses + 1
    /\ IF LEDGER = "unaware"
         THEN UNCHANGED <<credited, winT0, pausedAt>>
         ELSE \* _pause: credit what played, re-time the rest to now, hold there
              /\ credited' = LayoutPlayed(now) /\ winT0' = now /\ pausedAt' = now
    /\ UNCHANGED <<now, written, shift, cut, chartSecs, chartWords, truth,
                   part2>>

Resume ==
    /\ Live /\ paused
    /\ paused' = FALSE
    /\ IF LEDGER = "unaware"
         THEN UNCHANGED <<winT0, pausedAt, shift>>
         ELSE \* _resume: the rest plays from now; the words play that much later
              /\ winT0' = now /\ pausedAt' = -1
              /\ shift' = shift + (now - pausedAt)
    /\ UNCHANGED <<now, written, pauses, credited, cut, chartSecs, chartWords, truth,
                   part2>>

\* The caller's words cut the reply: the ledger charts it.
Cut ==
    /\ Live
    /\ cut' = TRUE
    /\ chartSecs' = LayoutPlayed(now) /\ chartWords' = CutWords(now) /\ truth' = written
    /\ UNCHANGED <<now, written, paused, pauses, credited, winT0, pausedAt, shift,
                   part2>>

\* --- part 2: the TTS's freeze ---------------------------------------------
Bound2 == 5
Tick2 ==
    /\ clk < Bound2
    /\ clk' = clk + 1
    /\ stopped' = IF holdOn \/ pauseOn THEN stopped + 1 ELSE stopped
    /\ UNCHANGED <<holdOn, pauseOn, sources, frozenAt, moved>>

\* _freeze_playout(src, True): the first freeze notes when.
Stop(src) ==
    /\ IF src = "hold" THEN ~holdOn /\ holdOn' = TRUE /\ UNCHANGED pauseOn
                       ELSE ~pauseOn /\ pauseOn' = TRUE /\ UNCHANGED holdOn
    /\ frozenAt' = IF (SLOT = "set" /\ sources = {}) \/ (SLOT = "single" /\ frozenAt = -1)
                    THEN clk ELSE frozenAt
    /\ sources' = sources \cup {src}
    /\ UNCHANGED <<clk, stopped, moved>>

\* _freeze_playout(src, False): when nothing else freezes (set), or on any end
\* (single), the anchor moves by the time since the freeze began.
Go(src) ==
    /\ IF src = "hold" THEN holdOn /\ holdOn' = FALSE /\ UNCHANGED pauseOn
                       ELSE pauseOn /\ pauseOn' = FALSE /\ UNCHANGED holdOn
    /\ sources' = sources \ {src}
    /\ IF frozenAt /= -1 /\ (SLOT = "single" \/ sources' = {})
         THEN moved' = moved + (clk - frozenAt) /\ frozenAt' = -1
         ELSE UNCHANGED <<moved, frozenAt>>
    /\ UNCHANGED <<clk, stopped>>

Freeze ==
    /\ (\E src \in {"hold", "pause"} : Stop(src) \/ Go(src)) \/ Tick2
    /\ UNCHANGED <<now, written, paused, pauses, credited, winT0, pausedAt, shift, cut,
                   chartSecs, chartWords, truth>>

Next == IF PART = 1 THEN Tick \/ Pause \/ Resume \/ Cut ELSE Freeze

Spec == Init /\ [][Next]_vars

\* At a cut the ledger's heard accounting is exactly what the transport wrote: the
\* layout's seconds AND the words the pts cut keeps, across any number of pauses.
HeardIsPlayed == cut => chartSecs = truth /\ chartWords = truth

\* The TTS's model is frozen whenever anything is stopping playout ...
FrozenWhileStopped == (holdOn \/ pauseOn) => frozenAt /= -1
\* ... and once nothing does, the queued reply's anchor has moved by exactly the time
\* playout was stopped.
AnchorMovesByStop == (~holdOn /\ ~pauseOn) => moved = stopped

TypeOK ==
    /\ now \in 0..Bound /\ written \in 0..A /\ pauses \in 0..MaxPauses
    /\ credited \in 0..A /\ pausedAt \in -1..Bound /\ shift \in 0..Bound
    /\ clk \in 0..Bound2 /\ stopped \in 0..Bound2 /\ moved \in 0..(2 * Bound2)
=============================================================================
