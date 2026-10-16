--------------------------- MODULE LedgerPlayout ---------------------------
(***************************************************************************)
(* teaport -- TranscriptLedger's bot-turn state machine: the PLAYOUT design *)
(* (transcript_ledger.py after the review of PR #13).                       *)
(*                                                                          *)
(* Ledger.tla keeps the two designs this one replaced (asWritten and        *)
(* windowHead) with their counterexamples. The environment here is that     *)
(* module's -- the LLM stream, the TTS's sequential synthesis, the          *)
(* transport's push-order playout, the user's barge-in -- with the facts    *)
(* the design rests on spelled out (pipecat 1.8.1, engine_tts.py):         *)
(*                                                                          *)
(*   - A context opens (TTSStartedFrame) for ANY text with a non-blank      *)
(*     character: tts_service._push_tts_frames creates the audio context    *)
(*     and queues the started frame BEFORE run_tts, so text with nothing    *)
(*     synthesizable ("...", an emoji) opens a context that never yields    *)
(*     audio (UnspeakableOpens; the model's first cut assumed it opened     *)
(*     none). Such a context, and any whose LAST run_tts yielded no audio,  *)
(*     is not closed at the End: it ends on the stop-frame timeout.         *)
(*   - Every context that opened ends with a TTSStoppedFrame naming it      *)
(*     (push_stop_frames=True since PR #13's merge): appended behind its    *)
(*     last chunk, or pushed on the stop-frame timeout -- mid-reply too,    *)
(*     when the LLM is silent that long. The ledger takes it as "this       *)
(*     turn's synthesis is over". The output transport queues it behind     *)
(*     the audio and ends bot-speaking when it reaches the queue's head (if *)
(*     audio played since the last stop): BotStoppedSpeaking now comes at   *)
(*     the end of EACH context, possibly with the next context's chunks     *)
(*     still queued, and on the 3 s idle fallback when synthesis stalls.    *)
(*   - After the stop, the TTS re-pushes the response's LLMFullResponseEnd  *)
(*     frame, the SAME frame (tts_service._maybe_reset_word_timestamps):    *)
(*     its second sighting names the response the context spoke. It comes   *)
(*     only if the End had reached the TTS before the context ended; a      *)
(*     context that ended on the timeout while the LLM still streams gets a *)
(*     fresh End the ledger ignores, and is re-created under the SAME id    *)
(*     when the LLM's next sentence reaches the TTS (Resume).               *)
(*   - Every TTS push is tagged with its context (engine_tts.py yields      *)
(*     audio with context_id); the transport's untagged rebuild of each     *)
(*     chunk it played is recognised and ignored. The untagged legacy path  *)
(*     is out of scope here.                                                *)
(*                                                                          *)
(* The ledger: turns open only on the TTS's own frames, never on the        *)
(* anonymous BotStartedSpeaking; a turn claims the oldest expected context  *)
(* (the queue of ended responses), else the one streaming; several turns    *)
(* may be open at once; a turn's audio_start is its own chunk's place in    *)
(* the transport's queue; a window closing credits the queued chunks that   *)
(* have played by then and charts, oldest first, the turns whose audio has  *)
(* all played and whose synthesis is over; a context starting proves every  *)
(* OTHER open context ended; a context's stop frame says its synthesis is   *)
(* over; the drain re-push confirms or corrects a turn's response; an       *)
(* interruption charts every open turn.                                     *)
(*                                                                          *)
(* Everything numeric is abstracted away, as in Ledger.tla: what is kept    *)
(* is which turn a chunk is attributed to, whether a turn's playout start   *)
(* is known, which response's text it claims, and whether it is charted    *)
(* cut or complete. The ledger's playout layout (seconds, laid out from the *)
(* window's start) is taken to be accurate: at a window close or a cut it   *)
(* credits exactly the chunks the transport has played (PlayoutPause.tla   *)
(* checks its arithmetic).                                                  *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Replies,       \* reply ids; each is also its TTS context id
          Fillers,       \* filler context ids -- narrator / tool-ack TTSSpeakFrames
          Unspeakable,   \* replies with text but nothing synthesizable (no audio, ever)
          UnspeakableOpens, \* BOOLEAN: pipecat opens a context for an Unspeakable reply
                         \*   (TRUE: 1.8.1 as it is); FALSE is the premise the model was
                         \*   first written with -- no context at all
          LedgerSpeakCheck, \* BOOLEAN: the ledger applies the engine's has_speech
                         \*   before queuing a response (transcript_ledger._llm_side,
                         \*   as shipped); FALSE: it queues every response with text
          Drain,         \* BOOLEAN: the live wiring (tts given: the End re-push is seen);
                         \*   FALSE is the hermetic tests' -- no drain signal, a turn is
                         \*   taken as synthesized once its response has ended
          Resume,        \* BOOLEAN: a context that ended on the timeout mid-reply may resume
          MaxChunks,     \* audio pushes per context (2 makes a stall mid-reply reachable)
          MaxInterrupts,
          MaxFillerCtxs  \* the 512 in the TTSStarted branch, shrunk to make overflow reachable

NONE == "none"
OWN  == "own"            \* audio_start: the turn's own chunk's place in the playout layout
Ctxs == Replies \cup Fillers
Range(s) == {s[i] : i \in DOMAIN s}
Remove(s, x) == SelectSeq(s, LAMBDA y: y # x)
Drop(s, k) == SubSeq(s, 1, k - 1) \o SubSeq(s, k + 1, Len(s))

VARIABLES
    \* --- the pipeline: ground truth ---
    llm,         \* [Replies -> idle | streaming | done | aborted | cancelled]
    order,       \* replies in the order their completions started
    synth,       \* [Ctxs -> idle | started | audio | stopped | aborted]
    timedOut,    \* [Ctxs -> BOOLEAN]: stopped on the timeout, its reply still streaming
    drainPending,\* [Replies -> BOOLEAN]: stopped; its End re-push not yet sighted
    chunks,      \* [Ctxs -> Nat]: audio chunks pushed
    playedN,     \* [Ctxs -> Nat]: audio chunks the transport has played
    pq,          \* the transport's queue, push order: [k |-> "audio"|"stop", c |-> ctx]
    window,      \* the transport's bot-speaking window is open
    interrupts,
    \* --- the ledger: transcript_ledger.py ---
    genAcc,      \* _gen          : NONE or the reply streaming now
    ended,       \* replies whose End the TTS sighted with text (entry["ended"])
    claimed,     \* replies some turn has taken (entry["turn"] is set)
    queue,       \* _queue        : ended replies no context has claimed, oldest first
    turns,       \* _turns        : open turns, playout order (see Opened)
    fifo,        \* _fifo         : contexts of the chunks pushed and not yet credited
    winOpen,     \* _win_t0 is not None
    fillerCtxs,  \* _filler_ctxs  : in insertion order
    closedCtxs,  \* _closed_ctxs
    charted      \* self.events, assistant utterances only

envVars    == <<llm, order, synth, timedOut, drainPending, chunks, playedN, pq, window, interrupts>>
ledgerVars == <<genAcc, ended, claimed, queue, turns, fifo, winOpen, fillerCtxs, closedCtxs, charted>>
vars       == <<llm, order, synth, timedOut, drainPending, chunks, playedN, pq, window, interrupts,
                genAcc, ended, claimed, queue, turns, fifo, winOpen, fillerCtxs, closedCtxs, charted>>

Init ==
    /\ llm = [r \in Replies |-> "idle"] /\ order = <<>>
    /\ synth = [c \in Ctxs |-> "idle"] /\ timedOut = [c \in Ctxs |-> FALSE]
    /\ drainPending = [r \in Replies |-> FALSE]
    /\ chunks = [c \in Ctxs |-> 0] /\ playedN = [c \in Ctxs |-> 0]
    /\ pq = <<>> /\ window = FALSE /\ interrupts = 0
    /\ genAcc = NONE /\ ended = {} /\ claimed = {} /\ queue = <<>> /\ turns = <<>>
    /\ fifo = <<>> /\ winOpen = FALSE /\ fillerCtxs = <<>> /\ closedCtxs = {}
    /\ charted = <<>>

(* ---------------- the ledger's transition function ---------------- *)

\* Ground truth snapshotted into the chart, for the properties.
AnyPlayed(g) == playedN[g] > 0
AllPlayed(g) == chunks[g] > 0 /\ playedN[g] = chunks[g]
\* `stopping` is the context the same step ends (its synth is "stopped" in the
\* post-state; the chart is taken in the pre-state). Complete: no more of its audio
\* can come -- a context that ended on the timeout while its LLM still streams may
\* resume (Resume).
GenComplete(g, stopping) ==
    /\ synth[g] = "stopped" \/ g = stopping
    /\ ~(Resume /\ timedOut[g] /\ llm[g] = "streaming")
Chart(t, intr, stopping) ==
    [gen |-> t.gen, ctx |-> t.ctx, audioStart |-> t.audioStart, intr |-> intr,
     anyPlayed |-> AnyPlayed(t.gen), allPlayed |-> AllPlayed(t.gen),
     complete |-> GenComplete(t.gen, stopping)]
\* _finish charts nothing for a turn that holds no text (an anonymous turn).
Named(ts) == SelectSeq(ts, LAMBDA t: t.gen # NONE)
ChartsAs(ts, intr, stopping) == [i \in DOMAIN Named(ts) |-> Chart(Named(ts)[i], intr, stopping)]
\* At a cut, a turn whose synthesis is over and whose every chunk had played by the
\* cut (the layout's portion up to now, _interrupt) is charted complete, not cut.
CutComplete(t) == t.synthDone /\ t.qa /\ playedN[t.ctx] = chunks[t.ctx]
ChartsCut(ts) == [i \in DOMAIN Named(ts) |->
                    Chart(Named(ts)[i], ~CutComplete(Named(ts)[i]), NONE)]
CtxsOf(ts) == {t.ctx : t \in Range(ts)}
StopOf(c) == IF c \in Replies THEN c ELSE NONE

HasOpen(c) == \E i \in DOMAIN turns : turns[i].ctx = c
TurnOf(c) == CHOOSE i \in DOMAIN turns : turns[i].ctx = c   \* one open turn per context

\* _open_turn: what a turn opening for context c claims -- the oldest expected
\* context, else the reply streaming now if no turn took it yet, else nothing.
\* A context re-created after its turn was charted (Resume) claims nothing.
ClaimOf(c) ==
    IF c \in closedCtxs THEN NONE
    ELSE IF queue # <<>> THEN Head(queue)
    ELSE IF genAcc # NONE /\ genAcc \notin claimed THEN genAcc
    ELSE NONE
Opened(c, qa) ==
    [ctx |-> c, gen |-> ClaimOf(c), qa |-> qa, playedAll |-> FALSE, synthDone |-> FALSE,
     audioStart |-> IF qa /\ winOpen THEN OWN ELSE NONE]
QueueAfterOpen(c) == IF c \notin closedCtxs /\ queue # <<>> THEN Tail(queue) ELSE queue
ClaimedAfterOpen(c) == claimed \cup ({ClaimOf(c)} \ {NONE})

\* _context_started: every open turn of ANOTHER context is fully synthesized
\* (the TTS drains one context at a time); one with no chunk yet never gets
\* one, and is charted never-played -- a cut heard nothing of.
Marked(ts, c) == [i \in DOMAIN ts |-> IF ts[i].ctx = c THEN ts[i]
                                      ELSE [ts[i] EXCEPT !.synthDone = TRUE]]
Never(ts, c) == SelectSeq(ts, LAMBDA t: t.ctx # c /\ ~t.qa)
Survive(ts, c) == SelectSeq(Marked(ts, c), LAMBDA t: t.ctx = c \/ t.qa)

\* _close_ready: chart the turns that are over, oldest first, up to the first
\* that is not (its synthesis stalled, or its first chunk has not come).
\* Hermetic wiring (no drain signal): a turn is taken as synthesized once its
\* response has ended -- transcript_ledger._close_ready's `self._tts is None` arm.
Ready(t) == t.qa /\ t.playedAll /\ (t.gen = NONE \/ t.synthDone \/ (~Drain /\ t.gen \in ended))
ReadyLen(ts) == CHOOSE n \in 0..Len(ts) :
                  /\ \A i \in 1..n : Ready(ts[i])
                  /\ (n = Len(ts) \/ ~Ready(ts[n + 1]))
Done(ts) == SubSeq(ts, 1, ReadyLen(ts))
Kept(ts) == SubSeq(ts, ReadyLen(ts) + 1, Len(ts))

WithChunk(ts, c) ==   \* _push for an open turn: its (next) chunk is queued, not yet played
    [i \in DOMAIN ts |-> IF ts[i].ctx = c
                         THEN [ts[i] EXCEPT !.qa = TRUE, !.playedAll = FALSE,
                                            !.audioStart = IF winOpen /\ @ = NONE THEN OWN ELSE @]
                         ELSE ts[i]]

\* The audio still queued at the transport, in order: what a credit leaves in _fifo.
PqAudio == LET a == SelectSeq(pq, LAMBDA e: e.k = "audio") IN [i \in DOMAIN a |-> a[i].c]

Ledger(f) ==
  CASE f.type = "LLMStart" ->                    \* LLMFullResponseStart + LLMText, at the TTS
         /\ genAcc' = f.gen
         /\ UNCHANGED <<ended, claimed, queue, turns, fifo, winOpen, fillerCtxs, closedCtxs, charted>>
    [] f.type = "LLMEnd" ->                      \* LLMFullResponseEnd, at the TTS
         \* A cancelled completion's End finds no stream (genAcc cleared at the
         \* interruption) and is ignored; a response a live turn already took
         \* is not queued; any other ended response is an expected context.
         \* The ledger cannot tell WHOSE End this is: it ends the stream it holds
         \* (_llm_side: `gen, self._gen = self._gen, None`); a cancelled
         \* completion's End finds none and is ignored -- it arrives before any new
         \* stream begins, see LlmStart.
         IF genAcc = NONE THEN UNCHANGED ledgerVars
         ELSE /\ ended' = ended \cup {genAcc}
              \* LedgerSpeakCheck: text the engine will not synthesize is not an
              \* expected context (tts_text.has_speech is the engine's predicate).
              /\ queue' = IF genAcc \in claimed \/ (LedgerSpeakCheck /\ genAcc \in Unspeakable)
                            THEN queue ELSE Append(queue, genAcc)
              /\ genAcc' = NONE
              /\ UNCHANGED <<claimed, turns, fifo, winOpen, fillerCtxs, closedCtxs, charted>>
    [] f.type = "TTSStarted" ->
         LET never == Never(turns, f.ctx)
             kept  == Survive(turns, f.ctx)
         IN /\ charted' = charted \o ChartsAs(never, TRUE, NONE)
            /\ closedCtxs' = closedCtxs \cup CtxsOf(never)
            /\ IF f.filler THEN
                  /\ fillerCtxs' = IF f.ctx \in Range(fillerCtxs) THEN fillerCtxs
                                   ELSE (IF Len(fillerCtxs) >= MaxFillerCtxs
                                         THEN Tail(fillerCtxs) ELSE fillerCtxs) \o <<f.ctx>>
                  /\ turns' = kept
                  /\ UNCHANGED <<queue, claimed>>
               ELSE IF HasOpen(f.ctx) THEN        \* the same context, re-created: the same turn
                  /\ turns' = kept
                  /\ UNCHANGED <<queue, claimed, fillerCtxs>>
               ELSE
                  /\ turns' = Append(kept, Opened(f.ctx, FALSE))
                  /\ queue' = QueueAfterOpen(f.ctx)
                  /\ claimed' = ClaimedAfterOpen(f.ctx)
                  /\ UNCHANGED fillerCtxs
            /\ UNCHANGED <<genAcc, ended, fifo, winOpen>>
    [] f.type = "TTSAudio" ->                    \* the TTS's tagged push
         /\ fifo' = Append(fifo, f.ctx)
         /\ IF f.ctx \in Range(fillerCtxs) THEN
               UNCHANGED <<turns, queue, claimed>>
            ELSE IF HasOpen(f.ctx) THEN
               /\ turns' = WithChunk(turns, f.ctx)
               /\ UNCHANGED <<queue, claimed>>
            ELSE                                 \* no started frame seen: open on the audio
               /\ turns' = Append(turns, Opened(f.ctx, TRUE))
               /\ queue' = QueueAfterOpen(f.ctx)
               /\ claimed' = ClaimedAfterOpen(f.ctx)
         /\ UNCHANGED <<genAcc, ended, winOpen, fillerCtxs, closedCtxs, charted>>
    [] f.type = "TTSStopped" ->                  \* the context's stop frame, at the TTS's output
         \* A filler's: forget it. Otherwise only THIS context's turn is told its
         \* synthesis is over; with no chunk it never played, and is charted so. No
         \* _close_ready here: the window close behind it, or the drain, charts it.
         IF f.ctx \in Range(fillerCtxs) THEN
            /\ fillerCtxs' = Remove(fillerCtxs, f.ctx)
            /\ UNCHANGED <<genAcc, ended, claimed, queue, turns, fifo, winOpen, closedCtxs, charted>>
         ELSE IF HasOpen(f.ctx) THEN
            LET k == TurnOf(f.ctx)
                t == [turns[k] EXCEPT !.synthDone = TRUE]
            IN /\ IF ~t.qa THEN
                     /\ charted' = charted \o ChartsAs(<<t>>, TRUE, StopOf(f.ctx))
                     /\ turns' = Drop(turns, k)
                     /\ closedCtxs' = closedCtxs \cup {f.ctx}
                  ELSE
                     /\ turns' = [turns EXCEPT ![k] = t]
                     /\ UNCHANGED <<charted, closedCtxs>>
               /\ UNCHANGED <<genAcc, ended, claimed, queue, fifo, winOpen, fillerCtxs>>
         ELSE UNCHANGED ledgerVars
    [] f.type = "PlayStart" ->                   \* BotStartedSpeaking: anonymous; dates the window
         IF winOpen THEN UNCHANGED ledgerVars    \* a window still open (audio queued past a stop)
         ELSE
         /\ winOpen' = TRUE
         /\ turns' = [i \in DOMAIN turns |->
                        IF turns[i].ctx \in Range(fifo) /\ turns[i].audioStart = NONE
                        THEN [turns[i] EXCEPT !.audioStart = OWN] ELSE turns[i]]
         /\ UNCHANGED <<genAcc, ended, claimed, queue, fifo, fillerCtxs, closedCtxs, charted>>
    [] f.type = "PlayChunk" ->                   \* the transport's untagged copy: ignored
         UNCHANGED ledgerVars
    [] f.type = "PlayStop" ->                    \* BotStoppedSpeaking: credit what has played
         \* _credit: every queued chunk that has played is credited; what is still
         \* queued stays, re-timed, and the window stays open for it.
         LET ts == [i \in DOMAIN turns |->
                      IF turns[i].ctx \in Range(fifo)
                      THEN [turns[i] EXCEPT !.playedAll = (playedN[turns[i].ctx] = chunks[turns[i].ctx])]
                      ELSE turns[i]]
         IN /\ turns' = Kept(ts)
            /\ charted' = charted \o ChartsAs(Done(ts), FALSE, NONE)
            /\ closedCtxs' = closedCtxs \cup CtxsOf(Done(ts))
            /\ fifo' = PqAudio /\ winOpen' = (PqAudio # <<>>)
            /\ UNCHANGED <<genAcc, ended, claimed, queue, fillerCtxs>>
    [] f.type = "Drained" ->                     \* the response's End, sighted again below the TTS
         IF \/ LedgerSpeakCheck /\ f.gen \in Unspeakable   \* never in _end_ids
            \/ f.gen \in claimed /\ ~(\E i \in DOMAIN turns : turns[i].gen = f.gen)
         THEN UNCHANGED ledgerVars               \* its turn is charted already
         ELSE IF turns = <<>> THEN
            /\ queue' = Remove(queue, f.gen)
            /\ UNCHANGED <<genAcc, ended, claimed, turns, fifo, winOpen, fillerCtxs, closedCtxs, charted>>
         ELSE
            \* Unclaimed yet its context drained: the newest open turn is this
            \* response's, whatever it claimed by order -- that is let go (its
            \* entry["turn"] = None: queued again if its End is still to come).
            LET own == \E i \in DOMAIN turns : turns[i].gen = f.gen
                old == turns[Len(turns)].gen
                tsA == IF own THEN turns ELSE [turns EXCEPT ![Len(turns)].gen = f.gen]
                tsB == [i \in DOMAIN tsA |-> IF tsA[i].gen = f.gen
                                             THEN [tsA[i] EXCEPT !.synthDone = TRUE] ELSE tsA[i]]
                k   == CHOOSE i \in DOMAIN tsB : tsB[i].gen = f.gen
            IN /\ queue' = Remove(queue, f.gen)
               /\ claimed' = (IF own THEN claimed ELSE claimed \ {old}) \cup {f.gen}
               /\ IF ~tsB[k].qa THEN            \* drained with no audio: never played
                     /\ charted' = charted \o <<Chart(tsB[k], TRUE, f.gen)>>
                     /\ turns' = Drop(tsB, k)
                     /\ closedCtxs' = closedCtxs \cup {tsB[k].ctx}
                  ELSE                          \* its window may have closed already
                     /\ charted' = charted \o ChartsAs(Done(tsB), FALSE, f.gen)
                     /\ turns' = Kept(tsB)
                     /\ closedCtxs' = closedCtxs \cup CtxsOf(Done(tsB))
               /\ UNCHANGED <<genAcc, ended, fifo, winOpen, fillerCtxs>>
    [] f.type = "Interruption" ->
         /\ charted' = charted \o ChartsCut(turns)
         /\ closedCtxs' = closedCtxs \cup CtxsOf(turns)
         /\ turns' = <<>> /\ genAcc' = NONE /\ queue' = <<>>
         /\ fifo' = <<>> /\ winOpen' = FALSE
         /\ UNCHANGED <<ended, claimed, fillerCtxs>>

(* ---------------- the pipeline ---------------- *)

Streaming     == \E r \in Replies : llm[r] = "streaming"
Synthesizing  == \E c \in Ctxs : synth[c] \in {"started", "audio"}
Draining      == \E r \in Replies : drainPending[r]

\* A cancelled completion's End is pushed from its finally at cancellation, before
\* any new run can start: no completion begins while one is "aborted". (Without
\* this the ledger, which cannot tell whose End it is, closed the NEXT stream on
\* the stale frame.)
Aborted == \E r \in Replies : llm[r] = "aborted"
LlmStart(r) ==
    /\ llm[r] = "idle" /\ ~Streaming /\ ~Aborted
    /\ llm' = [llm EXCEPT ![r] = "streaming"]
    /\ order' = Append(order, r)
    /\ Ledger([type |-> "LLMStart", gen |-> r])
    /\ UNCHANGED <<synth, timedOut, drainPending, chunks, playedN, pq, window, interrupts>>

LlmEnd(r) ==
    /\ llm[r] = "streaming"
    /\ llm' = [llm EXCEPT ![r] = "done"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<order, synth, timedOut, drainPending, chunks, playedN, pq, window, interrupts>>

\* An interrupted completion still ends: pipecat pushes its End from a finally,
\* after the InterruptionFrame. The ledger finds no stream and ignores it.
LlmCancelEnd(r) ==
    /\ llm[r] = "aborted"
    /\ llm' = [llm EXCEPT ![r] = "cancelled"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<order, synth, timedOut, drainPending, chunks, playedN, pq, window, interrupts>>

\* Synthesis is sequential per the TTS service, and in the order responses
\* reached it: a reply's context cannot start while an earlier live reply's has
\* not, nor while the previous context's End re-push is still to be pushed (the
\* worker pushes it before it takes the next context). An Unspeakable reply opens
\* a context like any other (UnspeakableOpens) -- or, under the first cut's premise,
\* none, and is stranded at the queue's head for the next context to claim.
Pos(r) == CHOOSE i \in DOMAIN order : order[i] = r
Earlier(r) == {order[i] : i \in 1..(Pos(r) - 1)}
CanStart(c) ==
    /\ synth[c] = "idle" /\ ~Synthesizing /\ ~Draining
    /\ IF c \in Fillers THEN TRUE
         ELSE /\ c \notin Unspeakable \/ UnspeakableOpens
              /\ llm[c] \in {"streaming", "done"}
              /\ \A r2 \in Earlier(c) :
                    llm[r2] \in {"aborted", "cancelled"}
                        \/ (r2 \in Unspeakable /\ ~UnspeakableOpens)
                        \/ synth[r2] # "idle"

TtsStart(c) ==
    /\ CanStart(c)
    /\ synth' = [synth EXCEPT ![c] = "started"]
    /\ Ledger([type |-> "TTSStarted", ctx |-> c, filler |-> c \in Fillers])
    /\ UNCHANGED <<llm, order, timedOut, drainPending, chunks, playedN, pq, window, interrupts>>

TtsAudio(c) ==
    /\ synth[c] \in {"started", "audio"} /\ chunks[c] < MaxChunks /\ c \notin Unspeakable
    /\ synth' = [synth EXCEPT ![c] = "audio"]
    /\ chunks' = [chunks EXCEPT ![c] = @ + 1]
    /\ pq' = Append(pq, [k |-> "audio", c |-> c])
    /\ Ledger([type |-> "TTSAudio", ctx |-> c])
    /\ UNCHANGED <<llm, order, timedOut, drainPending, playedN, window, interrupts>>

\* The context ends: its TTSStoppedFrame, queued at the transport behind its audio.
\* One with audio, after its last chunk; an Unspeakable one with none, on the
\* stop-frame timeout. A reply's whose End reached the TTS then re-pushes that End
\* (TtsDrain, the ledger's drain signal); one whose LLM still streams hit the
\* stop-frame timeout instead and gets no re-push of its own.
TtsStop(c) ==
    /\ \/ synth[c] = "audio"
       \/ synth[c] = "started" /\ c \in Unspeakable
    /\ c \in Replies => llm[c] \in {"streaming", "done"}
    /\ synth' = [synth EXCEPT ![c] = "stopped"]
    /\ pq' = Append(pq, [k |-> "stop", c |-> c])
    /\ timedOut' = IF c \in Replies /\ llm[c] = "streaming"
                   THEN [timedOut EXCEPT ![c] = TRUE] ELSE timedOut
    /\ drainPending' = IF c \in Replies /\ llm[c] = "done" /\ Drain
                       THEN [drainPending EXCEPT ![c] = TRUE] ELSE drainPending
    /\ Ledger([type |-> "TTSStopped", ctx |-> c])
    /\ UNCHANGED <<llm, order, chunks, playedN, window, interrupts>>

\* The End re-push, sighted below the TTS right behind the stop frame. LLM-side
\* frames and the transport may interleave; no TTS frame of another context can.
TtsDrain(r) ==
    /\ drainPending[r]
    /\ drainPending' = [drainPending EXCEPT ![r] = FALSE]
    /\ Ledger([type |-> "Drained", gen |-> r])
    /\ UNCHANGED <<llm, order, synth, timedOut, chunks, playedN, pq, window, interrupts>>

\* Resume: the LLM's next sentence reaches the TTS after its context ended on the
\* timeout, and pipecat re-creates the context under the SAME id (_push_tts_frames:
\* a second TTSStartedFrame naming the old context).
TtsRestart(c) ==
    /\ Resume /\ c \in Replies /\ c \notin Unspeakable
    /\ synth[c] = "stopped" /\ timedOut[c] /\ llm[c] = "streaming"
    /\ ~Synthesizing /\ ~Draining
    /\ synth' = [synth EXCEPT ![c] = "started"]
    /\ timedOut' = [timedOut EXCEPT ![c] = FALSE]
    /\ Ledger([type |-> "TTSStarted", ctx |-> c, filler |-> FALSE])
    /\ UNCHANGED <<llm, order, drainPending, chunks, playedN, pq, window, interrupts>>

HeadIs(kind) == pq # <<>> /\ Head(pq).k = kind

PlayStart ==                                       \* BotStartedSpeaking: the window opens
    /\ ~window /\ HeadIs("audio")
    /\ window' = TRUE
    /\ Ledger([type |-> "PlayStart"])
    /\ UNCHANGED <<llm, order, synth, timedOut, drainPending, chunks, playedN, pq, interrupts>>

PlayChunk ==                                       \* a chunk plays; the transport re-pushes it UNTAGGED
    /\ window /\ HeadIs("audio")
    /\ pq' = Tail(pq)
    /\ playedN' = [playedN EXCEPT ![Head(pq).c] = @ + 1]
    /\ Ledger([type |-> "PlayChunk"])
    /\ UNCHANGED <<llm, order, synth, timedOut, drainPending, chunks, window, interrupts>>

\* A context's stop frame reaches the head of the transport's queue: bot-speaking
\* ends if audio played since it last did (base_output: _tts_audio_received) --
\* whatever is queued behind it.
PlayMark ==
    /\ HeadIs("stop")
    /\ pq' = Tail(pq)
    /\ IF window
         THEN window' = FALSE /\ Ledger([type |-> "PlayStop"])
         ELSE UNCHANGED window /\ UNCHANGED ledgerVars
    /\ UNCHANGED <<llm, order, synth, timedOut, drainPending, chunks, playedN, interrupts>>

PlayIdle ==                                        \* BOT_VAD_STOP_FALLBACK_SECS: the queue ran dry
    /\ window /\ pq = <<>>
    /\ window' = FALSE
    /\ Ledger([type |-> "PlayStop"])
    /\ UNCHANGED <<llm, order, synth, timedOut, drainPending, chunks, playedN, pq, interrupts>>

Interrupt ==                                       \* the user barges in
    /\ interrupts < MaxInterrupts
    /\ window \/ Synthesizing \/ Streaming \/ Draining \/ pq # <<>> \/ turns # <<>>
    /\ interrupts' = interrupts + 1
    /\ pq' = <<>> /\ window' = FALSE               \* the transport flushes
    /\ synth' = [c \in Ctxs |-> IF synth[c] \in {"started", "audio"} THEN "aborted" ELSE synth[c]]
    /\ timedOut' = [c \in Ctxs |-> FALSE]          \* the turn context is cleared: no resume
    /\ drainPending' = [r \in Replies |-> FALSE]   \* an End re-push in flight is dropped
    \* A streaming completion is cancelled (its End frame still comes, see
    \* LlmCancelEnd); a completed reply whose synthesis had not begun is dropped
    \* with the TTS service's queue and never starts.
    /\ llm' = [r \in Replies |-> IF llm[r] = "streaming" THEN "aborted"
                                 ELSE IF llm[r] = "done" /\ synth[r] = "idle" THEN "cancelled"
                                 ELSE llm[r]]
    /\ Ledger([type |-> "Interruption"])
    /\ UNCHANGED <<order, chunks, playedN>>

Next ==
    \/ \E r \in Replies : LlmStart(r) \/ LlmEnd(r) \/ LlmCancelEnd(r) \/ TtsRestart(r) \/ TtsDrain(r)
    \/ \E c \in Ctxs : TtsStart(c) \/ TtsAudio(c) \/ TtsStop(c)
    \/ PlayStart \/ PlayChunk \/ PlayMark \/ PlayIdle
    \/ Interrupt

Spec == Init /\ [][Next]_vars

(* ---------------- properties ---------------- *)

\* A reply charted cut with no audio_start -- heard_fraction 0 -- never plays: not
\* before the chart, nor after it. HeardContextCorrector deletes such a reply from
\* the context, so one the caller then hears is an answer the model forgets it gave.
\* (Read live, not snapshotted at the chart: the first cut only asked "before".)
NoUnheardWhenPlayed ==
    \A i \in DOMAIN charted :
        (charted[i].intr /\ charted[i].audioStart = NONE) => playedN[charted[i].gen] = 0

\* A reply charted COMPLETE had every chunk of its audio played. Stricter than
\* Ledger.tla's, which allowed "queued at the transport": a turn is charted only
\* once its audio has played out, so a barge-in that flushes a queued tail cuts it.
NoPhantomFullHeard ==
    \A i \in DOMAIN charted : ~charted[i].intr => charted[i].allPlayed

\* A reply charted complete had all of its synthesis pushed -- a turn is never
\* closed as done while the same reply is still being synthesized.
NoPrematureFullChart ==
    \A i \in DOMAIN charted : ~charted[i].intr => charted[i].complete

\* No response is charted twice.
ChartedAtMostOnce ==
    \A i, j \in DOMAIN charted : i # j => charted[i].gen # charted[j].gen

\* A turn charted under a reply's context carries THAT reply's text.
ChartedTextMatchesContext ==
    \A i \in DOMAIN charted : charted[i].ctx \in Replies => charted[i].ctx = charted[i].gen

\* A reply the transport played is charted once its turn is over: no played
\* audio vanishes from the transcript.
PlayedIsCharted ==
    \A r \in Replies :
        (playedN[r] > 0 /\ ~(\E i \in DOMAIN turns : turns[i].gen = r))
            => \E i \in DOMAIN charted : charted[i].gen = r

\* While a filler context is live, the ledger remembers it is a filler.
FillerCtxRemembered ==
    \A c \in Fillers : synth[c] \in {"started", "audio"} => c \in Range(fillerCtxs)

TypeOK ==
    /\ llm \in [Replies -> {"idle", "streaming", "done", "aborted", "cancelled"}]
    /\ order \in Seq(Replies)
    /\ synth \in [Ctxs -> {"idle", "started", "audio", "stopped", "aborted"}]
    /\ timedOut \in [Ctxs -> BOOLEAN] /\ drainPending \in [Replies -> BOOLEAN]
    /\ pq \in Seq([k : {"audio", "stop"}, c : Ctxs]) /\ window \in BOOLEAN
    /\ genAcc \in {NONE} \cup Replies /\ ended \subseteq Replies
    /\ claimed \subseteq Replies /\ queue \in Seq(Replies)
    /\ fifo \in Seq(Ctxs) /\ winOpen \in BOOLEAN
    /\ closedCtxs \subseteq Ctxs /\ fillerCtxs \in Seq(Fillers)
    /\ \A i \in DOMAIN turns :
         /\ turns[i].ctx \in Ctxs
         /\ turns[i].gen \in {NONE} \cup Replies
         /\ turns[i].audioStart \in {NONE, OWN}
=============================================================================
