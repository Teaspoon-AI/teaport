#
# The release manifest's signature: nothing that installs as root (teaport update's root step,
# install.sh) or offers an update (the brain's check) believes a manifest the release keys did
# not sign. One check does it for all of them: brain/teaport_brain/manifest_sig.py (ssh-keygen
# -Y verify, namespace teaport-manifest, against manifest/allowed_signers as the package ships it).
#
# What has to hold:
#   - A manifest signed by the main key or the backup verifies; one with a byte changed, one
#     signed in another namespace, by a key not in the signers file, or with no signature at
#     all does not, and neither does anything while the signers file has no key (the repo's
#     placeholder). A <channel>.json must say it is that channel's (a signed stable is no beta).
#   - update_apply.read_manifest fetches <url> and <url>.sig, verifies before it reads, keeps
#     both in its work directory, and refuses (nothing written) otherwise; it trusts only the
#     package's root-owned check and keys. teaport update never lets install.sh skip the check.
#   - install.sh's fetch_manifest treats a URL, a file:// URL and a path alike (the .sig beside
#     it), uses the package's keys and, with no package, its own checkout's; only a LOCAL
#     manifest with no .sig passes, and only with TEAPORT_MANIFEST_UNSIGNED=1.
#   - manifest/check.py fails a manifest whose signature does not verify, and lets an unsigned
#     one through only under --placeholders-ok when it names nothing installable.
#   - The brain's look (updates.load) shows a manifest that does not verify as an error.
#   - A release build refuses the placeholder signers file; the package ships the check, the
#     keys, and depends on openssh-client.
#
# Throwaway keys made here with the real ssh-keygen (release_keys.py); every path is a temp
# dir's, the one loopback HTTP server is the test's own. Nothing on the system is read or written.
#
# Run: python test_manifest_sig.py   (or via pytest test_suite.py)
#
import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import appliance  # noqa: E402
from release_keys import PLACEHOLDER_SIGNERS, Keys, commit_files  # noqa: E402
from tempdirs import tempdir  # noqa: E402

from teaport_brain import manifest_sig  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"
BOOTSTRAP_TEXT = (ROOT / "install-bootstrap.sh").read_text()
SIG_PY = ROOT / "brain/teaport_brain/manifest_sig.py"
KEYS = Keys()


def _manifest(channel="stable", brain="0.2.0"):
    return {"channel": channel, "brain": {"repo": "Teaspoon-AI/teaport", "tag": f"brain-v{brain}",
            "deb": {"url": f"https://get.teaspoon.tech/dl/brain/teaport-brain_{brain}_arm64.deb",
                    "sha256": "a" * 64}}}


def _body(channel="stable"):
    return json.dumps(_manifest(channel)).encode()


def _refused(fn, *want):
    try:
        fn()
    except (manifest_sig.SignatureError, RuntimeError) as e:
        assert all(w in str(e) for w in want), (want, str(e))
        return str(e)
    raise AssertionError(f"accepted, wanted a refusal naming {want}")


# --- the check ---------------------------------------------------------------------------

def test_the_main_and_the_backup_key_verify_and_nothing_else_does():
    body, url = _body(), "https://get.teaspoon.tech/manifest/stable.json"
    for key in ("main", "backup"):
        got, said = manifest_sig.accept(body, KEYS.sign(body, key), url, KEYS.signers)
        assert got == _manifest() and f"release-{key}" in said, said
    sig = KEYS.sign(body)
    tampered = body.replace(b"0.2.0", b"0.2.1")
    _refused(lambda: manifest_sig.accept(tampered, sig, url, KEYS.signers), "does not verify")
    _refused(lambda: manifest_sig.accept(body + b" ", sig, url, KEYS.signers), "does not verify")
    _refused(lambda: manifest_sig.accept(body, KEYS.sign(body, namespace="file"), url, KEYS.signers),
             "does not verify", "namespace")
    _refused(lambda: manifest_sig.accept(body, KEYS.sign(body, "stranger"), url, KEYS.signers),
             "not signed by a trusted key")
    _refused(lambda: manifest_sig.accept(body, None, url, KEYS.signers), "no signature")
    # A URL never goes unsigned, even when unsigned is allowed; a local file may.
    _refused(lambda: manifest_sig.accept(body, None, url, KEYS.signers, unsigned_ok=True), "no signature")
    got, said = manifest_sig.accept(body, None, "/srv/mirror/stable.json", KEYS.signers, unsigned_ok=True)
    assert got == _manifest() and "not signed" in said, said
    _refused(lambda: manifest_sig.accept(body, b"garbage", url, KEYS.signers), "not an SSH signature")


def test_a_signed_manifest_counts_only_for_the_channel_it_names():
    beta = _body("beta")
    sig = KEYS.sign(beta)
    _refused(lambda: manifest_sig.accept(beta, sig, "https://h/manifest/stable.json", KEYS.signers),
             "signed for the 'beta' channel, not 'stable'")
    _refused(lambda: manifest_sig.accept(beta, sig, "https://h/beta.json", KEYS.signers, channel="stable"),
             "not 'stable'")
    assert manifest_sig.accept(beta, sig, "file:///m/beta.json", KEYS.signers)[0]["channel"] == "beta"
    # A name that is no channel's binds none (a dev box's own manifest).
    assert manifest_sig.accept(beta, sig, "/home/me/box-manifest.json", KEYS.signers)[0]["channel"] == "beta"
    assert [manifest_sig.channel_named(s) for s in ("https://h/manifest/stable.json", "/a/beta.json",
            "file:///a/stable.json", "/a/nightly.json", "/a/stable.json.bak")] == ["stable", "beta", "stable", None, None]


def test_no_key_in_the_signers_file_verifies_nothing():
    body = _body()
    sig = KEYS.sign(body)
    # The signers file before the release keys exist: comments only (placeholders are
    # comments, never a key line).
    placeholder = os.path.join(tempdir("teaport-placeholder-"), "allowed_signers")
    pathlib.Path(placeholder).write_text(PLACEHOLDER_SIGNERS)
    assert manifest_sig.keys(PLACEHOLDER_SIGNERS) == []
    _refused(lambda: manifest_sig.accept(body, sig, "/m/stable.json", placeholder), "no trusted signing key")
    _refused(lambda: manifest_sig.accept(body, sig, "/m/stable.json", "/nonexistent/allowed_signers"),
             "cannot read the trusted signing keys")
    # No ssh-keygen: refused, never accepted.
    _refused(lambda: manifest_sig.accept(body, sig, "/m/stable.json", KEYS.signers,
                                         keygen="/nonexistent/ssh-keygen"), "missing")


