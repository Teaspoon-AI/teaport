#!/usr/bin/env bash
#
# teaport installer — served as:  bash <(curl -fsSL https://get.teaspoon.tech/teaport)
#
# Idempotent, re-runnable (re-run == repair). Installs the closed engine + models +
# the brain + (optionally) the OpenClaw plugin + systemd units on a JetPack 7.2 /
# CUDA 13 Jetson Orin, driven by the release manifest.
#
#   ./install.sh                   install / repair the whole appliance
#   ./install.sh --only brain      update just the brain: install its .deb (see below)
#   ./install.sh --rollback brain  put the previous brain package back
#   ./install.sh --only engine     update just the engine from the manifest (see below)
#   ./install.sh --rollback engine put the previous engine back
#   ./install.sh --dry-run         print what would happen; touch nothing
#   ./install.sh --help
#
# --only brain is how a brain change reaches an appliance that is already installed. The
# brain is a package, teaport-brain, built in CI (packaging/build-deb.sh) and never on the
# box: this installs the .deb the manifest names (brain.deb), or the one in
# TEAPORT_BRAIN_DEB, with apt (--allow-downgrades), keeps it in /var/cache/teaport (the
# newest three), renders the units that run brain code, restarts the one brain unit
# (teaport-brain: Talk and the phone line's front-end both live in it) and checks it
# comes back -- putting the package that was installed before back if it does not. The
# package carries the venv (/opt/teaport/brain), the teaport CLI and the config page's
# helper, so a rollback puts all of them back together. No EULA, no system packages beyond its dependencies, no
# engine download, no credential prompts: engine, agent, front door, bridge and SIP are
# left as they are.
# (One exception, once: a box that still has the retired teaport-sip-brain unit has it
# stopped before the swap and removed after it -- sip_brain_detach / sip_brain_retire --
# since the new brain serves the phone line itself; its drop-in tuning moves to brain.env.)
# A full install installs the same package, then -- once every unit and env file is
# written -- restarts the engine and the brain in one step and checks them. No automatic
# rollback there, since a full run changes far more than the brain (`--rollback brain`
# still works). The first package is the oldest a box can go back to: once the package's
# brain is up, the pre-package venvs under /opt/teaport are removed (brain_retire_old_venvs).
#
# --only engine does the same for an engine release (#158): `./install.sh --only engine`,
# from a checkout or the one-liner. Of the engine's files (binary, G2P dict, POS tagger,
# THIRD_PARTY_NOTICES.txt) and its two models it downloads the ones whose sha256 differs
# from the manifest's beside the live ones, and checks them as a full run does (ldd, and
# cuDNN, OpenBLAS and espeak-ng on the system). Then it keeps the live engine for a
# rollback (with any model it replaces), moves the new files in, renders
# teaport-engine.service from this checkout and restarts the engine (the brain goes down
# and comes back with it, and reconnects), and checks it listens and stays up with its G2P
# dict loaded, putting the previous engine back on its own if it does not.
# engine.env only gains the keys it lacks: a value already there stays as it is (a full
# run still rewrites them; who owns them is #264). The EULA gate runs (silent when this
# license version is accepted already); no brain build, no system packages, no other unit.
# A run that changes nothing restarts nothing.
# `--rollback engine` puts the previous engine back, its unit with it, and keeps the one
# it replaces, so a second run undoes it. Two are kept, in $PREFIX/engine.rollback/; a full
# run that changes the engine keeps one there too. Every run that changes anything holds
# $STATE/install.lock, so a second one started meanwhile stops at once (installer_lock).
#
# Env overrides (mainly for dev/testing):
#   TEAPORT_MANIFEST_URL   manifest URL or local path (default get.teaspoon.tech/manifest/stable.json)
#   TEAPORT_PREFIX         install root (default /opt/teaport; the brain package only installs there)
#   TEAPORT_ETC            non-secret env dir (default /etc/teaport)
#   TEAPORT_BRAIN_DEB      install the brain from this teaport-brain .deb (default: the manifest's)
#   TEAPORT_PLUGIN_SRC     install the plugin from this local dir instead of npm
#   TEAPORT_ACCEPT_EULA=1  accept the engine EULA non-interactively
#   TEAPORT_ALLOW_NON_JETSON=1   skip the Jetson/L4T gate (dev only; no real install)
#   TEAPORT_ENABLE_BRIDGE=1      install + enable the opt-in Discord voice bridge
#   TEAPORT_BRIDGE_GUILD_ID / TEAPORT_BRIDGE_FOLLOW_USER_ID   bridge guild + followed user
#   TEAPORT_BRIDGE_SRC     install the bridge from this local dir instead of cloning
#   TEAPORT_ENABLE_LOCAL_AUDIO=1  enable the opt-in local mic+speaker bridge (USB mic array)
#   TEAPORT_LOCAL_AUDIO_DEVICE    its ALSA device (default hw:CARD=Array,DEV=0, a ReSpeaker XVF3800;
#                                 set, it overrides the device already in local-audio.env)
#   TEAPORT_ALLOW_STALE_SOURCE=1  install from a checkout that is behind its upstream
#   TEAPORT_OPENCLAW_HEAP_MIB     the host OpenClaw gateway's JS heap limit (default 1536)
#   TEAPORT_HOST           the front door's name (default teaport.local, or teaport-<id>.local
#                          when another box already answers to teaport.local)
#   TEAPORT_TLS_CERT / TEAPORT_TLS_KEY  a certificate (full chain, PEM) and key for that name,
#                          served instead of the box's own; renewing them is up to you: replace
#                          both files, then `sudo systemctl reload caddy.service`
#   TEAPORT_REGENERATE_CA=1  replace the box's own certificate authority (every phone and
#                          laptop that trusts the box must install and trust the new one)
#
set -euo pipefail

MANIFEST_URL="${TEAPORT_MANIFEST_URL:-https://get.teaspoon.tech/manifest/stable.json}"
PREFIX="${TEAPORT_PREFIX:-/opt/teaport}"
ETC="${TEAPORT_ETC:-/etc/teaport}"
STATE="${TEAPORT_STATE:-/var/lib/teaport}"
SECRETS="${TEAPORT_SECRETS_DIR:-$HOME/.config/teaport}"
RUN_USER="${TEAPORT_USER:-$(id -un)}"
ACCEPT_EULA="${TEAPORT_ACCEPT_EULA:-0}"
ALLOW_NON_JETSON="${TEAPORT_ALLOW_NON_JETSON:-0}"
BRAIN_DEB_SRC="${TEAPORT_BRAIN_DEB:-}"
PLUGIN_SRC="${TEAPORT_PLUGIN_SRC:-}"
BRIDGE_SRC="${TEAPORT_BRIDGE_SRC:-}"
ALLOW_STALE_SOURCE="${TEAPORT_ALLOW_STALE_SOURCE:-0}"
DRY_RUN=0
ONLY=""       # --only <component>: "" = the whole appliance
ROLLBACK=""   # --rollback <component>
ARGS=("$@")   # as given: installer_lock runs this script again with them
# Where this script lives — lets a checkout install its own brain/plugin/systemd.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage_err() { printf '%s (see --help)\n' "$*" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --accept-eula) ACCEPT_EULA=1 ;;
    --only|--rollback)
      [ $# -ge 2 ] && [ -n "$2" ] || usage_err "$1 needs a component (brain or engine)"
      if [ "$1" = --only ]; then ONLY="$2"; else ROLLBACK="$2"; fi; shift ;;
    --only=*) ONLY="${1#*=}"; [ -n "$ONLY" ] || usage_err "--only needs a component (brain or engine)" ;;
    --rollback=*) ROLLBACK="${1#*=}"; [ -n "$ROLLBACK" ] || usage_err "--rollback needs a component (brain or engine)" ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) usage_err "unknown arg: $1" ;;
  esac
  shift
done
# brain and engine are the components with an incremental path so far; name the rest when they get one.
case "$ONLY" in ''|brain|engine) ;; *) usage_err "--only: unknown component '$ONLY' (supported: brain, engine)" ;; esac
case "$ROLLBACK" in ''|brain|engine) ;; *) usage_err "--rollback: unknown component '$ROLLBACK' (supported: brain, engine)" ;; esac
[ -z "$ONLY" ] || [ -z "$ROLLBACK" ] || usage_err "--only and --rollback are separate runs"

# --- output helpers ----------------------------------------------------------
log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
todo() { printf '\033[1;35mTODO:\033[0m %s\n' "$*"; }
# run: execute a command, or just print it under --dry-run.
run()  { if [ "$DRY_RUN" = 1 ]; then printf '\033[2m  [dry-run] %s\033[0m\n' "$*"; else "$@"; fi; }
# SUDO: privileged op (root when not already root). Honors --dry-run via run().
SUDO() { if [ "$(id -u)" = 0 ]; then run "$@"; else run sudo "$@"; fi; }
have() { command -v "$1" >/dev/null 2>&1; }
# contains <needle> <haystack> — substring test that replaces `cmd | grep -q needle`.
# Under `set -o pipefail` that idiom is a trap: grep -q exits at the first match, the writer
# takes SIGPIPE (141), and pipefail reports the pipeline as failed *because* it matched.
# Capture the output first, then test it — no pipe, no race.
contains() { case "$2" in *"$1"*) return 0 ;; *) return 1 ;; esac; }
# read_env_file <path> [--lenient] — print an env file, read privileged when the plain read
# fails. Ours are usually group-readable by RUN_USER (write_env's chgrp/chmod 640), but a
# first install under plain sudo leaves them root:root, and TEAPORT_USER may differ from
# whoever runs the repair. Plain `sudo`, not SUDO: a read, so --dry-run does it too.
# Fails when the read does; --lenient prints nothing instead, for a caller that can carry
# on without the file. The one copy: resolve_gateway_token, write_env and brain_verify
# each had their own, and a fix to one would have missed the others (#59).
read_env_file() {
  if [ -r "$1" ]; then cat "$1"
  elif [ "${2:-}" = --lenient ]; then sudo cat "$1" 2>/dev/null || true
  else sudo cat "$1"; fi
}

# --- one run at a time ---------------------------------------------------------
# Two runs at once trample each other: a full run's phase_engine stages and swaps the same
# engine files an --only engine run has staged (#158), and either can be half way through
# the brain's link. So a run holds an exclusive lock on $INSTALL_LOCK from main's first
# step to its last, and a second run stops at once and says so. flock(1) holds it, and runs
# this script again as its child with the lock's descriptor closed (-o): a process the run
# starts and leaves running (a gateway, say) inherits nothing, so the lock ends with the
# run. The lock file is root's (0644), which every user can open to lock. A dry run takes
# no lock; it says when a real run would stop.
INSTALL_LOCK="$STATE/install.lock"
installer_lock() {
  local me="$HERE/${BASH_SOURCE[0]##*/}" rc=0
  [ -z "${TEAPORT_INSTALL_LOCKED:-}" ] || return 0   # the child: flock(1) holds it for us
  if ! have flock; then warn "flock is missing: nothing stops a second install.sh from running beside this one"; return 0; fi
  if [ "$DRY_RUN" = 1 ]; then
    if [ -e "$INSTALL_LOCK" ]; then flock -n -E 75 "$INSTALL_LOCK" true 2>/dev/null || rc=$?; fi
    if [ "$rc" = 75 ]; then warn "another install.sh is running on this box now ($INSTALL_LOCK) — a real run stops here"; fi
    return 0
  fi
  if [ ! -f "$me" ]; then warn "not run from a file, so not locked: nothing stops a second install.sh from running beside this one"; return 0; fi
  if [ ! -e "$INSTALL_LOCK" ]; then SUDO mkdir -p "$STATE"; SUDO touch "$INSTALL_LOCK"; SUDO chmod 0644 "$INSTALL_LOCK"; fi
  TEAPORT_INSTALL_LOCKED=1 flock -n -o -E 75 "$INSTALL_LOCK" bash "$me" ${ARGS[@]+"${ARGS[@]}"} || rc=$?
  if [ "$rc" = 75 ]; then die "another install.sh is running on this box (it holds $INSTALL_LOCK) — let it finish, then run this again"; fi
  exit "$rc"
}

# --- manifest ----------------------------------------------------------------
MF=""  # local path to the fetched manifest
fetch_manifest() {
  MF="$(mktemp)"
  case "$MANIFEST_URL" in
    /*|file://*) cp "${MANIFEST_URL#file://}" "$MF" ;;
    *) curl -fsSL --retry 3 -o "$MF" "$MANIFEST_URL" || die "cannot fetch manifest: $MANIFEST_URL" ;;
  esac
  python3 -c "import json;json.load(open('$MF'))" 2>/dev/null || die "manifest is not valid JSON"
}
# mget <dotted.key.path> — read a string leaf out of the manifest (python3 = always present).
mget() { python3 - "$MF" "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d = d[k]
print(d)
PY
}

# download <url> <dest> <sha256> — resumable, verified, idempotent.
download() {
  local url="$1" dest="$2" sha="$3"
  if [[ "$url" == *TODO* || "$sha" == TODO ]]; then
    [ "$DRY_RUN" = 1 ] && { warn "placeholder artifact $(basename "$dest") — a real install is blocked until artifacts publish"; return 0; }
    die "artifact not published yet: $(basename "$dest") — the manifest has a placeholder url/sha"
  fi
  if [ -f "$dest" ] && [ -n "$sha" ] && echo "$sha  $dest" | sha256sum -c --status 2>/dev/null; then
    log "have $(basename "$dest") (sha ok)"; return
  fi
  log "download $(basename "$dest")"
  run curl -fL --retry 3 -C - -o "$dest.part" "$url" || die "download failed (is get.teaspoon.tech live?): $url"
  if [ "$DRY_RUN" != 1 ]; then
    # A bad .part is removed: `curl -C -` would resume it on every later run (the engine's
    # stage is kept across runs, #158), and fail the same way each time.
    [ -n "$sha" ] && { echo "$sha  $dest.part" | sha256sum -c --status || { rm -f "$dest.part"; die "sha256 mismatch: $dest (the download is removed; the next run starts it again)"; }; }
    mv "$dest.part" "$dest"
  fi
}

# --- EULA --------------------------------------------------------------------
# Manifest-driven license gate. The manifest pins the license version + text
# (engine.eula.{version,url,sha256}); acceptance is recorded per version in
# $STATE/eula-accepted.json, so re-runs repair silently and a changed license
# re-gates. The full text is displayed and explicitly accepted (clickwrap) —
# a URL pointer alone shows nobody anything on a headless box.
eula_gate() {
  local ver url sha
  ver="$(mget engine.eula.version 2>/dev/null || echo 1.0)"
  url="$(mget engine.eula.url 2>/dev/null || echo "")"
  sha="$(mget engine.eula.sha256 2>/dev/null || echo "")"
  local receipt="$STATE/eula-accepted.json"

  # Idempotent re-run: this license version is already accepted -> no prompt.
  if [ -f "$receipt" ]; then
    local prev; prev="$(python3 -c "import json;print(json.load(open('$receipt')).get('version',''))" 2>/dev/null || true)"
    if [ "$prev" = "$ver" ]; then log "engine license v$ver already accepted"; return; fi
    [ -n "$prev" ] && warn "engine license changed (accepted: v$prev, current: v$ver) — re-acceptance required"
  fi

  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] display engine license v%s and require acceptance (type "accept")\n' "$ver"
    return
  fi
  if [[ -z "$url" || "$url" == *TODO* ]]; then
    die "the engine license text is not published yet (manifest engine.eula.url is a placeholder)"
  fi

  # Fetch + verify the text. Fail closed: a license you cannot read cannot be accepted.
  local txt; txt="$(mktemp)"
  curl -fsSL --retry 3 -o "$txt" "$url" || die "cannot fetch the engine license: $url"
  if [ -n "$sha" ] && [ "$sha" != TODO ]; then
    echo "$sha  $txt" | sha256sum -c --status || die "engine license text failed sha256 verification"
  fi

  local method="tty"
  if [ "$ACCEPT_EULA" = 1 ]; then
    log "engine license v$ver accepted non-interactively (TEAPORT_ACCEPT_EULA=1 — you confirm you have read it: $url)"
    method="env"
  else
    [ -t 0 ] || die "the engine license needs acceptance — re-run on a TTY, or read $url and pass --accept-eula"
    printf '\n  Teaport Engine License v%s — summary (the full text below governs):\n' "$ver"
    printf '    - personal use by individuals: free\n'
    printf '    - any organizational or commercial use: requires a written license (stefano@teaspoon.tech)\n\n'
    read -r -p '  Press Enter to read the license... ' _ || true
    if have less; then less "$txt"; elif have more; then more "$txt"; else cat "$txt"; fi
    printf '\n'
    read -r -p '  Type "accept" to accept the Teaport Engine License v'"$ver"' (anything else aborts): ' ans
    [ "$ans" = "accept" ] || die "license not accepted — aborting"
  fi

  # Record the acceptance + keep the exact accepted text (both sides get a durable record).
  SUDO mkdir -p "$STATE"; SUDO chown "$RUN_USER" "$STATE"
  cp "$txt" "$STATE/EULA-v$ver.txt"
  local actual_sha; actual_sha="$(sha256sum "$txt" | cut -d' ' -f1)"
  printf '{"version":"%s","sha256":"%s","date":"%s","user":"%s","method":"%s"}\n' \
    "$ver" "$actual_sha" "$(date -Is)" "$RUN_USER" "$method" > "$receipt"
  log "engine license v$ver accepted (recorded in $receipt)"
}

# --- phases ------------------------------------------------------------------
# The "l4t38" in the asset name predates JetPack 7.2 shipping L4T R39 (7.2-b187 installs an
# R39.2 rootfs with CUDA 13.2). The binary is CUDA-13 based and resolves its deps on both R38
# and R39, so the name is a stale label rather than a real target. Renaming it would mean
# republishing the artifact and the manifest, so the key stays put.
ASSET_KEY="l4t38-cu13"   # JetPack 7.2 / CUDA 13 — L4T R38 or R39
NV_TEGRA_RELEASE=/etc/nv_tegra_release   # there on a Jetson only (also install_arp_multihome)
L4T_RELEASE=""           # R-number from $NV_TEGRA_RELEASE; empty off-Jetson (dev/dry-run)

# l4t_gate [--plan] — the Jetson/L4T gate; sets L4T_RELEASE. --plan (--only engine): a dry
# run off a Jetson warns and goes on, so `--only engine --dry-run` shows its plan anywhere,
# as --only brain's does. The full run's dry run still asks for TEAPORT_ALLOW_NON_JETSON.
l4t_gate() {
  if [ -f "$NV_TEGRA_RELEASE" ]; then
    local l4t; l4t="$(sed -n 's/.*# R\([0-9]\+\).*/\1/p' "$NV_TEGRA_RELEASE" | head -1)"
    L4T_RELEASE="$l4t"
    case "$l4t" in
      38|39) log "Jetson L4T R$l4t (JetPack 7.2) — engine asset: $ASSET_KEY" ;;
      *) die "unsupported L4T R${l4t:-?} — the engine ships for JetPack 7.2 (L4T R38/R39, CUDA 13) only" ;;
    esac
  elif [ "$ALLOW_NON_JETSON" = 1 ]; then
    warn "not a Jetson — continuing because TEAPORT_ALLOW_NON_JETSON=1 (dev/dry-run only)"
  elif [ "${1:-}" = --plan ] && [ "$DRY_RUN" = 1 ]; then
    warn "not an NVIDIA Jetson ($NV_TEGRA_RELEASE missing) — showing the plan; a real run stops here"
  else
    die "not an NVIDIA Jetson ($NV_TEGRA_RELEASE missing)"
  fi
}

phase_preflight() {
  log "preflight"
  l4t_gate
  local freegb; freegb="$(df -Pk "$(dirname "$PREFIX")" 2>/dev/null | awk 'NR==2{print int($4/1048576)}')" || true
  [ -n "$freegb" ] && [ "$freegb" -lt 15 ] && warn "only ${freegb}GB free near $PREFIX (need ~15GB for engine + models)"
  check_brain_source
  have curl || die "curl is required"
  # Look at the status code rather than using -f: the bare host root serves 404 (only /dl,
  # /manifest, /eula exist), so -f would warn "downloads will fail" on every healthy install —
  # but ignoring the code entirely calls a 502 from the edge "reachable" and defers the outage
  # to the download, an EULA prompt and ~15GB later. So 404 is the ONE healthy error shape;
  # anything else — 5xx, 000 (curl's code for a DNS/TCP/TLS failure), or another 4xx (a WAF's
  # 403, a proxy's 429) — deserves the early warning, because it is exactly the blocked-network
  # case that otherwise surfaces at the first download.
  if [ "$DRY_RUN" != 1 ]; then
    local code; code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 https://get.teaspoon.tech 2>/dev/null || true)"
    case "$code" in
      2*|3*|404) ;;
      *) warn "get.teaspoon.tech is not serving normally (HTTP ${code:-no response} — downloads will fail)" ;;
    esac
  fi
}

phase_sysdeps() {
  log "system deps: espeak-ng (engine G2P fallback), libopenblas0 (engine BLAS), libcudnn9-cuda-13 (engine TTS convs), logrotate (logs)"
  # espeak-ng is a hard runtime dep of the GPL-clean engine (arm's-length CLI child) —
  # but ONLY as the OOV + non-English fallback. The en-us G2P is the misaki-derived dict
  # that phase_engine installs to bin/g2p/; espeak-only en-us speaks robotic prosody.
  # libopenblas0 provides libopenblas.so.0, which the engine links against. JetPack does not
  # ship it, so without this the engine binary fails to load at first start.
  # libcudnn9-cuda-13 provides libcudnn.so.9: engine 1.5 on runs the Kokoro convs on
  # cuDNN (~2x TTS throughput, teagram-engine README). Full JetPack has it, the bare L4T
  # BSP does not; NVIDIA's Jetson apt repo, which L4T configures, carries it.
  SUDO apt-get update -qq
  SUDO apt-get install -y espeak-ng libopenblas0 libcudnn9-cuda-13 logrotate
  if [ "$DRY_RUN" != 1 ]; then engine_libs_check ""; fi
  setup_logs
}
# engine_libs_check <hint> — what the engine needs from the system at run time (see
# phase_sysdeps): espeak-ng, libopenblas.so.0, and libcudnn.so.9 (engine 1.5 on). The one
# copy: phase_sysdeps checks that its packages took, --only engine (which installs none)
# that the release it is about to start will find them. <hint> ends each message.
engine_libs_check() {
  local libs; libs="$(ldconfig -p 2>/dev/null || true)"
  have espeak-ng || die "espeak-ng not installed$1"
  contains 'libopenblas.so.0' "$libs" || die "libopenblas.so.0 not installed$1"
  contains 'libcudnn.so.9' "$libs" || die "libcudnn.so.9 not installed$1"
}

# --- logs: one store, capped -----------------------------------------------------------
# The L4T image keeps logs twice: journald's journal in RAM (volatile, capped at 10% of
# /run, gone at every reboot) and, forwarded from it, rsyslog's /var/log/syslog on disk,
# which nothing capped or rotated: the image ships without logrotate. One appliance had a
# 901 MB syslog grown since first boot, ~50 MB a day (two thirds of it the brain), and a
# component logging in a loop would have filled the disk.
# teaport reads its logs through journalctl only (teaport logs, doctor, this installer), so
# the journal becomes the one store: persistent, so the last boot's logs survive the next
# one; capped at JOURNAL_MAX_USE by journald itself, which also keeps 15% of the disk free
# (at most 4G: SystemKeepFree's default); and no longer forwarded to rsyslog. A journal
# entry takes several times its text on disk, so 2G is days to a couple of weeks at the
# box's rate, not a month: `journalctl --disk-usage` says how much it holds.
# /var/log/syslog then gets only what rsyslog reads itself (the kernel's messages), and
# logrotate, installed with the system packages, rotates that and the rest of /var/log with
# Ubuntu's own rules. (An old, big syslog takes about two weeks to free its space: logrotate's
# first run only records it, the weekly rule moves it to syslog.1, and delaycompress gzips
# that a week later. `sudo logrotate -f /etc/logrotate.conf` does it now.) Anything
# that read /var/log/syslog or auth.log for the services' lines (remote forwarding,
# fail2ban's file backend) reads the journal instead from here on.
# The journal config is install.sh's, like the units: $JOURNAL_DROPIN is rewritten on every
# run and wins, and the directory it lives in is kept at systemd's 0755, so it can be read
# back without sudo. An operator who wants something else puts it in a drop-in of their own
# named to sort after ours (zz-*.conf): journald reads the drop-ins in name order and the
# last one wins. (Setting Storage there? Set ForwardToSyslog too: with a volatile journal
# and no forwarding, nothing would be kept on disk.)
# Ours is named to sort after Ubuntu's journald.conf.d/syslog.conf (ForwardToSyslog=yes),
# so it wins over that; a "99-teaport.conf" would lose to it (digits sort before letters).
# journald is restarted only when the settings themselves are new to it: they differ from
# what it last took ($JOURNAL_APPLIED, written once a restart has worked) or from what the
# file on disk held (a deleted drop-in and a reboot). A comment-only change restarts
# nothing, and a restart that failed is tried again by the next run.
# Not covered here: logs that skip both journald and rsyslog (a container runtime's own
# files, say).
# write_root <path> <text>: a root-owned 0644 file holding <text>. The mode is set, not left
# to sudo's umask (a 0600 file would read back empty below, so every run would rewrite it
# and restart journald); install -D makes missing parents and leaves existing ones be.
write_root() {
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] write %s\n' "$1"; return 0; fi
  printf '%s\n' "$2" | SUDO install -D -m 0644 -o root -g root /dev/stdin "$1"
}
# journal_settings: the drop-in's text without comments and blank lines (what journald reads)
journal_settings() {
  grep -v -e '^[[:space:]]*[#;]' -e '^[[:space:]]*$' "$JOURNAL_DROPIN" 2>/dev/null || true
}
JOURNAL_CONF_D=/etc/systemd/journald.conf.d
JOURNAL_DROPIN=$JOURNAL_CONF_D/teaport.conf
JOURNAL_DIR=/var/log/journal
JOURNAL_APPLIED="$STATE/journald.applied"
JOURNAL_MAX_USE=2G
LOGROTATE_BIN=/usr/sbin/logrotate
setup_logs() {
  local settings target want have current applied timer skip=0 dir_ok=1
  # the journal config is the whole host's: a dev machine running this off-Jetson
  # (TEAPORT_ALLOW_NON_JETSON) keeps its own; a dry run still shows what a box would get
  if [ -z "${L4T_RELEASE:-}" ] && [ "$DRY_RUN" != 1 ]; then
    log "logs: not a Jetson — this machine's journald and logrotate setup are left as they are"
    return 0
  fi
  log "logs: the journal is the one store: persistent, capped at $JOURNAL_MAX_USE, no copy to /var/log/syslog; logrotate for the rest of /var/log"
  settings="$(printf '%s\n' "Storage=persistent" "SystemMaxUse=$JOURNAL_MAX_USE" "ForwardToSyslog=no")"
  target="$(printf '%s\n' "[Journal]" "$settings")"
  want="$(printf '%s\n' \
    "# teaport (install.sh setup_logs): the journal is the one log store. install.sh owns this" \
    "# file and rewrites it on every run. To change a setting, put it in a drop-in of your own" \
    "# that sorts after this one (zz-local.conf, say); if you set Storage there, set" \
    "# ForwardToSyslog too." \
    "$target")"
  have="$(cat "$JOURNAL_DROPIN" 2>/dev/null || true)"
  # what the file on disk sets: if it isn't $target, journald can't have these settings now
  # (a deleted or edited drop-in, then a reboot)
  current="$(journal_settings)"
  if [ "$want" != "$have" ]; then
    if ! { SUDO install -d -m 0755 "$JOURNAL_CONF_D" && write_root "$JOURNAL_DROPIN" "$want"; }; then
      warn "logs: could not write $JOURNAL_DROPIN — the journal stays as it was (logs are not capped)"
      # nothing to apply, unless the file there holds the settings journald lacks: read
      # again, since a write that failed half-way may have left it truncated
      current="$(journal_settings)"
      [ "$current" = "$target" ] || skip=1
    fi
  fi
  applied="$(cat "$JOURNAL_APPLIED" 2>/dev/null || true)"
  if [ "$skip" != 1 ] && [ "$settings" = "$applied" ] && [ "$current" = "$target" ]; then
    log "logs: journald has these settings already"
  elif [ "$skip" != 1 ]; then
    # $JOURNAL_DIR, made before journald needs it when it isn't there (setgid
    # systemd-journal), then systemd's own tmpfiles rules for it (its mode, owner and the
    # ACLs that let adm read it; they only adjust a directory that exists). Left to
    # journald, it would be root's alone until the next boot, and `teaport logs` as the
    # operator would lose lines.
    { { [ -d "$JOURNAL_DIR" ] || SUDO install -d -m 2755 -o root -g systemd-journal "$JOURNAL_DIR"; } &&
      SUDO systemd-tmpfiles --create --prefix "$JOURNAL_DIR"; } \
      || { warn "logs: could not set up $JOURNAL_DIR — it is set up at the next boot (or the next run)"; dir_ok=0; }
    # The restart applies the drop-in now (services' stdout streams survive it: journald
    # keeps them in systemd's fd store); the flush moves this boot's journal to disk. Neither
    # fails the install: both happen at the next boot, and the next run tries again.
    if SUDO systemctl restart systemd-journald; then
      SUDO journalctl --flush || warn "logs: journalctl --flush failed — this boot's journal moves at the next boot"
      # recorded only when all of it worked, so the next run retries what didn't
      if [ "$dir_ok" = 1 ]; then
        write_root "$JOURNAL_APPLIED" "$settings" \
          || warn "logs: could not record the journal settings in $JOURNAL_APPLIED — the next run restarts journald again"
      fi
    else
      warn "logs: systemd-journald did not restart — the new journal settings apply at the next boot (or the next run)"
    fi
  fi
  [ "$DRY_RUN" = 1 ] && return 0
  # The package enables and starts its timer; anything else is an operator's choice or a
  # box mid-setup: said, with what would fix it, not overridden.
  timer="$(systemctl is-enabled logrotate.timer 2>/dev/null || true)"
  case "$timer" in
    enabled|generated)
      systemctl is-active --quiet logrotate.timer \
        || warn "logs: logrotate.timer is not running — /var/log is not rotated until it is (systemctl start logrotate.timer)" ;;
    static) warn "logs: logrotate.timer is static — something else must start it at boot, or /var/log stops being rotated (systemctl status logrotate.timer)" ;;
    enabled-runtime|linked|linked-runtime|indirect|alias|transient)
      warn "logs: logrotate.timer is $timer — it does not start at boot, so /var/log may stop being rotated (systemctl enable logrotate.timer)" ;;
    masked|masked-runtime) warn "logs: logrotate.timer is $timer — /var/log is not rotated (systemctl unmask $([ "$timer" = masked-runtime ] && echo '--runtime ')logrotate.timer && systemctl enable --now logrotate.timer)" ;;
    disabled) warn "logs: logrotate.timer is disabled — /var/log is not rotated (systemctl enable --now logrotate.timer)" ;;
    ""|not-found) warn "logs: there is no logrotate.timer — /var/log is not rotated (apt-get install --reinstall logrotate)" ;;
    *) warn "logs: logrotate.timer is $timer — /var/log may not be rotated (systemctl status logrotate.timer)" ;;
  esac
}

