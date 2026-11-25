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
#   - every entry in REQUIRED (and engine.watermark and engine.zh_dict from engine 1.7
#     on) is present with a url and a sha256;
#   - every url entry has a sha256 (install.sh refuses one it cannot check; the
#     models.voxtral.aux files' are in models.voxtral.aux_sha256, by file name), and is
#     fetched and the body's sha256 compared (a gated one as below);
#   - every {repo, tag} node must resolve with an anonymous `git ls-remote`, as a
#     branch or tag (what `git clone --branch` takes); a private repo fails. The brain's
#     (the installer `teaport update` runs as root) must also name brain.commit, and its
#     tag must be a tag at exactly that commit: the signature pins the commit, not a tag
#     anyone with push access can move;
#   - with access, a brain.tag that is a release tag must be an installer that reads it:
#     its install.sh says ACCESS_PROTOCOL=1. `teaport update` runs that tag's installer as
#     root, and one from before the gate would download the gated files without a token.
#   - openclaw names the OpenClaw release this one is tested with, a version npm has,
#     and its Node.js major (the guide installs both, install.sh compares the box's).
#     Required in every manifest, and checked against registry.npmjs.org: a manifest
#     without it, or an npm registry that does not answer, fails the check.
#   - first of all, its signature: <manifest>.sig beside it must verify against the
#     release keys in manifest/allowed_signers (brain/teaport_brain/manifest_sig.py, the
#     check every box runs), and a <channel>.json must say it is that channel's. A box
#     installs nothing from a manifest whose signature does not verify, so publishing one
#     would break every install and update on the channel. The release's brain.commit
#     must carry exactly this tree's manifest/allowed_signers (and, in the full check, its
#     .deb exactly the commit's file, once). With
#     --boxes-signers the signature must also verify against the keys the boxes on the
#     channel have, which git records: the allowed_signers at the brain.commit that the
#     --base commit's manifest/<channel>.json names (--base is the last uploaded commit, so
#     its manifest is what is published), or, while that names no release with keys, those of
#     --base's install-bootstrap.sh. Boxes trust the keys of the package they have, so a
#     manifest signed only by a key added since would be refused by every one of them, the
#     update that brings the new keys included. The one manifest let through with other
#     keys than the tree's is a re-signature: byte for byte --base's, naming the release
#     --base names. Nothing here reads trust from the bucket; the box side is what protects
#     boxes from what the bucket serves.
#   - access.gated (the engine license gate's download prefixes) and access.api are
#     https URLs on the download host. A gated artifact must refuse an anonymous download
#     (HTTP 401), asked on every run; one served without a token means the gate is not in
#     front of it, an error, and one the gate serves open (GATE = "open") is a warning.
#     Then it is fetched with DL_CHECK_TOKEN (the download gate Worker's CHECK_TOKEN) and
#     its sha256 compared; without the token (a pull request's run) the refusal is all
#     that is checked of it. --require-enforce (the weekly run) makes an open gate an error.
#   - a gated artifact is also asked for with the slashes of its path percent-encoded
#     (dl%2Fengine%2Fx, dl/engine%2Fx): the Worker's routes do not match those, so the
#     public bucket answers them, and a copy of the file left there would be served around
#     the gate. Either form answering 2xx is an error; a 404 (any refusal) passes.
# The publish workflow runs it on every PR that changes a manifest, before every upload,
# and weekly, which catches an artifact re-uploaded under an unchanged URL. Nothing here
# signs: the signature is made offline (packaging/RELEASING.md) and committed beside the
# manifest; CI only checks it.
#
# A placeholder ("TODO") entry means "not released yet": an error unless
# --placeholders-ok (beta.json is all placeholders until the first beta is cut). Under
# --placeholders-ok a manifest whose every artifact is a placeholder may also go unsigned:
# it names nothing to install, and boxes refuse it all the same. One that names anything
# installable must be signed, and the signer signs none with a sha256 or url placeholder:
# those are filled in first (packaging/RELEASING.md); other TODO fields may stay.
#
#   python3 manifest/check.py manifest/stable.json
#   python3 manifest/check.py --placeholders-ok manifest/beta.json
#   python3 manifest/check.py --signature-only manifest/stable.json   (no downloads)
#   python3 manifest/check.py --boxes-signers manifest/stable.json     (against --base: CI
#        passes the last uploaded commit, manifest/last_upload.py; by default the merge-base
#        with origin/main)
#   python3 manifest/check.py --bootstrap install-bootstrap.sh         (the one-line installer:
#        it carries manifest/allowed_signers unchanged, and its .sig verifies, namespace
#        teaport-bootstrap)
#
# Stdlib + git only: it runs on a bare CI runner. A full check downloads every artifact
# (~4.6 GB, mostly the Voxtral model): minutes on a runner.
#
import argparse
import contextlib
import hashlib
import importlib.util
import re
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

# Every artifact is served from the download bucket (the manifest's own _note).
HOST = "get.teaspoon.tech"
HERE = os.path.dirname(os.path.abspath(__file__))
# The keys a manifest may be signed with, and the check boxes run (by its path: this runs on
# a bare runner, with no brain installed).
SIGNERS = os.path.join(HERE, "allowed_signers")
SIG_CHECK = os.path.join(os.path.dirname(HERE), "brain", "teaport_brain", "manifest_sig.py")
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
# Entries required from an engine release on, by install.sh's engine_release_from, which
# counts a release that is not a dotted number as later: engine.watermark (the AI
# watermark's weights) from the first release that marks its speech, engine.zh_dict (the
# Mandarin dict) from the first that reads it. Keep WATERMARK_FROM and ZH_FROM in step with
# its ENGINE_WATERMARK_FROM and ENGINE_ZH_FROM.
WATERMARK_FROM = "1.7"
ZH_FROM = "1.7"
# The {repo, tag} entries install.sh clones (brain: the Discord bridge's source).
REQUIRED_REPOS = ("brain",)
# The one repository a release names (update_apply.REPO; the bootstrap, updates.source() and
# install.sh's bridge hold it to the same).
RELEASE_REPO = "Teaspoon-AI/teaport"
GITHUB = "https://github.com"
# What an installer that reads access says (install.sh, beside UPDATE_PROTOCOL=1).
ACCESS_PROTOCOL = "\nACCESS_PROTOCOL=1\n"
NPM_REGISTRY = "https://registry.npmjs.org"


def release_from(release, first):
    """Whether that engine release is first or later (install.sh's engine_release_from):
    anything not a dotted number counts as later."""
    if not isinstance(release, str) or not re.fullmatch(r"[0-9]+(\.[0-9]+)*", release):
        return True
    num = lambda v: tuple(int(x) for x in v.split("."))
    return num(release) >= num(first)


def release_marks(release):
    """Whether that engine release puts the AI watermark on its speech."""
    return release_from(release, WATERMARK_FROM)


def required(manifest):
    """The url + sha256 entries install.sh cannot install this manifest without."""
    release = lookup(manifest, "engine.release")
    return (REQUIRED + (("engine.watermark",) if release_marks(release) else ())
            + (("engine.zh_dict",) if release_from(release, ZH_FROM) else ()))


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
                continue   # read with "aux", below
            if key == "aux" and isinstance(value, dict):
                # models.voxtral.aux: {filename: url}, their sha256s by the same file name in
                # the sibling aux_sha256 (a separate map, so installers that read aux as
                # {filename: url} still parse the manifest).
                shas = node.get("aux_sha256") if isinstance(node.get("aux_sha256"), dict) else {}
                for name, url in value.items():
                    if isinstance(url, str):
                        yield f"{sub}.{name}", url, shas.get(name)
                    else:
                        yield f"{sub}.{name}", None, f"url is not a string: {url!r}"
            else:
                yield from entries(value, sub)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from entries(value, f"{path}[{i}]")


def repos(node, path=""):
    """(dotted path, repo, tag, commit or None) for every {repo, tag} node."""
    if isinstance(node, dict):
        if "repo" in node and "tag" in node:
            yield path, node["repo"], node["tag"], node.get("commit")
        for key, value in node.items():
            if not key.startswith("_"):
                yield from repos(value, f"{path}.{key}" if path else key)