def test_the_command_line_says_who_signed_or_why_not():
    d = tempdir("teaport-sig-cli-")
    m = os.path.join(d, "stable.json")
    KEYS.write(m, _manifest())

    def run(*args):
        p = subprocess.run([sys.executable, "-I", str(SIG_PY), "check", *args], capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr
    rc, out = run(m, m + ".sig", KEYS.signers)
    assert rc == 0 and "release-main" in out, out
    rc, out = run(m, m + ".sig", KEYS.signers, "--source", "https://h/manifest/beta.json")
    assert rc == 1 and "not 'beta'" in out, out
    os.unlink(m + ".sig")
    rc, out = run(m, m + ".sig", KEYS.signers)
    assert rc == 1 and "no signature" in out, out
    rc, out = run(m, m + ".sig", KEYS.signers, "--unsigned-ok")
    assert rc == 0 and "not signed" in out, out
    assert run(m)[0] == 2


# --- teaport update's root step ----------------------------------------------------------

def _apply(tmp):
    """update_apply, loaded fresh, trusting a root-owned-as-far-as-it-knows copy of the check
    and the test keys (ROOT_UID is ours)."""
    spec = importlib.util.spec_from_file_location(f"ua_sig_{os.getpid()}_{len(os.listdir(tmp))}",
                                                  ROOT / "packaging/deb/update_apply.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    lib = os.path.join(tmp, "lib")
    os.makedirs(lib, exist_ok=True)
    os.chmod(lib, 0o755)
    shutil.copy(SIG_PY, os.path.join(lib, "manifest_sig.py"))
    shutil.copy(KEYS.signers, os.path.join(lib, "allowed_signers"))
    for f in ("manifest_sig.py", "allowed_signers"):
        os.chmod(os.path.join(lib, f), 0o644)
    mod.SIG_CHECK, mod.SIGNERS = os.path.join(lib, "manifest_sig.py"), os.path.join(lib, "allowed_signers")
    mod.ROOT_UID = os.getuid()
    return mod


def _served(files):
    asked = []

    def fetch(url):
        asked.append(url)
        if url not in files:
            raise OSError(f"HTTP Error 404: {url}")
        return files[url]
    return fetch, asked


def test_the_root_step_installs_only_from_a_manifest_that_verifies():
    tmp = tempdir("teaport-apply-sig-")
    mod = _apply(tmp)
    url = "https://get.teaspoon.tech/manifest/stable.json"
    body = _body()
    work = tempdir("teaport-apply-work-")
    for key in ("main", "backup"):
        fetch, asked = _served({url: body, url + ".sig": KEYS.sign(body, key)})
        dest = os.path.join(work, f"{key}-stable.json")
        assert mod.read_manifest("stable", dest, fetch=fetch) == _manifest()
        assert asked == [url, url + ".sig"], asked
        # The pair install.sh is handed: the exact bytes, and their signature.
        assert pathlib.Path(dest).read_bytes() == body and pathlib.Path(dest + ".sig").read_bytes().startswith(b"-----BEGIN SSH")
    cases = {
        "missing": ({url: body}, "no signature"),
        "tampered": ({url: body.replace(b"0.2.0", b"0.2.9"), url + ".sig": KEYS.sign(body)}, "does not verify"),
        "namespace": ({url: body, url + ".sig": KEYS.sign(body, namespace="git")}, "does not verify"),
        "unknown": ({url: body, url + ".sig": KEYS.sign(body, "stranger")}, "not signed by a trusted key"),
        # A signed beta manifest served at stable's address.
        "channel": ({url: _body("beta"), url + ".sig": KEYS.sign(_body("beta"))}, "not 'stable'"),
    }
    for name, (files, want) in cases.items():
        dest = os.path.join(work, f"{name}.json")
        why = _refused(lambda: mod.read_manifest("stable", dest, fetch=_served(files)[0]), want)
        assert "nothing" in why, why
        assert not os.path.exists(dest) and not os.path.exists(dest + ".sig"), name
    # The check and the keys must be root's alone: a writable one is not trusted.
    os.chmod(mod.SIGNERS, 0o666)
    fetch, _ = _served({url: body, url + ".sig": KEYS.sign(body)})
    _refused(lambda: mod.read_manifest("stable", os.path.join(work, "w.json"), fetch=fetch), "not root's own file")
    os.chmod(mod.SIGNERS, 0o644)
    mod.ROOT_UID = os.getuid() + 1
    _refused(lambda: mod.read_manifest("stable", os.path.join(work, "u.json"), fetch=fetch), "not root's own file")


def test_the_root_step_leaves_no_bytecode_beside_the_package_files():
    # Loading the check by its path would write /usr/lib/teaport/__pycache__/*.pyc as root:
    # a file no package owns.
    tmp = tempdir("teaport-apply-pyc-")
    mod = _apply(tmp)
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = False
    try:
        mod.signature_check()
        assert not os.path.exists(os.path.join(tmp, "lib", "__pycache__")), os.listdir(os.path.join(tmp, "lib"))
    finally:
        sys.dont_write_bytecode = saved


def test_the_root_step_hands_install_sh_the_verified_pair_and_never_the_unsigned_switch():
    mod = _apply(tempdir("teaport-apply-env-"))
    env = mod.step_env("bob", "/home/bob", "/tmp/w/stable.json")
    assert env["TEAPORT_MANIFEST_URL"] == "/tmp/w/stable.json"
    assert not any("UNSIGNED" in k for k in env), env
    src = (ROOT / "packaging/deb/update_apply.py").read_text()
    assert "manifest_copy = os.path.join(work, f\"{plan['channel']}.json\")" in src


# --- install.sh ---------------------------------------------------------------------------

def _fn(name):
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _fetch(url, *, here=ROOT, pkg_signers=None, unsigned=""):
    """install.sh's fetch_manifest, as it is, against <url>: (exit code, output, the bytes it
    kept). <pkg_signers> stands in for /usr/lib/teaport/allowed_signers (None: no package)."""
    tmp = tempdir("teaport-fetch-")
    script = ('log() { printf "LOG %s\\n" "$*"; }\nwarn() { printf "WARN %s\\n" "$*"; }\n'
              'die() { printf "DIE %s\\n" "$*"; exit 1; }\n'
              + f'HERE="{here}" MANIFEST_URL="{url}" MANIFEST_UNSIGNED="{unsigned or 0}"\n'
              + f'MANIFEST_CHECK_PY="{here}/brain/teaport_brain/manifest_sig.py" GATED=() DOWNLOAD_HOST=get.teaspoon.tech\n'
              + f'MANIFEST_SIGNERS_PKG="{pkg_signers or os.path.join(tmp, "no-package", "allowed_signers")}"\n'
              + _fn("manifest_cleanup") + _fn("exit_trap_base") + _fn("manifest_signers") + _fn("fetch_manifest")
              + f'fetch_manifest\ncp "$MF" "{tmp}/kept"\nprintf "MF %s\\n" "$MF_DIR"\n')
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={**os.environ, "TMPDIR": tmp, "no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"})
    mf_dir = next((ln[3:] for ln in p.stdout.splitlines() if ln.startswith("MF ")), None)
    if mf_dir:   # one directory of the run's own, holding both, gone when the run ends
        assert pathlib.Path(mf_dir).parent == pathlib.Path(tmp) and not os.path.exists(mf_dir), mf_dir
    else:
        assert [n for n in os.listdir(tmp) if n.startswith("tmp.")] == [], os.listdir(tmp)
    kept = pathlib.Path(tmp, "kept")
    return p.returncode, p.stdout + p.stderr, (kept.read_bytes() if mf_dir else None)


@contextlib.contextmanager
def _server(files):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = files.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_install_sh_verifies_a_url_a_file_url_and_a_path_alike():
    d = tempdir("teaport-mirror-")
    m = os.path.join(d, "stable.json")
    body = KEYS.write(m, _manifest())
    for url in (m, f"file://{m}"):
        rc, out, kept = _fetch(url, pkg_signers=KEYS.signers)
        assert rc == 0 and "LOG manifest" in out and "release-main" in out and kept == body, out
    KEYS.write(m, _manifest(), key="backup")
    rc, out, _ = _fetch(m, pkg_signers=KEYS.signers)
    assert rc == 0 and "release-backup" in out, out
    with _server({"/manifest/stable.json": body, "/manifest/stable.json.sig": KEYS.sign(body),
                  "/manifest/beta.json": body, "/manifest/beta.json.sig": KEYS.sign(body),
                  "/bad/stable.json": body, "/bad/stable.json.sig": KEYS.sign(body, "stranger"),
                  "/none/stable.json": body}) as base:
        rc, out, kept = _fetch(f"{base}/manifest/stable.json", pkg_signers=KEYS.signers)
        assert rc == 0 and "release-main" in out and kept == body, out
        for path, want in (("/bad/stable.json", "not signed by a trusted key"),
                           ("/none/stable.json", "no signature"),
                           ("/manifest/beta.json", "not 'beta'")):
            rc, out, kept = _fetch(base + path, pkg_signers=KEYS.signers)
            assert rc == 1 and "DIE the release manifest is refused" in out and want in out and kept is None, (path, out)
        # The unsigned switch is for a local manifest only.
        rc, out, _ = _fetch(f"{base}/none/stable.json", pkg_signers=KEYS.signers, unsigned="1")
        assert rc == 1 and "no signature" in out, out


def test_only_an_absolute_path_or_file_url_is_local_and_may_go_unsigned():
    # install.sh hands anything else to curl: "host/path" is plain HTTP, not a file.
    assert [manifest_sig.is_local(s) for s in ("/m/stable.json", "file:///m/stable.json",
            "host/m/stable.json", "127.0.0.1:8080/stable.json", "https://h/stable.json")] \
        == [True, True, False, False, False]
    body = _body()
    _refused(lambda: manifest_sig.accept(body, None, "host/m/stable.json", KEYS.signers, unsigned_ok=True),
             "no signature")
    with _server({"/m/stable.json": body}) as base:
        rc, out, _ = _fetch(base[len("http://"):] + "/m/stable.json", pkg_signers=KEYS.signers, unsigned="1")
        assert rc == 1 and "no signature" in out, out
    from teaport_brain import updates
    _refused(lambda: updates.manifest_sig.accept(body, None, "h/stable.json", KEYS.signers,
                                                 unsigned_ok=True), "no signature")


def test_install_sh_installs_no_artifact_without_its_sha256():
    tmp = tempdir("teaport-download-")
    script = ('log() { printf "LOG %s\\n" "$*"; }\nwarn() { printf "WARN %s\\n" "$*"; }\n'
              'die() { printf "DIE %s\\n" "$*"; exit 1; }\nSUDO() { "$@"; }\n'
              'curl() { printf "CURL %s\\n" "$*"; return 0; }\n' + _fn("download"))
    for dry, want in (("0", "DIE the manifest names no sha256 for voxtral"), ("1", "WARN the manifest names no sha256")):
        p = subprocess.run(["bash", "-c", script + f'DRY_RUN={dry}\ndownload https://h/voxtral "{tmp}/voxtral" ""\n'],
                           capture_output=True, text=True)
        assert want in p.stdout and "CURL" not in p.stdout, (dry, p.stdout + p.stderr)
        assert (p.returncode == 1) == (dry == "0"), p.stdout
    assert not os.path.exists(os.path.join(tmp, "voxtral"))
    # Every caller passes the manifest's sha256; none relies on an empty one.
    calls = [ln.strip() for ln in INSTALL.read_text().splitlines()
             if re.match(r"\s*(download|engine_fetch) \"", ln)]
    assert calls and all(not ln.endswith('""') for ln in calls), calls


def test_install_sh_refuses_a_local_manifest_it_cannot_verify_unless_told_to_take_it_unsigned():
    d = tempdir("teaport-local-")
    m = os.path.join(d, "stable.json")
    KEYS.write(m, _manifest())
    pathlib.Path(m).write_bytes(pathlib.Path(m).read_bytes().replace(b"0.2.0", b"6.6.6"))
    rc, out, _ = _fetch(m, pkg_signers=KEYS.signers)
    assert rc == 1 and "does not verify" in out, out
    rc, out, _ = _fetch(m, pkg_signers=KEYS.signers, unsigned="1")   # a bad .sig is never skipped
    assert rc == 1 and "does not verify" in out, out
    os.unlink(m + ".sig")
    rc, out, _ = _fetch(m, pkg_signers=KEYS.signers)
    assert rc == 1 and "no signature" in out, out
    rc, out, kept = _fetch(m, pkg_signers=KEYS.signers, unsigned="1")
    assert rc == 0 and "WARN manifest" in out and "TEAPORT_MANIFEST_UNSIGNED=1" in out and b"6.6.6" in kept, out


def test_install_sh_uses_its_checkouts_keys_on_a_box_with_no_package():
    # A first install (the factory's first boot): no /usr/lib/teaport yet, so the keys come
    # from the checkout install.sh runs from.
    here = tempdir("teaport-checkout-")
    os.makedirs(os.path.join(here, "brain/teaport_brain"))
    os.makedirs(os.path.join(here, "manifest"))
    shutil.copy(SIG_PY, os.path.join(here, "brain/teaport_brain/manifest_sig.py"))
    shutil.copy(KEYS.signers, os.path.join(here, "manifest/allowed_signers"))
    d = tempdir("teaport-first-")
    m = os.path.join(d, "stable.json")
    KEYS.write(m, _manifest())
    rc, out, _ = _fetch(m, here=here)
    assert rc == 0 and "manifest/allowed_signers" in out, out
    # The package's keys, once there, are the ones used: these trust nothing that signed it.
    other = os.path.join(d, "pkg_signers")
    pathlib.Path(other).write_text("# no keys\n")
    rc, out, _ = _fetch(m, here=here, pkg_signers=other)
    assert rc == 1 and "no trusted signing key" in out, out
    # A checkout whose signers file is the placeholder, and no package: nothing verifies.
    pathlib.Path(here, "manifest/allowed_signers").write_text(PLACEHOLDER_SIGNERS)
    rc, out, _ = _fetch(m, here=here)
    assert rc == 1 and "no trusted signing key" in out, out


def test_update_applys_copy_verifies_again_in_install_sh():
    # What teaport update hands install.sh: <work>/<channel>.json with its .sig beside it.
    tmp = tempdir("teaport-apply-then-install-")
    mod = _apply(tmp)
    url = "https://get.teaspoon.tech/manifest/stable.json"
    body = _body()
    fetch, _ = _served({url: body, url + ".sig": KEYS.sign(body)})
    work = tempdir("teaport-work-")
    dest = os.path.join(work, "stable.json")
    mod.read_manifest("stable", dest, fetch=fetch)
    env = mod.step_env("bob", "/home/bob", dest)
    rc, out, kept = _fetch(env["TEAPORT_MANIFEST_URL"], pkg_signers=KEYS.signers)
    assert rc == 0 and kept == body and "release-main" in out, out


# --- the publish check ----------------------------------------------------------------------

def _check_py():
    spec = importlib.util.spec_from_file_location("manifest_check", ROOT / "manifest/check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_publish_check_verifies_each_manifests_signature():
    check = _check_py()
    check.SIGNERS = KEYS.signers
    d = tempdir("teaport-publish-")
    stable = os.path.join(d, "stable.json")
    KEYS.write(stable, _manifest())

    def run(path, placeholders_ok=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = check.check(path, placeholders_ok, signature_only=True)
        return ok, out.getvalue()
    ok, out = run(stable)
    assert ok and "ok signature" in out and "release-main" in out, out
    KEYS.write(stable, _manifest(), key="backup")
    assert run(stable)[0]
    for key, namespace, want in (("stranger", "teaport-manifest", "not signed by a trusted key"),
                                 ("main", "file", "does not verify")):
        KEYS.write(stable, _manifest(), key=key, namespace=namespace)
        ok, out = run(stable)
        assert not ok and want in out and "::error" in out, out
    KEYS.write(stable, _manifest())
    with open(stable, "ab") as f:
        f.write(b"\n")
    ok, out = run(stable)
    assert not ok and "does not verify" in out, out
    # A signed beta is no stable.
    KEYS.write(stable, _manifest("beta"))
    ok, out = run(stable)
    assert not ok and "not 'stable'" in out, out
    # Unsigned: a problem, unless the channel names nothing installable and placeholders are ok.
    os.unlink(stable + ".sig")
    KEYS.write(stable, _manifest(), key=None)
    assert not run(stable)[0] and not run(stable, placeholders_ok=True)[0]
    beta = os.path.join(d, "beta.json")
    KEYS.write(beta, {"channel": "beta", "brain": {"deb": {"url": "https://get.teaspoon.tech/dl/TODO", "sha256": "TODO"}}},
               key=None)
    ok, out = run(beta, placeholders_ok=True)
    assert ok and "names nothing installable" in out, out
    ok, out = run(beta)
    assert not ok and "missing" in out, out
    # The repo's own beta.json, all placeholders, passes unsigned under --placeholders-ok.
    check.SIGNERS = str(ROOT / "manifest/allowed_signers")
    if not os.path.exists(ROOT / "manifest/beta.json.sig"):
        assert run(str(ROOT / "manifest/beta.json"), placeholders_ok=True)[0]


def _git(repo, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True, env=env).stdout


def test_the_publish_check_holds_a_rotation_to_the_keys_boxes_have():
    # Boxes trust the keys of the package they run: those at the brain.commit of the
    # channel's manifest as published now. A rotation release's own tag is pushed before its
    # manifest PR, so the newest tag is the wrong reference: here it exists first, and a
    # manifest signed only by the key the rotation adds is still caught.
    lines = {ln.split()[0]: ln for ln in pathlib.Path(KEYS.signers).read_text().splitlines()
             if ln.startswith("release-")}
    repo = tempdir("teaport-rotation-repo-")
    _git(repo, "init", "-q")
    os.makedirs(os.path.join(repo, "manifest"))
    signers = pathlib.Path(repo, "manifest/allowed_signers")
    # The release boxes have: trusts the main and the backup key.
    signers.write_text(lines["release-main"] + "\n" + lines["release-backup"] + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "release 1")
    _git(repo, "tag", "brain-v1.0.0")
    old = _git(repo, "rev-parse", "HEAD").strip()
    # The rotation: the main key goes, the backup becomes release-main, a new key (here:
    # "stranger") comes in as release-backup; its tag first.
    stranger = subprocess.run(["ssh-keygen", "-y", "-f", os.path.join(KEYS.dir, "stranger")],
                              capture_output=True, text=True, check=True).stdout.split()
    new_line = f'release-backup namespaces="teaport-manifest,teaport-bootstrap" {stranger[0]} {stranger[1]}'
    signers.write_text(lines["release-backup"].replace("release-backup", "release-main", 1) + "\n"
                       + new_line + "\n")
    _git(repo, "commit", "-qam", "rotation")
    _git(repo, "tag", "brain-v2.0.0")
    new = _git(repo, "rev-parse", "HEAD").strip()
    check = _check_py()
    check.HERE = os.path.join(repo, "manifest")
    check.SIGNERS = str(signers)            # the PR's keys: backup and the new one
    # The PR's base: main with the release boxes have, as published (git records it).
    def base_with(files):
        check.BASE[0] = commit_files(repo, old, files)
    pub = lambda manifest: json.dumps(manifest).encode()   # noqa: E731
    base_with({"manifest/stable.json": pub({**_manifest(), "brain": {**_manifest()["brain"], "commit": old}})})
    found, why = check.boxes_signers("stable")
    assert why is None and found and old[:12] in found[0], (found, why)
    try:
        assert "release-main" in pathlib.Path(found[1]).read_text()
        d = tempdir("teaport-rotation-")
        m = os.path.join(d, "stable.json")
        rotation = {**_manifest(), "brain": {**_manifest()["brain"], "commit": new}}
        KEYS.write(m, rotation, key="stranger")    # only the key the rotation adds
        assert check.signature(m, False) is None   # the PR's keys alone would let it through
        why = check.signature(m, False, also=(found,))
        assert why and "not with the published stable release" in why, why
        KEYS.write(m, rotation, key="backup")      # a key both name: what RELEASING.md says
        assert check.signature(m, False, also=(found,)) is None
        # The rotation PR also re-signs the manifest published now with the backup: signed by
        # the dropped main key, it would fail that PR's own check (and every weekly one).
        current = {**_manifest(), "brain": {**_manifest()["brain"], "commit": old}}
        m2 = os.path.join(tempdir("teaport-rotation-current-"), "stable.json")
        KEYS.write(m2, current, key="main")
        why = check.signature(m2, False, also=(found,))
        assert why and "not signed by a trusted key" in why, why
        KEYS.write(m2, current, key="backup")
        assert check.signature(m2, False, also=(found,)) is None
        # The key change the release brings (its commit's signers against the boxes'), named:
        # it passes only signed by a key the boxes already trust.
        change, why = check.release_key_change(m, found)
        assert why is None and "- release-main" in change and f"+ {new_line}" in change, (change, why)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert check.signature(m, False, also=(found,), key_change=change) is None
        assert "::warning" in out.getvalue() and "changes the keys boxes trust" in out.getvalue(), out.getvalue()
        KEYS.write(m, rotation, key="stranger")
        why = check.signature(m, False, also=(found,), key_change=change)
        assert why and "This release changes the keys boxes trust" in why and stranger[1] in why, why
        # A release at the same keys changes nothing.
        same = {**_manifest(), "brain": {**_manifest()["brain"], "commit": old}}
        KEYS.write(m, same)
        assert check.release_key_change(m, found) == ("", None)
    finally:
        os.unlink(found[1])
    # A channel whose manifest at --base gives no keys is held to stable's (the keys boxes
    # have): the first beta answers to them, whether --base has no beta.json or its beta
    # names no commit.
    stable_old = pub({**_manifest(), "brain": {**_manifest()["brain"], "commit": old}})
    base_with({"manifest/stable.json": stable_old})
    assert check.boxes_signers_of("beta") == (None, None)   # nothing for beta itself
    found, why = check.boxes_signers("beta")
    assert why is None and found and "stable" in found[0] and old[:12] in found[0], (found, why)
    os.unlink(found[1])
    base_with({"manifest/stable.json": stable_old, "manifest/beta.json": pub({**_manifest("beta"), "brain": {"repo": "x/y"}})})
    assert check.boxes_signers_of("beta") == (None, None)   # beta names no commit
    found, why = check.boxes_signers("beta")
    assert found and "stable" in found[0], found
    os.unlink(found[1])
    # Nothing to hold it to: no manifest at --base, no commit named, a commit without a signers file.
    base_with({})
    assert check.boxes_signers("beta") == (None, None)
    assert check.boxes_signers("stable") == (None, None)
    base_with({"manifest/stable.json": pub(_manifest())})   # brain.commit absent
    assert check.boxes_signers("stable") == (None, None)
    pathlib.Path(repo, "README").write_text("x")
    _git(repo, "rm", "-q", "manifest/allowed_signers")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "no signers")
    bare = _git(repo, "rev-parse", "HEAD").strip()
    base_with({"manifest/stable.json": pub({**_manifest(), "brain": {**_manifest()["brain"], "commit": bare}})})
    assert check.boxes_signers("stable") == (None, None)
    # A commit that cannot be had is a problem, not a pass.
    cannot = pub({**_manifest(), "brain": {"repo": "not a repo (no fetch from here)", "commit": "d" * 40}})
    base_with({"manifest/stable.json": cannot})
    found, why = check.boxes_signers("stable")
    assert found is None and why and "cannot fetch" in why, why
    # The command line: --base and --boxes-signers, per channel.
    os.makedirs(signers.parent, exist_ok=True)
    signers.write_text(pathlib.Path(KEYS.signers).read_text())
    m_cli = os.path.join(tempdir("teaport-cli-m-"), "stable.json")
    KEYS.write(m_cli, _manifest())
    rc, out = _check_cli(check, KEYS.signers, m_cli, base=check.BASE[0])
    assert rc == 1 and "cannot fetch" in out, out
    # A --base that does not resolve fails the check: never taken as nothing published.
    rc, out = _check_cli(check, KEYS.signers, m_cli, base="e" * 40)
    assert rc == 1 and "does not resolve" in out, out
    wf = (ROOT / ".github/workflows/publish.yml").read_text()
    assert wf.count("--boxes-signers manifest/stable.json") == 2 and "--released-signers" not in wf, wf
    # Each of the workflow's check.py lines parses as meant: no option takes a file for its value.
    # ("${strict[@]}" is the weekly run's --require-enforce, or nothing.)
    lines = [[a for a in ln.split("python3 manifest/check.py", 1)[1].split() if a != '"${strict[@]}"']
             for ln in wf.splitlines() if "python3 manifest/check.py" in ln]
    assert len(lines) == 6, lines
    import yaml
    flow = yaml.safe_load(wf)
    jobs = flow["jobs"]
    # Read access only: the repository's contents and the publish runs' record. Nothing in
    # the workflow writes to the repository.
    assert flow["permissions"] == {"contents": "read"}, flow
    for job in ("artifacts", "publish"):
        assert jobs[job]["permissions"] == {"contents": "read", "actions": "read"}, (job, jobs[job])
        step_names = [st.get("name") for st in jobs[job]["steps"]]
        assert "The base this run is measured against" in step_names, (job, step_names)
        base_step = jobs[job]["steps"][step_names.index("The base this run is measured against")]
        assert "python3 manifest/last_upload.py base" in base_step["run"], base_step
    assert ": write" not in wf and "git push" not in wf and "Record what was uploaded" not in wf
    # The names the lookup finds the upload by (manifest/last_upload.py) are the workflow's.
    spec = importlib.util.spec_from_file_location("last_upload_wf", ROOT / "manifest/last_upload.py")
    lu = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lu)
    assert jobs["publish"]["name"] == lu.JOB and lu.WORKFLOW == "publish.yml", jobs["publish"].get("name")
    publish_steps = [st.get("name") for st in jobs["publish"]["steps"]]
    assert all(name in publish_steps for name in lu.UPLOAD_STEPS), (lu.UPLOAD_STEPS, publish_steps)
    dispatch = flow[True]["workflow_dispatch"]["inputs"]["base"]
    assert dispatch["default"] == "", dispatch
    # Only a PR into main, retargeted onto it included; the lookup refuses another base too.
    pr = flow[True]["pull_request"]
    assert pr["branches"] == ["main"] and pr["types"] == ["opened", "synchronize", "reopened", "edited"], pr
    for job in ("artifacts", "publish"):
        steps = {st.get("name"): st for st in jobs[job]["steps"]}
        base_step = steps["The base this run is measured against"]
        assert base_step["env"]["BASE_REF"] == "${{ github.base_ref }}", base_step
        assert '--base-ref "$BASE_REF"' in base_step["run"], base_step
    # Missing bucket credentials fail a step of their own, before the upload, so the upload
    # step is skipped (the lookup reads that run as one that uploaded nothing).
    steps = jobs["publish"]["steps"]
    creds = publish_steps.index("The bucket's credentials are set")
    upload = publish_steps.index(lu.UPLOAD_STEPS[0])
    assert creds == upload - 1, publish_steps
    assert steps[creds]["if"] == steps[upload]["if"], (steps[creds], steps[upload])
    assert '-z "$AWS_ACCESS_KEY_ID"' in steps[creds]["run"] and "exit 1" in steps[creds]["run"]
    assert "-z " not in steps[upload]["run"] and "exit 1" not in steps[upload]["run"], steps[upload]
    for args in lines:
        parsed = check.parser().parse_args(args)
        assert parsed.boxes_signers is True, args
        assert parsed.manifests or parsed.bootstrap, args
        assert parsed.base == '"$BASE"', args   # what this change is measured against
        positional = [a for i, a in enumerate(args) if not a.startswith("--") and args[i - 1] != "--base"]
        assert positional == parsed.manifests + ([parsed.bootstrap] if parsed.bootstrap else []), args


def _deb(path, files, compression="gz", control=None, reorder=None, fields=None, fmt=None):
    """A minimal .deb (ar: debian-binary, control.tar.gz, data.tar.<compression>). <files> is
    {"./path": bytes} or a list of (name, bytes) regular files, ("dir", name) directories,
    ("symlink"|"hardlink", name, target) links, and dicts of TarInfo fields (name, type, mode,
    uid, gid, uname, gname, linkname, body, pax), in that order in the tar; <control> adds
    {name: bytes} to the control tar (maintainer scripts); <fields> replaces the control
    file's text."""
    import io as _io
    import tarfile

    def tar(entries, mode, fmt_=tarfile.GNU_FORMAT):
        buf = _io.BytesIO()
        with tarfile.open(fileobj=buf, mode=mode, format=fmt_) as t:
            for entry in entries:
                if isinstance(entry, dict):
                    info = tarfile.TarInfo(entry["name"])
                    body = entry.get("body", b"")
                    for k, v in entry.items():
                        if k not in ("name", "body", "pax"):
                            setattr(info, k, v)
                    if "pax" in entry:
                        info.pax_headers = entry["pax"]
                    if info.type == tarfile.DIRTYPE and "mode" not in entry:
                        info.mode = 0o755
                    info.size = len(body) if info.type == tarfile.REGTYPE else 0
                    t.addfile(info, _io.BytesIO(body) if info.type == tarfile.REGTYPE else None)
                elif entry[0] in ("dir", "symlink", "hardlink"):
                    info = tarfile.TarInfo(entry[1])
                    info.type = {"dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE,
                                 "hardlink": tarfile.LNKTYPE}[entry[0]]
                    info.mode = 0o755 if entry[0] == "dir" else 0o644
                    if entry[0] != "dir":
                        info.linkname = entry[2]
                    t.addfile(info)
                else:
                    info = tarfile.TarInfo(entry[0])
                    info.size = len(entry[1])
                    t.addfile(info, _io.BytesIO(entry[1]))
        return buf.getvalue()
    items = list(files.items()) if isinstance(files, dict) else list(files)
    text = fields if fields is not None else (b"Package: teaport-brain\nVersion: 1.2.3\nArchitecture: arm64\n"
                                               b"Maintainer: x <x@x>\nDescription: x\n")
    ctrl = [("./control", text)] + list((control or {}).items())
    members = [("debian-binary", b"2.0\n"), ("control.tar.gz", tar(ctrl, "w:gz")),
               (f"data.tar.{compression}", tar(items, f"w:{compression}", fmt or tarfile.GNU_FORMAT))]
    if reorder:
        members = reorder(members)
    with open(path, "wb") as f:
        f.write(b"!<arch>\n")
        for name, data in members:
            f.write(f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode())
            f.write(data + (b"\n" if len(data) % 2 else b""))


def _bad_signers():
    """Signers files that ssh-keygen reads differently from a loose reader, or that name more
    than the two release keys: {name: bytes}. Each also has the real main key line."""
    good = {ln.split()[0]: ln for ln in pathlib.Path(KEYS.signers).read_text().splitlines()
            if ln.startswith("release-")}
    stranger = subprocess.run(["ssh-keygen", "-y", "-f", os.path.join(KEYS.dir, "stranger")],
                              capture_output=True, text=True, check=True).stdout.split()
    ns = 'namespaces="teaport-manifest,teaport-bootstrap"'
    s_key = f"{stranger[0]} {stranger[1]}"
    main = good["release-main"]
    return {
        # Python's splitlines() breaks on \x1c: a reader using it sees a comment, ssh-keygen
        # sees the principals "\x1c#x" and "*".
        "x1c": f"{main}\n\x1c#x,* {ns} {s_key}\n".encode(),
        "wildcard": f"{main}\nrelease-* {ns} {s_key}\n".encode(),
        "option": f'{main.replace(ns, ns + " valid-after=20200101")}\n'.encode(),
        "duplicate": f"{main}\nrelease-main {ns} {s_key}\n".encode(),
        "crlf": f"{main}\r\n".encode(),
        "tab": f"{main}\n\t# a comment\n".encode(),
        "other principal": f"{main}\nrelease-new {ns} {s_key}\n".encode(),
        "u2028": f"{main}\n\u2028{ns}\n".encode(),
        # Over the size limit: refused whole, never read in part (a key past the cut would
        # otherwise go unseen by the reader and be read by ssh-keygen).
        "oversize": f"{main}\n".encode() + b"#" * (1 << 20) + b"\n",
    }


def test_a_signers_file_read_any_other_way_than_strictly_is_refused_everywhere():
    good = pathlib.Path(KEYS.signers).read_bytes()
    assert len(manifest_sig.keys(good)) == 2
    body = _body()
    d = tempdir("teaport-bad-signers-")
    # ssh-keygen itself takes the \x1c line: a manifest signed by that key verifies with it.
    x1c = os.path.join(d, "x1c")
    pathlib.Path(x1c).write_bytes(_bad_signers()["x1c"])
    sig_file = os.path.join(d, "m.sig")
    pathlib.Path(sig_file).write_bytes(KEYS.sign(body, "stranger"))
    found = subprocess.run(["ssh-keygen", "-Y", "find-principals", "-s", sig_file, "-f", x1c],
                           capture_output=True, stdin=subprocess.DEVNULL)
    assert found.returncode == 0 and b"*" in found.stdout, found
    for name, data in _bad_signers().items():
        path = os.path.join(d, name.replace(" ", "-"))
        pathlib.Path(path).write_bytes(data)
        _refused(lambda: manifest_sig.keys(data), "signers file")
        # verify() fails closed, whatever key signed it.
        for key in ("main", "stranger"):
            _refused(lambda: manifest_sig.accept(body, KEYS.sign(body, key), "/m/stable.json", path),
                     "signers file")
        p = subprocess.run([sys.executable, "-I", str(SIG_PY), "signers", path], capture_output=True, text=True)
        assert p.returncode == 1 and "signers file" in p.stderr, (name, p.stderr)
        # The publish check refuses it outright.
        check = _check_py()
        check.SIGNERS = path
        saved_argv = sys.argv
        sys.argv = ["check.py", "--signature-only", os.path.join(d, "stable.json")]
        KEYS.write(os.path.join(d, "stable.json"), _manifest())
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                check.main()
        except SystemExit as e:
            assert e.code == 1 and "signers file" in out.getvalue(), (name, out.getvalue())
        else:
            raise AssertionError(name)
        finally:
            sys.argv = saved_argv
    # Only the two release principals, once each, in exactly one shape: the good file passes
    # the CLI with its count, which build-deb.sh's guard reads.
    p = subprocess.run([sys.executable, "-I", str(SIG_PY), "signers", KEYS.signers], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip() == "2", p


def test_the_publish_check_reads_the_keys_the_package_carries():
    # What boxes trust is the package's /usr/lib/teaport/allowed_signers, whatever the
    # repository's files say: the full check reads it out of brain.deb, strictly, and holds a
    # change in it to the boxes' keys.
    check = _check_py()
    d = tempdir("teaport-deb-keys-")
    boxes = os.path.join(d, "boxes")
    shutil.copy(KEYS.signers, boxes)
    deb = os.path.join(d, "teaport-brain.deb")
    member = "./usr/lib/teaport/allowed_signers"
    for compression in ("gz", "xz"):
        _deb(deb, {member: pathlib.Path(KEYS.signers).read_bytes()}, compression)
        assert check.deb_signers(deb) == pathlib.Path(KEYS.signers).read_bytes()
        assert check.package_keys(deb, ("the boxes", boxes), None) == ([], [])
    stranger = subprocess.run(["ssh-keygen", "-y", "-f", os.path.join(KEYS.dir, "stranger")],
                              capture_output=True, text=True, check=True).stdout.split()
    swapped = f'release-backup namespaces="teaport-manifest,teaport-bootstrap" {stranger[0]} {stranger[1]}'.encode()
    main_line = [ln for ln in pathlib.Path(KEYS.signers).read_bytes().split(b"\n") if ln.startswith(b"release-main")][0]
    _deb(deb, {member: main_line + b"\n" + swapped + b"\n"})
    problems, warnings = check.package_keys(deb, ("the boxes", boxes), "signature: not by a boxes key")
    assert problems and "carries other keys" in problems[0] and "+ release-backup" in problems[0], problems
    problems, warnings = check.package_keys(deb, ("the boxes", boxes), None)
    assert not problems and warnings and "changes the keys boxes trust" in warnings[0], (problems, warnings)
    for name, data in (("x1c", _bad_signers()["x1c"]), ("wildcard", _bad_signers()["wildcard"])):
        _deb(deb, {member: data})
        problems, _ = check.package_keys(deb, ("the boxes", boxes), None)
        assert problems and "is refused" in problems[0], (name, problems)
        problems, _ = check.package_keys(deb, None, None)   # first publish: still read strictly
        assert problems and "is refused" in problems[0], (name, problems)
    _deb(deb, {"./usr/bin/teaport": b"#!/bin/sh\n"})
    problems, _ = check.package_keys(deb, None, None)
    assert problems and "installs no" in problems[0], problems
    _deb(deb, {member: b"# no keys\n"})
    problems, _ = check.package_keys(deb, None, None)
    assert problems and "names no key" in problems[0], problems
    pathlib.Path(deb).write_bytes(b"not a deb")
    problems, _ = check.package_keys(deb, None, None)
    assert problems and "cannot be told" in problems[0], problems
    # The nfpm recipe puts the repository's file there; the check reads the package, not it.
    assert "dst: /usr/lib/teaport/allowed_signers" in (ROOT / "packaging/nfpm.yaml").read_text()
    assert check.DEB_SIGNERS == "usr/lib/teaport/allowed_signers"


def test_the_package_is_refused_when_dpkg_could_install_other_keys_than_the_check_reads():
    # dpkg installs every entry in order (the last one wins), strips a leading "/", and runs
    # maintainer scripts after unpacking: so one entry for the file, as a regular file under
    # real directories, and no script naming it. Anything else, refused.
    check = _check_py()
    d = tempdir("teaport-deb-dpkg-")
    deb = os.path.join(d, "b.deb")
    good, evil = pathlib.Path(KEYS.signers).read_bytes(), b"# other keys\n"
    dirs = [("dir", "./"), ("dir", "./usr/"), ("dir", "./usr/lib/"), ("dir", "./usr/lib/teaport/")]
    target = "./usr/lib/teaport/allowed_signers"
    _deb(deb, dirs + [(target, good), ("./usr/bin/teaport", b"#!/bin/sh\n")])
    assert check.deb_signers(deb) == good
    commit = ("manifest/allowed_signers at the commit", good)
    assert check.package_keys(deb, None, None, commit) == ([], [])
    cases = {
        "the same name twice": dirs + [(target, good), (target, evil)],
        "a leading /": dirs + [(target, good), ("/usr/lib/teaport/allowed_signers", evil)],
        "a /./": dirs + [(target, good), ("./usr/lib/teaport/./allowed_signers", evil)],
        "a //": dirs + [(target, good), ("./usr/lib//teaport/allowed_signers", evil)],
        "a parent symlink": [("dir", "./"), ("dir", "./usr/"), ("dir", "./usr/lib/"),
                             ("symlink", "./usr/lib/teaport", "/etc/elsewhere"), (target, good)],
        "a parent hard link": [("dir", "./"), ("dir", "./usr/"), ("hardlink", "./usr/lib", "./opt"), (target, good)],
        "the file a symlink": dirs + [("symlink", target, "/tmp/keys")],
        "the file a hard link": dirs + [("./usr/share/k", good), ("hardlink", target, "./usr/share/k")],
        "a .. path": dirs + [(target, good), ("./usr/../usr/lib/teaport/allowed_signers", evil)],
        "over the size limit": dirs + [(target, good + b"#" * (1 << 20))],
    }
    for name, entries in cases.items():
        _deb(deb, entries)
        try:
            check.deb_signers(deb)
        except ValueError as e:
            assert str(e), name
        else:
            raise AssertionError(f"{name}: taken")
        problems, _ = check.package_keys(deb, None, None, commit)
        assert problems and "cannot be told" in problems[0], (name, problems)
    # A maintainer script that names the file could write it after unpacking.
    _deb(deb, dirs + [(target, good)], control={"./postinst": b"#!/bin/sh\ncp /tmp/k /usr/lib/teaport/allowed_signers\n"})
    problems, _ = check.package_keys(deb, None, None, commit)
    assert problems and "maintainer script postinst" in problems[0], problems
    _deb(deb, dirs + [(target, good)], control={"./postinst": b"#!/bin/sh\nsystemctl daemon-reload\n"})
    assert check.package_keys(deb, None, None, commit) == ([], [])
    # The keys the package carries must be the ones the signer read at brain.commit; a
    # deliberate rotation has the new keys in both.
    _deb(deb, dirs + [(target, good)])
    problems, _ = check.package_keys(deb, None, None, ("manifest/allowed_signers at abc", evil))
    assert problems and "other keys than manifest/allowed_signers at abc" in problems[0], problems
    problems, _ = check.package_keys(deb, None, None, ("manifest/allowed_signers at abc (cannot fetch abc)", b""))
    assert problems and "cannot fetch" in problems[0], problems
    # The nfpm recipe ships the one file, and no maintainer script of the package names it.
    for script in ("postinst", "postrm"):
        assert "allowed_signers" not in (ROOT / "packaging/deb" / script).read_text(), script


def _key_line(principal, name):
    pub = subprocess.run(["ssh-keygen", "-y", "-f", os.path.join(KEYS.dir, name)],
                         capture_output=True, text=True, check=True).stdout.split()
    return f'{principal} namespaces="teaport-manifest,teaport-bootstrap" {pub[0]} {pub[1]}'


def _check_cli(check, signers, manifest, base):
    """manifest/check.py --signature-only --base <base> --boxes-signers <manifest>, with
    check.HERE and SIGNERS as <check> has them: (exit code, output)."""
    p = subprocess.run([sys.executable, "-c",
                        f"import sys,importlib.util as u;s=u.spec_from_file_location('c',{str(ROOT / 'manifest/check.py')!r});"
                        "c=u.module_from_spec(s);s.loader.exec_module(c);"
                        f"c.HERE={check.HERE!r};c.SIGNERS={signers!r};"
                        f"sys.argv=['check.py','--signature-only','--base',{base!r},'--boxes-signers',{manifest!r}];c.main()"],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


class _History:
    """A repository with three releases' worth of keys: a commit with none ("keyless",
    standing for brain-v0.1.0), one with main and backup ("old"), one where a push swapped
    the backup for the stranger's key ("pushed"), and the rotation ("new": the backup as
    release-main, the stranger's key as release-backup). The tree's signers file is apart."""

    def __init__(self):
        self.main, self.backup = _key_line("release-main", "main"), _key_line("release-backup", "backup")
        self.stranger_backup = _key_line("release-backup", "stranger")
        self.rotated = [self.backup.replace("release-backup", "release-main", 1), self.stranger_backup]
        self.repo = tempdir("teaport-history-")
        _git(self.repo, "init", "-q")
        pathlib.Path(self.repo, "README").write_text("before signing\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "keyless")
        self.keyless = self.head()
        os.makedirs(os.path.join(self.repo, "manifest"))
        self.commit_keys([self.main, self.backup], "old")
        self.old = self.head()
        self.commit_keys([self.main, self.stranger_backup], "pushed")
        self.pushed = self.head()
        self.commit_keys(self.rotated, "rotation")
        self.new = self.head()
        self.tree = os.path.join(tempdir("teaport-history-tree-"), "allowed_signers")

    def head(self):
        return _git(self.repo, "rev-parse", "HEAD").strip()

    def commit_keys(self, lines, message):
        pathlib.Path(self.repo, "manifest/allowed_signers").write_text("\n".join(lines) + "\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", message)

    def check(self, tree_lines):
        pathlib.Path(self.tree).write_text("\n".join(tree_lines) + "\n")
        check = _check_py()
        check.HERE = os.path.join(self.repo, "manifest")
        check.SIGNERS = self.tree
        return check

    def publish(self, commit, bootstrap_lines=None, stable=None, beta=None):
        """A commit holding what is published: manifest/stable.json naming <commit> (or the
        bytes <stable>), beta.json when given, and an install-bootstrap.sh carrying
        <bootstrap_lines> (none: a bootstrap from before keys). Its id: the --base."""
        files = {"manifest/stable.json": stable if stable is not None else json.dumps(
                     {**_manifest(), "brain": {**_manifest()["brain"], "commit": commit}}).encode(),
                 "install-bootstrap.sh": (b"#!/bin/bash\necho old bootstrap\n" if bootstrap_lines is None else
                                          ("#!/bin/bash\ntrusted_signers() {\n  cat <<'SIGNERS'\n"
                                           + "\n".join(bootstrap_lines) + "\nSIGNERS\n}\n").encode())}
        if beta is not None:
            files["manifest/beta.json"] = beta
        return commit_files(self.repo, self.new, files)

    def manifest(self, commit, key):
        m = os.path.join(tempdir("teaport-history-m-"), "stable.json")
        KEYS.write(m, {**_manifest(), "brain": {**_manifest()["brain"], "commit": commit}}, key=key)
        return m


def _signed_copy(path, key, name="stable.json"):
    """The bytes of the manifest at <path>, re-signed with <key>, in a new directory."""
    out = os.path.join(tempdir("teaport-resign-"), name)
    shutil.copy(path, out)
    pathlib.Path(out + ".sig").write_bytes(KEYS.sign(pathlib.Path(path).read_bytes(), key))
    return out


def _bootstrap_for(lines):
    """The real bootstrap carrying <lines> as its keys."""
    from test_install_bootstrap import _embedded
    return BOOTSTRAP_TEXT.replace(_embedded(BOOTSTRAP_TEXT), "\n".join(lines) + "\n")


def _bootstrap_check(check, lines, key):
    """check.bootstrap(--boxes-signers) of the bootstrap carrying <lines>, signed with <key>."""
    text = _bootstrap_for(lines)
    script = os.path.join(tempdir("teaport-flow-boot-"), "install-bootstrap.sh")
    pathlib.Path(script).write_text(text)
    pathlib.Path(script + ".sig").write_bytes(KEYS.sign(text.encode(), key, namespace="teaport-bootstrap"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ok = check.bootstrap(script, True)
    return ok, out.getvalue()


def test_every_release_carries_exactly_the_keys_of_the_tree_that_releases_it():
    # A routine release whose tagged commit had a key swapped in: the real signer's key
    # signs the manifest, the boxes' keys verify it, and it still fails, because the
    # commit's keys are not the tree's. The turn-on (no release with keys at --base) is
    # held the same way.
    h = _History()
    for published_keys in ("boxes", "none"):
        base = h.publish(h.old if published_keys == "boxes" else h.keyless, [h.main, h.backup])
        check = h.check([h.main, h.backup])
        rc, out = _check_cli(check, h.tree, h.manifest(h.pushed, "main"), base)
        assert rc == 1 and "is not this tree's manifest/allowed_signers" in out and h.stranger_backup.split()[-1] in out, (published_keys, out)
        rc, out = _check_cli(check, h.tree, h.manifest(h.old, "main"), base)
        assert rc == 0, (published_keys, out)


def test_the_documented_flows_pass_and_their_failures_fail():
    # Every flow RELEASING.md documents, with all trust state from git at --base.
    h = _History()
    keys = [h.main, h.backup]
    stable_before = json.dumps({**_manifest(), "brain": {**_manifest()["brain"], "tag": "main", "commit": "TODO"}}).encode()
    # The turn-on: the base (main before it, the last upload) names no release and has the old
    # bootstrap (no keys). Step 1 signs the bootstrap; step 2 the first release with keys.
    turnon_base = h.publish(None, None, stable=stable_before)
    check = h.check(keys)
    check.BASE[0] = turnon_base
    ok, out = _bootstrap_check(check, keys, "main")
    assert ok, ("turn-on bootstrap", out)
    turnon = h.manifest(h.old, "main")
    rc, out = _check_cli(check, h.tree, turnon, turnon_base)
    assert rc == 0, ("turn-on, the first release with keys", out)
    rc, out = _check_cli(check, h.tree, h.manifest(h.pushed, "main"), turnon_base)
    assert rc == 1 and "is not this tree's" in out, ("turn-on, a swapped key", out)
    rc, out = _check_cli(check, h.tree, h.manifest(h.old, "stranger"), turnon_base)
    assert rc == 1, ("turn-on, signed by a key the bootstrap does not carry", out)
    # A release without keys (brain-v0.1.0's kind) is never signed: boxes on it verify nothing.
    rc, out = _check_cli(check, h.tree, h.manifest(h.keyless, "main"), turnon_base)
    assert rc == 1 and "carries no keys" in out, ("a release without keys", out)
    # A merge right after the turn-on (its base the turn-on commit): it passes, as does the
    # weekly run on main as it is (the base is this commit: the manifest is the base's).
    after_turnon = h.publish(None, keys, stable=pathlib.Path(turnon).read_bytes())
    rc, out = _check_cli(check, h.tree, turnon, after_turnon)
    assert rc == 0, ("right after the turn-on", out)
    # A routine release: the base names the release boxes have (with keys).
    released = h.manifest(h.old, "main")
    routine_base = h.publish(None, keys, stable=pathlib.Path(released).read_bytes())
    rc, out = _check_cli(check, h.tree, h.manifest(h.old, "main"), routine_base)
    assert rc == 0, ("routine release", out)
    rc, out = _check_cli(check, h.tree, released, routine_base)
    assert rc == 0, ("weekly", out)
    # Beta's first release: --base has no beta.json, so beta answers to stable's keys.
    beta = os.path.join(tempdir("teaport-flow-beta-"), "beta.json")
    KEYS.write(beta, {**_manifest("beta"), "brain": {**_manifest("beta")["brain"], "commit": h.old}})
    rc, out = _check_cli(check, h.tree, beta, routine_base)
    assert rc == 0, ("first beta", out)
    KEYS.write(beta, {**_manifest("beta"), "brain": {**_manifest("beta")["brain"], "commit": h.old}}, key="stranger")
    rc, out = _check_cli(check, h.tree, beta, routine_base)
    assert rc == 1, ("first beta, a key boxes do not have", out)
    # The rotation PR: the tree has the new keys; it re-signs the published manifest (the
    # same bytes) and the bootstrap with the old backup. Measured against its base (main,
    # the old keys and that manifest).
    check = h.check(h.rotated)
    rc, out = _check_cli(check, h.tree, _signed_copy(released, "backup"), routine_base)
    assert rc == 0, ("rotation PR", out)
    check.BASE[0] = routine_base
    assert _bootstrap_check(check, h.rotated, "backup")[0], "rotation PR bootstrap"
    ok, out = _bootstrap_check(check, h.rotated, "stranger")
    assert not ok, ("rotation PR bootstrap, the new key alone", out)
    # Another manifest naming that release is no re-signature: its keys must be the tree's.
    other = os.path.join(tempdir("teaport-flow-other-"), "stable.json")
    KEYS.write(other, {**_manifest(), "version": "9.9.9", "brain": {**_manifest()["brain"], "commit": h.old}}, key="backup")
    rc, out = _check_cli(check, h.tree, other, routine_base)
    assert rc == 1 and "is not this tree's" in out, ("not a re-signature", out)
    # The rotation release, right after the rotation PR (its base the PR's merge: the new
    # keys in the tree and the bootstrap, stable still naming the old release).
    after_rotation = h.publish(None, h.rotated, stable=pathlib.Path(released).read_bytes())
    rc, out = _check_cli(check, h.tree, h.manifest(h.new, "backup"), after_rotation)
    assert rc == 0 and "changes the keys boxes trust" in out, ("rotation release", out)
    rc, out = _check_cli(check, h.tree, h.manifest(h.new, "stranger"), after_rotation)
    assert rc == 1 and "not with the published stable release" in out, ("rotation release, the new key alone", out)
    check.BASE[0] = after_rotation
    assert _bootstrap_check(check, h.rotated, "backup")[0], "the bootstrap right after the rotation"
    # Both keys lost: nothing signed only by the new keys passes against what boxes trust,
    # by design (RELEASING.md: the owner publishes that one pair by hand).
    lost = h.check([h.stranger_backup.replace("release-backup", "release-main", 1)])
    rc, out = _check_cli(lost, h.tree, h.manifest(h.old, "stranger"), routine_base)
    assert rc == 1, ("both keys lost", out)


def test_the_base_is_the_branchs_merge_base_and_must_resolve():
    # Run by hand on a branch (the signer's own check), --base defaults to where the branch
    # left main: what is published. Without origin/main, HEAD. A base that does not resolve
    # fails the check, never "nothing published".
    repo = tempdir("teaport-base-")
    _git(repo, "init", "-q")
    pathlib.Path(repo, "a").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a")
    a = _git(repo, "rev-parse", "HEAD").strip()
    check = _check_py()
    check.HERE = os.path.join(repo, "manifest")
    assert check.default_base() == "HEAD"
    _git(repo, "update-ref", "refs/remotes/origin/main", a)
    pathlib.Path(repo, "a").write_text("b\n")
    _git(repo, "commit", "-qam", "b")
    b = _git(repo, "rev-parse", "HEAD").strip()
    assert check.default_base() == a
    assert check.resolve_base("HEAD~1") == (a, None)
    found, why = check.resolve_base("no-such-ref")
    assert found is None and "does not resolve" in why, why
    # An explicitly empty --base (a lookup that failed in "$(...)") is refused, never taken
    # as the default.
    m = os.path.join(tempdir("teaport-base-m-"), "stable.json")
    KEYS.write(m, _manifest())
    for empty in ("", "  "):
        rc, out = _check_cli(check, KEYS.signers, m, empty)
        assert rc == 1 and "--base is empty" in out, (empty, out)
    # No `published` ref is consulted any more: CI names the last upload explicitly.
    _git(repo, "update-ref", "refs/remotes/origin/published", b)
    assert check.default_base() == a


def test_a_change_whose_run_was_refused_is_measured_against_the_last_upload():
    # A release signed only by the rotation's new key: refused against what was uploaded.
    # Its push run went red, but the commit is on main; measured against a later main that
    # already contains it, its own commit would give the boxes' keys and it would pass. So
    # --base is the last uploaded commit (the branch `published`), never main's tip.
    h = _History()
    check = h.check(h.rotated)
    uploaded = h.publish(None, [h.main, h.backup], stable=pathlib.Path(h.manifest(h.old, "main")).read_bytes())
    refused = h.manifest(h.new, "stranger")
    rc, out = _check_cli(check, h.tree, refused, uploaded)
    assert rc == 1 and "not with the published stable release" in out, out
    later_main = h.publish(None, h.rotated, stable=pathlib.Path(refused).read_bytes())
    rc, out = _check_cli(check, h.tree, refused, later_main)
    assert rc == 0, out   # what a base that is not the last upload lets through
    # The publish workflow measures against the last upload (manifest/last_upload.py).
    wf = (ROOT / ".github/workflows/publish.yml").read_text()
    assert "python3 manifest/last_upload.py base" in wf and "refs/heads/published" not in wf
    assert 'BASE: ${{ steps.base.outputs.base }}' in wf and "github.event.before || inputs.base" not in wf


def test_a_manifest_never_names_a_release_without_keys():
    # A release whose commit has no manifest/allowed_signers would install no keys: boxes on it
    # verify nothing. Refused whatever --base names, keys or none.
    h = _History()
    check = h.check([h.main, h.backup])
    for base in (h.publish(h.old, [h.main, h.backup]), h.publish(h.keyless, [h.main, h.backup])):
        rc, out = _check_cli(check, h.tree, h.manifest(h.keyless, "main"), base)
        assert rc == 1 and ("has no manifest/allowed_signers" in out or "carries no keys" in out), out


def test_the_package_is_refused_when_dpkg_could_unpack_other_owners_modes_links_or_fields():
    # What dpkg would make of the package beyond the keys file (the signing helper's rules,
    # which also hold each file outside the venv to the commit): every entry root's by
    # number and by name, no group- or other-write, directories 0755, no hard link, device,
    # fifo or pax header; outside the venv no link and no directory above no file; in the
    # venv only links that lead inside it, bin/python -> /usr/bin/python3.12 aside; only the
    # control members and fields a release has.
    import tarfile
    check = _check_py()
    d = tempdir("teaport-deb-layout-")
    deb = os.path.join(d, "b.deb")
    good = pathlib.Path(KEYS.signers).read_bytes()
    target = "./usr/lib/teaport/allowed_signers"
    dirs = [("dir", "./"), ("dir", "./usr/"), ("dir", "./usr/lib/"), ("dir", "./usr/lib/teaport/")]
    venv = [("dir", "./opt/"), ("dir", "./opt/teaport/"), ("dir", "./opt/teaport/brain/"),
            ("dir", "./opt/teaport/brain/bin/"), ("dir", "./opt/teaport/brain/lib/"),
            {"name": "./opt/teaport/brain/bin/python", "type": tarfile.SYMTYPE, "linkname": "/usr/bin/python3.12"},
            {"name": "./opt/teaport/brain/bin/python3", "type": tarfile.SYMTYPE, "linkname": "python"},
            {"name": "./opt/teaport/brain/lib64", "type": tarfile.SYMTYPE, "linkname": "lib"}]
    base = dirs + venv + [(target, good)]
    _deb(deb, base)
    assert check.deb_signers(deb) == good
    V = "./opt/teaport/brain"
    cases = {
        "a name that is not root's": base + [{"name": "./usr/lib/teaport/x", "uname": "ubuntu", "body": b"x"}],
        "a uid that is not root's": base + [{"name": "./usr/lib/teaport/x", "uid": 1000, "body": b"x"}],
        "a world-writable directory": dirs + venv + [{"name": "./usr/lib/x/", "type": tarfile.DIRTYPE, "mode": 0o777},
                                                     (target, good)],
        "a directory of mode 700": [("dir", "./"), {"name": "./usr/", "type": tarfile.DIRTYPE, "mode": 0o700}] + base[2:],
        "a directory above no file": base + [("dir", "./etc/sudoers.d/")],
        "a group-writable file": base + [{"name": V + "/w", "mode": 0o664, "body": b"x"}],
        "a setuid file": base + [{"name": V + "/s", "mode": 0o4755, "body": b"x"}],
        "a hard link": base + [("hardlink", V + "/shadow", "./etc/shadow")],
        "a character device": base + [{"name": V + "/mem", "type": tarfile.CHRTYPE, "devmajor": 1, "devminor": 1}],
        "a fifo": base + [{"name": V + "/fifo", "type": tarfile.FIFOTYPE}],
        "a link out of the venv": base + [("symlink", V + "/share", "/etc/sudoers.d")],
        "a relative link out of the venv": base + [("symlink", V + "/share", "../../../etc/sudoers.d")],
        "a link outside the venv": base + [("symlink", "./usr/lib/teaport/k", "allowed_signers")],
        "a file through a link": base + [(V + "/lib64/x.py", b"x")],
        "a directory twice": base + [("dir", "./usr/lib/")],
    }
    for name, entries in cases.items():
        _deb(deb, entries)
        try:
            check.deb_signers(deb)
        except ValueError as e:
            assert str(e), name
        else:
            raise AssertionError(f"{name}: taken")
    _deb(deb, base + [{"name": V + "/p", "body": b"x", "pax": {"comment": "x"}}], fmt=tarfile.PAX_FORMAT)
    try:
        check.deb_signers(deb)
    except ValueError as e:
        assert "not GNU tar's" in str(e), e
    else:
        raise AssertionError("pax: taken")
    fields = b"Package: teaport-brain\nVersion: 1.2.3\nArchitecture: arm64\nMaintainer: x\nDescription: x\n"
    for name, kw in (("Pre-Depends", {"fields": fields + b"Pre-Depends: evil\n"}),
                     ("Conflicts", {"fields": fields + b"Conflicts: sudo\n"}),
                     ("Essential", {"fields": fields + b"Essential: yes\n"}),
                     ("another package", {"fields": fields.replace(b"teaport-brain", b"sudo")}),
                     ("a triggers member", {"control": {"./triggers": b"interest /usr\n"}})):
        _deb(deb, base, **kw)
        try:
            check.deb_signers(deb)
        except ValueError as e:
            assert str(e), name
        else:
            raise AssertionError(f"{name}: taken")


def _ar_members(raw):
    pos, members = 8, []
    while pos < len(raw):
        h = raw[pos:pos + 60]
        size = int(h[48:58])
        members.append((h[:16].decode().strip().rstrip("/"), raw[pos + 60:pos + 60 + size]))
        pos += 60 + size + (size % 2)
    return members


def _ar(members):
    out = b"!<arch>\n"
    for name, body in members:
        out += (name.ljust(16) + "0".ljust(12) + "0".ljust(6) + "0".ljust(6) + "100644".ljust(8)
                + str(len(body)).ljust(10) + "`\n").encode() + body + (b"\n" if len(body) % 2 else b"")
    return out


def _tar_entry(name, data=b"", mode=0o644, typ=None, encoding="utf-8"):
    import tarfile
    i = tarfile.TarInfo(name)
    i.type = typ or tarfile.REGTYPE
    i.mode, i.uname, i.gname = mode, "root", "root"
    i.size = len(data) if i.type == tarfile.REGTYPE else 0
    b = i.tobuf(format=tarfile.GNU_FORMAT, encoding=encoding) + data
    return b + b"\0" * (-len(b) % 512)


def _rechecksum(h):
    h[148:156] = b" " * 8
    h[148:156] = b"%06o\0 " % sum(h)
    return h


def _tar_body_end(t):
    """The offset of t's first end-of-archive block (its entries end there)."""
    off = 0
    while t[off:off + 512] != b"\0" * 512:
        size = int(t[off + 124:off + 135], 8)
        off += 512 + (size + 511) // 512 * 512
    return off


def _polyglot(raw, which):
    """The member named as an uncompressed tar (what dpkg reads raw) whose bytes are also a
    gzip stream (FEXTRA) of the original tar: a reader that sniffs gzip sees the original."""
    import struct
    import tarfile
    import zlib
    members = _ar_members(raw)
    idx = 1 if which == "control" else 2
    name, body = members[idx]
    clean = zlib.decompress(body, 31)
    evil = (_tar_entry("./postinst", b"#!/bin/sh\nevil\n", 0o755) if which == "control" else
            _tar_entry("./etc/", typ=tarfile.DIRTYPE, mode=0o755) + _tar_entry("./etc/sudoers.d/", typ=tarfile.DIRTYPE,
            mode=0o755) + _tar_entry("./etc/sudoers.d/zz", b"ALL ALL=(ALL) NOPASSWD: ALL\n", 0o440))
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    gzbody = co.compress(clean) + co.flush() + struct.pack("<II", zlib.crc32(clean), len(clean) & 0xFFFFFFFF)
    a = bytearray(_tar_entry("\x1f\x8b\x08\x04", encoding="iso8859-1")[:512])
    zz = tarfile.TarInfo("./zz")
    zz.size, zz.uname, zz.gname = len(gzbody), "root", "root"
    zzhdr = zz.tobuf(format=tarfile.GNU_FORMAT)
    a[4:12] = b"\0" * 6 + struct.pack("<H", len(a) + len(evil) + len(zzhdr) - 12)
    rawtar = bytes(_rechecksum(a)) + evil + zzhdr + gzbody
    rawtar += b"\0" * (-len(rawtar) % 512) + b"\0" * 1024
    assert zlib.decompress(rawtar, 31) == clean
    members[idx] = (name[:-3], rawtar)
    return _ar(members)


def _badfield(raw, which):
    """Entries appended after one whose devmajor is not octal (a regular file: dpkg never reads
    the field); a reader that stops at a bad header sees only the original entries. "areg": a
    directory of type "\\0" with size 512, whose data block dpkg skips and another reader does not."""
    import gzip
    import tarfile
    members = _ar_members(raw)
    idx = 1 if which == "control" else 2
    name, body = members[idx]
    t = gzip.decompress(body)
    t = t[:_tar_body_end(t)]
    bad = bytearray(_tar_entry("./zz")[:512])
    bad[329:337] = b"zzzzzzz\0"
    bad = bytes(_rechecksum(bad))
    if which == "control":
        extra = bad + _tar_entry("./postinst", b"#!/bin/sh\nevil\n", 0o755)
    elif which == "areg":
        evil = _tar_entry("./etc/", typ=tarfile.DIRTYPE, mode=0o755) + _tar_entry(
            "./etc/sudoers.d/zz", b"ALL ALL=(ALL) NOPASSWD: ALL\n", 0o440)
        d = tarfile.TarInfo("./opt/teaport/brain/x/")
        d.mode, d.uname, d.gname, d.size = 0o755, "root", "root", 512
        dh = bytearray(d.tobuf(format=tarfile.GNU_FORMAT))
        dh[156:157] = b"\0"
        bi = tarfile.TarInfo("./opt/teaport/brain/blob")
        bi.uname, bi.gname, bi.size = "root", "root", len(evil)
        extra = bytes(_rechecksum(dh)) + bi.tobuf(format=tarfile.GNU_FORMAT) + evil
    else:
        extra = bad + _tar_entry("./etc/sudoers.d/zz", b"ALL ALL=(ALL) NOPASSWD: ALL\n", 0o440)
    t = t + extra + b"\0" * 1024
    members[idx] = (name, gzip.compress(t + b"\0" * (-len(t) % 10240), 1))
    return _ar(members)


def _linkname_on_file():
    h = bytearray(_tar_entry("./usr/lib/teaport/x")[:512])
    h[157:158] = b"y"
    return bytes(_rechecksum(h))


def test_a_package_read_one_way_here_and_another_by_dpkg_is_refused():
    # The check reads the tar dpkg installs, and only one way (review 4 of the signing
    # redesign): a member compressed as its name says, never sniffed; a strict header walk with
    # no stop at a bad header; no type \0 entry with data. Each of these showed the check
    # the original tar while dpkg installed another.
    import tarfile
    check = _check_py()
    d = tempdir("teaport-deb-differential-")
    deb = os.path.join(d, "b.deb")
    good = pathlib.Path(KEYS.signers).read_bytes()
    target = "./usr/lib/teaport/allowed_signers"
    base = [("dir", "./"), ("dir", "./usr/"), ("dir", "./usr/lib/"), ("dir", "./usr/lib/teaport/"), (target, good),
            ("dir", "./opt/"), ("dir", "./opt/teaport/"), ("dir", "./opt/teaport/brain/")]
    _deb(deb, base, control={"./postinst": b"#!/bin/sh\nexit 0\n"})
    raw = pathlib.Path(deb).read_bytes()
    assert check.deb_signers(deb) == good
    for name, made in (("a control.tar that is also gzip", _polyglot(raw, "control")),
                       ("a data.tar that is also gzip", _polyglot(raw, "data")),
                       ("a non-octal devmajor in the control tar", _badfield(raw, "control")),
                       ("a non-octal devmajor in the data tar", _badfield(raw, "data")),
                       ("a type \\0 directory with data", _badfield(raw, "areg"))):
        pathlib.Path(deb).write_bytes(made)
        try:
            check.deb_signers(deb)
        except ValueError as e:
            assert str(e), name
        else:
            raise AssertionError(f"{name}: taken")
    # More of the one dialect: a header whose checksum is wrong, a size on a directory, a link
    # name on a file, trailing bytes after the end-of-archive blocks, a gzip stream with bytes after it.
    members = _ar_members(raw)
    import gzip
    t = gzip.decompress(members[2][1])
    end = _tar_body_end(t)
    first = bytearray(t[:512])
    variants = {
        "a wrong checksum": bytes(first[:148]) + b"000000\0 " + bytes(first[156:]) + t[512:],
        "a directory with a size": bytes(_rechecksum(bytearray(first[:124]) + b"00000001000\0" + first[136:])) + t[512:],
        "bytes after the end": t + b"x",
        "a link name on a file": t[:end] + _linkname_on_file() + t[end:],
    }
    for name, stream in variants.items():
        pathlib.Path(deb).write_bytes(_ar([members[0], members[1], ("data.tar.gz", gzip.compress(stream))]))
        try:
            check.deb_signers(deb)
        except ValueError as e:
            assert str(e), name
        else:
            raise AssertionError(f"{name}: taken")
    pathlib.Path(deb).write_bytes(_ar([members[0], members[1], ("data.tar.gz", members[2][1] + b"\0\0\0\0")]))
    try:
        check.deb_signers(deb)
    except ValueError as e:
        assert "one gz stream" in str(e), e
    else:
        raise AssertionError("bytes after the gzip stream: taken")


def test_a_package_dpkg_would_read_otherwise_by_its_members_is_refused():
    # dpkg reads debian-binary, then the first control.tar*, then the first data.tar*: a
    # package with any other ar members is refused, not read some other way.
    check = _check_py()
    d = tempdir("teaport-deb-ar-")
    deb = os.path.join(d, "b.deb")
    files = {"./usr/lib/teaport/allowed_signers": pathlib.Path(KEYS.signers).read_bytes()}
    _deb(deb, files)
    assert check.deb_signers(deb) == files["./usr/lib/teaport/allowed_signers"]
    for name, reorder in (("two data members", lambda m: m + [("data.tar.gz", m[2][1])]),
                          ("data before control", lambda m: [m[0], m[2], m[1]]),
                          ("an extra member", lambda m: m + [("_gpgorigin", b"x")]),
                          ("no debian-binary", lambda m: m[1:])):
        _deb(deb, files, reorder=reorder)
        try:
            check.deb_signers(deb)
        except ValueError as e:
            assert "ar members" in str(e), (name, e)
        else:
            raise AssertionError(name)


def test_a_release_without_keys_is_refused():
    # No signers file at its commit, none in its package: refused, brain-v0.1.0 included (no
    # manifest may name it: it predates signing, and the license gate). Only one of the two
    # having it is a mismatch; anything with a signers file keeps every rule.
    check = _check_py()
    d = tempdir("teaport-deb-keyless-")
    deb = os.path.join(d, "b.deb")
    good = pathlib.Path(KEYS.signers).read_bytes()
    v010 = ("manifest/allowed_signers at 38f7fac2558d", None, "38f7fac2558d1e29bd8faca66a2ff316c8e78771")
    rc1 = ("manifest/allowed_signers at 6475bfaaf01a", None, "6475bfaaf01ac4d259f2616377a7561146476af8")
    _deb(deb, {"./usr/bin/teaport": b"#!/bin/sh\n"})
    assert not hasattr(check, "PRE_SIGNING_RELEASES")
    for keyless in (v010, rc1):
        problems, warnings = check.package_keys(deb, None, None, keyless)
        assert problems and "boxes verifying nothing" in problems[0] and not warnings, (problems, warnings)
    problems, _ = check.package_keys(deb, None, None, ("manifest/allowed_signers at abc", good))
    assert problems and "installs no" in problems[0], problems
    _deb(deb, {"./usr/lib/teaport/allowed_signers": good})
    problems, _ = check.package_keys(deb, None, None, v010)
    assert problems and "has none" in problems[0], problems
    # The signature-only run before upload refuses such a release too, from its commit alone
    # (commit_signers faked: a shallow checkout has neither).
    check.SIGNERS = KEYS.signers
    check.commit_signers = lambda commit, repo: (None, None)
    m = os.path.join(d, "stable.json")
    for keyless in (v010, rc1):
        KEYS.write(m, {**_manifest(), "brain": {**_manifest()["brain"], "commit": keyless[2]}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert not check.check(m, False, signature_only=True), out.getvalue()
        assert "the release carries no keys" in out.getvalue(), out.getvalue()


def test_a_sha256_that_is_not_64_hex_characters_never_reaches_sha256sum():
    d = tempdir("teaport-sha-")
    any_file = os.path.join(d, "anything")
    pathlib.Path(any_file).write_bytes(b"whatever bytes")
    empty = __import__("hashlib").sha256(b"").hexdigest()
    forged = f"{empty}  /dev/null\nx"
    # Why: sha256sum -c takes the value as two lines, and passes on the one that checks out.
    p = subprocess.run(["bash", "-c", f'printf "%s  %s\\n" "$1" "$2" | sha256sum -c --status', "_", forged, any_file],
                       capture_output=True, text=True)
    assert p.returncode == 0, p
    script = ('log() { printf "LOG %s\\n" "$*"; }\nwarn() { printf "WARN %s\\n" "$*"; }\n'
              'die() { printf "DIE %s\\n" "$*"; exit 1; }\nSUDO() { "$@"; }\n'
              'curl() { printf "CURL %s\\n" "$*"; return 0; }\n' + _fn("sha256_ok") + _fn("download"))
    for bad in (forged, empty.upper(), empty[:-1], empty + "0", "TODOx"):
        p = subprocess.run(["bash", "-c", script + 'DRY_RUN=0\ndownload https://h/f "$1" "$2"\n', "_",
                            os.path.join(d, "f"), bad], capture_output=True, text=True)
        assert p.returncode == 1 and "not 64 lowercase hex" in p.stdout and "CURL" not in p.stdout, (bad, p.stdout)
    # The engine license: its sha256 is required like every other, and checked the same way.
    mf = os.path.join(d, "m.json")
    gate = ('log() { :; }\nwarn() { :; }\ndie() { printf "DIE %s\\n" "$*"; exit 1; }\n'
            'curl() { printf "CURL %s\\n" "$*"; return 0; }\n' + _fn("sha256_ok") + _fn("mget") + _fn("eula_gate")
            + f'MF="{mf}" STATE="{d}/state" DRY_RUN=0 ACCEPT_EULA=1\neula_gate\n')
    for eula, want in (({"version": "1.0", "url": "https://h/eula.txt"}, "no sha256 for the engine license"),
                       ({"version": "1.0", "url": "https://h/eula.txt", "sha256": "TODO"}, "no sha256 for the engine license"),
                       ({"version": "1.0", "url": "https://h/eula.txt", "sha256": forged}, "not 64 lowercase hex")):
        pathlib.Path(mf).write_text(json.dumps({"engine": {"eula": eula}}))
        p = subprocess.run(["bash", "-c", gate], capture_output=True, text=True)
        assert p.returncode == 1 and want in p.stdout and "CURL" not in p.stdout, (eula, p.stdout)
    # A signed manifest with such a value is refused before anything reads it: by the check
    # every box runs (update_apply.py, install.sh, the brain), and by the publish check.
    bad_manifest = {**_manifest(), "models": {"voxtral": {"url": "https://h/v", "sha256": forged,
                                                          "aux": {"t.json": "https://h/t"},
                                                          "aux_sha256": {"t.json": "zz"}}}}
    body = json.dumps(bad_manifest).encode()
    try:
        manifest_sig.accept(body, KEYS.sign(body), "/m/stable.json", KEYS.signers)
    except ValueError as e:
        assert "models.voxtral.sha256" in str(e) and "models.voxtral.aux_sha256.t.json" in str(e), e
    else:
        raise AssertionError("a forged sha256 was taken")
    assert manifest_sig.bad_hashes(json.loads((ROOT / "manifest/stable.json").read_text())) == []
    assert manifest_sig.bad_hashes(json.loads((ROOT / "manifest/beta.json").read_text())) == []
    mod = _apply(tempdir("teaport-apply-sha-"))
    url = "https://get.teaspoon.tech/manifest/stable.json"
    fetch, _ = _served({url: body, url + ".sig": KEYS.sign(body)})
    _refused(lambda: mod.read_manifest("stable", os.path.join(d, "u.json"), fetch=fetch), "not 64 lowercase hex")


def test_a_bootstrap_with_another_key_block_is_refused():
    # bash runs the last definition of trusted_signers; a reader might take the first.
    from test_install_bootstrap import KEYS as BOOT_KEYS, Release   # the keys its bootstrap carries
    r = Release()
    check = _check_py()
    check.SIGNERS = BOOT_KEYS.signers
    text = pathlib.Path(r.script).read_text()
    assert check.embedded_signers(text) == pathlib.Path(BOOT_KEYS.signers).read_text()
    for extra in ("\ntrusted_signers() {\n  echo 'release-main x'\n}\n",
                  "\nfunction trusted_signers {\n  cat <<'SIGNERS'\nx\nSIGNERS\n}\n",
                  "\ncat <<SIGNERS\nx\nSIGNERS\n"):
        script = os.path.join(r.dir, "two-blocks")
        pathlib.Path(script).write_text(text.replace("\nneed() {", extra + "\nneed() {", 1))
        assert check.embedded_signers(pathlib.Path(script).read_text()) is None, extra
        pathlib.Path(script + ".sig").write_bytes(BOOT_KEYS.sign(pathlib.Path(script).read_bytes(),
                                                                 namespace="teaport-bootstrap"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert not check.bootstrap(script)
        assert "not exactly one trusted_signers function" in out.getvalue(), out.getvalue()


def test_the_bootstrap_refuses_keys_it_would_read_another_way_than_ssh_keygen():
    from test_install_bootstrap import Release, _embedded
    r = Release()
    r.manifest()
    text = pathlib.Path(r.script).read_text(encoding="utf-8", errors="surrogateescape")
    for name, data in _bad_signers().items():
        script = os.path.join(r.dir, f"bad-{name.replace(' ', '-')}")
        pathlib.Path(script).write_bytes(text.replace(_embedded(text), "").encode("utf-8", "surrogateescape")
                                         .replace(b"cat <<'SIGNERS'\n", b"cat <<'SIGNERS'\n" + data, 1))
        rc, out, git = r.run(script=script)
        assert rc == 1 and "malformed" in out and "INSTALL ran" not in out and git == "", (name, out)


def test_the_publish_check_wants_every_artifacts_sha256_and_the_installers_commit():
    check = _check_py()
    body = b"x"

    deb_signers = [pathlib.Path(KEYS.signers).read_bytes()]
    # manifest/allowed_signers at brain.commit, as the repository has it (no fetch from here).
    commit_files = [pathlib.Path(KEYS.signers).read_bytes()]
    check.commit_signers = lambda commit, repo: (commit_files[0], None)
    deb_version = ["9.9.9"]     # the control file's Version of the package fetch() serves

    def fetch(url, digest, token=None, dest=None):
        if dest:   # the brain package: one that installs deb_signers[0] as its keys
            _deb(dest, {"./usr/lib/teaport/allowed_signers": deb_signers[0]},
                 fields=(f"Package: teaport-brain\nVersion: {deb_version[0]}\nArchitecture: arm64\n"
                         "Maintainer: x <x@x>\nDescription: x\n").encode())
        if digest is None:
            return None
        d = digest.copy()
        d.update(body)
        return d
    check.fetch = fetch
    sha = __import__("hashlib").sha256(body).hexdigest()
    asked = []

    real_run = subprocess.run

    def ls_remote(tags):
        # Only git ls-remote is faked (check.subprocess is the subprocess module itself: the
        # signature check's ssh-keygen goes through it too).
        def run(cmd, **k):
            if cmd[:2] != ["git", "ls-remote"]:
                return real_run(cmd, **k)
            asked.append(cmd)
            out = "".join(f"{v}\t{r}\n" for r, v in tags.items())
            return subprocess.CompletedProcess(cmd, 0 if tags else 2, out, "")
        return run
    commit = "c0ffee" + "0" * 34
    art = {"url": "https://get.teaspoon.tech/dl/x", "sha256": sha}
    manifest = {"channel": "stable", "openclaw": {"version": "2026.9.7", "node": "24"},
                "brain": {"repo": "Teaspoon-AI/teaport", "tag": "brain-v9.9.9", "commit": commit,
                          "deb": {**art, "url": "https://get.teaspoon.tech/dl/brain/teaport-brain_9.9.9_arm64.deb"}},
                "engine": {"release": "1.6", "eula": art, "assets": {"l4t38-cu13": art}, "g2p_dict": art,
                           "pos_model": art, "notices": art},
                "models": {"kokoro": art, "voxtral": {**art, "aux": {"params.json": "https://get.teaspoon.tech/dl/p"},
                                                      "aux_sha256": {"params.json": sha}}}}
    d = tempdir("teaport-check-")
    m = os.path.join(d, "stable.json")

    def run(manifest, tags):
        KEYS.write(m, manifest)
        check.SIGNERS = KEYS.signers
        out = io.StringIO()
        check.subprocess.run = ls_remote(tags)
        try:
            with contextlib.redirect_stdout(out):
                ok = check.check(m, False)
        finally:
            check.subprocess.run = real_run
        return ok, out.getvalue()
    try:
        ok, out = run(manifest, {"refs/tags/brain-v9.9.9": "aaaa", "refs/tags/brain-v9.9.9^{}": commit})
        assert ok and "models.voxtral.aux.params.json" in out and f"= {commit}" in out, out
        # The package's keys are not the ones at the commit the manifest names: refused.
        commit_files[0] = b"# no keys at this commit\n"
        ok, out = run(manifest, {"refs/tags/brain-v9.9.9^{}": commit})
        assert not ok and "brain.deb carries other keys than manifest/allowed_signers at c0ffee" in out, out
        commit_files[0] = pathlib.Path(KEYS.signers).read_bytes()
        # The tag moved off the commit; the tag is a branch; no commit at all.
        ok, out = run(manifest, {"refs/tags/brain-v9.9.9": "beef" + "0" * 36})
        assert not ok and "not brain.commit" in out, out
        ok, out = run(manifest, {"refs/heads/brain-v9.9.9": commit})
        assert not ok and "is not a tag" in out, out
        nocommit = json.loads(json.dumps(manifest))
        del nocommit["brain"]["commit"]
        ok, out = run(nocommit, {"refs/tags/brain-v9.9.9": commit})
        assert not ok and "brain.commit: missing or not a full commit id" in out, out
        # Another repository: the only one a release names is the release repository.
        other = json.loads(json.dumps(manifest))
        other["brain"]["repo"] = "evil/teaport"
        ok, out = run(other, {"refs/tags/brain-v9.9.9^{}": commit})
        assert not ok and "brain.repo: 'evil/teaport', not Teaspoon-AI/teaport" in out, out
        # The control file's Version: the file name's and the tag's, each way (a final tag with
        # a candidate's package, a candidate's tag with the final one).
        for control_version, said in (("9.9.9~rc1", "Version '9.9.9~rc1' is not its file name's '9.9.9'"),
                                      ("9.9.8", "is not '9.9.9', the version brain-v9.9.9 builds")):
            deb_version[0] = control_version
            ok, out = run(manifest, {"refs/tags/brain-v9.9.9^{}": commit})
            assert not ok and said in out, (control_version, out)
        rc_beta = json.loads(json.dumps(manifest))
        rc_beta["channel"] = "beta"
        rc_beta["brain"]["tag"] = "brain-v9.9.9-rc1"
        rc_beta["brain"]["deb"]["url"] = "https://get.teaspoon.tech/dl/brain/teaport-brain_9.9.9~rc1_arm64.deb"
        mb = os.path.join(d, "beta.json")
        for control_version, ok_ in (("9.9.9~rc1", True), ("9.9.9", False)):
            deb_version[0] = control_version
            KEYS.write(mb, rc_beta)
            out = io.StringIO()
            check.subprocess.run = ls_remote({"refs/tags/brain-v9.9.9-rc1^{}": commit})
            try:
                with contextlib.redirect_stdout(out):
                    got = check.check(mb, False)
            finally:
                check.subprocess.run = real_run
            assert got == ok_ and (ok_ or "is not '9.9.9~rc1', the version brain-v9.9.9-rc1 builds" in out.getvalue()), \
                (control_version, out.getvalue())
        deb_version[0] = "9.9.9"
        # An aux file without its sha256.
        noaux = json.loads(json.dumps(manifest))
        del noaux["models"]["voxtral"]["aux_sha256"]
        ok, out = run(noaux, {"refs/tags/brain-v9.9.9^{}": commit})
        assert not ok and "models.voxtral.aux.params.json: no sha256" in out, out
    finally:
        subprocess.run = real_run
    # The shipped manifests: every aux file has its sha256 (or both are placeholders), and the
    # brain names a commit (or a placeholder for one).
    for name in ("stable.json", "beta.json"):
        shipped = json.loads((ROOT / "manifest" / name).read_text())
        vox = shipped["models"]["voxtral"]
        assert set(vox["aux"]) == set(vox["aux_sha256"]), name
        assert "commit" in shipped["brain"], name


def test_install_sh_installs_no_aux_file_without_its_sha256():
    text = INSTALL.read_text()
    start = text.index("  local aux u sha\n")
    loop = text[start:text.index("  done\n", start) + len("  done\n")]
    d = tempdir("teaport-aux-")
    mf = os.path.join(d, "m.json")

    def run(voxtral, dry=0):
        pathlib.Path(mf).write_text(json.dumps({"models": {"voxtral": voxtral}}))
        script = ('download() { printf "DOWNLOAD %s %s %s\\n" "$1" "$(basename "$2")" "$3"; }\n'
                  'warn() { printf "WARN %s\\n" "$*"; }\ndie() { printf "DIE %s\\n" "$*"; exit 1; }\n'
                  + f'MF="{mf}" PREFIX="{d}" DRY_RUN={dry}\n' + _fn("voxtral_aux")
                  + "f() {\n" + loop + "}\nf\n")
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr
    rc, out = run({"aux": {"tekken.json": "https://h/t", "params.json": "https://h/p"},
                   "aux_sha256": {"tekken.json": "aa", "params.json": "bb"}})
    assert rc == 0 and "DOWNLOAD https://h/t tekken.json aa" in out and "DOWNLOAD https://h/p params.json bb" in out, out
    rc, out = run({"aux": {"tekken.json": "https://h/t"}})
    assert rc == 1 and "DIE manifest has no sha256 for models.voxtral.aux tekken.json" in out and "DOWNLOAD" not in out, out
    rc, out = run({"aux": {"tekken.json": "https://h/t"}}, dry=1)
    assert rc == 0 and "WARN" in out and "DOWNLOAD" not in out, out
    rc, out = run({"aux": {"tekken.json": "TODO"}})   # not released: nothing to fetch
    assert rc == 0 and "DOWNLOAD" not in out, out
    # The bridge's clone is held to the same commit, when the manifest names one: the tag by
    # its full ref, every object checked, the commit checked out by its id.
    assert 'die "github.com/$repo at $tag is $head, not the commit the release manifest names' in text
    assert ('SUDO git -C "$work" -c transfer.fsckObjects=true fetch --quiet --depth 1 --no-tags \\\n'
            '      "https://github.com/$repo.git" "+refs/tags/$tag:refs/tags/$tag"') in text
    assert 'checkout --quiet "$commit"' in text
    # Only the release repository's bridge source is fetched.
    assert 'die "the release manifest names $repo, not Teaspoon-AI/teaport: the Discord bridge' in text
    # A manifest with no commit names nothing to fetch: no unpinned clone of the tag.
    assert "git clone" not in text and "the release manifest names no brain.commit, so the Discord bridge" in text


def test_publish_uploads_the_signature_before_the_manifest_and_ci_cannot_sign():
    wf = (ROOT / ".github/workflows/publish.yml").read_text()
    up = wf[wf.index("Upload to the download bucket"):]
    assert up.index('put "manifest/$ch.json.sig"') < up.index('put "manifest/$ch.json" "manifest/$ch.json"'), up
    assert "check.py --signature-only --base \"$BASE\" --boxes-signers manifest/stable.json" in wf
    assert "manifest/stable.json.sig manifest/beta.json.sig" in wf   # the tip check covers them
    assert "-Y sign" not in wf and "-Y sign" not in (ROOT / ".github/workflows/ci.yml").read_text()


# --- the brain's look ---------------------------------------------------------------------

def test_the_brains_look_shows_a_manifest_that_does_not_verify_as_an_error():
    from teaport_brain import updates
    saved = manifest_sig.SIGNERS
    manifest_sig.SIGNERS = KEYS.signers
    d = tempdir("teaport-look-")
    m = os.path.join(d, "stable.json")
    try:
        KEYS.write(m, _manifest())
        assert updates.load(m, env={}) == _manifest()
        KEYS.write(m, _manifest(), key="stranger")
        _refused(lambda: updates.load(m, env={}), "not signed by a trusted key")
        os.unlink(m + ".sig")
        _refused(lambda: updates.load(m, env={}), "no signature")
        assert updates.load(m, env={"TEAPORT_MANIFEST_UNSIGNED": "1"}) == _manifest()
        # Through the CLI's path: an error, not an offer.
        out, err = io.StringIO(), io.StringIO()
        saved_env = os.environ.get("TEAPORT_MANIFEST_URL")
        os.environ["TEAPORT_MANIFEST_URL"] = m
        pathlib.Path(d, "manifest.json").write_text(json.dumps(_manifest(brain="0.1.0")))
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = updates.main(["check", "--prefix", d, "--etc", d])
        finally:
            if saved_env is None:
                os.environ.pop("TEAPORT_MANIFEST_URL", None)
            else:
                os.environ["TEAPORT_MANIFEST_URL"] = saved_env
        assert rc == 1 and "cannot read the release manifest" in err.getvalue() and "no signature" in err.getvalue(), err.getvalue()
    finally:
        manifest_sig.SIGNERS = saved


# --- the package --------------------------------------------------------------------------

def test_the_build_refuses_a_setup_py_or_setup_cfg():
    # Either would run code (or steer the build backend) during uv sync, outside the signer's
    # reading of the package: build-deb.sh builds from brain/pyproject.toml alone.
    build = (ROOT / "packaging/build-deb.sh").read_text()
    start = build.index("for f in brain/setup.py brain/setup.cfg; do")
    guard = build[start:build.index("done", start) + 4]
    for name in ("setup.py", "setup.cfg", None):
        tmp = tempdir("teaport-setup-guard-")
        os.makedirs(os.path.join(tmp, "brain"))
        if name:
            pathlib.Path(tmp, "brain", name).write_text("")
        p = subprocess.run(["bash", "-c", f'set -euo pipefail\nREPO="{tmp}"\ndie() {{ echo "DIE $*"; exit 1; }}\n'
                            + guard + "\necho PASSED\n"], capture_output=True, text=True)
        assert ("DIE" in p.stdout and name in p.stdout) if name else "PASSED" in p.stdout, (name, p.stdout)


def test_a_release_build_refuses_the_placeholder_keys_and_the_package_ships_the_check():
    build = (ROOT / "packaging/build-deb.sh").read_text()
    start = build.index('  signers="$REPO/manifest/allowed_signers"')
    end = build.index("into its trusted_signers block\"", start)
    guard = build[start:build.index("\n", end)]
    tmp = tempdir("teaport-guard-")
    os.makedirs(os.path.join(tmp, "manifest"))
    os.makedirs(os.path.join(tmp, "brain", "teaport_brain"))   # the strict reader the guard runs
    shutil.copy(SIG_PY, os.path.join(tmp, "brain", "teaport_brain", "manifest_sig.py"))
    signers = os.path.join(tmp, "manifest/allowed_signers")

    def run():
        p = subprocess.run(["bash", "-c", f'set -euo pipefail\nREPO="{tmp}" VERSION=9.9.9\n'
                            'die() { echo "DIE $*"; exit 1; }\n' + guard + "\necho PASSED\n"],
                           capture_output=True, text=True)
        return p.stdout + p.stderr
    boot = os.path.join(tmp, "install-bootstrap.sh")
    # The placeholder signers file, and a bootstrap carrying it (built here, whatever keys
    # the repository has now): each refused.
    from test_install_bootstrap import placeholder_bootstrap
    pathlib.Path(signers).write_text(PLACEHOLDER_SIGNERS)
    placeholder_bootstrap(boot)
    out = run()
    assert "DIE" in out and "manifest/allowed_signers still has its PLACEHOLDER lines" in out, out
    shutil.copy(KEYS.signers, signers)
    out = run()
    assert "DIE" in out and "install-bootstrap.sh still carries the PLACEHOLDER keys" in out, out
    pathlib.Path(boot).write_text("#!/bin/bash\n")
    assert "PASSED" in run(), run()
    one = [ln for ln in pathlib.Path(KEYS.signers).read_text().splitlines() if "release-main" in ln]
    pathlib.Path(signers).write_text("\n".join(one) + "\n")
    assert "DIE" in run() and "two keys" in run()
    assert "manifest/allowed_signers)" in build.split("SRC_PATHS=(", 1)[1].split("\n", 1)[0]
    recipe = (ROOT / "packaging/nfpm.yaml").read_text()
    assert "- src: brain/teaport_brain/manifest_sig.py\n    dst: /usr/lib/teaport/manifest_sig.py\n" in recipe
    assert "- src: manifest/allowed_signers\n    dst: /usr/lib/teaport/allowed_signers\n" in recipe
    assert "\n  - openssh-client\n" in recipe
    # The paths the check and its callers name agree with what the package installs.
    helper = (ROOT / "packaging/deb/update_apply.py").read_text()
    assert 'SIG_CHECK = "/usr/lib/teaport/manifest_sig.py"' in helper
    assert 'SIGNERS = "/usr/lib/teaport/allowed_signers"' in helper
    assert manifest_sig.SIGNERS == "/usr/lib/teaport/allowed_signers"
    assert "MANIFEST_SIGNERS_PKG=/usr/lib/teaport/allowed_signers\n" in INSTALL.read_text()
    # Stdlib only: the root step runs it with the system python3, outside the brain's venv.
    src = SIG_PY.read_text()
    assert "teaport_brain" not in "".join(ln for ln in src.splitlines() if ln.startswith(("import", "from")))


def test_the_signers_trust_list_covers_every_file_the_package_installs():
    # RELEASING.md's list of the files a signer reads line by line before signing a release
    # (what boxes trust or run as root) must name every repository file packaging/nfpm.yaml
    # puts in the package, but the docs: config_apply.py and wifi_apply.py run as root
    # through sudo, and a file added to the package must not slip past the signer.
    import yaml
    spec = yaml.safe_load((ROOT / "packaging/nfpm.yaml").read_text())
    installed = [c["src"] for c in spec.get("contents", [])
                 if isinstance(c, dict) and isinstance(c.get("src"), str) and not c["src"].startswith("/")
                 and not str(c.get("dst", "")).startswith("/usr/share/doc/")]
    # and every maintainer script it runs as root
    scripts = dict(spec.get("scripts") or {}, **((spec.get("deb") or {}).get("scripts") or {}))
    installed += [v for v in scripts.values() if isinstance(v, str)]
    assert any(v.endswith("postinst") for v in scripts.values()), scripts
    assert "brain/teaport_brain/config_apply.py" in installed and "brain/teaport_brain/wifi_apply.py" in installed
    doc = (ROOT / "packaging/RELEASING.md").read_text()
    block = doc[doc.index("<published brain.commit> <new brain.commit> -- manifest/allowed_signers"):]
    block = block[:block.index("```")]
    listed = [w for w in block.replace("\\", " ").split()[5:] if not w.startswith("':(")]
    assert "manifest/allowed_signers" in listed and "install.sh" in listed, listed

    def covered(path):
        return any(path == f.rstrip("/") or (f.endswith("/") and path.startswith(f)) for f in listed)
    missing = [p for p in installed if not covered(p.rstrip("/"))]
    assert not missing, f"RELEASING.md's trust list does not name {missing}"
    # and what steers how the brain is built (a build backend's config, a setup.py that runs,
    # the build backend's pinned wheel), in every directory
    for glob in ("setup.py", "setup.cfg", "MANIFEST.in", "pyproject.toml", "uv.lock", "requirements.txt",
                 "package.json", "package-lock.json", ".gitattributes"):
        assert f"':(glob)**/{glob}'" in block, glob
    assert "brain/build-requirements.txt" in listed, listed


def test_the_build_backend_is_pinned_by_hash():
    # uv.lock records no build dependencies: brain/build-requirements.txt pins the setuptools
    # wheel the release build installs (--require-hashes) and builds with (--no-build-isolation),
    # and it must satisfy pyproject.toml's own bounds for it.
    import tomllib
    from packaging.specifiers import SpecifierSet
    lines = [ln for ln in (ROOT / "brain/build-requirements.txt").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert len(lines) == 1, lines
    m = re.fullmatch(r"setuptools==(\S+) --hash=sha256:([0-9a-f]{64})", lines[0])
    assert m, lines[0]
    py = tomllib.loads((ROOT / "brain/pyproject.toml").read_text())
    bounds = [py["build-system"]["requires"][0], py["tool"]["uv"]["build-constraint-dependencies"][0]]
    for req in bounds:
        assert req.startswith("setuptools"), req
        assert m.group(1) in SpecifierSet(req[len("setuptools"):]), (m.group(1), req)
    script = (ROOT / "packaging/build-deb.sh").read_text()
    assert "--require-hashes" in script and "brain/build-requirements.txt" in script, "build-deb.sh"
    sync = script[script.index('"$UV" sync --locked'):]
    assert "--no-build-isolation" in sync.split("--project")[0], sync[:400]
    # A release build: the fetched uv and nfpm only (never one on PATH), an empty uv cache.
    for tool in ("have uv &&", "have nfpm &&"):
        line = next(ln for ln in script.splitlines() if tool in ln)
        assert '[ "$release" = 0 ]' in line, line
    assert 'release=0; [ -z "${TEAPORT_DEB_VERSION:-}" ] || release=1' in script
    assert 'export UV_CACHE_DIR="$UVCACHE"' in script and 'rm -rf "$UVCACHE"' in script


def test_ci_prints_the_release_debs_sha256_for_the_signing_laptop():
    # The signing laptop cross-checks a manifest's brain.deb with the sha256 release-deb.yml
    # printed for the tag (a check-run annotation, read anonymously through the API, from that
    # workflow file only). That workflow runs on brain-v* tags alone, and holds only the build
    # and the hash: no permissions at the top, contents: read for the build (checkout without
    # credentials), actions: read for the hash, which has no checkout, downloads the artifact by
    # the id the build reported and checks its digest and the .deb's sha256 against the build's.
    # Every action is pinned by a full commit id.
    import yaml
    flow = yaml.safe_load((ROOT / ".github/workflows/release-deb.yml").read_text())
    assert flow[True] == {"push": {"tags": ["brain-v*"]}}, flow[True]
    assert flow["permissions"] == {}, flow["permissions"]
    jobs = flow["jobs"]
    assert set(jobs) == {"release-deb-build", "brain-deb-sha256", "release-deb-check"}, list(jobs)
    build, hashing = jobs["release-deb-build"], jobs["brain-deb-sha256"]
    # No job takes a name main's ruleset requires: a tag's run must not stand in for them.
    required = {"brain", "brain-deb", "plugin", "formal", "installer", "worker", "docs"}
    assert not {j.get("name", k) for k, j in jobs.items()} & required, list(jobs)
    ci_jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    docs_jobs = yaml.safe_load((ROOT / ".github/workflows/docs.yml").read_text())["jobs"]
    assert required <= set(ci_jobs) | set(docs_jobs), (list(ci_jobs), list(docs_jobs))
    assert ci_jobs["brain"]["strategy"]["matrix"]["runner"] == ["ubuntu-latest", "ubuntu-24.04-arm"]
    assert build["permissions"] == {"contents": "read"} and hashing["permissions"] == {"actions": "read"}
    checkout = next(st for st in build["steps"] if "checkout" in st.get("uses", ""))
    assert checkout["with"]["persist-credentials"] is False, checkout
    assert "actions/upload-artifact@" in build["steps"][-1].get("uses", ""), build["steps"][-1]
    assert hashing["needs"] == "release-deb-build"
    assert not any("checkout" in st.get("uses", "") for st in hashing["steps"]), hashing["steps"]
    download = next(st for st in hashing["steps"] if st.get("uses"))
    assert download["uses"].startswith("actions/download-artifact@"), download
    assert download["with"]["artifact-ids"] == "${{ needs.release-deb-build.outputs.artifact-id }}", download
    for job in jobs.values():
        for st in job["steps"]:
            if "uses" in st and st["uses"] != "./.github/actions/brain-deb-check":
                assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", st["uses"].split(" ")[0]), st["uses"]
    step = next(st for st in hashing["steps"] if st.get("name") == "The .deb's sha256")
    assert "packaging/" not in step["run"] and "brain/" not in step["run"], step["run"]
    work = tempdir("teaport-ci-annotation-")
    os.makedirs(os.path.join(work, "dist"))
    with open(os.path.join(work, "dist", "teaport-brain_1.2.3_arm64.deb"), "wb") as f:
        f.write(b"a package")
    want = hashlib.sha256(b"a package").hexdigest()
    with open(os.path.join(work, "dist", "teaport-brain_1.2.3_arm64.deb.sha256"), "w") as f:
        f.write(f"{want}  teaport-brain_1.2.3_arm64.deb\n")

    def hash_step(sha):
        return subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=work, capture_output=True, text=True,
                              env={**os.environ, "WANT_SHA256": sha})
    out = hash_step(want)
    assert out.returncode == 0, out.stderr
    assert [ln for ln in out.stdout.splitlines() if ln.startswith("::notice")] == \
        [f"::notice title=teaport-brain-deb-sha256::{want}  teaport-brain_1.2.3_arm64.deb"], out.stdout
    # The downloaded .deb is not the one the build reported: no notice.
    bad = hash_step("0" * 64)
    assert bad.returncode != 0 and "::notice" not in bad.stdout, bad.stdout
    # Before the download: GitHub's record of the artifact, by the build's id, carries the build's digest.
    names = [st.get("name") or st.get("uses") for st in hashing["steps"]]
    assert names.index("The artifact is the one the build uploaded") < names.index(download["uses"]), names
    check = next(st for st in hashing["steps"] if st.get("name") == "The artifact is the one the build uploaded")
    assert check["env"]["WANT_DIGEST"] == "${{ needs.release-deb-build.outputs.artifact-digest }}", check["env"]
    assert "actions/artifacts/$ARTIFACT_ID" in check["run"] and '"${WANT_DIGEST#sha256:}"' in check["run"], check["run"]
    assert build["outputs"]["artifact-digest"] == "${{ steps.upload.outputs.artifact-digest }}", build["outputs"]
    # ci.yml no longer prints the annotation (one workflow is its record).
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert "teaport-brain-deb-sha256" not in (ROOT / ".github/workflows/ci.yml").read_text()
    # The release artifact has a name of its own; ci.yml's builds keep theirs.
    upload = build["steps"][-1]
    assert upload["with"]["name"] == "teaport-brain-release-deb", upload
    assert "teaport-brain-release-deb" not in (ROOT / ".github/workflows/ci.yml").read_text()
    # The release .deb is installed and checked too, after the hash, by the build's id and
    # digest, with no write permission, through the same checks as ci.yml's builds.
    checking = jobs["release-deb-check"]
    assert checking["needs"] == ["release-deb-build", "brain-deb-sha256"], checking["needs"]
    assert checking["permissions"] == {"contents": "read", "actions": "read"}, checking["permissions"]
    names = [st.get("name") or st.get("uses") for st in checking["steps"]]
    got = next(st for st in checking["steps"] if "download-artifact" in st.get("uses", ""))
    assert got["with"]["artifact-ids"] == "${{ needs.release-deb-build.outputs.artifact-id }}", got
    assert names.index("The artifact is the one the build uploaded") < names.index(got["uses"]), names
    assert names.index("The .deb, the one hashed") < names.index("./.github/actions/brain-deb-check"), names
    assert next(st for st in checking["steps"] if "checkout" in st.get("uses", ""))["with"]["persist-credentials"] is False
    assert any(st.get("uses") == "./.github/actions/brain-deb-check" for st in ci["jobs"]["brain-deb-check"]["steps"])
    action = yaml.safe_load((ROOT / ".github/actions/brain-deb-check/action.yml").read_text())
    assert any("selfcheck" in st.get("run", "") for st in action["runs"]["steps"]), action


def test_a_release_candidate_is_signed_for_beta_only():
    # #449: brain-vX.Y.Z-rcN builds X.Y.Z~rcN (teaport-brain_X.Y.Z~rcN_arm64.deb). A signed
    # manifest names a candidate only on beta, and a release tag's .deb is the version it
    # builds: manifest_sig (every box), check.py, update_apply, the bootstrap, release-deb.yml.
    def manifest(channel, tag, deb_version, commit="c0ffee" + "0" * 34):
        return {"channel": channel, "brain": {
            "repo": "Teaspoon-AI/teaport", "tag": tag, "commit": commit,
            "deb": {"url": f"https://get.teaspoon.tech/dl/brain/teaport-brain_{deb_version}_arm64.deb",
                    "sha256": "a" * 64}}}
    rp = manifest_sig.release_problem
    assert manifest_sig.release_version("brain-v0.1.1-rc2") == "0.1.1~rc2"
    assert manifest_sig.release_version("brain-v0.1.1") == "0.1.1"
    for bad in ("brain-v0.1.1-rc0", "brain-v0.1.1-rc02", "brain-v0.1.1rc2", "brain-v0.1", "main"):
        assert manifest_sig.release_version(bad) is None, bad
    assert rp(manifest("beta", "brain-v0.1.1-rc2", "0.1.1~rc2")) is None
    assert rp(manifest("beta", "brain-v0.1.1", "0.1.1")) is None
    assert rp(manifest("stable", "brain-v0.1.1", "0.1.1")) is None
    assert "only the beta channel" in rp(manifest("stable", "brain-v0.1.1-rc2", "0.1.1~rc2"))
    assert "only the beta channel" in rp(manifest("beta", "brain-v0.1.1-rc2", "0.1.1~rc2"), "stable")
    for tag, version in (("brain-v0.1.1-rc2", "0.1.1"), ("brain-v0.1.1-rc2", "0.1.1-rc2"),
                         ("brain-v0.1.1-rc2", "0.1.1%7Erc2"), ("brain-v0.1.1", "0.1.1~rc1"),
                         ("brain-v0.1.1", "0.1.2")):
        assert "is not teaport-brain_" in rp(manifest("beta", tag, version)), (tag, version)
    # A manifest that names a release (a full brain.commit) names a release tag; the
    # repository's placeholders (tag main, commit TODO) name no release and pass.
    for tag in ("main", "v0.1.1", "brain-v0.1.1-rc0", "brain-v0.1"):
        assert "is not a release tag" in rp(manifest("stable", tag, "0.1.1")), tag
        assert rp(manifest("stable", tag, "0.1.1", commit="TODO")) is None, tag
    # Every box: a signed stable manifest naming a candidate is refused, on beta it is taken.
    for channel, ok in (("stable", False), ("beta", True)):
        body = json.dumps(manifest(channel, "brain-v0.1.1-rc2", "0.1.1~rc2")).encode()
        url = f"https://get.teaspoon.tech/manifest/{channel}.json"
        if ok:
            assert manifest_sig.accept(body, KEYS.sign(body), url, KEYS.signers)[0]["channel"] == "beta"
        else:
            _refused(lambda: manifest_sig.accept(body, KEYS.sign(body), url, KEYS.signers), "only the beta channel")
    # check.py (the publish check), on a signed manifest: the same rule, and a release tag.
    check = _check_py()
    check.SIGNERS = KEYS.signers
    d = tempdir("teaport-rc-check-")
    for channel, tag, version, said in (("stable", "brain-v0.1.1-rc2", "0.1.1~rc2", "only the beta channel"),
                                        ("beta", "brain-v0.1.1-rc2", "0.1.1", "is not teaport-brain_0.1.1~rc2_"),
                                        ("stable", "main", "0.1.1", "is not a release tag")):
        path = os.path.join(d, f"{channel}.json")
        KEYS.write(path, manifest(channel, tag, version))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = check.check(path, False, signature_only=True)
        assert not ok and said in out.getvalue(), (channel, tag, out.getvalue())
    # update_apply: the installer it fetches is a release tag's, a candidate's only from beta.
    spec = importlib.util.spec_from_file_location("update_apply_rc", ROOT / "packaging/deb/update_apply.py")
    ua = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ua)
    assert ua.read_source(manifest("beta", "brain-v0.1.1-rc2", "0.1.1~rc2"), manifest_sig)["tag"] == "brain-v0.1.1-rc2"
    assert ua.read_source(manifest("stable", "brain-v0.1.1", "0.1.1"), manifest_sig)["tag"] == "brain-v0.1.1"
    for m, said in ((manifest("stable", "brain-v0.1.1-rc2", "0.1.1~rc2"), "only beta offers one"),
                    (manifest("stable", "main", "0.1.1"), "not a release tag")):
        try:
            ua.read_source(m, manifest_sig)
        except RuntimeError as e:
            assert said in str(e), e
        else:
            raise AssertionError(m)
    # A non-release tag with a full commit is neither offered (updates.source, the brain's
    # daily check and teaport update --check) nor applied (update_apply.read_source).
    from teaport_brain import updates
    assert updates.source(manifest("stable", "brain-v0.1.1", "0.1.1")) is not None
    assert updates.source(manifest("beta", "brain-v0.1.1-rc2", "0.1.1~rc2")) is not None
    refused = (manifest("stable", "main", "0.1.1"), manifest("stable", "v0.1.1", "0.1.1"),
               manifest("beta", "brain-v0.1.1-rc0", "0.1.1~rc0"), manifest("stable", "brain-v0.1.1-rc2", "0.1.1~rc2"))
    for m in refused + (manifest("beta", "brain-v0.1.1-rc2", "0.1.1"),):
        assert updates.source(m) is None, m["brain"]
    for m in refused:
        try:
            ua.read_source(m, manifest_sig)
        except RuntimeError:
            continue
        raise AssertionError(m["brain"])
    # The bootstrap's reading of the verified manifest: the same rule.
    boot = (ROOT / "install-bootstrap.sh").read_text()
    py = boot[boot.index("release=\"$(python3 -I - "):]
    py = py[py.index("<<'PY'\n") + len("<<'PY'\n"):py.index("\nPY\n")]
    for channel, tag, ok in (("beta", "brain-v0.1.1-rc2", True), ("stable", "brain-v0.1.1-rc2", False),
                             ("stable", "brain-v0.1.1", True), ("stable", "main", False)):
        path = os.path.join(d, f"boot-{channel}.json")
        with open(path, "w") as f:
            json.dump(manifest(channel, tag, "x"), f)
        p = subprocess.run([sys.executable, "-I", "-", path, channel, "Teaspoon-AI/teaport"], input=py,
                           capture_output=True, text=True)
        assert (p.returncode == 0) == ok, (channel, tag, p.stdout, p.stderr)
        if not ok:
            assert "release candidate" in p.stderr or "not a release tag" in p.stderr, p.stderr
    # release-deb.yml builds a candidate's tag too, and hands build-deb.sh the X.Y.Z-rcN it takes.
    import fnmatch
    import yaml
    flow = yaml.safe_load((ROOT / ".github/workflows/release-deb.yml").read_text())
    tags = flow[True]["push"]["tags"]
    assert any(fnmatch.fnmatchcase("brain-v0.1.1-rc2", t) for t in tags), tags
    assert any(fnmatch.fnmatchcase("brain-v0.1.1", t) for t in tags), tags
    build = next(st for st in flow["jobs"]["release-deb-build"]["steps"] if st.get("name") == "Build the .deb")
    assert 'TEAPORT_DEB_VERSION="${GITHUB_REF_NAME#brain-v}"' in build["run"], build["run"]


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
