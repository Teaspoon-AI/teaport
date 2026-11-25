#!/usr/bin/env bash
#
# teaport web installer — the entrypoint served at https://get.teaspoon.tech/teaport
#
#   bash <(curl -fsSL https://get.teaspoon.tech/teaport)
#
# A piped one-liner only receives this single file, but install.sh needs the whole
# product tree (brain/, systemd/*.in, cli/teaport, bridge/). So this bootstrap clones
# the teaport repository into a temp dir and runs its install.sh from that checkout.
# The engine binary + models are downloaded separately from the release manifest.
#
# What it installs is what the release keys signed, not what a server says: it carries the
# keys itself (below: the same lines as manifest/allowed_signers, which a test keeps equal).
#   1. It downloads the channel's release manifest and its signature (<channel>.json and
#      <channel>.json.sig) and verifies them with ssh-keygen -Y verify, in the
#      teaport-manifest namespace, against those keys. The manifest must name the channel.
#   2. From the verified bytes only: brain.repo, brain.tag and brain.commit. The tag must be
#      at that commit on the server (git ls-remote), and so must the clone's HEAD.
#   3. It runs that checkout's install.sh against the copy it verified
#      (TEAPORT_MANIFEST_URL=file://..., the .sig beside it, which install.sh verifies again).
# A check that fails stops it, and nothing it fetched is run. The download
# host serves this file too, so this file is signed as well (install-bootstrap.sh.sig, served
# as https://get.teaspoon.tech/teaport.sig, namespace teaport-bootstrap): the installation
# guide shows how to check it before running it.
#
# Env overrides (rarely needed):
#   TEAPORT_CHANNEL       stable (default) or beta: the manifest it installs
#   TEAPORT_MANIFEST_URL  read the manifest (and <url>.sig) here instead, e.g. a local mirror
#                         (file:// or a path); it is verified all the same
#   TEAPORT_REPO          git URL to clone instead of github.com/<brain.repo>, e.g. a mirror;
#                         the clone must still be at brain.commit
#   TEAPORT_REF           DEVELOPERS ONLY: install this branch or tag of TEAPORT_REPO as it is
#                         on the server, with no release signature behind it
# All other args/env pass straight through to install.sh (e.g. --dry-run).
set -euo pipefail

CHANNEL="${TEAPORT_CHANNEL:-stable}"
MANIFEST="${TEAPORT_MANIFEST_URL:-https://get.teaspoon.tech/manifest/$CHANNEL.json}"
REPO="${TEAPORT_REPO:-}"
REF="${TEAPORT_REF:-}"
MANIFEST_NAMESPACE=teaport-manifest
# The one repository a release may name (update_apply.REPO, check.py's RELEASE_REPO).
RELEASE_REPO=Teaspoon-AI/teaport

die() { printf 'teaport: %s\n' "$*" >&2; exit 1; }

# The release keys: manifest/allowed_signers, whole (a test keeps the two the same).
trusted_signers() {
  cat <<'SIGNERS'
# The keys a teaport release is signed with (ssh-keygen -Y verify's allowed_signers format),
# in two namespaces: teaport-manifest for the release manifests (manifest/<channel>.json is
# trusted only when manifest/<channel>.json.sig verifies against one of these), and
# teaport-bootstrap for the one-line installer (install-bootstrap.sh.sig). The brain
# package ships this file as /usr/lib/teaport/allowed_signers, which `teaport update`, the
# brain's release check and install.sh read; install.sh reads the copy in its own checkout
# on a box that has no package yet, and install-bootstrap.sh carries a copy of this whole
# file (a test keeps the two the same). packaging/RELEASING.md says how to sign.
#
# Two keys, each on its own line: the main key, which signs releases, and the backup, kept
# elsewhere, which signs only when the main key is lost or leaked (RELEASING.md, rotation).
# One line each:
#   <principal> namespaces="teaport-manifest,teaport-bootstrap" ssh-ed25519 <public key>
#
# PLACEHOLDER: the release keys do not exist yet. Until the two lines below are real keys,
# nothing verifies (every manifest is refused), and packaging/build-deb.sh refuses a
# release build while the word PLACEHOLDER is anywhere in this file. Replace the two lines
# with the keys' lines and delete this paragraph.
# PLACEHOLDER release-main namespaces="teaport-manifest,teaport-bootstrap" ssh-ed25519 <main public key>
# PLACEHOLDER release-backup namespaces="teaport-manifest,teaport-bootstrap" ssh-ed25519 <backup public key>
SIGNERS
}

