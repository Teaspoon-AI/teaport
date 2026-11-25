#!/usr/bin/env python3
#
# teaport — which commit did the publish workflow last upload, and which commit does this run
# measure itself against (check.py --base)?
#
# The bucket is not asked: GitHub's own record is. The last upload is the head commit of the
# most recent run of publish.yml on main whose publish job's upload step and read-back step
# both concluded success. A run that skipped the upload (at the tip check, or at the
# credentials check before it) or failed before it uploaded nothing; one whose upload step
# ran (failed, cancelled, or succeeded without the read-back succeeding) may have uploaded
# part of its files, and stops the lookup (below). That needs the Actions API with read
# access only (GITHUB_TOKEN with `actions: read`, or none for a public repository).
#
#   python3 manifest/last_upload.py commit
#       the last uploaded commit; exit 3 when there is none in the runs listed, 1 on any error
#   python3 manifest/last_upload.py base --event E [--pr-base SHA] [--base-ref REF] [--before SHA]
#                                        [--input REF]
#       the --base for this run: the PR's base on pull_request (a PR into main only); a
#       dispatch's explicit input; else the last upload. With none, main before the push (or
#       HEAD, the weekly run) only while that commit has no manifest/stable.json.sig (signing
#       not on yet); otherwise it fails, saying to dispatch with an explicit base. A dispatch
#       with no input and no upload fails too. It never guesses: a lookup error fails.
#
# Only main's own runs count: a push or a dispatch, from this repository, of
# .github/workflows/publish.yml (a fork's pull_request run shows head_branch "main" too, with
# its own workflow file). They are listed by event (push, then workflow_dispatch, each on
# main), so other runs cannot push them out of the listing; the filter above is applied again
# to what comes back. Any of them still running, other than this run (GITHUB_RUN_ID), stops
# the lookup: it may be uploading. (Runs on main share one concurrency group: a run that
# stops for a queued one ends and lets it start, and the queued one does not count this one,
# which has ended; it never waits on another.) Every attempt of a run counts (a re-run does
# not hide an earlier upload), ordered by when the read-back completed; the result must be
# an ancestor of main. If the newest upload step ran but was not read back, the bucket may
# serve part of it: that fails, asking for an explicit base. So does a publish job that ran
# without the named steps (renamed), rather than walking back to an older upload.
#
# Environment: GITHUB_TOKEN (optional: this public repository's runs can be read without
# one, at 60 requests an hour), GITHUB_REPOSITORY (default Teaspoon-AI/teaport),
# GITHUB_RUN_ID (this run, in Actions), GITHUB_API_URL. Locally: python3 manifest/last_upload.py commit.
#
# Stdlib only.
#
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

WORKFLOW = "publish.yml"
WORKFLOW_PATH = ".github/workflows/publish.yml"
BRANCH = "main"
REPO = "Teaspoon-AI/teaport"     # GITHUB_REPOSITORY, when not set
JOB = "publish"
# The steps that must both have concluded success: the files went up, and read back the same.
UPLOAD_STEPS = ("Upload to the download bucket", "Read each file back from get.teaspoon.tech")
# Only main's own runs of this workflow count: a fork's pull_request run shows head_branch
# "main" too, with its own workflow file.
EVENTS = ("push", "workflow_dispatch")
PAGES = 10          # 100 runs a page, per event: far more than a gap between uploads
NONE = 3            # exit code: no upload in the runs listed


class LookupFailed(Exception):
    """The record could not be read: the caller fails, never guesses."""


class NoUpload(LookupFailed):
    """None of the runs listed uploaded and read back (<listed>: how many runs were listed)."""

    def __init__(self, listed):
        super().__init__(f"no publish run uploaded in the last {listed} push and dispatch runs on {BRANCH}")
        self.listed = listed


def checked_token(token):
    """<token> stripped, or "" (anonymous: this public repository's runs can be read without
    one, rate-limited). Raises LookupFailed, without the token, when it is not one."""
    token = (token or "").strip()
    if token and not re.fullmatch(r"[A-Za-z0-9_.\-]{10,255}", token):
        raise LookupFailed("GITHUB_TOKEN is not a token (unexpected characters or length)")
    return token