# --- engine: staged, swapped in whole, one rollback away ---------------------------------
# The engine is a binary and three files of its own release (the manifest's g2p_dict note:
# "Must be the same engine release as the binary"): the G2P dict and POS tagger it reads
# from <exe_dir>/g2p/, and the THIRD_PARTY_NOTICES.txt for what it is built from. Its two
# models go with it here too: a release that changes one changes what the binary loads.
# phase_engine used to download each over the live one in turn, so a run that failed half
# way (the dict's download, the binary's ldd) left a box whose next engine start mixed two
# releases, and nothing kept the one before: every rollback copy on the box was made by
# hand (#158). Now:
#
#   $PREFIX/engine.staged/<file>    the downloads: only the files whose sha256 differs
#                                   from the live one's. Kept across runs, each beside a
#                                   <file>.sha256 naming the file it is a download of, so
#                                   an interrupted download resumes (models are GBs) and
#                                   one of another release is never resumed into this one.
#                                   Gone once they are live.
#   $PREFIX/engine.rollback/<n>-<when>-<release>/
#                                   the engine as it was before a run changed it: its
#                                   files, the models the run replaced, the unit it ran
#                                   under (teaport-engine.service) and its manifest entry
#                                   (engine.json). <n> counts up, so the newest is the one
#                                   with the highest <n> whatever the clock says (a box
#                                   with no RTC can boot in another year; <when> is UTC,
#                                   for people). The newest $ENGINE_KEEP are kept.
#   $PREFIX/engine.swapping         there only while the staged files are moved in
#                                   (engine_swap_recover).
#
# A staged file goes live by rename (engine_swap_in): an engine that is running keeps the
# file it has open, and each path names a whole file throughout. --rollback engine renames
# a kept engine back in (engine_restore) after keeping the live one the same way, so a
# second rollback undoes the first. A file the run is about to replace is kept by a hard
# link, free even for a model: the rename that follows leaves the copy the only name of
# the old file. Every other engine file is copied, never linked: a hand deploy that cp's a
# binary over bin/voxtral writes into its inode, and would rewrite a linked copy with it.
# A model the run does not replace is not kept (it is the same in both). So a copy made by
# a run that changed a model holds the old model (the Voxtral one is ~4.4 GB) until it is
# pruned. The models' tekken.json/params.json have no sha256 in the manifest; they are
# fetched in place on every run, as before, and are not kept.
# Swapping a release directory behind one symlink would make the swap one rename; it would
# move the engine out of bin/ (which uv shares) and THIRD_PARTY_NOTICES.txt out of
# $PREFIX, where the unit, doctor and the docs name them: a follow-up, not this.
ENGINE_STAGE="$PREFIX/engine.staged"
ENGINE_ROLLBACKS="$PREFIX/engine.rollback"
ENGINE_SWAP_MARK="$PREFIX/engine.swapping"
ENGINE_KEEP=2
ENGINE_FILES=(bin/voxtral bin/g2p/kokoro_g2p.v2.dict bin/g2p/kokoro_pos.bin THIRD_PARTY_NOTICES.txt)
ENGINE_MODELS=(models/voxtral/consolidated.safetensors models/kokoro/Kokoro_espeak_F16.gguf)
ENGINE_UNIT=/etc/systemd/system/teaport-engine.service   # where render_unit writes it
ENGINE_CHANGED=""   # the files this run staged (engine_fetch), as " <file> <file>..."
ENGINE_KEPT=""      # the rollback copy this run made (engine_keep_live)

# engine_fetch <url> <file> <sha256> — one engine file (a path under $PREFIX): left alone
# when the live one already matches <sha256>, else downloaded into $ENGINE_STAGE and
# added to ENGINE_CHANGED. download() resumes a partial one and skips a whole one.
engine_fetch() {
  local url="$1" rel="$2" sha="$3" dest="$ENGINE_STAGE/$2"
  if [ -f "$PREFIX/$rel" ] && echo "$sha  $PREFIX/$rel" | sha256sum -c --status 2>/dev/null; then
    log "have ${rel##*/} (sha ok)"
    if [ -e "$dest" ] || [ -e "$dest.part" ]; then run rm -f "$dest" "$dest.part" "$dest.sha256"; fi
    return 0
  fi
  run mkdir -p "$(dirname "$dest")"
  if [ "$(cat "$dest.sha256" 2>/dev/null || true)" != "$sha" ]; then
    if [ -e "$dest" ] || [ -e "$dest.part" ]; then run rm -f "$dest" "$dest.part"; fi
    if [ "$DRY_RUN" != 1 ]; then printf '%s\n' "$sha" > "$dest.sha256"; fi
  fi
  download "$url" "$dest" "$sha"
  ENGINE_CHANGED="$ENGINE_CHANGED $rel"
}

# engine_describe <binary> — what the installed manifest says <binary> is. Sets
# ENGINE_RELEASE, its release ("unknown" when the manifest names another binary: a hand
# deploy's, say), and ENGINE_ENTRY, the manifest's engine entry for it as JSON, kept with a
# rollback copy so that `teaport update` names the right release after a rollback.
ENGINE_RELEASE="" ENGINE_ENTRY=""
engine_entry_py() { python3 - "$@" <<'PY'
import json, sys
try:
    e = json.load(open(sys.argv[1])).get("engine") or {}
except (OSError, ValueError, AttributeError):
    e = {}
if not sys.argv[3] or (e.get("assets") or {}).get(sys.argv[2], {}).get("sha256") != sys.argv[3]:
    e = {"release": "unknown"}
print("%s\t%s" % (e.get("release") or "unknown", json.dumps({"engine": e})))
PY
}
engine_describe() {
  local sha out
  sha="$(sha256sum "$1" 2>/dev/null | cut -d' ' -f1)" || sha=""
  out="$(engine_entry_py "$PREFIX/manifest.json" "$ASSET_KEY" "$sha" 2>/dev/null)" || out=""
  ENGINE_RELEASE="${out%%$'\t'*}" ENGINE_ENTRY="${out#*$'\t'}"
  # one plain path component: it names the rollback copy's directory
  case "$ENGINE_RELEASE" in ''|*[!A-Za-z0-9._-]*) ENGINE_RELEASE=unknown ;; esac
  case "$out" in *$'\t'*) ;; *) ENGINE_ENTRY='{"engine": {"release": "unknown"}}' ;; esac
}

# engine_copies — the rollback copies, newest first, NUL-separated: the directories whose
# name starts with the six-digit count engine_keep_live gives them. Anything else in there
# is not this installer's, and is neither a target nor pruned.
engine_copies() {
  local d
  while IFS= read -r -d '' d; do
    case "${d##*/}" in [0-9][0-9][0-9][0-9][0-9][0-9]-*) printf '%s\0' "$d" ;; esac
  done < <(find "$ENGINE_ROLLBACKS" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null | sort -rz)
}

# engine_keep_live <files> — keep the live engine (ENGINE_FILES, the models among <files>,
# the unit it runs under, its manifest entry) as a new rollback copy, and set ENGINE_KEPT.
# <files> are the ones about to be replaced: linked, not copied (see above). Built under a
# .partial name and renamed once complete, so an interrupted copy is never taken for a
# rollback target. A copy that fails (a full disk) stops the run before anything moved.
engine_keep_live() {
  local replacing=" ${1:-} " rel snap fail d n=0
  ENGINE_KEPT=""
  if [ ! -f "$PREFIX/bin/voxtral" ]; then log "no live engine — nothing to keep for --rollback engine"; return 0; fi
  engine_describe "$PREFIX/bin/voxtral"
  while IFS= read -r -d '' d; do d="${d##*/}"; d="${d%%-*}"; if [ "$((10#$d))" -gt "$n" ]; then n="$((10#$d))"; fi; done < <(engine_copies)
  snap="$ENGINE_ROLLBACKS/$(printf '%06d' $((n + 1)))-$(date -u +%Y%m%dT%H%M%SZ)-$ENGINE_RELEASE"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] keep the live engine (%s) for --rollback engine: %s\n' "$ENGINE_RELEASE" "$snap"; return 0
  fi
  fail="could not keep the live engine for a rollback in $snap (disk full?) — nothing was swapped"
  log "keeping the live engine ($ENGINE_RELEASE) for --rollback engine: $snap"
  rm -rf "$snap.partial"
  mkdir -p "$snap.partial" || die "$fail"
  for rel in "${ENGINE_FILES[@]}" "${ENGINE_MODELS[@]}"; do
    [ -f "$PREFIX/$rel" ] || continue
    mkdir -p "$(dirname "$snap.partial/$rel")" || { rm -rf "$snap.partial"; die "$fail"; }
    if contains " $rel " "$replacing"; then
      ln "$PREFIX/$rel" "$snap.partial/$rel" 2>/dev/null || cp -p "$PREFIX/$rel" "$snap.partial/$rel" \
        || { rm -rf "$snap.partial"; die "$fail"; }
    elif contains " $rel " " ${ENGINE_FILES[*]} "; then
      cp -p "$PREFIX/$rel" "$snap.partial/$rel" || { rm -rf "$snap.partial"; die "$fail"; }
    fi
  done
  if [ -f "$ENGINE_UNIT" ]; then
    cp "$ENGINE_UNIT" "$snap.partial/teaport-engine.service" || { rm -rf "$snap.partial"; die "$fail"; }
  fi
  printf '%s\n' "$ENGINE_ENTRY" > "$snap.partial/engine.json" || { rm -rf "$snap.partial"; die "$fail"; }
  mv "$snap.partial" "$snap"
  ENGINE_KEPT="$snap"
}

# engine_swap_in — make the staged files live: keep the live engine first (engine_keep_live),
# then rename each staged file over its live one. Nothing staged: nothing changes.
# The renames are one per file, so the engine is whole only before and after them. A rename
# that fails, or a run stopped in between (Ctrl-C, a kill), puts the kept engine back at
# once (engine_swap_abort); for what nothing can catch (SIGKILL, a power cut), the marker
# stays, and the next run puts the kept engine back first (engine_swap_recover).
engine_swap_in() {
  local rel
  if [ -z "$ENGINE_CHANGED" ]; then
    log "engine files: the live ones are the manifest's"
    if [ -e "$ENGINE_STAGE" ]; then run rm -rf "$ENGINE_STAGE"; fi
    return 0
  fi
  engine_keep_live "$ENGINE_CHANGED"
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] move the new%s into %s\n' "$ENGINE_CHANGED" "$PREFIX"; return 0; fi
  printf '%s\n' "$ENGINE_KEPT" > "$ENGINE_SWAP_MARK" || die "cannot write $ENGINE_SWAP_MARK — nothing was swapped"
  trap 'engine_swap_abort "the run was stopped while it moved the engine files in"' INT TERM HUP
  for rel in $ENGINE_CHANGED; do
    { mkdir -p "$(dirname "$PREFIX/$rel")" && mv -f "$ENGINE_STAGE/$rel" "$PREFIX/$rel"; } \
      || engine_swap_abort "could not move $rel into place"
  done
  trap - INT TERM HUP
  rm -f "$ENGINE_SWAP_MARK"
  rm -rf "$ENGINE_STAGE"
  log "engine files live:$ENGINE_CHANGED"
  engine_prune
}

# engine_swap_abort <why> — a swap stopped half way: put the kept engine back, whole, and
# stop. With nothing kept (no engine before this run), say what to do instead.
engine_swap_abort() {
  trap - INT TERM HUP
  if [ -n "$ENGINE_KEPT" ] && [ -f "$ENGINE_KEPT/bin/voxtral" ]; then
    warn "$1 — putting the engine before this run back (${ENGINE_KEPT##*/})"
    engine_restore "$ENGINE_KEPT"
    rm -f "$ENGINE_SWAP_MARK"
    die "$1; the engine is back as it was before this run — run this again"
  fi
  die "$1; the engine files may be a mix of two releases — run this again to finish"
}

# engine_swap_recover — the marker of a swap that something uncatchable stopped (SIGKILL, a
# power cut): the live engine may be two releases at once, and starts that way. Put the
# engine kept before that swap back, whole, before this run looks at what is live; the
# run then stages and swaps as usual. Called by phase_engine and --rollback engine.
engine_swap_recover() {
  local kept
  [ -f "$ENGINE_SWAP_MARK" ] || return 0
  kept="$(cat "$ENGINE_SWAP_MARK" 2>/dev/null || true)"
  case "$kept" in "$ENGINE_ROLLBACKS"/*) ;; *) kept="" ;; esac   # only ever one of ours
  if [ "$DRY_RUN" = 1 ]; then
    warn "an engine swap was stopped half way ($ENGINE_SWAP_MARK): a real run puts ${kept:+${kept##*/}, }the engine before it, back first"
    return 0
  fi
  if [ -n "$kept" ] && [ -f "$kept/bin/voxtral" ]; then
    warn "an engine swap was stopped half way — putting the engine before it back first (${kept##*/})"
    engine_restore "$kept"
  else
    warn "an engine swap was stopped half way, and nothing was kept from before it: this run replaces what is live"
  fi
  rm -f "$ENGINE_SWAP_MARK"
}

# engine_restore <copy> — rename a rollback copy's files back over the live ones (its models
# too, when it kept any), put its unit back (systemd reads it at the caller's daemon-reload)
# and its manifest entry, and remove the copy: it is the live engine now. A file the copy
# lacks (one its release did not have, or a model it did not replace) stays as it is.
# A copy without its binary is no engine: refused, loudly, before anything moves.
engine_restore() {
  local snap="$1" rel models=""
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] move the engine in %s back into %s, with its unit and manifest entry\n' "$snap" "$PREFIX"; return 0
  fi
  [ -f "$snap/bin/voxtral" ] || die "cannot put the engine in $snap back: it is gone or has no bin/voxtral — the live engine files were left as they are"
  for rel in "${ENGINE_FILES[@]}" "${ENGINE_MODELS[@]}"; do
    [ -f "$snap/$rel" ] || continue
    # The copy keeps a file as a hard link (engine_keep_live): one a stopped swap never
    # replaced is still the live file, and GNU mv refuses to move a file onto itself.
    if [ "$snap/$rel" -ef "$PREFIX/$rel" ]; then rm -f "$snap/$rel"; continue; fi
    { mkdir -p "$(dirname "$PREFIX/$rel")" && mv -f "$snap/$rel" "$PREFIX/$rel"; } \
      || die "could not move $rel back from $snap — the engine files may be a mix of two releases (what is left to move back is in $snap)"
    if contains " $rel " " ${ENGINE_MODELS[*]} "; then models="$models ${rel##*/}"; fi
  done
  if [ -f "$snap/teaport-engine.service" ]; then write_root "$ENGINE_UNIT" "$(cat "$snap/teaport-engine.service")"; fi
  if [ -f "$snap/engine.json" ]; then manifest_take "$snap/engine.json" engine; fi
  rm -rf "$snap"
  log "engine files back from ${snap##*/}${models:+, with the model(s)$models}"
}

# engine_rollback_target — print the newest complete rollback copy; fail when there is none.
engine_rollback_target() {
  local d
  while IFS= read -r -d '' d; do
    case "$d" in *.partial) continue ;; esac
    if [ -f "$d/bin/voxtral" ]; then printf '%s\n' "$d"; return 0; fi
  done < <(engine_copies)
  return 1
}

# engine_prune — keep the newest $ENGINE_KEEP rollback copies and remove the rest, and any
# copy left incomplete (.partial, or without a binary). The live engine is not in there, so
# a count is all it takes; the copy this run made is never removed, whatever its place.
# Never fails the run. The hand-made bin/voxtral.rollback-* copies on some boxes are not
# this installer's, and stay.
engine_prune() {
  local d n=0
  while IFS= read -r -d '' d; do
    if [ -n "$ENGINE_KEPT" ] && [ "$d" -ef "$ENGINE_KEPT" ]; then n=$((n + 1)); continue; fi
    case "$d" in
      *.partial) ;;
      *) if [ -f "$d/bin/voxtral" ] && [ "$n" -lt "$ENGINE_KEEP" ]; then n=$((n + 1)); continue; fi ;;
    esac
    log "removing old engine rollback copy $d"
    run rm -rf "$d" 2>/dev/null || SUDO rm -rf "$d" || warn "could not remove $d — harmless; remove it by hand"
  done < <(engine_copies)
}

# manifest_take <from.json> <key...> — set those top-level keys of the installed manifest
# ($PREFIX/manifest.json, what `teaport update` compares) to <from.json>'s, and leave the
# rest as it is: --only engine installs the engine and checks the models, not the brain or
# the plugin a new manifest may also name; a rollback puts back one engine entry.
manifest_take_py() { python3 - "$@" <<'PY'
import json, os, sys
dest, src, keys = sys.argv[1], sys.argv[2], sys.argv[3:]
try:
    d = json.load(open(dest))
except (OSError, ValueError):
    d = {}
s = json.load(open(src))
for k in keys:
    if k in s:
        d[k] = s[k]
with open(dest + ".tmp", "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
os.replace(dest + ".tmp", dest)
PY
}
manifest_take() {
  local from="$1"; shift
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] record the new "%s" in %s, and leave the rest of it\n' "$*" "$PREFIX/manifest.json"; return 0; fi
  manifest_take_py "$PREFIX/manifest.json" "$from" "$@" \
    || warn "could not record $* in $PREFIX/manifest.json — teaport update may name the wrong release"
}

# Every url + sha256 entry read here (and eula_gate's) is listed in manifest/check.py's
# REQUIRED, which the publish workflow checks before a manifest goes up: add one there
# when adding one here. models.voxtral.aux is optional (a missing aux file is skipped).
phase_engine() {
  log "engine + models -> $PREFIX"
  SUDO mkdir -p "$PREFIX/bin/g2p" "$PREFIX/models/voxtral" "$PREFIX/models/kokoro"
  SUDO chown -R "$RUN_USER" "$PREFIX"
  # The engine's own files and its models are staged, not downloaded over the live ones
  # (engine_fetch), and go live together once all of them have verified (engine_swap_in,
  # below). A swap a power cut stopped is put right first (engine_swap_recover).
  engine_swap_recover
  ENGINE_CHANGED=""
  engine_fetch "$(mget engine.assets.$ASSET_KEY.url)" bin/voxtral "$(mget engine.assets.$ASSET_KEY.sha256)"
  # The binary that will run: the staged one when it is new, the live one when it matched.
  local bin="$PREFIX/bin/voxtral" kept=""
  if contains " bin/voxtral " "$ENGINE_CHANGED "; then
    bin="$ENGINE_STAGE/bin/voxtral"; kept="; nothing was swapped: the live engine is untouched"
  fi
  run chmod +x "$bin"
  # The preflight gate accepts R38 *and* R39 on the claim that this CUDA-13 binary resolves its
  # deps on both (see ASSET_KEY). Check the claim instead of trusting the label: an unresolved
  # .so is a crisp message here rather than a crash-looping unit after the ~15GB of models
  # below. phase_sysdeps already installed libopenblas0, so a miss now is a real ABI gap.
  if [ "$DRY_RUN" != 1 ]; then
    local unresolved; unresolved="$(ldd "$bin" 2>/dev/null | grep 'not found' || true)"
    if [ -n "$unresolved" ]; then
      die "engine binary has unresolved libraries on this L4T (R${L4T_RELEASE:-?}):
$unresolved
the engine ships for JetPack 7.2 (L4T R38/R39, CUDA 13) — report this with the lines above$kept"
    fi
  fi
  # The en-us G2P dict MUST land at bin/g2p/ — the engine probes
  # <exe_dir>/g2p/kokoro_g2p.v2.dict (v2 header; an older binary keeps reading its own kokoro_g2p.dict, so a binary-only rollback works) and, if absent, silently degrades to
  # espeak-only English: every function word gets its stressed citation form,
  # the duration predictor doubles it, and speech goes robotic with no error
  # anywhere (shipped exactly so, 2026-08-05). espeak-ng (phase_sysdeps) is
  # only the OOV + non-English fallback, NOT the en-us G2P.
  # The dict is GENERATED FROM the misaki en-US gold/silver lexicons (Apache-2.0,
  # hexgrad and the misaki contributors) by teagram-engine tools/g2p_dict_convert.py —
  # flattened and merged, not a file copied from misaki. Attribution for it and for the
  # Apache-2.0 model weights below lives in the engine's THIRD_PARTY_NOTICES.txt.
  local g2p_url g2p_sha
  g2p_url="$(mget engine.g2p_dict.url 2>/dev/null)" || g2p_url=""
  g2p_sha="$(mget engine.g2p_dict.sha256 2>/dev/null)" || g2p_sha=""
  if [ -z "$g2p_url" ] || [ -z "$g2p_sha" ]; then
    # Fail-fast on a real install: shipping without the dict reproduces the 2026-08-05
    # incident. A preview must not abort mid-plan though — an operator pointing --dry-run
    # at an older (pre-g2p) manifest gets the full plan plus this heads-up.
    local g2p_msg="manifest has no engine.g2p_dict (url+sha256) — a box installed without the dict speaks robotic en-us prosody; republish the manifest"
    if [ "$DRY_RUN" = 1 ]; then warn "$g2p_msg"; else die "$g2p_msg"; fi
  else
    engine_fetch "$g2p_url" bin/g2p/kokoro_g2p.v2.dict "$g2p_sha"
  fi
  # The part-of-speech tagger beside the dict (kokoro_pos.bin, distilled from spaCy
  # en_core_web_sm tags; sources/licenses in the engine's NOTICE). The engine probes
  # <exe_dir>/g2p/kokoro_pos.bin; without it the dict still works but every homograph
  # (read/lead/live/content/...) reads its default form, with one journal line saying so.
  # Fail-fast like the dict: that is a silent quality regression, not an error.
  local pos_url pos_sha
  pos_url="$(mget engine.pos_model.url 2>/dev/null)" || pos_url=""
  pos_sha="$(mget engine.pos_model.sha256 2>/dev/null)" || pos_sha=""
  if [ -z "$pos_url" ] || [ -z "$pos_sha" ]; then
    local pos_msg="manifest has no engine.pos_model (url+sha256) — a box installed without the POS tagger reads every homograph in its default form; republish the manifest"
    if [ "$DRY_RUN" = 1 ]; then warn "$pos_msg"; else die "$pos_msg"; fi
  else
    engine_fetch "$pos_url" bin/g2p/kokoro_pos.bin "$pos_sha"
  fi
  # THIRD_PARTY_NOTICES.txt — the MIT/BSD/Apache-2.0 attributions (misaki, Marlin, Voxtral,
  # Kokoro, civetweb, fvad, ggml, …) for the engine binary and the dict above, per the engine
  # repo's own check-third-party gate. Three docs (this repo's THIRD_PARTY_LICENSES.md, this
  # manifest's g2p_dict._note, teagram-engine's README) all say this file "ships with the
  # engine download" — nothing downloaded it until now (#50). Fail-fast like the dict: a box
  # installed without it is out of compliance with every license it names, silently.
  local notices_url notices_sha
  notices_url="$(mget engine.notices.url 2>/dev/null)" || notices_url=""
  notices_sha="$(mget engine.notices.sha256 2>/dev/null)" || notices_sha=""
  if [ -z "$notices_url" ] || [ -z "$notices_sha" ]; then
    local notices_msg="manifest has no engine.notices (url+sha256) — a box installed without THIRD_PARTY_NOTICES.txt has no attribution text for its third-party components; republish the manifest"
    if [ "$DRY_RUN" = 1 ]; then warn "$notices_msg"; else die "$notices_msg"; fi
  else
    engine_fetch "$notices_url" THIRD_PARTY_NOTICES.txt "$notices_sha"
  fi
  engine_fetch "$(mget models.voxtral.url)" models/voxtral/consolidated.safetensors "$(mget models.voxtral.sha256)"
  engine_fetch "$(mget models.kokoro.url)"  models/kokoro/Kokoro_espeak_F16.gguf     "$(mget models.kokoro.sha256)"
  # A full run makes them live now (its restart is phase_services'). --only engine does it
  # itself, right before its restart, so that it can check the result and put the previous
  # engine back, models included (engine_swap).
  if [ "$ONLY" != engine ]; then engine_swap_in; fi
  # tekken.json / params.json ride alongside the voxtral model (manifest models.voxtral.aux.*).
  for aux in tekken.json params.json; do
    local u; u="$(python3 -c "import json;print(json.load(open('$MF'))['models']['voxtral'].get('aux',{}).get('$aux',''))")"
    [ -n "$u" ] && [ "$u" != TODO ] && download "$u" "$PREFIX/models/voxtral/$aux" ""
  done
  # --only engine records what it installed once the engine has come up on it (main_engine_only).
  if [ "$ONLY" != engine ]; then
    run cp "$MF" "$PREFIX/manifest.json" 2>/dev/null || SUDO cp "$MF" "$PREFIX/manifest.json"
  fi
}