need() {  # need <command> <package>
  command -v "$1" >/dev/null 2>&1 \
    || die "$1 is required to install — run 'sudo apt-get install -y $2' and retry."
}
need git git
need curl curl

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# --- the developer's path: a branch or tag as GitHub has it, unverified -------------------
if [ -n "$REF" ]; then
  REPO="${REPO:-https://github.com/Teaspoon-AI/teaport.git}"
  {
    printf '\n\033[1;31mWARNING:\033[0m TEAPORT_REF=%s installs %s at %s exactly as that server has\n' "$REF" "$REPO" "$REF"
    printf 'it now. No release signature is checked for it: whoever can push there decides what\n'
    printf 'runs on this machine, as root. This is for development; for a release, unset TEAPORT_REF.\n\n'
  } >&2
  echo "==> fetching teaport ($REF, unverified)…"
  git clone --quiet --depth 1 --branch "$REF" "$REPO" "$work/teaport"
  bash "$work/teaport/install.sh" "$@"
  exit $?
fi

# --- the release: the signed manifest, then the commit it names -------------------------
need ssh-keygen openssh-client
need python3 python3
case "$CHANNEL" in stable|beta) ;; *) die "TEAPORT_CHANNEL must be stable or beta (got '$CHANNEL')" ;; esac

signers="$work/allowed_signers"
trusted_signers > "$signers"
# Read strictly, as manifest_sig.keys reads a signers file: at most 1 MiB, printable ASCII and newlines only,
# every line blank, a comment ("#" first) or exactly a release key line, each principal once.
# ssh-keygen trusts lines a looser reader misses (a "*" principal, an option, a line split
# on a control character), so anything else is refused.
key_line='^release-(main|backup) namespaces="teaport-manifest,teaport-bootstrap" ssh-ed25519 [A-Za-z0-9+/]+={0,2}$'
if [ "$(wc -c < "$signers")" -gt 1048576 ] || LC_ALL=C grep -q '[^ -~]' "$signers" \
    || LC_ALL=C grep -qvE "^( *|#.*)\$|$key_line" "$signers" \
    || [ -n "$(LC_ALL=C grep -oE '^release-(main|backup) ' "$signers" | LC_ALL=C sort | uniq -d)" ]; then
  die "the release keys this installer carries are malformed; nothing was installed"
fi
LC_ALL=C grep -qE "$key_line" "$signers" \
  || die "this installer carries no release keys yet, so it can verify no release; nothing was installed"

