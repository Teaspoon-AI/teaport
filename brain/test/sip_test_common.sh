# SPDX-License-Identifier: MIT
# shellcheck shell=bash
#
# Shared rig for the offline SIP-brain drivers (run_sip_tools_test.sh,
# run_sip_percall_test.sh). Both runs load the same env the same way, snapshot the
# same host state, sweep the same stale processes and stop the brain the same way;
# they differ only in WHICH fake gateway they launch and with what flags. That is
# all that is left in the runners — everything else lives here.
#
# This is not tidiness, it is the bug that produced it. The two runners had already
# drifted, and the drift was live: run_sip_percall_test.sh swept BOTH drivers before
# binding /tmp/teaport-fakegw.sock, run_sip_tools_test.sh swept only 'fake_gateway.py'
# — and `pkill -f 'fake_gateway.py'` does NOT match fake_gateway_multicall.py (the
# pattern is a regex, so the '.' is any-char and it needs "py" two characters past
# "fake_gateway", where the multicall driver has "_m"). Interrupt a per-call run and
# then start a tools run and the surviving multicall driver still owned the socket:
# `rm -f "$SOCK"` unlinked the path out from under it, the new fake_gateway.py bound
# a fresh socket at the same name, and the run then sat in `wait $GWPID` until the
# stale driver's own caps expired. One sweep, used by both, cannot drift like that.
#
# Source it, do not execute it:
#   . "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/sip_test_common.sh"

# --- Where we are, and what to run with --------------------------------------
# Derived from this file's own location so the tests run from a git checkout. They
# used to hardcode $HOME/teaport-src/brain and /opt/teaport/venv, which meant they
# ran on exactly one machine (the Jetson) and nowhere else — a contributor could
# read them but never run them. The appliance layout is now the FALLBACK, taken
# only when this file is not sitting inside a source tree (e.g. copied to /tmp,
# which is how RUNLOG-sip-tools.md records the original run).
SIP_TEST_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TEST=$SIP_TEST_DIR
BRAIN=$(dirname -- "$SIP_TEST_DIR")
if [ ! -f "$BRAIN/teaport_brain/sip_server.py" ]; then
  BRAIN=${TEAPORT_BRAIN_SRC:-$HOME/teaport-src/brain}
  TEST=$BRAIN/test
fi

# The interpreter is load-bearing: brain/tests/pinned_pipecat.py exists because the
# pipeline asserts on pipecat internals, and a python with the wrong pipecat imports
# cleanly and then fails somewhere deep in a call. Prefer the checkout venv that
# brain/tests/README.md builds (uv venv .venv + uv pip install -e ./brain), then the
# appliance venv. SIP_TEST_PYTHON overrides both.
PY=${SIP_TEST_PYTHON:-}
if [ -z "$PY" ]; then
  for _cand in "$(dirname -- "$BRAIN")/.venv/bin/python" /opt/teaport/venv/bin/python; do
    if [ -x "$_cand" ]; then PY=$_cand; break; fi
  done
  unset _cand
fi
if [ -z "$PY" ]; then
  echo "no venv python found: neither $(dirname -- "$BRAIN")/.venv nor /opt/teaport/venv." >&2
  echo "Build one per brain/tests/README.md, or set SIP_TEST_PYTHON." >&2
  exit 1
fi

# The live gateway's socket. The fake gateways refuse to BIND this path, but that
# guard runs inside python — far too late for the shell, which unlinks the socket
# before it ever launches a driver. Guarded here so an overridden $SOCK cannot make
# `rm -f` delete the real teaport-sip gateway's socket and drop live telephony. The
# path moved from /tmp to /run (RuntimeDirectory); both are refused, because a box
# running an older gateway still has the /tmp one.
SIP_LIVE_SOCKETS="/run/teaport/teaport-sip.sock /tmp/teaport-sip.sock"

