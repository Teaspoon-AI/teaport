#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Offline SIP-brain TOOLS test: fake gateway plays a host-status question WAV,
# the tools-enabled brain must FIRE get_host_status and speak the REAL value.
set -u
BRAIN=$HOME/teaport-src/brain
TEST=$BRAIN/test
SOCK=/tmp/teaport-fakegw.sock
QWAV=$TEST/question_host_status.wav
OUTWAV=/tmp/sip-tools-audio-out.wav
GWLOG=/tmp/sip-tools-fakegw.log
BRAINLOG=/tmp/sip-tools-brain.log

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
export SIP_HALF_DUPLEX=0          # full-duplex for the offline test (per task)
export LOGURU_LEVEL=DEBUG         # surface the tool handler's debug line + real values
export HF_HUB_OFFLINE=1

# Snapshot the REAL host state at test time, for hallucination comparison.
echo "=== REAL /proc/meminfo MemAvailable + loadavg at test start ===" > "$BRAINLOG.hostref"
grep MemAvailable /proc/meminfo >> "$BRAINLOG.hostref"
cat /proc/loadavg >> "$BRAINLOG.hostref"

# Clean any stale test socket / fake gateways (safe: matches only real fake_gateway.py procs)
pkill -f 'fake_gateway.py' 2>/dev/null || true
rm -f "$SOCK"
sleep 0.5

# Fake gateway (SERVER) first, so the brain client can connect.
/opt/teaport/venv/bin/python "$TEST/fake_gateway.py" \
    --socket "$SOCK" --wav "$QWAV" --out-wav "$OUTWAV" \
    --greet-wait 14 --reply-idle 3 --reply-cap 75 > "$GWLOG" 2>&1 &
GWPID=$!
sleep 1

# Brain (CLIENT).
PYTHONPATH=$BRAIN /opt/teaport/venv/bin/python -m teaport_brain.sip_server \
    --socket "$SOCK" > "$BRAINLOG" 2>&1 &
BRAINPID=$!

# The fake gateway exits on its own after collecting the reply.
wait $GWPID
GWRC=$?

# Let the brain flush the assistant ledger + tool logs, then STOP it by PID
# (leaves the brain stopped, as required — no restart).
sleep 2
kill "$BRAINPID" 2>/dev/null || true
sleep 1
kill -9 "$BRAINPID" 2>/dev/null || true
rm -f "$SOCK"

echo "=== fake_gateway rc=$GWRC ==="
echo "GWLOG=$GWLOG"
echo "BRAINLOG=$BRAINLOG"
echo "OUTWAV=$OUTWAV"
