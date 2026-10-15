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
(*     none). pipecat closes a context at its End only if its LAST run_tts  *)
(*     yielded audio (on_turn_context_completed); a reply whose last piece  *)
(*     has nothing synthesizable ("Great!!" ends in a lone "!") is left to  *)
(*     the stop-frame timeout, and so is an unspeakable reply -- unless the *)
(*     service says it yields synchronously (CloseAtEnd: engine_tts).       *)
(*     A reply the engine fails on every clause (Failed) has speech, opens  *)
(*     a context, yields no audio -- and pipecat commits its text anyway:   *)
(*     the TTS force-completes a context's text at its stop.                *)
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
(* (the queue of ended responses: every one with text, synthesizable or     *)
(* not), else the one streaming; several turns may be open at once; a       *)
(* turn's audio_start is its own chunk's place in the transport's queue; a  *)
(* window closing credits the queued chunks that have played by then and    *)
(* charts, oldest first, the turns whose audio has all played and whose     *)
(* synthesis is over; a context starting proves every OTHER open context    *)
(* ended; a context's stop frame says its synthesis is over; the drain      *)
(* re-push confirms or corrects a turn's response; an interruption charts   *)
(* every open turn. A whole response with nothing synthesizable closes      *)
(* charting nothing: none of it could be heard.                             *)
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
          Failed,        \* replies with speech the engine fails on every clause (no audio,
                         \*   ever: CUDA OOM, an engine restart); has_speech says yes
          TrailingMute,  \* replies with audio whose LAST piece run_tts gets alone has
                         \*   nothing synthesizable: "Great!!" -> "Great!", "!"
          Corrector,     \* how HeardContextCorrector finds a cut reply's message:
                         \*   "seq" -- as shipped: its own, by its rank among the window's
                         \*     committed replies (Utterance.turn_seq, .committed; Target);
                         \*   "guarded" -- the newest message, a cut heard nothing let be
                         \*     when that cannot be its own (the first fix);
                         \*   "position" -- the newest message, always (before either)
          CloseAtEnd,    \* BOOLEAN: the TTS closes a context at its End whatever its last
                         \*   run_tts yielded (engine_tts.tts_process_generator, shipped);
                         \*   FALSE is pipecat's default: only if it yielded audio
          UnspeakableOpens, \* BOOLEAN: pipecat opens a context for an Unspeakable reply
                         \*   (TRUE: 1.8.1 as it is); FALSE is the premise the model was
                         \*   first written with -- no context at all
          SpeakCheck,    \* where the ledger applies the engine's has_speech:
                         \*   "chart" -- as shipped: every response with text is queued,
                         \*     and a turn with no audio and nothing synthesizable is
                         \*     charted nothing (_finish; see Silent);
                         \*   "queue" -- before this fix: such a response is not queued
                         \*     (_llm_side), on the first cut's premise that no context
                         \*     opens for it;
                         \*   "none" -- every response with text queued AND charted
          Drain,         \* BOOLEAN: the live wiring (tts given: the End re-push is seen);
                         \*   FALSE is the hermetic tests' -- no drain signal, a turn is
                         \*   taken as synthesized once its response has ended
          Resume,        \* BOOLEAN: a context that ended on the timeout mid-reply may resume
          MaxChunks,     \* audio pushes per context (2 makes a stall mid-reply reachable)
          MaxInterrupts,
          MaxFillerCtxs  \* the 512 in the TTSStarted branch, shrunk to make overflow reachable

ASSUME SpeakCheck \in {"chart", "queue", "none"}
ASSUME TrailingMute \cap Unspeakable = {} /\ Failed \cap (Unspeakable \cup TrailingMute) = {}
ASSUME Corrector \in {"seq", "guarded", "position"}

NONE == "none"
OWN  == "own"            \* audio_start: the turn's own chunk's place in the playout layout
Ctxs == Replies \cup Fillers
Silenced == Unspeakable \cup Failed     \* a context opens, no audio ever comes
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
    swept,       \* contexts the stop-frame timeout closed after their End reached the TTS
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

envVars    == <<llm, order, synth, timedOut, drainPending, chunks, playedN, pq, window, interrupts, swept>>
ledgerVars == <<genAcc, ended, claimed, queue, turns, fifo, winOpen, fillerCtxs, closedCtxs, charted>>
vars       == <<llm, order, synth, timedOut, drainPending, chunks, playedN, pq, window, interrupts, swept,
                genAcc, ended, claimed, queue, turns, fifo, winOpen, fillerCtxs, closedCtxs, charted>>

Init ==
    /\ llm = [r \in Replies |-> "idle"] /\ order = <<>>
    /\ synth = [c \in Ctxs |-> "idle"] /\ timedOut = [c \in Ctxs |-> FALSE]
    /\ drainPending = [r \in Replies |-> FALSE]
    /\ chunks = [c \in Ctxs |-> 0] /\ playedN = [c \in Ctxs |-> 0]
    /\ pq = <<>> /\ window = FALSE /\ interrupts = 0 /\ swept = {}
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
\* `said`: pipecat committed a message for it -- words of it played, or its context
\* ended and the TTS force-completed its text (tts_service._apply_force_complete
\* pushes the remaining text as a word frame, which the assistant aggregator
\* commits: a failed reply's text too, and an emoji). Only a reply a barge-in cut
\* before its context ended, with nothing played, committed none.
Chart(t, intr, stopping, said) ==
    [gen |-> t.gen, ctx |-> t.ctx, audioStart |-> t.audioStart, intr |-> intr,
     anyPlayed |-> AnyPlayed(t.gen), allPlayed |-> AllPlayed(t.gen),
     complete |-> GenComplete(t.gen, stopping),
     said |-> said,
     epoch |-> interrupts]                      \* the user turns (barge-ins) before it
\* _finish charts nothing for a turn that holds no text (an anonymous turn) -- nor,
\* as shipped, for one with no audio and nothing synthesizable in its text
\* (has_speech, at the chart), unless a barge-in cut its response while it still
\* streamed (`cut`): the rest of it may have had words.
Silent(t, cut) == /\ SpeakCheck = "chart" /\ t.gen \in Unspeakable /\ ~t.qa
                  /\ cut => t.gen \in ended
Named(ts, cut) == SelectSeq(ts, LAMBDA t: t.gen # NONE /\ ~Silent(t, cut))
ChartsAs(ts, intr, stopping) ==
    [i \in DOMAIN Named(ts, FALSE) |-> Chart(Named(ts, FALSE)[i], intr, stopping, TRUE)]
\* At a cut, a turn whose synthesis is over and whose every chunk had played by the
\* cut (the layout's portion up to now, _interrupt) is charted complete, not cut.
CutComplete(t) == t.synthDone /\ t.qa /\ playedN[t.ctx] = chunks[t.ctx]
ChartsCut(ts) == [i \in DOMAIN Named(ts, TRUE) |->
                    LET t == Named(ts, TRUE)[i]
                    IN Chart(t, ~CutComplete(t), NONE, AnyPlayed(t.gen) \/ t.synthDone)]
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
              \* SpeakCheck = "queue" (before this fix): text the engine will not
              \* synthesize is not an expected context (tts_text.has_speech).
              /\ queue' = IF genAcc \in claimed \/ (SpeakCheck = "queue" /\ genAcc \in Unspeakable)
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
         IF \/ SpeakCheck = "queue" /\ f.gen \in Unspeakable   \* never in _end_ids
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
                     /\ charted' = charted \o ChartsAs(<<tsB[k]>>, TRUE, f.gen)
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
    /\ UNCHANGED <<synth, timedOut, drainPending, chunks, playedN, pq, window, interrupts, swept>>

LlmEnd(r) ==
    /\ llm[r] = "streaming"
    /\ llm' = [llm EXCEPT ![r] = "done"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<order, synth, timedOut, drainPending, chunks, playedN, pq, window, interrupts, swept>>

\* An interrupted completion still ends: pipecat pushes its End from a finally,
\* after the InterruptionFrame. The ledger finds no stream and ignores it.
LlmCancelEnd(r) ==
    /\ llm[r] = "aborted"
    /\ llm' = [llm EXCEPT ![r] = "cancelled"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<order, synth, timedOut, drainPending, chunks, playedN, pq, window, interrupts, swept>>

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
    /\ UNCHANGED <<llm, order, timedOut, drainPending, chunks, playedN, pq, window, interrupts, swept>>

TtsAudio(c) ==
    /\ synth[c] \in {"started", "audio"} /\ chunks[c] < MaxChunks /\ c \notin Silenced
    /\ synth' = [synth EXCEPT ![c] = "audio"]
    /\ chunks' = [chunks EXCEPT ![c] = @ + 1]
    /\ pq' = Append(pq, [k |-> "audio", c |-> c])
    /\ Ledger([type |-> "TTSAudio", ctx |-> c])
    /\ UNCHANGED <<llm, order, timedOut, drainPending, playedN, window, interrupts, swept>>

\* The context ends: its TTSStoppedFrame, queued at the transport behind its audio.
\* One with audio, after its last chunk; a Silenced one with none. A reply's
\* whose End reached the TTS then re-pushes that End (TtsDrain, the ledger's drain
\* signal); one whose LLM still streams hit the stop-frame timeout instead and gets
\* no re-push of its own.
\*
\* Which of the two closes it once its End has reached the TTS: the End itself
\* (on_turn_context_completed appends the stop frame and ends the context), but
\* in pipecat 1.8.1 only if the LAST run_tts yielded audio
\* (tts_process_generator's flag) -- else nothing ends it but the stop-frame
\* timeout, 15 s on, with the TTS's queue held behind it. MutedAtEnd: the
\* replies whose last run_tts yields none.
MutedAtEnd == Silenced \cup TrailingMute
TtsStop(c) ==
    /\ \/ synth[c] = "audio"
       \/ synth[c] = "started" /\ c \in Silenced
    /\ c \in Replies => llm[c] \in {"streaming", "done"}
    /\ synth' = [synth EXCEPT ![c] = "stopped"]
    /\ pq' = Append(pq, [k |-> "stop", c |-> c])
    /\ timedOut' = IF c \in Replies /\ llm[c] = "streaming"
                   THEN [timedOut EXCEPT ![c] = TRUE] ELSE timedOut
    /\ drainPending' = IF c \in Replies /\ llm[c] = "done" /\ Drain
                       THEN [drainPending EXCEPT ![c] = TRUE] ELSE drainPending
    /\ swept' = IF c \in MutedAtEnd /\ llm[c] = "done" /\ ~CloseAtEnd
                 THEN swept \cup {c} ELSE swept
    /\ Ledger([type |-> "TTSStopped", ctx |-> c])
    /\ UNCHANGED <<llm, order, chunks, playedN, window, interrupts>>

\* The End re-push, sighted below the TTS right behind the stop frame. LLM-side
\* frames and the transport may interleave; no TTS frame of another context can.
TtsDrain(r) ==
    /\ drainPending[r]
    /\ drainPending' = [drainPending EXCEPT ![r] = FALSE]
    /\ Ledger([type |-> "Drained", gen |-> r])
    /\ UNCHANGED <<llm, order, synth, timedOut, chunks, playedN, pq, window, interrupts, swept>>

\* Resume: the LLM's next sentence reaches the TTS after its context ended on the
\* timeout, and pipecat re-creates the context under the SAME id (_push_tts_frames:
\* a second TTSStartedFrame naming the old context).
TtsRestart(c) ==
    /\ Resume /\ c \in Replies /\ c \notin Silenced
    /\ synth[c] = "stopped" /\ timedOut[c] /\ llm[c] = "streaming"
    /\ ~Synthesizing /\ ~Draining
    /\ synth' = [synth EXCEPT ![c] = "started"]
    /\ timedOut' = [timedOut EXCEPT ![c] = FALSE]
    /\ Ledger([type |-> "TTSStarted", ctx |-> c, filler |-> FALSE])
    /\ UNCHANGED <<llm, order, drainPending, chunks, playedN, pq, window, interrupts, swept>>

HeadIs(kind) == pq # <<>> /\ Head(pq).k = kind

PlayStart ==                                       \* BotStartedSpeaking: the window opens
    /\ ~window /\ HeadIs("audio")
    /\ window' = TRUE
    /\ Ledger([type |-> "PlayStart"])
    /\ UNCHANGED <<llm, order, synth, timedOut, drainPending, chunks, playedN, pq, interrupts, swept>>

PlayChunk ==                                       \* a chunk plays; the transport re-pushes it UNTAGGED
    /\ window /\ HeadIs("audio")
    /\ pq' = Tail(pq)
    /\ playedN' = [playedN EXCEPT ![Head(pq).c] = @ + 1]
    /\ Ledger([type |-> "PlayChunk"])
    /\ UNCHANGED <<llm, order, synth, timedOut, drainPending, chunks, window, interrupts, swept>>

\* A context's stop frame reaches the head of the transport's queue: bot-speaking
\* ends if audio played since it last did (base_output: _tts_audio_received) --
\* whatever is queued behind it.
PlayMark ==
    /\ HeadIs("stop")
    /\ pq' = Tail(pq)
    /\ IF window
         THEN window' = FALSE /\ Ledger([type |-> "PlayStop"])
         ELSE UNCHANGED window /\ UNCHANGED ledgerVars
    /\ UNCHANGED <<llm, order, synth, timedOut, drainPending, chunks, playedN, interrupts, swept>>

PlayIdle ==                                        \* BOT_VAD_STOP_FALLBACK_SECS: the queue ran dry
    /\ window /\ pq = <<>>
    /\ window' = FALSE
    /\ Ledger([type |-> "PlayStop"])
    /\ UNCHANGED <<llm, order, synth, timedOut, drainPending, chunks, playedN, pq, interrupts, swept>>

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
    /\ UNCHANGED <<order, chunks, playedN, swept>>

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

\* HeardContextCorrector finds a cut reply's message by POSITION: the newest
\* assistant message since its last reconcile, at a user turn. A reply charted
\* heard-nothing ahead of a reply that played, with no user turn between them (the
\* model's user turns are its barge-ins), has its cut land on the played reply's
\* message, and the positional corrector deletes an answer the caller heard. The
\* premise the corrector had, as a property of the charts; NoPlayedReplyDeleted,
\* below, is the requirement itself, with the corrector's rule.
NoUnheardAheadOfPlayed ==
    \A i, j \in DOMAIN charted :
        (/\ i < j /\ charted[i].intr /\ charted[i].audioStart = NONE
         /\ charted[j].anyPlayed)
            => charted[i].epoch < charted[j].epoch

\* ---- HeardContextCorrector, at a user turn now: every chart of the current window
\* (epoch) is in its batch, and every message committed in it is in its context, in
\* the order the replies reached the TTS (Pos: each commits at its End's re-push, or
\* at the cut). That is the ledger's response seq, which each utterance carries; the
\* order of the CHARTS can differ (a failed reply is charted at its stop, while the
\* reply ahead of it still plays).
SameWindow(i, j) == charted[i].epoch = charted[j].epoch
Newer(j, i) == Pos(charted[j].gen) > Pos(charted[i].gen)
\* The committed messages of a cut i's window. (Uncharted commits -- an unspeakable
\* reply's force-completed text -- are left out; heard_context skips messages with
\* no speech.)
Said(i) == {j \in DOMAIN charted : SameWindow(i, j) /\ charted[j].said}
Newest(js) == IF js = {} THEN 0 ELSE CHOOSE j \in js : \A k \in js : ~Newer(k, j)
Anchor(i) == Newest(Said(i))
\* Which cuts the corrector reconciles. "seq": every one that committed a message (one
\* that did not has nothing to correct). "guarded": a cut heard nothing is let be when
\* its message cannot be the newest -- a reply newer than it in its window played, or
\* one older did and it committed nothing.
PlayedIn(i, newer) == \E j \in DOMAIN charted :
    /\ j # i /\ SameWindow(i, j) /\ charted[j].anyPlayed /\ Newer(j, i) = newer
Acts(i) ==
    /\ charted[i].intr
    /\ CASE Corrector = "seq"      -> charted[i].said
         [] Corrector = "position" -> TRUE
         [] OTHER -> \/ charted[i].anyPlayed     \* heard something: its own words are newest
                    \/ /\ ~PlayedIn(i, TRUE)
                       /\ charted[i].said \/ ~PlayedIn(i, FALSE)

\* The message the corrector rewrites for cut i (0: none). "seq"
\* (heard_context._resolve): every cut's found before any is rewritten -- the one as
\* many committed messages back from the newest as the batch shows committed replies
\* newer than the cut. The positional correctors take the newest message still in
\* the context, cut by cut in the batch's (chart) order: a cut heard nothing before
\* this one removed its target.
NewerSaid(i) == Cardinality({j \in Said(i) : Newer(j, i)})
RECURSIVE Target(_)
Target(i) ==
    IF Corrector = "seq"
    THEN LET js == {j \in Said(i) : NewerSaid(j) = NewerSaid(i)}
         IN IF js = {} THEN 0 ELSE CHOOSE j \in js : TRUE
    ELSE Newest(Said(i) \ {Target(k) : k \in {k \in 1..(i - 1) :
                                         /\ SameWindow(i, k) /\ Acts(k)
                                         /\ ~charted[k].anyPlayed}})

\* The corrector never rewrites or deletes the message of ANOTHER reply that played.
NoPlayedReplyDeleted ==
    \A i \in DOMAIN charted :
        (Acts(i) /\ Target(i) \notin {0, i}) => ~charted[Target(i)].anyPlayed

\* A cut that heard something has its heard prefix written on its OWN message: on
\* another, its own keeps the full text and the context says all of it was heard.
HeardPrefixOnOwn ==
    \A i \in DOMAIN charted : (Acts(i) /\ charted[i].anyPlayed) => Target(i) = i

\* ...and every cut it reconciles is reconciled on its own message.
CutAnchorsOwn ==
    \A i \in DOMAIN charted : (Acts(i) /\ charted[i].said) => Target(i) = i

\* ...and every cut heard nothing that committed a message is reconciled (removed:
\* none of it was heard).
UnheardSaidIsCorrected ==
    \A i \in DOMAIN charted :
        (charted[i].intr /\ ~charted[i].anyPlayed /\ charted[i].said) => Acts(i)

\* A turn charted under a reply's context carries THAT reply's text.
ChartedTextMatchesContext ==
    \A i \in DOMAIN charted : charted[i].ctx \in Replies => charted[i].ctx = charted[i].gen

\* A reply the transport played is charted once its turn is over: no played
\* audio vanishes from the transcript.
PlayedIsCharted ==
    \A r \in Replies :
        (playedN[r] > 0 /\ ~(\E i \in DOMAIN turns : turns[i].gen = r))
            => \E i \in DOMAIN charted : charted[i].gen = r

\* A context whose End has reached the TTS is closed by that End, not by the
\* stop-frame timeout: nothing the TTS says next (the answer after a tool call, a
\* narrator line) waits out the 15 s behind it, and bot-speaking ends on the stop
\* frame rather than the transport's 3 s idle fallback.
NoSweepAfterEnd == swept = {}

\* While a filler context is live, the ledger remembers it is a filler.
FillerCtxRemembered ==
    \A c \in Fillers : synth[c] \in {"started", "audio"} => c \in Range(fillerCtxs)

TypeOK ==
    /\ llm \in [Replies -> {"idle", "streaming", "done", "aborted", "cancelled"}]
    /\ order \in Seq(Replies)
    /\ synth \in [Ctxs -> {"idle", "started", "audio", "stopped", "aborted"}]
    /\ timedOut \in [Ctxs -> BOOLEAN] /\ drainPending \in [Replies -> BOOLEAN]
    /\ pq \in Seq([k : {"audio", "stop"}, c : Ctxs]) /\ window \in BOOLEAN
    /\ swept \subseteq Replies
    /\ genAcc \in {NONE} \cup Replies /\ ended \subseteq Replies
    /\ claimed \subseteq Replies /\ queue \in Seq(Replies)
    /\ fifo \in Seq(Ctxs) /\ winOpen \in BOOLEAN
    /\ closedCtxs \subseteq Ctxs /\ fillerCtxs \in Seq(Fillers)
    /\ \A i \in DOMAIN turns :
         /\ turns[i].ctx \in Ctxs
         /\ turns[i].gen \in {NONE} \cup Replies
         /\ turns[i].audioStart \in {NONE, OWN}
=============================================================================