def api_getter(token, repo, base_url=None):
    """GET <path> from the GitHub API, as JSON. Raises LookupFailed (whose message never
    carries the token or a request header)."""
    token = checked_token(token)
    base_url = (base_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    def get(path, params=None):
        url = f"{base_url}/repos/{repo}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "teaport-last-upload"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:  # noqa: S310
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise LookupFailed(f"GitHub API {path}: HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise LookupFailed(f"GitHub API {path}: cannot reach it ({type(e.reason).__name__})") from None
        except Exception as e:  # noqa: BLE001 -- JSON, a header urllib refuses: no text of it
            raise LookupFailed(f"GitHub API {path}: {type(e).__name__}") from None
    return get


def relevant(run, repo):
    """Whether <run> is one of main's own runs of this workflow (a push or a dispatch, from
    this repository, of .github/workflows/publish.yml)."""
    path = str(run.get("path") or "")
    return (run.get("event") in EVENTS and run.get("head_branch") == BRANCH
            and ((run.get("head_repository") or {}).get("full_name") == repo)
            and (path == WORKFLOW_PATH or path.startswith(WORKFLOW_PATH + "@")))


def attempts(get, run):
    """What each attempt of <run> did, from its publish jobs (filter=all: a re-run does not
    hide an earlier attempt): [(kind, time)], kind "uploaded" (upload and read-back both
    succeeded; time = the read-back's completion) or "partial" (the upload step ran but the
    two did not both succeed: the bucket may have changed). Raises LookupFailed, also when a
    publish job ran without the named steps (renamed: never walk back to an older base)."""
    jobs = get(f"actions/runs/{run['id']}/jobs", {"per_page": 100, "filter": "all"}).get("jobs")
    if not isinstance(jobs, list):
        raise LookupFailed(f"run {run.get('id')}: no job list")
    found = []
    for job in jobs:
        if job.get("name") != JOB or job.get("conclusion") == "skipped":
            continue
        steps = {s.get("name"): s for s in job.get("steps") or []}
        if job.get("steps") and not all(name in steps for name in UPLOAD_STEPS):
            raise LookupFailed(f"run {run.get('id')}'s {JOB} job has no step named "
                               f"{[n for n in UPLOAD_STEPS if n not in steps]}: the names "
                               "last_upload.py looks for must be the workflow's")
        upload, readback = (steps.get(name) or {} for name in UPLOAD_STEPS)
        if upload.get("conclusion") == "success" and readback.get("conclusion") == "success":
            found.append(("uploaded", str(readback.get("completed_at") or "")))
        elif upload.get("conclusion") not in (None, "skipped") or upload.get("status") == "in_progress":
            found.append(("partial", str(upload.get("completed_at") or upload.get("started_at") or "")))
    return found


def main_runs(get, repo=REPO, this_run=""):
    """main's own publish runs (relevant()), newest first, and how many runs were listed:
    two listings, by event, each on main, of up to PAGES pages. Raises LookupFailed, also when
    one of them other than <this_run> (GITHUB_RUN_ID) is not completed: it may be uploading."""
    runs, listed = {}, 0
    for event in EVENTS:
        for page in range(1, PAGES + 1):
            found = get(f"actions/workflows/{WORKFLOW}/runs",
                        {"branch": BRANCH, "event": event, "per_page": 100, "page": page})
            batch = found.get("workflow_runs") if isinstance(found, dict) else None
            if not isinstance(batch, list):
                raise LookupFailed("no workflow_runs in the GitHub API's answer")
            listed += len(batch)
            for run in batch:
                if relevant(run, repo):
                    runs[run.get("id")] = run
            if len(batch) < 100:
                break
    for run in runs.values():
        if run.get("status") != "completed" and str(run.get("id")) != str(this_run or ""):
            raise LookupFailed(f"a publish run is in flight (run {run.get('id')}, {run.get('status')}): "
                               "re-run when it finishes")
    return sorted(runs.values(), key=lambda r: str(r.get("updated_at") or ""), reverse=True), listed


def last_upload(get, repo=REPO, this_run=""):
    """The head commit of the publish run whose upload and read-back last succeeded (by the
    read-back's completion, over every attempt). It must be an ancestor of main. Raises
    NoUpload when there is none in the runs listed; LookupFailed on any error, also when a
    later upload was cut off (the bucket may serve it) or another run is in flight."""
    runs, listed = main_runs(get, repo, this_run)
    best = None   # (time, kind, run)
    for run in runs:
        # An attempt ends no later than its run's last update: skip runs that cannot hold
        # anything newer than what is found.
        if best and str(run.get("updated_at") or "") < best[0]:
            continue
        for kind, when in attempts(get, run):
            if best is None or when > best[0]:
                best = (when, kind, run)
    if best is None:
        raise NoUpload(listed)
    when, kind, run = best
    if kind == "partial":
        raise LookupFailed(f"publish run {run.get('id')} ran its upload ({when}) without reading it "
                           "back: the bucket may serve part of it. Dispatch publish with an explicit "
                           "base, the commit whose files the bucket serves")
    sha = run.get("head_sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise LookupFailed(f"run {run.get('id')} has no head_sha")
    status = (get(f"compare/{sha}...{BRANCH}") or {}).get("status")
    if status not in ("ahead", "identical"):
        raise LookupFailed(f"the last upload {sha} is not on {BRANCH} ({status}): not taken")
    return sha


def signing_on(commit, git=None):
    """Whether <commit> has manifest/stable.json.sig (fetched by id when this checkout lacks
    it). Raises LookupFailed when it cannot be told."""
    git = git or (lambda *a: subprocess.run(["git", *a], capture_output=True, text=True))
    if git("cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
        if re.fullmatch(r"[0-9a-f]{40}", commit):
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            git("fetch", "--quiet", "--no-tags", "--depth=1", f"https://github.com/{repo}.git", commit)
        if git("cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
            raise LookupFailed(f"{commit} is not here and cannot be fetched")
    return git("cat-file", "-e", f"{commit}:manifest/stable.json.sig").returncode == 0


def choose_base(event, get, pr_base="", before="", given="", git=None, repo=REPO, base_ref="",
                this_run=""):
    """The --base for this run, and what to say about it: (base, note). Raises LookupFailed."""
    if event == "pull_request":
        if base_ref != BRANCH:
            raise LookupFailed(f"a pull_request into {base_ref!r}, not {BRANCH}: this check is for "
                               f"changes to {BRANCH} only")
        if not pr_base:
            raise LookupFailed("a pull_request run with no base sha")
        return pr_base, f"the PR's base {pr_base}"
    if given:
        return given, f"{given} (the dispatch's explicit base)"
    try:
        found = last_upload(get, repo, this_run)
        return found, f"the last uploaded commit {found} (the last publish run that uploaded and read back)"
    except NoUpload as e:
        none = str(e)
    if event == "workflow_dispatch":
        raise LookupFailed(f"{none}: dispatch again with an explicit base")
    fallback = before if event == "push" and before and set(before) != {"0"} else "HEAD"
    if signing_on(fallback, git):
        raise LookupFailed(f"{none}, but {fallback} has manifest/stable.json.sig (signing is on): "
                           "dispatch publish with an explicit base, the last commit whose files "
                           "the bucket serves")
    return fallback, (f"{fallback}: {none}, and signing is not on there "
                      "(before the first upload with this workflow)")


def main(argv):
    repo = os.environ.get("GITHUB_REPOSITORY") or REPO
    if not argv or argv[0] not in ("commit", "base"):
        print("usage: last_upload.py commit | base --event E [--pr-base SHA] [--base-ref REF] "
              "[--before SHA] [--input REF]", file=sys.stderr)
        return 2
    opts, rest = {}, argv[1:]
    while rest:
        key = rest.pop(0)
        if key not in ("--event", "--pr-base", "--base-ref", "--before", "--input") or not rest:
            print(f"unknown or incomplete argument: {key}", file=sys.stderr)
            return 2
        opts[key] = rest.pop(0)
    try:
        get = api_getter(os.environ.get("GITHUB_TOKEN", ""), repo)
        this_run = os.environ.get("GITHUB_RUN_ID", "")
        if argv[0] == "commit":
            print(last_upload(get, repo, this_run))
            return 0
        base, note = choose_base(opts.get("--event", ""), get, opts.get("--pr-base", ""),
                                 opts.get("--before", ""), opts.get("--input", ""), repo=repo,
                                 base_ref=opts.get("--base-ref", ""), this_run=this_run)
    except NoUpload as e:
        print(str(e), file=sys.stderr)
        return NONE
    except LookupFailed as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    print(f"base: {note}", file=sys.stderr)
    print(base)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