# --- brain.env, loaded LITERALLY ---------------------------------------------
# Exactly like systemd's EnvironmentFile and for the same reason: no shell expansion,
# so LLM_EXTRA_BODY's JSON (braces, quotes, $) reaches the brain byte-for-byte.
# `source`-ing brain.env mangles it, and the brain then starts with a subtly wrong
# sampler config that looks like a model problem.
sip_test_load_env() {
  local envfile=${TEAPORT_BRAIN_ENV:-/etc/teaport/brain.env}
  if [ -r "$envfile" ]; then
    set -a
    local line key val
    while IFS= read -r line; do
      case "$line" in ''|\#*) continue;; esac
      key=${line%%=*}
      val=${line#*=}
      export "$key=$val"
    done < "$envfile"
    set +a
  elif [ -n "${LLM_BASE_URL:-}" ] && [ -n "${TEAPORT_URL:-}" ]; then
    envfile="(none — LLM_BASE_URL/TEAPORT_URL already exported)"
  else
    # Do not limp on. Without this file the brain comes up with no engine URL and no
    # LLM, the driver waits out its full reply cap against a brain that was never
    # going to speak, and the run reads as a pipeline failure rather than as setup.
    echo "no readable $envfile, and LLM_BASE_URL/TEAPORT_URL are not exported." >&2
    echo "Point TEAPORT_BRAIN_ENV at a brain.env, or export the LLM/engine vars." >&2
    exit 1
  fi

  # --- Test overrides ---
  export SIP_HALF_DUPLEX=0     # full-duplex offline: nothing here can echo back
  export LOGURU_LEVEL=DEBUG    # STT session-created/disconnect + the tool handlers'
                               # own debug lines with their REAL return values —
                               # those lines are the evidence the RUNLOGs quote
  export HF_HUB_OFFLINE=1

  echo "brain src : $BRAIN"
  echo "python    : $PY"
  echo "env file  : $envfile"
}

# --- Host state snapshot ------------------------------------------------------
# Taken BEFORE the brain loads, into a file the runner names. The tools test's whole
# claim is that get_host_status returned a live reading rather than a plausible
# invention, and that is only checkable against an independent reading of the same
# box from the same minute (the brain's own models move MemAvailable by hundreds of
# MB, so the two numbers differ — the point is that they track).
sip_test_host_snapshot() {
  local ref=$1
  echo "=== REAL /proc/meminfo MemAvailable + loadavg at test start ===" > "$ref"
  grep MemAvailable /proc/meminfo >> "$ref"
  cat /proc/loadavg >> "$ref"
}

# --- Stale process + socket sweep ---------------------------------------------
# pgrep/pkill -f match whole COMMAND LINES, not programs, so a plain `pkill -f
# fake_gateway.py` also kills every shell whose own argv happens to mention one: a
# copy-pasted command still in a `bash -c`, a CI step, an agent's eval — up to and
# including the shell running this test, which then dies between the sweep and the
# bind. (Observed while writing this: the sweep killed its own invoking wrapper.)
# So filter the matches down to processes that really ARE a driver, by checking argv[0]
# is a python interpreter. The drivers are mode 644 with no shebang, so they are always
# launched as `<python> fake_gateway*.py ...` — a shell that merely quotes that string
# has argv[0] of bash/zsh/sh and is skipped.
sip_test_driver_pids() {
  local pid argv0
  for pid in $(pgrep -f "$1" 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    argv0=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | head -1)
    case ${argv0##*/} in python|python[0-9]*) printf '%s\n' "$pid" ;; esac
  done
}

sip_test_kill_drivers() {
  local pid
  for pid in $(sip_test_driver_pids "$1"); do
    kill "-${2:-TERM}" "$pid" 2>/dev/null || true
  done
}

# Run before binding the test socket. See the header for the failure this exists to
# stop; the ordering below is the fix, and it is ordering-sensitive.
sip_test_clean_stale() {
  local sock=$1 live i
  for live in $SIP_LIVE_SOCKETS; do
    if [ "$sock" = "$live" ]; then
      echo "refusing to sweep the LIVE gateway socket $live" >&2
      exit 1
    fi
  done

  # Every fake gateway, not just this runner's. The prefix match is deliberate: the
  # next fake_gateway_*.py driver is covered without anyone remembering to add a line
  # here, and forgetting is precisely how the two runners diverged. [^/ ] keeps it
  # from spanning a path separator into an unrelated command line.
  local gw_pat='fake_gateway[^/ ]*\.py'
  # ...and a brain left over from an interrupted run, which is just as damaging: it
  # is a client, so it races the fresh brain for the driver's single accept slot and
  # answers the call the real test was watching. Matched STRICTLY on the TEST socket.
  # NEVER broaden this to a bare 'sip_server': teaport-sip-brain.service runs the same
  # module on the live UDS, and a bare pattern kills the appliance's telephony.
  local brain_pat="teaport_brain\\.sip_server .*--socket $sock"

  # Name what is being killed. "No leftover sip_server / fake_gateway processes" is a
  # claim every RUNLOG makes about the box it ran on, and a silent sweep gives the next
  # run log no way to say whether the box was already dirty when it started.
  local stale
  stale=$(sip_test_driver_pids "$gw_pat"; sip_test_driver_pids "$brain_pat")
  # shellcheck disable=SC2086  # deliberate split: one line of pids, not one per line
  [ -n "$stale" ] && echo "sweeping stale SIP test processes:" $stale

  sip_test_kill_drivers "$gw_pat"
  sip_test_kill_drivers "$brain_pat"

  # Wait for them to be GONE before touching the path — signalling is not reaping,
  # and both races that remain are races against a process that is still alive:
  #   (a) unlink now and a still-listening stale gateway keeps the old (now nameless)
  #       inode while we bind a new socket at the same name;
  #   (b) worse, a stale gateway that exits NORMALLY — caps expired, driver "done" —
  #       runs its own os.unlink(args.socket) on the way out and deletes the socket
  #       the new run just bound, so the brain's reconnect finds nothing.
  for i in {1..20}; do
    if [ -z "$(sip_test_driver_pids "$gw_pat")$(sip_test_driver_pids "$brain_pat")" ]; then
      break
    fi
    sleep 0.5
  done
  sip_test_kill_drivers "$gw_pat" KILL
  sip_test_kill_drivers "$brain_pat" KILL
  sleep 0.5

  # A survivor here is not ours to kill (a driver left by another user, most likely).
  # Say so rather than unlinking the path under it and reproducing the exact race this
  # function exists to prevent — the run that follows will look inexplicable otherwise.
  if [ -n "$(sip_test_driver_pids "$gw_pat")$(sip_test_driver_pids "$brain_pat")" ]; then
    echo "warning: stale drivers SURVIVED the sweep and still hold $sock;" >&2
    echo "         this run will race them. Check: pgrep -af fake_gateway" >&2
  fi

  rm -f "$sock"
}

# --- Brain teardown -----------------------------------------------------------
# By PID, and the runs deliberately leave the brain STOPPED afterwards.
sip_test_stop_brain() {
  local pid=$1
  # The pause is not politeness: the assistant ledger and the tool-call lines are
  # flushed at end of turn, and they are the whole record the RUNLOG quotes. Then
  # make sure it is really dead — a survivor holds the engine's single STT slot and
  # the next run gets a brain that connects and can never transcribe.
  sleep 2
  kill "$pid" 2>/dev/null || true
  sleep 1
  kill -9 "$pid" 2>/dev/null || true
}

# --- Gateway exit -------------------------------------------------------------
# The driver is launched under `timeout`, so $1 here is timeout's status: 124 means
# the cap fired. Worth naming, because an uncapped `wait $GWPID` is an unbounded
# hang — the drivers cap every phase of a call but nothing caps accept(), so a brain
# that dies before connecting (wrong venv, missing env, import error) leaves the
# driver waiting for a client that will never arrive.
sip_test_report_gw_rc() {
  local rc=$1 cap=$2 brainlog=$3
  if [ "$rc" = 124 ]; then
    echo "TIMEOUT: the fake gateway hit the ${cap}s cap — the brain probably never" >&2
    echo "connected or never answered; start at $brainlog" >&2
  fi
}
