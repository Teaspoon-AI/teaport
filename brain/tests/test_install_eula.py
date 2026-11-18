#
# install.sh's engine license gate (eula_gate) and the signed downloads behind it
# (engine_url): the acceptance is recorded by the license server (get-worker/) before the
# engine downloads, a repair run renews its URLs with the token it was given, headless
# acceptance names the license version, and an acceptance from before the server is made
# again.
#
# The functions are taken out of install.sh as they are (as test_install_engine_only.py
# does) and run by bash against a fake license server on 127.0.0.1, in a temp dir. The
# server here stands in for get-worker/src/index.js; its own tests are get-worker/test/.
#
# Run: python test_install_eula.py   (or via pytest test_suite.py)
#
import hashlib
import http.server
import json
import os
import pathlib
import pty
import select
import subprocess
import threading
import time

import appliance
from tempdirs import tempdir

REPO = pathlib.Path(__file__).resolve().parents[2]
INSTALL = REPO / "install.sh"
EULA = "Teaport Engine License v1.0\nthe terms\n"
EULA_SHA = hashlib.sha256(EULA.encode()).hexdigest()


def _fn(name: str) -> str:
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] in ("}", "PY") and
               (lines[i] == "}" or lines[i + 1] == "}"))
    if lines[end] == "PY":
        end += 1
    return "\n".join(lines[start:end + 1]) + "\n"


def _line(prefix: str) -> str:
    return next(ln for ln in INSTALL.read_text().splitlines() if ln.startswith(prefix)) + "\n"


class Server:
    """The license server's two endpoints, the license text, and what it was sent."""

    def __init__(self):
        self.calls, self.renew_status, self.accept_status = [], 200, 201
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/eula/teaport-engine-license-1.0.txt":
                    return self._send(200, EULA, "text/plain")
                self._send(404, "no")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.calls.append((self.path, body))
                signed = {p: f"{outer.base}{p}?sig=ok" for p in body.get("paths", [])}
                if self.path == "/v1/engine-license/accept":
                    if outer.accept_status != 201:
                        return self._send(outer.accept_status, {"error": "x", "message": "the server said no"})
                    return self._send(201, {"acceptance_id": "aid-1", "license_version": body["license_version"],
                                            "license_sha256": body["license_sha256"],
                                            "accepted_at": "2026-11-18T00:00:00.000Z", "token": "v1.tok.mac",
                                            "urls": signed, "expires_at": "x"})
                if self.path == "/v1/engine-license/urls":
                    if outer.renew_status != 200:
                        return self._send(outer.renew_status, {"error": "bad_token", "message": "revoked"})
                    return self._send(200, {"acceptance_id": "aid-1", "license_version": "1.0", "urls": signed})
                self._send(404, {"error": "not_found"})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def posts(self, path):
        return [b for p, b in self.calls if p == path]


def _manifest(tmp, base, eula_version="1.0"):
    m = json.loads((REPO / "manifest/stable.json").read_text())
    text = json.dumps(m).replace("https://get.teaspoon.tech", base)
    m = json.loads(text)
    m["engine"]["eula"].update(version=eula_version, sha256=EULA_SHA)
    p = pathlib.Path(tmp, "manifest.json")
    p.write_text(json.dumps(m))
    return p


STUBS = r'''
set -euo pipefail
DRY_RUN=0 RUN_USER="$(id -un)" ACCEPT_EULA="${ACCEPT_EULA:-}" EULA_EMAIL="${EULA_EMAIL:-}"
INSTALLER_VERSION=git:test STATE="$T/state"
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
die()  { printf 'DIE %s\n' "$*"; exit 1; }
have() { return 1; }   # no pager: the text is cat'ed
SUDO() { "$@"; }
'''
FNS = ("mget", "license_py", "license_call", "license_renew", "engine_url", "eula_gate")


def _script(tmp, srv, body):
    return (STUBS + f'MF="{_manifest(tmp, srv.base)}" LICENSE_API="{srv.base}"\n' + _line("EULA_RECEIPT=") +
            'LICENSE_TOKEN="" LICENSE_URLS=""\n' + "".join(_fn(f) for f in FNS) + body)


def _run(tmp, srv, body, env=None):
    e = {**os.environ, "T": str(tmp), "TMPDIR": str(tmp), **(env or {})}
    p = subprocess.run(["bash", "-c", _script(tmp, srv, body)], capture_output=True, text=True, env=e,
                       stdin=subprocess.DEVNULL, timeout=60)
    return p.returncode, p.stdout + p.stderr


def _receipt(tmp):
    return json.loads(pathlib.Path(tmp, "state/eula-accepted.json").read_text())


GATED = {"/dl/engine/teaport-engine-1.6-l4t38-cu13", "/dl/engine/teaport-engine-1.6-g2p.dict",
         "/dl/engine/teaport-engine-1.6-pos.bin", "/dl/models/kokoro/Kokoro_espeak_F16.gguf",
         "/dl/models/voxtral/consolidated.safetensors", "/dl/models/voxtral/params.json",
         "/dl/models/voxtral/tekken.json"}