# sudo_ready <what> — sudo usable now, before anything moves: a password prompt in the
# middle of a swap would leave it half done for as long as it waits, or for good on
# Ctrl-C. (Why `sudo -n true` before `sudo -v`: see brain_swap.)
sudo_ready() {
  if [ "$(id -u)" != 0 ] && ! sudo -n true 2>/dev/null; then
    sudo -v || die "sudo is needed to $1 — nothing was swapped"
  fi
}

# engine_verify — did the engine come up, and STAY up? It must listen on its port (ENGINE_PORT
# in engine.env, read privileged when the plain read fails, as brain_verify reads brain.env):
# the signal phase_verify and `teaport doctor` go by, since the engine binds it once its
# models are loaded, about a minute after it starts. It must do so in the invocation it
# started in: a new InvocationID, or a unit that is no longer active, is a crash its
# Restart= would otherwise hide. Then it must stay up through a settle window. Last, this
# start's own journal must not say the G2P dict failed to load (phase_verify's check, here
# scoped to the one invocation): that engine speaks robotic en-us (2026-08-05). A missing
# POS tagger only reads homographs in their default form; it is said, and does not fail.
# Not a live turn: the engine serves one session at a time, and a real one may hold it.
engine_verify() {
  local port envtxt id now st i up=0 line sel
  envtxt="$(read_env_file "$ETC/engine.env" --lenient)"
  port="$(sed -n 's/^ENGINE_PORT=//p' <<<"$envtxt" | tail -1)"; port="${port//[\"\']/}"; port="${port:-$ENGINE_PORT}"
  id="$(systemctl show -p InvocationID --value teaport-engine.service 2>/dev/null || true)"
  # Up to three minutes: the load takes about one, longer from a cold page cache, and
  # rolling back an engine that was only slow would cost another load.
  for i in $(seq 1 60); do
    st="$(systemctl is-active teaport-engine.service 2>/dev/null || true)"
    now="$(systemctl show -p InvocationID --value teaport-engine.service 2>/dev/null || true)"
    if [ "$st" != active ]; then warn "teaport-engine is ${st:-not active} while it loads"; return 1; fi
    if [ "$now" != "$id" ]; then warn "teaport-engine restarted while it loaded (crash-looping)"; return 1; fi
    if contains ":$port " "$(ss -ltn 2>/dev/null || true)"; then up=1; break; fi
    sleep 3
  done
  [ "$up" = 1 ] || { warn "teaport-engine did not listen on :$port within 3 minutes"; return 1; }
  for i in $(seq 1 15); do
    sleep 1
    st="$(systemctl is-active teaport-engine.service 2>/dev/null || true)"
    now="$(systemctl show -p InvocationID --value teaport-engine.service 2>/dev/null || true)"
    if [ "$st" != active ]; then warn "teaport-engine is ${st:-not active} after it came up"; return 1; fi
    if [ "$now" != "$id" ]; then warn "teaport-engine restarted after it came up (crash-looping)"; return 1; fi
  done
  sel=(-u teaport-engine.service --since '-10 min')
  if [ -n "$id" ]; then sel=("_SYSTEMD_INVOCATION_ID=$id"); fi
  line="$(SUDO journalctl "${sel[@]}" 2>/dev/null | grep 'dict G2P' | tail -1 || true)"
  if contains "dict G2P unavailable" "$line"; then
    warn "the engine is running WITHOUT the en-us G2P dict (espeak-only fallback — robotic prosody): $line"; return 1
  fi
  line="$(SUDO journalctl "${sel[@]}" 2>/dev/null | grep 'POS tagger' | tail -1 || true)"
  if contains "POS tagger unavailable" "$line"; then
    warn "the engine is running WITHOUT the POS tagger (homographs read in their default form): $line"
  fi
  log "teaport-engine is listening on :$port and stayed up"
}

# engine_restart — restart the engine on what is in place now and verify it. Only the engine
# is asked; the brain goes down and comes back with it (its Requires= on the engine, the
# engine's Upholds= on it) and reconnects. A restart that FAILS is a failed verify,
# returned, not a `set -e` exit that would skip engine_swap's rollback (see brain_restart).
engine_restart() {
  log "restarting teaport-engine (no speech for the minute its models take to load: a Talk session or a phone call in progress loses the engine, and the brain reconnects)"
  if ! SUDO systemctl restart teaport-engine.service; then warn "systemctl restart teaport-engine failed"; return 1; fi
  engine_verify || return 1
  # The brain is not judged (this path deploys the engine, and putting the engine back is
  # no fix for a brain), but a run that ends with the brain down must say so.
  if systemctl is-enabled --quiet teaport-brain.service 2>/dev/null && ! systemctl is-active --quiet teaport-brain.service 2>/dev/null; then
    warn "teaport-brain is $(systemctl is-active teaport-brain.service 2>/dev/null || true) after the engine restart — see: journalctl -u teaport-brain -n 50"
  fi
}

# engine_dropins_note — a drop-in changes the unit just rendered, and the engine runs with
# it: say so. A hand deploy can leave one in /run (shipping engine 1.5 by hand meant
# removing one, #158), and an ExecStart= in it runs another binary than the one this run
# installed, which the check after the restart would then vouch for. Not removed: it is
# not this installer's, and it may be there on purpose.
engine_dropins_note() {
  local d; d="$(systemctl show -p DropInPaths --value teaport-engine.service 2>/dev/null || true)"
  if [ -n "$d" ]; then
    warn "teaport-engine.service has drop-ins, which change the unit just rendered: $d — remove any a hand deploy left (then sudo systemctl daemon-reload)"
  fi
}

# engine_swap — --only engine's way live, once phase_engine has staged what differs from the
# manifest and env_add_missing has filled in engine.env: keep the live engine and move the
# staged files in (engine_swap_in), render the unit from this checkout, restart the engine
# and verify it (engine_restart). An engine that does not come back gets what ran before
# put back, its files and its unit, and the run fails. When nothing changed and the engine
# is running, it is checked but not restarted: a restart costs a minute without speech.
engine_swap() {
  local before after why="" journal="journalctl -u teaport-engine -n 100"
  if [ "$DRY_RUN" = 1 ]; then
    engine_swap_in
    render_unit teaport-engine.service.in teaport-engine.service
    printf '  [dry-run] systemctl daemon-reload; restart teaport-engine.service if any of the above changed it (or it is not running), verify it listens on :%s and stays up, and put the previous engine back if it does not\n' "$ENGINE_PORT"
    return 0
  fi
  sudo_ready "update and restart the engine"
  before="$(cat "$ENGINE_UNIT" 2>/dev/null || true)"
  engine_swap_in
  render_unit teaport-engine.service.in teaport-engine.service
  after="$(cat "$ENGINE_UNIT" 2>/dev/null || true)"
  SUDO systemctl daemon-reload
  engine_dropins_note
  if [ -n "$ENGINE_CHANGED" ]; then why="new engine files"; fi
  if [ "$before" != "$after" ]; then why="${why:+$why, }a new unit"; fi
  if [ "$ENV_ADDED" != 0 ]; then why="${why:+$why, }new engine.env keys"; fi
  if [ -z "$why" ] && systemctl is-active --quiet teaport-engine.service; then
    log "the engine's files, unit and engine.env have not changed — checking it, not restarting it"
    engine_verify || die "the engine is not healthy, and this run changed nothing it could put back — see: $journal"
    return 0
  fi
  log "teaport-engine: ${why:-it is not running}"
  if engine_restart; then return 0; fi
  # Put back all this run changed: the engine's files and models with its unit (the kept
  # copy), or the unit alone; and the keys it added to engine.env.
  local back=""
  if [ -n "$ENGINE_KEPT" ]; then
    engine_restore "$ENGINE_KEPT"; back="the engine files, models and unit (${ENGINE_KEPT##*/})"
  elif [ -n "$before" ] && [ "$before" != "$after" ]; then
    write_root "$ENGINE_UNIT" "$before"; back="the unit"
  fi
  if [ "$ENV_ADDED" != 0 ]; then env_put_back; back="${back:+$back, and }engine.env"; fi
  [ -n "$back" ] || die "the engine did not come back, and this run changed nothing it could put back — see: $journal"
  warn "the engine did not come back — put back as they were before this run: $back"
  SUDO systemctl daemon-reload
  if engine_restart; then
    die "rolled back: the engine is running on what it had before this run. The failed start is in: $journal"
  fi
  die "rolled back, but the engine is not healthy there either — see: $journal"
}

# check_brain_source — the checks that can run before anything is touched, on the checkout
# this installer runs from (it holds the unit templates, the udev rule and the installer
# itself; the brain is the .deb, not the checkout).
#
# Freshness: a checkout installs whatever it holds, so one that is behind its upstream
# installs OLDER units and rules than the branch it tracks -- an update that silently
# reverts merged work, with nothing failing and nothing logged. Operators' own checkouts
# do fall behind, so refuse it. A checkout with no upstream (CI, a detached tag) has
# nothing to be behind.
#
# git here only READS the checkout, with one exception: a fetch, so "behind" is judged
# against the remote as it is now. The fetch is skipped under root (`sudo ./install.sh`):
# it would leave root-owned objects and refs in the operator's .git, and root usually
# has no key for the remote anyway -- that run is judged against the last fetch, and says
# so. --no-optional-locks keeps `status` from rewriting .git/index (git's own flag for
# read-only callers like this one).
#
# What --only brain installs from the checkout, and so all its freshness check looks at:
# the units it renders (systemd), the busy lamp's udev rule (install_busy_lamp), the
# resolver's serve-stale drop-in (install_dns_resilience) -- and install.sh itself, which
# holds how the package is installed and verified, the ARP sysctl drop-in, and
# sip_brain_detach's and migrate_brain_dropins' unit edits. The price: a commit that
# touches install.sh for the engine or SIP alone also asks for a git pull first.
# Keep in step with those functions (test_install_brain_only.py checks the paths).
BRAIN_ONLY_PATHS=(systemd packaging/xvf3800 packaging/resolved install.sh)
# The same for --only engine: the engine's unit template (render_unit) and install.sh, which
# holds how the engine is installed and the engine.env keys it adds. The engine itself
# comes from the manifest, not the checkout. (test_install_engine_only.py checks the paths.)
ENGINE_ONLY_PATHS=(systemd/teaport-engine.service.in install.sh)
# brain_src_fetch -- fetch the checkout's upstream without ever stopping at a prompt:
# GIT_TERMINAL_PROMPT for https, ssh BatchMode for ssh, and a timeout for anything else.
# BatchMode only when neither the environment nor the repo names its own ssh command --
# overriding a deploy key's core.sshCommand would make every fetch fail.
brain_src_fetch() {
  local ssh=()
  if [ -z "${GIT_SSH_COMMAND:-}" ] && [ -z "$(git -C "$HERE" config core.sshCommand 2>/dev/null)" ]; then
    ssh=(GIT_SSH_COMMAND="ssh -o BatchMode=yes")
  fi
  env GIT_TERMINAL_PROMPT=0 "${ssh[@]}" timeout 30 git -C "$HERE" fetch --quiet 2>/dev/null
}
check_brain_source() {
  have git && [ -e "$HERE/.git" ] || return 0
  local up behind
  # A checkout git refuses to read (a "dubious ownership" refusal, say, under `su -`) must
  # say so: returning quietly would skip the freshness check with nothing on screen.
  if ! git -C "$HERE" rev-parse --git-dir >/dev/null 2>&1; then
    warn "git cannot read $HERE as this user — its freshness is NOT checked: $(git -C "$HERE" rev-parse --git-dir 2>&1 | head -1)"
    return 0
  fi
  up="$(git -C "$HERE" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)" || return 0
  # --dry-run touches nothing, .git included: judge against the last fetch.
  if [ "$DRY_RUN" = 1 ]; then :
  elif [ "$(id -u)" = 0 ]; then warn "running as root: not fetching $up into $HERE — checking against its last fetch"
  elif ! brain_src_fetch; then
    warn "cannot fetch $up — checking this checkout against the last fetch instead"
  fi
  # A full install consumes the whole checkout, so any upstream commit counts. --only brain
  # consumes only BRAIN_ONLY_PATHS (--only engine, ENGINE_ONLY_PATHS), so upstream commits
  # that touch none of them (the engine's, the plugin's, docs) do not make it stale and are
  # not refused.
  local scope=() what="" paths=("${BRAIN_ONLY_PATHS[@]}")
  if [ "$ONLY" = engine ]; then paths=("${ENGINE_ONLY_PATHS[@]}"); fi
  if [ -n "$ONLY" ]; then scope=(-- "${paths[@]}"); what=" in what --only $ONLY installs (${paths[*]})"; fi
  behind="$(git -C "$HERE" rev-list --count "HEAD..$up" "${scope[@]}" 2>/dev/null)" || behind=0
  if [ "$behind" -gt 0 ]; then
    local msg="this checkout ($HERE) is $behind commit(s) behind $up$what — it would install older code than $up holds. git pull first"
    if [ "$ALLOW_STALE_SOURCE" = 1 ]; then warn "$msg (installing anyway: TEAPORT_ALLOW_STALE_SOURCE=1)"
    elif [ "$DRY_RUN" = 1 ]; then warn "$msg (a real run stops here)"
    else die "$msg, or set TEAPORT_ALLOW_STALE_SOURCE=1 to install it anyway"; fi
  fi
  local p dirty=""
  for p in "${paths[@]}"; do
    if [ -n "$(git -C "$HERE" --no-optional-locks status --porcelain -- "$p" 2>/dev/null)" ]; then dirty="$dirty $p"; fi
  done
  if [ -n "$dirty" ]; then warn "uncommitted changes in $HERE are used too:$dirty"; fi
}

# brain_units — which units a --only brain swap (or --rollback brain) restarts. Sets
# SWAP_JUDGE, the brain processes whose health decides the swap, and SWAP_ALSO, units
# restarted with them but never judged. Only brain processes are judged: this path
# deploys the brain, and a failure anywhere else is not something swapping the brain
# back can fix — blaming it on the brain rolled good packages back.
#   teaport-brain: when it is active OR enabled. Enabled catches a brain crash-looping at
#     update time ("activating (auto-restart)", not active) — the run most likely to be
#     fixing it. Active catches one running although disabled (teaport-engine Upholds= it).
#     It is the ONE brain unit: Talk and the phone line's SIP front-end both run in it.
#   The SIP gateway (teaport-sip) is not restarted: the brain reconnects to it, and it
#   replays a call in progress to the restarted brain (teaport-sip#3).
SWAP_JUDGE=""; SWAP_ALSO=""
brain_units() {
  SWAP_JUDGE=""; SWAP_ALSO=""
  if systemctl is-active --quiet teaport-brain.service 2>/dev/null \
     || systemctl is-enabled --quiet teaport-brain.service 2>/dev/null; then
    SWAP_JUDGE="teaport-brain.service"
  fi
  # The local audio bridge runs brain code from the venv too, but it is a /talk client, not
  # a brain: restarted onto the new venv, never judged (a missing card is not a bad brain).
  if systemctl is-active --quiet teaport-local-audio.service 2>/dev/null; then
    SWAP_ALSO="${SWAP_ALSO:+$SWAP_ALSO }teaport-local-audio.service"
  fi
}

# brain_verify <units...> — did the units just restarted on the swapped-in venv come up
# and STAY up? teaport-brain must answer /health, which it only does once the app has
# imported everything and bound its port; then every unit must stay active, in the SAME
# invocation, through a settle window — a crash a few seconds in (the SIP front-end
# starts once the port is bound) shows as a restart, not as a failed start.
#
# The invocation, not NRestarts: systemd resets NRestarts on every start it did not make
# itself (teaport-engine's Upholds= makes such starts). InvocationID changes on every
# start however it came about, and the window is polled throughout. A crash inside it
# shows either as a new InvocationID or as a unit that is not active; the window
# outlasts the few seconds an import failure takes to kill the process.
#
# Not a live turn: the brain holds one conversation at a time, so a turn driven from
# here would be refused while a real one is live, or hold the box against one.
# The pipeline's in-process models were exercised by the package's self-check in CI.
FAILED_UNIT=""
brain_verify() {
  local u i n port now up=0 ids=()
  FAILED_UNIT=teaport-brain.service
  # brain.env can be root-only (a first install under plain sudo — see read_env_file). A
  # port read that fell back to the default would poll the wrong port and roll back a
  # healthy brain, so it is read privileged when the plain read fails.
  local envtxt=""
  envtxt="$(read_env_file "$ETC/brain.env" --lenient)"
  port="$(sed -n 's/^BRAIN_PORT=//p' <<<"$envtxt" | tail -1)"; port="${port//[\"\']/}"; port="${port:-$BRAIN_PORT}"
  if contains teaport-brain.service "$*"; then
    local deadline=$((SECONDS + 90))
    while [ "$SECONDS" -lt "$deadline" ]; do
      if curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$port/health" 2>/dev/null; then up=1; break; fi
      sleep 2
    done
    [ "$up" = 1 ] || { warn "teaport-brain did not answer http://127.0.0.1:$port/health within 90 s"; return 1; }
  fi
  for u in "$@"; do ids+=("$(systemctl show -p InvocationID --value "$u" 2>/dev/null || true)"); done
  for i in $(seq 1 15); do
    sleep 1; n=0
    for u in "$@"; do
      FAILED_UNIT="$u"
      if ! systemctl is-active --quiet "$u"; then warn "$u is $(systemctl is-active "$u" 2>/dev/null || true) after the swap"; return 1; fi
      now="$(systemctl show -p InvocationID --value "$u" 2>/dev/null || true)"
      if [ "$now" != "${ids[$n]}" ]; then warn "$u restarted after the swap (crash-looping)"; return 1; fi
      n=$((n + 1))
    done
  done
}

# brain_restart <extra> <units...> — restart <units> (and <extra>, a word list of units to
# restart alongside but not judge, or "") and verify <units>. A restart that FAILS is a
# failed verify, returned — not a `set -e` exit that would skip brain_swap's rollback and
# leave the link on a venv nothing has vouched for (a dependency job failing or the start
# rate limit make systemctl return non-zero).
brain_restart() {
  local extra="$1" u; shift
  log "restarting $extra${extra:+ }$* (a live Talk session is dropped; a phone call is held by the gateway and resumed)"
  # shellcheck disable=SC2086  # $extra is a word list of unit names
  if ! SUDO systemctl restart $extra "$@"; then
    warn "systemctl restart failed"
    FAILED_UNIT="$1"
    for u in "$@"; do if ! systemctl is-active --quiet "$u"; then FAILED_UNIT="$u"; break; fi; done
    return 1
  fi
  brain_verify "$@"
}

# --- the brain: the teaport-brain .deb (#60) -----------------------------------------
# The brain is a package, built in CI from brain/uv.lock against the system python3.12
# (packaging/build-deb.sh) and installed here with apt: /opt/teaport/brain (the venv),
# /usr/bin/teaport, /usr/lib/teaport/{config_apply.py,sudoers-sync}. Nothing is built on
# the box. Where the .deb comes from:
#   TEAPORT_BRAIN_DEB=<file>   a package you have (CI's brain-deb artifact, or one built
#                              with packaging/build-deb.sh on a build host)
#   the manifest's brain.deb   {url, sha256}: downloaded and verified
# Installing it is the whole update: its postinst writes the config page's sudoers line,
# links the old /opt/teaport/venv path, and restarts the brain's units; teaport-brain is
# Type=notify, so a brain that never becomes ready fails the install.
#
# Every package that is installed is kept in $BRAIN_DEBS (the newest $BRAIN_DEB_KEEP, and
# the installed one): the way back is `apt-get install --allow-downgrades` of the one
# before it, which is what --rollback brain and a failed --only brain do. The first
# release of the package is the oldest a box can go back to: a box that still has the
# pre-package venv has no earlier package to return to.
BRAIN_PKG=teaport-brain
BRAIN_DEBS=/var/cache/teaport
BRAIN_DEB_KEEP=3
BRAIN_DEB=""          # the file to install (brain_deb_fetch)
BRAIN_DEB_NEW=""      # its version
BRAIN_DEB_CACHED=""   # its copy in $BRAIN_DEBS (brain_deb_keep)
BRAIN_DEB_SHA=""      # its sha256 when the manifest names it (the root copy is checked against it)
BRAIN_DEB_DOWNLOADED=0  # 1 when BRAIN_DEB is a download of ours: removed once kept
BRAIN_DEB_ADDED=0     # 1 when this run put BRAIN_DEB_CACHED in the cache (it was not there before)
SUDOERS_FILE=/etc/sudoers.d/teaport-config   # the config page's line (packaging/deb/sudoers-sync)
# Where the pre-package installer put the CLI and the config helper (retired once the
# package's are in place: /usr/local/bin precedes /usr/bin on PATH).
OLD_TOOLS_DIR=/usr/local/lib/teaport
OLD_TOOLS_CLI=/usr/local/bin/teaport

brain_deb_arch() { dpkg --print-architecture; }
# The installed package's version; empty when it is not installed (or half-configured).
brain_installed_version() {
  { dpkg-query -W -f='${db:Status-Status} ${Version}\n' "$BRAIN_PKG" 2>/dev/null || true; } | awk '$1=="installed"{print $2; exit}'
}
# The package's version in any state that leaves files on the box, a half-configured one
# included (a postinst that failed): what --rollback and the cache pruning must not treat as
# gone. brain_installed_version is only a package that is fully there.
brain_package_version() {
  { dpkg-query -W -f='${db:Status-Status} ${Version}\n' "$BRAIN_PKG" 2>/dev/null || true; } \
    | awk '$1=="installed"||$1=="half-configured"||$1=="unpacked"||$1=="half-installed"{print $2; exit}'
}
brain_deb_path() { printf '%s/%s_%s_%s.deb\n' "$BRAIN_DEBS" "$BRAIN_PKG" "$1" "$(brain_deb_arch)"; }

# brain_deb_fetch -- choose the package to install and check it is one: BRAIN_DEB, with
# BRAIN_DEB_NEW its version.
brain_deb_fetch() {
  [ "$PREFIX" = /opt/teaport ] || die "the brain package installs to /opt/teaport; TEAPORT_PREFIX=$PREFIX cannot hold it"
  if [ -n "$BRAIN_DEB_SRC" ]; then
    [ -f "$BRAIN_DEB_SRC" ] || die "TEAPORT_BRAIN_DEB=$BRAIN_DEB_SRC is not a file"
    BRAIN_DEB="$BRAIN_DEB_SRC"
  else
    [ -n "$MF" ] || fetch_manifest
    local url sha
    url="$(mget brain.deb.url 2>/dev/null || true)"; sha="$(mget brain.deb.sha256 2>/dev/null || true)"
    if [ -z "$url" ] || [ -z "$sha" ]; then
      if [ "$DRY_RUN" = 1 ]; then
        warn "the release manifest names no brain package yet (brain.deb) — a real run stops here unless TEAPORT_BRAIN_DEB names one"
        BRAIN_DEB="" BRAIN_DEB_NEW="(none named)"; return 0
      fi
      die "the release manifest names no brain package (brain.deb.url, brain.deb.sha256). Take one from CI (the brain-deb artifact) or build it with packaging/build-deb.sh on a build host, then: TEAPORT_BRAIN_DEB=/path/to/teaport-brain_<version>_$(brain_deb_arch).deb ./install.sh"
    fi
    SUDO mkdir -p "$STATE"; SUDO chown "$RUN_USER" "$STATE"
    run mkdir -p "$STATE/brain-deb"
    BRAIN_DEB="$STATE/brain-deb/$(basename "$url")"
    BRAIN_DEB_SHA="$(printf '%s' "$sha" | tr 'A-F' 'a-f')" BRAIN_DEB_DOWNLOADED=1
    download "$url" "$BRAIN_DEB" "$sha"
  fi
  if [ ! -f "$BRAIN_DEB" ]; then   # a dry run that did not download
    BRAIN_DEB_NEW="(not downloaded in a dry run)"; return 0
  fi
  brain_deb_verify "$BRAIN_DEB"
  BRAIN_DEB_NEW="$(dpkg-deb -f "$BRAIN_DEB" Version)"
}

# brain_deb_verify <file> -- dies unless <file> is a $BRAIN_PKG package for this machine.
brain_deb_verify() {
  [ "$(dpkg-deb -f "$1" Package 2>/dev/null)" = "$BRAIN_PKG" ] || die "$1 is not a $BRAIN_PKG package"
  [ "$(dpkg-deb -f "$1" Architecture)" = "$(brain_deb_arch)" ] || die "$1 is for $(dpkg-deb -f "$1" Architecture), this machine is $(brain_deb_arch)"
}

# brain_deb_keep -- BRAIN_DEB into $BRAIN_DEBS under its canonical name; sets
# BRAIN_DEB_CACHED. The download directory belongs to the run user (the brain, and what
# its agent runs), so the file apt installs as root is a root-owned COPY, and it is the
# copy that is checked: its sha256 against the manifest's, its package fields against the
# ones read from the original. Nothing the run user does to the original afterwards reaches
# the install.
brain_deb_keep() {
  BRAIN_DEB_CACHED="$(brain_deb_path "$BRAIN_DEB_NEW")"
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] keep %s as %s\n' "$BRAIN_DEB" "$BRAIN_DEB_CACHED"; return 0; fi
  SUDO install -d -m 0755 -o root -g root "$BRAIN_DEBS"
  if ! [ "$BRAIN_DEB" -ef "$BRAIN_DEB_CACHED" ]; then
    [ -e "$BRAIN_DEB_CACHED" ] || BRAIN_DEB_ADDED=1
    local tmp="$BRAIN_DEBS/incoming.partial" ok=1
    SUDO install -m 0644 -o root -g root "$BRAIN_DEB" "$tmp"
    if [ -n "$BRAIN_DEB_SHA" ] && [ "$(sha256sum "$tmp" | awk '{print $1}')" != "$BRAIN_DEB_SHA" ]; then
      warn "the copy of $BRAIN_DEB changed after it was checked (sha256 differs from the manifest's)"; ok=0
    fi
    if [ "$ok" = 1 ] && ! ( brain_deb_verify "$tmp" && [ "$(dpkg-deb -f "$tmp" Version)" = "$BRAIN_DEB_NEW" ] ); then ok=0; fi
    if [ "$ok" != 1 ]; then SUDO rm -f "$tmp"; die "$BRAIN_DEB did not check out when it was copied to $BRAIN_DEBS — nothing was installed"; fi
    SUDO mv -f "$tmp" "$BRAIN_DEB_CACHED"
  fi
  if [ "$BRAIN_DEB_DOWNLOADED" = 1 ]; then rm -f "$BRAIN_DEB" || true; fi
}

# brain_deb_apply <file> -- apt installs <file>, over any version (--allow-downgrades: a
# rollback; --reinstall: a repair puts the files back). Its postinst restarts the units;
# a failure there is a failure here.
brain_deb_apply() {
  SUDO env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y --reinstall --allow-downgrades "$1"
}

# brain_deb_prune -- keep the newest $BRAIN_DEB_KEEP packages, and the installed one
# whatever its age. Never fails the run.
brain_deb_prune() {
  [ "$DRY_RUN" = 1 ] && return 0
  local cur f n=0; cur="$(brain_package_version)"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    n=$((n + 1))
    if [ -n "$cur" ] && [ "$f" = "$(brain_deb_path "$cur")" ]; then continue; fi
    if [ "$n" -gt "$BRAIN_DEB_KEEP" ]; then SUDO rm -f "$f" || true; fi
  done < <(ls -1t "$BRAIN_DEBS/${BRAIN_PKG}"_*.deb 2>/dev/null || true)
}

# brain_sudoers -- the config page's sudoers line, for $RUN_USER. The package's postinst
# writes it too, from the brain unit's User=; this is for a first install, where the unit
# did not exist yet when the package was configured. One writer: the package's own script.
brain_sudoers() {
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] /usr/lib/teaport/sudoers-sync %s\n' "$RUN_USER"; return 0; fi
  SUDO /usr/lib/teaport/sudoers-sync "$RUN_USER" || warn "the config page's sudoers line was not written (/usr/lib/teaport/sudoers-sync failed)"
}

