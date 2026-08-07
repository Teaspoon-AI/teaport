#!/usr/bin/env bash
#
# teaport installer — served as:  bash <(curl -fsSL https://get.teaspoon.tech/teaport)
#
# Idempotent, re-runnable (re-run == repair). Installs the closed engine + models +
# the brain + (optionally) the OpenClaw plugin + systemd units on a JetPack 7.2 /
# CUDA 13 Jetson Orin, driven by the release manifest.
#
#   ./install.sh              install / repair
#   ./install.sh --dry-run    print what would happen; touch nothing
#   ./install.sh --help
#
# Env overrides (mainly for dev/testing):
#   TEAPORT_MANIFEST_URL   manifest URL or local path (default get.teaspoon.tech/manifest/stable.json)
#   TEAPORT_PREFIX         install root (default /opt/teaport)
#   TEAPORT_ETC            non-secret env dir (default /etc/teaport)
#   TEAPORT_BRAIN_SRC      install the brain from this local dir instead of cloning
#   TEAPORT_PLUGIN_SRC     install the plugin from this local dir instead of npm
#   TEAPORT_ACCEPT_EULA=1  accept the engine EULA non-interactively
#   TEAPORT_ALLOW_NON_JETSON=1   skip the Jetson/L4T gate (dev only; no real install)
#   TEAPORT_ENABLE_BRIDGE=1      install + enable the opt-in Discord voice bridge
#   TEAPORT_BRIDGE_GUILD_ID / TEAPORT_BRIDGE_FOLLOW_USER_ID   bridge guild + followed user
#   TEAPORT_BRIDGE_SRC     install the bridge from this local dir instead of cloning
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
BRAIN_SRC="${TEAPORT_BRAIN_SRC:-}"
PLUGIN_SRC="${TEAPORT_PLUGIN_SRC:-}"
BRIDGE_SRC="${TEAPORT_BRIDGE_SRC:-}"
DRY_RUN=0
# Where this script lives — lets a checkout install its own brain/plugin/systemd.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    --accept-eula) ACCEPT_EULA=1 ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) printf 'unknown arg: %s (see --help)\n' "$a" >&2; exit 2 ;;
  esac
done

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
    [ -n "$sha" ] && { echo "$sha  $dest.part" | sha256sum -c --status || die "sha256 mismatch: $dest"; }
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
    printf '    - any organizational or commercial use: requires a written license (hello@teaspoon.tech)\n\n'
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
L4T_RELEASE=""           # R-number from /etc/nv_tegra_release; empty off-Jetson (dev/dry-run)

phase_preflight() {
  log "preflight"
  if [ -f /etc/nv_tegra_release ]; then
    local l4t; l4t="$(sed -n 's/.*# R\([0-9]\+\).*/\1/p' /etc/nv_tegra_release | head -1)"
    L4T_RELEASE="$l4t"
    case "$l4t" in
      38|39) log "Jetson L4T R$l4t (JetPack 7.2) — engine asset: $ASSET_KEY" ;;
      *) die "unsupported L4T R${l4t:-?} — the engine ships for JetPack 7.2 (L4T R38/R39, CUDA 13) only" ;;
    esac
  elif [ "$ALLOW_NON_JETSON" = 1 ]; then
    warn "not a Jetson — continuing because TEAPORT_ALLOW_NON_JETSON=1 (dev/dry-run only)"
  else
    die "not an NVIDIA Jetson (/etc/nv_tegra_release missing)"
  fi
  local freegb; freegb="$(df -Pk "$(dirname "$PREFIX")" 2>/dev/null | awk 'NR==2{print int($4/1048576)}')" || true
  [ -n "$freegb" ] && [ "$freegb" -lt 15 ] && warn "only ${freegb}GB free near $PREFIX (need ~15GB for engine + models)"
  have curl || die "curl is required"
  # Look at the status code rather than using -f: the bare host root serves 404 (only /dl,
  # /manifest, /eula exist), so -f would warn "downloads will fail" on every healthy install —
  # but ignoring the code entirely calls a 502 from the edge "reachable" and defers the outage
  # to the download, an EULA prompt and ~15GB later. A 4xx proves the host answered; a 5xx (or
  # 000, curl's code for a DNS/TCP/TLS failure) is the outage worth warning about.
  if [ "$DRY_RUN" != 1 ]; then
    local code; code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 https://get.teaspoon.tech 2>/dev/null || true)"
    case "$code" in
      5*|000|"") warn "get.teaspoon.tech not reachable (HTTP ${code:-no response} — downloads will fail)" ;;
    esac
  fi
}

