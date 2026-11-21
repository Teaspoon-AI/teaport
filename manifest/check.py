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
#   - every entry in REQUIRED (and engine.watermark from engine 1.7 on) is present with
#     a url and a sha256;
#   - every url + sha256 entry is fetched and the body's sha256 compared;
#   - a url with no sha256 (models.voxtral.aux in a manifest without aux_sha256) must answer;
#   - every {repo, tag} node must resolve with an anonymous `git ls-remote`, as a
#     branch or tag (what `git clone --branch` takes); a private repo fails;
#   - openclaw names the OpenClaw release this one is tested with, a version npm has,
#     and its Node.js major (the guide installs both, install.sh compares the box's).
#     Required in every manifest, and checked against registry.npmjs.org: a manifest
#     without it, or an npm registry that does not answer, fails the check.
#   - access.gated (the engine license gate's download prefixes) and access.api are
#     https URLs on the download host. A gated artifact is fetched with DL_CHECK_TOKEN (the
#     download gate Worker's CHECK_TOKEN) and its sha256 compared; without the token (a
#     pull request's run) it must refuse an anonymous download (HTTP 401), which is all
#     that is checked of it. One served without a token means the gate is not in front of
#     it: an error either way.
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
import re
import http.client
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

# The url + sha256 entries install.sh cannot install without (phase_engine, eula_gate
# and brain_deb_fetch; its engine asset key is ASSET_KEY). Keep in step with install.sh's
# mget reads: it points back here.
REQUIRED = (
    "brain.deb",
    "engine.eula",
    "engine.assets.l4t38-cu13",
    "engine.g2p_dict",
    "engine.pos_model",
    "engine.notices",
    "models.voxtral",
    "models.kokoro",
)
# engine.watermark (the AI watermark's weights) is required from the first engine release
# that marks its speech on: install.sh's engine_release_marks, which counts a release that
# is not a dotted number as marking. Keep WATERMARK_FROM in step with its
# ENGINE_WATERMARK_FROM.
WATERMARK_FROM = "1.7"
# The {repo, tag} entries install.sh clones (brain: the Discord bridge's source).
REQUIRED_REPOS = ("brain",)
NPM_REGISTRY = "https://registry.npmjs.org"


def release_marks(release):
    """Whether that engine release puts the AI watermark on its speech (install.sh's
    engine_release_marks): from WATERMARK_FROM on, and anything not a dotted number."""
    if not isinstance(release, str) or not re.fullmatch(r"[0-9]+(\.[0-9]+)*", release):
        return True
    num = lambda v: tuple(int(x) for x in v.split("."))
    return num(release) >= num(WATERMARK_FROM)


def required(manifest):
    """The url + sha256 entries install.sh cannot install this manifest without."""
    marks = release_marks(lookup(manifest, "engine.release"))
    return REQUIRED + (("engine.watermark",) if marks else ())


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
            if key == "aux_sha256":
                continue   # read with aux, below
            if key == "aux" and isinstance(value, dict):
                # models.voxtral.aux: {filename: url}, its hashes beside it in aux_sha256
                # (an older manifest has none: install.sh downloads them as-is).
                hashes = node.get("aux_sha256") if isinstance(node.get("aux_sha256"), dict) else {}
                for name, url in value.items():
                    if isinstance(url, str):
                        yield f"{sub}.{name}", url, hashes.get(name)
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


