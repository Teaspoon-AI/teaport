#!/usr/bin/env bash
#
# build-deb.sh — build teaport-brain_<version>_<arch>.deb from this checkout (#60).
#
#   packaging/build-deb.sh                           dev build: <BASE_VERSION>~dev0+g<sha>
#   TEAPORT_DEB_BUILD=12 packaging/build-deb.sh      dev build: <BASE_VERSION>~dev12+g<sha>
#   TEAPORT_DEB_VERSION=0.1.0 packaging/build-deb.sh release build: must equal BASE_VERSION,
#                                                    HEAD must be tag brain-v0.1.0, tree clean
#
# The same script CI's brain-deb job runs, so a local build is the package CI would
# make from the same commit. The .deb lands in dist/ (TEAPORT_DEB_OUT overrides).
#
# Needs an Ubuntu 24.04 host (the appliance's release) with /usr/bin/python3.12 and
# sudo: the venv is built with the SYSTEM interpreter — pyvenv.cfg's home and every
# console script's shebang name it — and AT /opt/teaport/brain, the path it installs
# to, because a venv is not relocatable. So the build takes /opt/teaport/brain on the
# build host for its duration and removes it afterwards: an `apt install` of the result
# then puts back exactly the package's files, not a leftover tree that hides a missing
# one. It refuses to start while an installed teaport-brain owns that path.
#
# Versions: BASE_VERSION below is the NEXT brain release. Dev builds are
# <BASE_VERSION>~dev<n>+g<sha>, and `~` sorts before the release, so installing the
# release over any of its dev builds is an upgrade. A brain-v<X.Y.Z> tag builds X.Y.Z,
# which must equal BASE_VERSION; bump BASE_VERSION right after tagging.
#
# uv and nfpm are pinned by version and sha256, like every other tool this repo fetches:
# one already on PATH is used only at exactly the pinned version, otherwise the release
# binary is downloaded into ~/.cache/teaport-deb and verified.
#
set -euo pipefail

BASE_VERSION="0.1.0"
DEST=/opt/teaport/brain
PY=/usr/bin/python3.12
UV_VERSION="0.9.24"   # the same uv install.sh and CI's brain job pin
NFPM_VERSION="2.47.0"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
OUT="${TEAPORT_DEB_OUT:-$REPO/dist}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/teaport-deb"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
SUDO() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }

# --- version -------------------------------------------------------------------
rev="$(git -C "$REPO" rev-parse --short=8 HEAD)"
# brain/, cli/ and packaging/ are what the package is built from.
dirty="$(git -C "$REPO" --no-optional-locks status --porcelain -- brain cli packaging)"
if [ -n "${TEAPORT_DEB_VERSION:-}" ]; then
  VERSION="$TEAPORT_DEB_VERSION"
  [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "TEAPORT_DEB_VERSION must be X.Y.Z (got '$VERSION')"
  [ "$VERSION" = "$BASE_VERSION" ] || die "release $VERSION is not BASE_VERSION $BASE_VERSION (packaging/build-deb.sh) — dev builds of this tree were versioned below $BASE_VERSION, so bump BASE_VERSION to the release being cut"
  # A release version carries no +g<sha> or .dirty, so nothing in it would tell two
  # different X.Y.Z packages apart, and apt treats them as the same package. Build it
  # only from the tagged commit, unmodified.
  tag="brain-v$VERSION"
  tagged="$(git -C "$REPO" rev-parse -q --verify "refs/tags/$tag^{commit}" 2>/dev/null)" \
    || die "release $VERSION needs the tag $tag, and this checkout has no such tag"
  [ "$tagged" = "$(git -C "$REPO" rev-parse HEAD)" ] \
    || die "release $VERSION must be built from $tag ($(git -C "$REPO" rev-parse --short=8 "$tagged")), not HEAD $rev"
  [ -z "$dirty" ] || die "release $VERSION must be built from a clean tree; uncommitted changes under brain/ cli/ packaging/:
$dirty"
else
  build="${TEAPORT_DEB_BUILD:-0}"
  [[ "$build" =~ ^[0-9]+$ ]] || die "TEAPORT_DEB_BUILD must be a number (got '$build')"
  VERSION="$BASE_VERSION~dev$build+g$rev"
  [ -z "$dirty" ] || VERSION="$VERSION.dirty"
fi

case "$(uname -m)" in
  aarch64) ARCH=arm64; UV_TRIPLE=aarch64-unknown-linux-gnu; NFPM_ARCH=arm64
           UV_SHA=9b291a1a4f2fefc430e4fc49c00cb93eb448d41c5c79edf45211ceffedde3334
           NFPM_SHA=1c0f5f2999b9a974bfb04fdb0cc3306096de530ac5dbb25d739cc5f5219c919c ;;
  x86_64)  ARCH=amd64; UV_TRIPLE=x86_64-unknown-linux-gnu; NFPM_ARCH=x86_64
           UV_SHA=fb13ad85106da6b21dd16613afca910994446fe94a78ee0b5bed9c75cd066078
           NFPM_SHA=0660ca602b2d2d2ae4781a06c692b3eeb9d437ffea05b831d76e41f4a3188783 ;;
  *) die "no pinned uv/nfpm for $(uname -m)" ;;
esac

# --- tools ---------------------------------------------------------------------
# fetch <url> <sha256> <dest> — download, verify, keep. A cached file is re-verified.
fetch() {
  local url="$1" sha="$2" dest="$3"
  if [ ! -f "$dest" ] || ! echo "$sha  $dest" | sha256sum -c --status; then
    mkdir -p "$(dirname "$dest")"
    curl -fsSL --retry 3 --retry-connrefused -o "$dest.part" "$url"
    echo "$sha  $dest.part" | sha256sum -c --status || { rm -f "$dest.part"; die "sha256 mismatch: $url"; }
    mv "$dest.part" "$dest"
  fi
}

