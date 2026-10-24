#!/usr/bin/env bash
# Run every model in this directory. Needs Java 11+ and tla2tools.jar:
#   TLA_TOOLS=/path/to/tla2tools.jar ./check.sh          # the default run (CI)
#   TLA_TOOLS=/path/to/tla2tools.jar ./check.sh --full   # plus the long rows (by hand)
#
# --full adds the rows too large for the default run: today six SttCommit rows (the
# carry, the missed stop, the repaired final and the reset in their full environments) whose state
# spaces are tens of millions (formal/README.md).
# Nothing runs it automatically (CI runs the default on push and pull request): it is a
# manual gate, to run before merging any change to SttCommit.tla or to the commit paths
# it models (stt.py's commits, the hold, the backstop, the carry). About 30 min.
#
# Each row prints the design checked and its verdict, and is GATED against the
# expectation in the last column: this script exits nonzero if any row misses it --
# an "expected: holds" design that is violated; an "expected: FAILS <Prop>" design
# that holds (a lost counterexample), or that now fails on a DIFFERENT invariant than
# the one it names (e.g. a broken TypeOK masking the real counterexample); or a row
# with no verdict at all. The FAILING rows are intentional: the designs we rejected,
# kept so their counterexamples stay reproducible.
#
# A FAILS row MUST name the single property it is kept to demonstrate, and the gate
# insists the named one is what fell (a nameless FAILS row is a bad expectation). Its
# cfg may also list guard invariants (TypeOK, ...) the design does not break -- but not
# a second one it does: a rejected design often violates several, and TLC reports
# whichever its search reaches first, which varies with the seed, so a row listing two
# falling properties would name a different one run to run.
set -u
fails=0            # rows whose verdict did not match the expectation they declare
case "${1:-}" in
  --full) FULL=1 ;;
  "")     FULL=0 ;;
  *)      echo "usage: $0 [--full]" >&2; exit 2 ;;
esac
JAR="${TLA_TOOLS:-tla2tools.jar}"
[ -f "$JAR" ] || { echo "tla2tools.jar not found; set TLA_TOOLS=/path/to/tla2tools.jar" >&2; exit 2; }
cd "$(dirname "$0")"

run() {  # run <module> <config> <expectation>
  printf '  %-12s %-32s %-26s ' "$1" "$2" "$3"
  # -deadlock: a state with no successor is not a finding here. The bounds every model
  # carries (stops, faults, the engine's closes) make terminal states ordinary, and
  # the properties are invariants over the states reached -- except Owed.tla's liveness
  # rows, whose fair spec reads a terminal state as stuttering, which is what they mean.
  out=$(java -XX:+UseParallelGC -cp "$JAR" tlc2.TLC -nowarning -deadlock -workers auto \
          -config "$2.cfg" "$1" 2>&1)

  # The verdict TLC actually produced, and a human label for it.
  if grep -qi "no error has been found" <<<"$out"; then
    verdict=holds; label=holds
  elif grep -q "Temporal properties were violated" <<<"$out"; then
    # TLC does not name the temporal property that fell. A liveness row checks exactly
    # one PROPERTY, so that is the one; a cfg with two cannot name it and the gate below
    # reports the row as WRONG PROPERTY.
    verdict=fails
    props=$(sed -En 's/^[[:space:]]*PROPERTY[[:space:]]+([A-Za-z][A-Za-z0-9_]*).*/\1/p' "$2.cfg")
    if [ "$(wc -w <<<"$props")" -eq 1 ]; then label="Property $props is violated"
    else label="a temporal property is violated"; fi
  elif grep -qE "is violated|were violated" <<<"$out"; then
    verdict=fails
    label=$(grep -oE "(Invariant|Property) [A-Za-z0-9_]+ is violated" <<<"$out" | head -1)
    [ -n "$label" ] || label="a property is violated"
  else
    verdict=error; label="ERROR (not a verdict): $(grep -m1 -E "Error|error|Exception" <<<"$out")"
  fi
  printf '%s' "$label"

  # What the row declares it expects. A FAILS row must also name the property it is
  # kept to demonstrate (the token after FAILS); a holds row names nothing.
  case "$3" in
    *FAILS*) expect=fails
             want=$(sed -En 's/.*FAILS[[:space:]]+([A-Za-z][A-Za-z0-9_]*).*/\1/p' <<<"$3") ;;
    *holds*) expect=holds; want= ;;
    *)       expect=bad;   want= ;;
  esac
  # The invariant TLC actually reported (empty if it was not an invariant violation).
  got=$(sed -En 's/.*(Invariant|Property)[[:space:]]+([A-Za-z][A-Za-z0-9_]*)[[:space:]]+is violated.*/\2/p' <<<"$label")

  # Gate: verdict direction first, then -- for a named FAILS row -- property identity.
  if [ "$expect" = bad ]; then
    printf '   <-- BAD EXPECTATION (needs holds/FAILS)\n'; fails=$((fails + 1))
  elif [ "$expect" = fails ] && [ -z "$want" ]; then
    printf '   <-- BAD EXPECTATION (FAILS must name its property)\n'; fails=$((fails + 1))
  elif [ "$verdict" = error ]; then
    printf '   <-- NO VERDICT\n'; fails=$((fails + 1))
  elif [ "$verdict" != "$expect" ]; then
    printf '   <-- MISMATCH (expected %s)\n' "$expect"; fails=$((fails + 1))
  elif [ "$expect" = fails ] && [ "$got" != "$want" ]; then
    printf '   <-- WRONG PROPERTY (expected %s, got %s)\n' "$want" "${got:-<none>}"; fails=$((fails + 1))
  else
    printf '\n'
  fi
}