def fetch(url, digest, token=None):
    """GET url, hashing the body into digest (None: just open it), with a download token
    when given. Retries a failed transfer from the start, as a fresh digest."""
    headers = {"User-Agent": "teaport-manifest-check"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(1, ATTEMPTS + 1):
        d = digest.copy() if digest is not None else None
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                while d is not None and (chunk := resp.read(CHUNK)):
                    d.update(chunk)
            return d
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == ATTEMPTS:
                raise
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            # HTTPException: IncompleteRead, a transfer cut off mid-body.
            if attempt == ATTEMPTS:
                raise
        time.sleep(5 * attempt)


# refused(): the gate serves it without a token because its GATE is "open", for the
# switchover only (worker/src/index.js).
GATE_OPEN = "gate open"


def refused(url):
    """None when url refuses an anonymous download (HTTP 401), as a gated artifact must;
    GATE_OPEN when the gate serves it during the switchover; else what it did. A HEAD: an
    open gate would otherwise start sending the file."""
    req = urllib.request.Request(url, headers={"User-Agent": "teaport-manifest-check"}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.headers.get("x-teaport-gate") == "open":
                return GATE_OPEN
    except urllib.error.HTTPError as e:
        return None if e.code == 401 else f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 -- no answer at all
        return str(e)
    return "served without a download token: the license gate is not in front of it"


def gate(manifest):
    """(gated URL prefixes, problems) of the manifest's access entry."""
    access = manifest.get("access")
    if access is None:
        return [], []
    problems, prefixes = [], []
    if not isinstance(access, dict):
        return [], [f"access: not an object ({access!r})"]
    api, gated = access.get("api"), access.get("gated")
    if not isinstance(api, str) or urlparse(api).scheme != "https" or urlparse(api).hostname != HOST:
        problems.append(f"access.api: not an https URL on {HOST} ({api!r})")
    if not isinstance(gated, list) or not gated:
        problems.append(f"access.gated: not a list of URL prefixes ({gated!r})")
    else:
        for p in gated:
            if (not isinstance(p, str) or urlparse(p).scheme != "https" or urlparse(p).hostname != HOST
                    or not p.endswith("/")):
                problems.append(f"access.gated: not an https URL prefix on {HOST} ending in / ({p!r})")
            else:
                prefixes.append(p)
    return prefixes, problems


def resolves(repo, tag):
    """Whether an anonymous clone of github.com/<repo> at <tag> would work, as
    install.sh's `git clone --branch <tag>` does it. None if so, else why not."""
    # Exact refs: ls-remote matches a pattern's tail, so a bare "main" would also
    # accept refs/heads/heads/main or refs/tags/release/main, which clone rejects.
    p = subprocess.run(
        ["git", "ls-remote", "--exit-code", f"https://github.com/{repo}.git",
         f"refs/heads/{tag}", f"refs/tags/{tag}"],
        capture_output=True, text=True, timeout=60, cwd="/",
        # Anonymous, as on a fresh box: no credential helper or token from this
        # machine's git config (cwd "/" keeps a repository's own config out too), and
        # no prompt.
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
    prefixes, problems = gate(manifest)
    token = os.environ.get("DL_CHECK_TOKEN", "")

    def placeholder(where, value):
        if not placeholders_ok:
            problems.append(f"{where}: placeholder ({value})")

    for where in required(manifest):
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
        if any(url.startswith(p) for p in prefixes) and not token:
            why = refused(url)
            if why == GATE_OPEN:
                checked += 1
                print(f"::warning::{where}: {url} is served without a download token: the gate's "
                      "GATE is open (the switchover). Set GATE = \"enforce\" in worker/wrangler.toml "
                      "and deploy once the installer that accepts is merged")
            elif why:
                problems.append(f"{where}: {url} -> {why}")
            else:
                checked += 1
                print(f"ok {where} (gated: refused without a token; its contents are checked "
                      "with DL_CHECK_TOKEN)")
            continue
        try:
            digest = fetch(url, hashlib.sha256() if sha else None,
                           token if any(url.startswith(p) for p in prefixes) else None)
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

    openclaw = manifest.get("openclaw")
    if not isinstance(openclaw, dict):
        problems.append("openclaw: missing (the OpenClaw release this one is tested with)")
    else:
        version, node = str(openclaw.get("version", "")), str(openclaw.get("node", ""))
        if not re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}", version):
            problems.append(f"openclaw.version: not an OpenClaw release ({version!r})")
        elif not re.fullmatch(r"\d{2}", node):
            problems.append(f"openclaw.node: not a Node.js major ({node!r})")
        else:
            try:
                fetch(f"{NPM_REGISTRY}/openclaw/{version}", None)
            except Exception as e:  # noqa: BLE001 -- not published, or no registry
                problems.append(f"openclaw.version: npm has no openclaw@{version} ({e})")
            else:
                checked += 1
                print(f"ok openclaw (npm openclaw@{version}, Node.js {node})")

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
