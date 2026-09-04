--------------------------- MODULE LedgerPlayout ---------------------------
(***************************************************************************)
(* teaport -- TranscriptLedger's bot-turn state machine: the PLAYOUT design *)
(* (transcript_ledger.py after the review of PR #13).                       *)
(*                                                                          *)
(* Ledger.tla keeps the two designs this one replaced (asWritten and        *)
(* windowHead) with their counterexamples. The environment here is that     *)
(* module's -- the LLM stream, the TTS's sequential synthesis, the          *)
(* transport's push-order playout, the user's barge-in -- with three facts  *)
(* the redesign rests on spelled out:                                       *)
(*                                                                          *)
(*   - The TTS re-pushes a response's LLMFullResponseEndFrame, the SAME     *)
(*     frame, once the response's context has drained (pipecat 1.7.0,       *)
(*     tts_service._maybe_reset_word_timestamps). Its second sighting names *)
(*     the response the context spoke and says its synthesis is over. It    *)
(*     comes only if the End had reached the TTS before the drain: a        *)
(*     context that drains on the stop-frame timeout while the LLM still    *)
(*     streams gets none, and may be re-created under the SAME id when its  *)
(*     audio resumes (Resume).                                              *)
(*   - Every TTS push is tagged with its context (engine_tts.py yields      *)
(*     audio with context_id); the transport's untagged rebuild of each     *)
(*     chunk it played is recognised and ignored. The untagged legacy path  *)
(*     is out of scope here.                                                *)
(*   - The engine TTS pushes no TTSStoppedFrame (push_stop_frames=False),   *)
(*     so the drain re-push is the ledger's only completion signal, and the *)
(*     transport's window closes on 0.35s of silence alone -- mid-reply,    *)
(*     when synthesis stalls.                                               *)
(*                                                                          *)
(* The ledger: turns open only on the TTS's own frames, never on the        *)
(* anonymous BotStartedSpeaking; a turn claims the oldest expected context  *)
(* (the queue of ended responses), else the one streaming; several turns    *)
(* may be open at once; a turn's audio_start is its own chunk's place in    *)
(* the transport's queue; a window closing credits every queued chunk as    *)
(* played and charts, oldest first, the turns whose audio has all played    *)
(* and whose synthesis is over; a context starting proves every OTHER open  *)
(* context ended; the drain re-push confirms or corrects a turn's response; *)
(* an interruption charts every open turn.                                  *)
(*                                                                          *)
(* Everything numeric is abstracted away, as in Ledger.tla: what is kept    *)
(* is which turn a chunk is attributed to, whether a turn's playout start   *)
(* is known, which response's text it claims, and whether it is charted    *)
(* cut or complete.                                                         *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Replies,       \* reply ids; each is also its TTS context id
          Fillers,       \* filler context ids -- narrator / tool-ack TTSSpeakFrames
          Unspeakable,   \* replies the TTS never opens a context for (nothing synthesizable)
          LedgerSpeakCheck, \* BOOLEAN: the ledger applies the engine's own has_speech
                         \*   before queuing a response (transcript_ledger._llm_side);
                         \*   FALSE is the ledger before that -- the counterexample
          Drain,         \* BOOLEAN: the live wiring (tts given: the End re-push is seen);
                         \*   FALSE is the hermetic tests' -- no drain signal, a turn is
                         \*   taken as synthesized once its response has ended
          Resume,        \* BOOLEAN: a context drained on the stop-frame timeout may resume
          MaxChunks,     \* audio pushes per context (2 makes a stall mid-reply reachable)
          MaxInterrupts,
          MaxFillerCtxs  \* the 512 in the TTSStarted branch, shrunk to make overflow reachable

NONE == "none"
OWN  == "own"            \* audio_start: the turn's own chunk's place in the playout layout
Ctxs == Replies \cup Fillers
Range(s) == {s[i] : i \in DOMAIN s}
Remove(s, x) == SelectSeq(s, LAMBDA y: y # x)

VARIABLES
    \* --- the pipeline: ground truth ---
    llm,         \* [Replies -> idle | streaming | done | aborted | cancelled]
    order,       \* replies in the order their completions started
    synth,       \* [Ctxs -> idle | started | audio | stopped | aborted]
    timedOut,    \* [Ctxs -> BOOLEAN]: stopped on the timeout, its reply still streaming
    chunks,      \* [Ctxs -> Nat]: audio chunks pushed
    playedN,     \* [Ctxs -> Nat]: audio chunks the transport has played
    pq,          \* audio at the output transport, one entry per chunk, in push order
    window,      \* the transport's bot-speaking window is open
    interrupts,
    \* --- the ledger: transcript_ledger.py ---
    genAcc,      \* _gen          : NONE or the reply streaming now
    ended,       \* replies whose End the TTS sighted with text (entry["ended"])
    claimed,     \* replies some turn has taken (entry["turn"] is set)
    queue,       \* _queue        : ended replies no context has claimed, oldest first
    turns,       \* _turns        : open turns, playout order (see Opened)
    fifo,        \* _fifo         : contexts pushed since the window last closed
    winOpen,     \* _win_t0 is not None
    fillerCtxs,  \* _filler_ctxs  : in insertion order
    closedCtxs,  \* _closed_ctxs
    charted      \* self.events, assistant utterances only

envVars    == <<llm, order, synth, timedOut, chunks, playedN, pq, window, interrupts>>
ledgerVars == <<genAcc, ended, claimed, queue, turns, fifo, winOpen, fillerCtxs, closedCtxs, charted>>
vars       == <<llm, order, synth, timedOut, chunks, playedN, pq, window, interrupts,
                genAcc, ended, claimed, queue, turns, fifo, winOpen, fillerCtxs, closedCtxs, charted>>

Init ==
    /\ llm = [r \in Replies |-> "idle"] /\ order = <<>>
    /\ synth = [c \in Ctxs |-> "idle"] /\ timedOut = [c \in Ctxs |-> FALSE]
    /\ chunks = [c \in Ctxs |-> 0] /\ playedN = [c \in Ctxs |-> 0]
    /\ pq = <<>> /\ window = FALSE /\ interrupts = 0
    /\ genAcc = NONE /\ ended = {} /\ claimed = {} /\ queue = <<>> /\ turns = <<>>
    /\ fifo = <<>> /\ winOpen = FALSE /\ fillerCtxs = <<>> /\ closedCtxs = {}
    /\ charted = <<>>

(* ---------------- the ledger's transition function ---------------- *)

\* Ground truth snapshotted into the chart, for the properties.
AnyPlayed(g) == playedN[g] > 0
AllPlayed(g) == chunks[g] > 0 /\ playedN[g] = chunks[g]
\* `stopping` is the context the same step drains (its synth is "stopped" in the
\* post-state; the chart is taken in the pre-state).
GenComplete(g, stopping) == (synth[g] = "stopped" \/ g = stopping) /\ (~Resume \/ ~timedOut[g])
Chart(t, intr, stopping) ==
    [gen |-> t.gen, ctx |-> t.ctx, audioStart |-> t.audioStart, intr |-> intr,
     anyPlayed |-> AnyPlayed(t.gen), allPlayed |-> AllPlayed(t.gen),
     complete |-> GenComplete(t.gen, stopping)]
\* _finish charts nothing for a turn that holds no text (an anonymous turn).
Named(ts) == SelectSeq(ts, LAMBDA t: t.gen # NONE)
ChartsAs(ts, intr, stopping) == [i \in DOMAIN Named(ts) |-> Chart(Named(ts)[i], intr, stopping)]
\* At a cut, a turn whose synthesis is over and whose every chunk had played
\* out by the previous window close is charted complete, not cut.
ChartsCut(ts) == [i \in DOMAIN Named(ts) |->
                    Chart(Named(ts)[i], ~(Named(ts)[i].synthDone /\ Named(ts)[i].playedAll), NONE)]
CtxsOf(ts) == {t.ctx : t \in Range(ts)}

HasOpen(c) == \E i \in DOMAIN turns : turns[i].ctx = c

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
    [] f.type = "PlayStart" ->                   \* BotStartedSpeaking: anonymous; dates the window
         /\ winOpen' = TRUE
         /\ turns' = [i \in DOMAIN turns |->
                        IF turns[i].ctx \in Range(fifo) /\ turns[i].audioStart = NONE
                        THEN [turns[i] EXCEPT !.audioStart = OWN] ELSE turns[i]]
         /\ UNCHANGED <<genAcc, ended, claimed, queue, fifo, fillerCtxs, closedCtxs, charted>>
    [] f.type = "PlayChunk" ->                   \* the transport's untagged copy: ignored
         UNCHANGED ledgerVars
    [] f.type = "PlayStop" ->                    \* BotStoppedSpeaking: everything queued played
         LET ts == [i \in DOMAIN turns |->
                      IF turns[i].ctx \in Range(fifo)
                      THEN [turns[i] EXCEPT !.playedAll = TRUE] ELSE turns[i]]
         IN /\ turns' = Kept(ts)
            /\ charted' = charted \o ChartsAs(Done(ts), FALSE, NONE)
            /\ closedCtxs' = closedCtxs \cup CtxsOf(Done(ts))
            /\ fifo' = <<>> /\ winOpen' = FALSE
            /\ UNCHANGED <<genAcc, ended, claimed, queue, fillerCtxs>>
    [] f.type = "Drained" ->                     \* the response's End, sighted again below the TTS
         IF f.gen \in claimed /\ ~(\E i \in DOMAIN turns : turns[i].gen = f.gen) THEN
            UNCHANGED ledgerVars                 \* its turn is charted already
         ELSE IF turns = <<>> THEN
            /\ queue' = Remove(queue, f.gen)
            /\ UNCHANGED <<genAcc, ended, claimed, turns, fifo, winOpen, fillerCtxs, closedCtxs, charted>>
         ELSE
            \* Unclaimed yet its context drained: the newest open turn is this
            \* response's, whatever it claimed by order (that was never spoken).
            LET tsA == IF \E i \in DOMAIN turns : turns[i].gen = f.gen THEN turns
                       ELSE [turns EXCEPT ![Len(turns)].gen = f.gen]
                tsB == [i \in DOMAIN tsA |-> IF tsA[i].gen = f.gen
                                             THEN [tsA[i] EXCEPT !.synthDone = TRUE] ELSE tsA[i]]
                k   == CHOOSE i \in DOMAIN tsB : tsB[i].gen = f.gen
            IN /\ queue' = Remove(queue, f.gen)
               /\ claimed' = claimed \cup {f.gen}
               /\ IF ~tsB[k].qa THEN            \* drained with no audio: never played
                     /\ charted' = charted \o <<Chart(tsB[k], TRUE, f.gen)>>
                     /\ turns' = SubSeq(tsB, 1, k - 1) \o SubSeq(tsB, k + 1, Len(tsB))
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
    /\ UNCHANGED <<synth, timedOut, chunks, playedN, pq, window, interrupts>>

LlmEnd(r) ==
    /\ llm[r] = "streaming"
    /\ llm' = [llm EXCEPT ![r] = "done"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<order, synth, timedOut, chunks, playedN, pq, window, interrupts>>

\* An interrupted completion still ends: pipecat pushes its End from a finally,
\* after the InterruptionFrame. The ledger finds no stream and ignores it.
LlmCancelEnd(r) ==
    /\ llm[r] = "aborted"
    /\ llm' = [llm EXCEPT ![r] = "cancelled"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<order, synth, timedOut, chunks, playedN, pq, window, interrupts>>

\* Synthesis is sequential per the TTS service, and in the order responses
\* reached it: a reply's context cannot start while an earlier live reply's has
\* not. An Unspeakable reply's text never opens a context (nothing synthesizable,
\* or the guard emptied it): it ends, is queued by the ledger, and is stranded at
\* the queue's head for the next context to claim.
Pos(r) == CHOOSE i \in DOMAIN order : order[i] = r
Earlier(r) == {order[i] : i \in 1..(Pos(r) - 1)}
CanStart(c) ==
    /\ synth[c] = "idle" /\ ~Synthesizing
    /\ IF c \in Fillers THEN TRUE
         ELSE /\ c \notin Unspeakable
              /\ llm[c] \in {"streaming", "done"}
              /\ \A r2 \in Earlier(c) :
                    llm[r2] \in {"aborted", "cancelled"} \/ r2 \in Unspeakable
                        \/ synth[r2] # "idle"

TtsStart(c) ==
    /\ CanStart(c)
    /\ synth' = [synth EXCEPT ![c] = "started"]
    /\ Ledger([type |-> "TTSStarted", ctx |-> c, filler |-> c \in Fillers])
    /\ UNCHANGED <<llm, order, timedOut, chunks, playedN, pq, window, interrupts>>

TtsAudio(c) ==
    /\ synth[c] \in {"started", "audio"} /\ chunks[c] < MaxChunks
    /\ synth' = [synth EXCEPT ![c] = "audio"]
    /\ chunks' = [chunks EXCEPT ![c] = @ + 1]
    /\ pq' = Append(pq, c)
    /\ Ledger([type |-> "TTSAudio", ctx |-> c])
    /\ UNCHANGED <<llm, order, timedOut, playedN, window, interrupts>>

\* The context drains. A reply's whose End reached the TTS re-pushes that End
\* (the ledger's drain signal); one whose LLM still streams hit the stop-frame
\* timeout instead, and the ledger hears nothing.
TtsStop(c) ==
    /\ synth[c] = "audio"
    /\ synth' = [synth EXCEPT ![c] = "stopped"]
    /\ IF c \in Fillers THEN
          /\ UNCHANGED ledgerVars /\ UNCHANGED timedOut
       ELSE IF llm[c] = "done" /\ Drain THEN
          /\ Ledger([type |-> "Drained", gen |-> c]) /\ UNCHANGED timedOut
       ELSE IF llm[c] = "done" THEN            \* hermetic: there is no drain signal
          /\ UNCHANGED ledgerVars /\ UNCHANGED timedOut
       ELSE
          /\ UNCHANGED ledgerVars
          /\ timedOut' = [timedOut EXCEPT ![c] = TRUE]
    /\ UNCHANGED <<llm, order, chunks, playedN, pq, window, interrupts>>

\* Resume: audio for a timed-out context arrives while its LLM still streams, and
\* pipecat re-creates the context under the SAME id (tts_service
\* append_to_audio_context): a second TTSStartedFrame naming the old context.
TtsRestart(c) ==
    /\ Resume /\ c \in Replies
    /\ synth[c] = "stopped" /\ timedOut[c] /\ llm[c] = "streaming" /\ ~Synthesizing
    /\ synth' = [synth EXCEPT ![c] = "started"]
    /\ timedOut' = [timedOut EXCEPT ![c] = FALSE]
    /\ Ledger([type |-> "TTSStarted", ctx |-> c, filler |-> FALSE])
    /\ UNCHANGED <<llm, order, chunks, playedN, pq, window, interrupts>>

PlayStart ==                                       \* BotStartedSpeaking: the window opens
    /\ ~window /\ pq # <<>>
    /\ window' = TRUE
    /\ Ledger([type |-> "PlayStart"])
    /\ UNCHANGED <<llm, order, synth, timedOut, chunks, playedN, pq, interrupts>>

PlayChunk ==                                       \* a chunk plays; the transport re-pushes it UNTAGGED
    /\ window /\ pq # <<>>
    /\ pq' = Tail(pq)
    /\ playedN' = [playedN EXCEPT ![Head(pq)] = @ + 1]
    /\ Ledger([type |-> "PlayChunk"])
    /\ UNCHANGED <<llm, order, synth, timedOut, chunks, window, interrupts>>

PlayStop ==                                        \* BotStoppedSpeaking: the queue ran dry
    /\ window /\ pq = <<>>
    /\ window' = FALSE
    /\ Ledger([type |-> "PlayStop"])
    /\ UNCHANGED <<llm, order, synth, timedOut, chunks, playedN, pq, interrupts>>

Interrupt ==                                       \* the user barges in
    /\ interrupts < MaxInterrupts
    /\ window \/ Synthesizing \/ Streaming         \* otherwise a no-op for everything modelled
    /\ interrupts' = interrupts + 1
    /\ pq' = <<>> /\ window' = FALSE               \* the transport flushes
    /\ synth' = [c \in Ctxs |-> IF synth[c] \in {"started", "audio"} THEN "aborted" ELSE synth[c]]
    /\ timedOut' = [c \in Ctxs |-> FALSE]          \* the turn context is cleared: no resume
    \* A streaming completion is cancelled (its End frame still comes, see
    \* LlmCancelEnd); a completed reply whose synthesis had not begun is dropped
    \* with the TTS service's queue and never starts.
    /\ llm' = [r \in Replies |-> IF llm[r] = "streaming" THEN "aborted"
                                 ELSE IF llm[r] = "done" /\ synth[r] = "idle" THEN "cancelled"
                                 ELSE llm[r]]
    /\ Ledger([type |-> "Interruption"])
    /\ UNCHANGED <<order, chunks, playedN>>

Next ==
    \/ \E r \in Replies : LlmStart(r) \/ LlmEnd(r) \/ LlmCancelEnd(r) \/ TtsRestart(r)
    \/ \E c \in Ctxs : TtsStart(c) \/ TtsAudio(c) \/ TtsStop(c)
    \/ PlayStart \/ PlayChunk \/ PlayStop
    \/ Interrupt

Spec == Init /\ [][Next]_vars

(* ---------------- properties ---------------- *)

\* A cut reply the transport actually PLAYED never charts with no audio_start --
\* that is heard_fraction 0, and HeardContextCorrector then deletes the assistant
\* message the user heard part of.
NoUnheardWhenPlayed ==
    \A i \in DOMAIN charted :
        (charted[i].intr /\ charted[i].audioStart = NONE) => ~charted[i].anyPlayed

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
    /\ timedOut \in [Ctxs -> BOOLEAN]
    /\ pq \in Seq(Ctxs) /\ window \in BOOLEAN
    /\ genAcc \in {NONE} \cup Replies /\ ended \subseteq Replies
    /\ claimed \subseteq Replies /\ queue \in Seq(Replies)
    /\ fifo \in Seq(Ctxs) /\ winOpen \in BOOLEAN
    /\ closedCtxs \subseteq Ctxs /\ fillerCtxs \in Seq(Fillers)
    /\ \A i \in DOMAIN turns :
         /\ turns[i].ctx \in Ctxs
         /\ turns[i].gen \in {NONE} \cup Replies
         /\ turns[i].audioStart \in {NONE, OWN}
=============================================================================
