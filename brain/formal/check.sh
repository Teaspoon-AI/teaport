#!/usr/bin/env bash
# Run every model in this directory. Needs Java 11+ and tla2tools.jar:
#   TLA_TOOLS=/path/to/tla2tools.jar ./check.sh
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
JAR="${TLA_TOOLS:-tla2tools.jar}"
[ -f "$JAR" ] || { echo "tla2tools.jar not found; set TLA_TOOLS=/path/to/tla2tools.jar" >&2; exit 2; }
cd "$(dirname "$0")"

run() {  # run <module> <config> <expectation>
  printf '  %-12s %-32s %-26s ' "$1" "$2" "$3"
  out=$(java -XX:+UseParallelGC -cp "$JAR" tlc2.TLC -nowarning -workers auto \
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
echo "UserTurn.tla — whether the user can interrupt the bot at all"
echo "  (the precondition Ledger/LedgerPlayout's Interrupt action takes for granted)"
run UserTurn.tla ut_asWritten_nostrandedturn    "(expected: FAILS NoStrandedTurn)"
run UserTurn.tla ut_asWritten_nomissedbargein         "(expected: FAILS NoMissedBargeIn)"
run UserTurn.tla ut_retryOnQuiet_nostrandedturn "(expected: holds)"
run UserTurn.tla ut_retryOnQuiet_nomissedbargein      "(expected: holds)"
echo
echo "UserTurn.tla — the speculative reply (SPEC): asked under the ceiling, adopted at the commit"
run UserTurn.tla ut_spec_byContext_nostrandedturn  "(expected: holds)"
run UserTurn.tla ut_spec_byContext_nomissedbargein "(expected: holds)"
run UserTurn.tla ut_spec_byContext_nostalereply    "(expected: holds)"
run UserTurn.tla ut_spec_byText_nostalereply       "(expected: FAILS NoStaleReply)"
echo
echo "SttCommit.tla — when the STT closes the engine's transcript segment (#43)"
run SttCommit.tla sc_vadStop_split "(expected: FAILS NoSplitOnIncomplete — the commit at the raw VAD stop)"
run SttCommit.tla sc_vadStop_sound "(expected: holds — what that design did right)"
run SttCommit.tla sc_verdict_silentStop "(expected: FAILS NoOrphanedHold — the first cut: nothing reported when the turn is closed under the ceiling)"
run SttCommit.tla sc_verdict       "(expected: holds — TEAPORT_STT_COMMIT_ON=verdict, a lost verdict, a missed stop and a turn closed under the ceiling)"
echo
echo "SttSlot.tla — arbitration of the engine's single STT slot"
run SttSlot.tla  stt_fixedSettle     "(expected: FAILS NoFalseBusy)"
run SttSlot.tla  stt_retryWhileBusy  "(expected: holds)"
echo
echo "SipCall.tla — the SIP per-call lifecycle (teardown + the blocked reader)"
run SipCall.tla  sipwt_asWritten     "(expected: FAILS NoWrongTeardown)"
run SipCall.tla  sipwt_callIdChecked "(expected: holds)"
run SipCall.tla  sip_asWritten       "(expected: FAILS NoBlockedWithPendingControl)"
run SipCall.tla  sip_asyncSetup      "(expected: holds)"
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
echo "  the TTS's frames only, the transport's queue laid out, the End frame's post-drain re-push"
run LedgerPlayout.tla lp_phantom       "(expected: holds — strict: PLAYED, not queued)"
run LedgerPlayout.tla lp_unheard       "(expected: holds)"
run LedgerPlayout.tla lp_once          "(expected: holds)"
run LedgerPlayout.tla lp_wrongText     "(expected: holds — two replies)"
run LedgerPlayout.tla lp_premature     "(expected: holds — stalls, timeouts)"
run LedgerPlayout.tla lp_playedCharted "(expected: holds — new)"
run LedgerPlayout.tla lp_fillerSet     "(expected: holds)"
run LedgerPlayout.tla lp_resume        "(expected: FAILS NoPrematureFullChart — a context resumed after a timeout)"
run LedgerPlayout.tla lp_wrongText_unspeakable "(expected: FAILS ChartedTextMatchesContext — the ledger before has_speech: the kept counterexample)"
run LedgerPlayout.tla lp_wrongText_unspeakable_checked "(expected: holds — the ledger applies the engine's has_speech)"
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
