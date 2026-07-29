#!/usr/bin/env bash
#
# teaport web installer — the entrypoint served at https://get.teaspoon.tech/teaport
#
#   bash <(curl -fsSL https://get.teaspoon.tech/teaport)
#
# A piped one-liner only receives this single file, but install.sh needs the whole
# product tree (brain/, systemd/*.in, cli/teaport, bridge/). So this bootstrap clones
# the public teaport repo into a temp dir and runs its install.sh from that checkout.
# The engine binary + models are downloaded separately from the release manifest.
#
# Env overrides (rarely needed):
#   TEAPORT_REPO   git URL to clone (default the public teaport repo)
#   TEAPORT_REF    branch/tag to install (default: main)
# All other args/env pass straight through to install.sh (e.g. --dry-run).
set -euo pipefail

REPO="${TEAPORT_REPO:-https://github.com/Teaspoon-AI/teaport.git}"
REF="${TEAPORT_REF:-main}"

if ! command -v git >/dev/null 2>&1; then
  echo "teaport: git is required to install — run 'sudo apt-get install -y git' and retry." >&2
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "==> fetching teaport ($REF)…"
git clone --quiet --depth 1 --branch "$REF" "$REPO" "$work/teaport"

# Run the real installer from the checkout; it copies everything it needs into place,
# so the temp clone is disposable (removed by the trap on exit).
bash "$work/teaport/install.sh" "$@"
