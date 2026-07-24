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
ASSET_KEY="l4t38-cu13"   # the only supported target (JetPack 7.2 / CUDA 13)

phase_preflight() {
  log "preflight"
  if [ -f /etc/nv_tegra_release ]; then
    local l4t; l4t="$(sed -n 's/.*# R\([0-9]\+\).*/\1/p' /etc/nv_tegra_release | head -1)"
    [ "$l4t" = 38 ] || die "unsupported L4T R${l4t:-?} — the engine ships for JetPack 7.2 (L4T R38 / CUDA 13) only"
    log "Jetson L4T R38 (JetPack 7.2) — engine asset: $ASSET_KEY"
  elif [ "$ALLOW_NON_JETSON" = 1 ]; then
    warn "not a Jetson — continuing because TEAPORT_ALLOW_NON_JETSON=1 (dev/dry-run only)"
  else
    die "not an NVIDIA Jetson (/etc/nv_tegra_release missing)"
  fi
  local freegb; freegb="$(df -Pk "$(dirname "$PREFIX")" 2>/dev/null | awk 'NR==2{print int($4/1048576)}')" || true
  [ -n "$freegb" ] && [ "$freegb" -lt 15 ] && warn "only ${freegb}GB free near $PREFIX (need ~15GB for engine + models)"
  have curl || die "curl is required"
  run curl -fsSL -o /dev/null --max-time 8 https://get.teaspoon.tech 2>/dev/null || warn "get.teaspoon.tech not reachable (downloads will fail)"
}

phase_sysdeps() {
  log "system deps: espeak-ng (engine G2P), python3.12-venv (brain)"
  # espeak-ng is a hard runtime dep of the GPL-clean engine (arm's-length CLI child).
  SUDO apt-get update -qq
  SUDO apt-get install -y espeak-ng python3.12-venv
  if [ "$DRY_RUN" != 1 ]; then have espeak-ng || die "espeak-ng not installed"; fi
}

