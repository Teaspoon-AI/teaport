#!/usr/bin/env python3
#
# teaport — check that a release manifest names everything install.sh needs, and that it
# is all published, as named.
#
# install.sh reads the manifest at get.teaspoon.tech. It downloads each artifact and
# refuses one whose sha256 differs, dies when a required entry is missing, and
# `git clone`s the repo + tag entries anonymously. So a manifest that lacks an entry,
# names an artifact not yet in the bucket (or since re-uploaded with other bytes), or
# names a tag nobody can clone, breaks every fresh install. This checks all of it
# against the public URLs:
#   - every entry in REQUIRED is present with a url and a sha256;
#   - every url + sha256 entry is fetched and the body's sha256 compared;
#   - a url with no sha256 (models.voxtral.aux) must answer;
#   - every {repo, tag} node must resolve with an anonymous `git ls-remote`.
# The publish workflow runs it on every PR that changes a manifest, before every upload,
# and weekly, which catches an artifact re-uploaded under an unchanged URL.
#
# A placeholder ("TODO") entry means "not released yet": an error unless
# --placeholders-ok (beta.json is all placeholders until the first beta is cut).
#
#   python3 manifest/check.py manifest/stable.json
#   python3 manifest/check.py --placeholders-ok manifest/beta.json
#
# Stdlib + git only: it runs on a bare CI runner. A full check downloads every artifact
# (~4.6 GB, mostly the Voxtral model): minutes on a runner.
#
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

# Every artifact is served from the download bucket (the manifest's own _note).
HOST = "get.teaspoon.tech"
CHUNK = 1 << 20
ATTEMPTS = 3  # install.sh downloads with curl --retry 3

# The url + sha256 entries install.sh cannot install without (phase_engine and
# eula_gate; its engine asset key is ASSET_KEY). Keep in step with install.sh's mget
# reads: it points back here.
REQUIRED = (
    "engine.eula",
    "engine.assets.l4t38-cu13",
    "engine.g2p_dict",
    "engine.pos_model",
    "engine.notices",
    "models.voxtral",
    "models.kokoro",
)
# The {repo, tag} entries install.sh always clones (the brain); oled_avatar is cloned
# only on request, so it is checked when present.
REQUIRED_REPOS = ("brain",)


def lookup(manifest, dotted):
    node = manifest
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def entries(node, path=""):
    """(dotted path, url, sha256 or None) for every artifact the manifest names, and
    (dotted path, None, problem) for a url that is not a string."""
    if isinstance(node, dict):
        if "url" in node:
            if isinstance(node["url"], str):
                yield path, node["url"], node.get("sha256")
            else:
                yield path, None, f"url is not a string: {node['url']!r}"
        for key, value in node.items():
            if key.startswith("_") or key in ("url", "sha256"):
                continue
            sub = f"{path}.{key}" if path else key
            if key == "aux" and isinstance(value, dict):
                # models.voxtral.aux: {filename: url}, no hash (install.sh downloads as-is).
                for name, url in value.items():
                    if isinstance(url, str):
                        yield f"{sub}.{name}", url, None
                    else:
                        yield f"{sub}.{name}", None, f"url is not a string: {url!r}"
            else:
                yield from entries(value, sub)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from entries(value, f"{path}[{i}]")


def repos(node, path=""):
    """(dotted path, repo, tag) for every {repo, tag} node."""
    if isinstance(node, dict):
        if "repo" in node and "tag" in node:
            yield path, node["repo"], node["tag"]
        for key, value in node.items():
            if not key.startswith("_"):
                yield from repos(value, f"{path}.{key}" if path else key)


def fetch(url, digest):
    """GET url, hashing the body into digest (None: just open it). Retries a failed
    transfer from the start, as a fresh digest."""
    for attempt in range(1, ATTEMPTS + 1):
        d = digest.copy() if digest is not None else None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "teaport-manifest-check"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                while d is not None and (chunk := resp.read(CHUNK)):
                    d.update(chunk)
            return d
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == ATTEMPTS:
                raise
        except (urllib.error.URLError, OSError):
            if attempt == ATTEMPTS:
                raise
        time.sleep(5 * attempt)


def resolves(repo, tag):
    """Whether an anonymous clone of github.com/<repo> at <tag> would work, as
    install.sh's `git clone --branch <tag>` does it. None if so, else why not."""
    p = subprocess.run(
        ["git", "ls-remote", "--exit-code", f"https://github.com/{repo}.git", tag],
        capture_output=True, text=True, timeout=60,
        # Anonymous, as on a fresh box: no credential helper or token from this
        # machine's git config, and no prompt.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true",
             "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    if p.returncode == 0:
        return None
    if p.returncode == 2:
        return f"no branch or tag {tag!r} in github.com/{repo}"
    return (f"github.com/{repo} is not cloneable anonymously "
            f"({(p.stderr.strip().splitlines() or ['?'])[-1]})")


def check(manifest_path, placeholders_ok):
    with open(manifest_path) as f:
        manifest = json.load(f)
    problems, checked = [], 0

    def placeholder(where, value):
        if not placeholders_ok:
            problems.append(f"{where}: placeholder ({value})")

    for where in REQUIRED:
        node = lookup(manifest, where)
        if not isinstance(node, dict) or not node.get("url") or not node.get("sha256"):
            problems.append(f"{where}: missing, or without a url and a sha256 "
                            "(install.sh needs it)")
    for where in REQUIRED_REPOS:
        node = lookup(manifest, where)
        if not isinstance(node, dict) or not node.get("repo") or not node.get("tag"):
            problems.append(f"{where}: missing, or without a repo and a tag (install.sh "
                            "clones it)")

    for where, url, sha in entries(manifest):
        if url is None:
            problems.append(f"{where}: {sha}")
            continue
        if "TODO" in url or sha == "TODO":
            placeholder(where, url)
            continue
        if urlparse(url).hostname != HOST or urlparse(url).scheme != "https":
            problems.append(f"{where}: not served from https://{HOST}/ ({url})")
            continue
        try:
            digest = fetch(url, hashlib.sha256() if sha else None)
        except Exception as e:  # HTTPError, URLError, timeouts: all mean "not published"
            problems.append(f"{where}: {url} -> {e}")
            continue
        checked += 1
        if digest is not None and digest.hexdigest() != sha:
            problems.append(f"{where}: {url} serves sha256 {digest.hexdigest()}, "
                            f"the manifest says {sha}")
        else:
            print(f"ok {where}" + ("" if sha else " (no sha256 to check; it answers)"))

    for where, repo, tag in repos(manifest):
        if "TODO" in f"{repo}{tag}":
            placeholder(where, f"{repo}@{tag}")
            continue
        why = resolves(repo, tag)
        if why:
            problems.append(f"{where}: {why}")
        else:
            checked += 1
            print(f"ok {where} (github.com/{repo} @ {tag})")

    if checked == 0 and not placeholders_ok:
        problems.append("no artifact checked: the manifest names nothing installable")
    for p in problems:
        print(f"::error file={manifest_path}::{p}")
    print(f"{manifest_path}: {checked} entr{'y' if checked == 1 else 'ies'} checked, "
          f"{len(problems)} problem(s)")
    return not problems


def main():
    ap = argparse.ArgumentParser(
        description="Check that a release manifest names everything install.sh needs, "
                    "published as named.")
    ap.add_argument("manifests", nargs="+")
    ap.add_argument("--placeholders-ok", action="store_true",
                    help='allow "TODO" entries (a channel with nothing released yet)')
    args = ap.parse_args()
    ok = all([check(m, args.placeholders_ok) for m in args.manifests])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
