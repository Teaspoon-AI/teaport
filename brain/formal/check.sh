#!/usr/bin/env bash
# Run every model in this directory. Needs Java 11+ and tla2tools.jar:
#   TLA_TOOLS=/path/to/tla2tools.jar ./check.sh
#
# Each line prints the design being checked and whether it holds. The FAILING rows
# are intentional: they are the designs we rejected, kept so the counterexamples
# stay reproducible. Only the rows marked (expected: holds) must hold.
#
# One property per failing row, deliberately. A rejected design often violates more
# than one, and TLC reports whichever its search reaches first — which varies with the
# seed, so a row checking several at once prints a different name run to run and is
# useless as a gate.
set -u
JAR="${TLA_TOOLS:-tla2tools.jar}"
[ -f "$JAR" ] || { echo "tla2tools.jar not found; set TLA_TOOLS=/path/to/tla2tools.jar" >&2; exit 2; }
cd "$(dirname "$0")"

run() {  # run <module> <config> <expectation>
  printf '  %-12s %-20s %-22s ' "$1" "$2" "$3"
  out=$(java -XX:+UseParallelGC -cp "$JAR" tlc2.TLC -nowarning -workers auto \
          -config "$2.cfg" "$1" 2>&1)
  if grep -qi "no error has been found" <<<"$out"; then echo "holds"
  else grep -oE "Invariant [A-Za-z]+ is violated" <<<"$out" | head -1; fi
}

echo "Followup.tla — retiring the consult follow-up's one-shot trigger"
run Followup.tla fu_asWritten_loss    "(expected: FAILS)"
run Followup.tla fu_asWritten_repeat  "(expected: FAILS)"
run Followup.tla fu_gateOnOwn_repeat  "(expected: FAILS)"
run Followup.tla fu_retireOnRead      "(expected: holds)"
echo
echo "SttSlot.tla — arbitration of the engine's single STT slot"
run SttSlot.tla  stt_fixedSettle     "(expected: FAILS)"
run SttSlot.tla  stt_retryWhileBusy  "(expected: holds)"
echo
echo "SipCall.tla — the SIP per-call lifecycle (teardown + the blocked reader)"
run SipCall.tla  sipwt_asWritten     "(expected: FAILS)"
run SipCall.tla  sipwt_callIdChecked "(expected: holds)"
run SipCall.tla  sip_asWritten       "(expected: FAILS)"
run SipCall.tla  sip_asyncSetup      "(expected: holds)"