echo "Followup.tla — retiring a one-shot order at its read (ClientNotes' reaction; the consult follow-up's until #203)"
run Followup.tla fu_asWritten_loss    "(expected: FAILS NoSilentLoss)"
run Followup.tla fu_asWritten_repeat  "(expected: FAILS NoRepeatRecital)"
run Followup.tla fu_gateOnOwn_repeat  "(expected: FAILS NoRepeatRecital)"
run Followup.tla fu_retireOnRead      "(expected: holds)"
echo
echo "Followup.tla — PR #13: releasing the LLM latch on a tool call (LATCH)"
run Followup.tla fu_clearedOnToolCall_existing "(expected: holds — blind)"
run Followup.tla fu_held_interject             "(expected: holds)"
run Followup.tla fu_clearedOnToolCall_interject "(expected: FAILS NoInterjectMidTurn)"
run Followup.tla fu_held_deadair               "(expected: FAILS NoDeadAirDuringTool)"
run Followup.tla fu_clearedOnToolCall_deadair  "(expected: holds)"
run Followup.tla fu_turnAware_interject        "(expected: holds)"
run Followup.tla fu_turnAware_deadair          "(expected: holds)"
run Followup.tla fu_turnAware_all              "(expected: holds)"
echo
echo "Owed.tla — owed until heard (#203): the record, the note rendered per request, closed on evidence"
echo "  (written before the code; owed.py, the render hook and the gate's door implement it)"
run Owed.tla ow_design            "(expected: holds — the design as the model corrected it (anyMiss, about), runs that tell part or nothing, every safety property)"
run Owed.tla ow_design_live       "(expected: holds — OwedResolves under FairSpec: heard, the session ends, or the one re-offer is spent)"
run Owed.tla ow_asWritten_closed  "(expected: FAILS ClosedOnEvidence — DESIGN §3 as written: an uncut run closes, and a run that told one item of two played through)"
run Owed.tla ow_asWritten_live    "(expected: FAILS OwedResolves — DESIGN §4 as written: a run with no audio (or dropped at the hold cap) arms no re-offer, owed for good)"
run Owed.tla ow_fraction          "(expected: FAILS ClosedOnEvidence — today's >= 1/3 of the words: a run cut before its items closes (#203))"
run Owed.tla ow_countSettle       "(expected: FAILS ClosedOnEvidence — two uncut noted replies close, neither with every item (X2-Igone))"
run Owed.tla ow_singleWord        "(expected: FAILS ClosedOnEvidence — no common-word list: \"Sure, take your time.\" closes)"
run Owed.tla ow_oneItem           "(expected: FAILS ClosedOnEvidence — one item's terms close a two-item outcome)"
run Owed.tla ow_ledgerMax         "(expected: FAILS ClosedOnEvidence — the ledger's heard_text as it is (the longer of the pts prefix and an estimate): a cut reads as more than played)"
run Owed.tla ow_storeNote         "(expected: FAILS NoStoredNote — the note stored at landing: rendered to the turn after it was heard)"
run Owed.tla ow_renderInGet       "(expected: FAILS NoteWhileOwed — the hook in get_chat_completions: the speculative reply goes out bare)"
run Owed.tla ow_runAfterCut       "(expected: FAILS OneReoffer — no bound on runs after a cut or a turn (today's 3-attempt loop, §4 (c)))"
run Owed.tla ow_lagSettle         "(expected: FAILS NoRunAfterHeard — the lull reads the record before the settle feed: a re-offer after a full telling)"
run Owed.tla ow_lagSettle_repeat  "(expected: FAILS NoRepeatRecital — the same: the re-offer recites what the caller just heard (08-26 08:17))"
run Owed.tla ow_askOnly           "(expected: FAILS NoRunAfterHeard — the run validated only at gate.ask: a post-tool answer ahead of it told it)"
run Owed.tla ow_oneSlot           "(expected: FAILS HoldNoLoss — the hold's single slot: a held run and a held post-tool answer, the newer displaces the older)"
run Owed.tla ow_turnLevel         "(expected: FAILS NoRunUnderSpeech — a turn-level window: the run goes into speech whose words have not come (#197))"
run Owed.tla ow_vadLevel          "(expected: FAILS NoRunMidTurn — a VAD-level window: the run goes between a tool call and its answer)"
run Owed.tla ow_endClearsTool     "(expected: FAILS NoRunMidTurn — the gate's turn ends at a completion's End with its call still out (\"Let me check.\" + a tool): the run goes before the answer)"
run Owed.tla ow_staleFlag          "(expected: FAILS OwedResolves — the gate's tool round as one flag: a cancelled call's uninterruptible in-progress frame sets it after the barge-in, no completion clears it, the door and the lull stay shut)"
run Owed.tla ow_adoptDropsKeys     "(expected: FAILS NoRepeatRecital — the commit adopting a speculation does not register the keys its note carried: a full telling is no evidence, and the lull tells it again)"
run Owed.tla ow_failure           "(expected: holds — a failure record (one term-less item) under the design (about): every safety property)"
run Owed.tla ow_failure_live      "(expected: holds — a failure record: OwedResolves under FairSpec)"
run Owed.tla ow_failure_runOnly   "(expected: FAILS NoRepeatRecital — a failure closed only by an uncut run: heard to its end on any other path, it is re-offered)"
run Owed.tla ow_flaky             "(expected: FAILS NoRedundantLookup — FLAKY (measured 1/20 in round 1): a re-lookup with the note in view)"
run Owed.tla ow_flaky_rest        "(expected: holds — FLAKY: every other property; a re-lookup costs time, never a lost or repeated answer)"
run Owed.tla ow_judgeWrong        "(expected: FAILS ClosedOnEvidence — the cut judge answers HEARD when the rest held part of the answer (the residual, 1/145 measured): the record closes unheard)"
run Owed.tla ow_prefixLexical     "(expected: FAILS ClosedOnEvidence — a cut decided by the lexical rule on the heard prefix (a8fa2ba): two content words of each item close it before its facts are out)"
run Owed.tla ow_judgeOwes         "(expected: FAILS NoRunAfterHeard — OWES, the residual: the judge answers OWED (or not in time) though all was heard (14/96 measured, at the trailing edge); the record stays owed and the lull re-offers it)"
run Owed.tla ow_judgeOwes_rest    "(expected: holds — OWES: every property but the re-offer's repeat of what was heard)"
run Owed.tla ow_gist_lexical      "(expected: FAILS RetellOnce — CLOSE lexical with LEXMISS, as shipped in #204: a one-fact answer told in the caller's words reads as not covering, and every noted turn re-tells it (2026-10-23 14:06))"
run Owed.tla ow_gist_noBackstop   "(expected: FAILS RetellOnce — the gist judge with no backstop: a judge that answers OWED on full tellings (PTOWES) re-tells a heard answer on every turn)"
run Owed.tla ow_gist_oneReply     "(expected: FAILS RetellOnce — the judges read the one reply, not what was heard of the record: a re-telling cut before its facts is OWED though the telling before it was heard)"
run Owed.tla ow_gist_ptOwes       "(expected: FAILS NoStaleNote — PTOWES, the residual (2-4/29 measured): a full telling judged OWED, the next request still says NOT heard)"
run Owed.tla ow_gist_ptOwes_rest  "(expected: holds — PTOWES: every property but the one re-telling and its stale note; RetellOnce by the backstop)"
run Owed.tla ow_gist_noVerdict    "(expected: FAILS ClosedOnEvidence — NOVERDICT, the user's choice: no verdict on a reply about the record played through closes it, a mention with no answer included)"
run Owed.tla ow_gist_noVerdict_rest "(expected: holds — NOVERDICT: every other property)"
run Owed.tla ow_gist_wrong        "(expected: FAILS ClosedOnEvidence — the gist judge answers HEARD on a part or a mention (0/25 by majority measured))"
run Owed.tla ow_gist_twice        "(expected: FAILS ClosedOnEvidence — TWICE: two about-replies with no answer in a row, and the backstop closes it unheard)"
echo
echo "OwedAnswers.tla — how a sync tool's answer becomes owed (#204): position, one more reply, the narrow re-lookup"
run OwedAnswers.tla oa_design          "(expected: holds — position only: an answer's first reply settles it if it says something of its own, one more if it did not; a caller turn in words of their own ends it; a round is a record's re-lookup only in the turn right after its cut)"
run OwedAnswers.tla oa_lose            "(expected: holds — LOSE: the caller moves on before the answer was settled; every property but NoRedundantLookup still holds)"
run OwedAnswers.tla oa_lose_lookup     "(expected: FAILS NoRedundantLookup — LOSE, what remains of before #204: an answer the caller moved on from is history, and the B0 shape may look it up again)"
run OwedAnswers.tla oa_lose_aside      "(expected: FAILS RecordIsOwn — ASIDE, the one case left: a caller turn in words of their own drops the post-tool request and is answered with something else, taken for the telling)"
run OwedAnswers.tla oa_fillerSettles   "(expected: FAILS RecordIsOwn — a reply with no terms of its own settles it (74c3509): \"Sure, take your time.\" cut is a record)"
run OwedAnswers.tla oa_ownWordsKeep    "(expected: FAILS RecordIsOwn — a caller turn in words of their own leaves an older answer open (74c3509): another question's answer, cut, is its record)"
run OwedAnswers.tla oa_streamCut_rest  "(expected: holds — STREAMCUT: a telling cut mid-stream spends a reply; every other property)"
run OwedAnswers.tla oa_streamCut       "(expected: FAILS NoRedundantLookup — STREAMCUT, the residual: both replies cut mid-stream (or unheard, then cut mid-stream), no record, the B0 shape)"
run OwedAnswers.tla oa_late            "(expected: holds — LATE: the readers' bounded wait runs out before a cut telling's settle, which re-reads the live book; every property)"
run OwedAnswers.tla oa_noCutRecord     "(expected: FAILS NoRedundantLookup — no record from a cut sync answer (before #204's sync extension: W-goahead 20/20 re-fetch))"
run OwedAnswers.tla oa_postOnly        "(expected: FAILS NoRedundantLookup — only the post-tool request carries the answer: the caller's turn that dropped it tells it, is cut, no record)"
run OwedAnswers.tla oa_noSecondChance  "(expected: FAILS NoRedundantLookup — the first reply only: the post-tool reply unheard, the telling after it cut, no record)"
run OwedAnswers.tla oa_partialRecord   "(expected: FAILS RecordIsOwn — a record from a telling cut mid-stream: a fragment)"
run OwedAnswers.tla oa_callRecords     "(expected: FAILS RecordIsOwn — a reply that calls again settles: its line before the call, cut, a record)"
run OwedAnswers.tla oa_actionOwed      "(expected: FAILS NoConfirmationOwed — an action's cut confirmation made a record)"
run OwedAnswers.tla oa_keepOpen        "(expected: FAILS NoLaterRoundFiling — a new round leaves an older answer open: the newer round's post-tool reply files it (the third review's cases A, C))"
run OwedAnswers.tla oa_relookAll       "(expected: FAILS NoRedundantLookup — every round started while a record is owed is its re-lookup: a new question's cut telling, no record)"
run OwedAnswers.tla oa_vagueConsent    "(expected: FAILS NoRedundantLookup — a round whose words carry no terms is a re-lookup whenever a record is owed (9b602f8; the fourth review's trace): \"Sure, go ahead.\" swallowed)"
run OwedAnswers.tla oa_lateReplace     "(expected: FAILS OwedUnheard — the late settle makes its record whatever the book holds: after a full telling)"
run OwedAnswers.tla oa_lateNoEvidence  "(expected: FAILS OwedUnheard — a reply rendered with the open answer is no evidence for the record made of it since: told in full, still owed)"
run OwedAnswers.tla oa_prefixLexical   "(expected: FAILS ToldOnHearing — a sync cut judged by the lexical rule on the heard prefix (a8fa2ba): \"the developer kit at two hundred\" taken for heard, no record)"
echo
echo "UserTurn.tla — whether the user can interrupt the bot at all"
echo "  (the precondition Ledger/LedgerPlayout's Interrupt action takes for granted)"
run UserTurn.tla ut_asWritten_nostrandedturn    "(expected: FAILS NoStrandedTurn)"
run UserTurn.tla ut_asWritten_nomissedbargein         "(expected: FAILS NoMissedBargeIn)"
run UserTurn.tla ut_retryOnQuiet_nostrandedturn "(expected: holds)"
run UserTurn.tla ut_retryOnQuiet_nomissedbargein      "(expected: holds)"
echo
echo "UserTurn.tla — the speculative reply (SPEC): asked on the settled interim under the ceiling, adopted at the commit"
run UserTurn.tla ut_spec_byContext_nostrandedturn  "(expected: holds)"
run UserTurn.tla ut_spec_byContext_nomissedbargein "(expected: holds)"
run UserTurn.tla ut_spec_byContext_nostalereply    "(expected: holds)"
run UserTurn.tla ut_spec_byText_nostalereply       "(expected: FAILS NoStaleReply — a context written after the snapshot)"
run UserTurn.tla ut_spec_byHistory_nostalereply    "(expected: FAILS NoStaleReply — a final that differs from the interim)"
echo
echo "UserTurn.tla — the reply hold (HOLD, #85): a reply started over a caller who resumed"
run UserTurn.tla ut_hold_none_nostalereply           "(expected: FAILS NoStaleReply — before #85: the reply plays over the resumed caller)"
run UserTurn.tla ut_hold_none_nosplitturn            "(expected: FAILS NoSplitTurn — before #85: a reply dropped unplayed leaves fragment + continuation)"
run UserTurn.tla ut_hold_gateNoMerge_nosplitturn     "(expected: FAILS NoSplitTurn — the hold without TurnMerge)"
run UserTurn.tla ut_hold_gateNoResume_nosilencedbot  "(expected: FAILS NoSilencedBot — first cut: a drop left the queue paused)"
run UserTurn.tla ut_hold_gateEndWaits_noendbehindhold "(expected: FAILS NoEndBehindHold — first cut: the End waited behind the hold)"
run UserTurn.tla ut_hold_gateNoEnding_noendbehindhold "(expected: FAILS NoEndBehindHold — no _ending: a hold started behind the End)"
run UserTurn.tla ut_hold_gateKeepsTool_nolatetool    "(expected: FAILS NoLateToolRound — before 2026-10-15: a dropped hold's tool-call frames (Uninterruptible) land after the turn that dropped it)"
run UserTurn.tla ut_hold_gateDropsResult_noorphancall "(expected: FAILS NoOrphanCall — c1c329f: a drop dropped a held result whose IN_PROGRESS had passed: the call never settled)"
run UserTurn.tla ut_hold_gateNoFoldCancelled_nosplitturn "(expected: FAILS NoSplitTurn — before 2026-10-15: the merge refused across the tool round the drop cancelled)"
run UserTurn.tla ut_hold_gate_all                    "(expected: holds — as shipped, ASSUMING the words arrive before the release timer / cap)"
run UserTurn.tla ut_hold_gate_timed_nostalereply     "(expected: FAILS NoStaleReply — the timers as they are: the ACCEPTED RESIDUAL, words later than the release)"
run UserTurn.tla ut_hold_gate_timed_rest             "(expected: holds — the timers as they are: every other property)"
echo
echo "UserTurn.tla — the barge-in pause (PAUSE, #86): pause on speech, cancel on words, resume on none"
run UserTurn.tla ut_pause_asWritten_nolostanswer      "(expected: FAILS NoLostAnswer — first cut: a one-word answer over the reply's end, dropped)"
run UserTurn.tla ut_pause_aOnly_nolostanswer          "(expected: FAILS NoLostAnswer — the near-end rule alone: an answer 0.5-1 s before the end, dropped)"
run UserTurn.tla ut_pause_tailAtPause_nolostanswer    "(expected: FAILS NoLostAnswer — round 1: synthesis completing DURING the pause leaves no tail)"
run UserTurn.tla ut_pause_shipped_reach_band          "(expected: FAILS NoPauseInTailBand — witness: a pause in the 0.5-1 s band is reachable as shipped)"
run UserTurn.tla ut_pause_shipped_reach_elapsed       "(expected: FAILS NoTailElapsed — witness: a held-back tail that would have ended is reachable as shipped)"
run UserTurn.tla ut_pause_noStopWord_nomissedbargein  "(expected: FAILS NoMissedBargeIn — no stop word: told stop, the paused bot resumes)"
run UserTurn.tla ut_pause_keepOnInterrupt_nostuckpause "(expected: FAILS NoStuckPause — a transport that keeps its pause through the interruption)"
run UserTurn.tla ut_pause_shipped_all                 "(expected: holds — barge_pause.py as shipped, with the reply hold and every other property)"
run UserTurn.tla ut_pause_shipped_timed               "(expected: holds — the shipped pause with the reply hold's timers as they are: everything but the accepted NoStaleReply residual)"
echo
echo "UserTurn.tla — the cut and the commit (FENCE, CutReplyFence) and the refused finalize's second commit (EARLY_FINAL)"
run UserTurn.tla ut_fence_none_nocutaftercommit       "(expected: FAILS NoCutAfterCommit — before 2026-10-13: the late-start barge-in's commit overtakes its cut)"
run UserTurn.tla ut_fence_none_nostalereply           "(expected: FAILS NoStaleReply — before 2026-10-13: the false TurnMerge disarms the gate under the turn's own reply)"
run UserTurn.tla ut_fence_none_nosplitturn            "(expected: FAILS NoSplitTurn — before 2026-10-13: the merge for an unplayed reply lands on the turn after)"
run UserTurn.tla ut_fence_commitOnly_nocutaftercommit "(expected: FAILS NoCutAfterCommit — 228298e: the speculation's snapshot spends the cut before the reply lands)"
run UserTurn.tla ut_fence_timed_nocutaftercommit      "(expected: FAILS NoCutAfterCommit — the fence's 0.5 s bound as it is: the ACCEPTED RESIDUAL)"
run UserTurn.tla ut_fence_timed_nosplitturn           "(expected: FAILS NoSplitTurn — the same bound as shipped: a commit past it, a continuation's included, is not merged: the ACCEPTED RESIDUAL)"
run UserTurn.tla ut_retryOnQuiet_early_nosplitturn    "(expected: FAILS NoSplitTurn — before 2026-10-15: words finalized before the retry's VAD stop are a second commit, the held reply still plays)"
run UserTurn.tla ut_retryMergesAtEnd_nolostgoodbye    "(expected: FAILS NoLostGoodbye — e62e67a: the session-end commit continued too, dropping the reply the End was behind)"
echo
echo "UserTurn.tla — a tool call overtaken by its own cancel (CALLS, #185)"
run UserTurn.tla ut_calls_asWritten_noorphancall      "(expected: FAILS NoOrphanCall — pipecat's aggregator as written: the late InProgress lands IN_PROGRESS for good)"
run UserTurn.tla ut_calls_dropAbove_noorphancall      "(expected: FAILS NoOrphanCall — a drop in a processor above the aggregator: a cancel that overtakes inside the aggregator's own queue gets past it)"
run UserTurn.tla ut_calls_settleAtCommit_nounsettledread "(expected: FAILS NoUnsettledRead — settling at the next commit: a sync tool's result asks the LLM before it)"
echo
echo "UserTurn.tla — the answer after the tool (POSTTOOL, #17): asked while the caller talks, played before their words"
run UserTurn.tla ut_post_asWritten_talk_noanswerunderspeech "(expected: FAILS NoAnswerUnderSpeech — pipecat's push, gated on the turn: Talk and the room)"
run UserTurn.tla ut_post_asWritten_sip_noanswerunderspeech  "(expected: FAILS NoAnswerUnderSpeech — the same on SIP: the filler disarmed the reply gate, the pause cannot hold an answer not yet asked)"
run UserTurn.tla ut_post_vadDrop_nolosttoolanswer           "(expected: FAILS NoLostToolAnswer — the issue's VAD filter: dropped under a cough, no turn ever reads the result)"
run UserTurn.tla ut_post_capDiscard_noanswerunderspeech     "(expected: FAILS NoAnswerUnderSpeech — discarding at the TTS hold's cap: the hold ends at the VAD stop, before the words)"
run UserTurn.tla ut_post_lullAtStop_noanswerunderspeech     "(expected: FAILS NoAnswerUnderSpeech — a queue that delivers at the VAD stop: the words come after it)"
run UserTurn.tla ut_post_gateNoCommit_noholdoncommittedspeech "(expected: FAILS NoHoldOnCommittedSpeech — the gate as first written: a turn of two segments, its words committed, still held for a release window with nobody talking)"
run UserTurn.tla ut_post_gateById_noanswerunderspeech "(expected: FAILS NoAnswerUnderSpeech — 0db54f6: a commit made while the caller still talked emptied the gate, and the rest of their words came after the answer)"
run UserTurn.tla ut_post_gateOwedBlind_noanswerunderspeech "(expected: FAILS NoAnswerUnderSpeech — ff033c9: the re-applied stop's commit, made before that stop's flush lands, emptied the gate)"
run UserTurn.tla ut_post_gate_talk_all                      "(expected: holds — FollowupGate holds the answer while speech is pending, drops it at the turn: Talk)"
run UserTurn.tla ut_post_gate_sip_all                       "(expected: holds — the same on SIP as shipped, with every property ut_shipped_all checks)"
run UserTurn.tla ut_post_gate_timed_noanswerunderspeech     "(expected: FAILS NoAnswerUnderSpeech — the timers as they are: the ACCEPTED RESIDUAL, words later than the release or speech past both caps)"
run UserTurn.tla ut_post_gate_timed_rest                    "(expected: holds — the timers as they are: every other property, NoLostToolAnswer included)"
run UserTurn.tla ut_post_asWritten_noskippedanswer          "(expected: FAILS NoSkippedAnswer — pipecat's skip for an open turn whose commit already ran: a result nothing answers (#196, not #17's fix))"
run UserTurn.tla ut_shipped_all                      "(expected: holds — SIP as shipped, speculation on, early finals, tool rounds: every property; a commit with the gate armed drops the unplayed reply and merges)"
run UserTurn.tla ut_shipped_timed                     "(expected: holds — the same with the hold's timers as they are (release, cap): everything but the accepted NoStaleReply residual)"
echo
echo "PlayoutClock.tla — a held reply's playout times at the transport's clock (#85)"
run PlayoutClock.tla pc_release_words      "(expected: FAILS CommittedIsPlayed — first cut: only the words' pts moved, the End overtook them)"
run PlayoutClock.tla pc_release_firstFrame "(expected: FAILS WordsOnTime — round 1: the shift fixed on the TTSStartedFrame, LAT too far)"
run PlayoutClock.tla pc_release_all        "(expected: holds — every pts moved by the first held audio's wait)"
run PlayoutClock.tla pc_second_asWritten   "(expected: FAILS SecondOnTime — BEFORE THIS FIX: a context queued behind the held reply is anchored off its real start)"
run PlayoutClock.tla pc_second_realClock   "(expected: FAILS SecondOnTime — the fix but anchored on the real clock: a context that starts after the held reply's pre-hold end is late)"
run PlayoutClock.tla pc_second_fix2        "(expected: holds — AS SHIPPED: anchor on the frozen schedule, move by the hold since the first audio, one shift for the held queue)"
echo
echo "PlayoutPause.tla — heard accounting across playout pauses, and the TTS's freeze (#86)"
run PlayoutPause.tla pp_unaware_heard "(expected: FAILS HeardIsPlayed — the ledger before #86: its layout runs on through a pause)"
run PlayoutPause.tla pp_noShift_heard "(expected: FAILS HeardIsPlayed — layout frozen, but the words' pts read as scheduled)"
run PlayoutPause.tla pp_shipped_heard "(expected: holds — frozen, re-anchored, pts cut moved; two pauses in one reply)"
run PlayoutPause.tla pp_single_anchor "(expected: FAILS AnchorMovesByStop — one freeze slot shared by the hold and the pause)"
run PlayoutPause.tla pp_set_anchor    "(expected: holds — one freeze per source: frozen while stopped, the anchor moved by exactly the stop)"
run PlayoutPause.tla pp_late_ahead_heard   "(expected: FAILS HeardIsPlayed — BEFORE THIS FIX: a pause in an underrun, the next clause after it: its words carry the pause twice)"
run PlayoutPause.tla pp_late_moved_heard   "(expected: holds — AS SHIPPED: _freeze_playout always moves _play_end by the stop)"
run PlayoutPause.tla pp_hold_anchor        "(expected: FAILS AnchorMovesByStop — BEFORE THIS FIX: a hold from before the reply's first audio moved its end by the whole hold)"
run PlayoutPause.tla pp_hold_anchor_fixed  "(expected: holds — AS SHIPPED: move it by the hold since its first audio, as _ctx_held_ns does)"
echo
echo "SttCommit.tla — when the STT closes the engine's transcript segment (#43)"
run SttCommit.tla sc_vadStop_split "(expected: FAILS NoSplitOnIncomplete — the commit at the raw VAD stop: before #43, and the no-verdict fallback's cost)"
run SttCommit.tla sc_vadStop_sound "(expected: holds — what that design did right, and what the fallback keeps)"
run SttCommit.tla sc_verdict_silentStop "(expected: FAILS NoOrphanedHold — the first cut: nothing reported when the turn is closed under the ceiling)"
run SttCommit.tla sc_verdict       "(expected: holds — as shipped: a lost verdict, a missed stop and a turn closed under the ceiling, the VAD one edge ahead)"
run SttCommit.tla sc_verdict_stale "(expected: FAILS NoSplitOnIncomplete — BEFORE NUMBERING (shipped until 2026-10-15), the strategy three edges behind: a verdict for the stop before closes the held one)"
run SttCommit.tla sc_verdict_numbered "(expected: holds — AS SHIPPED, the strategy three edges behind: the verdict names its stop and the STT ignores another's)"
echo
echo "SttCommit.tla — the caller's voice (#140): no commit drawn over words the VAD has not started on"
run SttCommit.tla sc_voice_blind "(expected: FAILS NoCutVoice — before #140: the verdict commits over the resumed voice)"
run SttCommit.tla sc_voice       "(expected: holds — as shipped: held for the pause, a VAD start or the bound)"
run SttCommit.tla sc_voice_reach "(expected: FAILS HeldOnlyForVerdict — witness: the wait is reached, a hold no verdict ends)"
run SttCommit.tla sc_voice_reach_pause  "(expected: FAILS NoPauseExit — witness: a wait ends on the caller's pause)"
run SttCommit.tla sc_voice_reach_resume "(expected: FAILS NoResumeExit — witness: a wait ends on a VAD start)"
run SttCommit.tla sc_voice_cap   "(expected: FAILS NoCapCut — the ACCEPTED RESIDUAL: the bound commits over voice that never paused)"
run SttCommit.tla sc_voice_catchup "(expected: FAILS NoCatchupCut — the ACCEPTED RESIDUAL: the catch-up bound commits over voice not yet heard)"
run SttCommit.tla sc_voice_final "(expected: holds — the final's protocol as shipped (receipts), under the wait, on the marked engine)"
echo
echo "SttCommit.tla — the carry (#154): the segment after a commit the caller was quiet for is fed"
run SttCommit.tla sc_carry_off   "(expected: holds — control: without a carry, no interim for a segment with no words)"
run SttCommit.tla sc_carry_reach "(expected: FAILS NoWordlessInterim — witness: a carry's audio streams an interim, the backstop behind it)"
echo
echo "SttCommit.tla — the final (PR #49): whose utterance a late final is, and when the turn may end on it"
run SttCommit.tla sc_final_clock         "(expected: FAILS NoStaleTurnEnd — the first cut: finals placed by clock)"
run SttCommit.tla sc_final_clock_marked  "(expected: FAILS NoStaleTurnEnd — the clock rule falls even on a marked wire)"
run SttCommit.tla sc_final_stop          "(expected: FAILS NoStaleTurnEnd — BEFORE RECEIPTS, on an engine before teagram-engine#36: the pairing residual, and THE ORDER's)"
run SttCommit.tla sc_final_stop_marked   "(expected: FAILS NoStaleTurnEnd — BEFORE RECEIPTS (shipped until 2026-10-15): an engine cut stamped as a stop the strategy has just counted)"
run SttCommit.tla sc_final_stop_stranded "(expected: FAILS NoStrandedTurn — BEFORE RECEIPTS: a stop's close lands before the strategy counts the stop, and the debt is never paid)"
run SttCommit.tla sc_final_receipts      "(expected: holds — AS SHIPPED: receipts for closes that come early, no stamp on an unasked final)"
run SttCommit.tla sc_final_receipts_backstop "(expected: holds — as shipped: the same, with the backstop's half in place of the engine's cut)"
run SttCommit.tla sc_final_stop_residual "(expected: holds — as shipped, on an engine before teagram-engine#36: every stale end is a mispairing)"
run SttCommit.tla sc_reset_owed          "(expected: FAILS NoStrandedTurn — BEFORE 2026-10-15: a lost engine session takes a stop's close, and the strategy still owes it)"
run SttCommit.tla sc_reset_pays          "(expected: holds — AS SHIPPED: the reset is the close of every stop the STT had seen)"
if [ "$FULL" = 1 ]; then
  echo "  --full: the environments too large for the default run"
  run SttCommit.tla sc_vadStop_sound_carry "(expected: holds — the fallback's rows, with the carry)"
  run SttCommit.tla sc_voice_carry         "(expected: holds — the caller's voice, with the carry)"
  run SttCommit.tla sc_voice_missed        "(expected: holds — the caller's voice, with a missed VAD stop too)"
  run SttCommit.tla sc_voice_final_carry   "(expected: holds — the final under the wait, as shipped, with the carry and a lost verdict)"
  run SttCommit.tla sc_final_receipts_full "(expected: holds — the shipped final with the backstop, a lost verdict, the engine's cut and the carry)"
  run SttCommit.tla sc_reset_pays_closes   "(expected: holds — the shipped reset, with the engine's own close)"
