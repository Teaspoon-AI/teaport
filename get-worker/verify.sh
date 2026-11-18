#!/usr/bin/env bash
#
# get-worker/verify.sh <open|enforce> [base]  — check what get.teaspoon.tech serves, after a
# deploy or a domain move (docs/engine-license-gate.md). Read-only: it records no
# acceptance (that is the Jetson install's job) and downloads only small files, plus the
# first byte of the gated ones.
#
#   public objects        200, the same bytes as the manifest's sha256 says (or as before)
#   / and a missing key   404
#   engine + models       open: 200 to a plain request; enforce: 403
#   the license endpoint  answers (400 to an empty body: it validates before it records)
#
set -uo pipefail
mode="${1:-}"; base="${2:-https://get.teaspoon.tech}"
case "$mode" in open|enforce) ;; *) echo "usage: $0 <open|enforce> [base]" >&2; exit 2 ;; esac
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mf="$here/../manifest/stable.json"
fail=0
code() { curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "$@" 2>/dev/null || echo 000; }
expect() {  # expect <want> <what> <curl args...>
  local want="$1" what="$2"; shift 2
  local got; got="$(code "$@")"
  if [ "$got" = "$want" ]; then echo "ok   $got  $what"; else echo "FAIL $got  $what (want $want)"; fail=1; fi
}
sha_of() {  # sha_of <manifest key> -> sha256
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."): d=d[k]
print(d)' "$mf" "$1"
}
same() {  # same <path> <sha256>
  local got; got="$(curl -fsS --max-time 60 "$base/$1" | sha256sum | cut -d' ' -f1)"
  if [ "$got" = "$2" ]; then echo "ok   sha  /$1"; else echo "FAIL sha  /$1 ($got, want $2)"; fail=1; fi
}

expect 200 /manifest/stable.json "$base/manifest/stable.json"
expect 200 /manifest/beta.json "$base/manifest/beta.json"
expect 200 /teaport "$base/teaport"
expect 404 / "$base/"
expect 404 /no-such-object "$base/no-such-object"
same "eula/teaport-engine-license-$(sha_of engine.eula.version).txt" "$(sha_of engine.eula.sha256)"
notices="$(sha_of engine.notices.url)"; same "${notices#https://get.teaspoon.tech/}" "$(sha_of engine.notices.sha256)"
expect 206 "/dl/brain (public, first byte)" -r 0-0 "$(sha_of brain.deb.url | sed "s|https://get.teaspoon.tech|$base|")"
want=403; [ "$mode" = open ] && want=206
for key in engine.assets.l4t38-cu13.url engine.g2p_dict.url engine.pos_model.url models.voxtral.url models.kokoro.url; do
  u="$(sha_of "$key" | sed "s|https://get.teaspoon.tech|$base|")"
  expect "$want" "${u#"$base"} (plain request, first byte)" -r 0-0 "$u"
done
expect 400 "POST /v1/engine-license/accept (empty body)" -X POST -H 'content-type: application/json' --data '{}' "$base/v1/engine-license/accept"
expect 405 "GET /v1/engine-license/accept" "$base/v1/engine-license/accept"

if [ "$fail" = 0 ]; then echo "all ok ($mode)"; else echo "FAILED ($mode)"; fi
exit "$fail"
