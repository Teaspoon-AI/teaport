#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Offline SIP-brain PER-CALL test: a fake gateway drives 3 SEQUENTIAL calls on ONE
# persistent socket. Proves each call builds a FRESH pipeline (new STT session id),
# the connection + control survive between calls, the STT slot is freed between
# calls, and a tool (get_host_status) fires with a REAL value. Leaves the brain
# STOPPED afterwards.
set -u
BRAIN=$HOME/teaport-src/brain
TEST=$BRAIN/test
SOCK=/tmp/teaport-fakegw.sock
QWAV=$TEST/question_host_status.wav
OUTPREFIX=/tmp/sip-percall
GWLOG=/tmp/sip-percall-fakegw.log
BRAINLOG=/tmp/sip-percall-brain.log

# --- Load brain.env LITERALLY (like systemd EnvironmentFile: no shell expansion,
# so LLM_EXTRA_BODY JSON is not mangled) ---
set -a
while IFS= read -r line; do
  case "$line" in ''|\#*) continue;; esac
  key=${line%%=*}
  val=${line#*=}
  export "$key=$val"
done < /etc/teaport/brain.env
set +a

# --- Test overrides ---
export SIP_HALF_DUPLEX=0          # full-duplex for the offline test
export LOGURU_LEVEL=DEBUG         # surface STT session-created/disconnect + tool debug
export HF_HUB_OFFLINE=1

# Snapshot the REAL host state at test time, for hallucination comparison.
echo "=== REAL /proc/meminfo MemAvailable + loadavg at test start ===" > "$BRAINLOG.hostref"
grep MemAvailable /proc/meminfo >> "$BRAINLOG.hostref"
cat /proc/loadavg >> "$BRAINLOG.hostref"

# Clean any stale test socket / fake gateways (matches only real fake_gateway procs).
pkill -f 'fake_gateway_multicall.py' 2>/dev/null || true
pkill -f 'fake_gateway.py' 2>/dev/null || true
rm -f "$SOCK"
sleep 0.5

# Fake gateway (SERVER) first, so the brain client can connect.
/opt/teaport/venv/bin/python "$TEST/fake_gateway_multicall.py" \
    --socket "$SOCK" --wav "$QWAV" --out-prefix "$OUTPREFIX" --calls 3 \
    --greet-wait 14 --reply-idle 3 --reply-cap 75 --between 3 > "$GWLOG" 2>&1 &
GWPID=$!
sleep 1

# Brain (CLIENT).
PYTHONPATH=$BRAIN /opt/teaport/venv/bin/python -m teaport_brain.sip_server \
    --socket "$SOCK" > "$BRAINLOG" 2>&1 &
BRAINPID=$!

# The fake gateway exits on its own after driving all 3 calls.
wait $GWPID
GWRC=$?

# Let the brain flush the last call's ledger + tool logs, then STOP it by PID.
sleep 2
kill "$BRAINPID" 2>/dev/null || true
sleep 1
kill -9 "$BRAINPID" 2>/dev/null || true
rm -f "$SOCK"

echo "=== fake_gateway_multicall rc=$GWRC ==="
echo "GWLOG=$GWLOG"
echo "BRAINLOG=$BRAINLOG"
echo "OUTPREFIX=$OUTPREFIX (per-call WAVs: ${OUTPREFIX}-call{1,2,3}-audio-out.wav)"
