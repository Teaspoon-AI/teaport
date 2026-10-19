--------------------------------- MODULE Owed ---------------------------------
(***************************************************************************)
(* teaport — "owed until heard" (#203): handing an async consult's answer   *)
(* to the caller. Written before the code, which follows it action for      *)
(* action (README.md, "Actions and the code they become"):                  *)
(*                                                                          *)
(*   brain/teaport_brain/owed.py           -- the record, the note, Settle  *)
(*   services.BoundedOpenAILLMService._open_stream -- the render hook       *)
(*   followup_gate.FollowupGate            -- gate.ask, the two-slot hold   *)
(*   followup_gate / transcript_ledger     -- the settle feed               *)
(*                                                                          *)
(* Replaces Followup.tla's delivery loop (MODE = "heard"). That loop kept a *)
(* STANDING ORDER in the context and retired it at the read, then asked     *)
(* whether the caller heard >= _MIN_HEARD of the reply's WORDS. #203 broke  *)
(* the premise: 1.1 s of a lead-in that restated the question was 31% of    *)
(* the words and none of the information, the trigger was retired as        *)
(* "already given", and "I'm ready." was answered with a second lookup.     *)
(*                                                                          *)
(* The new shape has three concepts. The RECORD (owed.py: none -> owed ->   *)
(* closed) lives outside the conversation. The NOTE is rendered into every  *)
(* request while the record is owed and is never stored. The EVIDENCE that  *)
(* closes the record is the caller having heard every item of the outcome   *)
(* (DESIGN.md §3), or an uncut telling run. The brain speaks unprompted at  *)
(* most twice per record: one RUN when the answer lands, and one RE-OFFER   *)
(* at a long lull after the caller cut in or spoke (§4 (a), the user's      *)
(* choice, 2026-10-16).                                                     *)
(*                                                                          *)
(* What the model says is NONDETERMINISTIC, bounded by what the replay      *)
(* rounds measured (DESIGN.md "What the evidence supports", RESULTS-round1  *)
(* to -round5 in the #203 design notes):                                    *)
(*   - a request carrying the note: tell / tell part / defer / other        *)
(*     (X1-I 18/20 answer, W 18-20/20, HOLD 60/60 defer); a re-lookup only  *)
(*     under FLAKY -- measured, not hypothetical: 1/20 in X1-P and in X2-I  *)
(*     (round 1), 0 in round 2's "I'm ready"/"go ahead" arms (a request for *)
(*     MORE -- a link, a summary, N-more 4/20 -- is a new request, not a    *)
(*     re-lookup)                                                           *)
(*   - a request without the note, rendered after the answer landed and     *)
(*     before it was heard: anything, a re-lookup included (B0 19/20)       *)
(*   - without the note before the answer landed: nothing to tell           *)
(*   - without the note after it was heard (closed: the telling is in the   *)
(*     history): mention it or not, no re-lookup (K-M-ready 15 ok / 5       *)
(*     mention / 0 lookups)                                                 *)
(*   - the landing run: it tells (D-first 20/20, round 5 C 20/20); PARTIAL  *)
(*     lets it tell only some items, SILENT lets it produce no audio (the   *)
(*     guard emptied it, #70; the LLM failed after its first token)         *)
(*   - the re-offer run: tells or calls stay_silent (round 5: 10/20 after   *)
(*     "hold on", 2/20 after an unrelated exchange)                         *)
(*                                                                          *)
(* The outcome is abstracted to TWO ITEMS with terms (a two-story list), so *)
(* that "one item of two" and "a filler that shares a common word" are      *)
(* expressible. What the caller heard of a reply is one of                  *)
(*   nothing | lead (a lead-in, no item terms) | filler (no item terms, but *)
(*   common words) | some (one item's terms) | all (every item's terms)     *)
(* and the ghost `heard` is "some reply's heard text carried every item":   *)
(* the lexical premise (§3, the corpus test) is that the shipped rule reads *)
(* that exactly, so the model can ask whether the RULE closes on it.        *)
(*                                                                          *)
(* MUT, a set ({} = the design). The evidence rule (one at a time):         *)
(*   "fraction"    -- the rule before #203: a run's reply heard >= 1/3 of   *)
(*                    its words closes (the removed _MIN_HEARD). #203.      *)
(*   "countSettle" -- two uncut replies that carried the note close (the    *)
(*                    inbox proposal; X2-Igone: 20/20 re-lookups after).    *)
(*   "singleWord"  -- no common-word list and no 2-term minimum: a filler   *)
(*                    whose common words meet the items closes ("Sure, take *)
(*                    your time." against an outcome with "time" in it).    *)
(*   "oneItem"     -- one item's terms close a two-item outcome.            *)
(* The rest:                                                                *)
(*   "storeNote"   -- the note is written into the context once, at         *)
(*                    landing, instead of rendered per request.             *)
(*   "runAfterCut" -- no bound on unprompted runs: a run at every lull      *)
(*                    after an audible cut (today's 3-attempt loop, §4 (c)).*)
(*   "lagSettle"   -- the readers of the record (the run decision, the      *)
(*                    lull, the render) do not wait for the settle feed:    *)
(*                    the ledger charts a reply that played through only at *)
(*                    the next bot activity (the 2026-09-11 14:23 loss, in  *)
(*                    the removed TranscriptLedger.next_assistant's doc).   *)
(*   "oneSlot"     -- the hold keeps one frame, a newer one replacing the   *)
(*                    older (FollowupGate._hold_answer before #203).        *)
(*   "askOnly"     -- a run is validated when it is asked (gate.ask) and    *)
(*                    not again when it goes: a post-tool answer released   *)
(*                    from the same hold ahead of it can tell first.        *)
(*   "turnLevel"   -- the run's window is turn-level (wait_until_idle's     *)
(*                    `_user` is the TURN): it goes into speech whose words *)
(*                    have not started a turn yet (#197).                   *)
(*   "vadLevel"    -- the run's window is the VAD alone: it goes inside a   *)
(*                    caller's turn (words in, not yet answered) or between *)
(*                    a tool call and its answer (Followup's                *)
(*                    NoInterjectMidTurn).                                  *)
(*   "endClearsTool" -- the gate's turn ends at a completion's End even when *)
(*                    it put a sync tool call out with its text ("Let me    *)
(*                    check." + get_weather): the run goes between the call *)
(*                    and its answer (FollowupGate before the review round).*)
(*   "staleFlag"   -- the gate's tool round is ONE flag, set by a call's     *)
(*                    in-progress frame: uninterruptible, it can reach the  *)
(*                    gate after the barge-in that cancelled the call, and  *)
(*                    with no completion after it nothing clears the flag   *)
(*                    (FollowupGate before the second review round).        *)
(*   "ledgerMax"   -- the feed reads the ledger's heard_text as it is: the  *)
(*                    longer of the pts-exact prefix and an audio-fraction  *)
(*                    estimate, so a cut can read as more than played.      *)
(*   "renderInGet" -- the render hook in get_chat_completions instead of    *)
(*                    _open_stream: the speculative reply calls             *)
(*                    _open_stream directly (speculate.py:148) and goes out *)
(*                    bare.                                                 *)
(*                                                                          *)
(* REOFFER: what arms the one re-offer.                                     *)
(*   "cutOrTurn" -- DESIGN.md §4 as written: an audible cut or a caller     *)
(*                  turn since the answer landed.                           *)
(*   "anyMiss"   -- also any run that ended without closing the record: a   *)
(*                  run dropped at the hold cap, a run with no audio, a run *)
(*                  that played through with an item untold (RUNCLOSE       *)
(*                  "terms").                                               *)
(*                                                                          *)
(* RUNCLOSE: what closes an item with no terms (a failure, a bare "done"). *)
(*   "uncut" -- DESIGN.md §3 as written: a run that played to the end       *)
(*              uncut closes the record, whatever it said.                  *)
(*   "terms" -- an item with terms is closed by its terms, from a run as    *)
(*              from any other reply; one with none only by an uncut run.   *)
(*   "about" -- as "terms", and a term-less item also by ANY noted reply    *)
(*              heard to its end (played through, or cut after its last     *)
(*              word) that is about the record (it shares a non-common word *)
(*              with the request: owed._about) -- the failure the caller's  *)
(*              own turn told in full is not re-offered (Fable's review).   *)
(*                                                                          *)
(* OUTCOME: "items" -- two items with terms (a two-story list); "failure"   *)
(*   -- one term-less item (the consult failed). The lexical premise for    *)
(*   the latter: a reply about the record heard to its end told it -- a     *)
(*   failure is one statement, told whole or not at all (no "part"), and    *)
(*   the telling is the reply (a question after it, cut, reads owed).       *)
(*                                                                          *)
(* SOURCE: where the record comes from.                                    *)
(*   "consult" -- an async consult lands (Land): the model above.           *)
(*   "sync"    -- no consult: an ANSWER tool's result (web_search,          *)
(*                web_fetch: tools.Tool.answers) is told by a reply, and a  *)
(*                CUT of a telling before every item was heard makes the    *)
(*                record (#204, 2026-10-18: the weather and toy-store       *)
(*                calls). No run: the caller's turn that cut it carries the *)
(*                note. The context's own state is `sctx`: the result fresh *)
(*                (in the context, untold), told (a telling heard in full   *)
(*                is in the history), or cut (a telling's heard prefix).    *)
(*                WHICH reply tells it is the BOOK's: the answer is fresh   *)
(*                (`carry`) from the first render that sees it until a      *)
(*                telling is heard in full or recorded -- whoever asked:    *)
(*                the post-tool reply, or -- the post-tool request dropped  *)
(*                by a caller turn (TurnStart) -- that turn's reply, which  *)
(*                reads the fresh result. The record is made at the cut's   *)
(*                settle, which re-reads the live book (LateSettle; LATE:   *)
(*                the readers' bounded wait runs out first). Any            *)
(*                reply may call again (a CHAIN, web_search then web_fetch, *)
(*                at most two rounds here): bare, after a line ("Let me get *)
(*                their hours."), or after telling it ("There's a Toy Joy   *)
(*                at 5501 Burnet. Let me get their hours.").                *)
(*   "action"  -- a sync ACTION tool (set_volume, switch_voice, remember):  *)
(*                its reply confirms what the caller asked for, which is    *)
(*                nothing they still need to hear -- the ghost `heard`      *)
(*                holds from the start, and no record is made.              *)
(*                                                                          *)
(* STREAMCUT: a cut may land while the telling's stream is still going and  *)
(*   its first audio already plays (a long reply: the LLM streams past the  *)
(*   TTS's first clause). The design makes no record then: the text as far  *)
(*   as it got is not the answer (`partialRecord`). FALSE in the design     *)
(*   rows: the premise that a one-or-two-sentence answer streams to its end *)
(*   before its first audio; TRUE, the residual is a re-lookup (B0), the    *)
(*   cost `ow_sync_streamCut` shows -- never a lost or a repeated answer    *)
(*   (`ow_sync_streamCut_rest`).                                            *)
(*                                                                          *)
(* The sync record's mutations (MUT), each the code before the #204 review: *)
(*   noCutRecord   -- no record from a cut answer: the caller's next turn   *)
(*                    has only the cut prefix and the result, and the model *)
(*                    looks it up again (W-goahead 20/20 re-fetch, #204).   *)
(*   partialRecord -- a record from whatever had streamed at the cut: a     *)
(*                    fragment of the answer, rendered as "everything that  *)
(*                    result contains", closed by a telling of the fragment.*)
(*   actionOwed    -- an action's cut confirmation is a record too, and its *)
(*                    re-offer repeats what the caller asked for.           *)
(*   postOnly      -- only the post-tool request carries the answer (the    *)
(*                    frame's direction decides): a caller turn's telling   *)
(*                    of the fresh result, cut, makes no record.            *)
(*   callNoRecord  -- a telling that also put another call out makes no     *)
(*                    record when cut (`not reply.calls`).                  *)
(*   lateReplace   -- the cut's late settle makes its record whatever the   *)
(*                    book holds (c8e046a): after a full telling, or over   *)
(*                    another reply's record, its re-offer counter reset.   *)
(*   lateNoEvidence -- a reply rendered with the fresh answer is no         *)
(*                    evidence for the record made of it after its render.  *)
(*                                                                          *)
(* adoptDropsKeys (MUT) -- the commit that adopts a speculative reply's     *)
(*   stream does not register the keys its note carried (the note was in    *)
(*   the request, rendered in the speculation's own task): its telling is   *)
(*   no evidence, and the record stays owed after it was heard.             *)
(*                                                                          *)
(* Checked designs and results are in README.md next to this file.          *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxSpeech,   \* bound on the caller's bursts of speech (VAD starts)
          MUT,         \* the mutations switched on (see the header); {} = the design
          REOFFER,     \* "cutOrTurn" | "anyMiss"
          RUNCLOSE,    \* "uncut" | "terms" | "about" (see the header)
          OUTCOME,     \* "items" | "failure" (see the header)
          SOURCE,      \* "consult" | "sync" | "action" (see the header)
          FLAKY,       \* a request carrying the note may still re-look-up
          PARTIAL,     \* a telling run may carry only some of the items
          SILENT,      \* a run's completion may produce no audio at all
          STREAMCUT,   \* a cut may land mid-stream with audio playing (see the header)
          LATE         \* the readers' bounded wait may run out before a sync cut's settle

Mut(m) == m \in MUT
Runs   == {"run", "reoffer"}        \* the brain's own, unprompted requests
Opt(b, x) == IF b THEN {x} ELSE {}
Sync   == SOURCE \in {"sync", "action"}           \* a sync tool round, no consult
Calls  == {"tool", "texttool", "telltool"}        \* a reply that puts a sync call out
Tells  == {"tell", "part", "telltool"}            \* a reply that tells the answer
MaxRounds == 2                                    \* sync tool rounds (a chain of two)

VARIABLES
    \* --- the record (owed.py) ---
    st,           \* "none" (the consult is still running) | "owed" | "closed"
    fed,          \* the settle feed: "close" = a reply that closes the record has
                  \*   ended and owed.py has not been told yet; "none" = settled
    uncut,        \* countSettle only: noted replies played through, up to 2
    sctx,         \* Sync: the tool's answer in the context with no record --
                  \*   "none" (no result yet) | "fresh" | "told" | "cut" (see the header)
    frag,         \* partialRecord only: the record's outcome is a fragment of the answer
    late,         \* Sync: a cut telling's settle, still to come -- the record it makes:
                  \*   "none" | "rec" | "frag" (frag: partialRecord's fragment)
    gone,         \* LATE: the readers' wait for that settle ran out (SETTLE_WAIT_S)
    missed,       \* the re-offer is armed (REOFFER says by what)
    reoffers,     \* re-offer runs asked (a held-then-dropped one counts)
    \* --- the ghosts ---
    heard,        \* the caller really has every item (some reply's heard text did)
    repeat,       \* an unprompted telling began after `heard`
    \* --- the line (the #198 shape: UserTurn.tla, POSTTOOL = "gate") ---
    vad,          \* the caller is audibly speaking (VADUserStartedSpeakingFrame)
    pend,         \* speech has begun whose words have not resolved: no turn start,
                  \*   no commit carrying them, no release timer since its VAD start
    turn,         \* a caller turn is open (UserStartedSpeakingFrame .. its commit)
    speech,       \* bursts of speech so far (the bound)
    \* --- the door: FollowupGate and the LLM's input ---
    q,            \* the LLM's next request: "none" | "caller" (a commit's context
                  \*   frame) | "post" (a sync tool result's answer, pushed up)
    hPost,        \* the post-tool answer is held in the gate (speech pending)
    rloc,         \* the run: "none" | "wait" (gate.ask'd: held while speech is
                  \*   pending, then waiting for a turn-free window)
    rk,           \* what the waiting run is: "none" | "run" | "reoffer"
    tres,         \* a sync tool's call is out and its result not yet back
    tend,         \* ...and the completion that put it out has ended at the gate (it
                  \*   had text: "Let me check." + the call); only endClearsTool reads it
    mark,         \* the call's FunctionCallInProgressFrame is still on its way to the
                  \*   gate: uninterruptible, it waits in the TTS's and the transport's
                  \*   queues behind the line's audio, and survives the barge-in's reset
    gtool,        \* the gate's own record of a call out (the design: its ids, an id
                  \*   added by its in-progress frame unless already settled or
                  \*   cancelled; staleFlag: one flag)
    rounds,       \* Sync: tool rounds put out so far (MaxRounds bounds a chain)
    \* --- the request in flight and its reply (one at a time) ---
    req,          \* "none" | "caller" | "post" | "run" | "reoffer"
    rst,          \* the record's state when the request was rendered
    noted,        \* the request carried the note
    nreg,         \* ...and the reply is registered as carrying it: its evidence counts
    say,          \* what the model chose: tell | part | defer | other | lookup
    ph,           \* "none" | "gen" (asked, no output yet) | "play" (audio playing)
    sdone,        \* the reply's stream has ended (FALSE only while it plays, STREAMCUT)
    carry,        \* Sync: the request was rendered with the answer fresh in the BOOK (no
                  \*   record of it, not told): owed.Owed.noted's Reply.fresh
    ended,        \* the session ended (the client left, the call hung up)
    \* --- monitors ---
    runUnder,     \* a run went to the LLM while caller speech was pending
    runMid,       \* a run went inside a caller's turn or a tool round
    displaced,    \* the hold dropped a run or a post-tool answer for the other
    staleNote,    \* a request carried the note for a record already closed
    noNote,       \* a request rendered while owed went without the note
    staleHeard,   \* a request carried "NOT heard" to a caller who had heard it
    relook,       \* the model looked the answer up again although it had it
    offers,       \* re-offers asked, ever (OneReoffer; a replaced record's reset counter
                  \*   does not reset this)
    got           \* what the caller heard of the last reply that ended (for traces)

rec   == <<st, fed, uncut, missed, reoffers, sctx, frag, late, gone>>
line  == <<vad, pend, turn, speech>>
door  == <<q, hPost, rloc, rk, tres, tend, mark, gtool, rounds>>
reply == <<req, rst, noted, nreg, say, ph, sdone, carry>>
mons  == <<runUnder, runMid, displaced, staleNote, noNote, staleHeard, relook, got, offers>>
vars  == <<rec, heard, repeat, line, door, reply, ended, mons>>

\* What a request rendered now carries. The design renders the note from the record on
\* every request (the render hook) and stores nothing; storeNote wrote it once, at
\* landing, where it stays.
Noted(r) == IF Mut("storeNote") THEN r # "none" ELSE r = "owed"

\* The readers (the render, the run's door, the lull) wait for every pending settle
\* (owed.Owed.settled); LATE lets that bounded wait run out with a sync cut's settle
\* still to come (`gone`). lagSettle does not wait at all.
Settled == (fed = "none" /\ (late = "none" \/ gone)) \/ Mut("lagSettle")

Init ==
    /\ st = "none" /\ fed = "none" /\ uncut = 0 /\ missed = FALSE /\ reoffers = 0
    /\ sctx = "none" /\ frag = FALSE /\ late = "none" /\ gone = FALSE
    \* An action's confirmation is nothing the caller lacks: they asked for it.
    /\ heard = (SOURCE = "action") /\ repeat = FALSE
    /\ vad = FALSE /\ pend = FALSE /\ turn = FALSE /\ speech = 0
    /\ q = "none" /\ hPost = FALSE /\ rloc = "none" /\ rk = "none" /\ tres = FALSE
    /\ tend = FALSE /\ mark = FALSE /\ gtool = FALSE /\ rounds = 0
    /\ req = "none" /\ rst = "none" /\ noted = FALSE /\ nreg = FALSE /\ say = "none" /\ ph = "none"
    /\ sdone = TRUE /\ carry = FALSE /\ offers = 0
    /\ ended = FALSE
    /\ runUnder = FALSE /\ runMid = FALSE /\ displaced = FALSE /\ staleNote = FALSE
    /\ noNote = FALSE /\ staleHeard = FALSE /\ relook = FALSE /\ got = "none"

(* ============================ the evidence (§3) ============================ *)

\* What the caller heard of a reply that played to its end, and of one cut while it
\* played. A telling may open with a lead-in that restates the question (#203: "The top
\* story on Hacker News right now is…"; answer-first is the model's choice, 17-20/20,
\* not a guarantee), and the cut can land before, inside or after the items. A reply
\* that tells nothing shares only common words with the outcome ("Sure, take your
\* time."). Cut before its first audio (still generating, or the TTS not yet open, #79),
\* it was heard not at all.
PlayGot(s) == CASE s \in {"tell", "telltool"} -> "all"
                [] s = "part" -> "some"
                [] OTHER      -> "filler"          \* defer, other, a line before a call, lookup's ack
CutGot(s, p) ==
    IF p = "gen" THEN {"nothing"}
    ELSE CASE s \in {"tell", "telltool"} -> {"lead", "some", "all"}
           [] s = "part" -> {"lead", "some"}
           [] OTHER      -> {"filler"}

\* The run that played its telling to the end, uncut: the dedicated telling. Audio
\* must have played -- a run with no audio is not "uncut", it is unheard (the feed takes
\* the reply's OWN audio, as Followup.tla's FIX "ownAudio"/"unspoken" do).
RunUncut(p, cut, g) ==
    RUNCLOSE = "uncut" /\ p \in Runs /\ ~cut /\ g \in {"some", "all"}

\* owed.py's close rule, on one ended reply. Only a reply whose request carried the
\* note can close (§3: "when it carried the note in its request").
Closes(p, n, cut, g) ==
    /\ n
    /\ IF     Mut("fraction")    THEN p \in Runs /\ g \in {"lead", "some", "all"}
       \* singleWord keeps the per-item rule: only the common-word list and the "2" are gone,
       \* so a filler whose common words meet every item closes, one item's terms do not.
       ELSE IF Mut("singleWord") THEN RunUncut(p, cut, g) \/ g \in {"filler", "all"}
       ELSE IF Mut("oneItem")    THEN RunUncut(p, cut, g) \/ g \in {"some", "all"}
       ELSE IF Mut("countSettle") THEN FALSE        \* the count below decides
       \* partialRecord: the record's items are the fragment's, which a telling of part
       \* of the answer carries all of
       ELSE IF frag THEN g \in {"some", "all"}
       \* one term-less item. Before "about": only a run played through uncut. "about":
       \* any noted reply about it, heard to its end -- played through, or cut after its
       \* last word (the ledger's pts-exact prefix is all of it): `g = "all"`
       ELSE IF OUTCOME = "failure"
            THEN IF RUNCLOSE = "about" THEN g = "all"
                 ELSE p \in Runs /\ ~cut /\ g \in {"some", "all"}
       ELSE \* the design: an uncut run, or every item's terms in the heard text
            RunUncut(p, cut, g) \/ g = "all"

\* The reply ends: played through, cut, or with no audio at all. Its evidence goes to the
\* settle feed (`fed`), which owed.py applies in a step of its own (`Settle`): until then
\* the record still says owed. `heard` is the truth, and it is immediate.
\* `g` is what the caller heard; `r` what the feed READS they heard. The design reads the
\* pts-exact prefix: the words whose scheduled playout is before the cut, never more.
\* ledgerMax reads the ledger's `heard_text` as it is (transcript_ledger.py:902): the
\* LONGER of that prefix and an audio-fraction estimate (`_prefix(intended, frac)`,
\* rounded to the nearest word, frac over max(audio, words x _SECONDS_PER_WORD)) --
\* built for the corrector, where reading too little deletes heard words. For the
\* record, too much is the unsafe side: a cut a word short of the second item reads
\* as both.
Reads(cut, g) ==
    IF Mut("ledgerMax") /\ cut
      THEN CASE g = "lead" -> {"lead", "some"}
             [] g = "some" -> {"some", "all"}
             [] OTHER      -> {g}
      ELSE {g}

Finish(cut, g) ==
    \E r \in Reads(cut, g) :
      LET u  == IF Mut("countSettle") /\ nreg /\ ~cut /\ g # "nothing"
                  THEN (IF uncut < 2 THEN uncut + 1 ELSE 2) ELSE uncut
          \* A reply rendered with the answer fresh is evidence for the record made of it
          \* since -- a late settle's (owed.Owed.settle: Reply.fresh against the live
          \* book); lateNoEvidence drops it.
          ev == nreg \/ (carry /\ st = "owed" /\ ~Mut("lateNoEvidence"))
          c  == Closes(req, ev, cut, r) \/ (Mut("countSettle") /\ u = 2)
      IN /\ req' = "none" /\ rst' = "none" /\ noted' = FALSE /\ nreg' = FALSE
         /\ say' = "none" /\ ph' = "none" /\ sdone' = TRUE /\ carry' = FALSE
         /\ heard' = (heard \/ g = "all")
         /\ fed' = IF c /\ st = "owed" THEN "close" ELSE fed
         /\ uncut' = u
         /\ got' = g
         \* Sync: a telling heard in full is history; a telling of the fresh answer cut
         \* while it played leaves its heard prefix in the context (heard_context
         \* truncates the reply to it). Anything else -- a deferral, a line before a
         \* call, a reply cut before its audio (the corrector removes it) -- leaves the
         \* answer as it was.
         /\ sctx' = CASE ~Sync \/ st # "none"                                -> sctx
                       [] g = "all"                                         -> "told"
                       [] cut /\ ph = "play" /\ carry /\ say \in Tells       -> "cut"
                       [] OTHER                                             -> sctx

\* Did a run end without closing the record? Under "anyMiss" that arms the re-offer.
MissedRun(cut, g) ==
    /\ REOFFER = "anyMiss" /\ req \in Runs /\ st = "owed"
    /\ ~Closes(req, nreg, cut, g)

(* ============================ the record (owed.py) ============================ *)

\* gate.ask(key): the run enters FollowupGate by the post-tool answer's branch
\* (followup_gate.py:485-501 -> _hold_answer :632). It waits there while caller speech is
\* pending and then for a turn-free window (`RunGo`). The hold is a small queue: a run
\* and a held post-tool answer do not displace each other. oneSlot is today's single
\* `_held_answer`, a newer frame replacing the older.
Ask(k) ==
    /\ rloc' = "wait" /\ rk' = k
    /\ IF Mut("oneSlot") /\ hPost /\ (vad \/ pend)
         THEN hPost' = FALSE /\ displaced' = TRUE
         ELSE UNCHANGED <<hPost, displaced>>

\* The consult lands (tools._consult_and_land -> Owed.land): the placeholder tool
\* result is rewritten once to the cleaned outcome (the code from agent_session.py:331-345,
\* moved), the record is owed, and the landing run is asked -- unless a caller request
\* is still to be RENDERED (their turn is open, or committed and not yet asked): that
\* request carries the note, and the run would speak over or before it. A caller request
\* already rendered (in flight without the note) does not stop the run: it waits behind.
Land ==
    /\ SOURCE = "consult" /\ ~ended /\ st = "none"
    /\ st' = "owed"
    /\ IF turn \/ q = "caller"
         THEN missed' = TRUE /\ UNCHANGED <<rloc, rk, hPost, displaced>>
         ELSE Ask("run") /\ UNCHANGED missed
    /\ UNCHANGED <<fed, uncut, reoffers, sctx, frag, late, gone, heard, repeat, line, q, tres, tend,
                   mark, gtool, rounds, reply, ended, runUnder, runMid, staleNote, noNote, staleHeard,
                   relook, got, offers>>

\* The settle feed: owed.py hears how the reply ended and closes the record on the
\* evidence. Its own step, so a reader that does not wait for it reads a stale record.
\* The feed's source is the reply's own end -- the interruption (ledger._interrupt, ahead
\* of the commit by CutReplyFence) or its synthesis done and all of its own audio played
\* (ledger._close_ready, at the window close its last chunk played out in) -- NOT a chart
\* of a reply that played through that comes only at the next bot activity.
Settle ==
    /\ fed = "close"
    /\ fed' = "none"
    /\ st' = IF st = "owed" THEN "closed" ELSE st
    /\ UNCHANGED <<uncut, missed, reoffers, sctx, frag, late, gone, heard, repeat, line, door, reply,
                   ended, mons>>

\* The lull (§4 (a)): the caller's turn has been answered and the line has been quiet for
\* LULL_S (~10 s) -- owed.py's lull timer, re-armed by every VAD start, turn and reply.
\* One re-offer per record, and only once the re-offer is armed (`missed`). An
\* environment action, enabled only on a quiet line with no turn open; it is time
\* passing, so it is fair (FairSpec).
Quiet == ~vad /\ ~pend /\ ~turn /\ q = "none" /\ req = "none" /\ ~tres /\ ~gtool
         /\ rloc = "none" /\ ~hPost
Lull ==
    /\ ~ended /\ st = "owed" /\ missed /\ Quiet
    /\ Settled
    /\ reoffers = 0 \/ (Mut("runAfterCut") /\ reoffers < 2)
    /\ reoffers' = reoffers + 1
    /\ offers' = offers + 1
    /\ missed' = FALSE                         \* runAfterCut: re-armed by the next cut
    /\ Ask("reoffer")
    /\ UNCHANGED <<st, fed, uncut, sctx, frag, late, gone, heard, repeat, line, q, tres, tend, mark,
                   gtool, rounds, reply, ended, runUnder, runMid, staleNote, noNote, staleHeard,
                   relook, got>>

(* ============================ the door (FollowupGate) ============================ *)

\* The run's window: no caller speech pending (the #198 hold, at the VAD level) AND
\* turn-free (no turn open, no caller words waiting for their answer, no tool round out:
\* FollowupGate's `_turn` and the claim the gate now takes itself -- TurnCommitMark never
\* sees an upstream run).
Window ==
    /\ Mut("turnLevel") \/ (~vad /\ ~pend)
    /\ Mut("vadLevel")  \/ (~turn /\ q = "none" /\ (~tres \/ (Mut("endClearsTool") /\ tend))
                            /\ ~gtool)

\* The run goes: the gate pushes it up to the LLM and claims the window. At this door,
\* not at gate.ask, owed.py is asked again whether the record is still owed: between the
\* ask and the go a post-tool answer (released from the same hold ahead of it) or a
\* caller turn may have told it. A run for a record no longer owed is dropped here.
\* The render is the hook in _open_stream: the run line FIRST, then the note.
RunGo ==
    /\ ~ended /\ rloc = "wait" /\ req = "none" /\ Window
    /\ Settled
    /\ rloc' = "none" /\ rk' = "none"
    /\ IF st = "owed" \/ Mut("askOnly")
         THEN /\ req' = rk /\ rst' = st /\ noted' = Noted(st) /\ nreg' = Noted(st)
              /\ ph' = "gen" /\ carry' = FALSE
              /\ UNCHANGED <<say, sdone>>
              /\ runUnder'   = (runUnder \/ vad \/ pend)
              /\ runMid'     = (runMid \/ turn \/ q # "none" \/ tres)
              /\ staleNote'  = (staleNote \/ (Noted(st) /\ st = "closed"))
              /\ noNote'     = (noNote \/ (st = "owed" /\ ~Noted(st)))
              /\ staleHeard' = (staleHeard \/ (Noted(st) /\ heard))
         ELSE UNCHANGED <<reply, runUnder, runMid, staleNote, noNote, staleHeard>>
    /\ UNCHANGED <<rec, heard, repeat, line, q, hPost, tres, tend, mark, gtool, rounds, ended, displaced,
                   relook, got, offers>>

\* The hold cap (TEAPORT_REPLY_HOLD_MAX_S) on a held run: DROPPED, never released (room
\* TV noise holds it forever otherwise; §4). A timer: never forced (not in FairSpec).
Cap ==
    /\ ~ended /\ rloc = "wait" /\ (vad \/ pend)
    /\ rloc' = "none" /\ rk' = "none"
    /\ missed' = (missed \/ (REOFFER = "anyMiss" /\ st = "owed"))
    /\ UNCHANGED <<st, fed, uncut, reoffers, sctx, frag, late, gone, heard, repeat, line, q, hPost, tres,
                   tend, mark, gtool, rounds, reply, ended, mons>>

\* The caller's VAD stopped and no words came within TEAPORT_REPLY_HOLD_RELEASE_S (a
\* cough): FollowupGate's _speech_over. The held post-tool answer goes on; a held run
\* goes on waiting for its window. A timer under UserTurn.tla's WORDS_IN_TIME premise:
\* it fires only once the words are known not to be coming (no turn opens instead).
GateQuiet ==
    /\ ~ended /\ pend /\ ~vad /\ ~turn
    /\ pend' = FALSE
    /\ IF hPost THEN hPost' = FALSE /\ q' = "post" ELSE UNCHANGED <<hPost, q>>
    /\ UNCHANGED <<rec, heard, repeat, vad, turn, speech, rloc, rk, tres, tend, mark, gtool, rounds, reply,
                   ended, mons>>

\* A sync tool's result lands and the assistant aggregator pushes the answer UP
\* (_maybe_push_context_after_function_result): held while speech is pending, else asked.
\* (A turn start cancelled the call: TurnStart clears `tres`.)
ToolResult ==
    /\ ~ended /\ tres
    /\ tres' = FALSE /\ tend' = FALSE /\ gtool' = FALSE      \* the result settles its id
    /\ IF vad \/ pend
         THEN /\ hPost' = TRUE /\ UNCHANGED q
              /\ IF Mut("oneSlot") /\ rloc = "wait"
                   THEN rloc' = "none" /\ rk' = "none" /\ displaced' = TRUE
                   ELSE UNCHANGED <<rloc, rk, displaced>>
         ELSE q' = "post" /\ UNCHANGED <<hPost, rloc, rk, displaced>>
    \* Sync: the answer is in the context now, nothing of it told. A chain's second
    \* result leaves it as it is: fresh still when the first went untold, and when the
    \* first was told (or cut) the second's answer is another record (not modelled).
    /\ sctx' = IF Sync /\ sctx = "none" THEN "fresh" ELSE sctx
    /\ UNCHANGED <<st, fed, uncut, missed, reoffers, frag, late, gone, heard, repeat, line, mark, rounds,
                   reply, ended, runUnder, runMid, staleNote, noNote, staleHeard, relook, got, offers>>

\* The call's in-progress frame reaches the gate. It is uninterruptible, so it may come
\* after the barge-in that cancelled the call (TurnStart) or after its result. The
\* design keeps the gate's call ids: an id already settled or cancelled is not added
\* (`gtool` follows `tres`). staleFlag sets its one flag whatever happened meanwhile.
Mark ==
    /\ ~ended /\ mark
    /\ mark' = FALSE
    /\ gtool' = IF Mut("staleFlag") THEN TRUE ELSE (gtool \/ tres)
    /\ UNCHANGED <<rec, heard, repeat, line, q, hPost, rloc, rk, tres, tend, rounds, reply, ended,
                   mons>>

(* ============================ the LLM and the reply ============================ *)

\* The LLM takes the caller's commit or the post-tool answer and the render hook runs
\* (BoundedOpenAILLMService._open_stream, after HeardContextCorrector's reconcile): the
\* note from the record as it is now. The previous reply's settle has been applied
\* first: its end (the interruption, or BotStoppedSpeaking) reaches owed.py before the
\* next commit can be rendered -- CutReplyFence holds a commit for its cut. `lagSettle`
\* drops that order.
\*
\* `sp`: the request is the speculative reply's, opened through _open_stream directly
\* (speculate.py) and adopted at the commit. Its note was rendered in the speculation's
\* own task, so the keys it carried ride on the stream (_RenumberedStream.owed_noted,
\* read through speculate._AdoptedStream) and the adopting commit registers them: the
\* design takes the carried keys to survive the adoption (`nreg` = `noted`).
\* renderInGet renders no note into it; adoptDropsKeys registers none.
Issue ==
    /\ ~ended /\ q # "none" /\ req = "none"
    /\ Settled
    /\ \E sp \in (IF q = "caller" THEN BOOLEAN ELSE {FALSE}) :
         LET n == ~(Mut("renderInGet") /\ sp) /\ Noted(st) IN
         \* Sync with no record: the model reads the context as it is (`sctx`): a told
         \* answer is history ("closed"), a fresh result is answered from ("fresh"), a
         \* cut one is an answer not given ("owed", no note: the B0 shape) -- except an
         \* action's: what it confirms is done, and nothing is looked up again.
         /\ rst' = IF Sync /\ st = "none"
                      THEN CASE sctx = "told" -> "closed"
                             [] sctx = "cut"  -> (IF SOURCE = "action" THEN "closed" ELSE "owed")
                             [] sctx = "fresh" -> "fresh" [] OTHER -> "none"
                      ELSE st
         /\ req' = q /\ noted' = n /\ ph' = "gen" /\ q' = "none"
         \* the book's own state, not the context's: fresh until told or recorded -- a
         \* telling cut mid-stream (STREAMCUT) or one whose settle is late leaves it so
         /\ carry' = (Sync /\ st = "none" /\ sctx \in {"fresh", "cut"})
         /\ nreg' = (n /\ ~(Mut("adoptDropsKeys") /\ sp))
         /\ staleNote'  = (staleNote \/ (n /\ st = "closed"))
         /\ noNote'     = (noNote \/ (st = "owed" /\ ~n))
         /\ staleHeard' = (staleHeard \/ (n /\ heard))
    \* staleFlag: any completion's start clears the one flag (LLMFullResponseStartFrame).
    /\ gtool' = IF Mut("staleFlag") THEN FALSE ELSE gtool
    /\ UNCHANGED <<say, sdone, rec, heard, repeat, line, hPost, rloc, rk, tres, tend, mark, rounds,
                   ended, runUnder, runMid, displaced, relook, got, offers>>

\* What the model may say, by who asked and what the request carried (header; DESIGN.md
\* "What the evidence supports"). "tool" is a sync tool call (get_current_time, ...),
\* only on a caller's turn, and "texttool" the same call with a line before it ("Let me
\* check." + get_weather): the line plays while the call is out; "telltool" tells the
\* answer and then calls (Sync, a chain); "lookup" is ask_openclaw again for what the
\* outcome answers.
Says(p, r, n) ==
    LET pt == Opt(OUTCOME = "items", "part")       \* a failure is told whole or not at all
        t  == IF p = "caller" THEN {"tool", "texttool"} ELSE {}
    IN
    IF p = "run"     THEN {"tell"} \cup (Opt(PARTIAL, "part") \cap pt) \cup Opt(SILENT, "silent")
    ELSE IF p = "reoffer"
                     THEN {"tell", "stay"} \cup (Opt(PARTIAL, "part") \cap pt)
                          \cup Opt(SILENT, "silent")
    \* Sync, the answer fresh in the context: a reply answers from it. The post-tool reply
    \* is the answer's telling by its position (a clarifying question in its place is what
    \* the caller is owed instead, and recorded as such); a caller's turn that dropped the
    \* post-tool request tells it, defers to "hold on", or says something else. Any reply
    \* may call again: bare, after a line, or after telling it (the chain).
    ELSE IF Sync /\ r = "fresh"
                     THEN (IF p = "post" THEN {"tell"} ELSE {"tell", "defer", "other"}) \cup Calls
    ELSE IF n            THEN {"tell", "defer", "other"} \cup pt \cup t \cup Opt(FLAKY, "lookup")
    ELSE IF r = "none"   THEN {"other"} \cup t
    ELSE IF r = "closed" THEN {"tell", "other"} \cup t
    ELSE {"tell", "defer", "other", "lookup"} \cup pt \cup t

\* The completion's first output. stay_silent (offered only to the re-offer run; the run
\* loop consumes the call and its result), a reply with no audio, and a bare tool call
\* end here, uncut and unheard. Everything else starts playing.
Say ==
    /\ ~ended /\ req # "none" /\ ph = "gen"
    /\ \E s \in Says(req, rst, noted) :
         \* Sync: a chain of at most MaxRounds tool rounds (the state space's bound)
         /\ (Sync /\ s \in Calls) => rounds < MaxRounds
         /\ rounds' = IF Sync /\ s \in Calls THEN rounds + 1 ELSE rounds
         /\ repeat' = (repeat \/ (req \in Runs /\ s \in {"tell", "part"} /\ heard))
         /\ relook' = (relook \/ (s = "lookup" /\ rst # "none"))
         /\ IF s \in {"silent", "stay", "tool"}
              THEN /\ Finish(FALSE, "nothing")
                   /\ tres' = (s = "tool") /\ mark' = (s = "tool")
                   /\ missed' = (missed \/ MissedRun(FALSE, "nothing"))
              ELSE /\ say' = s /\ ph' = "play"
                   /\ tres' = (s \in {"texttool", "telltool"})
                   /\ mark' = (s \in {"texttool", "telltool"})
                   \* STREAMCUT: a telling of the fresh answer may still be streaming as
                   \* its first audio plays (StreamEnd ends it)
                   /\ sdone' \in IF STREAMCUT /\ carry /\ s \in Tells
                                   THEN BOOLEAN ELSE {TRUE}
                   /\ UNCHANGED <<req, rst, noted, nreg, heard, fed, uncut, missed, got, sctx, carry>>
    /\ UNCHANGED <<st, frag, late, gone, reoffers, line, q, hPost, rloc, rk, tend, gtool, ended,
                   runUnder, runMid, displaced, staleNote, noNote, staleHeard, offers>>

\* The reply's stream ends while its audio plays (STREAMCUT).
StreamEnd ==
    /\ ~ended /\ ph = "play" /\ ~sdone
    /\ sdone' = TRUE
    /\ UNCHANGED <<rec, heard, repeat, line, door, req, rst, noted, nreg, say, ph, carry, ended, mons>>

\* BotStoppedSpeaking: the reply played to its end. A "texttool" reply's End passes the
\* gate here with its call still out (tend).
Played ==
    /\ ~ended /\ ph = "play" /\ sdone
    /\ Finish(FALSE, PlayGot(say))
    /\ missed' = (missed \/ MissedRun(FALSE, PlayGot(say)))
    /\ tend' = (tres /\ say \in {"texttool", "telltool"})
    /\ UNCHANGED <<st, frag, late, gone, reoffers, repeat, line, q, hPost, rloc, rk, tres, mark, gtool,
                   rounds, ended, runUnder, runMid, displaced, staleNote, noNote, staleHeard, relook,
                   offers>>

(* ============================ the caller ============================ *)

\* The caller starts speaking: the gate's hold begins (pend), whatever is waiting stays.
VadStart ==
    /\ ~ended /\ ~vad /\ speech < MaxSpeech
    /\ vad' = TRUE /\ pend' = TRUE /\ speech' = speech + 1
    /\ UNCHANGED <<rec, heard, repeat, turn, door, reply, ended, mons>>

VadStop ==
    /\ ~ended /\ vad
    /\ vad' = FALSE
    /\ UNCHANGED <<rec, heard, repeat, pend, turn, speech, door, reply, ended, mons>>

\* Their words start a turn: the InterruptionFrame. The reply in flight is cut (what the
\* caller heard of it is charted at the cut), the gate drops what it holds -- a held
\* post-tool answer (the turn's commit reads the result) and the run (an attempt: the
\* turn carries the note, §4) -- the reset drops anything queued at the LLM, and a sync
\* tool call is cancelled. A caller turn after the answer landed arms the re-offer.
TurnStart ==
    /\ ~ended /\ pend /\ ~turn
    /\ turn' = TRUE /\ pend' = FALSE
    /\ q' = "none" /\ hPost' = FALSE /\ rloc' = "none" /\ rk' = "none" /\ tres' = FALSE
    /\ tend' = FALSE /\ gtool' = FALSE  \* the cancel; a queued in-progress frame stays (mark)
    /\ IF req # "none"
         THEN \E g \in CutGot(say, ph) :
                /\ Finish(TRUE, g)
                \* Sync (#204): a telling of the fresh answer, cut before every item was
                \* heard, makes the record AT ITS SETTLE (Owed.settle -> Owed._cut_answer;
                \* LateSettle) -- whoever asked for the reply: the post-tool request, or the
                \* caller's turn that dropped it and read the fresh result (postOnly). Only
                \* a reply whose stream ended and whose audio played: its text is the whole
                \* answer and the caller heard a prefix of it (partialRecord). A telling
                \* that called again too (callNoRecord). An answer tool's, never an
                \* action's (actionOwed). No run: this turn carries the note, and the cut
                \* arms the one re-offer. A cut after its last item (g = "all") makes none;
                \* noCutRecord makes none ever. One settle pending at a time here: a second
                \* reply's, cut while the first is still late, settles at once.
                /\ LET made == /\ Sync /\ st = "none" /\ ph = "play" /\ say \in Tells
                               /\ g # "all" /\ ~Mut("noCutRecord")
                               /\ IF Mut("postOnly") THEN req = "post" /\ carry ELSE carry
                               /\ sdone \/ Mut("partialRecord")
                               /\ say # "telltool" \/ ~Mut("callNoRecord")
                               /\ SOURCE = "sync" \/ Mut("actionOwed")
                       kind == IF sdone THEN "rec" ELSE "frag" IN
                   IF made /\ late = "none"
                     THEN /\ late' = kind /\ gone' = FALSE
                          /\ UNCHANGED <<st, frag>> /\ missed' = (missed \/ st = "owed")
                     ELSE /\ st' = IF made THEN "owed" ELSE st
                          /\ frag' = (frag \/ (made /\ kind = "frag"))
                          /\ missed' = (missed \/ st = "owed" \/ made)
                          /\ UNCHANGED <<late, gone>>
         ELSE /\ UNCHANGED <<reply, heard, fed, uncut, got, st, sctx, frag, late, gone>>
              /\ missed' = (missed \/ st = "owed")
    /\ UNCHANGED <<reoffers, repeat, vad, speech, mark, rounds, ended, runUnder, runMid,
                   displaced, staleNote, noNote, staleHeard, relook, offers>>

\* The cut telling's settle (owed.Owed.settle -> _cut_answer): it re-reads the LIVE book
\* before it makes the record. Late (LATE: after the readers' wait ran out, GiveUp), a
\* reply rendered meanwhile with the same fresh answer may have told it in full (the
\* answer is told: no record) or been cut and made the record itself (this one is only
\* evidence, its heard prefix short of the answer: nothing). lateReplace makes its record
\* whatever the book holds -- after a full telling, and over the other's, its re-offer
\* counter reset.
LateSettle ==
    /\ late # "none"
    /\ late' = "none" /\ gone' = FALSE
    /\ IF Mut("lateReplace") \/ (st = "none" /\ sctx # "told")
         THEN /\ st' = "owed" /\ frag' = (late = "frag") /\ missed' = TRUE
              /\ reoffers' = IF Mut("lateReplace") THEN 0 ELSE reoffers
         ELSE UNCHANGED <<st, frag, missed, reoffers>>
    /\ UNCHANGED <<fed, uncut, sctx, heard, repeat, line, door, reply, ended, mons>>

\* SETTLE_WAIT_S passes with that settle still to come: the readers go on (LATE; a timer,
\* never forced).
GiveUp ==
    /\ LATE /\ late # "none" /\ ~gone
    /\ gone' = TRUE
    /\ UNCHANGED <<st, fed, uncut, missed, reoffers, sctx, frag, late, heard, repeat, line, door,
                   reply, ended, mons>>

\* The turn ends: its words are committed and the LLM asked (TurnCommitMark claims the
\* window). The commit takes the words of speech that has stopped (UserTurn's
\* PendCommitted). An empty final (a 0-char decode, noise) commits nothing and asks
\* nothing (push_aggregation returns ""), as in Followup.tla's UserStop.
Commit ==
    /\ ~ended /\ turn /\ ~vad
    /\ turn' = FALSE /\ pend' = FALSE
    /\ \E words \in BOOLEAN : q' = IF words THEN "caller" ELSE "none"
    /\ UNCHANGED <<rec, heard, repeat, vad, speech, hPost, rloc, rk, tres, tend, mark, gtool, rounds, reply,
                   ended, mons>>

\* The client disconnects or the call hangs up. Nothing is owed to nobody: the escape in
\* the liveness property, never forced.
SessionEnd ==
    /\ ~ended
    /\ ended' = TRUE
    /\ UNCHANGED <<rec, heard, repeat, line, door, reply, mons>>

Next ==
    \/ Land \/ Settle \/ LateSettle \/ GiveUp \/ Lull
    \/ RunGo \/ Cap \/ GateQuiet \/ ToolResult \/ Mark
    \/ Issue \/ Say \/ StreamEnd \/ Played
    \/ VadStart \/ VadStop \/ TurnStart \/ Commit \/ SessionEnd

Spec == Init /\ [][Next]_vars

\* For the liveness row. Every step the brain or pipecat takes on its own is weakly fair:
\* the LLM answers what it is asked, audio plays out, the settle feed runs, the gate
\* lets a waiting run go once its window is open, a release timer fires. So does the
\* caller's VAD stop and commit (nobody talks forever: MaxSpeech bounds them anyway).
\* `Lull` is time passing on a quiet line. NOT fair: the consult landing, the caller
\* starting to speak or to take a turn, the hold cap, the session ending.
FairSpec ==
    /\ Spec
    /\ WF_vars(Settle) /\ WF_vars(LateSettle) /\ WF_vars(Lull) /\ WF_vars(RunGo) /\ WF_vars(GateQuiet)
    /\ WF_vars(ToolResult) /\ WF_vars(Issue) /\ WF_vars(Say) /\ WF_vars(StreamEnd)
    /\ WF_vars(Played)
    /\ WF_vars(VadStop) /\ WF_vars(Commit) /\ WF_vars(Mark)

(* ============================ properties ============================ *)

\* The record is closed only once the caller really has the information.
ClosedOnEvidence == st = "closed" => heard

\* Every request rendered while the record is owed carries the note (B0: without it,
\* 19/20 re-lookups).
NoteWhileOwed == ~noNote

\* No request carries a note for a record that is closed. A type invariant over what the
\* render writes would hold by construction: the storeNote row is what carries it.
NoStoredNote == ~staleNote

\* No request tells the model "They have NOT heard this yet" when they have.
NoStaleNote == ~staleHeard

\* The brain never asks for an unprompted run once the caller has the information --
\* ghost-based, so a run decided on a record the settle feed has not caught up with is a
\* counterexample, whatever the record says.
NoRunAfterHeard == req \in Runs => ~heard

\* No unprompted telling begins after the caller has heard it (08-26 08:17).
NoRepeatRecital == ~repeat

\* A run never goes to the LLM while caller speech is pending (#198's hold, #197).
NoRunUnderSpeech == ~runUnder

\* A run never goes inside a caller's turn or a tool round.
NoRunMidTurn == ~runMid

\* Neither a run nor a held post-tool answer is displaced by the other in the hold.
HoldNoLoss == ~displaced

\* At most one re-offer per record (§4 (a)).
OneReoffer == offers <= 1

\* The model never re-looks-up what the outcome answers (the #203 49-second redo).
NoRedundantLookup == ~relook

\* Liveness: an owed answer is heard, or the session ends, or the brain has spent its one
\* re-offer (a terminal for UNPROMPTED speech only: the note goes on rendering, so the
\* caller who asks gets it).
OwedResolves == (st = "owed") ~> (st = "closed" \/ ended \/ reoffers > 0)

TypeOK ==
    /\ st \in {"none", "owed", "closed"} /\ fed \in {"none", "close"}
    /\ uncut \in 0..2 /\ missed \in BOOLEAN /\ reoffers \in 0..2
    /\ heard \in BOOLEAN /\ repeat \in BOOLEAN
    /\ vad \in BOOLEAN /\ pend \in BOOLEAN /\ turn \in BOOLEAN /\ speech \in 0..MaxSpeech
    /\ q \in {"none", "caller", "post"} /\ hPost \in BOOLEAN
    /\ rloc \in {"none", "wait"} /\ rk \in {"none", "run", "reoffer"} /\ tres \in BOOLEAN
    /\ tend \in BOOLEAN /\ (tend => tres)
    /\ mark \in BOOLEAN /\ gtool \in BOOLEAN /\ (Mut("staleFlag") \/ (gtool => tres))
    /\ (rloc = "wait") = (rk # "none")
    /\ req \in {"none", "caller", "post", "run", "reoffer"}
    /\ rst \in {"none", "owed", "closed", "fresh"} /\ noted \in BOOLEAN
    /\ say \in {"none", "tell", "part", "defer", "other", "lookup", "texttool", "telltool"}
    /\ ph \in {"none", "gen", "play"} /\ ((req = "none") = (ph = "none"))
    /\ (ph = "play") = (say # "none")
    \* the model's one-slot queue at the LLM is enough: a held post-tool answer and a
    \* queued request never coexist (a turn start drops the one, a commit needs a turn)
    /\ ~(hPost /\ q # "none")
    /\ ended \in BOOLEAN /\ got \in {"none", "nothing", "lead", "filler", "some", "all"}
    /\ MUT \subseteq {"fraction", "countSettle", "singleWord", "oneItem", "storeNote",
                      "runAfterCut", "lagSettle", "oneSlot", "askOnly", "turnLevel",
                      "vadLevel", "renderInGet", "ledgerMax", "endClearsTool",
                      "staleFlag", "adoptDropsKeys", "noCutRecord", "partialRecord",
                      "actionOwed", "postOnly", "callNoRecord", "lateReplace",
                      "lateNoEvidence"}
    /\ REOFFER \in {"cutOrTurn", "anyMiss"} /\ RUNCLOSE \in {"uncut", "terms", "about"}
    /\ OUTCOME \in {"items", "failure"} /\ SOURCE \in {"consult", "sync", "action"}
    /\ STREAMCUT \in BOOLEAN
    /\ sctx \in {"none", "fresh", "told", "cut"} /\ nreg \in BOOLEAN /\ (nreg => noted)
    /\ frag \in BOOLEAN /\ (frag => Mut("partialRecord"))
    /\ rounds \in 0..MaxRounds /\ sdone \in BOOLEAN /\ (~sdone => ph = "play")
    /\ late \in {"none", "rec", "frag"} /\ gone \in BOOLEAN /\ (gone => late # "none")
    /\ carry \in BOOLEAN /\ offers \in 0..3 /\ LATE \in BOOLEAN
=============================================================================