phase_sysdeps() {
  log "system deps: espeak-ng (engine G2P fallback), libopenblas0 (engine BLAS), python3.12-venv (brain)"
  # espeak-ng is a hard runtime dep of the GPL-clean engine (arm's-length CLI child) —
  # but ONLY as the OOV + non-English fallback. The en-us G2P is the misaki dict that
  # phase_engine installs to bin/g2p/; espeak-only en-us speaks robotic prosody.
  # libopenblas0 provides libopenblas.so.0, which the engine links against. JetPack does not
  # ship it, so without this the engine binary fails to load at first start.
  SUDO apt-get update -qq
  SUDO apt-get install -y espeak-ng libopenblas0 python3.12-venv
  if [ "$DRY_RUN" != 1 ]; then
    have espeak-ng || die "espeak-ng not installed"
    contains 'libopenblas.so.0' "$(ldconfig -p 2>/dev/null || true)" || die "libopenblas.so.0 not installed"
  fi
}

phase_engine() {
  log "engine + models -> $PREFIX"
  SUDO mkdir -p "$PREFIX/bin/g2p" "$PREFIX/models/voxtral" "$PREFIX/models/kokoro"
  SUDO chown -R "$RUN_USER" "$PREFIX"
  download "$(mget engine.assets.$ASSET_KEY.url)"  "$PREFIX/bin/voxtral"                         "$(mget engine.assets.$ASSET_KEY.sha256)"
  run chmod +x "$PREFIX/bin/voxtral"
  # The preflight gate accepts R38 *and* R39 on the claim that this CUDA-13 binary resolves its
  # deps on both (see ASSET_KEY). Check the claim instead of trusting the label: an unresolved
  # .so is a crisp message here rather than a crash-looping unit after the ~15GB of models
  # below. phase_sysdeps already installed libopenblas0, so a miss now is a real ABI gap.
  if [ "$DRY_RUN" != 1 ]; then
    local unresolved; unresolved="$(ldd "$PREFIX/bin/voxtral" 2>/dev/null | grep 'not found' || true)"
    if [ -n "$unresolved" ]; then
      die "engine binary has unresolved libraries on this L4T (R${L4T_RELEASE:-?}):
$unresolved
the engine ships for JetPack 7.2 (L4T R38/R39, CUDA 13) — report this with the lines above"
    fi
  fi
  # The misaki G2P dict MUST land at bin/g2p/ — the engine probes
  # <exe_dir>/g2p/kokoro_g2p.dict and, if absent, silently degrades to
  # espeak-only English: every function word gets its stressed citation form,
  # the duration predictor doubles it, and speech goes robotic with no error
  # anywhere (shipped exactly so, 2026-08-05). espeak-ng (phase_sysdeps) is
  # only the OOV + non-English fallback, NOT the en-us G2P.
  local g2p_url g2p_sha
  g2p_url="$(mget engine.g2p_dict.url 2>/dev/null)" || die "manifest has no engine.g2p_dict — a box installed without the dict speaks robotic en-us prosody; republish the manifest"
  g2p_sha="$(mget engine.g2p_dict.sha256 2>/dev/null)" || die "manifest engine.g2p_dict.sha256 is missing"
  download "$g2p_url" "$PREFIX/bin/g2p/kokoro_g2p.dict" "$g2p_sha"
  download "$(mget models.voxtral.url)"            "$PREFIX/models/voxtral/consolidated.safetensors" "$(mget models.voxtral.sha256)"
  download "$(mget models.kokoro.url)"             "$PREFIX/models/kokoro/Kokoro_espeak_F16.gguf"     "$(mget models.kokoro.sha256)"
  # tekken.json / params.json ride alongside the voxtral model (manifest models.voxtral.aux.*).
  for aux in tekken.json params.json; do
    local u; u="$(python3 -c "import json;print(json.load(open('$MF'))['models']['voxtral'].get('aux',{}).get('$aux',''))")"
    [ -n "$u" ] && [ "$u" != TODO ] && download "$u" "$PREFIX/models/voxtral/$aux" ""
  done
  run cp "$MF" "$PREFIX/manifest.json" 2>/dev/null || SUDO cp "$MF" "$PREFIX/manifest.json"
}

