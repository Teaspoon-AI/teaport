---------------------------- MODULE PlayoutPause ----------------------------
(***************************************************************************)
(* teaport -- the ledger's heard accounting across a playout PAUSE (#86),   *)
(* and the TTS's playout model when two things stop playout at once.        *)
(*                                                                          *)
(*   brain/teaport_brain/barge_pause.py      -- the pause                   *)
(*   brain/teaport_brain/sip_transport.py    -- set_playout_paused          *)
(*   brain/teaport_brain/transcript_ledger.py -- _pause / _resume / cut     *)
(*   brain/teaport_brain/engine_tts.py       -- _freeze_playout,            *)
(*                                              _note_playout               *)
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
(* LATE > 0: the reply's last LATE units are a second clause the TTS        *)
(* synthesizes at any tick -- after the first has played out, too (an       *)
(* underrun: the LLM's next sentence is slow). The TTS places its words at  *)
(* the context's audio so far plus the playout gap it saw (_note_playout,   *)
(* on its own clock, which stands still while playout is stopped).          *)
(* TTSGAP = "ahead" -- before the fix: _freeze_playout moved its _play_end *)
(*                      by the stop only when that was still in the future *)
(*                      at the stop.                                        *)
(* TTSGAP = "moved" -- as shipped: it always moves it, so a gap never       *)
(*                      includes stop time.                                 *)
(*                                                                          *)
(* Part 2, the TTS. A reply hold (reply_hold.py) and a playout pause each   *)
(* stop playout; the two can overlap. engine_tts._freeze_playout freezes its *)
(* playout model through them and, when the LAST one ends, moves the anchor *)
(* of a reply queued behind (_prev_audio_end_ns) by the time since the FIRST *)
(* began. The check is that arithmetic: once nothing stops playout, the     *)
(* anchor has moved by exactly the time playout was stopped with the        *)
(* reply's audio waiting to play.                                           *)
(*                                                                          *)
(* SLOT = "single" -- #86 as first written: one frozen-since slot, which any *)
(*                    end of a stop clears.                                 *)
(* SLOT = "set"    -- one freeze per source (engine_tts._freezes).          *)
(* FIRSTAUDIO = "before" -- the stopped reply's audio was queued before any *)
(*                    stop began (a pause mid-reply).                       *)
(* FIRSTAUDIO = "any"    -- it may come during the stop (a hold armed at the *)
(*                    commit, before the reply's first audio).              *)
(* ANCHOR = "stop"  -- as shipped: an ended context's anchor moves by the   *)
(*                    whole stop (`now - then`).                            *)
(* ANCHOR = "audio" -- PROPOSED, not code: by the stop since its first      *)
(*                    audio, as the in-progress path (_ctx_held_ns) does.   *)
(***************************************************************************)
EXTENDS Integers, Sequences

CONSTANTS PART,       \* 1: the ledger; 2: the TTS's freeze (checked separately)
          A,          \* units of audio in the reply (word k at pts k, k in 1..A)
          MaxPauses,
          LEDGER,
          SLOT,
          LATE,       \* units of the reply synthesized late, as a second clause
          TTSGAP,
          FIRSTAUDIO,
          ANCHOR

ASSUME /\ LEDGER \in {"unaware", "noShift", "shipped"} /\ SLOT \in {"single", "set"}
       /\ TTSGAP \in {"ahead", "moved"} /\ LATE \in 0..(A - 1)
       /\ FIRSTAUDIO \in {"before", "any"} /\ ANCHOR \in {"stop", "audio"}

A1 == A - LATE                      \* the first clause: emitted at 0
Bound == A + 2 * MaxPauses + 1 + (IF LATE > 0 THEN 2 ELSE 0)

VARIABLES
    now,         \* the clock
    written,     \* units the transport has written: the truth
    emitted,     \* units the TTS has pushed (A1, then A once the second clause comes)
    paused,      \* the transport is not writing
    pauses,
    \* --- the ledger ---
    credited,    \* units credited at the last pause (re-timed layout, as _credit)
    items,       \* the layout's queued pushes: <<[n, at]>> (units, push time)
    winT0,       \* where the layout's remaining audio starts playing
    pausedAt,    \* -1, or when the pause in force began
    shift,       \* pausing the reply has had: added to the pts cut
    cut,         \* the reply was cut
    chartSecs,   \* at the cut: units the ledger says played
    chartWords,  \* at the cut: words the ledger says were heard
    truth,       \* at the cut: units the transport had written
    \* --- the TTS (part 1) ---
    ttsEnd,      \* _play_end: when the audio it has emitted finishes playing
    ttsFrozenAt, \* -1, or _frozen_at
    base2,       \* -1, or the second clause's word base (its word j at base2 + j)
    \* --- part 2 ---
    clk,               \* part 2's own clock
    holdOn, pauseOn,   \* the two sources currently stopping playout
    sources,           \* engine_tts._freezes (set design)
    frozenAt,          \* -1, or _frozen_clock: when the freeze began
    audioAt,           \* -1, or when the stopped reply's first audio was queued
    ctxEnded,          \* that reply's context has ended (its anchor is _prev_audio_end_ns)
    stopped,           \* ground truth: ticks playout was stopped with its audio waiting
    moved              \* how far _freeze_playout has moved the queued reply's anchor

vars == <<now, written, emitted, paused, pauses, credited, items, winT0, pausedAt, shift,
          cut, chartSecs, chartWords, truth, ttsEnd, ttsFrozenAt, base2,
          clk, holdOn, pauseOn, sources, frozenAt, audioAt, ctxEnded, stopped, moved>>
part1 == <<now, written, emitted, paused, pauses, credited, items, winT0, pausedAt, shift,
           cut, chartSecs, chartWords, truth, ttsEnd, ttsFrozenAt, base2>>
part2 == <<clk, holdOn, pauseOn, sources, frozenAt, audioAt, ctxEnded, stopped, moved>>

Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b

Init ==
    /\ now = 0 /\ written = 0 /\ emitted = A1 /\ paused = FALSE /\ pauses = 0
    /\ credited = 0 /\ items = <<[n |-> A1, at |-> 0]>> /\ winT0 = 0 /\ pausedAt = -1
    /\ shift = 0 /\ cut = FALSE /\ chartSecs = -1 /\ chartWords = -1 /\ truth = -1
    /\ ttsEnd = A1 /\ ttsFrozenAt = -1 /\ base2 = -1
    /\ clk = 0 /\ holdOn = FALSE /\ pauseOn = FALSE /\ sources = {}
    /\ frozenAt = -1 /\ audioAt = IF FIRSTAUDIO = "before" THEN 0 ELSE -1
    /\ ctxEnded = (FIRSTAUDIO = "before") /\ stopped = 0 /\ moved = 0

\* The ledger's playout clock at `t`: frozen at a pause in force (as designed).
PlayT(t) == IF LEDGER /= "unaware" /\ pausedAt >= 0 THEN Min(t, pausedAt) ELSE t
\* _layout: the queued pushes back to back from winT0, none before it was pushed.
Start1 == Max(winT0, items[1].at)
Played1(t) == IF items = <<>> THEN 0 ELSE Min(items[1].n, Max(0, t - Start1))
Played2(t) == IF Len(items) < 2 THEN 0
              ELSE Min(items[2].n, Max(0, t - Max(Start1 + items[1].n, items[2].at)))
\* What its layout says has played by `t`: credited, then the rest laid out.
LayoutPlayed(t) == credited + Played1(PlayT(t)) + Played2(PlayT(t))
\* _credit at `t`: what is still to play, re-timed to play from `t`.
Rest(t) == LET r1 == IF items = <<>> THEN <<>>
                     ELSE IF Played1(t) < items[1].n
                          THEN <<[n |-> items[1].n - Played1(t), at |-> t]>> ELSE <<>>
               r2 == IF Len(items) < 2 THEN <<>>
                     ELSE IF Played2(t) < items[2].n
                          THEN <<[n |-> items[2].n - Played2(t), at |-> t]>> ELSE <<>>
           IN r1 \o r2
\* Words of n placed from base b whose pts the cut x reaches.
Cnt(n, b, x) == Max(0, Min(n, x - b))
\* Words whose pts the cut reaches (shipped: less the pausing the reply has had).
CutWords(t) == LET x == PlayT(t) - (IF LEDGER = "shipped" THEN shift ELSE 0)
               IN Cnt(A1, 0, x) + (IF base2 >= 0 THEN Cnt(LATE, base2, x) ELSE 0)

Live == ~cut /\ now < Bound

\* A tick: the transport writes a unit unless paused or it has nothing to write.
Tick ==
    /\ Live
    /\ now' = now + 1
    /\ written' = IF ~paused /\ written < emitted THEN written + 1 ELSE written
    /\ UNCHANGED <<emitted, paused, pauses, credited, items, winT0, pausedAt, shift, cut,
                   chartSecs, chartWords, truth, ttsEnd, ttsFrozenAt, base2, part2>>

\* The second clause: _note_playout sees the gap since its playout model ran dry
\* (on its clock, frozen while playout is stopped) and its words follow the gap.
Emit2 ==
    /\ Live /\ LATE > 0 /\ base2 = -1
    /\ LET ttsNow == IF ttsFrozenAt >= 0 THEN ttsFrozenAt ELSE now
       IN /\ base2' = A1 + Max(0, ttsNow - ttsEnd)
          /\ ttsEnd' = Max(ttsEnd, ttsNow) + LATE
    /\ emitted' = A
    /\ items' = Append(items, [n |-> LATE, at |-> now])      \* the ledger's _push
    /\ UNCHANGED <<now, written, paused, pauses, credited, winT0, pausedAt, shift, cut,
                   chartSecs, chartWords, truth, ttsFrozenAt, part2>>

Pause ==
    /\ Live /\ ~paused /\ written < A /\ pauses < MaxPauses
    /\ paused' = TRUE /\ pauses' = pauses + 1
    /\ ttsFrozenAt' = now                                   \* the TTS freezes too
    /\ IF LEDGER = "unaware"
         THEN UNCHANGED <<credited, items, winT0, pausedAt>>
         ELSE \* _pause: credit what played, re-time the rest to now, hold there
              /\ credited' = LayoutPlayed(now) /\ items' = Rest(now)
              /\ winT0' = now /\ pausedAt' = now
    /\ UNCHANGED <<now, written, emitted, shift, cut, chartSecs, chartWords, truth,
                   ttsEnd, base2, part2>>

Resume ==
    /\ Live /\ paused
    /\ paused' = FALSE
    /\ ttsFrozenAt' = -1
    \* _freeze_playout: what was emitted and not yet played plays that much later.
    /\ ttsEnd' = IF TTSGAP = "moved" \/ ttsEnd > ttsFrozenAt
                 THEN ttsEnd + (now - ttsFrozenAt) ELSE ttsEnd
    /\ IF LEDGER = "unaware"
         THEN UNCHANGED <<winT0, pausedAt, shift>>
         ELSE \* _resume: the rest plays from now; the words play that much later
              /\ winT0' = now /\ pausedAt' = -1
              /\ shift' = shift + (now - pausedAt)
    /\ UNCHANGED <<now, written, emitted, pauses, credited, items, cut, chartSecs, chartWords,
                   truth, base2, part2>>

\* The caller's words cut the reply: the ledger charts it.
Cut ==
    /\ Live
    /\ cut' = TRUE
    /\ chartSecs' = LayoutPlayed(now) /\ chartWords' = CutWords(now) /\ truth' = written
    /\ UNCHANGED <<now, written, emitted, paused, pauses, credited, items, winT0, pausedAt,
                   shift, ttsEnd, ttsFrozenAt, base2, part2>>

\* --- part 2: the TTS's freeze ---------------------------------------------
Bound2 == 5
Tick2 ==
    /\ clk < Bound2
    /\ clk' = clk + 1
    /\ stopped' = IF (holdOn \/ pauseOn) /\ audioAt >= 0 THEN stopped + 1 ELSE stopped
    /\ UNCHANGED <<holdOn, pauseOn, sources, frozenAt, audioAt, ctxEnded, moved>>

\* The stopped reply's first audio reaches the TTS's worker (its word baseline is set
\* now); later its context ends, and its end becomes _prev_audio_end_ns.
FirstAudio ==
    /\ audioAt = -1
    /\ audioAt' = clk
    /\ UNCHANGED <<clk, holdOn, pauseOn, sources, frozenAt, ctxEnded, stopped, moved>>
CtxEnd ==
    /\ audioAt >= 0 /\ ~ctxEnded
    /\ ctxEnded' = TRUE
    /\ UNCHANGED <<clk, holdOn, pauseOn, sources, frozenAt, audioAt, stopped, moved>>

\* _freeze_playout(src, True): the first freeze notes when.
Stop(src) ==
    /\ IF src = "hold" THEN ~holdOn /\ holdOn' = TRUE /\ UNCHANGED pauseOn
                       ELSE ~pauseOn /\ pauseOn' = TRUE /\ UNCHANGED holdOn
    /\ frozenAt' = IF (SLOT = "set" /\ sources = {}) \/ (SLOT = "single" /\ frozenAt = -1)
                    THEN clk ELSE frozenAt
    /\ sources' = sources \cup {src}
    /\ UNCHANGED <<clk, audioAt, ctxEnded, stopped, moved>>

\* _freeze_playout(src, False): when nothing else freezes (set), or on any end
\* (single), the anchor moves: an ended context's _prev_audio_end_ns by the time
\* since the freeze began (`now - then`); one still in progress by the time since
\* max(then, its baseline) (_ctx_held_ns, added at its end). No audio yet: nothing
\* of it is anchored, nothing moves.
Delta == IF audioAt < 0 THEN 0
         ELSE IF ANCHOR = "stop" /\ ctxEnded THEN clk - frozenAt
         ELSE clk - Max(frozenAt, audioAt)
Go(src) ==
    /\ IF src = "hold" THEN holdOn /\ holdOn' = FALSE /\ UNCHANGED pauseOn
                       ELSE pauseOn /\ pauseOn' = FALSE /\ UNCHANGED holdOn
    /\ sources' = sources \ {src}
    /\ IF frozenAt /= -1 /\ (SLOT = "single" \/ sources' = {})
         THEN moved' = moved + Delta /\ frozenAt' = -1
         ELSE UNCHANGED <<moved, frozenAt>>
    /\ UNCHANGED <<clk, audioAt, ctxEnded, stopped>>

Freeze ==
    /\ \/ \E src \in {"hold", "pause"} : Stop(src) \/ Go(src)
       \/ Tick2 \/ FirstAudio \/ CtxEnd
    /\ UNCHANGED part1

Next == IF PART = 1 THEN Tick \/ Emit2 \/ Pause \/ Resume \/ Cut ELSE Freeze

Spec == Init /\ [][Next]_vars

\* At a cut the ledger's heard accounting is exactly what the transport wrote: the
\* layout's seconds AND the words the pts cut keeps, across any number of pauses.
HeardIsPlayed == cut => chartSecs = truth /\ chartWords = truth

\* The TTS's model is frozen whenever anything is stopping playout ...
FrozenWhileStopped == (holdOn \/ pauseOn) => frozenAt /= -1
\* ... and once nothing does, the queued reply's anchor has moved by exactly the time
\* playout was stopped with the reply's audio waiting.
AnchorMovesByStop == (~holdOn /\ ~pauseOn) => moved = stopped

TypeOK ==
    /\ now \in 0..Bound /\ written \in 0..A /\ emitted \in {A1, A}
    /\ pauses \in 0..MaxPauses /\ credited \in 0..A /\ pausedAt \in -1..Bound
    /\ shift \in 0..Bound /\ base2 \in -1..(A + Bound) /\ ttsEnd \in 0..(A + 2 * Bound)
    /\ clk \in 0..Bound2 /\ stopped \in 0..Bound2 /\ moved \in 0..(2 * Bound2)
=============================================================================
