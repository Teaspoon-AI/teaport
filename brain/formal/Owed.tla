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
(* A CUT is judged (owed.Owed._judge, #204): a reply played to the end    *)
(* closes by the lexical rule on its whole text (coverage); a cut one only *)
(* if its whole text covers the record and an LLM judge, shown the heard   *)
(* prefix and the unheard rest, answers HEARD (Verdicts below).            *)
(*   "judgeWrong"    -- the judge answers HEARD with part of the answer    *)
(*                      unheard (the measured residual, 1/145).            *)
(*   "prefixLexical" -- the rule before: the lexical rule on the heard     *)
(*                      prefix (89/145 false closes on live cuts).         *)
(* OWES: the judge answers OWED, or not in time, though all was heard     *)
(*   (14/96, at the trailing edge): errs owed, and the lull may re-offer.  *)
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
(* CLOSE: how a reply PLAYED TO ITS END closes an outcome with items (#210). *)
(*   "lexical" -- the coverage rule as shipped in #204: min(2, |terms|) of  *)
(*                each item's terms in its whole text. Exact on the corpus, *)
(*                but LEXMISS: a full telling can read as not covering --   *)
(*                a one-fact answer told in the caller's words is one term  *)
(*                of seven, the request having the rest (2026-10-23 14:06,  *)
(*                re-told five times).                                      *)
(*   "gist"    -- an LLM judge, shown the caller's words, the result and    *)
(*                the whole reply, asked whether it gave them the answer to *)
(*                what they asked (owed.gist_judge_messages), on a reply    *)
(*                ABOUT the record (a word shared with its request or       *)
(*                result: owed.Record.about). No verdict (a timeout, an     *)
(*                error) CLOSES: the user's choice, 2026-10-23 -- a repeat  *)
(*                is worse than a detail lost, and the result stays in the  *)
(*                context either way. The BACKSTOP: the second noted reply  *)
(*                about the record played to its end closes it whatever     *)
(*                the verdict, so a heard answer is re-told at most once.   *)
(*                A cut keeps the cut judge; its gate (the whole text gives *)
(*                the answer) becomes this judge, its misses folding into   *)
(*                OWES and judgeWrong. Both judges read what the caller     *)
(*                HEARD OF THE RECORD so far -- every noted reply about it, *)
(*                played through or the cut's prefix (Record.heard) -- not  *)
(*                the one reply: a re-telling cut because they heard it     *)
(*                already is judged on the telling they heard before it.    *)
(* What the caller got of a reply about the record that gave no answer      *)
(* ("Want me to tell you the year?", "the summary is as provided") is       *)
(* "mention" (MENTION). The gist premises, measured on 58 judged live and   *)
(* written cases (tests/test_gist_judge_eval.py, 2026-10-23):               *)
(*   PTOWES    -- the judge answers OWED on a full telling (2-4/29 by       *)
(*                majority of 10).                                          *)
(*   NOVERDICT -- the judge gives no verdict in time (p90 1.15 s one at a   *)
(*                time), which closes even a mention.                       *)
(*   TWICE     -- the second about-reply after one that gave no answer      *)
(*                again gives none (a part, a mention), and the backstop    *)
(*                closes it unheard. The note was in view both times (X1:   *)
(*                18/20 answer).                                            *)
(*   "gistWrong" (MUT) -- the judge answers HEARD on a part or a mention    *)
(*                (0/25 by majority).                                       *)
(*   "noBackstop" (MUT) -- no backstop: a judge that keeps answering OWED   *)
(*                re-tells a heard answer on every turn.                    *)
(*   "oneReply" (MUT) -- the judges read the one reply, not what was heard  *)
(*                of the record: a re-telling cut before its facts is OWED  *)
(*                though the telling before it was heard.                   *)
(*                                                                          *)
(* DECLINED (#231): a record can end without being heard because the       *)
(* caller does not want it. When a caller's turn cuts a telling of an owed  *)
(* record, their words are read before that turn's request renders: the    *)
(* decline check (owed.decline_judge_messages), shown their question, what *)
(* they heard, the rest and what they said, answers DECLINED -- they have  *)
(* what they need, dismissed it, objected to it, or corrected the request  *)
(* so this answer no longer fits ("No, I mean the one on Elm Street") -- or *)
(* WANTED. DECLINED ends the record "declined": no note, no run, no        *)
(* re-offer; its tool result stays. Measured on 286 synthetic cuts          *)
(* (gpt-oss-120b, one call each, 2026-10-30): 10/770 false DECLINED,        *)
(* 35/660 false WANTED, p50 0.35 s. The render WAITS for it (the user's     *)
(* choice, 2026-10-27: the reply to "that's all I needed" must not carry    *)
(* the note).                                                               *)
(*   DECMISS        -- the check answers WANTED, or not in time, though     *)
(*                     they declined (the 35/660): errs owed, as before.    *)
(*   "declineWrong" (MUT) -- it answers DECLINED though they wanted the     *)
(*                     rest (the 10/770): the record ends unheard.          *)
(*   "noDecline"    (MUT) -- the words are not read (before #231): after    *)
(*                     "that's all I needed" every request carries the note.*)
(*   "declineAfter" (MUT) -- the check does not hold the render: the reply  *)
(*                     to the dismissal itself carries the note.            *)
(* ORIGIN: how the record is made. "consult" -- an async consult lands     *)
(* (Land). "cut" -- a sync answer tool's result (web_search) told by the    *)
(* reply after it and cut before it was heard: the cut MAKES the record     *)
(* (OwedAnswers.tla End; owed._cut_answer), the issue's own two cases, so   *)
(* that cut is the record's first cut telling and its words are read too.  *)
(*   "cutRecordUnread" (MUT) -- the record made at a cut has nothing due    *)
(*                     (the first fix's gap, found in review).              *)
(* The words read are the first the caller commits after the cut: those of *)
(* the turn that cut it, or, if it committed none (an empty final), the     *)
(* next turn's. The model lets those carry what the lost words meant (`dw`):*)
(* a later turn that reads WANTED where the lost words declined is         *)
(* DECMISS-shaped (ow_declineMiss_rest), one that reads DECLINED where they *)
(* wanted it declineWrong-shaped; neither adds a behaviour the rows lack.  *)
(* A dismissal in a turn that cut nothing is the note's FOOTER's ("If       *)
(* they're busy with something else..."), the model's, and not modelled.   *)
(*                                                                          *)
(* CUT TELLINGS COUNT (#231): a noted telling of the record cut after its   *)
(* audio began counts; the second such cut ends the record "dropped" (not   *)
(* heard, its tool result kept), so a telling that keeps being cut does not *)
(* come back, as the backstop bounds the ones played to their end.          *)
(*   "cutsUncounted" (MUT) -- before #231: a cut telling counts toward      *)
(*                     nothing, and a record told and cut on every turn     *)
(*                     stays owed (10-25: five tellings in five minutes).   *)
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
          FLAKY,       \* a request carrying the note may still re-look-up
          PARTIAL,     \* a telling run may carry only some of the items
          SILENT,      \* a run's completion may produce no audio at all
          OWES,        \* the cut judge may answer OWED (or not in time) though all was heard
          CLOSE,       \* "lexical" | "gist": how a reply played to its end closes (#210)
          LEXMISS,     \* "lexical": a full telling may read as not covering the record
          PTOWES,      \* "gist": the judge may answer OWED on a full telling
          NOVERDICT,   \* "gist": the judge may give no verdict in time (closes)
          MENTION,     \* a noted reply may be about the record and give no answer
          TWICE,       \* the about-reply after one that gave no answer may give none again
          DECMISS,     \* the decline check may answer WANTED (or not in time) though they declined
          ORIGIN       \* "consult" | "cut": how the record is made (see the header)

Mut(m) == m \in MUT
Runs   == {"run", "reoffer"}        \* the brain's own, unprompted requests
Opt(b, x) == IF b THEN {x} ELSE {}

VARIABLES
    \* --- the record (owed.py) ---
    st,           \* "none" (the consult is still running) | "owed" | "closed" (heard)
                  \*   | "declined" (they don't want it, #231) | "dropped" (cut twice, #231)
    fed,          \* the settle feed: "close" = a reply that closes the record has
                  \*   ended and owed.py has not been told yet; "drop" = the second cut
                  \*   telling has; "none" = settled
    cuts,         \* noted tellings of the record cut after their audio began, up to 2
    dw,           \* what the words the caller cuts a telling of the record with mean (a
                  \*   ghost until they are committed): "none" | "want" | "decline"
    due,          \* the brain marked a decline check due at that cut (owed.Record.decline)
    dck,          \* ...and the words are committed: every reader of the record waits for it
    uncut,        \* countSettle only: noted replies played through, up to 2
    missed,       \* the re-offer is armed (REOFFER says by what)
    reoffers,     \* re-offer runs asked (a held-then-dropped one counts)
    told,         \* "gist": noted replies about the record played to their end, up to 2
    part1,        \* ...and the first of them gave no answer (a part, a mention)
    hacc,         \* "gist": what the caller heard of the record so far holds the answer
    \* --- the ghosts ---
    heard,        \* the caller really has every item (some reply's heard text did)
    decl,         \* the caller declined the rest, in the words that cut its telling, and
                  \*   the brain has them (committed)
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
    \* --- the request in flight and its reply (one at a time) ---
    req,          \* "none" | "caller" | "post" | "run" | "reoffer"
    rst,          \* the record's state when the request was rendered
    noted,        \* the request carried the note
    nreg,         \* ...and the reply is registered as carrying it: its evidence counts
    say,          \* what the model chose: tell | part | defer | other | lookup
    ph,           \* "none" | "gen" (asked, no output yet) | "play" (audio playing)
    ended,        \* the session ended (the client left, the call hung up)
    \* --- monitors ---
    runUnder,     \* a run went to the LLM while caller speech was pending
    runMid,       \* a run went inside a caller's turn or a tool round
    displaced,    \* the hold dropped a run or a post-tool answer for the other
    staleNote,    \* a request carried the note for a record already closed
    noNote,       \* a request rendered while owed went without the note
    staleHeard,   \* a request carried "NOT heard" to a caller who had heard it
    relook,       \* the model looked the answer up again although it had it
    retell,       \* noted tellings begun after the caller had heard it, up to 2
    tellings,     \* noted tellings begun while the record was owed, up to 4
    staleDecl,    \* a request carried the note after the caller had declined it
    got           \* what the caller heard of the last reply that ended (for traces)

rec   == <<st, fed, uncut, missed, reoffers, told, part1, hacc, cuts, dw, due, dck>>
line  == <<vad, pend, turn, speech>>
door  == <<q, hPost, rloc, rk, tres, tend, mark, gtool>>
reply == <<req, rst, noted, nreg, say, ph>>
mons  == <<runUnder, runMid, displaced, staleNote, noNote, staleHeard, relook, retell, got,
           tellings, staleDecl>>
vars  == <<rec, heard, decl, repeat, line, door, reply, ended, mons>>

\* What a request rendered now carries. The design renders the note from the record on
\* every request (the render hook) and stores nothing; storeNote wrote it once, at
\* landing, where it stays.
Noted(r) == IF Mut("storeNote") THEN r # "none" ELSE r = "owed"

Init ==
    /\ st = "none" /\ fed = "none" /\ uncut = 0 /\ missed = FALSE /\ reoffers = 0
    /\ told = 0 /\ part1 = FALSE /\ hacc = FALSE /\ cuts = 0 /\ dw = "none" /\ due = FALSE /\ dck = FALSE
    /\ heard = FALSE /\ decl = FALSE /\ repeat = FALSE
    /\ vad = FALSE /\ pend = FALSE /\ turn = FALSE /\ speech = 0
    /\ q = "none" /\ hPost = FALSE /\ rloc = "none" /\ rk = "none" /\ tres = FALSE
    /\ tend = FALSE /\ mark = FALSE /\ gtool = FALSE
    /\ req = "none" /\ rst = "none" /\ noted = FALSE /\ nreg = FALSE /\ say = "none" /\ ph = "none"
    /\ ended = FALSE
    /\ runUnder = FALSE /\ runMid = FALSE /\ displaced = FALSE /\ staleNote = FALSE
    /\ noNote = FALSE /\ staleHeard = FALSE /\ relook = FALSE /\ retell = 0 /\ got = "none"
    /\ tellings = 0 /\ staleDecl = FALSE

(* ============================ the evidence (§3) ============================ *)

\* What the caller heard of a reply that played to its end, and of one cut while it
\* played. A telling may open with a lead-in that restates the question (#203: "The top
\* story on Hacker News right now is…"; answer-first is the model's choice, 17-20/20,
\* not a guarantee), and the cut can land before, inside or after the items. A reply
\* that tells nothing shares only common words with the outcome ("Sure, take your
\* time."). Cut before its first audio (still generating, or the TTS not yet open, #79),
\* it was heard not at all.
PlayGot(s) == CASE s = "tell" -> {"all"}
                [] s = "part" -> {"some"}
                [] s \in {"defer", "other"}
                       -> {"filler"} \cup Opt(MENTION /\ OUTCOME = "items", "mention")
                [] OTHER      -> {"filler"}        \* lookup's ack, a line before a call
\* "words": a cut after every item's content words but before its facts were all out --
\* "a high around seventy-five" before the low, "the developer kit at two hundred" before
\* "forty-nine" (2026-10-19 23:56). Two content words of each item, not what answers it.
CutGot(s, p) ==
    IF p = "gen" THEN {"nothing"}
    ELSE CASE s = "tell" -> {"lead", "some", "words", "all"}
           [] s = "part" -> {"lead", "some"}
           [] OTHER      -> {"filler"}

\* The run that played its telling to the end, uncut: the dedicated telling. Audio
\* must have played -- a run with no audio is not "uncut", it is unheard (the feed takes
\* the reply's OWN audio, as Followup.tla's FIX "ownAudio"/"unspoken" do).
RunUncut(p, cut, g) ==
    RUNCLOSE = "uncut" /\ p \in Runs /\ ~cut /\ g \in {"some", "all"}

\* A CUT is decided by the judge (owed.Owed._judge: an LLM asked whether the unheard rest
\* holds anything the caller still needs), called only when the reply's whole text covers
\* the record by the lexical rule (a telling of every item, `s = "tell"`): HEARD, OWED, or
\* none (a timeout, an error -- errs owed). The premise, measured on 241 live cuts: HEARD
\* means all was heard (1/145 false HEARD by majority; judgeWrong is that residual). OWES
\* lets it answer OWED, or not in time, when all was heard (14/96 false OWED, at the
\* trailing edge). prefixLexical is the rule before: the heard PREFIX by the lexical rule,
\* which closes on "words" (89/145 false closes).
Verdicts(s, r) ==
    IF s # "tell" THEN {"skip"}                                  \* coverage fails: no call
    ELSE (IF r = "all" THEN {"heard"} \cup (IF OWES THEN {"owed", "none"} ELSE {})
          ELSE {"owed", "none"})
         \cup (IF Mut("judgeWrong") THEN {"heard"} ELSE {})

\* A reply ABOUT the record (owed.Record.about): one the gist judge is asked about.
About(g) == g \in {"all", "some", "mention"}

\* The verdict on a reply played to its end (CLOSE). "lexical": the coverage rule, which
\* LEXMISS lets read a full telling as not covering (the 10-23 reply). "gist": the judge, on
\* a reply about the record only ("skip": an unrelated reply is not judged and closes
\* nothing); HEARD exactly on the answer, but for the measured residuals.
\* The gist judge reads what was heard of the record so far with this reply (`hacc`).
PtVerdicts(g) ==
    IF CLOSE = "lexical"
      THEN IF g = "all" THEN {"heard"} \cup Opt(LEXMISS, "owed") ELSE {"owed"}
    ELSE IF ~About(g) THEN {"skip"}
    ELSE (IF g = "all" \/ (hacc /\ ~Mut("oneReply")) THEN {"heard"} \cup Opt(PTOWES, "owed")
          ELSE {"owed"} \cup Opt(Mut("gistWrong"), "heard"))
         \cup Opt(NOVERDICT, "none")

\* A cut, "gist": once what was heard of the record holds the answer, the gate passes and
\* the judge reads all of it.
CutVerdicts(s, r) ==
    IF CLOSE = "gist" /\ hacc /\ ~Mut("oneReply") THEN Verdicts("tell", "all") ELSE Verdicts(s, r)

\* The backstop: a noted reply about the record, played to its end, closes it when one
\* already did not (owed.Owed.settle, Record.told).
Backstop(cut, g) == CLOSE = "gist" /\ ~Mut("noBackstop") /\ ~cut /\ About(g) /\ told >= 1

\* The gist premise ~TWICE: once a noted reply about the record played to its end with no
\* answer, the next one gives it (the model with the note in view, X1 18/20).
Restrained == ~TWICE /\ CLOSE = "gist" /\ told = 1 /\ part1 /\ st = "owed"

\* owed.py's close rule, on one ended reply. Only a reply whose request carried the
\* note can close (§3: "when it carried the note in its request"). A reply played through:
\* the verdict on its whole text (PtVerdicts: lexical or the gist judge; no verdict
\* closes), or the backstop. A cut: the cut judge's verdict `v`.
Closes(p, n, cut, g, v) ==
    /\ n
    /\ IF     Mut("fraction")    THEN p \in Runs /\ g \in {"lead", "some", "all"}
       \* singleWord keeps the per-item rule: only the common-word list and the "2" are gone,
       \* so a filler whose common words meet every item closes, one item's terms do not.
       ELSE IF Mut("singleWord") THEN RunUncut(p, cut, g) \/ g \in {"filler", "all"}
       ELSE IF Mut("oneItem")    THEN RunUncut(p, cut, g) \/ g \in {"some", "all"}
       ELSE IF Mut("countSettle") THEN FALSE        \* the count below decides
       ELSE IF cut /\ Mut("prefixLexical") THEN g \in {"words", "all"}
       ELSE IF cut THEN v = "heard"
       \* one term-less item. Before "about": only a run played through uncut. "about":
       \* any noted reply about it, heard to its end -- played through, or cut after its
       \* last word (the ledger's pts-exact prefix is all of it): `g = "all"`
       ELSE IF OUTCOME = "failure"
            THEN IF RUNCLOSE = "about" THEN g = "all"
                 ELSE p \in Runs /\ ~cut /\ g \in {"some", "all"}
       ELSE \* the design: an uncut run, the verdict, or the backstop
            RunUncut(p, cut, g) \/ v \in {"heard", "none"} \/ Backstop(cut, g)

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
             [] g = "words" -> {"words", "all"}
             [] OTHER      -> {g}
      ELSE {g}

\* `arm`: the ending arms the re-offer whatever it closed (a caller turn's cut). Under
\* "anyMiss" a run that ended without closing the record arms it too. `mk`: this cut makes
\* the record (ORIGIN "cut"), its first cut telling.
Finish(cut, g, arm, mk) ==
    \E r \in Reads(cut, g) :
    \E v \in (IF cut THEN CutVerdicts(say, r)
              ELSE IF OUTCOME = "items" THEN PtVerdicts(r) ELSE {"skip"}) :
      LET u  == IF Mut("countSettle") /\ nreg /\ ~cut /\ g # "nothing"
                  THEN (IF uncut < 2 THEN uncut + 1 ELSE 2) ELSE uncut
          c  == Closes(req, nreg, cut, r, v) \/ (Mut("countSettle") /\ u = 2)
          ab == CLOSE = "gist" /\ ~cut /\ nreg /\ st = "owed" /\ About(r)
          \* a noted telling of the record, cut after its audio began (#231)
          ct == cut /\ nreg /\ st = "owed" /\ say \in {"tell", "part"} /\ g # "nothing"
                /\ ~Mut("cutsUncounted")
      IN /\ req' = "none" /\ rst' = "none" /\ noted' = FALSE /\ nreg' = FALSE
         /\ say' = "none" /\ ph' = "none"
         /\ heard' = (heard \/ g = "all")
         /\ fed' = IF c /\ st = "owed" THEN "close"
                   ELSE IF ct /\ cuts >= 1 THEN "drop" ELSE fed
         /\ cuts' = IF ct THEN (IF cuts < 2 THEN cuts + 1 ELSE 2) ELSE IF mk THEN 1 ELSE cuts
         /\ uncut' = u
         /\ told' = IF ab THEN (IF told < 2 THEN told + 1 ELSE 2) ELSE told
         /\ part1' = IF ab /\ told = 0 THEN r # "all" ELSE part1
         /\ hacc' = (hacc \/ (CLOSE = "gist" /\ nreg /\ st = "owed" /\ r = "all"))
         /\ missed' = (missed \/ arm
                       \/ (REOFFER = "anyMiss" /\ req \in Runs /\ st = "owed" /\ ~c))
         /\ got' = g

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
\* A consult OpenClaw CANCELLED (#230: the caller called it off) lands no record
\* (Owed._cancelled): that is a behaviour in which Land never fires, which Spec already
\* has (Land is not fair), so it needs no action of its own.
Land ==
    /\ ~ended /\ st = "none" /\ ORIGIN = "consult"
    /\ st' = "owed"
    /\ IF turn \/ q = "caller"
         THEN missed' = TRUE /\ UNCHANGED <<rloc, rk, hPost, displaced>>
         ELSE Ask("run") /\ UNCHANGED missed
    /\ UNCHANGED <<fed, uncut, reoffers, told, part1, hacc, cuts, dw, due, dck, heard, decl, repeat, line,
                   q, tres, tend, mark, gtool, reply, ended, runUnder, runMid, staleNote, noNote,
                   staleHeard, relook, retell, got, tellings, staleDecl>>

\* The settle feed: owed.py hears how the reply ended and closes the record on the
\* evidence. Its own step, so a reader that does not wait for it reads a stale record.
\* The feed's source is the reply's own end -- the interruption (ledger._interrupt, ahead
\* of the commit by CutReplyFence) or its synthesis done and all of its own audio played
\* (ledger._close_ready, at the window close its last chunk played out in) -- NOT a chart
\* of a reply that played through that comes only at the next bot activity.
\* A "drop" (the second cut telling, #231) ends it unheard: "dropped".
Settle ==
    /\ fed # "none"
    /\ fed' = "none"
    /\ st' = IF st # "owed" THEN st ELSE IF fed = "close" THEN "closed" ELSE "dropped"
    /\ UNCHANGED <<uncut, missed, reoffers, told, part1, hacc, cuts, dw, due, dck, heard, decl, repeat, line,
                   door, reply, ended, mons>>

\* The decline check (#231): once the words of a turn that cut a telling of the owed record
\* are committed, it is due, and every reader of the record -- the render of any request
\* (Issue), a run at the door (RunGo), the lull -- waits for it, after the cut has
\* settled. Not the commit's request alone: a turn that drops that request and commits
\* nothing of its own would leave the words unread, and the lull re-offer the answer they
\* declined. The check answers DECLINED on a dismissal or a correction -- or WANTED on one
\* (DECMISS), or DECLINED on words that wanted it (declineWrong). A record no longer owed
\* (the cut judge found it heard, the second cut dropped it) is left as it is.
\* declineAfter: the readers do not wait for it.
Decline ==
    /\ ~ended /\ dck /\ fed = "none"
    /\ \E v \in (IF dw = "decline" THEN {"declined"} \cup Opt(DECMISS, "wanted")
                 ELSE {"wanted"} \cup Opt(Mut("declineWrong"), "declined")) :
         st' = IF v = "declined" /\ st = "owed" THEN "declined" ELSE st
    /\ dw' = "none" /\ due' = FALSE /\ dck' = FALSE
    /\ UNCHANGED <<fed, uncut, missed, reoffers, told, part1, hacc, cuts, heard, decl, repeat, line,
                   door, reply, ended, mons>>

\* The readers of the record wait for a due decline check (declineAfter: they do not).
Checked == ~dck \/ Mut("declineAfter")

\* The lull (§4 (a)): the caller's turn has been answered and the line has been quiet for
\* LULL_S (~10 s) -- owed.py's lull timer, re-armed by every VAD start, turn and reply.
\* One re-offer per record, and only once the re-offer is armed (`missed`). An
\* environment action, enabled only on a quiet line with no turn open; it is time
\* passing, so it is fair (FairSpec).
Quiet == ~vad /\ ~pend /\ ~turn /\ q = "none" /\ req = "none" /\ ~tres /\ ~gtool
         /\ rloc = "none" /\ ~hPost
Lull ==
    /\ ~ended /\ st = "owed" /\ missed /\ Quiet
    /\ fed = "none" \/ Mut("lagSettle")
    /\ Checked
    /\ reoffers = 0 \/ (Mut("runAfterCut") /\ reoffers < 2)
    /\ reoffers' = reoffers + 1
    /\ missed' = FALSE                         \* runAfterCut: re-armed by the next cut
    /\ Ask("reoffer")
    /\ UNCHANGED <<st, fed, uncut, told, part1, hacc, cuts, dw, due, dck, heard, decl, repeat, line, q, tres,
                   tend, mark, gtool, reply, ended, runUnder, runMid, staleNote, noNote,
                   staleHeard, relook, retell, got, tellings, staleDecl>>

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
    /\ fed = "none" \/ Mut("lagSettle")
    /\ Checked
    /\ rloc' = "none" /\ rk' = "none"
    /\ IF st = "owed" \/ Mut("askOnly")
         THEN /\ req' = rk /\ rst' = st /\ noted' = Noted(st) /\ nreg' = Noted(st)
              /\ ph' = "gen"
              /\ UNCHANGED say
              /\ runUnder'   = (runUnder \/ vad \/ pend)
              /\ runMid'     = (runMid \/ turn \/ q # "none" \/ tres)
              /\ staleNote'  = (staleNote \/ (Noted(st) /\ st = "closed"))
              /\ noNote'     = (noNote \/ (st = "owed" /\ ~Noted(st)))
              /\ staleHeard' = (staleHeard \/ (Noted(st) /\ heard))
              /\ staleDecl'  = (staleDecl \/ (Noted(st) /\ decl))
         ELSE UNCHANGED <<reply, runUnder, runMid, staleNote, noNote, staleHeard, staleDecl>>
    /\ UNCHANGED <<rec, heard, decl, repeat, line, q, hPost, tres, tend, mark, gtool, ended,
                   displaced, relook, retell, got, tellings>>

\* The hold cap (TEAPORT_REPLY_HOLD_MAX_S) on a held run: DROPPED, never released (room
\* TV noise holds it forever otherwise; §4). A timer: never forced (not in FairSpec).
Cap ==
    /\ ~ended /\ rloc = "wait" /\ (vad \/ pend)
    /\ rloc' = "none" /\ rk' = "none"
    /\ missed' = (missed \/ (REOFFER = "anyMiss" /\ st = "owed"))
    /\ UNCHANGED <<st, fed, uncut, reoffers, told, part1, hacc, cuts, dw, due, dck, heard, decl, repeat,
                   line, q, hPost, tres, tend, mark, gtool, reply, ended, mons>>

\* The caller's VAD stopped and no words came within TEAPORT_REPLY_HOLD_RELEASE_S (a
\* cough): FollowupGate's _speech_over. The held post-tool answer goes on; a held run
\* goes on waiting for its window. A timer under UserTurn.tla's WORDS_IN_TIME premise:
\* it fires only once the words are known not to be coming (no turn opens instead).
GateQuiet ==
    /\ ~ended /\ pend /\ ~vad /\ ~turn
    /\ pend' = FALSE
    /\ IF hPost THEN hPost' = FALSE /\ q' = "post" ELSE UNCHANGED <<hPost, q>>
    /\ UNCHANGED <<rec, heard, decl, repeat, vad, turn, speech, rloc, rk, tres, tend, mark, gtool,
                   reply, ended, mons>>

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
    /\ UNCHANGED <<rec, heard, decl, repeat, line, mark, reply, ended, runUnder, runMid, staleNote,
                   noNote, staleHeard, relook, retell, got, tellings, staleDecl>>

\* The call's in-progress frame reaches the gate. It is uninterruptible, so it may come
\* after the barge-in that cancelled the call (TurnStart) or after its result. The
\* design keeps the gate's call ids: an id already settled or cancelled is not added
\* (`gtool` follows `tres`). staleFlag sets its one flag whatever happened meanwhile.
Mark ==
    /\ ~ended /\ mark
    /\ mark' = FALSE
    /\ gtool' = IF Mut("staleFlag") THEN TRUE ELSE (gtool \/ tres)
    /\ UNCHANGED <<rec, heard, decl, repeat, line, q, hPost, rloc, rk, tres, tend, reply, ended,
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
    /\ fed = "none" \/ Mut("lagSettle")
    /\ Checked                                      \* a due decline check holds it (#231)
    /\ \E sp \in (IF q = "caller" THEN BOOLEAN ELSE {FALSE}) :
         LET n == ~(Mut("renderInGet") /\ sp) /\ Noted(st) IN
         /\ req' = q /\ rst' = st /\ noted' = n /\ ph' = "gen" /\ q' = "none"
         /\ nreg' = (n /\ ~(Mut("adoptDropsKeys") /\ sp))
         /\ staleNote'  = (staleNote \/ (n /\ st = "closed"))
         /\ noNote'     = (noNote \/ (st = "owed" /\ ~n))
         /\ staleHeard' = (staleHeard \/ (n /\ heard))
         /\ staleDecl'  = (staleDecl \/ (n /\ decl))
    \* staleFlag: any completion's start clears the one flag (LLMFullResponseStartFrame).
    /\ gtool' = IF Mut("staleFlag") THEN FALSE ELSE gtool
    /\ UNCHANGED <<say, rec, heard, decl, repeat, line, hPost, rloc, rk, tres, tend, mark, ended,
                   runUnder, runMid, displaced, relook, retell, got, tellings>>

\* What the model may say, by who asked and what the request carried (header; DESIGN.md
\* "What the evidence supports"). "tool" is a sync tool call (get_current_time, ...),
\* only on a caller's turn, and "texttool" the same call with a line before it ("Let me
\* check." + get_weather): the line plays while the call is out; "lookup" is
\* openclaw_agent_consult again for what the outcome answers.
Says(p, r, n) ==
    LET pt == Opt(OUTCOME = "items", "part") IN    \* a failure is told whole or not at all
    IF p = "run"     THEN {"tell"} \cup (Opt(PARTIAL, "part") \cap pt) \cup Opt(SILENT, "silent")
    ELSE IF p = "reoffer"
                     THEN {"tell", "stay"} \cup (Opt(PARTIAL, "part") \cap pt)
                          \cup Opt(SILENT, "silent")
    ELSE LET t == IF p = "caller" THEN {"tool", "texttool"} ELSE {} IN
         IF n            THEN {"tell", "defer", "other"} \cup pt \cup t \cup Opt(FLAKY, "lookup")
         \* ORIGIN "cut": the reply after the sync result, before any reply ended, tells it
         ELSE IF r = "none"   THEN IF ORIGIN = "cut" /\ got = "none"
                                     THEN {"tell", "other"} \cup pt \cup t ELSE {"other"} \cup t
         ELSE IF r \in {"closed", "declined", "dropped"} THEN {"tell", "other"} \cup t
         ELSE {"tell", "defer", "other", "lookup"} \cup pt \cup t

\* The completion's first output. stay_silent (offered only to the re-offer run; the run
\* loop consumes the call and its result), a reply with no audio, and a bare tool call
\* end here, uncut and unheard. Everything else starts playing.
Say ==
    /\ ~ended /\ req # "none" /\ ph = "gen"
    /\ \E s \in Says(req, rst, noted) \ Opt(Restrained /\ nreg, "part") :
         /\ repeat' = (repeat \/ (req \in Runs /\ s \in {"tell", "part"} /\ heard))
         /\ retell' = IF noted /\ heard /\ s \in {"tell", "part"}
                        THEN (IF retell < 2 THEN retell + 1 ELSE 2) ELSE retell
         /\ relook' = (relook \/ (s = "lookup" /\ rst # "none"))
         /\ tellings' = IF noted /\ rst = "owed" /\ s \in {"tell", "part"}
                          THEN (IF tellings < 4 THEN tellings + 1 ELSE 4) ELSE tellings
         /\ IF s \in {"silent", "stay", "tool"}
              THEN /\ Finish(FALSE, "nothing", FALSE, FALSE)
                   /\ tres' = (s = "tool") /\ mark' = (s = "tool")
              ELSE /\ say' = s /\ ph' = "play"
                   /\ tres' = (s = "texttool") /\ mark' = (s = "texttool")
                   /\ UNCHANGED <<req, rst, noted, nreg, heard, fed, uncut, missed, told, part1,
                                  hacc, cuts, got>>
    /\ UNCHANGED <<st, reoffers, dw, due, dck, decl, line, q, hPost, rloc, rk, tend, gtool, ended, runUnder,
                   runMid, displaced, staleNote, noNote, staleHeard, staleDecl>>

\* BotStoppedSpeaking: the reply played to its end. A "texttool" reply's End passes the
\* gate here with its call still out (tend).
Played ==
    /\ ~ended /\ ph = "play"
    /\ \E g \in PlayGot(say) \ Opt(Restrained /\ nreg, "mention") : Finish(FALSE, g, FALSE, FALSE)
    /\ tend' = (tres /\ say = "texttool")
    /\ UNCHANGED <<st, reoffers, dw, due, dck, decl, repeat, line, q, hPost, rloc, rk, tres, mark, gtool,
                   ended, runUnder, runMid, displaced, staleNote, noNote, staleHeard, relook,
                   retell, tellings, staleDecl>>

(* ============================ the caller ============================ *)

\* The caller starts speaking: the gate's hold begins (pend), whatever is waiting stays.
VadStart ==
    /\ ~ended /\ ~vad /\ speech < MaxSpeech
    /\ vad' = TRUE /\ pend' = TRUE /\ speech' = speech + 1
    /\ UNCHANGED <<rec, heard, decl, repeat, turn, door, reply, ended, mons>>

VadStop ==
    /\ ~ended /\ vad
    /\ vad' = FALSE
    /\ UNCHANGED <<rec, heard, decl, repeat, pend, turn, speech, door, reply, ended, mons>>

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
    \* What they say as they cut a telling of the owed record (#231): they want the rest, or
    \* not (a dismissal, a correction). The words reach the brain at a commit, and are read
    \* there (Decline). A turn that cut no telling leaves an earlier turn's words to be read.
    \* ORIGIN "cut": the cut of the first reply's telling of the sync result makes the record
    \* (unless the judge found it all heard -- or OWES), its first cut telling.
    /\ IF req # "none"
         THEN \E g \in CutGot(say, ph) :
                LET tl == ph = "play" /\ say \in {"tell", "part"}
                    mk == ORIGIN = "cut" /\ st = "none" /\ got = "none" /\ tl
                          /\ req \in {"caller", "post"} /\ (g # "all" \/ OWES)
                IN /\ Finish(TRUE, g, st = "owed" \/ mk, mk)
                   /\ st' = IF mk THEN "owed" ELSE st
                   /\ IF (tl /\ nreg /\ st = "owed") \/ mk
                        THEN /\ dw' \in {"want", "decline"}
                             /\ due' = ~(Mut("noDecline") \/ (mk /\ Mut("cutRecordUnread")))
                        ELSE UNCHANGED <<dw, due>>
         ELSE /\ missed' = (missed \/ st = "owed")
              /\ UNCHANGED <<st, dw, due, reply, heard, fed, uncut, told, part1, hacc, cuts, got>>
    /\ UNCHANGED <<reoffers, decl, dck, repeat, vad, speech, mark, ended, runUnder, runMid,
                   displaced, staleNote, noNote, staleHeard, relook, retell, tellings, staleDecl>>

\* The turn ends: its words are committed and the LLM asked (TurnCommitMark claims the
\* window). The commit takes the words of speech that has stopped (UserTurn's
\* PendCommitted). An empty final (a 0-char decode, noise) commits nothing and asks
\* nothing (push_aggregation returns ""), as in Followup.tla's UserStop. Words that
\* declined a telling they cut are known to the brain from here (`decl`). An empty final
\* leaves them due: the next words the caller commits are read in their place.
\* noDecline reads none of them.
Commit ==
    /\ ~ended /\ turn /\ ~vad
    /\ turn' = FALSE /\ pend' = FALSE
    /\ \E words \in BOOLEAN :
         /\ q' = IF words THEN "caller" ELSE "none"
         /\ decl' = (decl \/ (words /\ dw = "decline"))
         /\ dck' = (dck \/ (words /\ due))
         \* words nobody will read: their meaning is in `decl` now
         /\ dw' = IF words /\ ~due THEN "none" ELSE dw
    /\ UNCHANGED <<st, fed, uncut, missed, reoffers, told, part1, hacc, cuts, due, heard, repeat,
                   vad, speech, hPost, rloc, rk, tres, tend, mark, gtool, reply, ended, mons>>

\* The client disconnects or the call hangs up. Nothing is owed to nobody: the escape in
\* the liveness property, never forced.
SessionEnd ==
    /\ ~ended
    /\ ended' = TRUE
    /\ UNCHANGED <<rec, heard, decl, repeat, line, door, reply, mons>>

Next ==
    \/ Land \/ Settle \/ Decline \/ Lull
    \/ RunGo \/ Cap \/ GateQuiet \/ ToolResult \/ Mark
    \/ Issue \/ Say \/ Played
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
    /\ WF_vars(Settle) /\ WF_vars(Decline) /\ WF_vars(Lull) /\ WF_vars(RunGo) /\ WF_vars(GateQuiet)
    /\ WF_vars(ToolResult) /\ WF_vars(Issue) /\ WF_vars(Say) /\ WF_vars(Played)
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
OneReoffer == reoffers <= 1

\* A heard answer is re-told at most once by a request that carried the note (#210: the
\* one-fact answer of 2026-10-23, told five times while the note said "NOT heard").
RetellOnce == retell <= 1

\* The model never re-looks-up what the outcome answers (the #203 49-second redo).
NoRedundantLookup == ~relook

\* A record ends "declined" only when the caller declined it (#231).
DeclinedOnEvidence == st = "declined" => decl

\* Once the caller has declined the rest in the words that cut its telling, no request
\* carries its note and no run goes for it (#231: "Great, that's all I needed", then the
\* rest re-told into unrelated turns).
NoNoteAfterDecline == ~staleDecl

\* A record is told at most three times by requests carrying its note: a telling cut or
\* played through counts, and the third one's end closes or drops it (#231: five tellings,
\* four of them cut, in five minutes).
BoundedTellings == tellings <= 3

\* Liveness: an owed answer is heard, or the session ends, or the brain has spent its one
\* re-offer (a terminal for UNPROMPTED speech only: the note goes on rendering, so the
\* caller who asks gets it).
OwedResolves == (st = "owed") ~> (st \in {"closed", "declined", "dropped"} \/ ended \/ reoffers > 0)

TypeOK ==
    /\ st \in {"none", "owed", "closed", "declined", "dropped"} /\ fed \in {"none", "close", "drop"}
    /\ cuts \in 0..2 /\ dw \in {"none", "want", "decline"} /\ decl \in BOOLEAN
    /\ due \in BOOLEAN /\ dck \in BOOLEAN /\ (dck => due) /\ (due => dw # "none")
    /\ tellings \in 0..4 /\ staleDecl \in BOOLEAN
    /\ uncut \in 0..2 /\ missed \in BOOLEAN /\ reoffers \in 0..2
    /\ told \in 0..2 /\ part1 \in BOOLEAN /\ hacc \in BOOLEAN /\ retell \in 0..2
    /\ heard \in BOOLEAN /\ repeat \in BOOLEAN
    /\ vad \in BOOLEAN /\ pend \in BOOLEAN /\ turn \in BOOLEAN /\ speech \in 0..MaxSpeech
    /\ q \in {"none", "caller", "post"} /\ hPost \in BOOLEAN
    /\ rloc \in {"none", "wait"} /\ rk \in {"none", "run", "reoffer"} /\ tres \in BOOLEAN
    /\ tend \in BOOLEAN /\ (tend => tres)
    /\ mark \in BOOLEAN /\ gtool \in BOOLEAN /\ (Mut("staleFlag") \/ (gtool => tres))
    /\ (rloc = "wait") = (rk # "none")
    /\ req \in {"none", "caller", "post", "run", "reoffer"}
    /\ rst \in {"none", "owed", "closed", "declined", "dropped"} /\ noted \in BOOLEAN
    /\ say \in {"none", "tell", "part", "defer", "other", "lookup", "texttool"}
    /\ ph \in {"none", "gen", "play"} /\ ((req = "none") = (ph = "none"))
    /\ (ph = "play") = (say # "none")
    \* the model's one-slot queue at the LLM is enough: a held post-tool answer and a
    \* queued request never coexist (a turn start drops the one, a commit needs a turn)
    /\ ~(hPost /\ q # "none")
    /\ ended \in BOOLEAN
    /\ got \in {"none", "nothing", "lead", "filler", "mention", "some", "words", "all"}
    /\ OWES \in BOOLEAN /\ CLOSE \in {"lexical", "gist"} /\ LEXMISS \in BOOLEAN
    /\ PTOWES \in BOOLEAN /\ NOVERDICT \in BOOLEAN /\ MENTION \in BOOLEAN /\ TWICE \in BOOLEAN
    /\ DECMISS \in BOOLEAN /\ ORIGIN \in {"consult", "cut"}
    /\ MUT \subseteq {"fraction", "countSettle", "singleWord", "oneItem", "storeNote",
                      "runAfterCut", "lagSettle", "oneSlot", "askOnly", "turnLevel",
                      "vadLevel", "renderInGet", "ledgerMax", "endClearsTool",
                      "staleFlag", "adoptDropsKeys", "judgeWrong", "prefixLexical",
                      "gistWrong", "noBackstop", "oneReply", "noDecline", "declineAfter",
                      "declineWrong", "cutsUncounted", "cutRecordUnread"}
    /\ REOFFER \in {"cutOrTurn", "anyMiss"} /\ RUNCLOSE \in {"uncut", "terms", "about"}
    /\ OUTCOME \in {"items", "failure"} /\ nreg \in BOOLEAN /\ (nreg => noted)
=============================================================================
