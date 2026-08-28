#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Offline SIP-brain TOOLS test: fake gateway plays a host-status question WAV,
# the tools-enabled brain must FIRE get_host_status and speak the REAL value.
# Env loading, path resolution, the stale sweep and the teardown are shared with
# run_sip_percall_test.sh — see sip_test_common.sh (they diverged once, and the
# divergence was a bug).
set -u

# Find the shared rig next to this script, else in the appliance checkout. Both are
# needed: RUNLOG-sip-tools.md's recorded run copied this runner ALONE to /tmp and ran
# `bash /tmp/run_sip_tools_test.sh`, where a bare dirname finds no helper.
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
OUTWAV=/tmp/sip-tools-audio-out.wav
GWLOG=/tmp/sip-tools-fakegw.log
BRAINLOG=/tmp/sip-tools-brain.log
# One call's worst case: greeting 14s + question ~5s + reply cap 75s, plus room for
# the first model load. A cap exists at all because `wait $GWPID` is otherwise
# unbounded — see sip_test_report_gw_rc.
GW_TIMEOUT=${GW_TIMEOUT:-240}

sip_test_load_env
sip_test_host_snapshot "$BRAINLOG.hostref"
sip_test_clean_stale "$SOCK"

# Fake gateway (SERVER) first, so the brain client can connect.
timeout -k 10 "$GW_TIMEOUT" "$PY" "$TEST/fake_gateway.py" \
    --socket "$SOCK" --wav "$QWAV" --out-wav "$OUTWAV" \
    --greet-wait 14 --reply-idle 3 --reply-cap 75 > "$GWLOG" 2>&1 &
GWPID=$!
sleep 1

# Brain (CLIENT).
PYTHONPATH=$BRAIN "$PY" -m teaport_brain.sip_server \
    --socket "$SOCK" > "$BRAINLOG" 2>&1 &
BRAINPID=$!

# The fake gateway exits on its own after collecting the reply.
wait $GWPID
GWRC=$?
sip_test_report_gw_rc "$GWRC" "$GW_TIMEOUT" "$BRAINLOG"

# Let the brain flush the assistant ledger + tool logs, then STOP it by PID
# (leaves the brain stopped, as required — no restart).
sip_test_stop_brain "$BRAINPID"
rm -f "$SOCK"

echo "=== fake_gateway rc=$GWRC ==="
echo "GWLOG=$GWLOG"
echo "BRAINLOG=$BRAINLOG"
echo "OUTWAV=$OUTWAV"