phase_engine() {
  log "engine + models -> $PREFIX"
  SUDO mkdir -p "$PREFIX/bin" "$PREFIX/models/voxtral" "$PREFIX/models/kokoro"
  SUDO chown -R "$RUN_USER" "$PREFIX"
  download "$(mget engine.assets.$ASSET_KEY.url)"  "$PREFIX/bin/voxtral"                         "$(mget engine.assets.$ASSET_KEY.sha256)"
  run chmod +x "$PREFIX/bin/voxtral"
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

# NemoClaw sandbox name (empty if none) + the nemoclaw binary + the /talk auth token,
# resolved once so the brain (brain.env) and the plugin config share the same token.
SANDBOX=""; NEMOCLAW=""; GATEWAY_TOKEN=""
detect_sandbox() {
  local sj="$HOME/.nemoclaw/sandboxes.json"
  { have nemoclaw || [ -x "$HOME/.local/bin/nemoclaw" ]; } || return 0
  [ -f "$sj" ] || return 0
  # sandboxes.json = {defaultSandbox, sandboxes:{<name>:...}} — prefer the default.
  SANDBOX="$(python3 -c "import json;d=json.load(open('$sj'));print(d.get('defaultSandbox') or (list(d.get('sandboxes',{})) or [''])[0])" 2>/dev/null || true)"
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

phase_agent() {
  detect_sandbox
  if [ -z "$SANDBOX" ]; then
    log "no NemoClaw sandbox detected — voice-only install"
    warn "to add the agent later: install NemoClaw (bash <(curl -fsSL https://www.nvidia.com/nemoclaw.sh)) and re-run"
    return 0
  fi
  resolve_nemoclaw; resolve_gateway_token
  log "NemoClaw sandbox '$SANDBOX' — installing the plugin + wiring talk.realtime"
  local brain_ws="ws://172.18.0.1:${BRAIN_PORT}/talk"   # sandbox -> host brain over the docker bridge

  # 1. Plugin into the sandbox. Published -> npm spec; otherwise npm-pack a tgz (honors the
  #    files allowlist) and `openclaw plugins install <tgz>` copies it into .openclaw/extensions/.
  local psrc="${PLUGIN_SRC:-}"; if [ -z "$psrc" ] && [ -d "$HERE/plugin" ]; then psrc="$HERE/plugin"; fi
  if [ -n "$psrc" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      printf '  [dry-run] (cd %s && npm pack) -> nemoclaw %s upload -> openclaw plugins install <tgz> --force\n' "$psrc" "$SANDBOX"
    else
      local tgz; tgz="$(cd "$psrc" && npm pack --silent --pack-destination /tmp)" || die "npm pack failed in $psrc"
      "$NEMOCLAW" "$SANDBOX" upload "/tmp/$tgz" "/tmp/$tgz"
      "$NEMOCLAW" "$SANDBOX" exec --no-tty -- openclaw plugins install "/tmp/$tgz" --force
    fi
  else
    run "$NEMOCLAW" "$SANDBOX" exec --no-tty -- openclaw plugins install "@teaspoon-ai/openclaw-teaport-realtime" --pin
  fi
  run "$NEMOCLAW" "$SANDBOX" exec --no-tty -- openclaw plugins enable teaport-realtime

  # 2. talk.realtime — one validated merge (openclaw config patch). The token matches brain.env.
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] openclaw config patch: talk.realtime provider=teaport brain=none url=%s (+token)\n' "$brain_ws"
  else
    local patch; patch="$(mktemp)"
    cat > "$patch" <<JSON
{ "talk": { "realtime": {
  "provider": "teaport", "mode": "realtime", "transport": "gateway-relay", "brain": "none",
  "providers": { "teaport": { "url": "$brain_ws", "token": "$GATEWAY_TOKEN" } } } } }
JSON
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
  local path="$1"; shift
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] write %s:\n' "$path"; printf '    %s\n' "$@"; return; fi
  printf '%s\n' "$@" | SUDO tee "$path" >/dev/null
  SUDO chmod 640 "$path"; SUDO chgrp "$RUN_USER" "$path" 2>/dev/null || true
}

# write_caddyfile <path> — the front-door reverse proxy. 'tls internal' is a self-signed
# cert (offline, one-time browser trust); swap it for an ACME/DNS-01 block to get a real
# no-warning cert for a name that resolves to the LAN IP. WebSocket upgrades (the /talk
# stream) pass through reverse_proxy automatically.
write_caddyfile() {
  local path="$1"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] write %s  (%s -> 127.0.0.1:%s, tls internal)\n' "$path" "$FRONTDOOR_HOST" "$GATEWAY_PORT"; return
  fi
  printf '%s\n' \
    "# teaport front door — rendered by install.sh. Edit the tls/upstream here, then:" \
    "#   sudo systemctl reload caddy.service" \
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
  # KOKORO_RESERVE_FPT: 6 when a sandbox coexists (RAM shared), 12 voice-only. The code
  # default (50) OOMs an 8GB box — the installer must always set it.
  local fpt=12; [ -n "$SANDBOX" ] && fpt=6
  # GATEWAY_TOKEN protects the local /talk port; the same value is wired into the plugin
  # config (phase_agent) — resolve it once for both sides.
  resolve_gateway_token

  write_env "$ETC/engine.env" \
    "KOKORO_RESERVE_FPT=$fpt" "ENGINE_PORT=$ENGINE_PORT" "ENGINE_DELAY=240" "TTS_CTX=192"
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
  if [ -z "$SANDBOX" ]; then log "front door: skipped (no gateway — voice-only install)"; return 0; fi
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
  SUDO systemctl reload caddy.service 2>/dev/null || SUDO systemctl restart caddy.service
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
  if [ "$DRY_RUN" = 1 ]; then log "(dry-run) would check engine :$ENGINE_PORT, brain :$BRAIN_PORT$([ -n "$SANDBOX" ] && echo ', front door :443'), sandbox->brain"; return; fi
  # Engine loads its model in ~1 min (the --delay window); poll before giving up.
  local ok=1
  for i in $(seq 1 40); do ss -ltn 2>/dev/null | grep -q ":$ENGINE_PORT " && break; sleep 3; done
  ss -ltn 2>/dev/null | grep -q ":$ENGINE_PORT " || { warn "engine :$ENGINE_PORT not listening"; ok=0; }
  ss -ltn 2>/dev/null | grep -q ":$BRAIN_PORT "  || { warn "brain :$BRAIN_PORT not listening";  ok=0; }
  # Front door only exists when a gateway was fronted (phase_frontdoor).
  if [ -n "$SANDBOX" ]; then
    ss -ltn 2>/dev/null | grep -q ":443 " || { warn "front door :443 not listening — browser access is down"; ok=0; }
  fi
  [ "$ok" = 1 ] && log "engine + brain are up" || warn "something is not up — 'teaport doctor' / journalctl -u teaport-brain"
}

usage_footer() {
  log "done."
  if [ -n "$SANDBOX" ]; then
    printf '  browser: open https://%s from a LAN device (accept the one-time cert), then pair + Talk.\n' "$FRONTDOOR_HOST"
  fi
  cat <<EOF
  next: open your OpenClaw dashboard, pair the device, start a Talk session.
  ops:  teaport status | teaport doctor | teaport logs [engine|brain]
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
  detect_sandbox
  phase_agent
  phase_credentials
  phase_services
  phase_frontdoor
  phase_bridge
  phase_verify
  usage_footer
}

main "$@"