phase_brain() {
  log "brain -> $PREFIX/venv (python3.12)"
  run python3.12 -m venv "$PREFIX/venv"
  local src="$BRAIN_SRC"
  if [ -z "$src" ] && [ -d "$HERE/brain" ]; then src="$HERE/brain"; fi   # installing from a checkout
  if [ -z "$src" ]; then
    # production: clone the product repo at the manifest-pinned tag, install its brain/
    local repo tag work; repo="$(mget brain.repo)"; tag="$(mget brain.tag)"
    work="$STATE/brain-src"; SUDO mkdir -p "$STATE"; SUDO chown "$RUN_USER" "$STATE"
    run git clone --depth 1 --branch "$tag" "https://github.com/$repo.git" "$work"
    src="$work/brain"
  fi
  log "pip install $src"
  run "$PREFIX/venv/bin/pip" install -q -U pip
  run "$PREFIX/venv/bin/pip" install "$src"
  # ship the teaport operator CLI on PATH (repo root = the parent of the brain dir)
  local cli; cli="$(dirname "$src")/cli/teaport"
  if [ -f "$cli" ]; then log "install teaport CLI -> /usr/local/bin/teaport"; SUDO install -m 0755 "$cli" /usr/local/bin/teaport; fi
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
resolve_gateway_token() {
  [ -n "$GATEWAY_TOKEN" ] && return 0
  if [ -n "${TEAPORT_GATEWAY_TOKEN:-}" ]; then GATEWAY_TOKEN="$TEAPORT_GATEWAY_TOKEN"; return 0; fi
  # idempotent re-run: reuse the token already in brain.env instead of re-minting
  if [ -f "$ETC/brain.env" ]; then GATEWAY_TOKEN="$(sed -n 's/^GATEWAY_TOKEN=//p' "$ETC/brain.env" 2>/dev/null | head -1)"; fi
  [ -n "$GATEWAY_TOKEN" ] && return 0
  GATEWAY_TOKEN="$( (head -c18 /dev/urandom 2>/dev/null || echo "teaport-$$") | od -An -tx1 | tr -d ' \n')"
  return 0
}

# The OpenClaw config both agent paths share; only the brain URL differs.
#
# talk.realtime — brain=none: the teaport brain orchestrates the turn, so OpenClaw must not
# answer too. The token matches brain.env's GATEWAY_TOKEN so the brain accepts the relayed
# session.
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
  "provider": "teaport", "mode": "realtime", "transport": "gateway-relay", "brain": "none",
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
      warn "to add the agent later: install OpenClaw on the host (sudo npm install -g openclaw)"
      warn "or NemoClaw (bash <(curl -fsSL https://www.nvidia.com/nemoclaw.sh)), then re-run this installer"
      ;;
  esac
}

# --- host OpenClaw -----------------------------------------------------------
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
    printf '  [dry-run] openclaw config patch: talk.realtime provider=teaport brain=none url=%s (+token)\n' "$brain_ws"
    printf '  [dry-run] openclaw config patch: plugins.device-pair enabled=true publicUrl=wss://%s (phone pairing)\n' "$FRONTDOOR_HOST"
    printf '  [dry-run] openclaw config patch: gateway.trustedProxies=[127.0.0.1, ::1] (Caddy front door)\n'
  else
    local patch; patch="$(mktemp)"
    write_talk_patch "$patch" "$brain_ws"
    "$OPENCLAW" config patch --file "$patch"
    # phase_frontdoor puts Caddy in front of the gateway on the same host. Caddy injects
    # X-Forwarded-* headers, and the gateway refuses to treat a forwarded connection as local
    # unless the proxy is trusted — logging "Proxy headers detected from untrusted address" on
    # every browser hit. Loopback entries are valid here precisely for same-host reverse
    # proxies. This does not weaken auth: gateway.auth.mode stays as configured.
    cat > "$patch" <<'JSON'
{ "gateway": { "trustedProxies": ["127.0.0.1", "::1"] } }
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
  elif systemctl --user cat openclaw-gateway.service >/dev/null 2>&1; then
    "$OPENCLAW" gateway restart
  else
    "$OPENCLAW" gateway install
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
    printf '  [dry-run] openclaw config patch: talk.realtime provider=teaport brain=none url=%s (+token)\n' "$brain_ws"
    printf '  [dry-run] openclaw config patch: plugins.device-pair enabled=true publicUrl=wss://%s (phone pairing)\n' "$FRONTDOOR_HOST"
  else
    local patch; patch="$(mktemp)"
    write_talk_patch "$patch" "$brain_ws"
    "$NEMOCLAW" "$SANDBOX" upload "$patch" /tmp/teaport-talk.json
    "$NEMOCLAW" "$SANDBOX" exec --no-tty -- openclaw config patch --file /tmp/teaport-talk.json
  fi

  # 3. Reload the sandbox gateway + snapshot the wired state.
  run "$NEMOCLAW" "$SANDBOX" gateway restart
  run "$NEMOCLAW" "$SANDBOX" snapshot create --name teaport-installed
  # NemoClaw egress-locks the sandbox by default; the brain is on the host docker bridge
  # (172.18.0.1), reachable without an egress exception. Verify with: nemoclaw $SANDBOX doctor.
  log "sandbox wired — verify with: $NEMOCLAW $SANDBOX doctor"
}

