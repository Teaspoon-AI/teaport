#
# Which commit the publish workflow measures itself against (manifest/last_upload.py): the
# last commit a publish run uploaded and read back, from GitHub's record of the runs, never
# from the bucket and never a guess.
#
# What has to hold:
#   - The last upload is the head of the most recent publish run on main whose publish
#     job's upload and read-back steps both succeeded. A run that skipped the upload (the tip
#     check, missing credentials) or failed before it uploaded nothing; one whose upload step
#     ran without both succeeding may have uploaded part, and stops the lookup.
#   - Only main's own runs, listed by event (push, dispatch) so nothing else floods them out;
#     another of them still running stops the lookup, this run never does.
#   - The base: the PR's base on pull_request (into main only); a dispatch's explicit base; else the last
#     upload. With none, main before the push (HEAD for the weekly run) only while that commit
#     has no manifest/stable.json.sig; a dispatch with no base and no upload, an API error,
#     or signing on there, fails.
#
# The GitHub API and git are faked; nothing leaves the machine.
#
# Run: python test_publish_base.py   (or via pytest test_suite.py)
#
import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import appliance  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "manifest/last_upload.py"


def _mod():
    spec = importlib.util.spec_from_file_location("last_upload", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LU = _mod()
REPO = "Teaspoon-AI/teaport"
SHA = {name: (c * 40) for name, c in (("new", "a"), ("skipped", "b"), ("failed", "c"), ("old", "d"),
                                      ("fork", "f"), ("rerun", "9"))}


def _run(rid, name, when, branch="main", event="push", repo=REPO, path=".github/workflows/publish.yml",
         status="completed"):
    return {"id": rid, "head_sha": SHA[name], "head_branch": branch, "event": event,
            "head_repository": {"full_name": repo}, "path": path, "updated_at": when, "status": status}


def _job(upload="success", readback="success", when="2026-11-01T00:00:00Z", name="publish", steps=None):
    return {"name": name, "conclusion": "success" if readback == "success" else "failure",
            "steps": steps if steps is not None else [
                {"name": "Only what main serves now is published", "conclusion": "success"},
                {"name": LU.UPLOAD_STEPS[0], "conclusion": upload, "completed_at": when},
                {"name": LU.UPLOAD_STEPS[1], "conclusion": readback, "completed_at": when}]}


def _api(runs, jobs, error=None, on_main=None, cap=1000):
    """A fake GitHub API: <runs> newest first, <jobs> {run id: [job, every attempt]};
    <error> raises; <on_main> the commits main contains (default: every sha). Like GitHub's,
    a listing filters on its branch and event and returns no more than <cap> runs; nothing
    may ask it to filter on status (a run in flight must be seen)."""
    asked = []

    def get(path, params=None):
        asked.append((path, params))
        if error:
            raise LU.LookupFailed(error)
        if path.startswith("actions/workflows/"):
            assert path == "actions/workflows/publish.yml/runs", path
            assert set(params) == {"branch", "event", "per_page", "page"}, params
            assert params["branch"] == "main" and params["event"] in ("push", "workflow_dispatch"), params
            listed = [r for r in runs if r["head_branch"] == params["branch"]
                      and r["event"] == params["event"]][:cap]
            page, per = params["page"], params["per_page"]
            return {"workflow_runs": listed[(page - 1) * per:page * per]}
        if path.startswith("compare/"):
            sha = path.split("/")[1].split("...")[0]
            return {"status": "ahead" if on_main is None or sha in on_main else "diverged"}
        assert params.get("filter") == "all", params   # every attempt, not the latest only
        rid = int(path.split("/")[2])
        return {"jobs": jobs[rid]}
    return get, asked


T1, T2, T3, T4 = (f"2026-11-0{d}T00:00:00Z" for d in (1, 2, 3, 4))


def test_the_last_upload_is_the_last_run_that_uploaded_and_read_back():
    runs = [_run(4, "skipped", T4), _run(3, "new", T3, branch="feature"), _run(1, "old", T1)]
    jobs = {4: [{"name": "artifacts", "conclusion": "success", "steps": []},
                _job(upload="skipped", readback="skipped", when=T4)],
            3: [_job(when=T3)],
            1: [{"name": "artifacts", "conclusion": "success", "steps": []}, _job(when=T1)]}
    get, _ = _api(runs, jobs)
    # Skipped at the tip check, another branch: none uploaded.
    assert LU.last_upload(get) == SHA["old"]
    get, _ = _api([_run(5, "new", T4)] + runs, {5: [_job(when=T4)], **jobs})
    assert LU.last_upload(get) == SHA["new"]
    get, _ = _api([], {})
    try:
        LU.last_upload(get)
    except LU.NoUpload as e:
        assert "in the last 0 push and dispatch runs" in str(e), e
    else:
        raise AssertionError("no upload, but an answer")
    get, _ = _api(runs, jobs, error="HTTP 502")
    try:
        LU.last_upload(get)
    except LU.LookupFailed as e:
        assert "502" in str(e)
    else:
        raise AssertionError("an API error was taken as an answer")


def _fails(get, want):
    try:
        LU.last_upload(get)
    except LU.LookupFailed as e:
        assert want in str(e), (want, e)
        return
    raise AssertionError(f"taken, wanted a refusal naming {want!r}")


def test_only_mains_own_runs_count():
    # A fork's pull_request run shows head_branch "main", from the fork's own workflow file,
    # and could name a publish job with two steps that succeed: never the base.
    old = _run(1, "old", T1)
    for name, run in (("fork PR", _run(9, "fork", T4, event="pull_request", repo="someone/teaport")),
                      ("fork push", _run(9, "fork", T4, repo="someone/teaport")),
                      ("PR from this repo", _run(9, "fork", T4, event="pull_request")),
                      ("another workflow file", _run(9, "fork", T4, path=".github/workflows/evil.yml"))):
        get, asked = _api([run, old], {9: [_job(when=T4)], 1: [_job(when=T1)]})
        assert LU.last_upload(get) == SHA["old"], name
        assert not any(p == "actions/runs/9/jobs" for p, _ in asked), name   # never even looked at
    # A dispatch counts, and so does a path GitHub writes with its ref.
    get, _ = _api([_run(9, "new", T4, event="workflow_dispatch",
                        path=".github/workflows/publish.yml@refs/heads/main"), old],
                  {9: [_job(when=T4)], 1: [_job(when=T1)]})
    assert LU.last_upload(get) == SHA["new"]
    # The result must be on main.
    get, _ = _api([old], {1: [_job(when=T1)]}, on_main=set())
    _fails(get, "is not on main")


def test_a_re_run_does_not_hide_an_upload_and_the_newest_read_back_wins():
    # Attempt 1 uploaded, attempt 2 (a re-run) skipped: the run still uploaded.
    get, _ = _api([_run(2, "rerun", T3), _run(1, "old", T1)],
                  {2: [_job(when=T2), _job(upload="skipped", readback="skipped", when=T3)], 1: [_job(when=T1)]})
    assert LU.last_upload(get) == SHA["rerun"]
    # An older run re-run later, whose read-back completed after a newer run's: it is the last.
    get, _ = _api([_run(5, "new", T2), _run(4, "rerun", T4)],
                  {5: [_job(when=T2)], 4: [_job(when=T1), _job(when=T4)]})
    assert LU.last_upload(get) == SHA["rerun"]


def test_an_upload_not_read_back_or_renamed_steps_stop_the_lookup():
    # The newest upload ran but did not read back (failed, cancelled): the bucket may serve
    # part of it, so no base is guessed.
    old = (_run(1, "old", T1), [_job(when=T1)])
    for upload, readback in (("success", "failure"), ("failure", "skipped"), ("cancelled", "skipped")):
        get, _ = _api([_run(2, "failed", T3), old[0]], {2: [_job(upload, readback, when=T3)], 1: old[1]})
        _fails(get, "without reading it back")
    # An older cut-off upload followed by a good one: the good one.
    get, _ = _api([_run(3, "new", T4), _run(2, "failed", T3), old[0]],
                  {3: [_job(when=T4)], 2: [_job("success", "failure", when=T3)], 1: old[1]})
    assert LU.last_upload(get) == SHA["new"]
    # The publish job ran without the steps' names (renamed): never walk back to an older run.
    renamed = [{"name": "Upload", "conclusion": "success", "completed_at": T3}]
    get, _ = _api([_run(2, "new", T3), old[0]], {2: [_job(steps=renamed, when=T3)], 1: old[1]})
    _fails(get, "has no step named")


def test_other_runs_cannot_flood_mains_out_of_the_listing():
    # GitHub lists at most 1000 runs for a filter. A fork's PRs from a branch named main (each
    # needs no approval to be listed) and the weekly runs outnumber main's own: listed by
    # event, main's pushes and dispatches are all still there.
    flood = [_run(10_000 + i, "fork", T4, event="pull_request", repo="someone/teaport") for i in range(1500)]
    flood += [_run(20_000 + i, "fork", T4, event="schedule") for i in range(200)]
    get, asked = _api(flood + [_run(1, "old", T1)], {1: [_job(when=T1)]})
    assert LU.last_upload(get) == SHA["old"]
    assert [p for p, _ in asked if p.endswith("/jobs")] == ["actions/runs/1/jobs"], asked[-3:]
    # No upload among them: how many of main's runs were looked at, not "yet".
    pushes = [_run(100 + i, "skipped", T2) for i in range(150)]
    get, _ = _api(flood + pushes, {100 + i: [_job("skipped", "skipped", when=T2)] for i in range(150)})
    try:
        LU.choose_base("workflow_dispatch", get)
    except LU.LookupFailed as e:
        assert "in the last 150 push and dispatch runs on main" in str(e) and "explicit base" in str(e), e
        assert "yet" not in str(e), e
    else:
        raise AssertionError("no upload, but a base")


def test_a_run_in_flight_stops_the_lookup_but_this_run_does_not():
    old = (_run(1, "old", T1), [_job(when=T1)])
    for status in ("queued", "in_progress", "pending", "waiting", "requested"):
        get, _ = _api([_run(2, "new", T3, status=status), old[0]], {2: [], 1: old[1]})
        _fails(get, "a publish run is in flight (run 2")
        _fails(get, "re-run when it finishes")
        # Not even this run (GITHUB_RUN_ID) may be skipped for another.
        try:
            LU.last_upload(get, this_run="3")
        except LU.LookupFailed as e:
            assert "in flight" in str(e)
        else:
            raise AssertionError(status)
    # A run in flight that is not main's own (a PR, the weekly run, a fork) does not count.
    for run in (_run(2, "fork", T3, event="pull_request", status="in_progress"),
                _run(2, "fork", T3, event="schedule", status="in_progress"),
                _run(2, "fork", T3, repo="someone/teaport", status="in_progress")):
        get, _ = _api([run, old[0]], {1: old[1]})
        assert LU.last_upload(get) == SHA["old"], run
    # The concurrency group: run A is running (its own lookup), run B queued behind it. A
    # never counts itself; it stops for B and ends, which lets B start; B's lookup no longer
    # sees A in flight, and nothing waits on anything: no deadlock.
    a = _run(5, "new", T3, status="in_progress")
    b = _run(6, "rerun", T4, status="pending")
    get, _ = _api([b, a, old[0]], {5: [], 6: [], 1: old[1]})
    try:
        LU.last_upload(get, this_run="5")
    except LU.LookupFailed as e:
        assert "run 6" in str(e) and "in flight" in str(e), e
    else:
        raise AssertionError("A did not stop for B")
    a = dict(a, status="completed")
    b = dict(b, status="in_progress")
    get, _ = _api([b, a, old[0]], {5: [_job("skipped", "skipped", when=T3)], 6: [], 1: old[1]})
    assert LU.last_upload(get, this_run="6") == SHA["old"]
    # Alone, this run is never in flight to itself, by number or by text.
    get, _ = _api([_run(7, "new", T4, status="in_progress"), old[0]], {7: [], 1: old[1]})
    assert LU.last_upload(get, this_run=7) == SHA["old"]
    assert LU.choose_base("push", get, before="e" * 40, this_run="7")[0] == SHA["old"]
    # The command line takes this run from GITHUB_RUN_ID.
    saved = (LU.api_getter, os.environ.get("GITHUB_RUN_ID"))
    try:
        LU.api_getter = lambda token, repo: get
        for run_id, rc, said in (("7", 0, SHA["old"]), ("8", 1, "in flight")):
            os.environ["GITHUB_RUN_ID"] = run_id
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                assert LU.main(["commit"]) == rc, run_id
            assert said in out.getvalue() + err.getvalue(), (run_id, out.getvalue(), err.getvalue())
    finally:
        LU.api_getter = saved[0]
        if saved[1] is None:
            os.environ.pop("GITHUB_RUN_ID", None)
        else:
            os.environ["GITHUB_RUN_ID"] = saved[1]


def test_missing_credentials_leave_the_upload_skipped_and_nothing_uploaded():
    # The publish job as it is now: the credentials check is its own step, before the upload.
    # Missing secrets fail it and the upload step is skipped: the run uploaded nothing, and
    # the lookup walks back to the last run that did.
    def creds_failed(when):
        return {"name": "publish", "conclusion": "failure", "steps": [
            {"name": "Only what main serves now is published", "conclusion": "success"},
            {"name": "The bucket's credentials are set", "conclusion": "failure", "completed_at": when},
            {"name": LU.UPLOAD_STEPS[0], "status": "completed", "conclusion": "skipped",
             "started_at": when, "completed_at": when},
            {"name": LU.UPLOAD_STEPS[1], "status": "completed", "conclusion": "skipped",
             "started_at": when, "completed_at": when}]}
    get, _ = _api([_run(3, "failed", T4), _run(2, "failed", T3), _run(1, "old", T1)],
                  {3: [creds_failed(T4)], 2: [creds_failed(T3)], 1: [_job(when=T1)]})
    assert LU.last_upload(get) == SHA["old"]
    # Before, the check was the upload step's first lines: such a run shows the upload step
    # failed and stops the lookup (the step could have failed halfway through its files).
    get, _ = _api([_run(2, "failed", T3), _run(1, "old", T1)],
                  {2: [_job("failure", "skipped", when=T3)], 1: [_job(when=T1)]})
    _fails(get, "without reading it back")


def test_a_token_is_checked_and_never_said():
    for bad in ("ghs_abc\nInjected: x", "token with spaces", "ghs_" + "é" * 20):
        try:
            LU.api_getter(bad, REPO)
        except LU.LookupFailed as e:
            assert bad not in str(e) and "Injected" not in str(e), e
        else:
            raise AssertionError(repr(bad))
    assert LU.checked_token("  ghs_" + "a" * 36 + "\n") == "ghs_" + "a" * 36
    assert LU.checked_token("") == ""   # anonymous: a public repository's runs read without one


def _git(have_sig):
    """A fake git: every commit is here; <have_sig> {commit: whether it has stable.json.sig}."""
    def git(*args):
        if args[0] == "cat-file" and args[2].endswith(":manifest/stable.json.sig"):
            ok = have_sig.get(args[2].split(":")[0], False)
        else:
            ok = True
        return subprocess.CompletedProcess(args, 0 if ok else 1, "", "")
    return git


def test_the_base_is_the_last_upload_and_never_a_guess():
    before = "e" * 40
    found, _ = _api([_run(1, "old", T1)], {1: [_job(when=T1)]})
    none, _ = _api([], {})
    broken, _ = _api([], {}, error="network unreachable")
    # A PR: its base, whatever the record says (the API is not even asked).
    assert LU.choose_base("pull_request", broken, pr_base=before, base_ref="main")[0] == before
    # Only a PR into main: one into another branch (or none named) is refused.
    for ref in ("release", "", "refs/heads/main"):
        try:
            LU.choose_base("pull_request", broken, pr_base=before, base_ref=ref)
        except LU.LookupFailed as e:
            assert "not main" in str(e), e
        else:
            raise AssertionError(f"a PR into {ref!r} taken")
    # A dispatch: its explicit base wins; empty is the lookup; with no upload it fails.
    assert LU.choose_base("workflow_dispatch", broken, given=SHA["new"])[0] == SHA["new"]
    assert LU.choose_base("workflow_dispatch", found)[0] == SHA["old"]
    for get, want in ((none, "explicit base"), (broken, "network unreachable")):
        try:
            LU.choose_base("workflow_dispatch", get, git=_git({}))
        except LU.LookupFailed as e:
            assert want in str(e), e
        else:
            raise AssertionError(want)
    # A push and the weekly run: the last upload.
    assert LU.choose_base("push", found, before=before)[0] == SHA["old"]
    assert LU.choose_base("schedule", found)[0] == SHA["old"]
    # No upload yet: main before the push (HEAD weekly) only while signing is not on there.
    assert LU.choose_base("push", none, before=before, git=_git({before: False}))[0] == before
    assert LU.choose_base("schedule", none, git=_git({"HEAD": False}))[0] == "HEAD"
    for event, kw in (("push", {"before": before}), ("schedule", {})):
        try:
            LU.choose_base(event, none, git=_git({before: True, "HEAD": True}), **kw)
        except LU.LookupFailed as e:
            assert "signing is on" in str(e) and "explicit base" in str(e), e
        else:
            raise AssertionError(event)
    # A lookup that fails never falls back.
    try:
        LU.choose_base("push", broken, before=before, git=_git({}))
    except LU.LookupFailed as e:
        assert "network unreachable" in str(e)
    else:
        raise AssertionError("a lookup error fell back")


def test_the_command_line_says_nothing_on_stdout_when_it_fails():
    # Whatever reads its output ($(...)) gets nothing on a failure: a malformed token, or a
    # GitHub API that cannot be reached.
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GITHUB_REPOSITORY")}
    p = subprocess.run([sys.executable, str(SCRIPT), "commit"], capture_output=True, text=True,
                       env={**env, "GITHUB_TOKEN": "usage: gh auth [flags]"})
    assert p.returncode == 1 and p.stdout == "" and "not a token" in p.stderr and "usage" not in p.stderr, p
    p = subprocess.run([sys.executable, str(SCRIPT), "base", "--event", "push", "--before", "e" * 40],
                       capture_output=True, text=True, env={**env, "GITHUB_API_URL": "http://127.0.0.1:9"})
    assert p.returncode == 1 and p.stdout == "" and "cannot reach" in p.stderr, p
    p = subprocess.run([sys.executable, str(SCRIPT), "base", "--event", "pull_request", "--pr-base", "f" * 40,
                        "--base-ref", "main"], capture_output=True, text=True, env=env)
    assert p.returncode == 0 and p.stdout.strip() == "f" * 40, p
    p = subprocess.run([sys.executable, str(SCRIPT), "base", "--event", "pull_request", "--pr-base", "f" * 40,
                        "--base-ref", "release"], capture_output=True, text=True, env=env)
    assert p.returncode == 1 and p.stdout == "" and "not main" in p.stderr, p
    p = subprocess.run([sys.executable, str(SCRIPT), "base", "--event", "workflow_dispatch", "--input", "f" * 40],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0 and p.stdout.strip() == "f" * 40, p
    assert subprocess.run([sys.executable, str(SCRIPT), "nope"], capture_output=True, env=env).returncode == 2


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
