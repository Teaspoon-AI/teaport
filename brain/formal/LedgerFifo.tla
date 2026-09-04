------------------------------- MODULE LedgerFifo -------------------------------
(***************************************************************************)
(* teaport — TranscriptLedger as it ships: the 66b6812 redesign.            *)
(*                                                                          *)
(*   brain/teaport_brain/transcript_ledger.py (from 66b6812)                *)
(*                                                                          *)
(* Ledger.tla models the PR #13 ledger and its first fix (windowHead), both *)
(* superseded by this design. The environment is the same pipeline, the     *)
(* properties are the same eight, so the two modules answer the same        *)
(* questions of three designs. This design's shape is different enough for  *)
(* its own module: several turns open at once; the ledger keeps its own     *)
(* copy of the transport's FIFO; text is bound to a context by the TTS's    *)
(* queue order and CONFIRMED by pipecat's post-drain re-push of the         *)
(* response's End frame; and a turn that never got a chunk is charted as    *)
(* "never played".                                                          *)
(*                                                                          *)
(* Two switches the code's docstring names:                                 *)
(*   Drain       -- TRUE: the live wiring (tts/output given): the End       *)
(*                  frame's second sighting confirms/corrects the claim.    *)
(*                  FALSE: the hermetic tests' wiring: no drain signal, a   *)
(*                  turn is synthesized once its response has ended.        *)
(*   Unspeakable -- replies the TTS never opens a context for (nothing      *)
(*                  synthesizable, or a guard emptied them): they complete, *)
(*                  are queued, and sit at the queue's head for the next    *)
(*                  context to claim. The drain re-push is meant to catch   *)
(*                  that; the model says when it does.                      *)
(*                                                                          *)
(* Abstraction, as in Ledger.tla: no durations. The layout arithmetic (a    *)
(* chunk's playout start from the window start and the chunks ahead) is    *)
(* ASSUMED to agree with the transport, which the tests check numerically:  *)
(* the ledger's "has this chunk started" is the transport's.                *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Replies, Fillers, Unspeakable, Split, Drain, MaxInterrupts, MaxFillerCtxs

NONE == "none"
OWN  == "own"

Range(s)  == {s[i] : i \in DOMAIN s}
CtxsOf(r) == IF Split THEN <<r, r \o "b">> ELSE <<r>>
ReplyCtxs == UNION {Range(CtxsOf(r)) : r \in Replies}
Ctxs      == ReplyCtxs \cup Fillers
GenOf(c)  == IF c \in Fillers THEN NONE ELSE CHOOSE r \in Replies : c \in Range(CtxsOf(r))
IsSecond(c) == c \in ReplyCtxs /\ c \notin Replies

VARIABLES
    \* --- the pipeline ---
    llm, order, synth, pq, window, played, interrupts, drainSent,
    \* --- the ledger ---
    gen,          \* _gen: NONE or the reply streaming now
    claimed,      \* [Replies -> BOOLEAN]: entry["turn"] is not None
    ended,        \* [Replies -> BOOLEAN]: entry["ended"]
    endSeen,      \* [Replies -> BOOLEAN]: in _end_ids (End sighted at the TTS, text non-empty)
    ldrained,     \* [Replies -> BOOLEAN]: entry["drained"]
    queue,        \* _queue: replies, oldest first
    turns,        \* _turns: open turns in playout order (records, see NewTurn)
    fillerCtxs, closedCtxs,
    lfifo,        \* _fifo: contexts pushed since the window last closed
    winOpen,      \* _win_t0 is not None
    charted

envVars    == <<llm, order, synth, pq, window, played, interrupts, drainSent>>
ledgerVars == <<gen, claimed, ended, endSeen, ldrained, queue, turns, fillerCtxs,
                closedCtxs, lfifo, winOpen, charted>>
vars       == <<llm, order, synth, pq, window, played, interrupts, drainSent,
                gen, claimed, ended, endSeen, ldrained, queue, turns, fillerCtxs,
                closedCtxs, lfifo, winOpen, charted>>

NoR == [r \in Replies |-> FALSE]

Init ==
    /\ llm = [r \in Replies |-> "idle"] /\ order = <<>>
    /\ synth = [c \in Ctxs |-> "idle"]
    /\ pq = <<>> /\ window = FALSE
    /\ played = [c \in Ctxs |-> FALSE]
    /\ interrupts = 0 /\ drainSent = NoR
    /\ gen = NONE /\ claimed = NoR /\ ended = NoR /\ endSeen = NoR /\ ldrained = NoR
    /\ queue = <<>> /\ turns = <<>> /\ fillerCtxs = {} /\ closedCtxs = {}
    /\ lfifo = <<>> /\ winOpen = FALSE
    /\ charted = <<>>

(* ---------------- ground truth for the properties ---------------- *)

GenPlayed(g)   == \E c \in Range(CtxsOf(g)) : played[c]
GenQueued(g)   == \E c \in Range(CtxsOf(g)) : \E i \in DOMAIN pq : pq[i] = c
GenComplete(g) == \A c \in Range(CtxsOf(g)) : synth[c] \in {"audio", "stopped"}
\* The layout's "this chunk has started playing" -- assumed to agree with the transport.
Started(c)     == played[c] \/ (window /\ pq # <<>> /\ Head(pq) = c)

(* ---------------- the ledger ---------------- *)

NewTurn(ctx, entry) ==
    [ctx |-> ctx, entry |-> entry, audioStart |-> NONE,
     pushed |-> FALSE, played |-> FALSE, synthDone |-> FALSE]

HasTurn(c) == \E i \in DOMAIN turns : turns[i].ctx = c
TurnIdx(c) == CHOOSE i \in DOMAIN turns : turns[i].ctx = c
Newest     == turns[Len(turns)]

Chart(tn, intr, never) ==
    [gen |-> tn.entry, ctx |-> tn.ctx, audioStart |-> tn.audioStart, intr |-> intr,
     never |-> never,
     played |-> GenPlayed(tn.entry), queued |-> GenQueued(tn.entry),
     complete |-> GenComplete(tn.entry)]
\* _finish for a batch of turns, in order: a turn with no entry charts nothing.
ChartsOf(seq, intr, never) ==
    LET withEntry == SelectSeq(seq, LAMBDA tn : tn.entry # NONE)
    IN [i \in DOMAIN withEntry |-> Chart(withEntry[i], intr, never)]
CtxsOfTurns(seq) == {seq[i].ctx : i \in DOMAIN seq}

\* _open_turn: which text is this context speaking?
ClaimFor(c) ==
    IF c \in closedCtxs THEN NONE
    ELSE IF queue # <<>> THEN Head(queue)
    ELSE IF gen # NONE /\ ~claimed[gen] THEN gen
    ELSE NONE
PopIf(c) == IF c \notin closedCtxs /\ queue # <<>> THEN Tail(queue) ELSE queue
ClaimedAfter(c) == LET e == ClaimFor(c) IN
    IF e = NONE THEN claimed ELSE [claimed EXCEPT ![e] = TRUE]

\* _context_started(c): every open turn of another context is fully synthesized;
\* one that never got a chunk never will (charted "never played").
Others(c)    == SelectSeq(turns, LAMBDA tn : tn.ctx # c)
NeverOnes(c) == SelectSeq(Others(c), LAMBDA tn : ~tn.pushed)
AfterCtxStarted(c) ==
    LET keep == SelectSeq(turns, LAMBDA tn : tn.ctx = c \/ tn.pushed)
    IN [i \in DOMAIN keep |-> IF keep[i].ctx = c THEN keep[i]
                              ELSE [keep[i] EXCEPT !.synthDone = TRUE]]

\* _close_ready: chart the longest prefix of turns that are over.
Over(tn) == tn.pushed /\ tn.played /\
            (tn.entry = NONE \/ tn.synthDone \/ (~Drain /\ ended[tn.entry]))
ReadyLen == IF \E i \in DOMAIN turns : ~Over(turns[i])
              THEN (CHOOSE i \in DOMAIN turns : ~Over(turns[i]) /\
                        \A j \in 1..(i-1) : Over(turns[j])) - 1
              ELSE Len(turns)
ReadyPrefix == SubSeq(turns, 1, ReadyLen)
ReadyRest   == SubSeq(turns, ReadyLen + 1, Len(turns))

Ledger(f) ==
  CASE f.type = "LLMStart" ->
         /\ gen' = f.gen
         /\ claimed' = [claimed EXCEPT ![f.gen] = FALSE]
         /\ ended' = [ended EXCEPT ![f.gen] = FALSE]
         /\ UNCHANGED <<endSeen, ldrained, queue, turns, fillerCtxs, closedCtxs,
                        lfifo, winOpen, charted>>
    [] f.type = "LLMEnd" ->                      \* at the TTS's sighting
         IF gen = NONE THEN                      \* a cancelled completion's End
              UNCHANGED ledgerVars
         ELSE /\ gen' = NONE
              /\ ended' = [ended EXCEPT ![f.gen] = TRUE]
              /\ endSeen' = [endSeen EXCEPT ![f.gen] = TRUE]
              /\ queue' = IF claimed[f.gen] THEN queue ELSE Append(queue, f.gen)
              /\ UNCHANGED <<claimed, ldrained, turns, fillerCtxs, closedCtxs,
                             lfifo, winOpen, charted>>
    [] f.type = "EndDrained" ->                  \* the re-push, downstream of the TTS
         LET r == f.gen IN
         IF ~endSeen[r] \/ ldrained[r] THEN UNCHANGED ledgerVars
         ELSE
           LET openHolds == \E i \in DOMAIN turns : turns[i].entry = r
               \* the turn this response ends up on, if any
               tIdx == IF claimed[r] THEN (IF openHolds THEN TurnIdx(CHOOSE c \in CtxsOfTurns(turns) :
                                                    turns[TurnIdx(c)].entry = r) ELSE 0)
                       ELSE IF turns = <<>> THEN 0
                       ELSE Len(turns)
               cand == IF tIdx = 0 THEN NONE ELSE turns[tIdx]
               reassigned == ~claimed[r] /\ tIdx # 0 /\ cand.entry # r
               old == IF reassigned THEN cand.entry ELSE NONE
               queue1 == IF ~claimed[r] THEN SelectSeq(queue, LAMBDA x : x # r) ELSE queue
               t1 == IF tIdx = 0 THEN turns
                     ELSE [turns EXCEPT ![tIdx] = [@ EXCEPT !.entry = r, !.synthDone = TRUE]]
               never == tIdx # 0 /\ ~t1[tIdx].pushed
           IN /\ ldrained' = [ldrained EXCEPT ![r] = TRUE]
              /\ queue' = queue1
              /\ claimed' = [x \in Replies |-> IF x = r /\ tIdx # 0 THEN TRUE
                                               ELSE IF old # NONE /\ x = old THEN FALSE
                                               ELSE claimed[x]]
              /\ IF tIdx = 0 THEN
                    /\ UNCHANGED <<turns, closedCtxs, charted>>
                 ELSE IF never THEN
                    /\ turns' = SelectSeq(t1, LAMBDA tn : tn.ctx # t1[tIdx].ctx)
                    /\ closedCtxs' = closedCtxs \cup {t1[tIdx].ctx}
                    /\ charted' = charted \o ChartsOf(<<t1[tIdx]>>, TRUE, TRUE)
                 ELSE
                    \* _close_ready over the updated turns
                    LET ov(tn) == tn.pushed /\ tn.played /\
                                  (tn.entry = NONE \/ tn.synthDone \/ (~Drain /\ ended'[tn.entry]))
                        k == IF \E i \in DOMAIN t1 : ~ov(t1[i])
                               THEN (CHOOSE i \in DOMAIN t1 : ~ov(t1[i]) /\
                                         \A j \in 1..(i-1) : ov(t1[j])) - 1
                               ELSE Len(t1)
                        pre == SubSeq(t1, 1, k)
                    IN /\ turns' = SubSeq(t1, k + 1, Len(t1))
                       /\ closedCtxs' = closedCtxs \cup CtxsOfTurns(pre)
                       /\ charted' = charted \o ChartsOf(pre, FALSE, FALSE)
              /\ UNCHANGED <<gen, ended, endSeen, fillerCtxs, lfifo, winOpen>>
    [] f.type = "TTSStarted" ->
         LET c == f.ctx
             t1 == AfterCtxStarted(c)
             nev == NeverOnes(c)
             opens == ~f.filler /\ ~HasTurn(c)
             e == ClaimFor(c)
         IN /\ fillerCtxs' = IF f.filler THEN fillerCtxs \cup {c} ELSE fillerCtxs
            /\ turns' = IF opens THEN Append(t1, NewTurn(c, e)) ELSE t1
            /\ queue' = IF opens THEN PopIf(c) ELSE queue
            /\ claimed' = IF opens THEN ClaimedAfter(c) ELSE claimed
            /\ closedCtxs' = closedCtxs \cup CtxsOfTurns(nev)
            /\ charted' = charted \o ChartsOf(nev, TRUE, TRUE)
            /\ UNCHANGED <<gen, ended, endSeen, ldrained, lfifo, winOpen>>
    [] f.type = "TTSAudio" ->                    \* the TTS's tagged push only
         LET c == f.ctx IN
         IF c \in fillerCtxs THEN
              /\ lfifo' = Append(lfifo, c)
              /\ UNCHANGED <<gen, claimed, ended, endSeen, ldrained, queue, turns,
                             fillerCtxs, closedCtxs, winOpen, charted>>
         ELSE
           LET opens == ~HasTurn(c)
               e == ClaimFor(c)
               t0 == IF opens THEN Append(turns, NewTurn(c, e)) ELSE turns
               i == CHOOSE i \in DOMAIN t0 : t0[i].ctx = c
           IN /\ turns' = [t0 EXCEPT ![i] = [@ EXCEPT !.pushed = TRUE,
                                                  !.audioStart = IF @ = NONE /\ winOpen
                                                                   THEN OWN ELSE @]]
              /\ queue' = IF opens THEN PopIf(c) ELSE queue
              /\ claimed' = IF opens THEN ClaimedAfter(c) ELSE claimed
              /\ lfifo' = Append(lfifo, c)
              /\ UNCHANGED <<gen, ended, endSeen, ldrained, fillerCtxs, closedCtxs,
                             winOpen, charted>>
    [] f.type = "BotStarted" ->
         IF winOpen THEN UNCHANGED ledgerVars
         ELSE /\ winOpen' = TRUE
              /\ turns' = [i \in DOMAIN turns |->
                            IF turns[i].audioStart = NONE /\ turns[i].ctx \in Range(lfifo)
                              THEN [turns[i] EXCEPT !.audioStart = OWN] ELSE turns[i]]
              /\ UNCHANGED <<gen, claimed, ended, endSeen, ldrained, queue, fillerCtxs,
                             closedCtxs, lfifo, charted>>
    [] f.type = "TTSStopped" ->
         LET c == f.ctx IN
         IF c \in fillerCtxs THEN
              /\ fillerCtxs' = fillerCtxs \ {c}
              /\ UNCHANGED <<gen, claimed, ended, endSeen, ldrained, queue, turns,
                             closedCtxs, lfifo, winOpen, charted>>
         ELSE IF ~HasTurn(c) THEN UNCHANGED ledgerVars
         ELSE
           LET i == TurnIdx(c)
               t1 == [turns EXCEPT ![i] = [@ EXCEPT !.synthDone = TRUE]]
           IN IF ~t1[i].pushed THEN
                 /\ turns' = SelectSeq(t1, LAMBDA tn : tn.ctx # c)
                 /\ closedCtxs' = closedCtxs \cup {c}
                 /\ charted' = charted \o ChartsOf(<<t1[i]>>, TRUE, TRUE)
                 /\ UNCHANGED <<gen, claimed, ended, endSeen, ldrained, queue, fillerCtxs,
                                lfifo, winOpen>>
              ELSE
                 /\ turns' = t1
                 /\ UNCHANGED <<gen, claimed, ended, endSeen, ldrained, queue, fillerCtxs,
                                closedCtxs, lfifo, winOpen, charted>>
    [] f.type = "BotStopped" ->                  \* _window_closed + _close_ready
         LET credited == IF winOpen
                           THEN [i \in DOMAIN turns |->
                                   IF turns[i].ctx \in Range(lfifo)
                                     THEN [turns[i] EXCEPT !.played = TRUE] ELSE turns[i]]
                           ELSE turns
             ov(tn) == tn.pushed /\ tn.played /\
                       (tn.entry = NONE \/ tn.synthDone \/ (~Drain /\ ended[tn.entry]))
             k == IF \E i \in DOMAIN credited : ~ov(credited[i])
                    THEN (CHOOSE i \in DOMAIN credited : ~ov(credited[i]) /\
                              \A j \in 1..(i-1) : ov(credited[j])) - 1
                    ELSE Len(credited)
             pre == SubSeq(credited, 1, k)
         IN /\ lfifo' = <<>> /\ winOpen' = FALSE
            /\ turns' = SubSeq(credited, k + 1, Len(credited))
            /\ closedCtxs' = closedCtxs \cup CtxsOfTurns(pre)
            /\ charted' = charted \o ChartsOf(pre, FALSE, FALSE)
            /\ UNCHANGED <<gen, claimed, ended, endSeen, ldrained, queue, fillerCtxs>>
    [] f.type = "Interruption" ->                \* _interrupt
         LET heardAll(tn) == tn.synthDone /\ played[tn.ctx]       \* every chunk had played
             recs == [i \in DOMAIN turns |->
                        LET tn == turns[i] IN
                        [gen |-> tn.entry, ctx |-> tn.ctx, audioStart |-> tn.audioStart,
                         intr |-> ~heardAll(tn), never |-> FALSE,
                         played |-> tn.entry # NONE /\ GenPlayed(tn.entry),
                         queued |-> tn.entry # NONE /\ GenQueued(tn.entry),
                         complete |-> tn.entry # NONE /\ GenComplete(tn.entry)]]
             withEntry == SelectSeq(recs, LAMBDA rc : rc.gen # NONE)
         IN /\ charted' = charted \o withEntry
            /\ closedCtxs' = closedCtxs \cup CtxsOfTurns(turns)
            /\ turns' = <<>> /\ gen' = NONE /\ queue' = <<>>
            /\ lfifo' = <<>> /\ winOpen' = FALSE
            /\ UNCHANGED <<claimed, ended, endSeen, ldrained, fillerCtxs>>

(* ---------------- the pipeline ---------------- *)

Streaming    == \E r \in Replies : llm[r] = "streaming"
Synthesizing == \E c \in Ctxs : synth[c] \in {"started", "audio"}
Pos(r)       == CHOOSE i \in DOMAIN order : order[i] = r
Earlier(r)   == {order[i] : i \in 1..(Pos(r) - 1)}

\* A cancelled completion's End is pushed from its finally at cancellation, long
\* before the next run can begin: no completion starts while one is "aborted".
Aborted     == \E r \in Replies : llm[r] = "aborted"
LlmStart(r) ==
    /\ llm[r] = "idle" /\ ~Streaming /\ ~Aborted
    /\ llm' = [llm EXCEPT ![r] = "streaming"]
    /\ order' = Append(order, r)
    /\ Ledger([type |-> "LLMStart", gen |-> r])
    /\ UNCHANGED <<synth, pq, window, played, interrupts, drainSent>>

LlmEnd(r) ==
    /\ llm[r] = "streaming"
    /\ llm' = [llm EXCEPT ![r] = "done"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<order, synth, pq, window, played, interrupts, drainSent>>

LlmCancelEnd(r) ==                     \* the cancelled completion's End, from a finally
    /\ llm[r] = "aborted"
    /\ llm' = [llm EXCEPT ![r] = "cancelled"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<order, synth, pq, window, played, interrupts, drainSent>>

\* pipecat re-pushes the response's End frame once the context that spoke it has
\* pushed its last audio (tts_service._maybe_reset_word_timestamps). Live wiring only.
EndDrained(r) ==
    /\ Drain /\ llm[r] = "done" /\ ~drainSent[r] /\ synth[r] = "stopped"
    /\ drainSent' = [drainSent EXCEPT ![r] = TRUE]
    /\ Ledger([type |-> "EndDrained", gen |-> r])
    /\ UNCHANGED <<llm, order, synth, pq, window, played, interrupts>>

CanStart(c) ==
    /\ synth[c] = "idle" /\ ~Synthesizing
    /\ IF c \in Fillers THEN TRUE
         ELSE /\ GenOf(c) \notin Unspeakable                    \* the TTS opens no context
              /\ llm[GenOf(c)] \in {"streaming", "done"}
              /\ \A r2 \in Earlier(GenOf(c)) :
                    llm[r2] \in {"aborted", "cancelled"} \/ r2 \in Unspeakable
                        \/ synth[r2] # "idle"
    /\ IF IsSecond(c) THEN synth[GenOf(c)] = "stopped" ELSE TRUE

TtsStart(c) ==
    /\ CanStart(c)
    /\ synth' = [synth EXCEPT ![c] = "started"]
    /\ Ledger([type |-> "TTSStarted", ctx |-> c, filler |-> c \in Fillers])
    /\ UNCHANGED <<llm, order, pq, window, played, interrupts, drainSent>>

TtsAudio(c) ==
    /\ synth[c] = "started"
    /\ synth' = [synth EXCEPT ![c] = "audio"]
    /\ pq' = Append(pq, c)
    /\ Ledger([type |-> "TTSAudio", ctx |-> c])
    /\ UNCHANGED <<llm, order, window, played, interrupts, drainSent>>

TtsStop(c) ==
    /\ synth[c] = "audio"
    /\ synth' = [synth EXCEPT ![c] = "stopped"]
    /\ Ledger([type |-> "TTSStopped", ctx |-> c])
    /\ UNCHANGED <<llm, order, pq, window, played, interrupts, drainSent>>

PlayStart ==
    /\ ~window /\ pq # <<>>
    /\ window' = TRUE
    /\ Ledger([type |-> "BotStarted"])
    /\ UNCHANGED <<llm, order, synth, pq, played, interrupts, drainSent>>

PlayChunk ==                                      \* the untagged rebuild is ignored by this design
    /\ window /\ pq # <<>>
    /\ pq' = Tail(pq)
    /\ played' = [played EXCEPT ![Head(pq)] = TRUE]
    /\ UNCHANGED <<llm, order, synth, window, interrupts, drainSent, ledgerVars>>

PlayStop ==
    /\ window /\ pq = <<>>
    /\ window' = FALSE
    /\ Ledger([type |-> "BotStopped"])
    /\ UNCHANGED <<llm, order, synth, pq, played, interrupts, drainSent>>

Interrupt ==
    /\ interrupts < MaxInterrupts
    /\ window \/ Synthesizing \/ Streaming
    /\ interrupts' = interrupts + 1
    /\ pq' = <<>> /\ window' = FALSE
    /\ synth' = [c \in Ctxs |-> IF synth[c] \in {"started", "audio"} THEN "aborted" ELSE synth[c]]
    /\ llm' = [r \in Replies |-> IF llm[r] = "streaming" THEN "aborted"
                                 ELSE IF llm[r] = "done" /\ synth[r] = "idle" THEN "cancelled"
                                 ELSE llm[r]]
    /\ Ledger([type |-> "Interruption"])
    /\ UNCHANGED <<order, played, drainSent>>

Next ==
    \/ \E r \in Replies : LlmStart(r) \/ LlmEnd(r) \/ LlmCancelEnd(r) \/ EndDrained(r)
    \/ \E c \in Ctxs : TtsStart(c) \/ TtsAudio(c) \/ TtsStop(c)
    \/ PlayStart \/ PlayChunk \/ PlayStop
    \/ Interrupt

Spec == Init /\ [][Next]_vars

(* ---------------- properties (as in Ledger.tla) ---------------- *)

NoUnheardWhenPlayed ==
    \A i \in DOMAIN charted :
        (charted[i].intr /\ charted[i].audioStart = NONE) => ~charted[i].played
NoPhantomFullHeard ==
    \A i \in DOMAIN charted :
        ~charted[i].intr => (charted[i].played \/ charted[i].queued)
AudioStartIsOwn ==
    \A i \in DOMAIN charted :
        (charted[i].ctx # NONE /\ charted[i].audioStart \in Ctxs)
            => charted[i].audioStart = charted[i].ctx
NoPrematureFullChart ==
    \A i \in DOMAIN charted : ~charted[i].intr => charted[i].complete
ChartedAtMostOnce ==
    \A i, j \in DOMAIN charted : i # j => charted[i].gen # charted[j].gen
ChartedTextMatchesContext ==
    \A i \in DOMAIN charted :
        charted[i].ctx \in ReplyCtxs => GenOf(charted[i].ctx) = charted[i].gen
FillerCtxRemembered ==
    \A c \in Fillers : synth[c] \in {"started", "audio"} => c \in fillerCtxs
\* This design's own claim: a reply that was played is never charted "never played".
NeverMeansNever ==
    \A i \in DOMAIN charted : charted[i].never => ~charted[i].played

TypeOK ==
    /\ llm \in [Replies -> {"idle", "streaming", "done", "aborted", "cancelled"}]
    /\ synth \in [Ctxs -> {"idle", "started", "audio", "stopped", "aborted"}]
    /\ pq \in Seq(Ctxs) /\ window \in BOOLEAN /\ played \in [Ctxs -> BOOLEAN]
    /\ gen \in {NONE} \cup Replies /\ queue \in Seq(Replies)
    /\ fillerCtxs \subseteq Ctxs /\ closedCtxs \subseteq Ctxs
    /\ lfifo \in Seq(Ctxs) /\ winOpen \in BOOLEAN
    /\ \A i \in DOMAIN turns : /\ turns[i].ctx \in Ctxs
                               /\ turns[i].entry \in {NONE} \cup Replies
                               /\ turns[i].audioStart \in {NONE, OWN}
=============================================================================
