#!/usr/bin/env python3
#
# teaport — check that every artifact a release manifest names is published, as named.
#
# install.sh downloads what the manifest at get.teaspoon.tech says and refuses a file
# whose sha256 differs. So a manifest that names an artifact not yet in the bucket, or
# one uploaded since with different bytes, breaks every fresh install. This fetches each
# url + sha256 entry the manifest has and hashes what the public URL actually serves. An
# entry with a url and no sha256 (models.voxtral.aux) must at least answer. The
# publish workflow runs it before it uploads a manifest, and on every PR that changes
# one, so a manifest goes up only once its artifacts are there.
#
# A placeholder ("TODO") entry means "not released yet": an error unless
# --placeholders-ok (beta.json is all placeholders until the first beta is cut).
#
#   python3 manifest/check.py manifest/stable.json
#   python3 manifest/check.py --placeholders-ok manifest/beta.json
#
# Stdlib only: it runs on a bare CI runner. The whole check downloads every artifact
# (~4.6 GB, mostly the Voxtral model) — minutes on a runner, which is why it runs only
# when a manifest changes.
#
import argparse
import hashlib
import json
import sys
import urllib.request
from urllib.parse import urlparse

# Every artifact is served from the download bucket (the manifest's own _note).
HOST = "get.teaspoon.tech"
CHUNK = 1 << 20


def entries(node, path=""):
    """(dotted path, url, sha256 or None) for every artifact the manifest names."""
    if isinstance(node, dict):
        if isinstance(node.get("url"), str):
            yield path, node["url"], node.get("sha256")
        for key, value in node.items():
            if key.startswith("_") or key in ("url", "sha256"):
                continue
            sub = f"{path}.{key}" if path else key
            if key == "aux" and isinstance(value, dict):
                # models.voxtral.aux: {filename: url}, no hash (install.sh downloads as-is).
                for name, url in value.items():
                    yield f"{sub}.{name}", url, None
            else:
                yield from entries(value, sub)


def fetch(url, digest):
    """Status of GET url, hashing the body into digest (None: just open it)."""
    req = urllib.request.Request(url, headers={"User-Agent": "teaport-manifest-check"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        if digest is not None:
            while chunk := resp.read(CHUNK):
                digest.update(chunk)
        return resp.status


def check(manifest_path, placeholders_ok):
    with open(manifest_path) as f:
        manifest = json.load(f)
    problems, checked = [], 0
    for where, url, sha in entries(manifest):
        if "TODO" in url or sha == "TODO":
            if not placeholders_ok:
                problems.append(f"{where}: placeholder ({url})")
            continue
        if urlparse(url).hostname != HOST or urlparse(url).scheme != "https":
            problems.append(f"{where}: not served from https://{HOST}/ ({url})")
            continue
        digest = hashlib.sha256() if sha else None
        try:
            fetch(url, digest)
        except Exception as e:  # HTTPError, URLError, timeouts: all mean "not published"
            problems.append(f"{where}: {url} -> {e}")
            continue
        checked += 1
        if digest is not None and digest.hexdigest() != sha:
            problems.append(f"{where}: {url} serves sha256 {digest.hexdigest()}, "
                            f"the manifest says {sha}")
        else:
            print(f"ok {where}" + ("" if sha else " (no sha256 to check; it answers)"))
    for p in problems:
        print(f"::error file={manifest_path}::{p}")
    print(f"{manifest_path}: {checked} artifact(s) checked, {len(problems)} problem(s)")
    return not problems


def main():
    ap = argparse.ArgumentParser(description="Check that a release manifest's artifacts are published, as named.")
    ap.add_argument("manifests", nargs="+")
    ap.add_argument("--placeholders-ok", action="store_true",
                    help='allow "TODO" entries (a channel with nothing released yet)')
    args = ap.parse_args()
    ok = all([check(m, args.placeholders_ok) for m in args.manifests])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