def fetch(url, digest, token=None, dest=None):
    """GET url, hashing the body into digest (None: just open it), with a download token
    when given (a gated file), and writing it to <dest> when given. Retries a failed
    transfer from the start, as a fresh digest."""
    headers = {"User-Agent": "teaport-manifest-check"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(1, ATTEMPTS + 1):
        d = digest.copy() if digest is not None else None
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp, \
                    (open(dest, "wb") if dest else contextlib.nullcontext()) as out:
                while d is not None and (chunk := resp.read(CHUNK)):
                    d.update(chunk)
                    if out is not None:
                        out.write(chunk)
            return d
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == ATTEMPTS:
                raise
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            # HTTPException: IncompleteRead, a transfer cut off mid-body.
            if attempt == ATTEMPTS:
                raise
        time.sleep(5 * attempt)


# refused(): the gate serves it without a token because its GATE is "open": the switchover,
# or the way back if enforcing broke installs (worker/src/index.js).
GATE_OPEN = "gate open"


def refused(url):
    """None when url refuses an anonymous download (HTTP 401), as a gated artifact must;
    GATE_OPEN when the gate serves it open (GATE = "open"); else what it did. A HEAD: an
    open gate would otherwise start sending the file. Retried as fetch() is: it runs before
    every upload, where one blip would leave the manifest unpublished."""
    req = urllib.request.Request(url, headers={"User-Agent": "teaport-manifest-check"}, method="HEAD")
    for attempt in range(1, ATTEMPTS + 1):
        try:
            # A HEAD answers at once: a shorter timeout than a download's, so a gate that
            # is down fails the run in minutes, not past the job's timeout.
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.headers.get("x-teaport-gate") == "open":
                    return GATE_OPEN
            return "served without a download token: the license gate is not in front of it"
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == ATTEMPTS:
                return None if e.code == 401 else f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            if attempt == ATTEMPTS:
                return str(e)
        except Exception as e:  # noqa: BLE001 -- anything else: a problem listed, not a traceback
            return str(e)
        time.sleep(5 * attempt)


def encoded_forms(url, prefix):
    """The gated url under prefix with the slashes of its path percent-encoded: all of them
    (/dl%2Fengine%2Fx) and the one ending the prefix (/dl/engine%2Fx). The Worker is routed
    on get.teaspoon.tech/dl/engine/* and /dl/models/*, which match neither, so the public
    bucket teaport-get answers them: from a copy of the file left there, it serves it to
    anyone, around the gate."""
    u, p = urlparse(url), urlparse(prefix).path
    base, rest = f"{u.scheme}://{u.netloc}", u.path[len(p):]
    forms = [base + "/" + quote(u.path[1:], safe=""), base + p[:-1] + "%2F" + quote(rest, safe="")]
    return list(dict.fromkeys(forms))


def bypassed(url):
    """None when url (an encoded_forms() URL) is refused, as it must be: a 404, or any other
    4xx. Else what it did: a 2xx serves the file around the gate; a 5xx or no answer, after
    the retries refused() makes, leaves it unknown, and fails too. A HEAD."""
    req = urllib.request.Request(url, headers={"User-Agent": "teaport-manifest-check"}, method="HEAD")
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return (f"HTTP {resp.status}: served without the license gate (a copy of the file "
                        "is public in the bucket; delete it)")
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return None
            if attempt == ATTEMPTS:
                return f"HTTP {e.code}: cannot tell whether it is served around the license gate"
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            if attempt == ATTEMPTS:
                return f"cannot tell whether it is served around the license gate ({e})"
        except Exception as e:  # noqa: BLE001 -- anything else: a problem listed, not a traceback
            return str(e)
        time.sleep(5 * attempt)


# access.api and each access.gated prefix: https on the download host, a plain path of
# segments (no userinfo, port, query, fragment, "." or ".."); the api without a trailing
# slash (install.sh appends /accept and /token), a prefix with one. The signing helper
# (sign_manifest.py ACCESS_URL) and install.sh hold them to the same rule.
ACCESS_URL = re.compile(r"https://" + re.escape(HOST) + r"((?:/(?!\.\.?(?:/|$))[A-Za-z0-9._~-]+)+)(/?)")


def access_url(value, prefix):
    """Whether <value> is an access URL as ACCESS_URL says: a gated prefix (<prefix>) ends in
    "/", the api does not."""
    m = ACCESS_URL.fullmatch(value) if isinstance(value, str) else None
    return bool(m) and (m.group(2) == "/") == prefix


def gate(manifest):
    """(gated URL prefixes, problems) of the manifest's access entry."""
    access = manifest.get("access")
    if access is None:
        return [], []
    problems, prefixes = [], []
    if not isinstance(access, dict):
        return [], [f"access: not an object ({access!r})"]
    api, gated = access.get("api"), access.get("gated")
    if not access_url(api, False):
        # install.sh POSTs who accepts, and later the acceptance id, to <api>/accept and
        # <api>/token: anywhere else would take both.
        problems.append(f"access.api: not https://{HOST}/<path>, without a trailing slash ({api!r})")
    if not isinstance(gated, list) or not gated:
        problems.append(f"access.gated: not a list of URL prefixes ({gated!r})")
    else:
        for p in gated:
            if not access_url(p, True):
                problems.append(f"access.gated: not an https URL prefix on {HOST} ending in / ({p!r})")
            else:
                prefixes.append(p)
    return prefixes, problems


def git(*args, cwd="/", timeout=60):
    """git, anonymously, as on a fresh box: no credential helper or token from this
    machine's git config (cwd "/" keeps a repository's own config out too), and no prompt."""
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=timeout, cwd=cwd,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true",
             "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})


def resolves(repo, tag, commit=None):
    """Whether an anonymous clone of github.com/<repo> at <tag> would work, as
    install.sh's `git clone --branch <tag>` does it, and, given <commit>, whether <tag> is
    a tag at exactly that commit (what update_apply.py holds it to). None if so, else why
    not."""
    # Exact refs: ls-remote matches a pattern's tail, so a bare "main" would also
    # accept refs/heads/heads/main or refs/tags/release/main, which clone rejects.
    p = git("ls-remote", "--exit-code", f"{GITHUB}/{repo}.git", f"refs/heads/{tag}", f"refs/tags/{tag}",
            f"refs/tags/{tag}^{{}}")
    if p.returncode == 0:
        if commit is None:
            return None
        refs = dict(reversed(ln.split("\t", 1)) for ln in p.stdout.splitlines() if "\t" in ln)
        at = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
        if at is None:
            return f"{tag!r} is not a tag of github.com/{repo} (brain.commit pins a release tag)"
        if at != commit:
            return f"the tag {tag} of github.com/{repo} is {at}, not brain.commit {commit}"
        return None
    if p.returncode == 2:
        return f"no branch or tag {tag!r} in github.com/{repo}"
    return (f"github.com/{repo} is not cloneable anonymously "
            f"({(p.stderr.strip().splitlines() or ['?'])[-1]})")


_SIG_CHECK_MODULE = []


def _sig_check():
    """brain/teaport_brain/manifest_sig.py, loaded once (one SignatureError class for all)."""
    if not _SIG_CHECK_MODULE:
        spec = importlib.util.spec_from_file_location("manifest_sig", SIG_CHECK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SIG_CHECK_MODULE.append(mod)
    return _SIG_CHECK_MODULE[0]


def installable(manifest):
    """Whether the manifest names a release a box could install: a brain.commit, or any of
    the artifacts install.sh cannot install without (REQUIRED) that is not a placeholder.
    One extra artifact filled in ahead of its release (beta's watermark weights, before the
    engine release that reads them) installs nothing: install.sh stops at the first
    placeholder it needs."""
    if re.fullmatch(r"[0-9a-f]{40}", str(lookup(manifest, "brain.commit") or "")):
        return True
    for where in REQUIRED:
        node = lookup(manifest, where)
        if isinstance(node, dict) and isinstance(node.get("url"), str) and "TODO" not in node["url"] \
                and node.get("sha256") != "TODO":
            return True
    return False


def git_show(ref, path, repo=RELEASE_REPO):
    """The bytes of <path> at <ref> (a commit id, or a ref this checkout has), fetching a
    commit id from github.com/<repo> when this checkout lacks it: (bytes or None when it has
    no such file, problem or None)."""
    git = ["git", "-C", os.path.dirname(HERE)]
    if subprocess.run([*git, "cat-file", "-e", f"{ref}^{{commit}}"], capture_output=True).returncode != 0:
        if re.fullmatch(r"[0-9a-f]{40}", ref) and isinstance(repo, str) \
                and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            try:
                subprocess.run([*git, "fetch", "--quiet", "--no-tags", "--depth=1",
                                f"{GITHUB}/{repo}.git", ref], capture_output=True, timeout=120)
            except subprocess.TimeoutExpired:
                pass   # said below: the commit is not here
        if subprocess.run([*git, "cat-file", "-e", f"{ref}^{{commit}}"], capture_output=True).returncode != 0:
            return None, f"cannot fetch {ref}"
    p = subprocess.run([*git, "show", f"{ref}:{path}"], capture_output=True)
    return (p.stdout if p.returncode == 0 else None), None


def commit_signers(commit, repo):
    """manifest/allowed_signers at <commit>: (bytes or None when that commit has none,
    problem or None)."""
    return git_show(commit, "manifest/allowed_signers", repo)


# The commit this change is measured against (--base): the last uploaded commit (the head of
# the last publish run that uploaded and read back: manifest/last_upload.py), or a PR's base.
# All trust state comes from git at this commit, never from the bucket.
BASE = ["HEAD"]


def resolve_base(ref):
    """<ref> as a commit id, fetching a 40-hex id from github.com/RELEASE_REPO when this
    checkout lacks it: (commit, None) or (None, problem). A base that does not resolve fails
    the check; it is never taken as nothing published."""
    git = ["git", "-C", os.path.dirname(HERE)]

    def rev():
        p = subprocess.run([*git, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                           capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None
    found = rev()
    if found is None and re.fullmatch(r"[0-9a-f]{40}", ref):
        try:
            subprocess.run([*git, "fetch", "--quiet", "--no-tags", "--depth=1",
                            f"https://github.com/{RELEASE_REPO}.git", ref], capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            pass
        found = rev()
    return (found, None) if found else (None, f"--base {ref} does not resolve to a commit here")


def default_base():
    """`git merge-base HEAD origin/main` when origin/main exists (a branch's base), else HEAD.
    CI passes the last uploaded commit (manifest/last_upload.py); so can a signer:
    --base "$(python3 manifest/last_upload.py commit)"."""
    git = ["git", "-C", os.path.dirname(HERE)]
    p = subprocess.run([*git, "merge-base", "HEAD", "origin/main"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else "HEAD"


def base_file(path):
    """<path> as the base commit has it (bytes), or None."""
    data, _ = git_show(BASE[0], path)
    return data


def base_manifest(channel):
    """The base commit's manifest/<channel>.json, parsed (a dict), or None."""
    data = base_file(f"manifest/{channel}.json")
    try:
        found = json.loads(data) if data else None
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def key_diff(old_text, new_text):
    """The key lines one signers file adds or drops against the other, as a short diff; ""
    when they name the same keys."""
    sig_check = _sig_check()
    old, new = sig_check.keys(old_text), sig_check.keys(new_text)   # strict: raises SignatureError
    if sorted(old) == sorted(new):
        return ""
    return "; ".join([f"- {ln}" for ln in old if ln not in new] + [f"+ {ln}" for ln in new if ln not in old])


def boxes_signers(channel):
    """boxes_signers_of(channel); when it gives no keys and the channel is not stable,
    stable's: the first beta is held to the keys boxes actually have, which are stable's."""
    found, why = boxes_signers_of(channel)
    if found or why or channel == "stable":
        return found, why
    print(f"note: holding {channel} to the keys of the stable release at --base instead")
    return boxes_signers_of("stable")


def boxes_signers_of(channel):
    """The keys the boxes on <channel> have: manifest/allowed_signers at the brain.commit that
    the base commit's manifest/<channel>.json names (what is published: --base is the last
    uploaded commit). Returns ((label, temp file) or None, problem or None); (None, None) when there are
    none (the base names no commit, or one with no signers file: a release from before
    signing). Not the newest brain-v* tag: a release's tag is pushed before its manifest PR,
    so that would be the new release's own keys."""
    published = base_manifest(channel)
    brain = (published or {}).get("brain") if isinstance((published or {}).get("brain"), dict) else {}
    commit, repo = brain.get("commit"), brain.get("repo")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        print(f"note: {BASE[0][:12]}'s {channel} manifest names no brain.commit: no earlier keys")
        return None, None
    text, why = commit_signers(commit, repo)
    if why:
        return None, f"{why}, the commit {BASE[0][:12]}'s {channel} manifest names"
    if text is None:
        print(f"note: {commit[:12]} ({BASE[0][:12]}'s {channel}) has no manifest/allowed_signers: no earlier keys")
        return None, None
    fd, path = tempfile.mkstemp(prefix="boxes-signers-")
    with os.fdopen(fd, "wb") as f:
        f.write(text)
    return (f"the published {channel} release ({commit[:12]})", path), None


def release_key_change(manifest_path, boxes):
    """What the release <manifest_path> names would change in the keys boxes trust: the key
    diff between manifest/allowed_signers at its brain.commit (what its package installs)
    and <boxes> ((label, path), the keys boxes have). ("", None) when nothing changes or
    there is no commit to read; ("", problem) when it cannot be read."""
    try:
        with open(manifest_path, "rb") as f:
            brain = json.load(f).get("brain") or {}
    except (OSError, ValueError, AttributeError):
        return "", None
    commit = brain.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return "", None
    text, why = commit_signers(commit, brain.get("repo"))
    if why:
        return "", f"{why}, the commit this manifest names"
    if text is None:
        return "", (f"{commit[:12]}, the commit this manifest names, has no manifest/allowed_signers: "
                    "its package would install no keys")
    with open(boxes[1], "rb") as f:
        old = f.read()
    try:
        return key_diff(old, text), None
    except _sig_check().SignatureError as e:
        return "", f"the signers file at {commit[:12]} or the boxes' is refused: {e}"


def signature(manifest_path, placeholders_ok, signers=None, also=(), key_change=""):
    """None when <manifest_path>.sig verifies against <signers> (default SIGNERS) and each
    of <also> ((label, path) pairs), or, under placeholders_ok, the manifest is unsigned and
    names nothing installable; else the problem. <key_change> (release_key_change) is the
    change in trusted keys the release brings: it passes only under the keys of <also>."""
    sig_check = _sig_check()
    with open(manifest_path, "rb") as f:
        body = f.read()
    sig = None
    if os.path.exists(manifest_path + ".sig"):
        with open(manifest_path + ".sig", "rb") as f:
            sig = f.read()
    try:
        if sig is None:
            manifest = sig_check.parse(body, sig_check.channel_named(manifest_path))
            if placeholders_ok and not installable(manifest):
                print(f"ok signature: none, and {manifest_path} names nothing installable "
                      "(boxes refuse it all the same)")
                return None
            return (f"signature: {manifest_path}.sig is missing — sign the manifest offline "
                    "(packaging/RELEASING.md) and commit the .sig beside it")
        _, said = sig_check.accept(body, sig, manifest_path, signers or SIGNERS)
        for label, path in also:
            try:
                sig_check.accept(body, sig, manifest_path, path)
            except sig_check.SignatureError as e:
                change = (f" This release changes the keys boxes trust ({key_change}): such a "
                          "change ships only in a release signed by a key they already trust."
                          if key_change else "")
                return (f"signature: verifies with this repo's keys but not with {label}'s, which "
                        f"boxes on that release trust ({e}): sign it with a key both name "
                        f"(packaging/RELEASING.md, rotation).{change}")
            said += f"; also trusted by {label}"
        if key_change:
            print(f"::warning file={manifest_path}::this release changes the keys boxes trust "
                  f"({key_change}); signed by a key they already trust, so it is a rotation the "
                  "signer approved")
    except (sig_check.SignatureError, ValueError) as e:
        return f"signature: {e}"
    print(f"ok signature ({said})")
    return None


# The one shape of the bootstrap's key block: a trusted_signers function that is only the heredoc.
TRUSTED_SIGNERS = re.compile(r"\ntrusted_signers\(\) \{\n  cat <<'SIGNERS'\n(.*?)\nSIGNERS\n\}\n", re.S)


def embedded_signers(bootstrap_text):
    """The signers file install-bootstrap.sh carries (its trusted_signers function, nothing
    but the heredoc), or None when there is not exactly one such function and one such
    heredoc: bash runs the last definition of a function, a reader might take another."""
    defs = re.findall(r"(?m)^\s*(?:function\s+trusted_signers\b|trusted_signers\s*\(\s*\))", bootstrap_text)
    heredocs = re.findall(r"<<-?\s*['\"]?SIGNERS\b", bootstrap_text)
    blocks = TRUSTED_SIGNERS.findall(bootstrap_text)
    if len(defs) != 1 or len(heredocs) != 1 or len(blocks) != 1:
        return None
    return blocks[0] + "\n"


def base_bootstrap_signers():
    """The keys the base commit's install-bootstrap.sh carries (the bootstrap published, which
    new installs and whoever checked it trust): (label, temp file), or None when it carries
    none (a bootstrap from before keys, or the placeholder), with a problem when its block
    is refused."""
    data = base_file("install-bootstrap.sh")
    block = embedded_signers(data.decode("utf-8", "replace")) if data else None
    try:
        trusted = _sig_check().keys(block) if block is not None else []
    except _sig_check().SignatureError as e:
        return None, f"the keys of {BASE[0][:12]}'s install-bootstrap.sh are refused: {e}"
    if not trusted:
        print(f"note: {BASE[0][:12]}'s install-bootstrap.sh carries no keys: nothing earlier to answer to")
        return None, None
    fd, path = tempfile.mkstemp(prefix="base-bootstrap-signers-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(block)
    return (f"the published bootstrap ({BASE[0][:12]}'s)", path), None


def bootstrap(path, against_base=False):
    """install-bootstrap.sh: it carries this repo's signers file unchanged (it verifies the
    manifest with it), and <path>.sig verifies in the teaport-bootstrap namespace against
    those keys (the ones the installation guide shows) and, <against_base>, against the keys
    the base commit's bootstrap carries: a key a push adds is trusted only once a key
    already trusted has signed the script that brings it. True when all holds."""
    sig_check = _sig_check()
    problems = []
    published, why = base_bootstrap_signers() if against_base else (None, None)
    if why:
        problems.append(why)
    with open(path, "rb") as f:
        body = f.read()
    with open(SIGNERS, encoding="utf-8") as f:
        signers_text = f.read()
    block = embedded_signers(body.decode("utf-8", "replace"))
    if block is None:
        problems.append("it has not exactly one trusted_signers function holding exactly one "
                        "SIGNERS heredoc (bash would run the last definition)")
    elif block != signers_text:
        problems.append("its trusted_signers block is not manifest/allowed_signers, line for line: "
                        "copy the file into it")
    if not os.path.exists(path + ".sig"):
        problems.append(f"{path}.sig is missing — sign it offline (packaging/RELEASING.md) and "
                        "commit the .sig beside it")
    else:
        with open(path + ".sig", "rb") as f:
            sig = f.read()
        try:
            who = sig_check.verify(body, sig, SIGNERS, namespace=sig_check.BOOTSTRAP_NAMESPACE)
            print(f"ok {path}: signed by {who}")
        except sig_check.SignatureError as e:
            problems.append(f"signature: {e}")
        if published:
            try:
                with open(published[1], "rb") as f:
                    change = key_diff(f.read(), signers_text)
            except sig_check.SignatureError as e:
                change = ""
                problems.append(f"signers: {e}")
            try:
                sig_check.verify(body, sig, published[1], namespace=sig_check.BOOTSTRAP_NAMESPACE)
                if change:
                    print(f"::warning file={path}::the bootstrap changes the keys it trusts ({change}); "
                          f"signed by a key {published[0]} already trusts")
            except sig_check.SignatureError as e:
                problems.append(f"signature: not by a key {published[0]} trusts ({e})"
                                + (f"; this bootstrap changes the keys it trusts ({change}), which "
                                   "only a key already trusted can sign for" if change else ""))
            finally:
                os.unlink(published[1])
    for p in problems:
        print(f"::error file={path}::{p}")
    return not problems


# Where the brain package carries the keys file in its data (packaging/nfpm.yaml's dst).
DEB_SIGNERS = "usr/lib/teaport/allowed_signers"


# The maintainer scripts dpkg runs: none of them may touch the keys file.
MAINTAINER_SCRIPTS = ("preinst", "postinst", "prerm", "postrm", "config", "triggers")

# The brain package as dpkg unpacks it (the signing helper's rules, which also hold every file
# outside the venv to the commit's): the venv nfpm.yaml installs from the build, its one link
# out, the control members and fields a release carries, and the tar entries dpkg unpacks
# as they are.
DEB_VENV = "opt/teaport/brain"
DEB_VENV_PYTHON = ("bin/python", "/usr/bin/python3.12")
DEB_CONTROL_MEMBERS = ("control", "md5sums", "conffiles", "preinst", "postinst", "prerm", "postrm")
DEB_FIELDS = ("Package", "Version", "Section", "Priority", "Architecture", "Maintainer", "Installed-Size",
              "Depends", "Homepage", "Description")
DEB_TYPES = (tarfile.REGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE)


def deb_entry_problem(info):
    """Why dpkg could unpack the tar entry <info> as something other than a plain root-owned
    entry (dpkg takes the owner by name first), or None."""
    if info.type not in DEB_TYPES or info.issparse():
        return "is not a regular file, a directory or a symbolic link (a hard link, a device, a fifo...)"
    if info.pax_headers:
        return "carries pax headers"
    if info.uid or info.gid or info.uname not in ("root", "") or info.gname not in ("root", ""):
        return f"is not root's (uid {info.uid} {info.uname!r}, gid {info.gid} {info.gname!r})"
    if info.mode & 0o7000:
        return "has a setuid, setgid or sticky bit"
    if info.mode & 0o022:
        return f"is group- or other-writable (mode {info.mode & 0o7777:o})"
    if info.isdir() and info.mode & 0o7777 != 0o755:
        return f"is a directory of mode {info.mode & 0o7777:o}, not 755"
    return None


def _deb_resolve(path, links, root):
    """Where <path> leads through the package's own symbolic links, or None when it leaves
    <root> (an absolute target is outside, the venv's python aside) or loops."""
    stack, res, hops = list(reversed(path.split("/"))), [], 0
    while stack:
        part = stack.pop()
        if part in ("", "."):
            continue
        if part == "..":
            if not res:
                return None
            res.pop()
        else:
            cand = "/".join(res + [part])
            target = links.get(cand)
            if target is None or (cand == f"{root}/{DEB_VENV_PYTHON[0]}" and target == DEB_VENV_PYTHON[1] and not stack):
                res.append(part)
            elif target.startswith("/"):
                return None
            else:
                hops += 1
                if hops > 40:
                    return None
                stack.extend(reversed(target.split("/")))
        cur = "/".join(res)
        if res and not (cur == root or cur.startswith(root + "/") or root.startswith(cur + "/")):
            return None
    out = "/".join(res)
    return out if out == root or out.startswith(root + "/") else None


def deb_layout(data, control):
    """Refuses (ValueError) a brain package dpkg could unpack into anything but plain root-owned
    entries: an entry deb_entry_problem names; a path twice; an entry under a symbolic link
    the package carries; outside the venv, a link, or a directory above no file; in the venv, a
    link that does not lead inside it (bin/python -> /usr/bin/python3.12 aside); a control
    member other than DEB_CONTROL_MEMBERS, or not a plain regular file; a control field other
    than DEB_FIELDS, or a Package other than teaport-brain."""
    for tar, what in ((data, "data"), (control, "control")):
        if tar.pax_headers:
            raise ValueError(f"its {what} tar carries pax headers")
    seen = set()
    for info in control:
        name = deb_path(info.name)
        problem = deb_entry_problem(info)
        if problem:
            raise ValueError(f"its control member {name or '.'!r} {problem}")
        if not name:
            continue
        if name in seen or name not in DEB_CONTROL_MEMBERS or not info.isfile():
            raise ValueError(f"its control tar carries {name!r} (twice, not a regular file, or not a member a "
                             "release has)")
        seen.add(name)
        if name == "control":
            fields = control.extractfile(info).read().decode("utf-8", "replace")
            keys = [ln.split(":", 1)[0] for ln in fields.split("\n") if ln and ln[0] not in " \t"]
            if len(set(keys)) != len(keys) or set(keys) - set(DEB_FIELDS):
                raise ValueError(f"its control file has the fields {keys}: only {', '.join(DEB_FIELDS)}, once each")
            if not re.search(r"^Package: teaport-brain$", fields, re.M):
                raise ValueError("its control file's Package is not teaport-brain")
    if "control" not in seen:
        raise ValueError("its control tar has no control file")
    entries, links = {}, {}
    for info in data:
        path = deb_path(info.name)
        problem = deb_entry_problem(info)
        if problem:
            raise ValueError(f"/{path} {problem} ({info.name!r})")
        if path in entries:
            raise ValueError(f"/{path} is in the package twice: dpkg would install the last")
        entries[path] = info
        in_venv = path == DEB_VENV or path.startswith(DEB_VENV + "/")
        if info.issym():
            if not in_venv:
                raise ValueError(f"/{path} is a symbolic link outside /{DEB_VENV}")
            links[path] = info.linkname
    parents = {""} | {"/".join(p.split("/")[:i]) for p in list(entries) if not entries[p].isdir()
                      or p == DEB_VENV for i in range(1, p.count("/") + 1)}
    for path, info in entries.items():
        parts = path.split("/")
        above = next(("/".join(parts[:i]) for i in range(1, len(parts)) if "/".join(parts[:i]) in links), None)
        if above:
            raise ValueError(f"/{path} is under /{above}, a symbolic link in the package")
        in_venv = path == DEB_VENV or path.startswith(DEB_VENV + "/")
        if info.isdir() and not in_venv and path not in parents:
            raise ValueError(f"/{path} is a directory above no file of the package: dpkg would make or change it")
    for path, target in links.items():
        if target.startswith("/"):
            if (path, target) != (f"{DEB_VENV}/{DEB_VENV_PYTHON[0]}", DEB_VENV_PYTHON[1]):
                raise ValueError(f"/{path} links to {target!r}, outside /{DEB_VENV}")
            continue
        to = _deb_resolve(path.rsplit("/", 1)[0] + "/" + target, links, DEB_VENV)
        if to is None or to not in entries:
            raise ValueError(f"/{path} links to {target!r}, which leads to no entry inside /{DEB_VENV}")


def deb_path(name):
    """The path dpkg installs a tar entry <name> at: no leading "/" or "./", "//" and "/./"
    collapsed. Raises ValueError for a ".." part."""
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"an entry with '..' in its path ({name!r})")
    return "/".join(parts)


# --- strict .deb reader: one copy in teaport's manifest/check.py, one in the signing helper ---
# The two copies are identical, line for line (the helper's test_against_teaport.py checks).
# The copies: teaport's manifest/check.py and teaport-factory's
# signing/teaport-release-signing/sign_manifest.py. Teaport's CI sees only the first: change
# both, then run test_against_teaport.py in teaport-factory, which enforces they are equal.
# It reads a .deb exactly one way, the one dpkg 1.21 reads the packages teaport builds: no
# reader of its own guessing (no tarfile "r:*", no format sniffing), so no package can show one
# tar to this check and another to dpkg. Anything outside that one dialect is refused.
#   - ar: "!<arch>\n", then exactly debian-binary ("2.0\n"), one control.tar*, one data.tar*,
#     strict headers, nothing after the last member.
#   - each tar's compression is the one its member name says, as dpkg picks it; a stream that
#     does not end where the compressed data ends is refused.
#   - each tar is walked header by header: GNU magic ("ustar  \0"), a verified checksum, every
#     number strict octal and NUL-terminated as GNU tar and nfpm write it, types 0 (a regular
#     file), 5 (a directory), 2 (a symbolic link) and L (a GNU long name, for the next entry
#     only); size 0 on all but files, a link name on symlinks only, devmajor/devminor empty or
#     0, nothing in the prefix area; names printable ASCII; after the end-of-archive block,
#     only NUL to the end of the stream.
#   - then dpkg-deb itself: its --ctrl-tarfile and --fsys-tarfile streams must be these bytes,
#     and its listing (dpkg-deb -c, and tar -tv of the control tar, which is what dpkg runs
#     for each) must be these entries.
# Errors are ValueError.
DEB_TOOLS = ("/usr/bin/dpkg-deb", "/usr/bin/tar")
DEB_CONTROL_NAMES = {"control.tar": "", "control.tar.gz": "gz", "control.tar.xz": "xz", "control.tar.zst": "zst"}
DEB_DATA_NAMES = {"data.tar": "", "data.tar.gz": "gz", "data.tar.xz": "xz", "data.tar.bz2": "bz2",
                  "data.tar.lzma": "lzma", "data.tar.zst": "zst"}
DEB_MAX_TAR = 8 << 30
DEB_GNU_MAGIC = b"ustar  \0"
DEB_NAME_BYTES = re.compile(rb"[ -\[\]-~]+")       # printable ASCII, no backslash


class DebEntry:
    """One tar entry as dpkg takes it (the long name applied), with tarfile's names for it."""
    pax_headers: dict = {}

    def __init__(self, name, type_, mode, uid, gid, size, linkname, uname, gname, offset):
        self.name, self.type, self.mode, self.uid, self.gid = name, type_, mode, uid, gid
        self.size, self.linkname, self.uname, self.gname, self.offset = size, linkname, uname, gname, offset

    def isfile(self):
        return self.type == b"0"

    def isdir(self):
        return self.type == b"5"

    def issym(self):
        return self.type == b"2"

    def issparse(self):
        return False


class DebTar:
    """The entries of one tar stream, and each file's bytes (extractfile, as tarfile has it)."""
    pax_headers: dict = {}

    def __init__(self, stream, entries):
        self.stream, self.entries = stream, entries

    def __iter__(self):
        return iter(self.entries)

    def extractfile(self, entry):
        return io.BytesIO(bytes(self.stream[entry.offset:entry.offset + entry.size]))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def deb_ar(raw):
    """[(member name, body)] of the ar archive <raw>: exactly debian-binary, control.tar*, data.tar*."""
    if raw[:8] != b"!<arch>\n":
        raise ValueError("not a Debian package (no ar header)")
    pos, members = 8, []
    while pos < len(raw):
        h = raw[pos:pos + 60]
        if len(h) < 60 or not re.fullmatch(rb"([A-Za-z0-9_.-]+)/? *([0-9]+) +([0-9]+) +([0-9]+) +([0-7]+) +([0-9]+) *`\n", h):
            raise ValueError("a damaged or unusual ar member header")
        name = h[:16].decode("ascii").strip().rstrip("/")
        size = int(h[48:58])
        body = raw[pos + 60:pos + 60 + size]
        if len(body) != size:
            raise ValueError(f"the ar member {name} is cut short")
        pos += 60 + size
        if size % 2:
            if raw[pos:pos + 1] != b"\n":
                raise ValueError(f"the ar member {name} is not padded as ar pads")
            pos += 1
        members.append((name, body))
    names = [n for n, _ in members]
    if len(members) != 3 or names[0] != "debian-binary" or names[1] not in DEB_CONTROL_NAMES \
            or names[2] not in DEB_DATA_NAMES:
        raise ValueError(f"its ar members are {', '.join(names)[:200]}, not debian-binary, control.tar*, data.tar*")
    if members[0][1] != b"2.0\n":
        raise ValueError("its debian-binary is not 2.0")
    return members


def deb_decompress(name, body, kind):
    """The tar stream of the member <name>, decompressed as its name says (zst: None, which
    only dpkg-deb reads here). A stream with bytes after its end, or cut short, is refused."""
    import bz2
    import lzma
    import zlib
    if kind == "":
        return body
    if kind == "zst":
        return None
    d = {"gz": lambda: zlib.decompressobj(31), "xz": lambda: lzma.LZMADecompressor(lzma.FORMAT_XZ),
         "lzma": lambda: lzma.LZMADecompressor(lzma.FORMAT_ALONE), "bz2": bz2.BZ2Decompressor}[kind]()
    try:
        out = d.decompress(body, DEB_MAX_TAR + 1)
    except (zlib.error, lzma.LZMAError, OSError, EOFError) as e:
        raise ValueError(f"its {name} does not decompress ({type(e).__name__})") from None
    if len(out) > DEB_MAX_TAR:
        raise ValueError(f"its {name} is over {DEB_MAX_TAR} bytes")
    if not d.eof or d.unused_data or (kind == "gz" and d.unconsumed_tail):
        raise ValueError(f"its {name} is not one {kind} stream that ends with the member")
    return out


def _deb_octal(field, what):
    """A numeric field as GNU tar and nfpm write it: octal digits filling it but the last byte,
    then NUL. Anything else (spaces, base-256, no terminator) is refused."""
    if not re.fullmatch(rb"[0-7]{%d}\0" % (len(field) - 1), field):
        raise ValueError(f"a tar header's {what} field is not strict octal ({bytes(field)!r})")
    return int(field[:-1], 8)


def _deb_string(field, what):
    """A NUL-padded string field: its bytes up to the first NUL, every byte after it NUL."""
    field = bytes(field)
    text = field.split(b"\0", 1)[0]
    if field[len(text):].strip(b"\0"):
        raise ValueError(f"a tar header's {what} field has bytes after its end")
    return text


def deb_walk(stream, what):
    """The entries of the tar <stream>, read in the one dialect above."""
    view, entries, off, longname = memoryview(stream), [], 0, None
    while True:
        h = view[off:off + 512]
        if len(h) < 512:
            raise ValueError(f"its {what} tar ends without an end-of-archive block")
        if not any(h):
            if longname is not None:
                raise ValueError(f"its {what} tar ends after a long name with no entry")
            if any(view[off:]):
                raise ValueError(f"its {what} tar has bytes after its end-of-archive block")
            return entries
        h = bytes(h)
        if h[257:265] != DEB_GNU_MAGIC:
            raise ValueError(f"its {what} tar has a header that is not GNU tar's ({h[257:265]!r})")
        if re.fullmatch(rb"[0-7]{6}\0 ", h[148:156]) is None or \
                int(h[148:154], 8) != sum(h[:148]) + 256 + sum(h[156:]):
            raise ValueError(f"its {what} tar has a header whose checksum is wrong or unusual")
        type_ = h[156:157]
        mode, uid, gid = _deb_octal(h[100:108], "mode"), _deb_octal(h[108:116], "uid"), _deb_octal(h[116:124], "gid")
        size, _mtime = _deb_octal(h[124:136], "size"), _deb_octal(h[136:148], "mtime")
        for a, z, field in ((329, 337, "devmajor"), (337, 345, "devminor")):
            if any(h[a:z]) and _deb_octal(h[a:z], field) != 0:
                raise ValueError(f"its {what} tar has an entry with a {field}")
        if any(h[345:512]):
            raise ValueError(f"its {what} tar has a header with bytes in its prefix or GNU extension area")
        name = _deb_string(h[:100], "name")
        linkname = _deb_string(h[157:257], "linkname")
        uname, gname = _deb_string(h[265:297], "uname"), _deb_string(h[297:329], "gname")
        data_at = off + 512
        off = data_at + (size + 511) // 512 * 512
        if off > len(view):
            raise ValueError(f"its {what} tar ends inside an entry's data")
        if type_ == b"L":
            body = bytes(view[data_at:data_at + size])
            if longname is not None or name != b"././@LongLink" or linkname or uname or gname or mode or uid \
                    or gid or not 2 <= size <= 4097 or body[-1:] != b"\0" or b"\0" in body[:-1]:
                raise ValueError(f"its {what} tar has a GNU long name record that is not the plain kind")
            longname = body[:-1]
            continue
        if type_ not in (b"0", b"5", b"2"):
            raise ValueError(f"its {what} tar has an entry of type {type_!r}: only a file, a directory "
                             "or a symbolic link (a hard link, a device, a fifo, a pax or GNU record)")
        if type_ != b"0" and size:
            raise ValueError(f"its {what} tar has a directory or a link with data ({size} bytes)")
        if (type_ == b"2") != bool(linkname):
            raise ValueError(f"its {what} tar has a link name on an entry that is not a symbolic link, or a "
                             "symbolic link with none")
        if longname is not None:
            name, longname = longname, None
        for text, field in ((name, "name"), (linkname, "link name"), (uname, "uname"), (gname, "gname")):
            if text and not DEB_NAME_BYTES.fullmatch(text):
                raise ValueError(f"its {what} tar has a {field} that is not printable ASCII ({text[:80]!r})")
        if not name or b" -> " in name or name.endswith(b" "):
            raise ValueError(f"its {what} tar has an entry with an empty or unusual name ({name[:80]!r})")
        entries.append(DebEntry(name.decode("ascii"), type_, mode, uid, gid, size, linkname.decode("ascii"),
                                uname.decode("ascii"), gname.decode("ascii"), data_at))


def _deb_listing(lines, entries, what):
    """dpkg-deb -c / tar -tv lines (LC_ALL=C) must name exactly <entries>, in order: the same
    type and mode, owner, size, name and link target."""
    import stat as _stat
    got = [ln for ln in lines.decode("ascii", "replace").split("\n") if ln]
    if len(got) != len(entries):
        raise ValueError(f"dpkg reads {len(got)} entries in its {what} tar, this reads {len(entries)}")
    kinds = {b"0": _stat.S_IFREG, b"5": _stat.S_IFDIR, b"2": _stat.S_IFLNK}
    for line, e in zip(got, entries):
        parts = line.split(None, 5)
        want_name = e.name + (f" -> {e.linkname}" if e.issym() else "")
        if len(parts) != 6 or parts[0] != _stat.filemode(kinds[e.type] | e.mode) \
                or parts[1] != f"{e.uname or e.uid}/{e.gname or e.gid}" or parts[2] != str(e.size) \
                or parts[5] != want_name:
            raise ValueError(f"dpkg reads its {what} tar otherwise: {line[:200]!r} for {want_name[:200]!r}")


def _deb_tool(args, stdin=None):
    """The stdout of <args>, run with a plain environment."""
    import subprocess
    env = {"LC_ALL": "C", "TZ": "UTC", "PATH": "/usr/bin:/bin"}
    p = subprocess.run(args, input=stdin, capture_output=True, env=env)
    if p.returncode != 0:
        raise ValueError(f"{os.path.basename(args[0])} cannot read the package "
                         f"({p.stderr.decode('ascii', 'replace').strip()[:200]})")
    return p.stdout


def deb_read(path):
    """(control DebTar, data DebTar) of the .deb at <path>, read as above."""
    for tool in DEB_TOOLS:
        if not os.access(tool, os.X_OK):
            raise ValueError(f"{tool} is needed to read a package (the check holds its reading to dpkg's)")
    with open(path, "rb") as f:
        raw = f.read()
    members = deb_ar(raw)
    tars = []
    for (name, body), kinds, flag in ((members[1], DEB_CONTROL_NAMES, "--ctrl-tarfile"),
                                      (members[2], DEB_DATA_NAMES, "--fsys-tarfile")):
        stream = deb_decompress(name, body, kinds[name])
        theirs = _deb_tool([DEB_TOOLS[0], flag, path])
        if stream is None:
            stream = theirs
        elif theirs != stream:
            raise ValueError(f"dpkg-deb decompresses its {name} to other bytes than its name says")
        tars.append(DebTar(stream, deb_walk(stream, name.split(".")[0])))
    _deb_listing(_deb_tool([DEB_TOOLS[1], "-tvf", "-"], stdin=bytes(tars[0].stream)), tars[0].entries, "control")
    _deb_listing(_deb_tool([DEB_TOOLS[0], "-c", path]), tars[1].entries, "data")
    return tars[0], tars[1]
# --- end of the strict .deb reader ---
# (Identical in teaport-factory's sign_manifest.py; test_against_teaport.py there enforces it.)


_DEB_READ = {}


def deb_tars(deb):
    """(data tar, control tar) of the .deb <deb>, through the strict reader above (read once
    per content: the keys check and the version check read the same package)."""
    digest = hashlib.sha256()
    with open(deb, "rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    key = (os.path.realpath(deb), digest.hexdigest())
    if key not in _DEB_READ:
        _DEB_READ.clear()
        _DEB_READ[key] = deb_read(deb)
    control, data = _DEB_READ[key]
    return data, control


def deb_version_problems(deb, manifest):
    """The package's control file Version against the version its file name says
    (teaport-brain_<version>_<arch>.deb) and the one its release tag builds
    (manifest_sig.release_version: X.Y.Z, or X.Y.Z~rcN for brain-vX.Y.Z-rcN), each way."""
    sig = _sig_check()
    brain = manifest.get("brain") if isinstance(manifest.get("brain"), dict) else {}
    deb_entry = brain.get("deb") if isinstance(brain.get("deb"), dict) else {}
    name = str(deb_entry.get("url", "")).rsplit("/", 1)[-1]
    named = sig.DEB_FILE.fullmatch(name)
    tagged = sig.release_version(brain.get("tag"))
    try:
        _data, control = deb_tars(deb)
    except (OSError, ValueError) as e:
        return [f"brain.deb: its control file cannot be read ({e})"]
    text = next((control.extractfile(e).read().decode("utf-8", "replace") for e in control
                 if deb_path(e.name) == "control"), "")
    version = next((ln.split(":", 1)[1].strip() for ln in text.split("\n") if ln.startswith("Version:")), None)
    problems = []
    if not named:
        problems.append(f"brain.deb: its file name {name!r} is not teaport-brain_<version>_<arch>.deb")
    elif version != named.group(1):
        problems.append(f"brain.deb: its control file's Version {version!r} is not its file name's {named.group(1)!r}")
    if tagged is not None and version != tagged:
        problems.append(f"brain.deb: its control file's Version {version!r} is not {tagged!r}, the version "
                        f"{brain.get('tag')} builds")
    return problems


def deb_signers(deb, member=DEB_SIGNERS):
    """The bytes of <member> in the package's data, as dpkg would install them, or None when
    it carries none. Every entry of the data tar is read, with its name as dpkg takes it
    (deb_path): the package is refused (ValueError) when more than one entry lands on
    <member>, when it is not a regular file, when it or a directory above it is a symlink or
    a hard link (or anything but a directory), when it is over manifest_sig's size limit,
    and when a maintainer script mentions allowed_signers (a script could write the file
    after dpkg unpacked it)."""
    sig_check = _sig_check()
    target = deb_path(member)
    parents = {"/".join(target.split("/")[:n]) for n in range(1, target.count("/") + 1)}
    data, control = deb_tars(deb)
    with data, control:
        deb_layout(data, control)
        entries = []
        for info in data:
            path = deb_path(info.name)
            if path == target:
                if not info.isfile():
                    raise ValueError(f"/{target} is not a regular file in the package ({info.name!r})")
                entries.append(info)
            elif path in parents and not info.isdir():
                raise ValueError(f"/{path}, above /{target}, is not a directory in the package "
                                 f"({info.name!r}: a link or a file)")
        if len(entries) > 1:
            raise ValueError(f"the package carries /{target} {len(entries)} times "
                             f"({', '.join(repr(e.name) for e in entries)}): dpkg would install the last")
        for info in control:
            if deb_path(info.name) in MAINTAINER_SCRIPTS and info.isfile():
                if b"allowed_signers" in control.extractfile(info).read():
                    raise ValueError(f"its maintainer script {deb_path(info.name)} mentions allowed_signers")
        if not entries:
            return None
        if entries[0].size > sig_check.MAX_SIGNERS_BYTES:
            raise ValueError(f"/{target} is over {sig_check.MAX_SIGNERS_BYTES} bytes")
        return data.extractfile(entries[0]).read()


def package_keys(deb, boxes, signature_problem, commit_keys=None):
    """What the brain package <deb> carries as /usr/lib/teaport/allowed_signers: the file its
    data holds, once, with no maintainer script touching it (deb_signers), read strictly,
    against <commit_keys> ((label, bytes): manifest/allowed_signers at the manifest's
    brain.commit, the file the signer reads) and <boxes> ((label, path), the keys boxes
    have, or None). Returns (problems, warnings): a package refused, or a file that is
    missing, refused, keyless or different from <commit_keys>, is a problem; a change
    against <boxes> is a warning when the manifest's signature verified under them, and a
    problem naming the change when it did not (<signature_problem>)."""
    sig_check = _sig_check()
    try:
        data = deb_signers(deb)
    except (OSError, ValueError, tarfile.TarError) as e:
        return [f"brain.deb: its /{DEB_SIGNERS} cannot be told ({e})"], []
    before_signing = commit_keys is not None and commit_keys[1] is None
    if data is None:
        if before_signing:
            return [f"brain.deb carries no /{DEB_SIGNERS} and neither does {commit_keys[0]}: a "
                    "release without keys would leave boxes verifying nothing"], []
        return [f"brain.deb installs no /{DEB_SIGNERS}: boxes on it would verify nothing"], []
    if before_signing:
        return [f"brain.deb carries /{DEB_SIGNERS} but {commit_keys[0]} has none: the package is "
                "not the commit the manifest names"], []
    try:
        if not sig_check.keys(data):
            return [f"brain.deb's /{DEB_SIGNERS} names no key: boxes on it would verify nothing"], []
        if commit_keys is not None:
            differs = key_diff(commit_keys[1], data)
            if differs:
                return [f"brain.deb carries other keys than {commit_keys[0]} ({differs}): the package "
                        "is not the commit the manifest names"], []
        if boxes is None:
            return [], []
        with open(boxes[1], "rb") as f:
            change = key_diff(f.read(), data)
    except sig_check.SignatureError as e:
        return [f"brain.deb's /{DEB_SIGNERS} is refused: {e}"], []
    if not change:
        print(f"ok brain.deb's /{DEB_SIGNERS}: the keys {boxes[0]} has")
        return [], []
    if signature_problem:
        return [f"brain.deb carries other keys than {boxes[0]} trusts ({change}), and the manifest "
                "is not signed by a key they trust: a key change ships only in a release signed by "
                "one"], []
    return [], [f"brain.deb changes the keys boxes trust ({change}); signed by a key {boxes[0]} trusts"]


def _commit_keys(manifest):
    """(label, bytes, commit) of manifest/allowed_signers at the manifest's brain.commit;
    (label, None, commit) when that commit has none (a release without keys: refused); None
    when the manifest names no full commit (a problem said elsewhere). A commit that cannot
    be had gives (label, b"", commit), so the package's keys differ from it."""
    brain = manifest.get("brain") if isinstance(manifest, dict) else None
    commit = (brain or {}).get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    text, why = commit_signers(commit, (brain or {}).get("repo"))
    if why:   # cannot be had: nothing matches it
        return f"manifest/allowed_signers at {commit[:12]} ({why})", b"", commit
    return f"manifest/allowed_signers at {commit[:12]}", text, commit   # None: that commit has none


def reads_access(repo, tag, commit):
    """None when install.sh at <commit> (brain.commit: resolves() has held <tag> to it) says
    ACCESS_PROTOCOL=1, or when the manifest names no full commit (a problem said elsewhere);
    else why not. `teaport update` (packaging/deb/update_apply.py) runs that commit's
    installer as root: it fetches refs/tags/<tag> and checks the clone is brain.commit. So
    the commit is read, never whatever `git clone --branch <tag>` would take (a branch of the
    same name wins there)."""
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    text, why = git_show(commit, "install.sh", repo)
    if why:
        return f"cannot read install.sh at {commit[:12]} ({why})"
    if text is None or ACCESS_PROTOCOL.encode() not in text:
        return (f"{tag}'s install.sh (at {commit[:12]}) predates the license gate (no ACCESS_PROTOCOL=1), "
                "and this manifest has access: `teaport update` would run it, and every gated download "
                "would fail. Name a release tag cut after the gate merged (packaging/RELEASING.md)")
    return None


def check(manifest_path, placeholders_ok, signature_only=False, also=(), key_change="",
          published_commit=None, require_enforce=False):
    """<also>: the keys already trusted that the signature must verify under too (the boxes',
    else the published bootstrap's, as --base has them); <published_commit>: the brain.commit
    --base's manifest/<channel>.json names, which a re-signed manifest may name with other
    keys than the tree's."""
    with open(manifest_path) as f:
        manifest = json.load(f)
    problems, checked = [], 0
    prefixes, problems = gate(manifest)
    token = os.environ.get("DL_CHECK_TOKEN", "")
    why = signature(manifest_path, placeholders_ok, also=also, key_change=key_change)
    if why:
        problems.append(why)
    # A manifest that names a release (a brain.commit) names a release tag, a candidate's
    # (brain-vX.Y.Z-rcN) only on beta, and the .deb that tag builds (manifest_sig's rule, the
    # one every box applies to a signed manifest).
    brain = manifest.get("brain") if isinstance(manifest, dict) and isinstance(manifest.get("brain"), dict) else {}
    if re.fullmatch(r"[0-9a-f]{40}", str(brain.get("commit", ""))):
        if _sig_check().release_version(brain.get("tag")) is None:
            problems.append(f"brain.tag {brain.get('tag')!r} is not a release tag (brain-vX.Y.Z, or "
                            "brain-vX.Y.Z-rcN on the beta channel)")
        rule = _sig_check().release_problem(manifest, _sig_check().channel_named(manifest_path))
        if rule:
            problems.append(rule)
    # A release whose commit carries no keys: refused (boxes on it would verify nothing). Read
    # here too, so the signature-only run before upload refuses one as the full check does.
    commit_keys = _commit_keys(manifest) if not why else None
    if commit_keys and commit_keys[1] is None:
        problems.append(f"{commit_keys[0]} does not exist: the release carries no keys, and boxes on "
                        "it would verify nothing")
    # Every release carries exactly the keys of the tree that releases it: a key swapped at a
    # tagged commit fails, whoever signed the manifest. Not for a re-signature: a manifest
    # byte for byte the base commit's, naming the release it names (<published_commit>), as
    # a rotation PR re-signs the published manifests while the tree already has the new
    # keys. The full check holds the package's file to the commit's (package_keys).
    resigned = bool(published_commit and commit_keys and commit_keys[2] == published_commit
                    and unchanged_from_base(manifest_path))
    if commit_keys and commit_keys[1] is not None and not resigned:
        with open(SIGNERS, "rb") as f:
            tree = f.read()
        try:
            differs = key_diff(tree, commit_keys[1])
        except _sig_check().SignatureError as e:
            differs = f"refused: {e}"
        if differs:
            problems.append(f"{commit_keys[0]} is not this tree's manifest/allowed_signers ({differs}): "
                            "a release carries exactly the keys of the tree that releases it")
    if signature_only:
        for p in problems:
            print(f"::error file={manifest_path}::{p}")
        return not problems
    work_dir = tempfile.TemporaryDirectory(prefix="manifest-check-", ignore_cleanup_errors=True)
    work = work_dir.name

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
        elif node.get("repo") != RELEASE_REPO and "TODO" not in str(node.get("repo")):
            problems.append(f"{where}.repo: {node.get('repo')!r}, not {RELEASE_REPO}: the only "
                            "repository whose installer teaport update, the bootstrap and install.sh take")
        elif "TODO" not in str(node.get("commit")) \
                and not re.fullmatch(r"[0-9a-f]{40}", str(node.get("commit", ""))):
            problems.append(f"{where}.commit: missing or not a full commit id "
                            f"({node.get('commit')!r}); teaport update runs that commit's "
                            "installer as root, and only the commit the signature covers")

    for where, url, sha in entries(manifest):
        if url is None:
            problems.append(f"{where}: {sha}")
            continue
        if "TODO" in url or sha == "TODO":
            placeholder(where, url)
            continue
        if not sha:
            problems.append(f"{where}: no sha256 ({url}); install.sh refuses an artifact it "
                            "cannot check")
            continue
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            problems.append(f"{where}: sha256 is not 64 lowercase hex characters ({sha!r})")
            continue
        if urlparse(url).hostname != HOST or urlparse(url).scheme != "https":
            problems.append(f"{where}: not served from https://{HOST}/ ({url})")
            continue
        prefix = next((p for p in prefixes if url.startswith(p)), None)
        gated, around = prefix is not None, []
        if gated:
            # Around the gate: the same file, asked for at paths the Worker is not routed on,
            # on every run (a copy left in the public bucket is served there).
            for u in encoded_forms(url, prefix):
                if (how := bypassed(u)):
                    around.append(f"{where}: {u} -> {how}")
            problems.extend(around)
            # Asked without a token on every run, token or not: a live gate that is open
            # (deployed from elsewhere than main, or opened as the way back) shows up here,
            # on pushes and the weekly run as well as on PRs.
            refusal = refused(url)
            if refusal == GATE_OPEN and require_enforce:
                problems.append(f"{where}: {url} is served without a download token: the gate's GATE "
                                "is open. main's worker/wrangler.toml enforces: deploy main's Worker "
                                "(Actions → worker-deploy → Run workflow, from main), unless it was "
                                "opened as the way back (packaging/RELEASING.md)")
            elif refusal == GATE_OPEN:
                print(f"::warning::{where}: {url} is served without a download token: the gate's "
                      "GATE is open. main's worker/wrangler.toml enforces: deploy main's Worker "
                      "(Actions → worker-deploy → Run workflow, from main), unless it was opened "
                      "as the way back (packaging/RELEASING.md); the weekly run fails until then")
            elif refusal:
                problems.append(f"{where}: {url} -> {refusal}")
                continue
            if not token:
                checked += 1
                if not refusal and not around:
                    print(f"ok {where} (gated: refused without a token; its contents are checked "
                          "with DL_CHECK_TOKEN)")
                continue
        # The brain package is kept, to read the keys file its data carries (package_keys).
        dest = os.path.join(work, "brain.deb") if where == "brain.deb" else None
        try:
            digest = (fetch(url, hashlib.sha256(), token if gated else None, dest=dest) if dest
                      else fetch(url, hashlib.sha256(), token if gated else None))
        except Exception as e:  # HTTPError, URLError, timeouts: all mean "not published"
            problems.append(f"{where}: {url} -> {e}")
            continue
        checked += 1
        if digest.hexdigest() != sha:
            problems.append(f"{where}: {url} serves sha256 {digest.hexdigest()}, "
                            f"the manifest says {sha}")
        elif not around:
            print(f"ok {where}")
            if dest:
                found, warned = package_keys(dest, also[0] if also else None, why,
                                             commit_keys or _commit_keys(manifest))
                problems += found + deb_version_problems(dest, manifest)
                for w in warned:
                    print(f"::warning file={manifest_path}::{w}")

    for where, repo, tag, commit in repos(manifest):
        if "TODO" in f"{repo}{tag}{commit or ''}":
            placeholder(where, f"{repo}@{tag} ({commit})")
            continue
        if where in REQUIRED_REPOS and not re.fullmatch(r"[0-9a-f]{40}", str(commit or "")):
            continue   # said above: no commit to hold the tag to
        why = resolves(repo, tag, commit)
        if not why and where == "brain" and "access" in manifest:
            why = reads_access(repo, tag, commit)
        if why:
            problems.append(f"{where}: {why}")
        else:
            checked += 1
            print(f"ok {where} (github.com/{repo} @ {tag}" + (f" = {commit})" if commit else ")"))

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
    work_dir.cleanup()
    return not problems


def published_commit_of(channel):
    """The brain.commit the base commit's manifest/<channel>.json names, or None."""
    commit = ((base_manifest(channel) or {}).get("brain") or {}).get("commit")
    return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None


def unchanged_from_base(manifest_path):
    """Whether <manifest_path> is byte for byte the base commit's manifest/<name> (a
    re-signature changes only the .sig)."""
    with open(manifest_path, "rb") as f:
        body = f.read()
    return base_file(f"manifest/{os.path.basename(manifest_path)}") == body


def parser():
    ap = argparse.ArgumentParser(
        description="Check that a release manifest names everything install.sh needs, "
                    "published as named.")
    ap.add_argument("manifests", nargs="*")
    ap.add_argument("--bootstrap", metavar="FILE",
                    help="also check the one-line installer (install-bootstrap.sh): its keys "
                         "are this repo's, and FILE.sig verifies (namespace teaport-bootstrap)")
    ap.add_argument("--placeholders-ok", action="store_true",
                    help='allow "TODO" entries (a channel with nothing released yet)')
    ap.add_argument("--require-enforce", action="store_true",
                    help="an open license gate (GATE = \"open\") is an error, not a warning: the "
                         "weekly run, so a gate left open goes red within a week")
    ap.add_argument("--signature-only", action="store_true",
                    help="check only each manifest's signature (no downloads)")
    ap.add_argument("--boxes-signers", action="store_true",
                    help="hold each signature to the keys already trusted, as --base has them: a "
                         "manifest's to those at the brain.commit of --base's manifest/<channel>.json "
                         "(else to --base's install-bootstrap.sh keys); the bootstrap's to --base's "
                         "install-bootstrap.sh keys")
    ap.add_argument("--base", metavar="REF", default=None,
                    help="the commit this change is measured against, which holds what is "
                         "published (the last uploaded commit, from manifest/last_upload.py; a "
                         "PR's base): the boxes' keys, the published bootstrap's, and the "
                         "manifest a re-signature must equal. Default: git merge-base HEAD "
                         "origin/main, else HEAD. It must resolve.")
    return ap


def main():
    ap = parser()
    args = ap.parse_args()
    if not args.manifests and not args.bootstrap:
        ap.error("name a manifest, or --bootstrap")
    if args.base is not None and not args.base.strip():
        # An empty --base is a lookup that failed upstream (base="$(...)"), not "the default".
        print("::error::--base is empty: name the commit this change is measured against")
        sys.exit(1)
    base, why = resolve_base(args.base or default_base())
    if why:
        print(f"::error::{why}: the check needs the commit it is measured against")
        sys.exit(1)
    BASE[0] = base
    print(f"base: {base}")
    results = []
    # The repository's signers file, read as every box reads it: refused, it fails it all.
    try:
        with open(SIGNERS, "rb") as f:
            _sig_check().keys(f.read())
    except (OSError, _sig_check().SignatureError) as e:
        print(f"::error file={SIGNERS}::{e}")
        sys.exit(1)
    for m in args.manifests:
        also, why = (), None
        channel = _sig_check().channel_named(m)
        change, published_commit = "", None
        if args.boxes_signers and channel:
            found, why = boxes_signers(channel)
            if not found and not why:
                # No published release's keys (the turn-on: stable names none): the keys new
                # installs trust, those of the published bootstrap, stand in for them.
                found, why = base_bootstrap_signers()
            also = (found,) if found else ()
            published_commit = published_commit_of(channel)
            if found and not why:
                change, why = release_key_change(m, found)
        try:
            if why:
                print(f"::error file={m}::signature: {why}")
                results.append(False)
            else:
                results.append(check(m, args.placeholders_ok, args.signature_only, also, change,
                                     published_commit, args.require_enforce))
        finally:
            for _, path in also:
                os.unlink(path)
    if args.bootstrap:
        results.append(bootstrap(args.bootstrap, args.boxes_signers))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
