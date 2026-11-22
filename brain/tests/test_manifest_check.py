#
# manifest/check.py's list of what a release manifest must name, against install.sh's reads:
# from engine 1.7 on, the AI watermark's weights (engine.watermark) are one of them, by the
# same rule install.sh's engine_release_marks applies. And how it reads the license gate's
# answer to a download without a token, and to the same file asked for at a percent-encoded
# path the gate is not routed on. Offline: the gate is a server on loopback.
#
# Run: python test_manifest_check.py   (or via pytest test_suite.py)
#
import http.server
import importlib.util
import json
import pathlib
import re
import subprocess
import threading

REPO = pathlib.Path(__file__).resolve().parents[2]
INSTALL = REPO / "install.sh"


def _check():
    spec = importlib.util.spec_from_file_location("manifest_check", REPO / "manifest/check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _install_marks(release):
    """install.sh's own answer: engine_release_marks and ENGINE_WATERMARK_FROM, as written."""
    text = INSTALL.read_text()
    first = re.search(r"^ENGINE_WATERMARK_FROM=.*$", text, re.M).group(0)
    lines = text.splitlines()
    start = lines.index("engine_release_marks() {")
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    fn = "\n".join(lines[start:end + 1])
    p = subprocess.run(["bash", "-c", f'{first}\n{fn}\nengine_release_marks "$1"', "-", release],
                       capture_output=True, text=True)
    assert p.returncode in (0, 1), p.stderr
    return p.returncode == 0


def test_the_watermark_rule_is_install_shs():
    check = _check()
    for release in ("1.6", "1.6.9", "1.7", "1.7.0", "1.10", "2", "TODO", "unknown", "", "1.7-rc1"):
        assert check.release_marks(release) == _install_marks(release), release
    assert check.release_marks(None)


def test_a_release_that_marks_must_name_its_watermark():
    check = _check()
    assert "engine.watermark" not in check.required({"engine": {"release": "1.6"}})
    assert "engine.watermark" in check.required({"engine": {"release": "1.7"}})
    assert "engine.watermark" in check.required({})   # no release named: fail closed
    # What the channels carry today: stable (1.6) does not need it, beta (a placeholder) names one.
    stable = json.loads((REPO / "manifest/stable.json").read_text())
    beta = json.loads((REPO / "manifest/beta.json").read_text())
    if not check.release_marks(stable["engine"]["release"]):
        assert "engine.watermark" not in check.required(stable)
    else:
        assert stable["engine"].get("watermark", {}).get("sha256"), "stable marks but names no watermark"
    assert "engine.watermark" in check.required(beta) and beta["engine"]["watermark"]["url"]


def test_a_gated_file_must_be_refused_without_a_token_unless_the_gate_is_open():
    check = _check()
    slept = []
    check.time = type("Clock", (), {"sleep": staticmethod(slept.append)})
    seen, flaky = [], iter([503, 502])

    class Gate(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            seen.append(self.command)
            status, headers = {"/closed": (401, {}), "/open": (200, {"x-teaport-gate": "open"}),
                               "/public": (200, {}), "/down": (503, {})}.get(self.path, (404, {}))
            if self.path == "/flaky":
                status = next(flaky, 401)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gate)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert check.refused(f"{base}/closed") is None
        assert check.refused(f"{base}/open") == check.GATE_OPEN
        assert "not in front of it" in check.refused(f"{base}/public")
        assert check.refused(f"{base}/gone") == "HTTP 404"
        assert slept == []   # an answer is final
        assert check.refused(f"{base}/flaky") is None   # 503, 502, then the gate's 401
        assert check.refused(f"{base}/down") == "HTTP 503"
        assert len(slept) == 2 + check.ATTEMPTS - 1
    finally:
        srv.shutdown()
        srv.server_close()
    assert set(seen) == {"HEAD"}   # an open gate is never asked for the file itself


def test_a_gated_file_is_asked_without_a_token_on_every_run():
    """With DL_CHECK_TOKEN set (pushes, the weekly run) as without: an open gate warns and
    the file is still checked with the token; a gate not in front of it is an error and
    the file is not fetched."""
    import contextlib
    import io
    import os
    import tempfile
    check = _check()
    check.bypassed = lambda u: None
    url = f"https://{check.HOST}/dl/engine/f"
    manifest = {"access": {"api": f"https://{check.HOST}/api", "gated": [f"https://{check.HOST}/dl/engine/"]},
                "engine": {"f": {"url": url, "sha256": "0" * 64}}}
    fetched = []

    class Digest:
        def hexdigest(self):
            return "0" * 64

    check.fetch = lambda u, digest, token=None: fetched.append((u, token)) or Digest()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(manifest, f)
    old = os.environ.get("DL_CHECK_TOKEN")
    os.environ["DL_CHECK_TOKEN"] = "t"
    try:
        for answer, warns, fails, fetches in ((check.GATE_OPEN, True, False, True),
                                              (None, False, False, True),
                                              ("served without a download token: the license gate "
                                               "is not in front of it", False, True, False)):
            fetched.clear()
            check.refused = lambda u, a=answer: a
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                check.check(f.name, True)
            text = out.getvalue()
            assert ("::warning::engine.f" in text) == warns, text
            assert ("::error" in text and "not in front of it" in text) == fails, text
            assert fetched == ([(url, "t")] if fetches else []), (answer, fetched)
    finally:
        os.unlink(f.name)
        if old is None:
            del os.environ["DL_CHECK_TOKEN"]
        else:
            os.environ["DL_CHECK_TOKEN"] = old


def _run(check, manifest, token="", require_enforce=False):
    """check.check() on <manifest> with DL_CHECK_TOKEN=<token>: (passed, what it printed)."""
    import contextlib
    import io
    import os
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(manifest, f)
    old = os.environ.get("DL_CHECK_TOKEN")
    os.environ["DL_CHECK_TOKEN"] = token
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            ok = check.check(f.name, True, require_enforce)
    finally:
        os.unlink(f.name)
        if old is None:
            del os.environ["DL_CHECK_TOKEN"]
        else:
            os.environ["DL_CHECK_TOKEN"] = old
    return ok, out.getvalue()


def _gated_manifest(check):
    """Two gated files, and nothing else check() requires: offline, with fetch() stubbed
    to serve what the manifest says, so the check passes or fails on the gate alone."""
    check.required = lambda manifest: ()
    check.REQUIRED_REPOS = ()
    check.fetch = lambda u, digest, token=None: type("D", (), {"hexdigest": lambda self: "0" * 64})()
    return {"access": {"api": f"https://{check.HOST}/api",
                       "gated": [f"https://{check.HOST}/dl/engine/", f"https://{check.HOST}/dl/models/"]},
            "engine": {"f": {"url": f"https://{check.HOST}/dl/engine/f.bin", "sha256": "0" * 64}},
            "models": {"m": {"url": f"https://{check.HOST}/dl/models/m/w.gguf", "sha256": "0" * 64}},
            "openclaw": {"version": "2026.9.7", "node": "24"}}


def test_the_encoded_forms_of_a_gated_path():
    """Every slash of the path encoded, and the one that ends the gated prefix: neither
    matches the Worker's routes (get.teaspoon.tech/dl/engine/*, /dl/models/*)."""
    check = _check()
    h = f"https://{check.HOST}"
    assert check.encoded_forms(f"{h}/dl/engine/teaport-engine-1.6-pos.bin", f"{h}/dl/engine/") == [
        f"{h}/dl%2Fengine%2Fteaport-engine-1.6-pos.bin", f"{h}/dl/engine%2Fteaport-engine-1.6-pos.bin"]
    assert check.encoded_forms(f"{h}/dl/models/voxtral/params.json", f"{h}/dl/models/") == [
        f"{h}/dl%2Fmodels%2Fvoxtral%2Fparams.json", f"{h}/dl/models%2Fvoxtral%2Fparams.json"]


def test_a_gated_file_served_at_an_encoded_path_is_caught():
    """bypassed(): a 404 (or any 4xx) passes; a 2xx is the file served around the gate; a 5xx
    is retried as refused() retries, and one that stays fails, as no answer does."""
    check = _check()
    slept = []
    check.time = type("Clock", (), {"sleep": staticmethod(slept.append)})
    seen, flaky = [], iter([503, 502])

    class Bucket(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            seen.append((self.command, self.path))
            status = {"/dl%2Fengine%2Fleft": 200, "/dl/engine%2Fleft": 200, "/dl%2Fengine%2Fgone": 404,
                      "/dl/engine%2Fdenied": 403, "/down": 503}.get(self.path, 404)
            if self.path == "/flaky":
                status = next(flaky, 404)
            self.send_response(status)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bucket)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        for u in check.encoded_forms(f"{base}/dl/engine/left", f"{base}/dl/engine/"):
            assert "served without the license gate" in check.bypassed(u), u
        assert check.bypassed(f"{base}/dl%2Fengine%2Fgone") is None
        assert check.bypassed(f"{base}/dl/engine%2Fdenied") is None
        assert slept == []   # an answer is final
        assert check.bypassed(f"{base}/flaky") is None   # 503, 502, then the bucket's 404
        assert "HTTP 503" in check.bypassed(f"{base}/down")
        assert len(slept) == 2 + check.ATTEMPTS - 1
    finally:
        srv.shutdown()
        srv.server_close()
    assert {m for m, _ in seen} == {"HEAD"}   # the file itself is never asked for
    # The encoding reaches the server as sent: urllib does not decode %2F.
    assert ("HEAD", "/dl%2Fengine%2Fleft") in seen and ("HEAD", "/dl/engine%2Fleft") in seen


def test_a_file_served_around_the_gate_is_an_error_on_every_run():
    """Each gated file's encoded forms are asked for with the token and without, after the
    gate refused it or served it open, and one that answers fails the check."""
    check = _check()
    asked = []
    for token in ("", "t"):
        for answer in (None, check.GATE_OPEN):
            check.refused = lambda u, a=answer: a
            check.bypassed = lambda u: asked.append(u)
            ok, text = _run(check, _gated_manifest(check), token)
            assert ok and "::error" not in text, text
            assert asked == [f for u, p in ((f"https://{check.HOST}/dl/engine/f.bin", f"https://{check.HOST}/dl/engine/"),
                                            (f"https://{check.HOST}/dl/models/m/w.gguf", f"https://{check.HOST}/dl/models/"))
                             for f in check.encoded_forms(u, p)], asked
            asked.clear()
            check.bypassed = lambda u: "HTTP 200: served" if "%2Fw.gguf" in u else None
            ok, text = _run(check, _gated_manifest(check), token)
            assert not ok, text
            errors = [line for line in text.splitlines() if line.startswith("::error")]
            assert len(errors) == 2 and all("models.m" in e and "HTTP 200: served" in e for e in errors), text
            assert "ok models.m" not in text and ("ok engine.f" in text) == (answer is None or bool(token)), text


def test_an_open_gate_fails_the_check_under_require_enforce():
    """The weekly run passes --require-enforce: a gate still open is an error there, a warning
    everywhere else."""
    check = _check()
    check.bypassed = lambda u: None
    check.refused = lambda u: check.GATE_OPEN
    for token in ("", "t"):
        ok, text = _run(check, _gated_manifest(check), token)
        assert ok and "::warning::engine.f" in text and "::error" not in text, text
        ok, text = _run(check, _gated_manifest(check), token, require_enforce=True)
        assert not ok and "::warning" not in text, text
        assert "::error" in text and "GATE is open" in text and "deploy main's Worker" in text, text
    check.refused = lambda u: None   # enforcing: nothing to report
    ok, text = _run(check, _gated_manifest(check), "t", require_enforce=True)
    assert ok and "::error" not in text, text


def _publish_step():
    """publish.yml's check step: (its run script, with python3 stubbed to print its argv and
    the DL_CHECK_TOKEN it gets; its env's DL_CHECK_TOKEN expression as a function of the
    event and the secret, GitHub's && and || being Python's and and or)."""
    text = (REPO / ".github/workflows/publish.yml").read_text().splitlines()
    start = next(i for i, l in enumerate(text) if "The manifests name everything install.sh needs" in l)
    run = next(i for i in range(start, len(text)) if text[i].strip() == "run: |")
    indent = len(text[run + 1]) - len(text[run + 1].lstrip())
    body = []
    for line in text[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    script = ('set -eo pipefail\npython3() { echo "python3 $* token=${DL_CHECK_TOKEN}"; }\n'
              + "\n".join(body))
    [expr] = [re.fullmatch(r"\s*DL_CHECK_TOKEN: \$\{\{(.*)\}\}\s*", l).group(1)
              for l in text[start:run] if l.strip().startswith("DL_CHECK_TOKEN:")]
    py = expr.replace("&&", " and ").replace("||", " or ")
    py = py.replace("github.event_name", "event").replace("secrets.DL_CHECK_TOKEN", "secret")
    # Nothing else may be in it: names, quoted strings, comparisons, and/or.
    assert re.fullmatch(r"(\s|event|secret|and|or|!=|==|'[a-z_]*')+", py), expr
    return script, lambda event, secret: eval(py, {"__builtins__": {}}, {"event": event, "secret": secret})


def _publish_run(script, event, token):
    import os
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={**os.environ, "EVENT": event, "DL_CHECK_TOKEN": token})


def test_only_the_weekly_run_requires_an_enforcing_gate():
    """publish.yml's check step, run with python3 stubbed: --require-enforce on a schedule
    event, and on no other."""
    script, _ = _publish_step()
    for event in ("schedule", "push", "workflow_dispatch", "pull_request"):
        p = _publish_run(script, event, "t")
        assert p.returncode == 0, (event, p.stdout, p.stderr)
        calls = [l for l in p.stdout.splitlines() if l.startswith("python3 manifest/check.py")]
        assert len(calls) == 2, (event, p.stdout)
        assert all(("--require-enforce" in c) == (event == "schedule") for c in calls), (event, calls)


def test_only_a_pull_requests_run_goes_without_the_download_token():
    """The step's env gives every run but a pull request's the DL_CHECK_TOKEN secret, and a
    pull request's none even when the secret is set; a run that should have it and does not
    fails before any check, with an ::error."""
    script, token_for = _publish_step()
    assert token_for("pull_request", "s3cret") == ""
    for event in ("push", "schedule", "workflow_dispatch"):
        assert token_for(event, "s3cret") == "s3cret", event
        assert token_for(event, "") == "", event   # the secret not set
        p = _publish_run(script, event, token_for(event, ""))
        assert p.returncode == 1, (event, p.stdout, p.stderr)
        assert "::error::DL_CHECK_TOKEN is not set" in p.stdout, (event, p.stdout)
        assert "python3 manifest/check.py" not in p.stdout, (event, p.stdout)
        p = _publish_run(script, event, token_for(event, "s3cret"))
        calls = [l for l in p.stdout.splitlines() if l.startswith("python3 manifest/check.py")]
        assert p.returncode == 0 and len(calls) == 2 and all(c.endswith("token=s3cret") for c in calls), (event, p.stdout)
    for secret in ("s3cret", ""):
        p = _publish_run(script, "pull_request", token_for("pull_request", secret))
        calls = [l for l in p.stdout.splitlines() if l.startswith("python3 manifest/check.py")]
        assert p.returncode == 0 and len(calls) == 2 and all(c.endswith("token=") for c in calls), (secret, p.stdout)


def _origin(tmp):
    """A repository standing in for github.com/o/r (check.GITHUB = file://<tmp>): tag
    brain-old's install.sh predates the gate, tag brain-new's and branch dev's read access."""
    bare = pathlib.Path(tmp, "o/r.git")
    work = pathlib.Path(tmp, "work")
    g = lambda *a, cwd=work: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                                            cwd=cwd, check=True, capture_output=True)
    work.mkdir()
    g("init", "-q", "-b", "dev")
    (work / "install.sh").write_text("#!/bin/bash\nUPDATE_PROTOCOL=1\n")
    g("add", "install.sh")
    g("commit", "-qm", "before the gate")
    g("tag", "brain-old")
    (work / "install.sh").write_text("#!/bin/bash\nUPDATE_PROTOCOL=1\nACCESS_PROTOCOL=1\n")
    g("commit", "-qam", "the gate")
    g("tag", "brain-new")
    bare.parent.mkdir()
    g("clone", "-q", "--bare", str(work), str(bare), cwd=tmp)
    g("config", "uploadpack.allowFilter", "true", cwd=bare)
    return f"file://{tmp}"