# LLM config gathered by phase_credentials, consumed by phase_services (brain.env).
LLM_BASE_URL=""; LLM_MODEL=""
phase_credentials() {
  log "credentials -> $SECRETS (secrets never baked into units)"
  run mkdir -p "$SECRETS"; run chmod 700 "$SECRETS"
  # Bring-your-own OpenAI-compatible LLM (item-2 model): endpoint + key + model.
  LLM_BASE_URL="${TEAPORT_LLM_BASE_URL:-}"; LLM_MODEL="${TEAPORT_LLM_MODEL:-gpt-oss-120b}"
  local key="${TEAPORT_LLM_API_KEY:-}"
  if [ -z "$LLM_BASE_URL" ] && [ -t 0 ] && [ "$DRY_RUN" != 1 ]; then
    read -r -p 'LLM base URL (OpenAI-compatible, e.g. https://api.groq.com/openai/v1): ' LLM_BASE_URL
    read -r -p 'LLM model [gpt-oss-120b]: ' m; [ -n "$m" ] && LLM_MODEL="$m"
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
FRONTDOOR_HOST="${TEAPORT_HOST:-teaport.local}"   # mDNS name the browser opens over HTTPS
# Where 'tls internal' keeps its CA. The Debian caddy package runs the daemon with
# HOME=/var/lib/caddy, so its data dir is the XDG default underneath that.
CADDY_PKI="${TEAPORT_CADDY_PKI:-/var/lib/caddy/.local/share/caddy/pki/authorities/local}"
render_unit() {  # render_unit <template.in> <dest-name>
  local tpl="$HERE/systemd/$1" out="$2"
  [ -f "$tpl" ] || die "missing unit template: $tpl"
  local nemoclaw_bin; nemoclaw_bin="$(command -v nemoclaw || echo /usr/local/bin/nemoclaw)"
  # Absolute node path for the bridge unit's ExecStart — systemd services get a minimal PATH
  # and node may live outside it (nvm/NodeSource), so resolve it at render time like @NEMOCLAW@.
  local node_bin; node_bin="$(command -v node || echo /usr/bin/node)"
  local body; body="$(sed -e "s#@USER@#$RUN_USER#g" -e "s#@PREFIX@#$PREFIX#g" -e "s#@ETC@#$ETC#g" \
                          -e "s#@NEMOCLAW@#$nemoclaw_bin#g" -e "s#@SANDBOX@#$SANDBOX#g" \
                          -e "s#@NODE@#$node_bin#g" "$tpl")"
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] render %s -> /etc/systemd/system/%s\n' "$1" "$out";
  else printf '%s\n' "$body" | SUDO tee "/etc/systemd/system/$out" >/dev/null; fi
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
  local path="$1"; shift
  local keep=() managed=" " line
  for line in "$@"; do managed="$managed${line%%=*} "; done
  # Read unprivileged: write_env's own chgrp/chmod 640 leaves these group-readable by
  # RUN_USER, which is who runs the installer. A first install has no file to read.
  if [ -r "$path" ]; then
    while IFS= read -r line; do
      case "$line" in
        ''|'#'*) continue ;;
        *=*) case "$managed" in *" ${line%%=*} "*) ;; *) keep+=("$line") ;; esac ;;
      esac
    done < "$path"
  fi
  # NB: `[ cond ] && cmd` as a statement returns non-zero when cond is false, which under
  # `set -e` aborts the installer on a first install (empty keep array). Use if-blocks.
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] write %s:\n' "$path"; printf '    %s\n' "$@"
    if [ ${#keep[@]} -gt 0 ]; then printf '    %s   (preserved operator setting)\n' "${keep[@]}"; fi
    return
  fi
  if [ ${#keep[@]} -gt 0 ]; then log "preserving ${#keep[@]} operator setting(s) in $path"; fi
  { printf '%s\n' "$@"
    if [ ${#keep[@]} -gt 0 ]; then printf '%s\n' "${keep[@]}"; fi
  } | SUDO tee "$path" >/dev/null
  SUDO chmod 640 "$path"; SUDO chgrp "$RUN_USER" "$path" 2>/dev/null || true
}

# write_caddyfile <path> — the front-door reverse proxy. 'tls internal' is a self-signed
# cert (offline, one-time browser trust); swap it for an ACME/DNS-01 block to get a real
# no-warning cert for a name that resolves to the LAN IP. WebSocket upgrades (the /talk
# stream) pass through reverse_proxy automatically.
#
# The plain-HTTP block exists for phones. A browser can click through an untrusted cert; the
# OpenClaw mobile app cannot, so pairing over wss:// needs Caddy's local root installed on the
# device first — and that download cannot itself sit behind the cert it is meant to fix. Hence
# exactly one path on :80, everything else redirected.
#
# It is served straight out of Caddy's PKI directory rather than copied: 'tls internal' issues
# ~12-hour leaves under a 10-year root, so a copy would be a snapshot of something that rotates
# twice a day, and would go stale silently if the CA were ever regenerated. The path matcher is
# exact and rewrites to a fixed file, so root.key next to it stays unreachable.
write_caddyfile() {
  local path="$1"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] write %s  (%s -> 127.0.0.1:%s, tls internal, + http://%s/teaport-ca.crt)\n' "$path" "$FRONTDOOR_HOST" "$GATEWAY_PORT" "$FRONTDOOR_HOST"; return
  fi
  printf '%s\n' \
    "# teaport front door — rendered by install.sh. Edit the tls/upstream here, then:" \
    "#   sudo systemctl reload caddy.service" \
    "" \
    "# Cleartext, one path only: the local CA, so a phone can trust us before it connects." \
    "http://$FRONTDOOR_HOST {" \
    "    handle /teaport-ca.crt {" \
    "        root * $CADDY_PKI" \
    "        rewrite * /root.crt" \
    "        file_server" \
    "        header Content-Type application/x-x509-ca-cert" \
    "        header Content-Disposition \"attachment; filename=teaport-ca.crt\"" \
    "    }" \
    "    handle {" \
    "        redir https://{host}{uri} permanent" \
    "    }" \
    "}" \
    "" \
    "$FRONTDOOR_HOST {" \
    "    tls internal" \
    "    reverse_proxy 127.0.0.1:$GATEWAY_PORT" \
    "}" | SUDO tee "$path" >/dev/null
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
  # KOKORO_RESERVE_FPT: 6 when an agent coexists (RAM shared), 12 voice-only. The code
  # default (50) OOMs an 8GB box — the installer must always set it.
  # Any agent counts, not just a NemoClaw sandbox: a host OpenClaw gateway is resident too,
  # so reserving the voice-only amount alongside it is the direction that OOMs. 6 is the
  # measured-safe sandbox figure and is conservative for the (lighter) host gateway.
  local fpt=12; [ -n "$AGENT_MODE" ] && fpt=6
  # GATEWAY_TOKEN protects the local /talk port; the same value is wired into the plugin
  # config (phase_agent) — resolve it once for both sides.
  resolve_gateway_token

  # VOX_REQUIRE_DICT_G2P: refuse to serve if the misaki dict failed to load
  # rather than silently fall back to espeak-only prosody (see phase_engine).
  # Engine <= 1.3 ignores it; from the next release it turns a degraded box
  # into a loud restart loop that `teaport doctor` / journalctl names.
  write_env "$ETC/engine.env" \
    "KOKORO_RESERVE_FPT=$fpt" "ENGINE_PORT=$ENGINE_PORT" "ENGINE_DELAY=240" "TTS_CTX=192" \
    "VOX_REQUIRE_DICT_G2P=1"
  write_env "$ETC/brain.env" \
    "BRAIN_PORT=$BRAIN_PORT" \
    "LLM_BASE_URL=$LLM_BASE_URL" "LLM_MODEL=$LLM_MODEL" \
    "TEAPORT_URL=ws://127.0.0.1:$ENGINE_PORT/v1/realtime" \
    "OPENCLAW_GATEWAY_URL=http://127.0.0.1:$GATEWAY_PORT" \
    "TEAPORT_PERSONA_FILE=$SECRETS/persona.md" \
    "GATEWAY_TOKEN=$GATEWAY_TOKEN" "MALLOC_ARENA_MAX=2" "HF_HUB_OFFLINE=1"

  render_unit teaport-engine.service.in teaport-engine.service
  render_unit teaport-brain.service.in  teaport-brain.service
  if [ -n "$SANDBOX" ]; then render_unit teaport-sandbox-recover.service.in teaport-sandbox-recover.service; fi

  SUDO systemctl daemon-reload
  SUDO systemctl enable --now teaport-engine.service teaport-brain.service
  if [ -n "$SANDBOX" ]; then SUDO systemctl enable --now teaport-sandbox-recover.service; fi
}

# Caddy isn't in the stock Ubuntu repos — add its official Cloudsmith apt repo (idempotent),
# then install. Keeps the TLS front door patched by apt instead of a frozen, sha-pinned R2 binary.
install_caddy_apt() {
  local key=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
  local list=/etc/apt/sources.list.d/caddy-stable.list
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] add Caddy apt repo + apt-get install -y caddy\n'; return; fi
  SUDO apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  if [ ! -f "$key" ]; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | SUDO gpg --dearmor -o "$key" || die "cannot fetch the Caddy repo key"
  fi
  if [ ! -f "$list" ]; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | SUDO tee "$list" >/dev/null || die "cannot fetch the Caddy repo list"
  fi
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
  # mDNS: publish $FRONTDOOR_HOST so LAN browsers resolve it with no DNS setup.
  SUDO apt-get install -y avahi-daemon
  write_caddyfile /etc/caddy/Caddyfile
  set_avahi_hostname "${FRONTDOOR_HOST%.local}"
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
    run git clone --depth 1 --branch "$tag" "https://github.com/$repo.git" "$work"
    src="$work/bridge/discord"
  fi
  SUDO mkdir -p "$PREFIX/bridge"; SUDO chown -R "$RUN_USER" "$PREFIX/bridge"
  run cp "$src/index.js" "$src/package.json" "$PREFIX/bridge/"
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
    SUDO systemctl enable --now teaport-discord-bridge.service
  else
    SUDO systemctl enable teaport-discord-bridge.service
    warn "bridge installed but not started — set BRIDGE_GUILD_ID/BRIDGE_FOLLOW_USER_ID in $ETC/bridge.env + the token in $tokfile, then: systemctl start teaport-discord-bridge"
  fi
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
    warn "engine is running WITHOUT the misaki G2P dict (espeak-only fallback — robotic prosody): $g2p_log"; ok=0
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
    if [ -n "$octok" ]; then
      printf '  browser: open %s/#token=%s from a LAN device (accept the one-time cert).\n' "$url" "$octok"
      printf '           the #token fragment authenticates you; it is never sent to the proxy.\n'
    else
      printf '  browser: open %s from a LAN device (accept the one-time cert).\n' "$url"
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
    printf '  phone:   install http://%s/teaport-ca.crt, then Settings > General > About >\n' "$FRONTDOOR_HOST"
    printf '           Certificate Trust Settings and switch it on (a separate step from installing it).\n'
    if [ "$AGENT_MODE" = nemoclaw ]; then
      printf '           then pair: %s %s exec -- openclaw qr\n' "$NEMOCLAW" "$SANDBOX"
    else
      printf '           then pair: openclaw qr   (the code expires in 10 minutes)\n'
    fi
    printf '  next:    start a Talk session from the dashboard.\n'
  else
    # Voice-only has no gateway, so no dashboard and no front door to point at.
    printf '  next:    voice-only install — the brain listens on ws://127.0.0.1:%s/talk and nothing fronts it.\n' "$BRAIN_PORT"
    printf '           install OpenClaw (sudo npm install -g openclaw) or NemoClaw, then re-run to add Talk.\n'
  fi
  cat <<EOF
  ops:     teaport status | teaport doctor | teaport logs [engine|brain]
EOF
}

main() {
  log "teaport installer  (prefix=$PREFIX, manifest=$MANIFEST_URL$([ "$DRY_RUN" = 1 ] && echo ', DRY-RUN'))"
  # The manifest pins the license version/text, so fetch it first; the gate
  # still runs before any phase touches the system.
  fetch_manifest
  eula_gate
  phase_preflight
  phase_sysdeps
  phase_engine
  phase_brain
  detect_agent
  phase_agent
  phase_credentials
  phase_services
  phase_frontdoor
  phase_bridge
  phase_verify
  usage_footer
}

main "$@"