def test_headless_acceptance_is_recorded_by_the_server_then_the_engine_is_signed():
    tmp, srv = tempdir("teaport-eula-"), Server()
    rc, out = _run(tmp, srv, 'eula_gate\n'
                   'engine_url "$(mget engine.assets.l4t38-cu13.url)"\n'
                   'engine_url "$(mget engine.notices.url)"\n'
                   'engine_url "$(mget brain.deb.url)"\n',
                   {"ACCEPT_EULA": "1.0", "EULA_EMAIL": "owner@example.com"})
    assert rc == 0, out
    [body] = srv.posts("/v1/engine-license/accept")
    assert body == {"license_version": "1.0", "license_sha256": EULA_SHA, "email": "owner@example.com",
                    "method": "flag", "installer_version": "git:test", "paths": sorted(GATED)}, body
    r = _receipt(tmp)
    assert r["token"] == "v1.tok.mac" and r["acceptance_id"] == "aid-1" and r["method"] == "flag", r
    assert r["email"] == "owner@example.com" and r["version"] == "1.0" and r["sha256"] == EULA_SHA, r
    assert oct(pathlib.Path(tmp, "state/eula-accepted.json").stat().st_mode & 0o777) == "0o600"
    assert pathlib.Path(tmp, "state/EULA-v1.0.txt").read_text() == EULA
    lines = out.splitlines()
    assert f"{srv.base}/dl/engine/teaport-engine-1.6-l4t38-cu13?sig=ok" in lines, out
    # The notices and the brain package are public: their own URLs.
    assert f"{srv.base}/dl/engine/teaport-engine-1.6-THIRD_PARTY_NOTICES.txt" in lines, out
    assert f"{srv.base}/dl/brain/teaport-brain_0.1.0_arm64.deb" in lines, out
    assert "deprecated" not in out


def test_a_headless_version_that_is_not_the_manifests_aborts_before_anything():
    for state in (None, "accepted"):
        tmp, srv = tempdir("teaport-eula-"), Server()
        if state:
            _run(tmp, srv, "eula_gate\n", {"ACCEPT_EULA": "1.0", "EULA_EMAIL": "a@b.co"})
            srv.calls.clear()
        rc, out = _run(tmp, srv, "eula_gate\n", {"ACCEPT_EULA": "2.0", "EULA_EMAIL": "a@b.co"})
        assert rc != 0 and "DIE --accept-eula=2.0 accepts engine license v2.0, but this release's is v1.0" in out, out
        assert srv.calls == [], srv.calls


def test_the_bare_flag_still_works_this_release_with_a_warning_but_needs_an_email():
    tmp, srv = tempdir("teaport-eula-"), Server()
    rc, out = _run(tmp, srv, "eula_gate\n", {"ACCEPT_EULA": "1"})
    assert rc != 0 and "deprecated" in out and "needs the email address" in out and srv.calls == [], out
    rc, out = _run(tmp, srv, "eula_gate\n", {"ACCEPT_EULA": "1", "EULA_EMAIL": "a@b.co"})
    assert rc == 0 and "WARN --accept-eula without a version" in out, out
    assert srv.posts("/v1/engine-license/accept")[0]["method"] == "flag"


def test_a_repair_run_renews_urls_with_its_token_and_asks_nothing():
    tmp, srv = tempdir("teaport-eula-"), Server()
    _run(tmp, srv, "eula_gate\n", {"ACCEPT_EULA": "1.0", "EULA_EMAIL": "a@b.co"})
    srv.calls.clear()
    rc, out = _run(tmp, srv, 'eula_gate\nengine_url "$(mget models.kokoro.url)"\n')   # no flag, no TTY
    assert rc == 0 and "already accepted" in out, out
    assert srv.posts("/v1/engine-license/accept") == [], srv.calls
    [renew] = srv.posts("/v1/engine-license/urls")
    assert renew == {"token": "v1.tok.mac", "paths": sorted(GATED)}, renew
    assert f"{srv.base}/dl/models/kokoro/Kokoro_espeak_F16.gguf?sig=ok" in out.splitlines(), out


def test_a_receipt_from_before_the_server_accepts_again():
    tmp, srv = tempdir("teaport-eula-"), Server()
    st = pathlib.Path(tmp, "state")
    st.mkdir()
    (st / "eula-accepted.json").write_text(json.dumps({"version": "1.0", "sha256": EULA_SHA,
                                                       "date": "2026-10-01T00:00:00", "user": "me", "method": "tty"}))
    # Headless with no flag: stops, and says why.
    rc, out = _run(tmp, srv, "eula_gate\n")
    assert rc != 0 and "never recorded with Teaspoon AI" in out and "needs acceptance" in out, out
    assert srv.calls == []
    rc, out = _run(tmp, srv, "eula_gate\n", {"ACCEPT_EULA": "1.0", "EULA_EMAIL": "a@b.co"})
    assert rc == 0 and _receipt(tmp)["token"] == "v1.tok.mac", out