# brain_retire_old_tools -- the pre-package installer's CLI and config helper, once the
# package's are in place and the brain has come back on them: its releases, `current`,
# the helper link and the CLI link/file. /usr/local/lib/teaport itself stays (the wifi
# setup keeps its own files there).
brain_retire_old_tools() {
  local f found=0 target
  for f in "$OLD_TOOLS_CLI" "$OLD_TOOLS_DIR/config_apply.py" "$OLD_TOOLS_DIR/current" "$OLD_TOOLS_DIR/releases"; do
    if [ -e "$f" ] || [ -L "$f" ]; then found=1; fi
  done
  [ "$found" = 1 ] || return 0
  if [ "${DRY_RUN:-0}" = 1 ]; then
    printf '  [dry-run] retire the pre-package CLI and config helper (%s, %s/{releases,current,config_apply.py}) once the package'"'"'s are in place and the brain is back\n' "$OLD_TOOLS_CLI" "$OLD_TOOLS_DIR"
    return 0
  fi
  [ -x /usr/bin/teaport ] && [ -f /usr/lib/teaport/config_apply.py ] || { warn "the package's CLI and config helper are not in place — the old ones at $OLD_TOOLS_DIR are left"; return 0; }
  # The config page's sudoers line must name the package's helper first: with the old
  # helper gone and the line still naming it, saving a setting would fail.
  SUDO grep -qs /usr/lib/teaport/config_apply.py "$SUDOERS_FILE" || { warn "$SUDOERS_FILE does not name the package's config helper — the old ones at $OLD_TOOLS_DIR are left"; return 0; }
  log "retiring the pre-package CLI and config helper ($OLD_TOOLS_CLI, $OLD_TOOLS_DIR/{releases,current,config_apply.py}); /usr/bin/teaport is the package's"
  SUDO rm -rf "$OLD_TOOLS_DIR/config_apply.py" "$OLD_TOOLS_DIR/current" "$OLD_TOOLS_DIR/releases"
  # The old CLI was a symlink into those releases. Anything else at that path is the
  # operator's own (a wrapper, a dev link) and stays.
  if [ -L "$OLD_TOOLS_CLI" ]; then
    target="$(readlink "$OLD_TOOLS_CLI" || true)"
    case "$target" in "$OLD_TOOLS_DIR"/*) SUDO rm -f "$OLD_TOOLS_CLI" ;; *) warn "$OLD_TOOLS_CLI is not the pre-package CLI's link ($target) — left" ;; esac
  elif [ -e "$OLD_TOOLS_CLI" ]; then
    warn "$OLD_TOOLS_CLI is not the pre-package CLI's link — left"
  fi
}

# brain_retire_old_venvs -- the pre-package brain venvs (each a few GB), once the package's
# brain has come back: $PREFIX/venvs/*, the rollback link venv.previous and the pip-era
# venv.pre-uv. A rollback now means a cached package, so nothing returns to them. "Come
# back" is checked, not assumed: a brain the operator stopped (brain_swap restarts nothing
# then) has not shown that the package runs, and on a first migration these venvs are the
# only way back. $PREFIX/venv itself stays: the package's link for a path older units and
# scripts name.
brain_retire_old_venvs() {
  local f found=0
  for f in "$PREFIX/venvs" "$PREFIX/venv.previous" "$PREFIX/venv.pre-uv"; do
    if [ -e "$f" ] || [ -L "$f" ]; then found=1; fi
  done
  if [ -d "$PREFIX/venv" ] && [ ! -L "$PREFIX/venv" ]; then
    warn "$PREFIX/venv is a directory, not the package's link — an old venv; if nothing uses it, remove it (sudo rm -rf $PREFIX/venv) and the package's next install puts the link there"
  fi
  [ "$found" = 1 ] || return 0
  if [ "${DRY_RUN:-0}" = 1 ]; then
    printf '  [dry-run] remove the pre-package brain venvs (%s/{venvs,venv.previous,venv.pre-uv}) once the package'"'"'s brain is running\n' "$PREFIX"
    return 0
  fi
  [ -x "$PREFIX/brain/bin/teaport-brain" ] || { warn "the package's brain is not in place — the old venvs under $PREFIX are left"; return 0; }
  systemctl is-active --quiet teaport-brain.service \
    || { warn "teaport-brain is not running, so the package's brain has not shown it works — the old venvs under $PREFIX are left; run the installer again once it is up"; return 0; }
  log "removing the pre-package brain venvs ($PREFIX/{venvs,venv.previous,venv.pre-uv}); the package's brain is $PREFIX/brain"
  SUDO rm -rf "$PREFIX/venvs" "$PREFIX/venv.previous" "$PREFIX/venv.pre-uv"
}

# brain_render_units -- the units that run brain code, rendered from this checkout: a
# package install changes what they name (the venv's path) and how the brain unit waits
# (Type=notify). The audio bridge and the wifi setup only when the box has them.
brain_render_units() {
  render_unit teaport-brain.service.in teaport-brain.service
  [ ! -e /etc/systemd/system/teaport-local-audio.service ] || render_unit teaport-local-audio.service.in teaport-local-audio.service
  [ ! -e /etc/systemd/system/teaport-wifi-setup.service ] || render_unit teaport-wifi-setup.service.in teaport-wifi-setup.service
  SUDO systemctl daemon-reload
}

# brain_swap <deb> <mode> -- install <deb> over the brain, render the units, restart and
# verify them (brain_units, brain_verify). <mode> --auto-rollback: brain units that do not
# come back get the package that was installed before back (same restart, same checks) and
# the run fails; "" = just fail. Used by --only brain and --rollback brain; a full install
# installs the package in phase_brain and leaves the restart to phase_services.
brain_swap() {
  local deb="$1" auto="$2" prev prevdeb="" journal oldvenv=""
  prev="$(brain_installed_version)"
  if [ -n "$prev" ] && [ -f "$(brain_deb_path "$prev")" ]; then prevdeb="$(brain_deb_path "$prev")"; fi
  # A box still on the pre-package venv: where its link points, before the package's
  # postinst repoints it (named if the first package does not come up).
  if [ -z "$prev" ] && [ -L "$PREFIX/venv" ]; then oldvenv="$(readlink "$PREFIX/venv" || true)"; fi
  brain_units
  # shellcheck disable=SC2086  # word lists of unit names
  set -- $SWAP_JUDGE
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] apt-get install %s (%s, was %s); render the brain units; restart %s, verify %s%s\n' \
      "$deb" "$BRAIN_DEB_NEW" "${prev:-no package}" "$SWAP_ALSO${SWAP_ALSO:+ }${*:-nothing}" "${*:-nothing}" \
      "${auto:+ (back to ${prevdeb:-the previous package -- none is kept} if they do not come up)}"
    return 0
  fi
  # sudo ready BEFORE anything changes: a password prompt between the install and the
  # restart would keep the old units running on files that are already the new ones for as
  # long as it waits -- or for good, if it is answered with Ctrl-C. `sudo -n true` first:
  # `sudo -v` wants a password unless EVERY sudoers rule for the user is NOPASSWD, so on a
  # box with a NOPASSWD rule beside a password one -- the appliance's own -- it would
  # prompt although the restart itself never would.
  sudo_ready "install the brain package and restart its units"
  log "teaport-brain ${prev:-(none)} -> $BRAIN_DEB_NEW"
  if brain_deb_apply "$deb" && brain_render_units; then
    brain_sudoers
    brain_units
    # shellcheck disable=SC2086
    set -- $SWAP_JUDGE
    if [ $# = 0 ]; then return 0; fi   # no brain unit to restart: the operator starts it
    if brain_restart "$SWAP_ALSO" "$@"; then log "brain units back up on $BRAIN_DEB_NEW: $*"; return 0; fi
  else
    warn "installing or configuring $deb failed (the package's postinst restarts the brain, and a brain that does not become ready fails it)"
    FAILED_UNIT=teaport-brain.service
    # apt that never got as far as the package (dpkg's lock held by an unattended upgrade,
    # a broken index): the installed package is the one that was there, so nothing changed
    # and there is nothing to put back. Restarting a healthy brain would only end a Talk
    # session, and the new package is not at fault.
    if [ -n "$prev" ] && [ "$(brain_installed_version)" = "$prev" ]; then
      # Out of the cache only if this run put it there: a package that was kept before
      # (a rollback target being installed again) stays.
      if [ "$BRAIN_DEB_ADDED" = 1 ] && [ "$deb" != "$prevdeb" ]; then SUDO rm -f "$deb" || true; fi
      die "apt could not install $BRAIN_DEB_NEW and left the box as it was: the brain still runs $prev, and nothing was restarted"
    fi
  fi
  journal="journalctl -u ${FAILED_UNIT%.service} -n 50"
  if [ "$auto" != --auto-rollback ]; then
    die "the brain did not come back on $BRAIN_DEB_NEW — see: $journal"
  fi
  if [ -z "$prevdeb" ]; then
    local back=""
    if [ -n "$oldvenv" ]; then
      back=". The pre-package venv ($oldvenv) is still on the box; to run on it again: sudo ln -sfn $oldvenv $PREFIX/venv && sudo systemctl restart teaport-brain"
    fi
    die "the brain did not come back on $BRAIN_DEB_NEW, and no earlier package is kept to go back to ($BRAIN_DEBS)$back — see: $journal"
  fi
  warn "the brain did not come back on $BRAIN_DEB_NEW — putting ${prev} back"
  sip_brain_restore
  brain_deb_apply "$prevdeb" || true
  brain_units
  # shellcheck disable=SC2086
  set -- $SWAP_JUDGE
  if [ $# -gt 0 ] && brain_restart "$SWAP_ALSO" "$@"; then
    if [ "$deb" = "$prevdeb" ]; then
      # The same version reinstalled (a repair) and it failed: the installed package and the
      # rollback target are one file, which stays.
      die "the brain did not come back on the reinstall of $prev; it runs on $prev again — see: $journal"
    fi
    # The package that failed is not a rollback target: out of the cache, or the next
    # --rollback would pick it as the newest. A file given with TEAPORT_BRAIN_DEB stays.
    SUDO rm -f "$deb" || true
    local from=""
    if [ "$BRAIN_DEB_DOWNLOADED" != 1 ]; then from=" ($BRAIN_DEB stays)"; fi
    die "rolled back: the brain is running on $prev again. $BRAIN_DEB_NEW failed and is no longer kept in $BRAIN_DEBS$from — see: $journal"
  fi
  die "rolled back to $prev, but the brain is not healthy there either — see: $journal"
}

# phase_brain -- install the brain package. It restarts nothing the caller does not (a
# unit that is already running is restarted by the package's postinst); the full install's
# own restart is phase_services'.
phase_brain() {
  log "brain -> the $BRAIN_PKG package ($BRAIN_PKG in /opt/teaport/brain)"
  brain_deb_fetch
  brain_deb_keep
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] apt-get install %s\n' "$BRAIN_DEB_CACHED"; return 0; fi
  brain_deb_apply "$BRAIN_DEB_CACHED" || die "installing $BRAIN_DEB_CACHED failed — see: journalctl -u teaport-brain -n 50; dpkg -s $BRAIN_PKG"
}

# The phone busy lamp (brain/teaport_brain/xvf_led.py): the brain turns a ReSpeaker
# XVF3800's LED ring red during a call, with USB control transfers on the device's
# usbfs node -- root's alone unless a udev rule hands it to the teaport-hw group, which
# the run user is put in (teaport-brain.service.in names it too). A group of its own,
# not plugdev: the node reaches every interface of the device, its DFU one included.
# Laid down whether or not one is plugged in (the rule matches only its 2886:001a); here
# rather than in phase_local_audio so that --only brain, which ships the lamp, ships its
# access too. The trigger re-applies the rule to a device already plugged in, without a
# replug: a "change" event on the USB device only, which leaves its audio driver bound.
# The tmpfiles.d line makes the lamp's marker directory on /run (xvf_led.MARKER): kept
# across a brain crash, cleared by a reboot.
BUSY_LAMP_GROUP=teaport-hw
# packaging_src <path under packaging/> — that file beside this installer.
packaging_src() { printf '%s\n' "$HERE/packaging/$1"; }
install_busy_lamp() {
  local rule=/etc/udev/rules.d/60-teaport-xvf3800.rules src
  local tmpfiles=/etc/tmpfiles.d/teaport-busy-lamp.conf tmp
  src="$(packaging_src xvf3800/60-teaport-xvf3800.rules)"
  # The group first, and whatever else is skipped: the brain unit names it
  # (SupplementaryGroups=), and a unit naming a group that does not exist will not start.
  if ! getent group "$BUSY_LAMP_GROUP" >/dev/null; then SUDO groupadd --system "$BUSY_LAMP_GROUP"; fi
  if ! contains " $BUSY_LAMP_GROUP " " $(id -nG "$RUN_USER" 2>/dev/null) "; then
    SUDO usermod -aG "$BUSY_LAMP_GROUP" "$RUN_USER"
    warn "busy lamp: added $RUN_USER to $BUSY_LAMP_GROUP — the brain picks it up at its next restart (systemctl restart teaport-brain)"
  fi
  if [ ! -f "$src" ] || ! have udevadm; then
    log "busy lamp: no udev rule source or no udevadm — skipped (the ring stays the room's)"
    return 0
  fi
  log "busy lamp: udev rule -> $rule (group $BUSY_LAMP_GROUP may set the XVF3800's LED ring)"
  SUDO install -D -m 0644 -o root -g root "$src" "$rule"
  SUDO udevadm control --reload \
    || warn "busy lamp: udevadm control --reload failed — the rule applies from the next boot"
  SUDO udevadm trigger --action=change --subsystem-match=usb \
    --attr-match=idVendor=2886 --attr-match=idProduct=001a \
    || warn "busy lamp: udevadm trigger failed — replug the XVF3800 or reboot for the rule to apply"
  tmp="$(mktemp)"
  printf '%s\n' "# teaport busy lamp: the ring's look to restore after a brain crash (xvf_led.MARKER)" \
    "d /run/teaport-busy-lamp 0700 $RUN_USER $RUN_USER -" > "$tmp"
  SUDO install -D -m 0644 -o root -g root "$tmp" "$tmpfiles"
  rm -f "$tmp"
  SUDO systemd-tmpfiles --create "$tmpfiles" \
    || warn "busy lamp: systemd-tmpfiles failed — /run/teaport-busy-lamp appears at the next boot"
}

# DNS that keeps answering through a stalled router. The brain opens connections to
# its language model by name, and a home router's DNS can stop answering for tens of
# seconds: each new connection then waits on a lookup, and the caller hears nothing
# (live 2026-10-15, three turns lost). systemd-resolved's serve-stale answers a name it
# resolved within the last day from the cache once the router's first attempt times
# out. The network's DNS server stays the one asked first: nothing here overrides it.
# Written, and the resolver reloaded, only when the drop-in changes, so a brain update
# does not flush the cache every time. Only where it does something: systemd-resolved
# running, the system's lookups going through it (/etc/resolv.conf its stub), and
# systemd 254 or later (StaleRetentionSec; older resolvers ignore the key).
RESOLVED_DROPIN=/etc/systemd/resolved.conf.d/60-teaport-serve-stale.conf
RESOLV_CONF=/etc/resolv.conf
install_dns_resilience() {
  local src why="" ver
  src="$(packaging_src resolved/60-teaport-serve-stale.conf)"
  ver="$(systemctl --version 2>/dev/null | awk 'NR==1 {print $2}' || true)"   # no systemctl: no version, a skip
  if [ ! -f "$src" ]; then why="no drop-in source"
  elif ! systemctl is-active --quiet systemd-resolved 2>/dev/null; then why="systemd-resolved is not running"
  elif ! grep -qs '^nameserver 127\.0\.0\.53' "$RESOLV_CONF"; then why="$RESOLV_CONF does not use systemd-resolved's stub"
  elif ! [ "${ver:-0}" -ge 254 ] 2>/dev/null; then why="systemd ${ver:-?} predates serve-stale (254)"
  fi
  if [ -n "$why" ]; then
    log "dns: $why — serve-stale skipped (lookups go to the network's DNS as before)"
    return 0
  fi
  if cmp -s "$src" "$RESOLVED_DROPIN" 2>/dev/null; then
    return 0
  fi
  log "dns: serve-stale drop-in -> $RESOLVED_DROPIN (a stalled router's DNS is answered from the last good lookup once its first attempt times out)"
  SUDO install -D -m 0644 -o root -g root "$src" "$RESOLVED_DROPIN"
  SUDO systemctl try-reload-or-restart systemd-resolved \
    || warn "dns: reloading systemd-resolved failed — the drop-in applies from the next boot"
}

# ARP on a box with Ethernet and Wi-Fi on one subnet (#225). With the kernel's defaults
# (arp_ignore=0, arp_announce=0) any interface answers ARP for any of the host's addresses,
# so after a cable re-plug the Wi-Fi card answers the Ethernet link's duplicate-address
# probe and NetworkManager logs a "conflict detected" with the box itself; both times the
# wired connection came back with no IPv4 routes (2026-10-20 and 10-24). That the false
# conflict is what drops the routes is the hypothesis #225 still has to prove on a box.
# arp_ignore=1: answer only for addresses on the interface the request came in on;
# arp_announce=2: send from the outgoing interface's own address. Only `all` is set: the
# kernel uses the larger of conf/all and conf/<interface> for both keys, so `all` already
# covers every interface, including ones that appear later, and `default` would add
# nothing. The settings are the whole host's, so a machine that is not a Jetson keeps its
# own (a dry run still shows them). Written only when the file differs, and applied only
# then or while $ARP_APPLIED is missing: the apply is `sysctl --system`, which reads every
# sysctl.d file in order as boot does, so an operator's drop-in that sorts after this one
# (99-local.conf) keeps winning, and a run with both in place touches nothing. The apply is
# judged by the live values in $ARP_PROC first: --system also fails on another file's key
# the kernel lacks (net.bridge.* without br_netfilter) after applying ours. Values that are
# ours, or not ours after a clean --system (a later drop-in setting them on purpose, said in
# one log line), are recorded in $ARP_APPLIED (as setup_logs' JOURNAL_APPLIED); values
# that are not ours after a failed --system are not, so the next run tries again. Rides
# --only brain too, like the resolver's drop-in: boxes are updated that way.
ARP_SYSCTL_DROPIN=/etc/sysctl.d/60-teaport-multihome.conf
ARP_APPLIED="$STATE/arp-multihome.applied"
ARP_PROC=/proc/sys/net/ipv4/conf/all
install_arp_multihome() {
  local want have ignore announce rc=0
  if [ ! -f "$NV_TEGRA_RELEASE" ] && [ "$DRY_RUN" != 1 ]; then
    log "network: not a Jetson — this machine's ARP settings are left as they are"
    return 0
  fi
  want="$(printf '%s\n' \
    "# teaport (install.sh install_arp_multihome, #225): with Ethernet and Wi-Fi on one subnet," \
    "# each interface answers ARP only for its own addresses and sends from them. install.sh" \
    "# owns this file and puts it back when it differs; to override, use a drop-in that sorts" \
    "# after it (99-local.conf, say)." \
    "net.ipv4.conf.all.arp_ignore = 1" \
    "net.ipv4.conf.all.arp_announce = 2")"
  have="$(cat "$ARP_SYSCTL_DROPIN" 2>/dev/null || true)"
  if [ "$want" = "$have" ] && [ "$(cat "$ARP_APPLIED" 2>/dev/null || true)" = "$want" ]; then
    return 0
  fi
  log "network: ARP drop-in -> $ARP_SYSCTL_DROPIN (each interface answers ARP only for its own addresses: Ethernet and Wi-Fi on one subnet)"
  if [ "$want" != "$have" ]; then
    # A new file is a new apply: the record of an earlier one goes first, so an apply of
    # this file that fails below leaves none, and the next run tries again.
    if ! SUDO rm -f "$ARP_APPLIED" || ! write_root "$ARP_SYSCTL_DROPIN" "$want"; then
      warn "network: could not write $ARP_SYSCTL_DROPIN — ARP stays as it was"
      return 0
    fi
  fi
  SUDO sysctl -q --system || rc=$?   # judged below, with what the kernel holds
  [ "$DRY_RUN" = 1 ] && return 0
  ignore="$(cat "$ARP_PROC/arp_ignore" 2>/dev/null || true)"
  announce="$(cat "$ARP_PROC/arp_announce" 2>/dev/null || true)"
  if ! { [ "$ignore" = 1 ] && [ "$announce" = 2 ]; }; then
    if [ "$rc" != 0 ]; then
      warn "network: ARP is arp_ignore=${ignore:-?} arp_announce=${announce:-?} after a failed sysctl --system, not 1 and 2 — the next run tries again"
      return 0
    fi
    # Every file applied cleanly and the values are still not ours: a drop-in sorting
    # after this one sets them on purpose. Said once, recorded, not fought.
    log "network: a sysctl.d drop-in after $ARP_SYSCTL_DROPIN sets ARP to arp_ignore=${ignore:-?} arp_announce=${announce:-?} — left as it is"
  fi
  write_root "$ARP_APPLIED" "$want" \
    || warn "network: could not record the ARP settings in $ARP_APPLIED — the next run applies them again"
}

# Which agent hosts the realtime-voice plugin: "nemoclaw" (OpenClaw inside NVIDIA's docker
# sandbox), "openclaw" (OpenClaw installed directly on the host), or "" (voice-only).
# NemoClaw wins when both are present: its sandbox is the packaged, supported arrangement.
AGENT_MODE=""
# NemoClaw sandbox name (empty if none) + the nemoclaw binary + the /talk auth token,
# resolved once so the brain (brain.env) and the plugin config share the same token.
SANDBOX=""; NEMOCLAW=""; GATEWAY_TOKEN=""; OPENCLAW=""
detect_sandbox() {
  local sj="$HOME/.nemoclaw/sandboxes.json"
  { have nemoclaw || [ -x "$HOME/.local/bin/nemoclaw" ]; } || return 0
  [ -f "$sj" ] || return 0
  # sandboxes.json = {defaultSandbox, sandboxes:{<name>:...}} — prefer the default.
  SANDBOX="$(python3 -c "import json;d=json.load(open('$sj'));print(d.get('defaultSandbox') or (list(d.get('sandboxes',{})) or [''])[0])" 2>/dev/null || true)"
}
# A host OpenClaw counts only once it has written its config — `openclaw` on PATH with no
# ~/.openclaw/openclaw.json means it was installed but never set up, and `config patch`
# would be writing into a config the user has not chosen the shape of yet.
detect_openclaw() {
  have openclaw || return 0
  [ -f "$HOME/.openclaw/openclaw.json" ] || return 0
  OPENCLAW="$(command -v openclaw)"
}
detect_agent() {
  detect_sandbox
  if [ -n "$SANDBOX" ]; then AGENT_MODE=nemoclaw; return 0; fi
  detect_openclaw
  [ -n "$OPENCLAW" ] && AGENT_MODE=openclaw
  return 0
}
resolve_nemoclaw() { NEMOCLAW="$(command -v nemoclaw || echo "$HOME/.local/bin/nemoclaw")"; }
# openclaw_spec — what to `npm install -g`: the OpenClaw release this one is tested with
# (manifest openclaw.version), or plain openclaw from a manifest that names none.
openclaw_spec() {
  local v; v="$(mget openclaw.version 2>/dev/null || true)"
  if [ -n "$v" ]; then printf 'openclaw@%s\n' "$v"; else printf 'openclaw\n'; fi
}
# check_openclaw_version — the box's OpenClaw (on the host, or in the NemoClaw sandbox)
# against the release this one is tested with. teaport leans on OpenClaw's own behaviour
# (the Talk relay's response ownership, the realtime brain, pairing and setup codes), read
# from that release's source, and the guide's `npm install -g openclaw` would otherwise
# take whatever is newest. A warning, not a stop: an operator may run another on purpose.
check_openclaw_version() {
  local want got=""
  want="$(mget openclaw.version 2>/dev/null || true)"
  if [ -z "$want" ]; then return 0; fi
  case "$AGENT_MODE" in
    openclaw) got="$("$OPENCLAW" --version 2>/dev/null || true)" ;;
    nemoclaw)
      resolve_nemoclaw
      # </dev/null: a prompt (a stopped sandbox, a login) fails fast into the warning.
      got="$(timeout 60 "$NEMOCLAW" "$SANDBOX" exec --no-tty -- openclaw --version </dev/null 2>/dev/null || true)" ;;
    *) return 0 ;;
  esac
  got="$(printf '%s\n' "$got" | grep -oE '[0-9]{4}\.[0-9]+\.[0-9]+' | head -1 || true)"
  if [ -z "$got" ]; then
    warn "could not read the OpenClaw version; this release is tested with OpenClaw $want"
  elif [ "$got" != "$want" ]; then
    warn "OpenClaw $got is installed; this release of teaport is tested with OpenClaw $want"
    if [ "$AGENT_MODE" = openclaw ]; then
      warn "to match it: back up ~/.openclaw, then sudo npm install -g $(openclaw_spec) and openclaw doctor --fix"
    else
      warn "the sandbox's OpenClaw comes with NemoClaw: use the NemoClaw release that ships OpenClaw $want"
    fi
  else
    log "OpenClaw $got (the release this one is tested with)"
  fi
}
resolve_gateway_token() {
  [ -n "$GATEWAY_TOKEN" ] && return 0
  if [ -n "${TEAPORT_GATEWAY_TOKEN:-}" ]; then GATEWAY_TOKEN="$TEAPORT_GATEWAY_TOKEN"; return 0; fi
  # idempotent re-run: reuse the token already in brain.env instead of re-minting.
  #
  # This read has to survive an UNREADABLE brain.env, for the same reason write_env's does:
  # a first install under plain sudo leaves the file root:root, and then a repair run by the
  # operator cannot read it. The old one-liner did not survive it. `sed` exits non-zero on a
  # file it cannot open, `set -o pipefail` promotes that to the pipeline, and the installer
  # died right here — exit 2, no message, before any phase that could have explained it, and
  # before write_env's privileged read ever ran (measured with a root:root brain.env,
  # 2026-08-12). Read privileged when the plain read fails (read_env_file), and parse
  # in-shell: `sed | head` also risks SIGPIPE killing sed under pipefail, which is the same
  # class of bug.
  if [ -f "$ETC/brain.env" ]; then
    local envtxt="" line
    envtxt="$(read_env_file "$ETC/brain.env" --lenient)"
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in GATEWAY_TOKEN=*) GATEWAY_TOKEN="${line#GATEWAY_TOKEN=}"; break ;; esac
    done <<<"$envtxt"
  fi
  [ -n "$GATEWAY_TOKEN" ] && return 0
  GATEWAY_TOKEN="$( (head -c18 /dev/urandom 2>/dev/null || echo "teaport-$$") | od -An -tx1 | tr -d ' \n')"
  return 0
}

# The OpenClaw config both agent paths share; only the brain URL differs.
#
# talk.realtime — brain=agent-consult: the teaport brain orchestrates the turn and asks
# OpenClaw only through its consult tool, which is what agent-consult means. It is also the
# only brain OpenClaw (2026.9.x) runs a realtime session with ("realtime talk.session.create
# requires ... brain=\"agent-consult\""; none is for transcription sessions), and the iOS
# app's Talk falls back to on-device speech for any other value, so brain=none -- written
# here before -- kept the OpenClaw app off the teaport voice entirely. The token matches
# brain.env's GATEWAY_TOKEN so the brain accepts the relayed session.
#
# device-pair — what `openclaw qr` advertises to a phone. Two non-obvious parts:
#   * `enabled` must be explicit. The plugin ships enabledByDefault, but as a *bundled* plugin
#     it still needs opt-in on this platform; config alone gets "plugin disabled ... but config
#     is present" and the pairing methods never load.
#   * publicUrl must be set at all. The gateway is loopback-only by design with Caddy as the
#     front door (see phase_frontdoor), and nothing else tells device-pair the front door
#     exists — so `openclaw qr` fails with "Gateway is only bound to loopback" and there is no
#     way to pair a phone. Pointing it at Caddy is what makes the QR resolvable.
# wss, not ws: OpenClaw hands a cleartext LAN pairing only a read+talk profile, so a phone
# paired over ws:// silently comes up without operator scope.
write_talk_patch() {
  cat > "$1" <<JSON
{ "talk": { "realtime": {
  "provider": "teaport", "mode": "realtime", "transport": "gateway-relay", "brain": "agent-consult",
  "providers": { "teaport": { "url": "$2", "token": "$GATEWAY_TOKEN" } } } },
  "plugins": { "entries": { "device-pair": {
  "enabled": true, "config": { "publicUrl": "wss://$FRONTDOOR_HOST" } } } } }
JSON
}

# The published plugin, when there is no local source to pack.
PLUGIN_NPM_SPEC="@teaspoon-ai/openclaw-teaport-realtime"
# plugin_tgz — echo the path of a packed local plugin tarball, or nothing when there is no
# local source (install from PLUGIN_NPM_SPEC instead). Both agent paths resolve and pack
# identically; only how the tgz reaches OpenClaw differs (direct vs sandbox upload), so only
# that part stays in the callers. Progress goes to stderr — stdout is the return value.
plugin_tgz() {
  local psrc="${PLUGIN_SRC:-}"; if [ -z "$psrc" ] && [ -d "$HERE/plugin" ]; then psrc="$HERE/plugin"; fi
  [ -n "$psrc" ] || return 0
  if [ "$DRY_RUN" = 1 ]; then
    printf '\033[2m  [dry-run] (cd %s && npm pack) -> /tmp/<pkg>.tgz\033[0m\n' "$psrc" >&2
    printf '/tmp/teaport-plugin.tgz'; return 0
  fi
  have npm || die "npm is required to package the plugin from $psrc"
  local tgz; tgz="$(cd "$psrc" && npm pack --silent --pack-destination /tmp)" || die "npm pack failed in $psrc"
  printf '/tmp/%s' "$tgz"
}

phase_agent() {
  case "$AGENT_MODE" in
    nemoclaw) agent_nemoclaw ;;
    openclaw) agent_openclaw ;;
    *)
      log "no agent detected — voice-only install"
      warn "to add the agent later: install OpenClaw on the host (sudo npm install -g $(openclaw_spec))"
      warn "or NemoClaw (bash <(curl -fsSL https://www.nvidia.com/nemoclaw.sh)), then re-run this installer"
      ;;
  esac
}

# --- host OpenClaw -----------------------------------------------------------
# OpenClaw sizes the gateway's JS heap at half the RAM it sees (3773 MiB on an 8 GB Orin),
# and on this box the speech engine holds ~4.3 GB of that RAM in GPU buffers that never
# swap: a gateway allowed to grow that far would crowd out the voice path. The flag lives
# in the --max-old-space-size of the unit OpenClaw generates, and OpenClaw keeps a heap
# flag it finds there whenever it rewrites the unit (an update, `gateway install
# --force`), so setting it there is enough. 1536 MiB is about 4x the heap the gateway
# used on the appliance (380 MB, 2026-10-14); past it the gateway restarts, as it does on
# any crash. Returns 0 when it changed the unit, so the caller knows a restart is due.
cap_openclaw_heap() {
  local unit; unit="$(systemctl --user show -p FragmentPath --value openclaw-gateway.service 2>/dev/null || true)"
  [ -n "$unit" ] && [ -f "$unit" ] || { warn "no openclaw-gateway unit file: its heap limit stays OpenClaw's"; return 1; }
  local want="--max-old-space-size=$OPENCLAW_HEAP_MIB"
  if grep -qE -- "^ExecStart=.* $want( |$)" "$unit"; then return 1; fi
  # Called from an && / || list, where set -e does not reach: each step checks for itself.
  if grep -qE -- '^ExecStart=.*--max-old-space-size=[0-9]+' "$unit"; then
    sed -i -E "/^ExecStart=/s/--max-old-space-size=[0-9]+/$want/" "$unit" \
      || { warn "could not edit $unit: the gateway heap limit stays OpenClaw's"; return 1; }
  elif grep -qE '^ExecStart=([^ "]*/)?node ' "$unit"; then
    # No flag of its own (OpenClaw found no capacity, or NODE_OPTIONS carried one) and node
    # runs the script directly: a flag right after it is a node option.
    sed -i -E "s#^(ExecStart=([^ \"]*/)?node) #\\1 $want #" "$unit" \
      || { warn "could not edit $unit: the gateway heap limit stays OpenClaw's"; return 1; }
  else
    # A wrapper or bun runs it (OpenClaw leaves the heap out for both), or a quoted path:
    # a flag after the first word would be an argument to something else.
    warn "the gateway unit does not run node directly ($unit): its heap limit stays OpenClaw's"
    return 1
  fi
  # OpenClaw reads the loaded unit back when it rewrites it: an edit systemd never loaded
  # would be overwritten with the old flag.
  systemctl --user daemon-reload \
    || { warn "systemctl --user daemon-reload failed: the gateway heap limit may not hold"; return 1; }
  log "OpenClaw gateway heap limit -> ${OPENCLAW_HEAP_MIB} MiB ($unit)"
}

