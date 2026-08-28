#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Offline SIP-brain PER-CALL test: a fake gateway drives 3 SEQUENTIAL calls on ONE
# persistent socket. Proves each call builds a FRESH pipeline (new STT session id),
# the connection + control survive between calls, the STT slot is freed between
# calls, and a tool (get_host_status) fires with a REAL value. Leaves the brain
# STOPPED afterwards. Env loading, path resolution, the stale sweep and the teardown
# are shared with run_sip_tools_test.sh — see sip_test_common.sh.
set -u

# Find the shared rig next to this script, else in the appliance checkout — the
# recorded runs invoke these runners from both places (see run_sip_tools_test.sh).
_common=
for _c in "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" \
          "${TEAPORT_BRAIN_SRC:-$HOME/teaport-src/brain}/test"; do
  if [ -r "$_c/sip_test_common.sh" ]; then _common=$_c/sip_test_common.sh; break; fi
done
[ -n "$_common" ] || { echo "sip_test_common.sh not found (set TEAPORT_BRAIN_SRC)" >&2; exit 1; }
. "$_common"
unset _c _common

SOCK=${SOCK:-/tmp/teaport-fakegw.sock}
QWAV=$TEST/question_host_status.wav
OUTPREFIX=/tmp/sip-percall
GWLOG=/tmp/sip-percall-fakegw.log
BRAINLOG=/tmp/sip-percall-brain.log
# 3 calls x (greeting 14s + question ~5s + reply cap 75s) + 2 inter-call pauses,
# plus room for the first model load. A cap exists at all because `wait $GWPID` is
# otherwise unbounded — see sip_test_report_gw_rc.
GW_TIMEOUT=${GW_TIMEOUT:-480}

sip_test_load_env
sip_test_host_snapshot "$BRAINLOG.hostref"
sip_test_clean_stale "$SOCK"

# Fake gateway (SERVER) first, so the brain client can connect.
timeout -k 10 "$GW_TIMEOUT" "$PY" "$TEST/fake_gateway_multicall.py" \
    --socket "$SOCK" --wav "$QWAV" --out-prefix "$OUTPREFIX" --calls 3 \
    --greet-wait 14 --reply-idle 3 --reply-cap 75 --between 3 > "$GWLOG" 2>&1 &
GWPID=$!
sleep 1

# Brain (CLIENT).
PYTHONPATH=$BRAIN "$PY" -m teaport_brain.sip_server \
    --socket "$SOCK" > "$BRAINLOG" 2>&1 &
BRAINPID=$!

# The fake gateway exits on its own after driving all 3 calls.
wait $GWPID
GWRC=$?
sip_test_report_gw_rc "$GWRC" "$GW_TIMEOUT" "$BRAINLOG"

# Let the brain flush the last call's ledger + tool logs, then STOP it by PID.
sip_test_stop_brain "$BRAINPID"
rm -f "$SOCK"

echo "=== fake_gateway_multicall rc=$GWRC ==="
echo "GWLOG=$GWLOG"
echo "BRAINLOG=$BRAINLOG"
echo "OUTPREFIX=$OUTPREFIX (per-call WAVs: ${OUTPREFIX}-call{1,2,3}-audio-out.wav)"