def test_a_token_the_server_no_longer_honours_is_accepted_again():
    tmp, srv = tempdir("teaport-eula-"), Server()
    _run(tmp, srv, "eula_gate\n", {"ACCEPT_EULA": "1.0", "EULA_EMAIL": "a@b.co"})
    srv.calls.clear()
    srv.renew_status = 401
    rc, out = _run(tmp, srv, "eula_gate\n", {"ACCEPT_EULA": "1.0", "EULA_EMAIL": "a@b.co"})
    assert rc == 0 and "no longer honours" in out, out
    assert len(srv.posts("/v1/engine-license/accept")) == 1


def test_a_server_that_refuses_or_is_down_stops_the_run_with_no_receipt():
    tmp, srv = tempdir("teaport-eula-"), Server()
    srv.accept_status = 409
    rc, out = _run(tmp, srv, "eula_gate\n", {"ACCEPT_EULA": "1.0", "EULA_EMAIL": "a@b.co"})
    assert rc != 0 and "(HTTP 409): the server said no" in out, out
    assert not pathlib.Path(tmp, "state/eula-accepted.json").exists()
    # Unreachable: the text comes from one place, the server is gone.
    dead = Server()
    dead.httpd.shutdown()
    dead.httpd.server_close()
    body = _script(tmp, srv, "eula_gate\n").replace(f'LICENSE_API="{srv.base}"', f'LICENSE_API="{dead.base}"')
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                       env={**os.environ, "T": str(tmp), "TMPDIR": str(tmp), "ACCEPT_EULA": "1.0",
                            "EULA_EMAIL": "a@b.co"})
    assert p.returncode != 0 and "(HTTP 000)" in p.stdout, p.stdout + p.stderr
    assert not pathlib.Path(tmp, "state/eula-accepted.json").exists()


def test_a_tty_acceptance_asks_for_an_email_and_records_it():
    tmp, srv = tempdir("teaport-eula-"), Server()
    master, slave = pty.openpty()
    e = {**os.environ, "T": str(tmp), "TMPDIR": str(tmp)}
    p = subprocess.Popen(["bash", "-c", _script(tmp, srv, "eula_gate\n")], stdin=slave, stdout=slave, stderr=slave,
                         env=e, start_new_session=True)
    os.close(slave)
    out, sent = b"", []
    answers = [(b"Press Enter", b"\n"), (b"Type \"accept\"", b"accept\n"), (b"Your email address", b"nope\n"),
               (b"not an email address", b"  me@example.com \n")]
    deadline = time.time() + 30
    while time.time() < deadline:
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        for prompt, reply in answers:
            if prompt in out and prompt not in sent and out.count(prompt) > sum(1 for s in sent if s == prompt):
                sent.append(prompt)
                os.write(master, reply)
        if p.poll() is not None and not r:
            break
    p.wait(timeout=10)
    os.close(master)
    text = out.decode(errors="replace")
    assert p.returncode == 0, text
    assert "the terms" in text and "records your acceptance" in text, text
    [body] = srv.posts("/v1/engine-license/accept")
    assert body["method"] == "tty" and body["email"] == "me@example.com", body


def test_the_dry_run_records_nothing():
    tmp, srv = tempdir("teaport-eula-"), Server()
    rc, out = _run(tmp, srv, "DRY_RUN=1; eula_gate\n", {"ACCEPT_EULA": "1.0", "EULA_EMAIL": "a@b.co"})
    assert rc == 0 and "[dry-run] accept engine license v1.0 non-interactively as a@b.co" in out, out
    assert srv.calls == [] and not pathlib.Path(tmp, "state").exists()


def test_the_arguments():
    def parse(*args, env=None):
        p = subprocess.run(["bash", str(INSTALL), *args, "--help"], capture_output=True, text=True,
                           env={**os.environ, **(env or {})}, timeout=30)
        return p.returncode, p.stdout + p.stderr
    assert parse("--accept-eula=1.0", "--eula-email=a@b.co")[0] == 0
    assert parse("--accept-eula", "--eula-email", "a@b.co")[0] == 0
    assert parse(env={"TEAPORT_ACCEPT_EULA": "0"})[0] == 0           # the old "ask"
    # --help exits before the version check; the check itself, without --help:
    p = subprocess.run(["bash", str(INSTALL), "--accept-eula=yes", "--dry-run"], capture_output=True, text=True,
                       timeout=30)
    assert p.returncode == 2 and "is not a license version" in p.stderr, p.stderr
    p = subprocess.run(["bash", str(INSTALL), "--accept-eula=", "--dry-run"], capture_output=True, text=True, timeout=30)
    assert p.returncode == 2 and "needs the license version" in p.stderr, p.stderr


def test_the_installer_and_the_worker_gate_the_same_paths():
    worker = (REPO / "get-worker/src/index.js").read_text()
    assert 'const GATED_PREFIXES = ["/dl/engine/", "/dl/models/"];' in worker
    assert 'const PUBLIC_SUFFIXES = ["THIRD_PARTY_NOTICES.txt"];' in worker
    gated = _fn("license_py")
    assert 'path.startswith("/dl/engine/") or path.startswith("/dl/models/")' in gated
    assert 'not path.endswith("THIRD_PARTY_NOTICES.txt")' in gated


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