fetch() {  # fetch <url, file:// URL or path> <dest>
  case "$1" in
    /*) cp -- "$1" "$2" ;;
    *) curl -q -fsSL --retry 3 -o "$2" "$1" ;;
  esac
}
mf="$work/$CHANNEL.json"
echo "==> reading the $CHANNEL release ($MANIFEST)…"
fetch "$MANIFEST" "$mf" || die "cannot read the release manifest: $MANIFEST"
fetch "$MANIFEST.sig" "$mf.sig" \
  || die "the release manifest has no signature ($MANIFEST.sig); nothing was installed"

# verified <file> <sig> <namespace> -- the principal whose key signed exactly <file>
verified() {
  local p IFS=$'\n'
  for p in $(ssh-keygen -Y find-principals -s "$2" -f "$signers" </dev/null 2>/dev/null); do
    if ssh-keygen -Y verify -f "$signers" -I "$p" -n "$3" -s "$2" <"$1" >/dev/null 2>&1; then
      printf '%s\n' "$p"; return 0
    fi
  done
  return 1
}
signer="$(verified "$mf" "$mf.sig" "$MANIFEST_NAMESPACE")" \
  || die "the release manifest's signature does not verify against the release keys this installer carries; nothing was installed"

# The installer the verified bytes name (and nothing else of them is read here).
release="$(python3 -I - "$mf" "$CHANNEL" "$RELEASE_REPO" <<'PY'
import json, re, sys
manifest = json.load(open(sys.argv[1], "rb"))
if not isinstance(manifest, dict) or manifest.get("channel") != sys.argv[2]:
    sys.exit(f"the release manifest is signed for the {manifest.get('channel') if isinstance(manifest, dict) else None!r} channel, not {sys.argv[2]!r}")
brain = manifest.get("brain") if isinstance(manifest.get("brain"), dict) else {}
repo, tag, commit = brain.get("repo"), brain.get("tag"), brain.get("commit")
if not (isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        and isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*", tag)
        and isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit)):
    sys.exit(f"the release manifest names no installer to fetch (brain.repo {repo!r}, "
             f"brain.tag {tag!r}, brain.commit {commit!r})")
if repo != sys.argv[3]:
    sys.exit(f"the release manifest names {repo!r}, not {sys.argv[3]}: no installer is fetched from another repository")
# A release tag (manifest_sig.release_problem's rule): a candidate's only on the beta channel.
rc = re.fullmatch(r"brain-v[0-9]+\.[0-9]+\.[0-9]+(-rc[1-9][0-9]*)?", tag)
if not rc:
    sys.exit(f"the release manifest names {tag!r}, not a release tag (brain-vX.Y.Z)")
if rc.group(1) and sys.argv[2] != "beta":
    sys.exit(f"the release manifest names the release candidate {tag} on the {sys.argv[2]} channel: only beta offers one")
print(repo, tag, commit)
PY
)" || die "nothing was installed"
read -r repo tag commit <<<"$release"
REPO="${REPO:-https://github.com/$repo.git}"
echo "==> the $CHANNEL release is signed by $signer: $tag ($commit)"

export GIT_TERMINAL_PROMPT=0
# The tag must still be at the signed commit (a tag can be moved), and so must the clone.
at="$(git ls-remote "$REPO" "refs/tags/$tag" "refs/tags/$tag^{}" </dev/null \
      | awk -v t="refs/tags/$tag" '$2 == t"^{}" {peeled=$1} $2 == t {plain=$1} END {print (peeled != "" ? peeled : plain)}')" \
  || die "cannot read the tags of $REPO"
[ -n "$at" ] || die "$tag is not a tag of $REPO; nothing was installed"
[ "$at" = "$commit" ] \
  || die "$tag on $REPO is $at, not the commit the signed release names ($commit): the tag was moved; nothing was installed"
echo "==> fetching teaport ($tag)…"
# The tag itself, by its full ref (a branch of the same name is not it), over git's pack
# transport, which re-hashes every object it receives: never a clone from a plain path,
# which would hard-link a mirror's objects as they are (a forged one included), and with
# transfer.fsckObjects. Then the signed commit, checked out by its id.
co="$work/teaport"
git init --quiet "$co"
git -C "$co" -c transfer.fsckObjects=true fetch --quiet --depth 1 --no-tags "$REPO" \
    "+refs/tags/$tag:refs/tags/$tag" </dev/null \
  || die "cannot fetch $tag from $REPO; nothing was run"
[ "$(git -C "$co" rev-parse "refs/tags/$tag^{commit}")" = "$commit" ] \
  || die "the $tag fetched is not the signed commit $commit; nothing was run"
git -C "$co" -c advice.detachedHead=false checkout --quiet "$commit" </dev/null
head="$(git -C "$co" rev-parse HEAD)"
[ "$head" = "$commit" ] \
  || die "the checkout of $tag is at $head, not the signed commit $commit; nothing was run"

# Run the release's installer from the checkout, against the manifest verified above; it
# copies everything it needs into place, so the temp dir is disposable (removed on exit).
TEAPORT_MANIFEST_URL="file://$mf" bash "$co/install.sh" "$@"
