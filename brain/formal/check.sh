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
  printf '  %-12s %-32s %-26s ' "$1" "$2" "$3"
  out=$(java -XX:+UseParallelGC -cp "$JAR" tlc2.TLC -nowarning -workers auto \
          -config "$2.cfg" "$1" 2>&1)
  if grep -qi "no error has been found" <<<"$out"; then echo "holds"
  elif grep -q "is violated" <<<"$out"; then grep -oE "Invariant [A-Za-z]+ is violated" <<<"$out" | head -1
  else echo "ERROR (not a verdict): $(grep -m1 -E "Error|error|Exception" <<<"$out")"; fi
}

echo "Followup.tla — retiring the consult follow-up's one-shot trigger"
run Followup.tla fu_asWritten_loss    "(expected: FAILS)"
run Followup.tla fu_asWritten_repeat  "(expected: FAILS)"
run Followup.tla fu_gateOnOwn_repeat  "(expected: FAILS)"
run Followup.tla fu_retireOnRead      "(expected: holds)"
echo
echo "Followup.tla — PR #13: releasing the LLM latch on a tool call (LATCH)"
run Followup.tla fu_clearedOnToolCall_existing "(expected: holds — blind)"
run Followup.tla fu_held_interject             "(expected: holds)"
run Followup.tla fu_clearedOnToolCall_interject "(expected: FAILS)"
run Followup.tla fu_held_deadair               "(expected: FAILS)"
run Followup.tla fu_clearedOnToolCall_deadair  "(expected: holds)"
run Followup.tla fu_turnAware_interject        "(expected: holds)"
run Followup.tla fu_turnAware_deadair          "(expected: holds)"
run Followup.tla fu_turnAware_all              "(expected: holds)"
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
echo
echo "Ledger.tla — the transcript ledger's bot-turn state machine, as written at PR #13 (asWritten)"
echo "  (the rejected design: every row fails, pinning the review's counterexamples)"
run Ledger.tla   ledger_phantom      "(expected: FAILS)"
run Ledger.tla   ledger_ownStart     "(expected: FAILS)"
run Ledger.tla   ledger_unheard      "(expected: FAILS)"
run Ledger.tla   ledger_untagged     "(expected: FAILS)"
run Ledger.tla   ledger_split        "(expected: FAILS)"
run Ledger.tla   ledger_fillerSet    "(expected: FAILS)"
run Ledger.tla   ledger_once         "(expected: FAILS — new)"
run Ledger.tla   ledger_wrongText    "(expected: FAILS)"
echo
echo "Ledger.tla — windowHead: the first fix (3a51294), itself superseded by LedgerPlayout.tla"
run Ledger.tla   ledgerfix_phantom   "(expected: holds)"
run Ledger.tla   ledgerfix_ownStart  "(expected: holds)"
run Ledger.tla   ledgerfix_unheard   "(expected: holds)"
run Ledger.tla   ledgerfix_untagged  "(expected: holds)"
run Ledger.tla   ledgerfix_fillerSet "(expected: holds)"
run Ledger.tla   ledgerfix_once      "(expected: holds)"
run Ledger.tla   ledgerfix_wrongText "(expected: holds)"
run Ledger.tla   ledgerfix_split     "(expected: FAILS — :236 still open)"
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
run LedgerPlayout.tla lp_resume        "(expected: FAILS — a context resumed after a timeout)"
echo
echo "LedgerFifo.tla — the ledger that ships (66b6812): live wiring (Drain) and the hermetic tests' (fifoh_)"
run LedgerFifo.tla fifo_NoPhantomFullHeard        "(expected: holds)"
run LedgerFifo.tla fifo_AudioStartIsOwn           "(expected: holds)"
run LedgerFifo.tla fifo_NoUnheardWhenPlayed       "(expected: holds)"
run LedgerFifo.tla fifo_FillerCtxRemembered       "(expected: holds)"
run LedgerFifo.tla fifo_NeverMeansNever           "(expected: holds)"
run LedgerFifo.tla fifo_ChartedAtMostOnce         "(expected: holds)"
run LedgerFifo.tla fifo_ChartedTextMatchesContext "(expected: holds)"
run LedgerFifo.tla fifoh_ChartedAtMostOnce        "(expected: holds)"
run LedgerFifo.tla fifoh_ChartedTextMatchesContext "(expected: holds)"
run LedgerFifo.tla fifo_split                     "(expected: FAILS — the design's stance)"
run LedgerFifo.tla fifo_wrongText_unspeakable     "(expected: FAILS — residual)"