UV=""
if have uv && [ "$(uv --version | awk '{print $2}')" = "$UV_VERSION" ]; then
  UV="$(command -v uv)"
else
  tgz="$CACHE/uv-$UV_VERSION-$UV_TRIPLE.tar.gz"
  fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$UV_TRIPLE.tar.gz" "$UV_SHA" "$tgz"
  tar -xzf "$tgz" -C "$CACHE"
  UV="$CACHE/uv-$UV_TRIPLE/uv"
fi

NFPM=""
if have nfpm && [ "$(nfpm --version 2>/dev/null | sed -n 's/^GitVersion: *//p')" = "$NFPM_VERSION" ]; then
  NFPM="$(command -v nfpm)"
else
  tgz="$CACHE/nfpm_${NFPM_VERSION}_Linux_$NFPM_ARCH.tar.gz"
  fetch "https://github.com/goreleaser/nfpm/releases/download/v$NFPM_VERSION/nfpm_${NFPM_VERSION}_Linux_$NFPM_ARCH.tar.gz" "$NFPM_SHA" "$tgz"
  mkdir -p "$CACHE/nfpm-$NFPM_VERSION"
  tar -xzf "$tgz" -C "$CACHE/nfpm-$NFPM_VERSION" nfpm
  NFPM="$CACHE/nfpm-$NFPM_VERSION/nfpm"
fi

# --- build root ----------------------------------------------------------------
[ -x "$PY" ] || die "$PY not found — build on Ubuntu 24.04 with its python3.12 (the appliance's interpreter), not a managed or setup-python one"
if have dpkg && dpkg -S "$DEST" >/dev/null 2>&1; then
  die "$DEST belongs to an installed package (dpkg -S $DEST) — remove it first: sudo apt remove teaport-brain"
fi
if [ -e "$DEST" ]; then
  # A tree an earlier, interrupted run of this script left behind is ours to clear; any
  # other content is not.
  if [ -O "$DEST" ] && [ -f "$DEST/pyvenv.cfg" ]; then
    log "removing an earlier build's $DEST"
    rm -rf "$DEST"
  elif [ -n "$(ls -A "$DEST")" ]; then
    die "$DEST exists and is not a build tree of this script — move it aside and re-run"
  fi
fi
parent_existed=0; [ -d "$(dirname "$DEST")" ] && parent_existed=1
SUDO install -d -o "$(id -u)" -g "$(id -g)" -m 0755 "$DEST"
cleanup() {
  rm -rf "$DEST" 2>/dev/null || SUDO rm -rf "$DEST"
  if [ "$parent_existed" = 0 ]; then SUDO rmdir "$(dirname "$DEST")" 2>/dev/null || true; fi
}
trap cleanup EXIT

# --- venv ----------------------------------------------------------------------
# The flags install.sh's brain_stage uses, for the same reasons (see there), plus:
#   --compile-bytecode    ship the .pyc files. The installed tree is root-owned, so the
#                         brain's user could never write a __pycache__ into it: without
#                         them, every start would recompile all of pipecat in memory.
#   UV_PYTHON_DOWNLOADS=never, with --python-preference only-system: never a managed
#                         CPython, so the venv's interpreter is the box's /usr/bin one.
# umask 022 for what uv creates; the chmod below settles what it does not honour it for.
# The files go into the package with the modes they have here.
umask 022
log "uv sync --locked $REPO/brain -> $DEST (python $("$PY" -c 'import platform; print(platform.python_version())'))"
env UV_PROJECT_ENVIRONMENT="$DEST" UV_PYTHON_DOWNLOADS=never \
  "$UV" sync --locked --no-editable --no-dev --compile-bytecode --link-mode copy \
  --reinstall-package teaport-brain \
  --python "$PY" --python-preference only-system --project "$REPO/brain"
# uv's own bookkeeping in the venv root: its lock file (created 0666 whatever the umask,
# and world-writable has no place in a root-owned tree) and the markers that keep a
# project venv out of git and backups. None of them means anything once it is packaged.
rm -f "$DEST/.lock" "$DEST/.gitignore" "$DEST/CACHEDIR.TAG"
# Modes as a root-owned tree wants them, whatever uv created them with (on the arm64
# runner, a world-writable lib/ despite the umask): nothing writable but by
# the owner, everything readable, executables and directories searchable.
chmod -R u+rwX,go+rX,go-w "$DEST"
ww="$(find "$DEST" -perm /022 ! -type l -print -quit)"
[ -z "$ww" ] || die "group- or world-writable file in the build: $(ls -ld "$ww")"
home="$(sed -n 's/^home *= *//p' "$DEST/pyvenv.cfg")"
[ "$home" = /usr/bin ] || die "the venv's interpreter is '$home', not /usr/bin — it would not run on the appliance"

# --- package -------------------------------------------------------------------
mkdir -p "$OUT"
log "nfpm $NFPM_VERSION -> $OUT/teaport-brain_${VERSION}_$ARCH.deb"
(cd "$REPO" && TEAPORT_DEB_VERSION="$VERSION" TEAPORT_DEB_ARCH="$ARCH" \
  "$NFPM" pkg --config packaging/nfpm.yaml --packager deb --target "$OUT/")
deb="$OUT/teaport-brain_${VERSION}_$ARCH.deb"
[ -f "$deb" ] || die "nfpm did not produce $deb"
log "built $deb ($(du -h "$deb" | cut -f1))"
