#
# manifest/check.py's list of what a release manifest must name, against install.sh's reads:
# from engine 1.7 on, the AI watermark's weights (engine.watermark) are one of them, by the
# same rule install.sh's engine_release_marks applies. And how it reads the license gate's
# answer to a download without a token. Offline: the gate is a server on loopback.
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
    url = f"https://{check.HOST}/dl/engine/f"
    manifest = {"access": {"api": f"https://{check.HOST}/api/", "gated": [f"https://{check.HOST}/dl/engine/"]},
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


if __name__ == "__main__":
    test_the_watermark_rule_is_install_shs()
    test_a_release_that_marks_must_name_its_watermark()
    test_a_gated_file_must_be_refused_without_a_token_unless_the_gate_is_open()
    test_a_gated_file_is_asked_without_a_token_on_every_run()
    print("ok")
