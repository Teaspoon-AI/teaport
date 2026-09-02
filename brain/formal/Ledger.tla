--------------------------------- MODULE Ledger ---------------------------------
(***************************************************************************)
(* teaport — TranscriptLedger's bot-turn state machine.                     *)
(*                                                                          *)
(*   brain/teaport_brain/transcript_ledger.py -- on_process_frame,          *)
(*     _new_bot, _ensure_bot, _is_filler, _ctx_ok, _audio_ok, _finish_bot   *)
(*                                                                          *)
(* Not a concurrency model: the ledger is a deterministic observer, and     *)
(* everything it gets wrong it gets wrong from the ORDER of frames over a   *)
(* small alphabet. The nondeterminism here is the pipeline's -- which of    *)
(* the LLM stream, TTS synthesis and transport playout advances next --     *)
(* and each pipeline step hands the ledger exactly one frame, atomically.   *)
(*                                                                          *)
(* Everything numeric is abstracted away: samples, pts, seconds. What is    *)
(* kept is what the numbers are computed FROM -- which turn a frame is      *)
(* attributed to, whether a turn ever got an audio_start and from which     *)
(* playout window, which response's text it claims, and whether it is      *)
(* charted as cut or complete. heard_fraction is 0 exactly when a cut       *)
(* turn has no audio_start (see _finish_bot), so "no audio_start" IS the    *)
(* heard-nothing verdict.                                                   *)
(*                                                                          *)
(* The environment includes the frame shapes the code's own comments and   *)
(* tests acknowledge: filler contexts (append_to_context=False), the output *)
(* transport's UNTAGGED rebuild of every played chunk (_audio_ok), gapless  *)
(* chaining inside one BotStarted window, a reply path with no              *)
(* TTSStartedFrame and no context_id (_ensure_bot), and -- under Split -- a *)
(* reply re-created under a second context id mid-turn.                    *)
(*                                                                          *)
(* This is the ledger AS WRITTEN at PR #13. The properties it fails are    *)
(* findings from that review; see README.md for the rows and traces.        *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Replies,       \* reply ids; each doubles as its primary TTS context id
          Fillers,       \* filler context ids -- narrator / tool-ack TTSSpeakFrames
          Untagged,      \* replies on the path with no TTSStartedFrame and no context_id
          Split,         \* BOOLEAN: a reply may be re-created under a 2nd context id mid-turn
          MaxInterrupts,
          MaxFillerCtxs  \* the 512 in the TTSStarted branch, shrunk to make overflow reachable

NONE   == "none"
PRESET == "preset"       \* audio_start preset from the previous turn's end (chained branch)

Range(s)  == {s[i] : i \in DOMAIN s}
CtxsOf(r) == IF Split THEN <<r, r \o "b">> ELSE <<r>>
ReplyCtxs == UNION {Range(CtxsOf(r)) : r \in Replies}
Ctxs      == ReplyCtxs \cup Fillers
GenOf(c)  == IF c \in Fillers THEN NONE ELSE CHOOSE r \in Replies : c \in Range(CtxsOf(r))
IsSecond(c) == c \in ReplyCtxs /\ c \notin Replies       \* the "b" context of a split reply
FrameCtx(c) == IF GenOf(c) \in Untagged THEN NONE ELSE c  \* the context_id its frames carry

VARIABLES
    \* --- the pipeline: ground truth ---
    llm,         \* [Replies -> idle | streaming | done | aborted | cancelled]
    synth,       \* [Ctxs -> idle | started | audio | stopped | aborted]
    pq,          \* audio at the output transport, one chunk per context, in push order
    window,      \* the transport's bot-speaking window is open
    played,      \* [Ctxs -> BOOLEAN]: the transport has played this context's chunk
    interrupts,
    \* --- the ledger: transcript_ledger.py ---
    genAcc,      \* _gen_acc      : NONE or the reply streaming now
    pending,     \* _pending_gen  : NONE or a reply
    pseq,        \* _pending_seq
    bot,         \* _bot          : a record; .open is "_bot is not None" (see NewBot)
    fillerCtxs,  \* _filler_ctxs
    lastFiller,  \* _last_started_filler
    charted      \* self.events, assistant utterances only

envVars    == <<llm, synth, pq, window, played, interrupts>>
ledgerVars == <<genAcc, pending, pseq, bot, fillerCtxs, lastFiller, charted>>
vars       == <<llm, synth, pq, window, played, interrupts,
                genAcc, pending, pseq, bot, fillerCtxs, lastFiller, charted>>

NoBot0 == [open |-> FALSE, intended |-> NONE, live |-> FALSE, genSeq |-> 0,
           ctx |-> NONE, audioStart |-> NONE, synthDone |-> FALSE]

Init ==
    /\ llm = [r \in Replies |-> "idle"]
    /\ synth = [c \in Ctxs |-> "idle"]
    /\ pq = <<>> /\ window = FALSE
    /\ played = [c \in Ctxs |-> FALSE]
    /\ interrupts = 0
    /\ genAcc = NONE /\ pending = NONE /\ pseq = 0
    /\ bot = NoBot0 /\ fillerCtxs = {} /\ lastFiller = FALSE
    /\ charted = <<>>

(* ---------------- the ledger's transition function ---------------- *)

BotOpen == bot.open

\* _bot is None. (TLC will not compare a record with a string, hence the flag.)
NoBot == NoBot0

\* _new_bot. genSeq = 0 stands for None (pseq >= 1 whenever pending is set).
NewBotWith(ctx, pend) ==
    LET live == genAcc # NONE
    IN [open       |-> TRUE,
        intended   |-> IF live THEN genAcc ELSE pend,
        live       |-> live,
        genSeq     |-> IF ~live /\ pend # NONE THEN pseq ELSE 0,
        ctx        |-> ctx,
        audioStart |-> NONE,
        synthDone  |-> FALSE]
NewBot(ctx) == NewBotWith(ctx, pending)

\* _ensure_bot: open a turn on the first TTS frame if there is text to attribute.
Ensured == IF ~BotOpen /\ (genAcc # NONE \/ pending # NONE) THEN NewBot(NONE) ELSE bot

\* Ground truth snapshotted into the chart, for the properties.
GenPlayed(g)   == \E c \in Range(CtxsOf(g)) : played[c]
GenQueued(g)   == \E c \in Range(CtxsOf(g)) : \E i \in DOMAIN pq : pq[i] = c
GenComplete(g) == \A c \in Range(CtxsOf(g)) : synth[c] \in {"audio", "stopped"}

\* _finish_bot, minus the arithmetic. Charts(b): something was attributed, so an
\* utterance is recorded; Consumes(b): _pending_gen is cleared, because this turn
\* spoke the text it holds (gen_seq None or == _pending_seq).
Charts(b)   == b.intended # NONE
Consumes(b) == Charts(b) /\ (b.genSeq = 0 \/ b.genSeq = pseq)
Chart(b, intr) ==
    [gen |-> b.intended, ctx |-> b.ctx, audioStart |-> b.audioStart, intr |-> intr,
     played |-> GenPlayed(b.intended), queued |-> GenQueued(b.intended),
     complete |-> GenComplete(b.intended)]
FinishBot(intr) ==
    /\ pending' = IF Consumes(bot) THEN NONE ELSE pending
    /\ charted' = IF Charts(bot) THEN Append(charted, Chart(bot, intr)) ELSE charted

\* _is_filler for a frame carrying context fc; only TTSStarted/TTSText carry the
\* append_to_context flag, so for audio and stop frames it is the ctx set alone.
FillerByCtx(fc) == fc # NONE /\ fc \in fillerCtxs

Ledger(f) ==
  CASE f.type = "LLMStart" ->                    \* LLMFullResponseStart + LLMText
         /\ genAcc' = f.gen
         /\ UNCHANGED <<pending, pseq, bot, fillerCtxs, lastFiller, charted>>
    [] f.type = "LLMEnd" ->                      \* LLMFullResponseEnd, text non-empty
         /\ pending' = f.gen /\ pseq' = pseq + 1 /\ genAcc' = NONE
         /\ bot' = IF BotOpen /\ bot.live
                     THEN [bot EXCEPT !.intended = f.gen, !.live = FALSE, !.genSeq = pseq + 1]
                     ELSE bot
         /\ UNCHANGED <<fillerCtxs, lastFiller, charted>>
    [] f.type = "TTSStarted" ->
         /\ lastFiller' = f.filler
         /\ IF f.filler THEN
               /\ fillerCtxs' = IF f.ctx = NONE THEN fillerCtxs
                                ELSE IF Cardinality(fillerCtxs \cup {f.ctx}) > MaxFillerCtxs
                                     THEN {}                        \* the overflow guard
                                     ELSE fillerCtxs \cup {f.ctx}
               /\ UNCHANGED <<bot, pending, charted>>
            ELSE IF ~BotOpen THEN                \* the opening frame names the turn's ctx
               /\ bot' = NewBot(f.ctx)
               /\ UNCHANGED <<fillerCtxs, pending, charted>>
            ELSE IF f.ctx = NONE \/ bot.ctx = NONE THEN   \* same turn; adopt the ctx if any
               /\ bot' = IF f.ctx # NONE THEN [bot EXCEPT !.ctx = f.ctx] ELSE bot
               /\ UNCHANGED <<fillerCtxs, pending, charted>>
            ELSE IF f.ctx # bot.ctx THEN         \* chained: chart the open turn, open the new
               /\ FinishBot(FALSE)
               /\ bot' = [NewBotWith(f.ctx, IF Consumes(bot) THEN NONE ELSE pending)
                            EXCEPT !.audioStart = IF Charts(bot) THEN PRESET ELSE NONE]
               /\ UNCHANGED fillerCtxs
            ELSE UNCHANGED <<bot, fillerCtxs, pending, charted>>
         /\ UNCHANGED <<genAcc, pseq>>
    [] f.type = "TTSAudio" ->                    \* the TTS push, or the transport's rebuild
         /\ bot' = IF FillerByCtx(f.ctx) THEN bot
                   ELSE LET b == Ensured IN
                        IF b.open /\ b.ctx = NONE /\ f.ctx # NONE
                          THEN [b EXCEPT !.ctx = f.ctx]     \* _audio_ok adopts
                          ELSE b                            \* counted or foreign; no state
         /\ UNCHANGED <<genAcc, pending, pseq, fillerCtxs, lastFiller, charted>>
    [] f.type = "BotStarted" ->                  \* anonymous; f.playing is ground truth
         /\ bot' = LET b == IF lastFiller THEN bot ELSE Ensured IN
                   IF b.open /\ b.audioStart = NONE
                     THEN [b EXCEPT !.audioStart = f.playing] ELSE b
         /\ UNCHANGED <<genAcc, pending, pseq, fillerCtxs, lastFiller, charted>>
    [] f.type = "TTSStopped" ->
         /\ IF FillerByCtx(f.ctx) THEN
               /\ fillerCtxs' = fillerCtxs \ {f.ctx}
               /\ UNCHANGED bot
            ELSE
               /\ bot' = IF BotOpen /\ (f.ctx = NONE \/ bot.ctx = NONE \/ f.ctx = bot.ctx)
                           THEN [bot EXCEPT !.synthDone = TRUE] ELSE bot
               /\ UNCHANGED fillerCtxs
         /\ UNCHANGED <<genAcc, pending, pseq, lastFiller, charted>>
    [] f.type = "BotStopped" ->
         /\ IF BotOpen THEN FinishBot(FALSE) ELSE UNCHANGED <<pending, charted>>
         /\ bot' = NoBot
         /\ UNCHANGED <<genAcc, pseq, fillerCtxs, lastFiller>>
    [] f.type = "Interruption" ->
         /\ IF BotOpen THEN FinishBot(TRUE) ELSE UNCHANGED <<pending, charted>>
         /\ bot' = NoBot
         /\ UNCHANGED <<genAcc, pseq, fillerCtxs, lastFiller>>

(* ---------------- the pipeline ---------------- *)

Streaming     == \E r \in Replies : llm[r] = "streaming"
Synthesizing  == \E c \in Ctxs : synth[c] \in {"started", "audio"}

LlmStart(r) ==
    /\ llm[r] = "idle" /\ ~Streaming
    /\ llm' = [llm EXCEPT ![r] = "streaming"]
    /\ Ledger([type |-> "LLMStart", gen |-> r])
    /\ UNCHANGED <<synth, pq, window, played, interrupts>>

LlmEnd(r) ==
    /\ llm[r] = "streaming"
    /\ llm' = [llm EXCEPT ![r] = "done"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<synth, pq, window, played, interrupts>>

\* An interrupted completion still ends: base_llm.py process_frame pushes
\* LLMFullResponseEndFrame in a finally (pipecat 0.0.108, lines 619-621), AFTER the
\* InterruptionFrame has gone through -- and the ledger takes the partial text as a
\* new _pending_gen. No more text follows, so no TTS ever starts for it.
LlmCancelEnd(r) ==
    /\ llm[r] = "aborted"
    /\ llm' = [llm EXCEPT ![r] = "cancelled"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<synth, pq, window, played, interrupts>>

\* Synthesis is sequential per the TTS service; playout lags it arbitrarily.
CanStart(c) ==
    /\ synth[c] = "idle" /\ ~Synthesizing
    /\ IF c \in Fillers THEN TRUE                                  \* a reply needs text
         ELSE llm[GenOf(c)] \in {"streaming", "done"}
    /\ IF IsSecond(c) THEN synth[GenOf(c)] = "stopped" ELSE TRUE   \* a re-created ctx follows its first

TtsStart(c) ==
    /\ CanStart(c)
    /\ synth' = [synth EXCEPT ![c] = "started"]
    /\ IF FrameCtx(c) = NONE                       \* the untagged path: no TTSStartedFrame is seen
         THEN UNCHANGED ledgerVars
         ELSE Ledger([type |-> "TTSStarted", ctx |-> FrameCtx(c), filler |-> c \in Fillers])
    /\ UNCHANGED <<llm, pq, window, played, interrupts>>

TtsAudio(c) ==
    /\ synth[c] = "started"
    /\ synth' = [synth EXCEPT ![c] = "audio"]
    /\ pq' = Append(pq, c)
    /\ Ledger([type |-> "TTSAudio", ctx |-> FrameCtx(c)])
    /\ UNCHANGED <<llm, window, played, interrupts>>

TtsStop(c) ==
    /\ synth[c] = "audio"
    /\ synth' = [synth EXCEPT ![c] = "stopped"]
    /\ Ledger([type |-> "TTSStopped", ctx |-> FrameCtx(c)])
    /\ UNCHANGED <<llm, pq, window, played, interrupts>>

PlayStart ==                                       \* BotStartedSpeaking: the window opens
    /\ ~window /\ pq # <<>>
    /\ window' = TRUE
    /\ Ledger([type |-> "BotStarted", playing |-> Head(pq)])
    /\ UNCHANGED <<llm, synth, pq, played, interrupts>>

PlayChunk ==                                       \* a chunk plays; the transport re-pushes it UNTAGGED
    /\ window /\ pq # <<>>
    /\ pq' = Tail(pq)
    /\ played' = [played EXCEPT ![Head(pq)] = TRUE]
    /\ Ledger([type |-> "TTSAudio", ctx |-> NONE])
    /\ UNCHANGED <<llm, synth, window, interrupts>>

PlayStop ==                                        \* BotStoppedSpeaking: the queue drained
    /\ window /\ pq = <<>>
    /\ window' = FALSE
    /\ Ledger([type |-> "BotStopped"])
    /\ UNCHANGED <<llm, synth, pq, played, interrupts>>

Interrupt ==                                       \* the user barges in
    /\ interrupts < MaxInterrupts
    /\ window \/ Synthesizing \/ Streaming         \* otherwise a no-op for everything modelled
    /\ interrupts' = interrupts + 1
    /\ pq' = <<>> /\ window' = FALSE               \* the transport flushes
    /\ synth' = [c \in Ctxs |-> IF synth[c] \in {"started", "audio"} THEN "aborted" ELSE synth[c]]
    /\ llm' = [r \in Replies |-> IF llm[r] = "streaming" THEN "aborted" ELSE llm[r]]
    /\ Ledger([type |-> "Interruption"])
    /\ UNCHANGED played

Next ==
    \/ \E r \in Replies : LlmStart(r) \/ LlmEnd(r) \/ LlmCancelEnd(r)
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
        (charted[i].intr /\ charted[i].audioStart = NONE) => ~charted[i].played

\* A reply charted COMPLETE had its own audio played, or at least queued at the
\* transport (the chained branch charts a turn while its tail may still be playing
\* out downstream; "queued" is that allowance). Never text whose TTS has not begun.
NoPhantomFullHeard ==
    \A i \in DOMAIN charted :
        ~charted[i].intr => (charted[i].played \/ charted[i].queued)

\* The playout start credited to a ctx-tagged turn is its own context's window,
\* never a filler's.
AudioStartIsOwn ==
    \A i \in DOMAIN charted :
        (charted[i].ctx # NONE /\ charted[i].audioStart \in Ctxs)
            => charted[i].audioStart = charted[i].ctx

\* A reply charted complete had all of its synthesis pushed -- a turn is never
\* closed as done while the same reply is still being synthesized under another id.
NoPrematureFullChart ==
    \A i \in DOMAIN charted : ~charted[i].intr => charted[i].complete

\* No response is charted twice (the stale-_pending_gen snapshot bug, fixed earlier).
ChartedAtMostOnce ==
    \A i, j \in DOMAIN charted : i # j => charted[i].gen # charted[j].gen

\* While a filler context is live, the ledger remembers it is a filler.
FillerCtxRemembered ==
    \A c \in Fillers : synth[c] \in {"started", "audio"} => c \in fillerCtxs

TypeOK ==
    /\ llm \in [Replies -> {"idle", "streaming", "done", "aborted", "cancelled"}]
    /\ synth \in [Ctxs -> {"idle", "started", "audio", "stopped", "aborted"}]
    /\ pq \in Seq(Ctxs) /\ window \in BOOLEAN /\ played \in [Ctxs -> BOOLEAN]
    /\ genAcc \in {NONE} \cup Replies /\ pending \in {NONE} \cup Replies
    /\ fillerCtxs \subseteq Ctxs /\ lastFiller \in BOOLEAN
    /\ bot.open \in BOOLEAN
    /\ bot.intended \in {NONE} \cup Replies
    /\ bot.ctx \in {NONE} \cup Ctxs
    /\ bot.audioStart \in {NONE, PRESET} \cup Ctxs
=============================================================================
