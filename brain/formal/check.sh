#!/usr/bin/env bash
# Run every model in this directory. Needs Java 11+ and tla2tools.jar:
#   TLA_TOOLS=/path/to/tla2tools.jar ./check.sh          # the default run (CI)
#   TLA_TOOLS=/path/to/tla2tools.jar ./check.sh --full   # plus the long rows (by hand)
#
# --full adds the rows too large for the default run: today five SttCommit rows (the
# carry, the missed stop and the repaired final in their full environments) whose state
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
  # the properties are invariants over the states reached, not liveness.
  out=$(java -XX:+UseParallelGC -cp "$JAR" tlc2.TLC -nowarning -deadlock -workers auto \
          -config "$2.cfg" "$1" 2>&1)

  # The verdict TLC actually produced, and a human label for it.
  if grep -qi "no error has been found" <<<"$out"; then
    verdict=holds; label=holds
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

echo "Followup.tla — retiring the consult follow-up's one-shot trigger"
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
echo "Followup.tla — the delivery loop (#30): delivered means HEARD; a retry re-posts at the tail"
run Followup.tla fu_heard_inPlace_loss      "(expected: FAILS NoSilentLoss — 17:36: the retry restored in place, under the caller's question)"
run Followup.tla fu_heard_shipped_loss      "(expected: FAILS NoSilentLoss — as shipped: cut before its TTS opened, the ledger charts nothing, the wait times out to delivered)"
run Followup.tla fu_heard_shipped_repeat    "(expected: FAILS NoRepeatRecital — as shipped: a flushed attempt leaves the trigger live with no shot armed)"
run Followup.tla fu_heard_shipped_rest      "(expected: holds — as shipped: the budget, the turn and the narrator)"
run Followup.tla fu_heard_no_void_repeat    "(expected: FAILS NoRepeatRecital — the fix without void: the barge-in's own reply fires our shot)"
run Followup.tla fu_heard_no_retireUnarmed_repeat "(expected: FAILS NoRepeatRecital — the fix without retireUnarmed)"
run Followup.tla fu_heard_no_unspoken_loss  "(expected: FAILS NoSilentLoss — the fix without unspoken)"
run Followup.tla fu_heard_no_ownAudio_loss  "(expected: FAILS NoSilentLoss — the fix without ownAudio: a tool-ack line settles the watch as played)"
run Followup.tla fu_heard_ownStart_loss     "(expected: FAILS NoSilentLoss — ownAudio by the reply's TTSStartedFrame alone: a context that never plays)"
run Followup.tla fu_heard_no_supersede_loss "(expected: FAILS NoSilentLoss — the fix without supersede: earlier copies say already given)"
run Followup.tla fu_heard_fixed_all         "(expected: holds — void, retireUnarmed, unspoken, ownAudio, supersede)"
run Followup.tla fu_heard_fixed_timed_loss  "(expected: FAILS NoSilentLoss — the fix with the timers: the ACCEPTED RESIDUAL, a reply still playing at 45 s)"
run Followup.tla fu_heard_fixed_timed_repeat "(expected: FAILS NoRepeatRecital — the fix with the timers: the ACCEPTED RESIDUAL, a completion slower than 20 s)"
run Followup.tla fu_heard_fixed_timed_rest  "(expected: holds — the fix with the timers: the budget, the one live trigger, the narrator)"
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
run UserTurn.tla ut_shipped_all                       "(expected: holds — SIP as shipped, speculation on, early finals: every property but NoSplitTurn)"
run UserTurn.tla ut_shipped_nosplitturn               "(expected: FAILS NoSplitTurn — as shipped: words finalized before the retry's VAD stop are a second commit, the held reply still plays)"
run UserTurn.tla ut_retryMerges_all                   "(expected: holds — PROPOSED fix, not code: that second commit drops the unplayed reply and merges)"
echo
echo "PlayoutClock.tla — a held reply's playout times at the transport's clock (#85)"
run PlayoutClock.tla pc_release_words      "(expected: FAILS CommittedIsPlayed — first cut: only the words' pts moved, the End overtook them)"
run PlayoutClock.tla pc_release_firstFrame "(expected: FAILS WordsOnTime — round 1: the shift fixed on the TTSStartedFrame, LAT too far)"
run PlayoutClock.tla pc_release_all        "(expected: holds — every pts moved by the first held audio's wait)"
run PlayoutClock.tla pc_second_shipped     "(expected: FAILS SecondOnTime — AS SHIPPED: a context queued behind the held reply is anchored off its real start)"
run PlayoutClock.tla pc_second_fix2        "(expected: holds — PROPOSED, not code: anchor on the frozen clock, move by the hold since the first audio, one shift for the held queue)"
echo
echo "PlayoutPause.tla — heard accounting across playout pauses, and the TTS's freeze (#86)"
run PlayoutPause.tla pp_unaware_heard "(expected: FAILS HeardIsPlayed — the ledger before #86: its layout runs on through a pause)"
run PlayoutPause.tla pp_noShift_heard "(expected: FAILS HeardIsPlayed — layout frozen, but the words' pts read as scheduled)"
run PlayoutPause.tla pp_shipped_heard "(expected: holds — frozen, re-anchored, pts cut moved; two pauses in one reply)"
run PlayoutPause.tla pp_single_anchor "(expected: FAILS AnchorMovesByStop — one freeze slot shared by the hold and the pause)"
run PlayoutPause.tla pp_set_anchor    "(expected: holds — one freeze per source: frozen while stopped, the anchor moved by exactly the stop)"
run PlayoutPause.tla pp_late_shipped_heard "(expected: FAILS HeardIsPlayed — AS SHIPPED: a pause in an underrun, the next clause after it: its words carry the pause twice)"
run PlayoutPause.tla pp_late_moved_heard   "(expected: holds — PROPOSED, not code: _freeze_playout always moves _play_end by the stop)"
run PlayoutPause.tla pp_hold_anchor        "(expected: FAILS AnchorMovesByStop — AS SHIPPED: a hold from before the reply's first audio moves its end by the whole hold)"
run PlayoutPause.tla pp_hold_anchor_fixed  "(expected: holds — PROPOSED, not code: move it by the hold since its first audio, as _ctx_held_ns does)"
echo
echo "SttCommit.tla — when the STT closes the engine's transcript segment (#43)"
run SttCommit.tla sc_vadStop_split "(expected: FAILS NoSplitOnIncomplete — the commit at the raw VAD stop: before #43, and the no-verdict fallback's cost)"
run SttCommit.tla sc_vadStop_sound "(expected: holds — what that design did right, and what the fallback keeps)"
run SttCommit.tla sc_verdict_silentStop "(expected: FAILS NoOrphanedHold — the first cut: nothing reported when the turn is closed under the ceiling)"
run SttCommit.tla sc_verdict       "(expected: holds — as shipped: a lost verdict, a missed stop and a turn closed under the ceiling, the VAD one edge ahead)"
run SttCommit.tla sc_verdict_stale "(expected: FAILS NoSplitOnIncomplete — AS SHIPPED, the strategy three edges behind: a verdict for the stop before closes the held one)"
run SttCommit.tla sc_verdict_numbered "(expected: holds — PROPOSED, not code: the verdict names its stop and the STT ignores another's)"
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
if [ "$FULL" = 1 ]; then
  echo "  --full: the environments too large for the default run"
  run SttCommit.tla sc_vadStop_sound_carry "(expected: holds — the fallback's rows, with the carry)"
  run SttCommit.tla sc_voice_carry         "(expected: holds — the caller's voice, with the carry)"
  run SttCommit.tla sc_voice_missed        "(expected: holds — the caller's voice, with a missed VAD stop too)"
  run SttCommit.tla sc_voice_final_carry   "(expected: holds — the final under the wait, as shipped, with the carry and a lost verdict)"
  run SttCommit.tla sc_final_receipts_full "(expected: holds — the shipped final with the backstop, a lost verdict, the engine's cut and the carry)"
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
run SipCall.tla  sip_shipped           "(expected: holds — as shipped, auto_answer=false: no wrong teardown, no bring-up on the reader, no dead line, no stranded ring)"
run SipCall.tla  sip_shipped_auto      "(expected: holds — the same behind a gateway that answers by itself)"
run SipCall.tla  sip_shipped_teardown  "(expected: FAILS NoBlockedWithPendingControl — the ACCEPTED RESIDUAL: the inline teardown, bounded by 5 s + 0.3 s)"
run SipCall.tla  sip_shipped_unclaimed "(expected: FAILS NoUnclaimedCall — as shipped: a teardown task's presence.end() releases the NEXT call's claim)"
run SipCall.tla  sip_shipped_replaced  "(expected: FAILS NoReplacedCall — as shipped: the next call asks while the last call's claim is held)"
run SipCall.tla  sip_claimByCall       "(expected: holds — PROPOSED fix, not code: each teardown releases its own claim; start_call waits out the teardown tasks)"
echo
echo "SessionArbiter.tla — one conversation at a time: the ask, the hold, the resume (#58, #111)"
run SessionArbiter.tla sa_shipped          "(expected: holds — as shipped, under the SIP front-end's contract: Talk + room + two calls, answered mid-question)"
run SessionArbiter.tla sa_unaskable        "(expected: holds — answer_phone_call off: a call ends Talk, an awake room refuses it)"
run SessionArbiter.tla sa_overlap_refused  "(expected: FAILS NoEngineRefusal — as shipped, with the overlap SipCall finds: REPLACED resumes the held Talk, the call overwrites it)"
run SessionArbiter.tla sa_overlap_redecide "(expected: holds — PROPOSED fix, not code: after the end, decide again if the slot was taken back)"
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
run LedgerPlayout.tla lp_wrongText_unspeakable "(expected: FAILS ChartedTextMatchesContext — first-cut premise (no context for unspeakable text), no has_speech)"
run LedgerPlayout.tla lp_wrongText_unspeakable_checked "(expected: holds — the same premise, has_speech: why it shipped)"
run LedgerPlayout.tla lp_wrongText_unspeakable_opens   "(expected: FAILS ChartedTextMatchesContext — AS SHIPPED, pipecat as it is: the unspeakable reply's context opens and claims the next reply)"
run LedgerPlayout.tla lp_unheard_unspeakable_opens     "(expected: FAILS NoUnheardWhenPlayed — the same: the next reply charted never-played, then heard in full)"
run LedgerPlayout.tla lp_unspeakable_queued            "(expected: holds — PROPOSED fix, not code: queue every response the TTS opens a context for, has_speech or not)"
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