fi
echo
echo "SttSlot.tla — the engine's single STT slot: has the engine let go when the brain thinks so"
run SttSlot.tla  stt_fixedSettle     "(expected: FAILS NoFalseBusy — two arbiters, before #58 step 2: one connect attempt)"
run SttSlot.tla  stt_retryWhileBusy  "(expected: holds — two arbiters, with the connect retry)"
run SttSlot.tla  stt_one_fixedSettle "(expected: FAILS NoFalseBusy — the one arbiter without the retry: a release frees the arbiter first)"
run SttSlot.tla  stt_one             "(expected: holds — as shipped: one arbiter, the retry, a holder that lets go within END_WAIT_SECS)"
run SttSlot.tla  stt_one_endTimeout  "(expected: FAILS NoGrantedBusy — AS SHIPPED without that fact: the arbiter grants past a holder still tearing down)"
echo
echo "SipCall.tla — the SIP per-call lifecycle (teardown, the blocked reader, the call's claim)"
run SipCall.tla  sipwt_asWritten       "(expected: FAILS NoWrongTeardown — 2026-08: a stale disconnected tears down the next call)"
run SipCall.tla  sipwt_callIdChecked   "(expected: holds — 2026-08: teardown guarded by call_id)"
run SipCall.tla  sip_asWritten         "(expected: FAILS NoBlockedWithPendingControl — 2026-08: the bring-up blocks the reader)"
run SipCall.tla  sip_shipped           "(expected: holds — as shipped, auto_answer=false: no wrong teardown, no bring-up on the reader, no dead line, no stranded ring; every claim its call's own, let go before the next is taken)"
run SipCall.tla  sip_shipped_auto      "(expected: holds — the same behind a gateway that answers by itself)"
run SipCall.tla  sip_shipped_teardown  "(expected: FAILS NoBlockedWithPendingControl — the ACCEPTED RESIDUAL: the inline teardown, bounded by 5 s + 0.3 s)"
run SipCall.tla  sip_heldClaim_unclaimed "(expected: FAILS NoUnclaimedCall — before the claim fix: a teardown task's presence.end() released the NEXT call's claim)"
run SipCall.tla  sip_heldClaim_replaced  "(expected: FAILS NoReplacedCall — before the claim fix: the next call asked while the last call's claim was held)"
run SipCall.tla  sip_ownClaim_replaced   "(expected: FAILS NoReplacedCall — the claim fix's identity check alone: no finally, no wait for the teardown tasks)"
echo
echo "SessionArbiter.tla — one conversation at a time: the ask, the hold, the resume (#58, #111)"
run SessionArbiter.tla sa_shipped          "(expected: holds — as shipped, under the SIP front-end's contract (SipCall's SequentialClaims, which sip_shipped holds): Talk + room + two calls, answered mid-question)"
run SessionArbiter.tla sa_unaskable        "(expected: holds — answer_phone_call off: a call ends Talk, an awake room refuses it)"
run SessionArbiter.tla sa_overlap_refused  "(expected: FAILS NoEngineRefusal — before the SIP claim fix, with the overlap SipCall found: REPLACED resumes the held Talk, the call overwrites it)"
run SessionArbiter.tla sa_overlap_redecide "(expected: holds — an arbiter-side alternative, NOT taken: the SIP fix removes the overlap instead)"
run SessionArbiter.tla sa_roomWakes        "(expected: FAILS NoSilentLiveEnd — the ACCEPTED RESIDUAL: the room wakes between decide() and its end, one loop tick)"
echo
echo "Ledger.tla — the transcript ledger's bot-turn state machine, as written at PR #13 (asWritten)"
echo "  (the rejected design: every row fails, pinning the review's counterexamples)"
run Ledger.tla   ledger_phantom      "(expected: FAILS NoPhantomFullHeard)"
run Ledger.tla   ledger_ownStart     "(expected: FAILS AudioStartIsOwn)"
run Ledger.tla   ledger_unheard      "(expected: FAILS NoUnheardWhenPlayed)"
run Ledger.tla   ledger_untagged     "(expected: FAILS NoUnheardWhenPlayed)"
run Ledger.tla   ledger_split        "(expected: FAILS NoPrematureFullChart)"
run Ledger.tla   ledger_fillerSet    "(expected: FAILS FillerCtxRemembered)"
run Ledger.tla   ledger_once         "(expected: FAILS ChartedAtMostOnce — new)"
run Ledger.tla   ledger_wrongText    "(expected: FAILS ChartedTextMatchesContext)"
echo
echo "Ledger.tla — windowHead: the first fix (3a51294), itself superseded by LedgerPlayout.tla"
run Ledger.tla   ledgerfix_phantom   "(expected: holds)"
run Ledger.tla   ledgerfix_ownStart  "(expected: holds)"
run Ledger.tla   ledgerfix_unheard   "(expected: holds)"
run Ledger.tla   ledgerfix_untagged  "(expected: holds)"
run Ledger.tla   ledgerfix_fillerSet "(expected: holds)"
run Ledger.tla   ledgerfix_once      "(expected: holds)"
run Ledger.tla   ledgerfix_wrongText "(expected: holds)"
run Ledger.tla   ledgerfix_split     "(expected: FAILS NoPrematureFullChart — :236 still open)"
echo
echo "LedgerPlayout.tla — the playout design (transcript_ledger.py after the review): turns on"
echo "  the TTS's frames only, the transport's queue laid out, a stop frame per context, the End"
echo "  frame's post-drain re-push"
run LedgerPlayout.tla lp_phantom       "(expected: holds — strict: PLAYED, not queued)"
run LedgerPlayout.tla lp_unheard       "(expected: holds — read live: a reply charted unheard never plays after)"
run LedgerPlayout.tla lp_once          "(expected: holds)"
run LedgerPlayout.tla lp_wrongText     "(expected: holds — two replies)"
run LedgerPlayout.tla lp_premature     "(expected: holds — stalls, timeouts)"
run LedgerPlayout.tla lp_playedCharted "(expected: holds)"
run LedgerPlayout.tla lp_fillerSet     "(expected: holds)"
run LedgerPlayout.tla lp_all           "(expected: holds — every property, two replies and a filler)"
run LedgerPlayout.tla lp_resume        "(expected: FAILS NoPrematureFullChart — a context resumed after the stop-frame timeout: the ACCEPTED RESIDUAL)"
run LedgerPlayout.tla lp_unspeakable                   "(expected: holds — AS SHIPPED, every property: every response with text is queued; one with no audio and nothing synthesizable closes charting nothing)"
run LedgerPlayout.tla lp_wrongText_unspeakable "(expected: FAILS ChartedTextMatchesContext — first-cut premise (no context for unspeakable text), no has_speech)"
run LedgerPlayout.tla lp_wrongText_unspeakable_checked "(expected: holds — the same premise, has_speech at the queue: why it shipped)"
run LedgerPlayout.tla lp_wrongText_unspeakable_opens   "(expected: FAILS ChartedTextMatchesContext — BEFORE THIS FIX (has_speech at the queue), pipecat as it is: the unspeakable reply's context opens and claims the next reply)"
run LedgerPlayout.tla lp_unheard_unspeakable_opens     "(expected: FAILS NoUnheardWhenPlayed — BEFORE THIS FIX, the same: the next reply charted never-played, then heard in full)"
run LedgerPlayout.tla lp_unspeakable_charted           "(expected: FAILS NoUnheardAheadOfPlayed — the first proposal: queued AND charted heard-nothing, ahead of a reply that played; the corrector deletes that one)"
run LedgerPlayout.tla lp_unspeakable_noOpen            "(expected: FAILS ChartedTextMatchesContext — the fix rests on pipecat opening the context (test_tts_stop_frame.py pins it); without that, the first cut's bug returns)"
run LedgerPlayout.tla lp_endings                       "(expected: FAILS NoSweepAfterEnd — BEFORE THIS FIX, pipecat's close: a reply ending in \"!!\" (a last piece with no audio) holds its context, and the TTS, to the 15 s stop-frame timeout)"
run LedgerPlayout.tla lp_endings_closed                "(expected: holds — AS SHIPPED, every property: EngineTTSService closes each context at its End whatever its last run_tts yielded)"
run LedgerPlayout.tla lp_failed_charted                "(expected: FAILS NoUnheardAheadOfPlayed — a reply the engine fails on every clause, charted heard 0, ahead of one that played: the positional corrector's premise does not hold)"
run LedgerPlayout.tla lp_failed_position               "(expected: FAILS NoPlayedReplyDeleted — BEFORE THIS FIX (positional corrector): the failed reply's cut deletes the reply the caller heard)"
run LedgerPlayout.tla lp_position                      "(expected: FAILS NoPlayedReplyDeleted — BEFORE THIS FIX, no failure: a barge-in before a queued reply's first audio deletes the reply that played before it)"
run LedgerPlayout.tla lp_guarded                       "(expected: FAILS HeardPrefixOnOwn — the first fix (guarded, still the newest message, cut by cut): a reply cut partway with a queued reply committed after it keeps its full text)"
run LedgerPlayout.tla lp_unspeakable_tail              "(expected: FAILS HeardPrefixOnOwn — BEFORE THE EMOJI FIX (seqTail): an emoji reply, charted nothing but committed, after a reply cut partway takes its heard prefix)"
run LedgerPlayout.tla lp_failed                        "(expected: holds — AS SHIPPED (seq corrector: each cut on its own message), every property but NoUnheardAheadOfPlayed)"
run LedgerPlayout.tla lph_phantom      "(expected: holds — hermetic wiring)"
run LedgerPlayout.tla lph_once         "(expected: holds — hermetic wiring, two replies)"
run LedgerPlayout.tla lph_wrongText    "(expected: holds — hermetic wiring, two replies)"
run LedgerPlayout.tla lph_premature    "(expected: FAILS NoPrematureFullChart — hermetic wiring takes ended as synthesized)"

echo
if [ "$fails" -eq 0 ]; then
  echo "OK -- every row met its declared expectation."
else
  echo "FAIL -- $fails row(s) did not meet expectation (see the <-- markers above)."
  exit 1
fi