def test_with_access_a_release_tag_must_be_an_installer_that_reads_it():
    """teaport update runs brain.tag's install.sh as root: with access in the manifest, a tag
    from before the gate would download every gated file without a token. A branch is not
    asked (teaport update refuses one), nor is anything without access."""
    from tempdirs import tempdir
    check = _check()
    check.GITHUB = _origin(tempdir())
    assert "predates the license gate" in check.reads_access("o/r", "brain-old")
    assert check.reads_access("o/r", "brain-new") is None
    assert check.reads_access("o/r", "dev") is None
    check.bypassed = lambda u: None
    check.refused = lambda u: None
    for tag, access, passes in (("brain-old", True, False), ("brain-new", True, True), ("dev", True, True),
                                ("brain-old", False, True)):
        manifest = _gated_manifest(check)
        manifest["brain"] = {"repo": "o/r", "tag": tag}
        if not access:
            del manifest["access"]
            manifest["engine"]["f"]["url"] = f"https://{check.HOST}/dl/brain/f.deb"
            manifest["models"]["m"]["url"] = f"https://{check.HOST}/dl/brain/m.deb"
        ok, text = _run(check, manifest, "t")
        assert ok == passes, (tag, access, text)
        assert ("ACCESS_PROTOCOL" in text) == (not passes), (tag, access, text)
    assert check.ACCESS_PROTOCOL in INSTALL.read_text()   # this tree's installer says it


