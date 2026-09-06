--------------------------------- MODULE Ledger ---------------------------------
(***************************************************************************)
(* teaport — TranscriptLedger's bot-turn state machine.                     *)
(*                                                                          *)
(*   brain/teaport_brain/transcript_ledger.py at PR #13 / 3a51294:          *)
(*     on_process_frame, _new_bot, _ensure_bot, _is_filler, _ctx_ok,        *)
(*     _audio_ok, _finish_bot (the names of that code; see LedgerPlayout)    *)
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
(* MODE selects the design:                                                 *)
(*   "asWritten"  -- the ledger at PR #13. A boolean set by the LAST started *)
(*                   frame decides whether a BotStarted window is a          *)
(*                   filler's; any TTS frame may open a turn; BotStarted     *)
(*                   stamps audio_start on whatever turn is open; the        *)
(*                   in-flight generation is preferred; an interruption      *)
(*                   leaves _gen_acc/_pending_gen armed; the filler set is   *)
(*                   cleared on overflow. Fails every property below.        *)
(*   "windowHead" -- the fix. A window is identified by the FIRST audio     *)
(*                   pushed since the previous one closed (the transport     *)
(*                   plays in push order and the ledger sees every push      *)
(*                   first); untagged audio never opens a turn in a filler's *)
(*                   window; a turn's audio_start is its OWN playout start   *)
(*                   (window start + audio queued ahead), never a window     *)
(*                   opened for something else; the oldest unspoken response *)
(*                   is preferred; an interruption drops both texts; the     *)
(*                   filler set evicts its oldest entry.                     *)
(* Both are rejected designs now: the review of PR #13 took windowHead      *)
(* apart in turn, and the ledger as it stands is LedgerPlayout.tla.         *)
(* The properties, the rows and their traces are in README.md.              *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS MODE,          \* "asWritten" | "windowHead"
          Replies,       \* reply ids; each doubles as its primary TTS context id
          Fillers,       \* filler context ids -- narrator / tool-ack TTSSpeakFrames
          Untagged,      \* replies on the path with no TTSStartedFrame and no context_id
          Split,         \* BOOLEAN: a reply may be re-created under a 2nd context id mid-turn
          MaxInterrupts,
          MaxFillerCtxs  \* the 512 in the TTSStarted branch, shrunk to make overflow reachable

NONE   == "none"
PRESET == "preset"       \* audio_start preset from the previous turn's end (chained branch)
OWN    == "own"          \* windowHead: a ctx-less turn's audio_start is its own playout start
Fix    == MODE = "windowHead"

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
    order,       \* replies in the order their completions started
    synth,       \* [Ctxs -> idle | started | audio | stopped | aborted]
    pq,          \* audio at the output transport, one chunk per context, in push order
    window,      \* the transport's bot-speaking window is open
    played,      \* [Ctxs -> BOOLEAN]: the transport has played this context's chunk
    interrupts,
    \* --- the ledger: transcript_ledger.py ---
    genAcc,      \* _gen_acc      : NONE or the reply streaming now
    genClaimed,  \* _gen_claimed  : a live turn took the in-flight text (windowHead)
    pending,     \* _pending_gen (asWritten: at most one) / _pending (windowHead: a queue)
    pseq,        \* _pending_seq
    bot,         \* _bot          : a record; .open is "_bot is not None" (see NewBot)
    fillerCtxs,  \* _filler_ctxs
    lastFiller,  \* _last_started_filler            (asWritten)
    winOpen,     \* _window_open                    (windowHead)
    winHeadSeen, \* _window_head_seen               (windowHead)
    winHeadFiller, \* _window_head_filler           (windowHead)
    charted      \* self.events, assistant utterances only

envVars    == <<llm, order, synth, pq, window, played, interrupts>>
winVars    == <<winOpen, winHeadSeen, winHeadFiller>>
ledgerVars == <<genAcc, genClaimed, pending, pseq, bot, fillerCtxs, lastFiller,
                winOpen, winHeadSeen, winHeadFiller, charted>>
vars       == <<llm, order, synth, pq, window, played, interrupts,
                genAcc, genClaimed, pending, pseq, bot, fillerCtxs, lastFiller,
                winOpen, winHeadSeen, winHeadFiller, charted>>

NoBot0 == [open |-> FALSE, intended |-> NONE, live |-> FALSE, genSeq |-> 0,
           ctx |-> NONE, audioStart |-> NONE, synthDone |-> FALSE, qa |-> FALSE]

Init ==
    /\ llm = [r \in Replies |-> "idle"] /\ order = <<>>
    /\ synth = [c \in Ctxs |-> "idle"]
    /\ pq = <<>> /\ window = FALSE
    /\ played = [c \in Ctxs |-> FALSE]
    /\ interrupts = 0
    /\ genAcc = NONE /\ genClaimed = FALSE /\ pending = <<>> /\ pseq = 0
    /\ bot = NoBot0 /\ fillerCtxs = {} /\ lastFiller = FALSE
    /\ winOpen = FALSE /\ winHeadSeen = FALSE /\ winHeadFiller = FALSE
    /\ charted = <<>>

(* ---------------- the ledger's transition function ---------------- *)

BotOpen == bot.open

\* _bot is None. (TLC will not compare a record with a string, hence the flag.)
NoBot == NoBot0

\* _new_bot. genSeq = 0 stands for None (pseq >= 1 whenever pending is set).
\* windowHead: a completed, unspoken reply is OLDER than anything streaming, so a
\* turn opening now is its; only with nothing pending is the turn the in-flight one.
NewBotWith(ctx, pend) ==
    LET hasP == pend # <<>>
        live == genAcc # NONE /\ (~Fix \/ ~hasP)
    IN [open       |-> TRUE,
        intended   |-> IF live THEN genAcc ELSE (IF hasP THEN Head(pend) ELSE NONE),
        live       |-> live,
        genSeq     |-> IF ~live /\ hasP THEN pseq ELSE 0,
        ctx        |-> ctx,
        audioStart |-> NONE,
        synthDone  |-> FALSE,
        qa         |-> FALSE]         \* windowHead: queue-ahead known (first audio seen)
Own(b) == IF b.ctx = NONE THEN OWN ELSE b.ctx
NewBot(ctx) == NewBotWith(ctx, pending)

\* _ensure_bot: open a turn on the first TTS frame if there is text to attribute.
Creates == ~BotOpen /\ (genAcc # NONE \/ pending # <<>>)
Ensured == IF Creates THEN NewBot(NONE) ELSE bot
\* windowHead: a turn takes the oldest queued reply OFF the queue when it opens.
PopIfClaimed(created) == IF Fix /\ created /\ pending # <<>> THEN Tail(pending) ELSE pending
\* ...and if it is live instead, it has taken the in-flight response's text.
ClaimsLive(created) == created /\ genAcc # NONE /\ (~Fix \/ pending = <<>>)

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
FinishCharted(intr) == charted' = IF Charts(bot) THEN Append(charted, Chart(bot, intr)) ELSE charted
FinishBot(intr) ==
    /\ pending' = IF ~Fix /\ Consumes(bot) THEN <<>> ELSE pending   \* windowHead: popped at open
    /\ FinishCharted(intr)
ResetWindow == winOpen' = FALSE /\ winHeadSeen' = FALSE /\ winHeadFiller' = FALSE

\* _is_filler for a frame carrying context fc; only TTSStarted/TTSText carry the
\* append_to_context flag, so for audio and stop frames it is the ctx set alone.
FillerByCtx(fc) == fc # NONE /\ fc \in fillerCtxs

Ledger(f) ==
  CASE f.type = "LLMStart" ->                    \* LLMFullResponseStart + LLMText
         /\ genAcc' = f.gen /\ genClaimed' = FALSE
         /\ UNCHANGED <<pending, pseq, bot, fillerCtxs, lastFiller, charted, winVars>>
    [] f.type = "LLMEnd" ->                      \* LLMFullResponseEnd
         \* windowHead: after an interruption cleared _gen_acc, the End frame's text
         \* is empty and _pending_gen is left alone.
         LET dropped == Fix /\ genAcc # f.gen IN
         /\ pending' = IF dropped THEN pending
                       ELSE IF ~Fix THEN <<f.gen>>                        \* one slot: overwrite
                       ELSE IF (BotOpen /\ bot.live) \/ genClaimed THEN pending   \* a live turn is/was this reply's
                       ELSE Append(pending, f.gen)
         /\ pseq' = IF dropped THEN pseq ELSE pseq + 1
         /\ genAcc' = NONE
         /\ bot' = IF BotOpen /\ bot.live /\ ~dropped
                     THEN [bot EXCEPT !.intended = f.gen, !.live = FALSE, !.genSeq = pseq + 1]
                     ELSE bot
         /\ UNCHANGED <<genClaimed, fillerCtxs, lastFiller, charted, winVars>>
    [] f.type = "TTSStarted" ->
         /\ lastFiller' = f.filler
         /\ IF f.filler THEN
               /\ fillerCtxs' = IF f.ctx = NONE THEN fillerCtxs
                                ELSE IF Cardinality(fillerCtxs \cup {f.ctx}) > MaxFillerCtxs
                                     THEN (IF Fix                   \* the overflow guard
                                           THEN (fillerCtxs \ {CHOOSE c \in fillerCtxs : TRUE}) \cup {f.ctx}
                                           ELSE {})
                                     ELSE fillerCtxs \cup {f.ctx}
               /\ UNCHANGED <<bot, pending, charted>>
            ELSE IF ~BotOpen THEN                \* the opening frame names the turn's ctx
               /\ bot' = NewBot(f.ctx)
               /\ pending' = PopIfClaimed(TRUE)
               /\ UNCHANGED <<fillerCtxs, charted>>
            ELSE IF f.ctx = NONE \/ bot.ctx = NONE THEN   \* same turn; adopt the ctx if any
               /\ bot' = IF f.ctx # NONE THEN [bot EXCEPT !.ctx = f.ctx] ELSE bot
               /\ UNCHANGED <<fillerCtxs, pending, charted>>
            ELSE IF f.ctx # bot.ctx THEN         \* chained: chart the open turn, open the new
               LET pendAfter == IF ~Fix /\ Consumes(bot) THEN <<>> ELSE pending IN
               /\ FinishCharted(FALSE)
               /\ pending' = IF Fix /\ pendAfter # <<>> THEN Tail(pendAfter) ELSE pendAfter
               /\ bot' = [NewBotWith(f.ctx, pendAfter)
                            EXCEPT !.audioStart = IF Charts(bot) THEN PRESET ELSE NONE]
               /\ UNCHANGED fillerCtxs
            ELSE UNCHANGED <<bot, fillerCtxs, pending, charted>>
         /\ genClaimed' = (genClaimed \/
              (~f.filler /\ genAcc # NONE /\ (~Fix \/ pending = <<>>) /\
               (~BotOpen \/ (f.ctx # NONE /\ bot.ctx # NONE /\ f.ctx # bot.ctx))))
         /\ UNCHANGED <<genAcc, pseq, winVars>>
    [] f.type = "TTSAudio" ->                    \* the TTS push, or the transport's rebuild
         LET filler  == FillerByCtx(f.ctx)
             \* windowHead: the first audio since the window closed is what the next
             \* window opens FOR; its filler-ness is decided now, while the context
             \* is still in _filler_ctxs.
             headF   == IF winHeadSeen THEN winHeadFiller ELSE filler
             \* windowHead: only audio that NAMES a context may open a turn inside a
             \* filler's window -- the transport's untagged rebuild is the filler's.
             mayOpen == f.ctx # NONE \/ ~Fix \/ ~headF
             created == ~filler /\ mayOpen /\ Creates
             b       == IF filler THEN bot ELSE IF mayOpen THEN Ensured ELSE bot
             accepted == ~filler /\ b.open /\ (b.ctx = NONE \/ f.ctx = b.ctx)   \* _audio_ok
             b2      == IF accepted /\ b.ctx = NONE /\ f.ctx # NONE
                          THEN [b EXCEPT !.ctx = f.ctx] ELSE b            \* _audio_ok adopts
             \* windowHead: this turn's first accepted audio fixes where its playout
             \* begins -- after whatever the window already holds.
             b3      == IF Fix /\ accepted /\ ~b2.qa
                          THEN [b2 EXCEPT !.qa = TRUE,
                                          !.audioStart = IF winOpen /\ b2.audioStart = NONE
                                                           THEN Own(b2) ELSE b2.audioStart]
                          ELSE b2
         IN /\ bot' = b3
            /\ pending' = PopIfClaimed(created)
            /\ genClaimed' = (genClaimed \/ ClaimsLive(created))
            /\ winHeadSeen' = TRUE
            /\ winHeadFiller' = headF
            /\ UNCHANGED <<genAcc, pseq, fillerCtxs, lastFiller, charted, winOpen>>
    [] f.type = "BotStarted" ->                  \* anonymous; f.playing is ground truth
         LET fillerWin == IF Fix THEN winHeadFiller ELSE lastFiller
             created == ~fillerWin /\ Creates
             b == IF fillerWin THEN bot ELSE Ensured
         IN /\ pending' = PopIfClaimed(created)
            /\ genClaimed' = (genClaimed \/ ClaimsLive(created))
            /\ bot' = IF ~Fix
                        THEN (IF b.open /\ b.audioStart = NONE
                                THEN [b EXCEPT !.audioStart = f.playing] ELSE b)
                        \* windowHead: only a turn whose own audio is in this window,
                        \* and then its OWN start (window start + queue-ahead).
                        ELSE (IF b.open /\ b.audioStart = NONE /\ b.qa
                                THEN [b EXCEPT !.audioStart = Own(b)] ELSE b)
            /\ winOpen' = TRUE
            /\ UNCHANGED <<genAcc, pseq, fillerCtxs, lastFiller, charted,
                           winHeadSeen, winHeadFiller>>
    [] f.type = "TTSStopped" ->
         /\ IF FillerByCtx(f.ctx) THEN
               /\ fillerCtxs' = fillerCtxs \ {f.ctx}
               /\ UNCHANGED bot
            ELSE
               /\ bot' = IF BotOpen /\ (f.ctx = NONE \/ bot.ctx = NONE \/ f.ctx = bot.ctx)
                           THEN [bot EXCEPT !.synthDone = TRUE] ELSE bot
               /\ UNCHANGED fillerCtxs
         /\ UNCHANGED <<genAcc, genClaimed, pending, pseq, lastFiller, charted, winVars>>
    [] f.type = "BotStopped" ->
         \* windowHead: only a turn whose audio was IN this window ends with it; a
         \* turn whose TTSStarted came but whose audio has not keeps waiting.
         LET closes == BotOpen /\ (~Fix \/ bot.qa) IN
         /\ IF closes THEN FinishBot(FALSE) ELSE UNCHANGED <<pending, charted>>
         /\ bot' = IF closes THEN NoBot ELSE bot
         /\ ResetWindow
         /\ UNCHANGED <<genAcc, genClaimed, pseq, fillerCtxs, lastFiller>>
    [] f.type = "Interruption" ->
         /\ IF BotOpen THEN FinishCharted(TRUE) ELSE UNCHANGED charted
         \* windowHead: the completion is cancelled and the TTS queue flushed --
         \* neither text will be spoken, so neither is left for a turn to claim.
         /\ pending' = IF Fix THEN <<>>
                       ELSE IF BotOpen /\ Consumes(bot) THEN <<>> ELSE pending
         /\ genAcc' = IF Fix THEN NONE ELSE genAcc
         /\ bot' = NoBot
         /\ ResetWindow
         /\ UNCHANGED <<genClaimed, pseq, fillerCtxs, lastFiller>>

(* ---------------- the pipeline ---------------- *)

Streaming     == \E r \in Replies : llm[r] = "streaming"
Synthesizing  == \E c \in Ctxs : synth[c] \in {"started", "audio"}

\* A cancelled completion's End is pushed from its finally at cancellation, long
\* before the next run can begin: no completion starts while one is "aborted".
Aborted     == \E r \in Replies : llm[r] = "aborted"
LlmStart(r) ==
    /\ llm[r] = "idle" /\ ~Streaming /\ ~Aborted
    /\ llm' = [llm EXCEPT ![r] = "streaming"]
    /\ order' = Append(order, r)
    /\ Ledger([type |-> "LLMStart", gen |-> r])
    /\ UNCHANGED <<synth, pq, window, played, interrupts>>

LlmEnd(r) ==
    /\ llm[r] = "streaming"
    /\ llm' = [llm EXCEPT ![r] = "done"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<order, synth, pq, window, played, interrupts>>

\* An interrupted completion still ends: base_llm.py process_frame pushes
\* LLMFullResponseEndFrame in a finally (pipecat 1.7.0, lines 571-573), AFTER the
\* InterruptionFrame has gone through -- and the ledger takes the partial text as a
\* new _pending_gen. No more text follows, so no TTS ever starts for it.
LlmCancelEnd(r) ==
    /\ llm[r] = "aborted"
    /\ llm' = [llm EXCEPT ![r] = "cancelled"]
    /\ Ledger([type |-> "LLMEnd", gen |-> r])
    /\ UNCHANGED <<order, synth, pq, window, played, interrupts>>

\* Synthesis is sequential per the TTS service, and in the order responses reached
\* it: a reply's context cannot start while an earlier, live reply's has not.
\* (Not modelled: a completed reply with nothing speakable, whose context never
\* starts at all -- see README.md.) Playout lags synthesis arbitrarily.
Pos(r) == CHOOSE i \in DOMAIN order : order[i] = r
Earlier(r) == {order[i] : i \in 1..(Pos(r) - 1)}
CanStart(c) ==
    /\ synth[c] = "idle" /\ ~Synthesizing
    /\ IF c \in Fillers THEN TRUE                                  \* a reply needs text
         ELSE /\ llm[GenOf(c)] \in {"streaming", "done"}
              /\ \A r2 \in Earlier(GenOf(c)) :
                    llm[r2] \in {"aborted", "cancelled"} \/ synth[r2] # "idle"
    /\ IF IsSecond(c) THEN synth[GenOf(c)] = "stopped" ELSE TRUE   \* a re-created ctx follows its first

TtsStart(c) ==
    /\ CanStart(c)
    /\ synth' = [synth EXCEPT ![c] = "started"]
    /\ IF FrameCtx(c) = NONE                       \* the untagged path: no TTSStartedFrame is seen
         THEN UNCHANGED ledgerVars
         ELSE Ledger([type |-> "TTSStarted", ctx |-> FrameCtx(c), filler |-> c \in Fillers])
    /\ UNCHANGED <<llm, order, pq, window, played, interrupts>>

TtsAudio(c) ==
    /\ synth[c] = "started"
    /\ synth' = [synth EXCEPT ![c] = "audio"]
    /\ pq' = Append(pq, c)
    /\ Ledger([type |-> "TTSAudio", ctx |-> FrameCtx(c)])
    /\ UNCHANGED <<llm, order, window, played, interrupts>>

TtsStop(c) ==
    /\ synth[c] = "audio"
    /\ synth' = [synth EXCEPT ![c] = "stopped"]
    /\ Ledger([type |-> "TTSStopped", ctx |-> FrameCtx(c)])
    /\ UNCHANGED <<llm, order, pq, window, played, interrupts>>

PlayStart ==                                       \* BotStartedSpeaking: the window opens
    /\ ~window /\ pq # <<>>
    /\ window' = TRUE
    /\ Ledger([type |-> "BotStarted", playing |-> Head(pq)])
    /\ UNCHANGED <<llm, order, synth, pq, played, interrupts>>

PlayChunk ==                                       \* a chunk plays; the transport re-pushes it UNTAGGED
    /\ window /\ pq # <<>>
    /\ pq' = Tail(pq)
    /\ played' = [played EXCEPT ![Head(pq)] = TRUE]
    /\ Ledger([type |-> "TTSAudio", ctx |-> NONE])
    /\ UNCHANGED <<llm, order, synth, window, interrupts>>

PlayStop ==                                        \* BotStoppedSpeaking: the queue drained
    /\ window /\ pq = <<>>
    /\ window' = FALSE
    /\ Ledger([type |-> "BotStopped"])
    /\ UNCHANGED <<llm, order, synth, pq, played, interrupts>>

Interrupt ==                                       \* the user barges in
    /\ interrupts < MaxInterrupts
    /\ window \/ Synthesizing \/ Streaming         \* otherwise a no-op for everything modelled
    /\ interrupts' = interrupts + 1
    /\ pq' = <<>> /\ window' = FALSE               \* the transport flushes
    /\ synth' = [c \in Ctxs |-> IF synth[c] \in {"started", "audio"} THEN "aborted" ELSE synth[c]]
    \* A streaming completion is cancelled (its End frame still comes, see
    \* LlmCancelEnd); a completed reply whose synthesis had not begun is dropped
    \* with the TTS service's queue and never starts.
    /\ llm' = [r \in Replies |-> IF llm[r] = "streaming" THEN "aborted"
                                 ELSE IF llm[r] = "done" /\ synth[r] = "idle" THEN "cancelled"
                                 ELSE llm[r]]
    /\ Ledger([type |-> "Interruption"])
    /\ UNCHANGED <<order, played>>

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

\* A turn charted under a reply's context carries THAT reply's text -- never an
\* earlier response's, adopted by a turn that was already open when the context began.
ChartedTextMatchesContext ==
    \A i \in DOMAIN charted :
        charted[i].ctx \in ReplyCtxs => GenOf(charted[i].ctx) = charted[i].gen

\* While a filler context is live, the ledger remembers it is a filler.
FillerCtxRemembered ==
    \A c \in Fillers : synth[c] \in {"started", "audio"} => c \in fillerCtxs

TypeOK ==
    /\ llm \in [Replies -> {"idle", "streaming", "done", "aborted", "cancelled"}]
    /\ order \in Seq(Replies)
    /\ synth \in [Ctxs -> {"idle", "started", "audio", "stopped", "aborted"}]
    /\ pq \in Seq(Ctxs) /\ window \in BOOLEAN /\ played \in [Ctxs -> BOOLEAN]
    /\ genAcc \in {NONE} \cup Replies /\ genClaimed \in BOOLEAN /\ pending \in Seq(Replies)
    /\ fillerCtxs \subseteq Ctxs /\ lastFiller \in BOOLEAN
    /\ bot.open \in BOOLEAN
    /\ bot.intended \in {NONE} \cup Replies
    /\ bot.ctx \in {NONE} \cup Ctxs
    /\ bot.audioStart \in {NONE, PRESET, OWN} \cup Ctxs /\ bot.qa \in BOOLEAN
    /\ winOpen \in BOOLEAN /\ winHeadSeen \in BOOLEAN /\ winHeadFiller \in BOOLEAN
=============================================================================