agent_openclaw() {
  resolve_gateway_token
  log "host OpenClaw — installing the plugin + wiring talk.realtime"
  # Loopback, not the docker bridge: OpenClaw runs on the host, beside the brain.
  local brain_ws="ws://127.0.0.1:${BRAIN_PORT}/talk"

  local tgz; tgz="$(plugin_tgz)"
  if [ -n "$tgz" ]; then
    run "$OPENCLAW" plugins install "$tgz" --force
  else
    run "$OPENCLAW" plugins install "$PLUGIN_NPM_SPEC" --pin
  fi
  run "$OPENCLAW" plugins enable teaport-realtime

  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] openclaw config patch: talk.realtime provider=teaport brain=agent-consult url=%s (+token)\n' "$brain_ws"
    printf '  [dry-run] openclaw config patch: plugins.device-pair enabled=true publicUrl=wss://%s (phone pairing)\n' "$FRONTDOOR_HOST"
    printf '  [dry-run] openclaw config patch: gateway.port=%s trustedProxies=[127.0.0.1, ::1] (Caddy front door)\n' "$GATEWAY_PORT"
    printf '  [dry-run] openclaw config patch: gateway.http.endpoints.chatCompletions.enabled=true (warm-lane consult; ships off)\n'
  else
    local patch; patch="$(mktemp)"
    write_talk_patch "$patch" "$brain_ws"
    "$OPENCLAW" config patch --file "$patch"
    # phase_frontdoor puts Caddy in front of the gateway on the same host. Caddy injects
    # X-Forwarded-* headers, and the gateway refuses to treat a forwarded connection as local
    # unless the proxy is trusted — logging "Proxy headers detected from untrusted address" on
    # every browser hit. Loopback entries are valid here precisely for same-host reverse
    # proxies. This does not weaken auth: gateway.auth.mode stays as configured.
    # gateway.port must be pinned too: Caddy proxies to 127.0.0.1:$GATEWAY_PORT and brain.env
    # points OPENCLAW_GATEWAY_URL at the same port, but nothing else tells a host OpenClaw to
    # LISTEN there — its own default (and any TEAPORT_GATEWAY_PORT override) would otherwise
    # leave Caddy proxying to a dead port that phase_verify never checks.
    # http.endpoints.chatCompletions.enabled: turn on the warm-lane consult endpoint the brain
    # prefers for openclaw_agent_consult (openclaw_client._completions_consult -> POST /v1/chat/completions).
    # It is a STOCK endpoint but ships DISABLED ("default: false"), so without this every consult
    # 404s the fast lane and falls back to the ~25s cold `openclaw agent --local` spawn (which then
    # tends to blow the 45s budget and fail silently). config patch deep-merges, so this only adds
    # the key. (Bare OpenClaw needs nothing more; the NemoClaw sandbox additionally needs the
    # 1e72408 dial-back patch for CHANNEL actions — a sandbox-image concern, see agent_nemoclaw.)
    cat > "$patch" <<JSON
{ "gateway": {
    "port": $GATEWAY_PORT,
    "trustedProxies": ["127.0.0.1", "::1"],
    "http": { "endpoints": { "chatCompletions": { "enabled": true } } }
} }
JSON
    "$OPENCLAW" config patch --file "$patch"
    rm -f "$patch"
  fi

  # `openclaw gateway install` installs AND starts the unit. Calling `gateway start` after
  # it restarts the service, killing that first process mid startup-migration and orphaning
  # a ~5 minute lease in ~/.openclaw/state/openclaw.sqlite — which crash-loops the gateway
  # until the lease expires. So: install when the unit is absent, restart when it is there.
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] openclaw gateway install (unit absent) or gateway restart (unit present)\n'
    printf '  [dry-run] gateway unit: --max-old-space-size=%s (cap_openclaw_heap)\n' "$OPENCLAW_HEAP_MIB"
  elif systemctl --user cat openclaw-gateway.service >/dev/null 2>&1; then
    cap_openclaw_heap || true
    "$OPENCLAW" gateway restart
  else
    "$OPENCLAW" gateway install
    # Not restarted for it: see above. The limit takes effect at its next start.
    cap_openclaw_heap && log "  ... from the gateway's next start (a reboot or the next install)" || true
  fi

  # The brain calls back into OpenClaw for memory recall and tools, authenticating with the
  # gateway's *own* bearer token — openclaw_client._token() reads OPENCLAW_GATEWAY_TOKEN, else
  # ~/.config/teaport/openclaw_token. Nothing wrote that file, so every install logged
  # "no gateway token; skipping tool 'memory_search'" and lost memory recall silently, with a
  # working voice loop hiding the failure. Read it after the gateway step above, which is what
  # mints the token when it is absent. Note this is neither GATEWAY_TOKEN (brain /talk) nor the
  # LLM key — three distinct secrets.
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] write %s/openclaw_token from openclaw.json gateway.auth.token (mode 600)\n' "$SECRETS"
  else
    local octok; octok="$(python3 -c "import json;print(json.load(open('$HOME/.openclaw/openclaw.json')).get('gateway',{}).get('auth',{}).get('token',''))" 2>/dev/null || true)"
    if [ -n "$octok" ]; then
      mkdir -p "$SECRETS"; chmod 700 "$SECRETS"
      printf '%s' "$octok" > "$SECRETS/openclaw_token"; chmod 600 "$SECRETS/openclaw_token"
      log "openclaw gateway token -> $SECRETS/openclaw_token (memory recall + tools)"
    else
      warn "no gateway.auth.token in openclaw.json — memory recall and OpenClaw tools will be skipped"
    fi
  fi

  # The gateway is a systemd *user* service, so without linger it does not start until the
  # user logs in — a headless appliance would reboot into no gateway and no front door.
  if ! contains '=yes' "$(loginctl show-user "$RUN_USER" --property=Linger 2>/dev/null || true)"; then
    log "enabling systemd linger for $RUN_USER (user gateway must survive reboot)"
    SUDO loginctl enable-linger "$RUN_USER"
  fi

  log "host OpenClaw wired — verify with: openclaw gateway status"
}

# --- NemoClaw sandbox --------------------------------------------------------
agent_nemoclaw() {
  resolve_nemoclaw; resolve_gateway_token
  log "NemoClaw sandbox '$SANDBOX' — installing the plugin + wiring talk.realtime"
  local brain_ws="ws://172.18.0.1:${BRAIN_PORT}/talk"   # sandbox -> host brain over the docker bridge

  # 1. Plugin into the sandbox. Published -> npm spec; otherwise npm-pack a tgz (honors the
  #    files allowlist), upload it, and `openclaw plugins install <tgz>` copies it into
  #    .openclaw/extensions/ — the pack itself is shared with the host path (plugin_tgz).
  local tgz; tgz="$(plugin_tgz)"
  if [ -n "$tgz" ]; then
    run "$NEMOCLAW" "$SANDBOX" upload "$tgz" "$tgz"
    run "$NEMOCLAW" "$SANDBOX" exec --no-tty -- openclaw plugins install "$tgz" --force
  else
    run "$NEMOCLAW" "$SANDBOX" exec --no-tty -- openclaw plugins install "$PLUGIN_NPM_SPEC" --pin
  fi
  run "$NEMOCLAW" "$SANDBOX" exec --no-tty -- openclaw plugins enable teaport-realtime

  # 2. talk.realtime + device-pair — one validated merge (openclaw config patch). The token
  #    matches brain.env. Caddy fronts the *sandbox's* gateway through the same host port, so
  #    the pairing URL is the host front door, exactly as on the host-OpenClaw path.
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] openclaw config patch: talk.realtime provider=teaport brain=agent-consult url=%s (+token)\n' "$brain_ws"
    printf '  [dry-run] openclaw config patch: plugins.device-pair enabled=true publicUrl=wss://%s (phone pairing)\n' "$FRONTDOOR_HOST"
    printf '  [dry-run] openclaw config patch: gateway.trustedProxies=[127.0.0.1, ::1, 172.18.0.1] (Caddy front door)\n'
    printf '  [dry-run] openclaw config patch: gateway.http.endpoints.chatCompletions.enabled=true (warm-lane consult; ships off)\n'
  else
    local patch; patch="$(mktemp)"
    write_talk_patch "$patch" "$brain_ws"
    "$NEMOCLAW" "$SANDBOX" upload "$patch" /tmp/teaport-talk.json
    "$NEMOCLAW" "$SANDBOX" exec --no-tty -- openclaw config patch --file /tmp/teaport-talk.json
    # Same reason as the host path: Caddy injects X-Forwarded-* into the proxied hop, and an
    # untrusted proxy gets every phone/browser hit refused as non-local — device-pair through
    # the front door needs this HERE too, not just on the host. The hop reaches the sandbox
    # gateway either from its own loopback (an in-sandbox forward) or from the docker-bridge
    # host address (the same 172.18.0.1 the brain URL uses) — trust both; the forward is
    # host-loopback-bound, so this adds no reachability an attacker didn't already have.
    # Also enable the warm-lane consult endpoint (POST /v1/chat/completions) the brain prefers
    # for openclaw_agent_consult — a stock endpoint that ships DISABLED ("default: false"). NOTE: inside the
    # sandbox this makes NON-channel consults fast, but a warm-lane turn that drives a CHANNEL
    # action still needs the openshell-dialback-locality patch (NemoClaw 1e72408) to escape the
    # device-pairing dial-back loop; without it the brain falls back to `openclaw agent --local`
    # (which loads channel plugins in-process) for those. Enabling the flag is still strictly
    # better than leaving every consult 404 the fast lane.
    cat > "$patch" <<'JSON'
{ "gateway": {
    "trustedProxies": ["127.0.0.1", "::1", "172.18.0.1"],
    "http": { "endpoints": { "chatCompletions": { "enabled": true } } }
} }
JSON
    "$NEMOCLAW" "$SANDBOX" upload "$patch" /tmp/teaport-proxies.json
    "$NEMOCLAW" "$SANDBOX" exec --no-tty -- openclaw config patch --file /tmp/teaport-proxies.json
    rm -f "$patch"
  fi

  # 3. Reload the sandbox gateway + snapshot the wired state.
  run "$NEMOCLAW" "$SANDBOX" gateway restart
  run "$NEMOCLAW" "$SANDBOX" snapshot create --name teaport-installed
  # NemoClaw egress-locks the sandbox by default; the brain is on the host docker bridge
  # (172.18.0.1), reachable without an egress exception. Verify with: nemoclaw $SANDBOX doctor.
  log "sandbox wired — verify with: $NEMOCLAW $SANDBOX doctor"
}

# LLM config gathered by phase_credentials, consumed by phase_services (brain.env).
# LLM_URL_SEED / LLM_MODEL_SEED are "?" when the operator gave no endpoint / model (no env
# var, no prompt answer) and the value is only the default: written as a write_env seed,
# so a repair keeps whatever the config page set rather than snapping back to the default.
# An explicit answer is written plain, and wins.
LLM_BASE_URL=""; LLM_MODEL=""; LLM_URL_SEED="?"; LLM_MODEL_SEED="?"
phase_credentials() {
  log "credentials -> $SECRETS (secrets never baked into units)"
  run mkdir -p "$SECRETS"; run chmod 700 "$SECRETS"
  # Bring-your-own OpenAI-compatible LLM (item-2 model): endpoint + key + model.
  LLM_BASE_URL="${TEAPORT_LLM_BASE_URL:-}"; LLM_MODEL="${TEAPORT_LLM_MODEL:-gpt-oss-120b}"
  [ -n "$LLM_BASE_URL" ] && LLM_URL_SEED=""
  [ -n "${TEAPORT_LLM_MODEL:-}" ] && LLM_MODEL_SEED=""
  local key="${TEAPORT_LLM_API_KEY:-}"
  if [ -z "$LLM_BASE_URL" ] && [ -t 0 ] && [ "$DRY_RUN" != 1 ]; then
    read -r -p 'LLM base URL (OpenAI-compatible, e.g. https://api.groq.com/openai/v1): ' LLM_BASE_URL
    read -r -p 'LLM model [gpt-oss-120b]: ' m; [ -n "$m" ] && LLM_MODEL="$m"
    [ -n "$LLM_BASE_URL" ] && LLM_URL_SEED=""
    [ -n "$m" ] && LLM_MODEL_SEED=""
    read -r -s -p 'LLM API key (blank for a local keyless server): ' key; echo
  fi
  [ -z "$LLM_BASE_URL" ] && LLM_BASE_URL="http://127.0.0.1:8182/v1"   # local default when unattended
  if [ -n "$key" ]; then
    if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] write %s/llm_key (mode 600)\n' "$SECRETS";
    else printf '%s' "$key" > "$SECRETS/llm_key"; chmod 600 "$SECRETS/llm_key"; fi
  else
    warn "no LLM API key given — set LLM_API_KEY or write $SECRETS/llm_key before starting the brain"
  fi
}

# Ports + tunables (observed on the reference appliance).
BRAIN_PORT="${TEAPORT_BRAIN_PORT:-7861}"
ENGINE_PORT="${TEAPORT_ENGINE_PORT:-8000}"
GATEWAY_PORT="${TEAPORT_GATEWAY_PORT:-18789}"     # OpenClaw gateway (sandbox -> host forward)
OPENCLAW_HEAP_MIB="${TEAPORT_OPENCLAW_HEAP_MIB:-1536}"   # its JS heap limit (cap_openclaw_heap)
# Checked here, before anything is installed: a value node refuses ("2G") would crash-loop
# the gateway, and found at the agent phase it would stop the install halfway.
[[ $OPENCLAW_HEAP_MIB =~ ^[1-9][0-9]*$ ]] || die "TEAPORT_OPENCLAW_HEAP_MIB must be a whole number of MiB, not '$OPENCLAW_HEAP_MIB'"
FRONTDOOR_HOST="${TEAPORT_HOST:-teaport.local}"   # mDNS name the browser opens over HTTPS (resolve_frontdoor)
SETTINGS_PORT="${TEAPORT_SETTINGS_PORT:-8443}"    # the owner's Settings, an origin of its own (#279)
[[ $SETTINGS_PORT =~ ^[1-9][0-9]{0,4}$ ]] && [ "$SETTINGS_PORT" -le 65535 ] && [ "$SETTINGS_PORT" != 443 ] \
  || die "TEAPORT_SETTINGS_PORT must be a port number other than 443, not '$SETTINGS_PORT'"
# Where 'tls internal' keeps its CA. The Debian caddy package runs the daemon with
# HOME=/var/lib/caddy, so its data dir is the XDG default underneath that. Its leaf
# certificates are cached beside it, under certificates/local.
CADDY_PKI="${TEAPORT_CADDY_PKI:-/var/lib/caddy/.local/share/caddy/pki/authorities/local}"
CADDY_LEAVES="${CADDY_PKI%/pki/authorities/local}/certificates/local"
# The local CA's certificate lifetimes (write_caddyfile says why they are long).
LEAF_LIFETIME=365d INTERMEDIATE_LIFETIME=1095d
CADDYFILE=/etc/caddy/Caddyfile   # ours (write_caddyfile); resolve_frontdoor reads it back
# The operator's own certificate for the front door (#249), in place of 'tls internal'.
# Unset on a re-run, the one the Caddyfile already serves stays (resolve_frontdoor); set
# but empty, the front door goes back to the box's own.
TLS_CERT="${TEAPORT_TLS_CERT:-}" TLS_KEY="${TEAPORT_TLS_KEY:-}"
if [ -n "$TLS_CERT$TLS_KEY" ] && { [ -z "$TLS_CERT" ] || [ -z "$TLS_KEY" ]; }; then
  die "TEAPORT_TLS_CERT and TEAPORT_TLS_KEY go together: set both, or neither"