def test_access_api_must_not_end_in_a_slash():
    """install.sh POSTs to <api>/accept: an api ending in / would be //accept, a 404 on every
    install."""
    check = _check()
    h = f"https://{check.HOST}"
    gated = [f"{h}/dl/engine/"]
    assert check.gate({"access": {"api": f"{h}/api", "gated": gated}}) == (gated, [])
    for api in (f"{h}/api/", f"{h}/"):
        _, problems = check.gate({"access": {"api": api, "gated": gated}})
        assert len(problems) == 1 and "ends in /" in problems[0], (api, problems)
    for name in ("stable", "beta"):
        manifest = json.loads((REPO / f"manifest/{name}.json").read_text())
        assert check.gate(manifest)[1] == [], name


if __name__ == "__main__":
    test_the_watermark_rule_is_install_shs()
    test_a_release_that_marks_must_name_its_watermark()
    test_a_gated_file_must_be_refused_without_a_token_unless_the_gate_is_open()
    test_a_gated_file_is_asked_without_a_token_on_every_run()
    test_the_encoded_forms_of_a_gated_path()
    test_a_gated_file_served_at_an_encoded_path_is_caught()
    test_a_file_served_around_the_gate_is_an_error_on_every_run()
    test_an_open_gate_fails_the_check_under_require_enforce()
    test_only_the_weekly_run_requires_an_enforcing_gate()
    test_only_a_pull_requests_run_goes_without_the_download_token()
    test_with_access_a_release_tag_must_be_an_installer_that_reads_it()
    test_access_api_must_not_end_in_a_slash()
    print("ok")