fi
REGENERATE_CA="${TEAPORT_REGENERATE_CA:-0}"
# The name of the box's own certificate authority, as devices list it (resolve_frontdoor).
FRONTDOOR_CA_NAME=""
render_unit() {  # render_unit <template.in> <dest-name>
  local tpl="$HERE/systemd/$1" out="$2"
  [ -f "$tpl" ] || die "missing unit template: $tpl"
  local nemoclaw_bin; nemoclaw_bin="$(command -v nemoclaw || echo /usr/local/bin/nemoclaw)"
  # Absolute node path for the bridge unit's ExecStart — systemd services get a minimal PATH
  # and node may live outside it (nvm/NodeSource), so resolve it at render time like @NEMOCLAW@.
  local node_bin; node_bin="$(command -v node || echo /usr/bin/node)"
  # SIP telephony unit substitutions (opt-in — see phase_sip). The GPL gateway binary lives
  # in its own repo/build tree (NOT $PREFIX); @SIP_CONF@ is the credentials .conf the CLI
  # (`teaport sip configure`) writes into the secrets dir and both SIP units gate on
  # (ConditionPathExists); @SIP_UDS@ is the AF_UNIX socket the gateway and brain meet on.
  local sip_gateway="${TEAPORT_SIP_GATEWAY:-$HOME/teaport-sip/build/teaport-sip}"
  local sip_conf="$SECRETS/teaport-sip.conf"
  local sip_uds="${TEAPORT_SIP_UDS:-/run/teaport/teaport-sip.sock}"
  local body; body="$(sed -e "s#@USER@#$RUN_USER#g" -e "s#@PREFIX@#$PREFIX#g" -e "s#@ETC@#$ETC#g" \
                          -e "s#@NEMOCLAW@#$nemoclaw_bin#g" -e "s#@SANDBOX@#$SANDBOX#g" \
                          -e "s#@NODE@#$node_bin#g" \
                          -e "s#@SIP_GATEWAY@#$sip_gateway#g" -e "s#@SIP_CONF@#$sip_conf#g" \
                          -e "s#@SIP_UDS@#$sip_uds#g" "$tpl")"
  # Through write_root, so the unit is root's and 0644 whatever sudo's umask (#199). systemd
  # reads a 0600 unit as root all the same, but `systemctl cat` reads it as whoever runs it,
  # and that is how the CLI (installed) and --only brain tell an installed unit from none.
  # A unit masked here (a link to /dev/null) stays masked: install would replace the link with
  # the unit, where tee wrote through it into /dev/null and kept the operator's mask.
  if [ "$(readlink "/etc/systemd/system/$out" 2>/dev/null)" = /dev/null ]; then
    warn "$out is masked on this machine; left masked (systemctl unmask $out, then re-run, to install it)"
  elif [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] render %s -> /etc/systemd/system/%s\n' "$1" "$out";
  else write_root "/etc/systemd/system/$out" "$body"; fi
}
write_env() {  # write_env <path> <lines...>  (SUDO, mode 640)
  # Keys passed here are OURS: rewritten every run so a repair restores them. Everything
  # else already in the file is an operator edit and is carried forward. Re-running the
  # installer is the documented repair action (README, FAQ, Troubleshooting all say so), and
  # a plain overwrite silently deleted hand-tuned settings — LLM_EXTRA_BODY provider routing,
  # TTS_SEAM_KEEP_*, ENDPOINT_STOP_SECS/SMARTTURN_COMPLETE_THRESHOLD. The box came back
  # "installed fine" and quietly sounded different, with nothing in the output saying why.
  # Comments and blank lines are not preserved: the file is generated, and re-emitting a
  # user's comment against a value we may have just changed would be worse than dropping it.
  #
  # A line given as `?KEY=value` is a SEED: written on a first install, but an existing
  # KEY in the file wins over it on a repair. That is for the values the config page
  # (https://<box>:$SETTINGS_PORT/settings/config) lets the operator change and the installer merely
  # defaults — LLM_BASE_URL/LLM_MODEL when nobody answered the prompt or set the env var.
  # Without it the documented repair action put the box back on the local LLM endpoint
  # with nothing in the page saying why. An explicit answer is passed plain, and wins.
  local path="$1"; shift
  local keep=() ours=() overwritten=() managed=" " existing_keys=" " line
  # The read must not silently degrade to a clobber. The rewrite below is privileged (SUDO
  # tee), so when the plain read fails, read privileged too (read_env_file) — and stop if
  # even that fails. A dry run does not sudo: it says what a real run would read.
  local existing=""
  if [ -e "$path" ] && [ ! -r "$path" ] && [ "$DRY_RUN" = 1 ]; then
    warn "$path is not readable by $(id -un) — a real run reads it with sudo to preserve operator settings"
  elif [ -e "$path" ]; then
    existing="$(read_env_file "$path")" || die "cannot read $path — refusing to overwrite it blind and lose operator settings"
  fi
  # `|| [ -n "$line" ]`: without it, read's non-zero at EOF drops a final line that lacks a
  # trailing newline — silently deleting that operator setting.
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) ;; *=*) existing_keys="$existing_keys${line%%=*} " ;; esac
  done <<< "$existing"
  # Which of our lines are written this run: every plain one, and a seed whose key the
  # file does not already hold. A seed that IS in the file drops out here, so the loop
  # below carries the file's line the way it carries any operator setting.
  for line in "$@"; do
    case "$line" in
      '?'*) line="${line#?}"; case "$existing_keys" in *" ${line%%=*} "*) continue ;; esac ;;
      *)
        # A plain key is OURS unconditionally (see the function header) — but if the file
        # already held a DIFFERENT value for it, that was an operator or config-page
        # setting, and overwriting it without a word is the "silently reverted my change"
        # bug reports look like. grep the old value and, if it differs, name the key below.
        # `|| true`: under `set -o pipefail` a key the file does not hold makes grep exit 1,
        # and `set -e` then kills the script inside this assignment with no message at all
        # — which is every key on a FIRST install (the file is empty), so the installer
        # died at "systemd units + env files" on any fresh box (and in CI's dry-run).
        local _k="${line%%=*}" _old
        _old="$(printf '%s\n' "$existing" | grep -E "^${_k}=" | tail -1 || true)"
        if [ -n "$_old" ] && [ "$_old" != "$line" ]; then overwritten+=("$_k"); fi
        ;;
    esac
    ours+=("$line"); managed="$managed${line%%=*} "
  done
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
      *=*) case "$managed" in *" ${line%%=*} "*) ;; *) keep+=("$line") ;; esac ;;
    esac
  done <<< "$existing"
  # NB: `[ cond ] && cmd` as a statement returns non-zero when cond is false, which under
  # `set -e` aborts the installer on a first install (empty keep array). Use if-blocks.
  if [ "$DRY_RUN" = 1 ]; then
    # --dry-run output is the transcript operators paste into bug reports and CI logs (see
    # usage_footer) — show secret-bearing managed keys and preserved operator lines by NAME
    # only, never the value.
    printf '  [dry-run] write %s:\n' "$path"
    for line in "${ours[@]}"; do
      case "${line%%=*}" in
        *TOKEN|*KEY|*SECRET|*PASSWORD) printf '    %s=<redacted>\n' "${line%%=*}" ;;
        *) printf '    %s\n' "$line" ;;
      esac
    done
    if [ ${#keep[@]} -gt 0 ]; then printf '    %s=…   (preserved operator setting)\n' "${keep[@]%%=*}"; fi
    if [ ${#overwritten[@]} -gt 0 ]; then printf '    %s=…   (changing a previously-set value)\n' "${overwritten[@]}"; fi
    return
  fi
  # Name the carried keys: a preserved-but-broken operator setting is invisible otherwise,
  # and "repair ran fine" + a box that still misbehaves points here.
  if [ ${#keep[@]} -gt 0 ]; then log "preserving ${#keep[@]} operator setting(s) in $path: ${keep[*]%%=*}"; fi
  # Same for a plain (non-seed) key that already held a DIFFERENT value: that value came
  # from an operator edit or the config page, and changing it out from under them without
  # saying so is exactly the "repair silently reverted my setting" failure mode.
  if [ ${#overwritten[@]} -gt 0 ]; then warn "changing ${#overwritten[@]} previously-set value(s) in $path: ${overwritten[*]}"; fi
  { printf '%s\n' "${ours[@]}"
    if [ ${#keep[@]} -gt 0 ]; then printf '%s\n' "${keep[@]}"; fi
  } | SUDO tee "$path" >/dev/null
  SUDO chmod 640 "$path"; SUDO chgrp "$RUN_USER" "$path" 2>/dev/null || true
}

# env_add_missing <path> <KEY=value...> — add to <path> the keys it does not have yet, with
# the values given, and leave everything already in it as it is: each key with its value,
# the comments, the order. --only engine's engine.env (#158), where write_env would not do:
# write_env rewrites every plain key it is given, so what is only an engine update would
# put an operator's KOKORO_RESERVE_FPT back to the installer's 8 or 12. Whether
# a full run should keep rewriting those values, and so who owns them, is #264; this path
# only fills gaps, so that a key a new engine release needs reaches a box without it.
# Read as write_env reads (privileged when the plain read fails; a file that cannot be read
# is not rewritten) and written as it writes (root:$RUN_USER 0640). Nothing to add: the
# file is not written. Sets ENV_ADDED to the number of keys added, and keeps the file as it
# was for env_put_back (engine_swap's rollback takes the keys back out).
ENV_ADDED=0 ENV_FILE="" ENV_BEFORE="" ENV_EXISTED=0
env_add_missing() {
  local path="$1"; shift
  local existing="" have=" " line key add=() kept=()
  ENV_ADDED=0
  if [ -e "$path" ] && [ ! -r "$path" ] && [ "$DRY_RUN" = 1 ]; then
    warn "$path is not readable by $(id -un) — a real run reads it with sudo to see which keys it has"
  elif [ -e "$path" ]; then
    existing="$(read_env_file "$path")" || die "cannot read $path — refusing to rewrite it blind and lose operator settings"
  fi
  # The keys the file sets, as systemd reads them: blank lines and #/; comments skipped,
  # whitespace around a key ignored. A key missed here would be appended again, and the
  # last assignment wins, so a miss would override the operator's value: the one failure
  # this function exists to rule out. `|| [ -n "$line" ]`: a last line without a newline.
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in
      ''|'#'*|';'*) ;;
      *=*) key="${line%%=*}"; key="${key%"${key##*[![:space:]]}"}"; have="$have$key " ;;
    esac
  done <<< "$existing"
  for line in "$@"; do
    case "$have" in *" ${line%%=*} "*) kept+=("${line%%=*}") ;; *) add+=("$line") ;; esac
  done
  if [ ${#add[@]} = 0 ]; then log "$path has every key already — left as it is"; return 0; fi
  ENV_ADDED=${#add[@]}
  if [ "$DRY_RUN" = 1 ]; then
    # By name only for a secret-bearing key, as write_env's dry run shows them.
    printf '  [dry-run] add to %s the keys it lacks:\n' "$path"
    for line in "${add[@]}"; do
      case "${line%%=*}" in
        *TOKEN|*KEY|*SECRET|*PASSWORD) printf '    %s=<redacted>\n' "${line%%=*}" ;;
        *) printf '    %s\n' "$line" ;;
      esac
    done
    if [ ${#kept[@]} -gt 0 ]; then printf '    %s=…   (kept as it is)\n' "${kept[@]}"; fi
    return 0
  fi
  log "adding ${#add[@]} key(s) $path lacks: ${add[*]%%=*}"
  ENV_FILE="$path" ENV_BEFORE="$existing" ENV_EXISTED=0
  if [ -e "$path" ]; then ENV_EXISTED=1; fi
  { if [ -n "$existing" ]; then printf '%s\n' "$existing"; fi
    printf '%s\n' "${add[@]}"
  } | SUDO tee "$path" >/dev/null
  SUDO chmod 640 "$path"; SUDO chgrp "$RUN_USER" "$path" 2>/dev/null || true
}

# env_put_back — undo env_add_missing's last write: the file as it was, or no file.
env_put_back() {
  [ "$ENV_ADDED" != 0 ] && [ -n "$ENV_FILE" ] || return 0
  if [ "$ENV_EXISTED" = 1 ]; then
    { if [ -n "$ENV_BEFORE" ]; then printf '%s\n' "$ENV_BEFORE"; fi; } | SUDO tee "$ENV_FILE" >/dev/null
  else
    SUDO rm -f "$ENV_FILE"
  fi
  ENV_ADDED=0
}

# engine_env_lines — the installer's engine.env keys and values, as ENGINE_ENV: what
# phase_services writes on every run, and what --only engine adds when engine.env lacks one.
ENGINE_ENV=()
engine_env_lines() {
  # KOKORO_RESERVE_FPT: 8 when an agent coexists (RAM shared), 12 voice-only. The code
  # default (50) OOMs an 8GB box — the installer must always set it.
  # Any agent counts, not just a NemoClaw sandbox: a host OpenClaw gateway is resident too,
  # so reserving the voice-only amount alongside it is the direction that OOMs.
  # 8 is what the appliance (8 GB Orin Nano, host OpenClaw gateway resident) runs, by the
  # owner's choice (#158). The installer wrote 6 here before, the first release's sandbox
  # figure, so each full install put the box back on 6 and it was restored to 8 by hand
  # (2026-10-15, 10-31, 11-01; #264). #167's survey of the box's RAM (2026-10-14, while
  # it ran 8 per #158) found nothing leaking: the engine holds ~4.3 GB of GPU buffers that
  # never swap, leaving ~3.2 GB for everything else, kernel included. #167 also capped the
  # gateway's JS heap at 1536 MiB (cap_openclaw_heap; it used ~380 MB, ~1 GB RSS) instead
  # of OpenClaw's half of RAM (3773 MiB), and put the voice path last for the OOM killer
  # (OOMScoreAdjust=-500 in its units). The heap cap is the host gateway's; a NemoClaw
  # sandbox is an agent too and gets 8 the same way.
  local fpt=12; [ -n "$AGENT_MODE" ] && fpt=8
  # VOX_REQUIRE_DICT_G2P: refuse to serve if the en-us G2P dict failed to load
  # rather than silently fall back to espeak-only prosody (see phase_engine).
  # Engine 1.3 ignores it; from 1.5 it turns a degraded box into a loud restart
  # loop that `teaport doctor` / journalctl names.
  # ENGINE_DELAY 480: the appliance's value since 2026-09-30 and the one engine 1.5 is
  # validated at (its batched decode is bit-identical to the unbatched one only there;
  # at 240 it scored a point worse, and --reprime's measurements are all at 480).
  ENGINE_ENV=("KOKORO_RESERVE_FPT=$fpt" "ENGINE_PORT=$ENGINE_PORT" "ENGINE_DELAY=480" "TTS_CTX=192"
              "VOX_REQUIRE_DICT_G2P=1")
}

# box_id — four hex digits that tell boxes apart: the end of the Wi-Fi MAC, the same digits
# Wi-Fi setup names its network by (teaport-ab12, wifi_setup.setup_ssid), so a box goes by
# one name on its screen, on the network and in a phone's certificate list. A box with no
# Wi-Fi takes its first wired interface's.
box_id() {
  local net="${SYSFS_NET:-/sys/class/net}" d mac="" kind
  for kind in wireless device; do
    for d in "$net"/*; do
      if [ -z "$mac" ] && [ -e "$d/$kind" ] && [ -r "$d/address" ]; then
        mac="$(tr -cd '0-9a-fA-F' < "$d/address" | tr 'A-F' 'a-f')"
        case "$mac" in ''|000000000000) mac="" ;; esac
      fi
    done
  done
  if [ ${#mac} -ge 4 ]; then printf '%s\n' "${mac: -4}"; else printf '0000\n'; fi
}

# frontdoor_name_taken <host> — another machine already answers to <host> over mDNS: it
# resolves to an address that is not one of this box's own.
frontdoor_name_taken() {
  local addrs mine a
  addrs="$(timeout 5 getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | sort -u || true)"
  mine=" $(hostname -I 2>/dev/null || true) 127.0.0.1 127.0.1.1 "
  for a in $addrs; do
    case "$mine" in *" $a "*) ;; *) return 0 ;; esac
  done
  return 1
}

# resolve_frontdoor — settle the front door's name, its local CA's name, and the operator's
# certificate if one is given, before phase_agent hands the name to OpenClaw (publicUrl)
# and phase_services writes them into brain.env (#251, #249).
#
# The host: TEAPORT_HOST when set; otherwise the name an earlier run settled on (brain.env),
# since the phones that already trust or bookmark the box use it; otherwise teaport.local,
# unless another box already answers to that, when this one becomes teaport-<id>.local. A
# second box keeping teaport.local would have avahi rename it on the wire (teaport-2.local)
# while its QR links still said teaport.local, so a phone opened the other box's page.
#
# The CA name: the existing root's, read back from it (Caddy never renames a root), or
# "Teaport <id> Local Authority" for a root Caddy has yet to make. Devices list the root as
# "<name> - <year> ECC Root"; the pages tell people to look for <name>.
resolve_frontdoor() {
  local id; id="$(box_id)"
  # Host names are case-insensitive; avahi and the QR link get one spelling.
  FRONTDOOR_HOST="$(printf '%s' "$FRONTDOOR_HOST" | tr 'A-Z' 'a-z')"
  have openssl || die "openssl is needed to read the front door's certificates: sudo apt-get install openssl"
  # The operator's certificate is kept across re-runs the way the name is: a repair run
  # without TEAPORT_TLS_CERT must not quietly put every device back on an untrusted one.
  # The Caddyfile is ours, so its tls line is the record of what an earlier run was given.
  if [ -z "${TEAPORT_TLS_CERT+set}${TEAPORT_TLS_KEY+set}" ]; then
    local tl
    tl="$(sudo cat "$CADDYFILE" 2>/dev/null | sed -n 's/^    tls "\([^"]*\)" "\([^"]*\)"$/\1 \2/p' | head -1 || true)"
    if [ -n "$tl" ]; then
      TLS_CERT="${tl%% *}" TLS_KEY="${tl#* }"
      log "keeping the front door's certificate $TLS_CERT (TEAPORT_TLS_CERT= with no value goes back to the box's own)"
    fi
  fi
  if [ -z "${TEAPORT_HOST:-}" ]; then
    local had; had="$(read_env_file "$ETC/brain.env" --lenient | grep -E '^TEAPORT_FRONTDOOR_HOST=' | tail -1 || true)"
    had="$(printf '%s' "${had#*=}" | tr 'A-Z' 'a-z')"   # lowercased like TEAPORT_HOST above
    if [ -n "$had" ]; then FRONTDOOR_HOST="$had"; fi
    # A .local name another machine answers to does not reach this box at all (avahi
    # renamed it on the wire), so keeping it only keeps phones on the other box: a saved
    # name too, such as two boxes installed before #251 that both wrote teaport.local.
    # Any other name is DNS, which may well resolve to an address not on this box (a
    # proxy, NAT): not ours to second-guess.
    if [ -z "$TLS_CERT" ] && [ "$FRONTDOOR_HOST" != "teaport-$id.local" ] \
       && [ "${FRONTDOOR_HOST%.local}" != "$FRONTDOOR_HOST" ] \
       && frontdoor_name_taken "$FRONTDOOR_HOST"; then
      log "another box already answers to $FRONTDOOR_HOST — this one is teaport-$id.local"
      FRONTDOOR_HOST="teaport-$id.local"
    fi
  fi
  # pairing.py's limit: https://<host>/p/<6 chars> in the screen's 53-byte QR code.
  if [ ${#FRONTDOOR_HOST} -gt 36 ]; then
    warn "front door name $FRONTDOOR_HOST is longer than 36 characters: its pairing link does not fit the QR code on the box's screen, so pairing a device by voice is not offered"
  fi
  if [ -n "$TLS_CERT" ]; then check_own_cert; fi
  FRONTDOOR_CA_NAME="Teaport $id Local Authority"
  if [ "$REGENERATE_CA" != 1 ]; then
    # A read, so a dry run does it too (plain sudo, as read_env_file).
    local cn
    cn="$(sudo cat "$CADDY_PKI/root.crt" 2>/dev/null | openssl x509 -noout -subject -nameopt multiline,utf8,-esc_msb 2>/dev/null \
          | sed -n 's/^ *commonName *= //p' | head -1 | tr -d '"\\' || true)"
    if [ -n "$cn" ]; then FRONTDOOR_CA_NAME="${cn% - [0-9][0-9][0-9][0-9] ECC Root}"; fi
  fi
}

# check_own_cert — refuse a certificate Caddy could not serve for $FRONTDOOR_HOST before
# anything is installed: a wrong file here takes the whole front door down at the reload.
check_own_cert() {
  local f
  for f in "$TLS_CERT" "$TLS_KEY"; do
    case "$f" in
      /*) ;; *) die "TEAPORT_TLS_CERT and TEAPORT_TLS_KEY must be absolute paths, not '$f'" ;;
    esac
    case "$f" in *[[:space:]\"{}]*) die "'$f': a certificate path with spaces, quotes or braces cannot go in the Caddyfile" ;; esac
    sudo test -f "$f" || die "$f does not exist (to go back to the box's own certificate: TEAPORT_TLS_CERT= TEAPORT_TLS_KEY= ./install.sh)"
  done
  local pem pub key out
  pem="$(sudo cat "$TLS_CERT")"
  pub="$(printf '%s\n' "$pem" | openssl x509 -noout -pubkey 2>/dev/null || true)"
  [ -n "$pub" ] || die "$TLS_CERT is not a PEM certificate"
  # -passin pass: : a passphrase-protected key fails here instead of prompting mid-install.
  key="$(sudo cat "$TLS_KEY" | openssl pkey -pubout -passin pass: 2>/dev/null || true)"
  if [ -z "$key" ] || [ "$pub" != "$key" ]; then
    die "$TLS_KEY is not the private key of $TLS_CERT, or it has a passphrase (Caddy needs it without one)"
  fi
  out="$(printf '%s\n' "$pem" | openssl x509 -noout -checkhost "$FRONTDOOR_HOST" 2>/dev/null || true)"
  contains "does match" "$out" || die "$TLS_CERT is not issued for $FRONTDOOR_HOST — set TEAPORT_HOST to the name it covers"
  if ! printf '%s\n' "$pem" | openssl x509 -noout -checkend 0 >/dev/null 2>&1; then
    warn "$TLS_CERT has expired: browsers will refuse the front door until it is replaced"
  elif ! printf '%s\n' "$pem" | openssl x509 -noout -checkend 604800 >/dev/null 2>&1; then
    warn "$TLS_CERT expires within 7 days"
  fi
}

# regenerate_ca — TEAPORT_REGENERATE_CA=1: set the box's CA aside so Caddy makes a new one
# under the current name. The cached leaf certificates go too: they are signed by the old
# CA, and Caddy would keep serving them to devices that trust only the new one. Kept, not
# deleted, so the old CA can be put back.
regenerate_ca() {
  local stamp d; stamp="$(date +%Y%m%d-%H%M%S)"
  warn "replacing the box's certificate authority (TEAPORT_REGENERATE_CA=1): every phone and laptop that trusts the box must install and trust the new one, \"$FRONTDOOR_CA_NAME\""
  SUDO systemctl stop caddy.service
  for d in "$CADDY_PKI" "$CADDY_LEAVES"; do
    if sudo test -e "$d"; then
      SUDO mv "$d" "$d.bak-$stamp"
      log "  the old one is kept at $d.bak-$stamp"
    fi
  done
}

# reissue_short_intermediate — a box whose local CA still has Caddy's 7-day intermediate
# (installed before the lifetimes above) keeps it, and every leaf under it, until it runs
# out: Caddy reads a lifetime only when it makes one. Set both aside once, keeping the
# root, so Caddy makes the long-lived ones at its start: devices that trust the root go on
# trusting the box, and the app asks to trust the new leaf one last time.
reissue_short_intermediate() {
  local dates nb na; dates="$(sudo cat "$CADDY_PKI/intermediate.crt" 2>/dev/null \
        | openssl x509 -noout -startdate -enddate 2>/dev/null || true)"
  nb="$(printf '%s\n' "$dates" | sed -n 's/^notBefore=//p')"; na="$(printf '%s\n' "$dates" | sed -n 's/^notAfter=//p')"
  if [ -z "$nb" ] || [ -z "$na" ]; then return 0; fi
  # Unparsed dates leave it alone: an empty epoch would read as a short lifetime.
  local from to; from="$(date -d "$nb" +%s 2>/dev/null || true)"; to="$(date -d "$na" +%s 2>/dev/null || true)"
  if [ -z "$from" ] || [ -z "$to" ]; then return 0; fi
  if [ $(( to - from )) -ge $(( 30 * 86400 )) ]; then return 0; fi
  local stamp f; stamp="$(date +%Y%m%d-%H%M%S)"
  log "front door: re-issuing the local CA's short-lived intermediate and site certificate (the root stays)"
  SUDO systemctl stop caddy.service
  for f in "$CADDY_PKI/intermediate.crt" "$CADDY_PKI/intermediate.key" "$CADDY_LEAVES"; do
    if sudo test -e "$f"; then SUDO mv "$f" "$f.bak-$stamp"; fi
  done
}

# write_caddyfile <path> — the front-door reverse proxy. 'tls internal' is a self-signed
# cert (offline, one-time trust on each device); TEAPORT_TLS_CERT/KEY serve a real one
# instead, for a name that resolves to the LAN IP. WebSocket upgrades (the /talk stream)
# pass through reverse_proxy automatically.
#
# The plain-HTTP block exists for phones. A browser can click through an untrusted cert; the
# OpenClaw mobile app cannot, so pairing over wss:// needs Caddy's local root installed on the
# device first — and that download cannot itself sit behind the cert it is meant to fix. Hence
# exactly one path on :80, everything else redirected. The same path is served over HTTPS
# too (the teaport_ca snippet, imported by both blocks): browsers that upgrade http:// links
# on their own (Brave by default, Chrome's and Safari's HTTPS upgrades) otherwise land on
# https://<host>/teaport-ca.crt, which the catch-all below handed to the dashboard -- an
# HTML page where the certificate should be (seen on the box, 2026-11-01).
#
# It is served straight out of Caddy's PKI directory rather than copied: a copy would go stale
# silently if the CA were ever regenerated. The path matcher is
# exact and rewrites to a fixed file, so root.key next to it stays unreachable.
#
# Two brain prefixes go through the front door: /p/*, the short pairing links the OLED
# shows as a QR code (#243), which redirect to OpenClaw's one-time owner handoff; and
# /settings and /settings/*, the owner's Settings (#279): the code links "open my
# settings" shows, and the config page with its routes, which take the session a code
# opens in place of the token (settings_access.py). Settings is served on a port of its
# own, $SETTINGS_PORT (TEAPORT_SETTINGS_PORT, written to brain.env): another origin than
# the gateway's, whose pages must not share the session; /settings on 80 and 443 only
# redirects there. Only those, and only over HTTPS (a
# code is a bearer secret until used, and the session cookie is Secure); the rest of the
# brain (/talk, the config page at /config) stays off the front door, and the http block
# above never matches them, so a cleartext hit is just redirected to https first. The
# brain's own port is open to the LAN (/talk), so the brain itself answers
# these only to a loopback peer that says X-Forwarded-Proto https -- this proxy, which
# sets that header itself -- and gives anything else a 404 (pairing.py via_front_door).
#
# The global block names the local CA for this box (#251): every Caddy root is otherwise
# "Caddy Local Authority", and a phone that trusts two boxes lists two identical entries.
# Caddy uses the name only when it creates a root; an existing root keeps its own
# (resolve_frontdoor reads it back so the pages name what the device shows), and
# TEAPORT_REGENERATE_CA=1 makes a new one. With the operator's own certificate (#249) the
# site serves that instead of the local CA's; the local CA and its download stay for a
# device that reaches the box some other way.
#
# Lifetimes: the site certificate lasts LEAF_LIFETIME under an intermediate of
# INTERMEDIATE_LIFETIME, not Caddy's 12 hours under 7 days. The OpenClaw apps pin the
# site certificate itself (a SHA-256 of the leaf, trust on first use), so every renewal
# stopped the iPhone app with "Gateway certificate changed" until someone trusted the new
# one -- with Caddy's defaults, every 8 hours. Caddy renews at a third of the lifetime left,
# so a year-long leaf changes about every 8 months. A year also stays inside what Apple
# accepts from a root a user installed (825 days).
#
# Written through write_root: root's and 0644, not whatever sudo's umask makes it (#199).
# The package's caddy.service runs Caddy as User=caddy, and a 0600 Caddyfile stops it from
# starting or reloading. Nothing in it needs hiding: the operator's certificate and key go
# in by path only (phase_frontdoor checks that the caddy user can read them).
write_caddyfile() {
  local path="$1" tls=("    tls {" "        issuer internal {" "            lifetime $LEAF_LIFETIME"
                       "        }" "    }")
  if [ -n "$TLS_CERT" ]; then tls=("    tls \"$TLS_CERT\" \"$TLS_KEY\""); fi
  if [ "$DRY_RUN" = 1 ]; then
    local how="local CA, leaf $LEAF_LIFETIME"; if [ -n "$TLS_CERT" ]; then how="own certificate $TLS_CERT"; fi
    printf '  [dry-run] write %s  (%s -> 127.0.0.1:%s, /p/* -> brain 127.0.0.1:%s, Settings on :%s, %s, local CA "%s", + /teaport-ca.crt over http and https)\n' "$path" "$FRONTDOOR_HOST" "$GATEWAY_PORT" "$BRAIN_PORT" "$SETTINGS_PORT" "$how" "$FRONTDOOR_CA_NAME"; return
  fi
  write_root "$path" "$(printf '%s\n' \
    "# teaport front door — rendered by install.sh. Edit the tls/upstream here, then:" \
    "#   sudo systemctl reload caddy.service" \
    "" \
    "{" \
    "    # The box's own certificate authority, named for this box. Caddy names a root only" \
    "    # when it creates one: an existing root keeps its name." \
    "    pki {" \
    "        ca local {" \
    "            name \"$FRONTDOOR_CA_NAME\"" \
    "            intermediate_lifetime $INTERMEDIATE_LIFETIME" \
    "        }" \
    "    }" \
    "}" \
    "" \
    "# The local CA, so a phone can trust us before it connects: served by both blocks." \
    "(teaport_ca) {" \
    "    handle /teaport-ca.crt {" \
    "        root * $CADDY_PKI" \
    "        rewrite * /root.crt" \
    "        file_server" \
    "        header Content-Type application/x-x509-ca-cert" \
    "        header Content-Disposition \"attachment; filename=teaport-ca.crt\"" \
    "    }" \
    "}" \
    "" \
    "# Cleartext, one path only: the CA above; everything else goes to https -- the" \
    "# owner's Settings to its own port (the address the box reads out has no port)." \
    "http://$FRONTDOOR_HOST {" \
    "    import teaport_ca" \
    "    @settings path /settings /settings/*" \
    "    handle @settings {" \
    "        redir https://{host}:$SETTINGS_PORT{uri}" \
    "    }" \
    "    handle {" \
    "        redir https://{host}{uri} permanent" \
    "    }" \
    "}" \
    "" \
    "$FRONTDOOR_HOST {" \
    "${tls[@]}" \
    "    import teaport_ca" \
    "    # Pairing short links (#243) go to the brain; nothing else of the brain is exposed" \
    "    # here. The owner's Settings (#279) has an origin of its own, below." \
    "    handle /p/* {" \
    "        reverse_proxy 127.0.0.1:$BRAIN_PORT" \
    "    }" \
    "    @settings path /settings /settings/*" \
    "    handle @settings {" \
    "        redir https://{host}:$SETTINGS_PORT{uri}" \
    "    }" \
    "    handle {" \
    "        reverse_proxy 127.0.0.1:$GATEWAY_PORT" \
    "    }" \
    "}" \
    "" \
    "# The owner's Settings (#279): another port, another origin, so no page the gateway" \
    "# serves above shares it. Only /settings; the brain checks the port too." \
    "$FRONTDOOR_HOST:$SETTINGS_PORT {" \
    "${tls[@]}" \
    "    import teaport_ca" \
    "    # Nothing may frame Settings (the brain says so too)." \
    "    header Content-Security-Policy \"frame-ancestors 'none'\"" \
    "    header X-Frame-Options DENY" \
    "    @settings path /settings /settings/*" \
    "    handle @settings {" \
    "        reverse_proxy 127.0.0.1:$BRAIN_PORT" \
    "    }" \
    "    handle {" \
    "        respond 404" \
    "    }" \
    "}")"
}

# set_avahi_hostname <name> — publish <name>.local over mDNS. Best-effort: if avahi's conf
# isn't where we expect, the box still answers to its default $(hostname).local.
set_avahi_hostname() {
  local name="$1" conf=/etc/avahi/avahi-daemon.conf
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] set avahi host-name=%s in %s\n' "$name" "$conf"; return; fi
  [ -f "$conf" ] || { warn "avahi conf not found ($conf) — mDNS name left at default"; return; }
  if grep -qE '^[[:space:]]*#?[[:space:]]*host-name=' "$conf"; then
    SUDO sed -i -E "s|^[[:space:]]*#?[[:space:]]*host-name=.*|host-name=$name|" "$conf"
  else
    SUDO sed -i -E "s|^\[server\]|[server]\nhost-name=$name|" "$conf"
  fi
}

phase_services() {
  log "systemd units + env files"
  SUDO mkdir -p "$ETC"
  # GATEWAY_TOKEN protects the local /talk port; the same value is wired into the plugin
  # config (phase_agent) — resolve it once for both sides.
  resolve_gateway_token

  engine_env_lines
  write_env "$ETC/engine.env" "${ENGINE_ENV[@]}"
  # TEAPORT_AGENT tells the brain what this fork already knows: with a gateway (host
  # OpenClaw or the sandbox) it advertises the gateway tools, runs memory recall and takes
  # OpenClaw's voice context (identity and profile); voice-only it advertises the local
  # tools alone and reads
  # TEAPORT_PERSONA_FILE. Before this the brain assumed a gateway on every box and a
  # voice-only one spent every turn on tools and a recall that had nothing to answer.
  #
  # Asymmetric on purpose. A detected gateway is positive evidence and is written plain,
  # so installing OpenClaw later and re-running is all it takes to turn the tools on. NOT
  # detecting one is not evidence of anything: detect_agent looks in $HOME, and a repair
  # run as root or as another account (both supported — see write_env) sees a different
  # home, would write `none`, and the next restart would silently strip a working box of
  # its tools. So the voice-only path only SEEDS `none` — a first install gets it, an
  # existing value (the installer's or the config page's) survives, and a box that truly
  # loses its gateway is switched on the page.
  local agent=("?TEAPORT_AGENT=none")
  if [ -n "$AGENT_MODE" ]; then
    # TEAPORT_FRONTDOOR_HOST: the name the pairing links (#243) are built on; phase_frontdoor
    # fronts exactly these installs (any agent), so it is written exactly here, with what
    # the pairing pages say about trusting the box (resolve_frontdoor).
    local cert=local; if [ -n "$TLS_CERT" ]; then cert=trusted; fi
    agent=("TEAPORT_AGENT=openclaw" "OPENCLAW_GATEWAY_URL=http://127.0.0.1:$GATEWAY_PORT"
           "TEAPORT_FRONTDOOR_HOST=$FRONTDOOR_HOST" "TEAPORT_SETTINGS_PORT=$SETTINGS_PORT"
           "TEAPORT_FRONTDOOR_CERT=$cert"
           "TEAPORT_FRONTDOOR_CA_NAME=$FRONTDOOR_CA_NAME")
  fi
  write_env "$ETC/brain.env" \
    "BRAIN_PORT=$BRAIN_PORT" \
    "${LLM_URL_SEED}LLM_BASE_URL=$LLM_BASE_URL" "${LLM_MODEL_SEED}LLM_MODEL=$LLM_MODEL" \
    "TEAPORT_URL=ws://127.0.0.1:$ENGINE_PORT/v1/realtime" \
    "${agent[@]}" \
    "TEAPORT_PERSONA_FILE=$SECRETS/persona.md" \
    "GATEWAY_TOKEN=$GATEWAY_TOKEN" "MALLOC_ARENA_MAX=2" "HF_HUB_OFFLINE=1"

  # TEAPORT_PERSONA_FILE points here, but nothing else ever creates it: without this a
  # voice-only box (and a gateway box whose gateway cannot answer) reads a file that
  # does not exist, load_persona() silently falls through to the baked-in
  # FALLBACK_PERSONA, and the box quietly ships the generic assistant instead of the
  # editable one the docs say it reads (docs/src/content/docs/guides/configuration.md,
  # "Where secrets live"; reference/settings.md, TEAPORT_PERSONA_FILE). Seed it once
  # with that same text — never overwrite an existing file, so an operator's persona
  # edit survives every repair.
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] write %s if absent (starter persona text)\n' "$SECRETS/persona.md"
  elif [ ! -f "$SECRETS/persona.md" ]; then
    printf '%s\n' \
      "You are a friendly voice assistant that runs on a small local device: a" \
      "local speech engine hears the user, a language model thinks, and a local" \
      "voice speaks. You are warm, concise, and genuinely helpful — the same" \
      "assistant whether the user reaches you by voice or text." \
      > "$SECRETS/persona.md"
    chmod 600 "$SECRETS/persona.md"
    log "starter persona -> $SECRETS/persona.md (edit it any time; a repair never overwrites it)"
  fi

  render_unit teaport-engine.service.in teaport-engine.service
  render_unit teaport-brain.service.in  teaport-brain.service
  if [ -n "$SANDBOX" ]; then render_unit teaport-sandbox-recover.service.in teaport-sandbox-recover.service; fi

  SUDO systemctl daemon-reload
  # `enable --now` starts a stopped unit but does NOT restart a running one (see the avahi
  # note in phase_frontdoor) — and a repair run exists precisely to change what a running
  # engine/brain loaded at startup: the bin/g2p dict, engine.env/brain.env, the brain code.
  # Without the restart, the repair "succeeds" and changes nothing until the next reboot —
  # and phase_verify's journal check reads the OLD startup's lines and passes. `restart`
  # also starts a stopped unit, so a first install behaves the same.
  SUDO systemctl enable teaport-engine.service teaport-brain.service
  # The brain package is installed (phase_brain); here, with everything it needs on disk
  # (units and env above, the SIP unit in phase_sip, which main runs first), the engine and
  # teaport-brain -- the phone line's front-end included -- restart together. The brain
  # unit is Type=notify, so the restart returns once the brain serves. A running SIP
  # gateway is not restarted: the brain reconnects to it (teaport-sip#3 replays a call in
  # progress).
  SUDO systemctl restart teaport-engine.service teaport-brain.service
  brain_deb_prune
  brain_retire_old_tools
  brain_retire_old_venvs
  if [ -n "$SANDBOX" ]; then SUDO systemctl enable --now teaport-sandbox-recover.service; fi
  brain_sudoers
}

# Caddy isn't in the stock Ubuntu repos — add its official Cloudsmith apt repo (idempotent),
# then install. Keeps the TLS front door patched by apt instead of a frozen, sha-pinned R2 binary.
# The key and the list are root's and 0644, set rather than left to sudo's umask (#199): apt
# checks the repo's signature as its _apt user, which a 0600 key shuts out (the repo reads
# as unsigned, and apt-get update fails), and apt run by anyone but root cannot open a 0600
# list. Caddy's own instructions chmod o+r both for the same reason.
CADDY_KEYRING=/usr/share/keyrings/caddy-stable-archive-keyring.gpg   # the list's signed-by= names it
CADDY_APT_LIST=/etc/apt/sources.list.d/caddy-stable.list
install_caddy_apt() {
  local tmp repo
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] add Caddy apt repo (key and list root:root 0644) + apt-get install -y caddy\n'; return; fi
  SUDO apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  if [ ! -f "$CADDY_KEYRING" ]; then
    # Dearmored into a temp and installed from there (it is binary: no write_root), so the
    # key is there only when the whole fetch worked.
    tmp="$(mktemp)"
    if ! curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | SUDO gpg --dearmor > "$tmp"; then
      rm -f "$tmp"; die "cannot fetch the Caddy repo key"
    fi
    SUDO install -D -m 0644 -o root -g root "$tmp" "$CADDY_KEYRING" || { rm -f "$tmp"; die "cannot install the Caddy repo key"; }
    rm -f "$tmp"
  fi
  if [ ! -f "$CADDY_APT_LIST" ]; then
    repo="$(curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt')" || die "cannot fetch the Caddy repo list"
    write_root "$CADDY_APT_LIST" "$repo" || die "cannot write the Caddy repo list"
  fi
  # The modes are set again on every run, not only when the files are written: a run before
  # #199 under umask 077 left both 0600, and the -f checks above skip them on the re-run
  # that is meant to repair the box. (The units and the Caddyfile need no such step:
  # write_root's install replaces the file and sets its mode each time.)
  { SUDO chown root:root "$CADDY_KEYRING" "$CADDY_APT_LIST" && SUDO chmod 0644 "$CADDY_KEYRING" "$CADDY_APT_LIST"; } \
    || die "cannot set $CADDY_KEYRING and $CADDY_APT_LIST to root:root 0644"
  SUDO apt-get update -qq
  SUDO apt-get install -y caddy
  command -v caddy >/dev/null || die "caddy not installed after apt"
}

# The browser front door: HTTPS + mDNS so a LAN browser reaches the gateway in a secure
# context — getUserMedia (mic) and the gateway's WebCrypto device identity only work over
# HTTPS or localhost, so plain http://<ip> loads the page but the mic and pairing are dead.
# Only meaningful when a gateway exists to front (a sandbox was wired in phase_agent); a
# voice-only install has no gateway, so skip.
phase_frontdoor() {
  # Any agent means an OpenClaw gateway on 127.0.0.1:$GATEWAY_PORT — sandboxed or on the
  # host, the Caddy config is the same. Only a voice-only install has nothing to front.
  if [ -z "$AGENT_MODE" ]; then log "front door: skipped (no gateway — voice-only install)"; return 0; fi
  log "front door: Caddy TLS (:443 -> gateway :$GATEWAY_PORT) + mDNS $FRONTDOOR_HOST"
  # Caddy is third-party and network-facing: install from its official apt repo so security
  # fixes flow through the package manager. The deb ships its own caddy.service (runs as user
  # caddy, reads /etc/caddy/Caddyfile) — we just drop our config there and reload.
  install_caddy_apt
  # The caddy user exists only from here, so whether it can read the operator's files is
  # checked here and not in check_own_cert. Unreadable, Caddy would not start at all.
  if [ -n "$TLS_CERT" ] && [ "$DRY_RUN" != 1 ]; then
    local f
    for f in "$TLS_CERT" "$TLS_KEY"; do
      SUDO runuser -u caddy -- test -r "$f" || die "Caddy (user caddy) cannot read $f: give the caddy user or group read access, then re-run"
    done
  fi
  # The operator's own certificate (#249) uses no intermediate of ours: nothing to reissue.
  if [ "$REGENERATE_CA" = 1 ]; then regenerate_ca
  elif [ -z "$TLS_CERT" ]; then reissue_short_intermediate; fi
  # mDNS: publish $FRONTDOOR_HOST so LAN browsers resolve it with no DNS setup.
  SUDO apt-get install -y avahi-daemon
  write_caddyfile "$CADDYFILE"
  # mDNS publishes single-label .local names only (avahi refuses a dotted host-name and
  # does not start): any other name is the operator's DNS to resolve.
  case "$FRONTDOOR_HOST" in
    .local|*.*.local|*..*) warn "$FRONTDOOR_HOST is not a name mDNS can publish — make your DNS resolve it to this box" ;;
    *.local) set_avahi_hostname "${FRONTDOOR_HOST%.local}" ;;
    *) log "front door name $FRONTDOOR_HOST is not .local: your DNS must resolve it to this box" ;;
  esac
  SUDO systemctl enable --now avahi-daemon caddy.service
  # `enable --now` starts a stopped unit but does NOT restart a running one, and avahi reads
  # host-name only at startup. The apt-get above tends to leave avahi already running, so
  # without an explicit restart it keeps publishing its default name and $FRONTDOOR_HOST
  # never resolves — the documented browser entry point silently does not exist.
  SUDO systemctl restart avahi-daemon
  SUDO systemctl reload caddy.service 2>/dev/null || SUDO systemctl restart caddy.service
  # Confirm the CA is actually downloadable. A 404 here is the same silent dead end as the
  # avahi case above: everything looks installed, and pairing a phone is simply impossible.
  # Poll — 'tls internal' provisions the CA when caddy first loads the site, which can trail
  # the reload by a moment.
  # Skip under --dry-run: nothing above actually ran, so this would probe whatever the box
  # already has and report on that instead of on the install being previewed.
  # (Written as an if, not `[ ] && return`: under set -e that construct returns non-zero when
  # the test fails, which is how write_env aborted a real install once.)
  if [ "$DRY_RUN" = 1 ]; then return 0; fi
  local ca_ok=0 i
  for i in $(seq 1 10); do
    if curl -fsS -o /dev/null -H "Host: $FRONTDOOR_HOST" http://127.0.0.1/teaport-ca.crt 2>/dev/null; then ca_ok=1; break; fi
    sleep 1
  done
  if [ "$ca_ok" = 1 ]; then
    log "front door up — root cert at http://$FRONTDOOR_HOST/teaport-ca.crt"
  else
    warn "http://$FRONTDOOR_HOST/teaport-ca.crt does not serve (looked in $CADDY_PKI)"
    warn "browsers still work; phones cannot install the root, so app pairing over wss:// will fail"
  fi
}

# The Discord voice bridge (opt-in). The repo ships bridge/discord but nothing runs it, so a
# plain voice/browser install stays Discord-free. Enable when TEAPORT_ENABLE_BRIDGE=1 or a bot
# token is already present. Discord voice is RTP/UDP and the NemoClaw sandbox is TCP-only, so
# the bridge runs on the host and owns only the media leg, piping audio to the brain's /talk WS.
phase_bridge() {
  local tokfile="$SECRETS/discord_bot_token"
  local have_token=0
  { [ -n "${DISCORD_BOT_TOKEN:-}" ] || [ -f "$tokfile" ]; } && have_token=1
  if [ "${TEAPORT_ENABLE_BRIDGE:-0}" != 1 ] && [ "$have_token" = 0 ]; then
    log "discord bridge: not configured — skipped (TEAPORT_ENABLE_BRIDGE=1 to add it)"
    return 0
  fi
  log "discord bridge: install + enable (host media leg -> brain /talk)"
  have node || die "the Discord bridge needs Node >= 22 on the host (not found)"

  local guild="${TEAPORT_BRIDGE_GUILD_ID:-}" follow="${TEAPORT_BRIDGE_FOLLOW_USER_ID:-}"
  local token="${DISCORD_BOT_TOKEN:-}"
  # Idempotent re-run: reuse guild/follow already in bridge.env when not re-supplied via env,
  # so a plain repair run doesn't blank a previously-configured bridge (same as the gateway token).
  if [ -f "$ETC/bridge.env" ]; then
    [ -z "$guild" ]  && guild="$(sed -n 's/^BRIDGE_GUILD_ID=//p' "$ETC/bridge.env" 2>/dev/null | head -1)"
    [ -z "$follow" ] && follow="$(sed -n 's/^BRIDGE_FOLLOW_USER_ID=//p' "$ETC/bridge.env" 2>/dev/null | head -1)"
  fi
  if [ -t 0 ] && [ "$DRY_RUN" != 1 ]; then
    [ -z "$guild" ]  && read -r -p 'Discord server (guild) id: ' guild
    [ -z "$follow" ] && read -r -p 'Discord user id to follow into voice: ' follow
    [ -z "$token" ] && [ ! -f "$tokfile" ] && { read -r -s -p 'Discord bot token: ' token; echo; }
  fi
  # Token -> secrets dir (mode 600), never in the unit or env file; the env file only points
  # the bridge at it via DISCORD_BOT_TOKEN_FILE.
  if [ -n "$token" ]; then
    if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] write %s (mode 600)\n' "$tokfile";
    else run mkdir -p "$SECRETS"; printf '%s' "$token" > "$tokfile"; chmod 600 "$tokfile"; fi
  fi

  # Source: a local checkout, else the manifest-pinned clone (same repo/tag as the brain).
  local src="$BRIDGE_SRC"
  [ -z "$src" ] && [ -d "$HERE/bridge/discord" ] && src="$HERE/bridge/discord"
  if [ -z "$src" ]; then
    local repo tag work; repo="$(mget brain.repo)"; tag="$(mget brain.tag)"
    work="$STATE/bridge-src"; SUDO mkdir -p "$STATE"; SUDO chown "$RUN_USER" "$STATE"
    # Same re-run == repair fix as resolve_brain_src: `git clone` dies on a non-empty destination.
    # This clone is pure staging too — index.js/env.js/package.json are copied out below.
    if [ -e "$work" ]; then run rm -rf "$work"; fi
    run git clone --depth 1 --branch "$tag" "https://github.com/$repo.git" "$work"
    src="$work/bridge/discord"
  fi
  SUDO mkdir -p "$PREFIX/bridge"; SUDO chown -R "$RUN_USER" "$PREFIX/bridge"
  run cp "$src/index.js" "$src/env.js" "$src/package.json" "$PREFIX/bridge/"
  # --omit=dev; the host toolchain builds @discordjs/opus from source (JetPack ships gcc/make/python3).
  run npm install --omit=dev --no-audit --no-fund --prefix "$PREFIX/bridge"

  write_env "$ETC/bridge.env" \
    "BRIDGE_GUILD_ID=$guild" "BRIDGE_FOLLOW_USER_ID=$follow" \
    "BRAIN_URL=ws://127.0.0.1:$BRAIN_PORT/talk" \
    "DISCORD_BOT_TOKEN_FILE=$tokfile"
  render_unit teaport-discord-bridge.service.in teaport-discord-bridge.service
  SUDO systemctl daemon-reload
  # Enable + start only when fully configured; otherwise install the unit inert so the operator
  # can fill $ETC/bridge.env + the token and start it, with no crash-loop on missing config.
  if [ -n "$guild" ] && [ -n "$follow" ] && { [ -n "$token" ] || [ -f "$tokfile" ]; }; then
    # restart, not `enable --now`: a repair updates bridge code + env (see phase_services).
    SUDO systemctl enable teaport-discord-bridge.service
    SUDO systemctl restart teaport-discord-bridge.service
  else
    SUDO systemctl enable teaport-discord-bridge.service
    warn "bridge installed but not started — set BRIDGE_GUILD_ID/BRIDGE_FOLLOW_USER_ID in $ETC/bridge.env + the token in $tokfile, then: systemctl start teaport-discord-bridge"
  fi
}

# --- one brain process (issue #58): retiring teaport-sip-brain -----------------------
# The SIP front-end now runs inside teaport-brain, so the separate teaport-sip-brain unit
# goes. In three steps, so a failed --only brain swap can put the old pair back:
#   sip_brain_detach   before the swap: the per-path tuning the box kept in drop-ins is
#                      carried into brain.env (migrate_brain_dropins: the new brain must
#                      start with it, and must not start with the Talk-only drop-in, which
#                      would now reach calls too); the gateway unit loses its Upholds=/
#                      PartOf= on the old unit (first: PartOf= would otherwise stop the
#                      gateway with it and drop a call); the old unit is stopped. Its file,
#                      its drop-ins and the gateway's unit as it was are all kept.
#   sip_brain_retire   once the new brain is verified: the old unit is disabled and
#                      removed, and its drop-in dir renamed *.retired-<date>.
#   sip_brain_restore  on an automatic rollback: the gateway unit and the Talk drop-ins as
#                      they were, the old unit started again if it was running. Loudly.
# A running gateway stays up throughout; the restarted brain connects to it (a call in
# progress is replayed to it, teaport-sip#3). Nothing to do once the unit is gone. The full
# install runs detach + retire in phase_sip (it has no automatic rollback).
SIP_BRAIN_UNIT=/etc/systemd/system/teaport-sip-brain.service
SIP_GW_UNIT=/etc/systemd/system/teaport-sip.service
SIP_BRAIN_DETACHED=0 SIP_BRAIN_WAS_ACTIVE=0 SIP_GW_SAVED=""
DROPINS_PARKED=()   # Talk drop-ins renamed by migrate_brain_dropins (sip_brain_restore undoes)
sip_brain_detach() {
  [ -e "$SIP_BRAIN_UNIT" ] || return 0
  log "one brain process: detaching teaport-sip-brain (phone calls become teaport-brain's own)"
  warn "rolling the brain back past this release by hand (--rollback brain) leaves the phone line without a brain: the old release needs the old units, from that release's install.sh"
  # An earlier run that stopped half-way (killed, or from before this trap) may have left
  # Talk drop-ins parked and the gateway unit stripped: put both back first, so this run
  # starts from the pair as it was. (A dry run cannot move them; the migration step is
  # told to read the parked ones as if they were back.)
  local f
  for f in /etc/systemd/system/teaport-brain.service.d/*.conf.retired-pending; do
    [ -e "$f" ] || continue
    log "  putting back $f, left parked by an earlier run that stopped half-way"
    SUDO mv "$f" "${f%.retired-pending}"
  done
  if [ -f "$SIP_GW_UNIT.pre-detach" ]; then
    log "  putting back $SIP_GW_UNIT as an earlier run that stopped half-way had it"
    SUDO mv "$SIP_GW_UNIT.pre-detach" "$SIP_GW_UNIT"
  fi
  if systemctl is-active --quiet teaport-sip-brain.service 2>/dev/null; then SIP_BRAIN_WAS_ACTIVE=1; fi
  # From the first change on, whatever ends this run before sip_brain_retire (a die,
  # Ctrl-C, a kill while the old unit stops, a failed swap's own rollback) puts the old
  # pair back; retire clears it.
  SIP_BRAIN_DETACHED=1
  trap sip_brain_restore EXIT
  migrate_brain_dropins || die "the drop-in tuning could not be carried into $ETC/brain.env — nothing was swapped, the old SIP brain is untouched"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] drop Upholds=/PartOf=teaport-sip-brain from %s; stop %s (kept until the new brain is verified)\n' "$SIP_GW_UNIT" "$SIP_BRAIN_UNIT"
    return 0
  fi
  if [ -f "$SIP_GW_UNIT" ] && grep -Eq '^(Upholds|PartOf)=teaport-sip-brain\.service' "$SIP_GW_UNIT"; then
    # Kept beside the unit, not in /tmp: a run killed after this finds it (above).
    SUDO cp -p "$SIP_GW_UNIT" "$SIP_GW_UNIT.pre-detach"
    SIP_GW_SAVED="$SIP_GW_UNIT.pre-detach"
    SUDO sed -i -E '/^(Upholds|PartOf)=teaport-sip-brain\.service/d' "$SIP_GW_UNIT"
  fi
  SUDO systemctl daemon-reload
  SUDO systemctl stop teaport-sip-brain.service 2>/dev/null || true
}

sip_brain_restore() {
  [ "$SIP_BRAIN_DETACHED" = 1 ] || return 0
  warn "putting the old SIP brain and the Talk drop-ins back (this run did not finish moving to one brain process, and the brain before it has no phone line of its own)"
  if [ "$DRY_RUN" = 1 ]; then return 0; fi
  if [ -n "$SIP_GW_SAVED" ] && [ -f "$SIP_GW_SAVED" ]; then
    SUDO mv "$SIP_GW_SAVED" "$SIP_GW_UNIT"; SIP_GW_SAVED=""
  fi
  local f
  for f in "${DROPINS_PARKED[@]}"; do SUDO mv "$f.retired-pending" "$f" || warn "could not put $f back"; done
  DROPINS_PARKED=()
  SUDO systemctl daemon-reload
  if [ "$SIP_BRAIN_WAS_ACTIVE" = 1 ]; then
    SUDO systemctl start teaport-sip-brain.service \
      || warn "teaport-sip-brain did not start again — the phone line answers into silence: systemctl status teaport-sip-brain"
  fi
  SIP_BRAIN_DETACHED=0
}

sip_brain_retire() {
  trap - EXIT
  [ -e "$SIP_BRAIN_UNIT" ] || return 0
  local stamp f d=/etc/systemd/system/teaport-sip-brain.service.d
  stamp="$(date +%Y%m%d)"
  log "retiring teaport-sip-brain: removing its unit"
  SUDO systemctl disable --now teaport-sip-brain.service 2>/dev/null || true
  SUDO rm -f "$SIP_BRAIN_UNIT"
  if [ -d "$d" ]; then log "  $d -> $d.retired-$stamp"; SUDO mv "$d" "$d.retired-$stamp"; fi
  for f in "${DROPINS_PARKED[@]}"; do SUDO mv "$f.retired-pending" "$f.retired-$stamp"; done
  DROPINS_PARKED=()
  SUDO systemctl daemon-reload
  SUDO systemctl reset-failed teaport-sip-brain.service 2>/dev/null || true
  if [ -n "$SIP_GW_SAVED" ]; then SUDO rm -f "$SIP_GW_SAVED"; SIP_GW_SAVED=""; fi
  SIP_BRAIN_DETACHED=0
}

# migrate_brain_dropins — the per-path tuning an appliance kept in systemd drop-ins, carried
# into brain.env as per-front-end settings, once, when the two brain processes become one:
#   teaport-sip-brain.service.d/*.conf  (the phone path): SIP_* keys as they are; the
#     per-front-end turn-taking knobs (endpointing.turn_settings) as their SIP_ forms
#     (ENDPOINT_STOP_SECS -> SIP_ENDPOINT_STOP_SECS, TEAPORT_INTERRUPT_MIN_WORDS ->
#     SIP_INTERRUPT_MIN_WORDS, ...). sip_brain_retire renames the dir afterwards.
#   teaport-brain.service.d/*.conf  (until now the Talk path only): the same knobs as their
#     TALK_ forms, since the drop-in would now reach phone calls too. A drop-in that holds
#     nothing but such settings is parked (renamed *.retired-pending, then *.retired-<date>
#     by sip_brain_retire) so it stops applying; the env file it loaded is left on disk,
#     unused. One that holds anything else stays, with a warning naming it.
# What took effect is what moves, as systemd resolved it: across drop-ins and within one
# the last value wins; an EnvironmentFile= value beats an Environment= one; and an
# Environment= value whose key brain.env (the unit's EnvironmentFile) also sets was never
# in effect, so it is not moved. A key brain.env already has is left as it is; an empty
# value is not moved. Anything without a per-front-end form is named, not moved. One
# brain.env key is copied too: a VAD_SAMPLE_RATE there reached phone calls, which now have
# SIP_VAD_SAMPLE_RATE (default 8000) instead, so it is written as that when brain.env has
# no SIP_VAD_SAMPLE_RATE and no drop-in gave the phone one (issue #215). The log
# names keys and files only, never values (a drop-in can hold a secret).
# The whole brain.env update is one privileged Python step: read, append the missing keys
# (after a newline if the file lacks a final one), write a temp file beside it and
# os.replace it in, with the mode and owner kept and a timestamped backup left. Any
# failure there (a file that cannot be read, sudo refused) fails the step, and with it the
# detach: --only brain then stops before the swap.
migrate_brain_dropins() {
  local plan sep=$'\x1f'
  plan="$(sudo_python - /etc/systemd/system/teaport-sip-brain.service.d \
            /etc/systemd/system/teaport-brain.service.d "$ETC/brain.env" "$DRY_RUN" <<'PY'
import glob, os, shlex, sys, time
os.umask(0o077)  # nothing this step creates is readable by others, even half-written
PER = {"ENDPOINT_STOP_SECS": "ENDPOINT_STOP_SECS", "SMARTTURN_STOP_SECS": "SMARTTURN_STOP_SECS",
       "SMARTTURN_COMPLETE_THRESHOLD": "SMARTTURN_COMPLETE_THRESHOLD",
       "TEAPORT_INTERRUPT_MIN_WORDS": "INTERRUPT_MIN_WORDS", "VAD_SAMPLE_RATE": "VAD_SAMPLE_RATE"}
SEP = "\x1f"
sipd, talkd, env_path, dry = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
brain_env = os.path.realpath(env_path)

def say(*fields):
    print(SEP.join(fields))

def env_file(path):
    out = {}
    for l in open(path, encoding="utf-8", errors="replace"):
        l = l.strip()
        if l and not l.startswith("#") and "=" in l:
            k, v = l.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out

existing_text = open(env_path, encoding="utf-8").read() if os.path.exists(env_path) else ""
existing = env_file(env_path) if existing_text else {}

def read(conf):
    """(Environment= values, EnvironmentFile= values, only_env), last value winning."""
    plain, files, only_env = {}, {}, True
    for n, line in enumerate(open(conf, encoding="utf-8", errors="replace"), 1):
        line = line.strip()
        if not line or line.startswith(("#", ";")) or line == "[Service]":
            continue
        if line.startswith("Environment="):
            try:
                toks = shlex.split(line[len("Environment="):])
            except ValueError:
                say("BAD", f"{conf}:{n}")
                only_env = False
                continue
            for tok in toks:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    plain[k] = (v, conf)
        elif line.startswith("EnvironmentFile="):
            path = line[len("EnvironmentFile="):].strip().lstrip("-")
            if os.path.realpath(path) == brain_env:
                only_env = False
            elif os.path.isfile(path):
                for k, v in env_file(path).items():
                    files[k] = (v, path)
        else:
            only_env = False
    return plain, files, only_env

add = {}
for prefix, d in (("SIP", sipd), ("TALK", talkd)):
    merged = {}        # key -> (value, source, from_file), systemd's precedence
    convs = []
    confs = glob.glob(os.path.join(d, "*.conf"))
    if dry:
        # A dry run cannot put back what an earlier run left parked (sip_brain_detach
        # does that for real): read those as the drop-ins they are about to be again.
        confs += [c[:-len(".retired-pending")]
                  for c in glob.glob(os.path.join(d, "*.conf.retired-pending"))]
    for conf in sorted(set(confs)):
        plain, files, only_env = read(conf if os.path.exists(conf)
                                      else conf + ".retired-pending")
        for k, (v, src) in plain.items():
            if k not in merged or not merged[k][2]:
                merged[k] = (v, src, False)
        for k, (v, src) in files.items():
            merged[k] = (v, src, True)
        convs.append((conf, set(plain) | set(files), only_env))
    moved, rest = set(), set()
    for k, (v, src, from_file) in merged.items():
        if prefix == "SIP" and k.startswith("SIP_"):
            target = k
        elif k in PER:
            target = f"{prefix}_{PER[k]}"
        else:
            say("SKIP", k, src); rest.add(k); continue
        if not from_file and k in existing:
            say("SHADOWED", k, src); moved.add(k); continue    # brain.env won in systemd
        if v == "":
            say("EMPTY", k, src); moved.add(k); continue
        if target in existing or target in add:
            say("HAVE", target, src); moved.add(k); continue
        add[target] = v
        say("SET", target, f"{src} ({k})" if target != k else src)
        moved.add(k)
    if prefix == "TALK":
        for conf, keys, only_env in convs:
            if keys and keys <= moved and only_env:
                say("PARK", conf)
            elif keys & moved:
                say("KEEP", conf)

# Silero's rate (#215) is the one shared knob whose brain.env value reached phone calls
# and now does not: SIP_VAD_SAMPLE_RATE has a default of its own (8000), which beats the
# shared VAD_SAMPLE_RATE. Written as the phone's own, so calls keep the rate they ran at;
# a drop-in's own value for the phone, carried above, comes first.
shared_rate = existing.get("VAD_SAMPLE_RATE", "")
if shared_rate and "SIP_VAD_SAMPLE_RATE" not in existing and "SIP_VAD_SAMPLE_RATE" not in add:
    add["SIP_VAD_SAMPLE_RATE"] = shared_rate
    say("PHONE", "SIP_VAD_SAMPLE_RATE", "VAD_SAMPLE_RATE")

def write_new(path, text, mode, uid, gid):
    """Create `path` (never an existing file) holding `text`, private while it is written,
    synced, then given its final mode and owner. Gone again if anything fails."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chown(path, uid, gid)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise

if add and not dry:
    st = os.stat(env_path)
    mode, uid, gid = st.st_mode & 0o777, st.st_uid, st.st_gid
    backup = f"{env_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    tmp = f"{env_path}.tmp-{os.getpid()}"
    text = existing_text
    if text and not text.endswith("\n"):
        text += "\n"
    text += "".join(f"{k}={v}\n" for k, v in add.items())
    write_new(backup, existing_text, mode, uid, gid)
    try:
        write_new(tmp, text, mode, uid, gid)
        os.replace(tmp, env_path)
    except BaseException:
        # brain.env is as it was: no backup of a change that did not happen.
        try:
            os.unlink(backup)
        except OSError:
            pass
        raise
    say("BACKUP", backup)
PY
)" || { warn "migrating the drop-in tuning failed (above) — nothing was changed"; return 1; }
  local kind key src
  while IFS="$sep" read -r kind key src; do
    case "$kind" in
      SET) log "  drop-in tuning: $key -> $ETC/brain.env (from $src)" ;;
      HAVE) log "  drop-in tuning: $key already in $ETC/brain.env — left as it is (not taking the value in $src)" ;;
      SHADOWED) log "  drop-in tuning: $key in $src was never in effect (brain.env sets it, and wins) — not moved" ;;
      EMPTY) log "  drop-in tuning: $key in $src is empty — not moved" ;;
      PHONE) log "  $src in $ETC/brain.env reached phone calls too, and now does not: copied to $key so they keep their rate" ;;
      SKIP) warn "  drop-in setting $key in $src has no per-front-end form — not moved" ;;
      BAD) warn "  unreadable Environment= line at $key — skipped; check it by hand" ;;
      BACKUP) log "  $ETC/brain.env backed up to $key" ;;
      PARK)
        log "  $key: all it set is in $ETC/brain.env now — parked (it would reach phone calls too)"
        SUDO mv "$key" "$key.retired-pending"
        if [ "$DRY_RUN" != 1 ]; then DROPINS_PARKED+=("$key"); fi ;;
      KEEP) warn "  $key stays: it does more than the per-front-end settings, and now for phone calls too" ;;
    esac
  done <<< "$plan"
  SUDO systemctl daemon-reload
}
sudo_python() { if [ "$(id -u)" = 0 ]; then python3 -I "$@"; else sudo python3 -I "$@"; fi; }

# teaport-sip telephony (opt-in). The GPL C++ gateway (separate teaport-sip repo, NOT
# built or installed here — the binary is expected at $HOME/teaport-sip/build/teaport-sip).
# The calls themselves are teaport-brain's: its SIP front-end connects to the gateway's
# socket whenever the gateway is up. The unit is laid down but left INERT: it carries
# ConditionPathExists on the SIP config that `teaport sip configure` writes, so a default
# box never starts it. Configuring — which test-registers, writes the .conf (mode 600),
# then enables and starts the gateway — is what turns telephony on. This mirrors the
# Discord bridge's opt-in shape, minus the env/token wiring the CLI owns for SIP.
phase_sip() {
  log "sip telephony: installing the gateway unit (opt-in — 'teaport sip configure' turns it on)"
  sip_brain_detach
  sip_brain_retire
  render_unit teaport-sip.service.in teaport-sip.service
  SUDO systemctl daemon-reload
  # Deliberately NOT enabled/started here: the unit is gated on the SIP config, and
  # `teaport sip configure` registers a trunk, writes that config, then enables it. A
  # re-run on an already-configured box re-renders the unit but leaves its enabled
  # state alone, so repairing never silently turns telephony off.
  if systemctl is-active --quiet teaport-sip.service 2>/dev/null; then
    log "sip telephony: on — the restarted brain reconnects to the running gateway"
  else
    log "sip telephony: unit installed but inert — run 'teaport sip configure' (or --conf FILE for an existing gateway .conf) to register a SIP trunk and start it"
  fi
}

# The local audio bridge (opt-in): talk to the agent through a sound card on the box — a
# USB mic array with a speaker on its jack (brain/teaport_brain/local_audio.py). The unit is
# always laid down but INERT (ConditionPathExists on local-audio.env). Only
# TEAPORT_ENABLE_LOCAL_AUDIO=1 seeds that file and enables the unit. A repair without it
# leaves the enabled state alone: an enabled bridge is restarted onto the new brain venv,
# and one the operator turned off (systemctl disable --now teaport-local-audio) stays off.
# Wi-Fi setup (always laid down, started only on request): when the user says "set up
# Wi-Fi" at the box, the brain starts teaport-wifi-setup (brain/teaport_brain/
# wifi_setup.py), a temporary setup network teaport-ab12 with a page where a phone picks
# the network and types its password. Three system pieces make that work as the run user:
#   - the unit (never enabled; exits by itself) and its root-side steps, a small sh
#     script in /usr/local/lib/teaport (wifi-setup-net), so they do not need the venv;
#   - a polkit rule letting the run user change NetworkManager's Wi-Fi (its scope and
#     trade-off: packaging/wifi-setup/50-teaport-wifi-setup.rules.in);
#   - NM shared-mode DNS that answers every name with the box, so phones open the page.
#     Kept here in /usr/local/lib/teaport (root-owned: dnsmasq runs what a conf names) and
#     copied into /etc/NetworkManager/dnsmasq-shared.d only while the unit runs — NM gives
#     that directory to EVERY shared-mode dnsmasq, and dnsmasq cannot scope an option to
#     one connection. A tmpfiles.d line removes a copy a power cut left behind. Known
#     limit: another shared network started while setup runs answers 10.42.0.1 too.
# The setup network's password is the flash-time one in $ETC/wifi-setup.env
# (WIFI_SETUP_PASSWORD=<password>, printed on the paper insert; 8-63 letters, digits and
# a few symbols, e.g. k7mq-x4tn-p9wr) — written by the image / flashing step, never by
# this installer, so a repair leaves it alone. Without that file each setup makes a fresh
# random one, twelve characters in three groups of four like that example
# (wifi_setup.setup_password).
phase_wifi_setup() {
  if ! have nmcli || [ ! -x /usr/sbin/iptables ]; then
    log "wifi setup: needs NetworkManager (nmcli) and iptables (/usr/sbin/iptables) — skipped"
    return 0
  fi
  log "wifi setup: unit + polkit rule + captive DNS (runs only when asked)"
  have pkaction || warn "wifi setup: polkit not found — the run user cannot change NetworkManager's Wi-Fi without it"
  render_unit teaport-wifi-setup.service.in teaport-wifi-setup.service
  local rules=/etc/polkit-1/rules.d/50-teaport-wifi-setup.rules pkg="$HERE/packaging/wifi-setup" tmp
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] render %s -> %s\n' 50-teaport-wifi-setup.rules.in "$rules"
  else
    tmp="$(mktemp)"
    sed -e "s#@USER@#$RUN_USER#g" "$pkg/50-teaport-wifi-setup.rules.in" > "$tmp"
    SUDO install -D -m 0644 -o root -g root "$tmp" "$rules"
    rm -f "$tmp"
  fi
  SUDO install -D -m 0644 -o root -g root "$pkg/teaport-captive.conf" /usr/local/lib/teaport/teaport-captive.conf
  # The unit's root-side steps (port-80 rule, captive DNS, clean-up): sh, not the venv.
  SUDO install -D -m 0755 -o root -g root "$pkg/wifi-setup-net" /usr/local/lib/teaport/wifi-setup-net
  SUDO install -D -m 0644 -o root -g root "$pkg/teaport-wifi-setup.tmpfiles" /etc/tmpfiles.d/teaport-wifi-setup.conf
  # An earlier version of this phase installed the captive DNS file for good.
  if ! systemctl is-active --quiet teaport-wifi-setup 2>/dev/null; then
    SUDO rm -f /etc/NetworkManager/dnsmasq-shared.d/teaport-captive.conf
  fi
  # The setup page's teaport-ab12.local name; the captive page works without it.
  if ! have avahi-publish; then
    SUDO apt-get install -y avahi-utils \
      || warn "avahi-utils did not install — Wi-Fi setup works, but only via the captive page / 10.42.0.1"
  fi
  # NM's connectivity check, which tells the brain whether the box is online (the spoken
  # "set up Wi-Fi" works only offline: wifi_voice._box_online). Without one NM calls any
  # default route "full" and the brain treats the box as offline for good; Ubuntu ships
  # the check as its own package, which a minimal image can lack.
  local probe=network-manager-config-connectivity-ubuntu status
  status="$(dpkg-query -W -f='${Status}' "$probe" 2>/dev/null || true)"
  if ! contains "ok installed" "$status"; then
    if SUDO apt-get install -y "$probe"; then
      SUDO nmcli general reload conf || warn "NetworkManager did not reload its config — the connectivity check starts at the next reboot"
    else
      warn "$probe did not install — the box counts as offline, so \"set up Wi-Fi\" always starts setup instead of going to the assistant"
    fi
  fi
  SUDO systemctl daemon-reload
}

# The OLED face is not part of this installer: Teaspoon AI appliances come with it. A box
# that already has it keeps it (nothing here touches its files or units); the brain drives
# any display that serves the face socket (LOCAL_AUDIO_FACE_SOCK).
note_oled_face() {
  if [ "${TEAPORT_ENABLE_OLED_AVATAR:-0}" = 1 ]; then
    log "the OLED face is installed with the appliance, not by this installer — carrying on without it"
  fi
}

phase_local_audio() {
  render_unit teaport-local-audio.service.in teaport-local-audio.service
  SUDO systemctl daemon-reload
  if [ "${TEAPORT_ENABLE_LOCAL_AUDIO:-0}" != 1 ]; then
    if systemctl is-enabled --quiet teaport-local-audio.service 2>/dev/null; then
      log "local audio: on — restarting it onto the new brain"
      SUDO systemctl restart teaport-local-audio.service \
        || warn "teaport-local-audio did not restart — journalctl -u teaport-local-audio -n 30"
    else
      log "local audio: not enabled — skipped (TEAPORT_ENABLE_LOCAL_AUDIO=1 to talk through a USB mic array)"
    fi
    return 0
  fi
  { have arecord && have aplay; } || die "local audio needs alsa-utils (arecord/aplay): sudo apt install alsa-utils"
  log "local audio: enable (sound card -> brain /talk)"
  # The default device is a seed, so a device changed by hand or on the config page
  # survives a repair; one named in TEAPORT_LOCAL_AUDIO_DEVICE is an explicit answer and wins.
  local device="?LOCAL_AUDIO_DEVICE=hw:CARD=Array,DEV=0"
  if [ -n "${TEAPORT_LOCAL_AUDIO_DEVICE:-}" ]; then device="LOCAL_AUDIO_DEVICE=$TEAPORT_LOCAL_AUDIO_DEVICE"; fi
  write_env "$ETC/local-audio.env" "$device" "?LOCAL_AUDIO_CAPTURE_CHANNEL=0"
  SUDO systemctl enable teaport-local-audio.service
  # The unit does not fail on a missing card (the bridge retries the card itself): look
  # at its log for "card ready".
  SUDO systemctl restart teaport-local-audio.service \
    || warn "teaport-local-audio did not start — journalctl -u teaport-local-audio -n 30"
}

phase_verify() {
  log "verify"
  if [ "$DRY_RUN" = 1 ]; then log "(dry-run) would check engine :$ENGINE_PORT, brain :$BRAIN_PORT$([ -n "$AGENT_MODE" ] && echo ', front door :443, gateway->brain')"; return; fi
  # Engine loads its model in ~1 min (the --delay window); poll before giving up.
  local ok=1
  for i in $(seq 1 40); do contains ":$ENGINE_PORT " "$(ss -ltn 2>/dev/null || true)" && break; sleep 3; done
  contains ":$ENGINE_PORT " "$(ss -ltn 2>/dev/null || true)" || { warn "engine :$ENGINE_PORT not listening"; ok=0; }
  contains ":$BRAIN_PORT "  "$(ss -ltn 2>/dev/null || true)" || { warn "brain :$BRAIN_PORT not listening";  ok=0; }
  # Front door only exists when a gateway was fronted (phase_frontdoor).
  if [ -n "$AGENT_MODE" ]; then
    contains ":443 " "$(ss -ltn 2>/dev/null || true)" || { warn "front door :443 not listening — browser access is down"; ok=0; }
  fi
  # A listening engine can still be prosody-degraded: without bin/g2p/ it runs
  # espeak-only G2P and only says so in one journal line. Check that line.
  local g2p_log; g2p_log="$(SUDO journalctl -u teaport-engine.service --since '-10 min' 2>/dev/null | grep 'dict G2P' | tail -1 || true)"
  if contains "dict G2P unavailable" "$g2p_log"; then
    warn "engine is running WITHOUT the en-us G2P dict (espeak-only fallback — robotic prosody): $g2p_log"; ok=0
  fi
  local pos_log; pos_log="$(SUDO journalctl -u teaport-engine.service --since '-10 min' 2>/dev/null | grep 'POS tagger' | tail -1 || true)"
  if contains "POS tagger unavailable" "$pos_log"; then
    warn "engine is running WITHOUT the POS tagger (homographs read in their default form): $pos_log"; ok=0
  fi
  [ "$ok" = 1 ] && log "engine + brain are up" || warn "something is not up — 'teaport doctor' / journalctl -u teaport-brain"
}

usage_footer() {
  log "done."
  if [ -n "$AGENT_MODE" ]; then
    # The Control UI needs the *gateway's* auth token — a different secret from GATEWAY_TOKEN,
    # which guards the brain's /talk. Without it the browser's WS connect is refused at
    # phase=auth_credentials_received ("gateway token missing") and no pairing request is ever
    # created, so the user lands on a page that silently never connects. Print the
    # authenticated URL rather than leaving them to find the token in openclaw.json.
    local url="https://$FRONTDOOR_HOST" octok=""
    # Read the token agent_openclaw already resolved and wrote, rather than re-parsing
    # openclaw.json here — one site owns where the token lives, so the two cannot drift.
    # NemoClaw keeps its gateway (and its token) inside the sandbox: nothing to fill in there.
    if [ "$AGENT_MODE" = openclaw ]; then
      if [ "$DRY_RUN" = 1 ]; then
        # --dry-run changes nothing, so its output is the transcript operators paste into bug
        # reports and CI logs. Show the URL's shape only: a real run's token at least reaches
        # just the operator who installed the box, while a pasted preview travels.
        octok="<gateway-token>"
      else
        octok="$(cat "$SECRETS/openclaw_token" 2>/dev/null || true)"
      fi
    fi
    # With the operator's certificate (#249) nothing needs accepting or installing.
    local accept=" (accept the one-time cert)"; if [ -n "$TLS_CERT" ]; then accept=""; fi
    if [ -n "$octok" ]; then
      printf '  browser: open %s/#token=%s from a LAN device%s.\n' "$url" "$octok" "$accept"
      printf '           the #token fragment authenticates you; it is never sent to the proxy.\n'
    else
      printf '  browser: open %s from a LAN device%s.\n' "$url" "$accept"
      if [ "$AGENT_MODE" = nemoclaw ]; then
        # The sandbox owns the OpenClaw CLI; there is no `openclaw` on the host to run.
        printf '           you will need the gateway auth token — see: %s %s exec -- openclaw dashboard --no-open\n' "$NEMOCLAW" "$SANDBOX"
      else
        printf '           you will need the gateway auth token — see: openclaw dashboard --no-open\n'
      fi
    fi
    # Phones need saying out loud. The mobile app cannot click through 'tls internal' the way
    # a browser can, so scanning the QR before installing the root just fails the TLS
    # handshake with nothing on screen to explain why. Order matters: cert, then scan.
    if [ -n "$TLS_CERT" ]; then
      printf '  phone:   '
    else
      printf '  phone:   install http://%s/teaport-ca.crt, then Settings > General > About >\n' "$FRONTDOOR_HOST"
      printf '           Certificate Trust Settings and switch on "%s"\n' "$FRONTDOOR_CA_NAME"
      printf '           (a separate step from installing it). then '
    fi
    if [ "$AGENT_MODE" = nemoclaw ]; then
      printf 'pair: %s %s exec -- openclaw qr\n' "$NEMOCLAW" "$SANDBOX"
    else
      printf 'pair: openclaw qr   (the code expires in 10 minutes)\n'
    fi
    printf '  next:    start a Talk session from the dashboard.\n'
  else
    # Voice-only has no gateway, so no dashboard and no front door to point at.
    printf '  next:    voice-only install — the brain listens on ws://127.0.0.1:%s/talk and nothing fronts it.\n' "$BRAIN_PORT"
    printf '           install OpenClaw (sudo npm install -g %s) or NemoClaw, then re-run to add Talk.\n' "$(openclaw_spec)"
  fi
  cat <<EOF
  ops:     teaport status | teaport doctor | teaport logs [engine|brain]
EOF
}

# --only brain: the brain package, and nothing else (see the header). An appliance must
# already exist -- this updates one.
main_brain_only() {
  log "teaport installer — brain only  (prefix=$PREFIX$([ "$DRY_RUN" = 1 ] && echo ', DRY-RUN'))"
  if [ ! -d "$PREFIX" ] || ! systemctl cat teaport-brain.service >/dev/null 2>&1; then
    local msg="no installed appliance here ($PREFIX, teaport-brain.service) — --only brain updates one; run install.sh without it first"
    if [ "$DRY_RUN" = 1 ]; then warn "$msg"; else die "$msg"; fi
  fi
  # --only brain installs no system packages, and of the box's setup it touches only the
  # busy lamp's udev rule, the resolver's serve-stale drop-in and the ARP sysctl drop-in
  # (after the swap, below). The logs are not among them: a box no full run has set up
  # since setup_logs (no logrotate, or no journal drop-in) keeps its unrotated, uncapped
  # logs, so say so.
  if [ ! -x "$LOGROTATE_BIN" ] || [ ! -e "$JOURNAL_DROPIN" ] || [ ! -e "$JOURNAL_APPLIED" ]; then
    warn "logs may not be rotated or capped on this box yet (no logrotate, or no record of journald taking $JOURNAL_DROPIN): run install.sh without --only once"
  fi
  check_brain_source
  brain_deb_fetch
  brain_deb_keep
  # Before the swap, never after: the new brain is the phone line's front-end itself, and
  # the old SIP brain must not be left running beside it. Its unit goes only once the swap
  # is verified; a rollback puts it back (see sip_brain_detach).
  sip_brain_detach
  brain_swap "$BRAIN_DEB_CACHED" --auto-rollback
  sip_brain_retire
  brain_retire_old_tools
  brain_retire_old_venvs
  install_busy_lamp   # only now: the swap has been verified
  install_dns_resilience
  install_arp_multihome
  brain_deb_prune
  log "done — brain updated to $BRAIN_DEB_NEW$([ "$DRY_RUN" = 1 ] && echo ' (dry-run: nothing changed)')"
  printf '  undo:    install.sh --rollback brain\n'
  printf '  check:   teaport doctor\n'
}

# --rollback brain: the package kept before the installed one (the newest other one in
# $BRAIN_DEBS), installed over it. The package being replaced stays kept, so a second
# --rollback goes back again.
main_rollback_brain() {
  log "teaport installer — brain rollback  (prefix=$PREFIX$([ "$DRY_RUN" = 1 ] && echo ', DRY-RUN'))"
  local cur target f
  cur="$(brain_package_version)"
  [ -n "$cur" ] || die "$BRAIN_PKG is not installed — there is nothing to roll back"
  target=""
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ "$f" != "$(brain_deb_path "$cur")" ]; then target="$f"; break; fi
  done < <(ls -1t "$BRAIN_DEBS/${BRAIN_PKG}"_*.deb 2>/dev/null || true)
  [ -n "$target" ] || die "no earlier brain package is kept in $BRAIN_DEBS to roll back to (installed: $cur)"
  BRAIN_DEB="$target"
  BRAIN_DEB_NEW="$(dpkg-deb -f "$target" Version)"
  BRAIN_DEB_CACHED="$target"
  # A release from before the SIP front-end moved into teaport-brain (#58) has no phone
  # line of its own; no package predates it, so there is nothing to warn about here.
  # Not --auto-rollback: rolling back a rollback is what the next --rollback is for, and
  # doing it unasked would hide the failure.
  brain_swap "$target" ""
  log "done — brain rolled back to $BRAIN_DEB_NEW (run --rollback brain again to undo)"
}

# --only engine: the engine's files from the manifest and its unit from this checkout, and
# nothing else (see the header). An engine must already be installed: this updates one.
main_engine_only() {
  log "teaport installer — engine only  (prefix=$PREFIX, manifest=$MANIFEST_URL$([ "$DRY_RUN" = 1 ] && echo ', DRY-RUN'))"
  if [ ! -f "$PREFIX/bin/voxtral" ] || ! systemctl cat teaport-engine.service >/dev/null 2>&1; then
    local msg="no installed engine here ($PREFIX/bin/voxtral, teaport-engine.service) — --only engine updates one; run install.sh without --only first"
    if [ "$DRY_RUN" = 1 ]; then warn "$msg"; else die "$msg"; fi
  fi
  # A masked unit stays masked (render_unit, #199), so the restart and check that decide the
  # swap could not run: say so before anything is downloaded.
  if [ "$(readlink "$ENGINE_UNIT" 2>/dev/null || true)" = /dev/null ]; then
    local masked="teaport-engine.service is masked on this machine — --only engine would install an engine nothing starts; systemctl unmask teaport-engine.service first"
    if [ "$DRY_RUN" = 1 ]; then warn "$masked"; else die "$masked"; fi
  fi
  # Before anything is downloaded: render_unit would only find it missing after the swap.
  [ -f "$HERE/systemd/teaport-engine.service.in" ] || die "missing unit template: $HERE/systemd/teaport-engine.service.in"
  check_brain_source   # this checkout's freshness, in what --only engine installs (ENGINE_ONLY_PATHS)
  fetch_manifest
  eula_gate
  l4t_gate --plan
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] check espeak-ng, libopenblas.so.0 and libcudnn.so.9 are installed (--only engine installs no packages)\n'
  else
    engine_libs_check " — --only engine installs no system packages; run install.sh without --only, which does"
  fi
  # A changed model stages beside the live one (Voxtral is ~4.4GB) and a kept copy can
  # hold the old one: warned before anything is fetched, as the full run warns at 15GB.
  local freegb; freegb="$(df -Pk "$PREFIX" 2>/dev/null | awk 'NR==2{print int($4/1048576)}')" || true
  [ -n "$freegb" ] && [ "$freegb" -lt 10 ] && warn "only ${freegb}GB free at $PREFIX (a new model stages beside the live one, ~4.4GB for Voxtral; an old copy in $ENGINE_ROLLBACKS can go to make room)"
  phase_engine
  # engine.env before the swap: a key added now changes nothing for the engine that is
  # running (it read its environment when it started), and a file that cannot be read stops
  # the run before anything has moved. The value for a missing KOKORO_RESERVE_FPT depends
  # on the agent, as in phase_services.
  detect_agent
  engine_env_lines
  log "engine.env: only the keys it lacks are added; a value already there stays"
  env_add_missing "$ETC/engine.env" "${ENGINE_ENV[@]}"
  engine_swap
  manifest_take "$MF" engine models
  log "done — engine $(mget engine.release 2>/dev/null || echo '?') from $MANIFEST_URL$([ "$DRY_RUN" = 1 ] && echo ' (dry-run: nothing changed)')"
  if [ -n "$ENGINE_KEPT" ] || [ "$DRY_RUN" = 1 ]; then printf '  undo:    install.sh --rollback engine\n'; fi
  printf '  check:   teaport doctor\n'
}

# --rollback engine: the newest engine kept in $ENGINE_ROLLBACKS goes live again, with the
# unit it ran under. The one it replaces is kept the same way first, so the next
# --rollback engine undoes this one. No automatic rollback of the rollback (as with the
# brain): a failure is said, and the next --rollback is how to go back.
main_rollback_engine() {
  log "teaport installer — engine rollback  (prefix=$PREFIX$([ "$DRY_RUN" = 1 ] && echo ', DRY-RUN'))"
  local target="" rel replacing=""
  # A swap that was stopped half way: putting back the engine before it IS the rollback.
  if [ -f "$ENGINE_SWAP_MARK" ]; then
    target="$(cat "$ENGINE_SWAP_MARK" 2>/dev/null || true)"; target="${target:-the engine before an interrupted swap}"
  else
    target="$(engine_rollback_target)" || die "no previous engine to roll back to (nothing kept in $ENGINE_ROLLBACKS)"
  fi
  log "rolling the engine back to ${target##*/}"
  if [ "$DRY_RUN" != 1 ]; then sudo_ready "roll the engine back"; fi
  # Renames into bin/, models/ and $PREFIX: theirs to own, as phase_engine leaves them.
  SUDO chown "$RUN_USER" "$PREFIX"; SUDO chown -R "$RUN_USER" "$PREFIX/bin" "$PREFIX/models" "$ENGINE_ROLLBACKS"
  if [ -f "$ENGINE_SWAP_MARK" ]; then
    engine_swap_recover
  else
    # Keep what the rollback replaces (a model the kept engine has, too), so that a second
    # --rollback engine undoes this one whole.
    for rel in "${ENGINE_FILES[@]}" "${ENGINE_MODELS[@]}"; do [ ! -f "$target/$rel" ] || replacing="$replacing $rel"; done
    engine_keep_live "$replacing"
    # The renames back are one per file, as in a swap: marked the same way, so a run stopped
    # among them is put back whole (to the engine kept just now) by the next one.
    if [ "$DRY_RUN" != 1 ] && [ -n "$ENGINE_KEPT" ]; then printf '%s\n' "$ENGINE_KEPT" > "$ENGINE_SWAP_MARK"; fi
    engine_restore "$target"
    if [ "$DRY_RUN" != 1 ]; then rm -f "$ENGINE_SWAP_MARK"; fi
  fi
  SUDO systemctl daemon-reload
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] restart teaport-engine.service, verify it listens on :%s and stays up\n' "$ENGINE_PORT"
  else
    engine_dropins_note
    engine_restart || die "the engine did not come back on ${target##*/} — see: journalctl -u teaport-engine -n 100 (run --rollback engine again to undo)"
  fi
  engine_prune
  log "done — engine rolled back to ${target##*/} (run --rollback engine again to undo)"
}

main() {
  installer_lock
  if [ "$ROLLBACK" = brain ]; then main_rollback_brain; return; fi
  if [ "$ROLLBACK" = engine ]; then main_rollback_engine; return; fi
  if [ "$ONLY" = brain ]; then main_brain_only; return; fi
  if [ "$ONLY" = engine ]; then main_engine_only; return; fi
  log "teaport installer  (prefix=$PREFIX, manifest=$MANIFEST_URL$([ "$DRY_RUN" = 1 ] && echo ', DRY-RUN'))"
  # The manifest pins the license version/text, so fetch it first; the gate
  # still runs before any phase touches the system.
  fetch_manifest
  eula_gate
  phase_preflight
  # Which agent is here (none of the phases before phase_agent installs one), then the
  # front door's names, and the operator's certificate checked, before anything is
  # installed: phase_agent gives the name to OpenClaw, phase_services writes brain.env.
  # Only an install with an agent has a front door (phase_frontdoor).
  detect_agent
  check_openclaw_version
  if [ -n "$AGENT_MODE" ]; then resolve_frontdoor
  elif [ -n "$TLS_CERT" ]; then warn "TEAPORT_TLS_CERT is ignored: a voice-only install has no front door"; fi
  phase_sysdeps
  phase_engine
  phase_brain
  install_busy_lamp
  install_dns_resilience
  install_arp_multihome
  phase_agent
  phase_credentials
  phase_sip        # renders the SIP units only; before phase_services, whose one restart uses them
  phase_services
  phase_frontdoor
  phase_bridge
  phase_local_audio
  phase_wifi_setup
  note_oled_face
  phase_verify
  usage_footer
}

main "$@"
