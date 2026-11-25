#
# The one-line installer (install-bootstrap.sh, served as get.teaspoon.tech/teaport): a fresh
# install runs what the release keys signed, not what the download host or GitHub says.
#
# What has to hold:
#   - The bootstrap carries manifest/allowed_signers unchanged, and a release build refuses
#     while it carries the placeholder. It verifies the channel's manifest and .sig with them
#     (ssh-keygen, namespace teaport-manifest), requires the channel it asked for, takes
#     brain.repo/tag/commit from the verified bytes only, requires the tag (peeled) and the
#     clone's HEAD to be brain.commit, and runs that checkout's install.sh against the copy it
#     verified (file://, the .sig beside it). A tampered manifest, a signature by a key it does
#     not carry, a signature in another namespace, none at all, a channel that is not the one
#     asked for, a moved tag and a clone at another commit each stop it before anything is
#     cloned or run. With no keys (the placeholder) it installs nothing.
#   - TEAPORT_REF is the developer's path: a loud warning that no signature is behind it.
#   - The bootstrap's own signature (namespace teaport-bootstrap) is what manifest/check.py
#     --bootstrap checks, and the installation guide shows the keys the file has.
#
# Throwaway keys (release_keys.py), a local bare repository with a tag, file:// URLs: nothing
# leaves the machine, and install.sh is a stub that says how it was run.
#
# Run: python test_install_bootstrap.py   (or via pytest test_suite.py)
#
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import appliance  # noqa: E402
from release_keys import BOOTSTRAP_NAMESPACE, PLACEHOLDER_SIGNERS, Keys, commit_files, git  # noqa: E402
from tempdirs import tempdir  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "install-bootstrap.sh"
SIGNERS = ROOT / "manifest/allowed_signers"
GIT = shutil.which("git") or "/usr/bin/git"
KEYS = Keys()
TAG = "brain-v1.0.0"
OPEN = "cat <<'SIGNERS'\n"
CLOSE = "\nSIGNERS\n"

STUB = """#!/bin/bash
UPDATE_PROTOCOL=1
echo "INSTALL ran with url=${TEAPORT_MANIFEST_URL:-} args=$*"
f="${TEAPORT_MANIFEST_URL#file://}"
if [ -f "$f" ] && [ -f "$f.sig" ]; then echo "INSTALL has the verified pair"; fi
"""


def _embedded(text):
    start = text.index(OPEN) + len(OPEN)
    return text[start:text.index(CLOSE, start) + 1]


def _git(repo, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run([GIT, "-C", repo, *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


class Release:
    """A repository with the release tag (annotated) on its second commit, served bare as
    file://, the manifest that names it signed, and the bootstrap carrying the test keys."""

    def __init__(self):
        self.dir = tempdir("teaport-boot-")
        src = os.path.join(self.dir, "src")
        os.makedirs(src)
        _git(src, "init", "-q")
        pathlib.Path(src, "install.sh").write_text("#!/bin/bash\necho old\n")
        _git(src, "add", "-A")
        _git(src, "commit", "-qm", "first")
        self.first = _git(src, "rev-parse", "HEAD")
        pathlib.Path(src, "install.sh").write_text(STUB)
        _git(src, "commit", "-qam", "release")
        self.commit = _git(src, "rev-parse", "HEAD")
        _git(src, "tag", "-a", "-m", "release", TAG)
        _git(src, "branch", "-M", "devel")
        self.bare = os.path.join(self.dir, "bare.git")
        subprocess.run([GIT, "clone", "-q", "--bare", src, self.bare], check=True, capture_output=True)
        self.repo = f"file://{self.bare}"
        self.serve = os.path.join(self.dir, "serve")
        os.makedirs(self.serve)
        text = BOOTSTRAP.read_text()
        self.script = os.path.join(self.dir, "teaport")
        pathlib.Path(self.script).write_text(
            text.replace(_embedded(text), pathlib.Path(KEYS.signers).read_text()))
        # Every git command the bootstrap runs, logged; rev-parse can be made to lie.
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.dir, "git.log")
        shim = os.path.join(self.bin, "git")
        pathlib.Path(shim).write_text(
            f'#!/bin/bash\necho "$*" >> "{self.log}"\n'
            'if [ -n "${FAKE_HEAD:-}" ] && [ "$1" = -C ] && [ "$3" = rev-parse ] && [ "$4" = HEAD ]; then echo "$FAKE_HEAD"; exit 0; fi\n'
            f'exec "{GIT}" "$@"\n')
        os.chmod(shim, 0o755)

    def manifest(self, channel="stable", commit=None, tag=TAG, key="main", namespace=None,
                 name="stable.json", tamper=False, repo="Teaspoon-AI/teaport"):
        m = {"channel": channel, "brain": {"repo": repo, "tag": tag,
                                            "commit": commit or self.commit}}
        path = os.path.join(self.serve, name)
        for p in (path, path + ".sig"):
            if os.path.exists(p):
                os.unlink(p)
        KEYS.write(path, m, key=key, **({"namespace": namespace} if namespace else {}))
        if tamper:
            pathlib.Path(path).write_bytes(pathlib.Path(path).read_bytes().replace(b"Teaspoon-AI", b"Teaspoon-Al"))
        return path

    def run(self, *args, script=None, **env):
        if os.path.exists(self.log):
            os.unlink(self.log)
        tmp = tempdir("teaport-boot-run-")
        e = {k: v for k, v in os.environ.items() if not k.startswith("TEAPORT_")}
        e.update({"PATH": f"{self.bin}:{os.environ['PATH']}", "TMPDIR": tmp,
                  "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                  "TEAPORT_MANIFEST_URL": f"file://{self.serve}/stable.json", "TEAPORT_REPO": self.repo})
        e.update({k: v for k, v in env.items() if v is not None})
        for k, v in env.items():
            if v is None:
                e.pop(k, None)
        p = subprocess.run(["bash", script or self.script, *args], capture_output=True, text=True,
                           env=e, stdin=subprocess.DEVNULL)
        git = pathlib.Path(self.log).read_text() if os.path.exists(self.log) else ""
        assert os.listdir(tmp) == [], f"the bootstrap left {os.listdir(tmp)} in its TMPDIR"
        return p.returncode, p.stdout + p.stderr, git


def test_it_runs_the_installer_of_the_commit_the_signed_manifest_names():
    r = Release()
    for key in ("main", "backup"):
        r.manifest(key=key)
        rc, out, git = r.run("--dry-run")
        assert rc == 0, out
        assert f"signed by release-{key}" in out and r.commit in out, out
        assert "INSTALL ran with url=file://" in out and "/stable.json args=--dry-run" in out, out
        assert "INSTALL has the verified pair" in out, out
        assert re.search(rf"^ls-remote {re.escape(r.repo)} refs/tags/{TAG} ", git, re.M), git
        # The tag by its full ref, every object checked, then the signed commit by its id.
        assert f"-c transfer.fsckObjects=true fetch --quiet --depth 1 --no-tags {r.repo} +refs/tags/{TAG}:refs/tags/{TAG}" in git, git
        assert re.search(rf"checkout --quiet {r.commit}$", git, re.M) and "clone" not in git, git
    # A plain path for the manifest, as a mirror on a disk has it.
    rc, out, _ = r.run(TEAPORT_MANIFEST_URL=f"{r.serve}/stable.json")
    assert rc == 0 and "INSTALL ran" in out, out
    # The beta channel, asked for.
    r.manifest(channel="beta", name="beta.json")
    rc, out, _ = r.run(TEAPORT_CHANNEL="beta", TEAPORT_MANIFEST_URL=f"file://{r.serve}/beta.json")
    assert rc == 0 and "/beta.json args=" in out, out


def test_nothing_is_cloned_or_run_from_a_release_that_does_not_verify():
    r = Release()
    cases = {
        "tampered": (dict(tamper=True), "does not verify"),
        "unknown key": (dict(key="stranger"), "does not verify"),
        "namespace": (dict(namespace=BOOTSTRAP_NAMESPACE), "does not verify"),   # a bootstrap's signature
        "channel": (dict(channel="beta"), "signed for the 'beta' channel, not 'stable'"),
        "moved tag": (dict(commit=None), "the tag was moved"),
        "no commit": (dict(commit="TODO"), "names no installer to fetch"),
        "no tag": (dict(tag="brain-v9.9.9"), "is not a tag of"),
        # Signed, but another repository: never fetched (TEAPORT_REPO may point at a mirror of
        # the release repository, not let the manifest name another).
        "another repository": (dict(repo="evil/teaport"), "not Teaspoon-AI/teaport"),
    }
    for name, (kw, want) in cases.items():
        if name == "moved tag":
            kw = dict(commit=r.first)   # the tag is at the release commit, not this one
        r.manifest(**kw)
        rc, out, git = r.run()
        assert rc == 1 and want in out, (name, out)
        assert "INSTALL ran" not in out and "fetch" not in git and "clone" not in git, (name, out, git)
    # No signature at all.
    path = r.manifest()
    os.unlink(path + ".sig")
    rc, out, git = r.run()
    assert rc == 1 and "has no signature" in out and "INSTALL ran" not in out and git == "", out
    # The checkout is not at the signed commit (the server lied after ls-remote): not run.
    r.manifest()
    rc, out, git = r.run(FAKE_HEAD="0" * 40)
    assert rc == 1 and "not the signed commit" in out and "INSTALL ran" not in out, out


def _forge(bare, commit):
    """Unpack <bare>'s objects and replace install.sh's blob at <commit> with other bytes
    under the same name: a mirror that lies about one object."""
    for pack in [os.path.join(bare, "objects/pack", n) for n in os.listdir(os.path.join(bare, "objects/pack"))
                 if n.endswith(".pack")]:
        data = pathlib.Path(pack).read_bytes()
        os.rename(pack, pack + ".x")
        os.rename(pack[:-5] + ".idx", pack[:-5] + ".idx.x")
        subprocess.run([GIT, "-C", bare, "unpack-objects", "-q"], input=data, check=True)
    blob = _git(bare, "rev-parse", f"{commit}:install.sh")
    evil = b'#!/bin/bash\necho "FORGED INSTALLER RAN"\n'
    path = os.path.join(bare, "objects", blob[:2], blob[2:])
    os.chmod(path, 0o644)
    pathlib.Path(path).write_bytes(__import__("zlib").compress(b"blob %d\0" % len(evil) + evil))


def test_a_mirror_that_forges_an_object_runs_nothing_whatever_its_address():
    # A plain-path clone would hard-link the mirror's objects as they are, so a forged blob
    # under the signed commit would pass a HEAD check. The bootstrap fetches over the pack
    # transport, which re-hashes every object: refused, by path and by file:// alike.
    r = Release()
    r.manifest()
    _forge(r.bare, r.commit)
    for repo in (r.bare, r.repo):
        rc, out, git = r.run("--dry-run", TEAPORT_REPO=repo)
        assert rc == 1 and "FORGED" not in out and "INSTALL ran" not in out, (repo, out)
        assert "cannot fetch" in out, (repo, out)


def test_a_branch_named_like_the_release_tag_changes_nothing():
    # git clone --branch X prefers refs/heads/X: a pushed branch named like the tag would
    # have blocked every install. The tag is fetched by its full ref instead.
    r = Release()
    _git(r.bare, "branch", TAG, r.first)   # refs/heads/brain-v1.0.0, at the older commit
    r.manifest()
    rc, out, git = r.run("--dry-run")
    assert rc == 0 and "INSTALL ran with url=file://" in out and "INSTALL has the verified pair" in out, out
    assert f"checkout --quiet {r.commit}" in git, git


def placeholder_bootstrap(path):
    """The bootstrap carrying the placeholder signers file (no keys), at <path>."""
    text = BOOTSTRAP.read_text()
    pathlib.Path(path).write_text(text.replace(_embedded(text), PLACEHOLDER_SIGNERS))
    return path


def test_a_bootstrap_with_no_keys_installs_nothing():
    r = Release()
    r.manifest()
    # The bootstrap as it was before the release keys existed (built here, whatever keys the
    # repository has now).
    rc, out, git = r.run(script=placeholder_bootstrap(os.path.join(r.dir, "placeholder")))
    assert rc == 1 and "carries no release keys yet" in out and git == "", out


def test_teaport_ref_is_the_developers_unverified_path_and_says_so():
    r = Release()
    rc, out, git = r.run("--dry-run", TEAPORT_REF="devel", TEAPORT_MANIFEST_URL=None)
    assert rc == 0 and "WARNING" in out and "No release signature is checked" in out, out
    assert "INSTALL ran with url= args=--dry-run" in out, out    # install.sh reads (and checks) the manifest itself
    assert "--branch devel" in git and "ls-remote" not in git, git
    assert "TEAPORT_REF" in BOOTSTRAP.read_text().split("set -euo pipefail")[0]


def test_the_bootstrap_carries_the_repos_keys_and_the_guide_shows_them():
    text = BOOTSTRAP.read_text()
    assert _embedded(text) == SIGNERS.read_text(), \
        "install-bootstrap.sh's trusted_signers block differs from manifest/allowed_signers"
    # The guide's verify-first path shows the keys: the key lines (or, until the keys exist,
    # the placeholder lines) of the signers file, exactly.
    guide = (ROOT / "docs/src/content/docs/getting-started/installation.md").read_text()
    after = guide.split("cat > teaport-signers <<'EOF'\n", 1)[1].splitlines()
    block = [ln.strip() for ln in after[:next(i for i, ln in enumerate(after) if ln.strip() == "EOF")]]
    want = [ln for ln in SIGNERS.read_text().splitlines()
            if ln.strip() and (not ln.startswith("#") or ln.startswith("# PLACEHOLDER release-"))]
    assert block == want, (block, want)
    # A release build refuses a bootstrap that still carries the placeholder.
    build = (ROOT / "packaging/build-deb.sh").read_text()
    assert 'grep -q PLACEHOLDER "$REPO/install-bootstrap.sh"' in build


def _check_py():
    spec = importlib.util.spec_from_file_location("manifest_check_boot", ROOT / "manifest/check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_publish_check_wants_the_bootstrap_signed_in_its_own_namespace():
    r = Release()
    check = _check_py()
    check.SIGNERS = KEYS.signers   # the test bootstrap carries these
    body = pathlib.Path(r.script).read_bytes()

    def run():
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = check.bootstrap(r.script)
        return ok, out.getvalue()
    pathlib.Path(r.script + ".sig").write_bytes(KEYS.sign(body, namespace=BOOTSTRAP_NAMESPACE))
    ok, out = run()
    assert ok and "signed by release-main" in out, out
    pathlib.Path(r.script + ".sig").write_bytes(KEYS.sign(body))   # a manifest's namespace
    ok, out = run()
    assert not ok and "does not verify" in out, out
    pathlib.Path(r.script + ".sig").write_bytes(KEYS.sign(body, "stranger", namespace=BOOTSTRAP_NAMESPACE))
    assert not run()[0]
    os.unlink(r.script + ".sig")
    ok, out = run()
    assert not ok and ".sig is missing" in out, out
    # Keys that are not the repository's.
    check.SIGNERS = str(SIGNERS)
    pathlib.Path(r.script + ".sig").write_bytes(KEYS.sign(body, namespace=BOOTSTRAP_NAMESPACE))
    ok, out = run()
    assert not ok and "is not manifest/allowed_signers" in out, out
    wf = (ROOT / ".github/workflows/publish.yml").read_text()
    up = wf[wf.index("Upload to the download bucket"):]
    assert up.index("put install-bootstrap.sh.sig teaport.sig") < up.index("put install-bootstrap.sh teaport "), up
    assert wf.count("--bootstrap install-bootstrap.sh") == 2, wf


def test_a_key_a_push_adds_is_not_trusted_until_a_trusted_key_signs_for_it():
    # Whoever can push can edit manifest/allowed_signers and the bootstrap's copy of it. So the
    # bootstrap's signature must also verify with the keys the published bootstrap carries
    # (--base's): a key added and signed with only by itself is refused, and the change named.
    r = Release()
    check = _check_py()
    stranger = subprocess.run(["ssh-keygen", "-y", "-f", os.path.join(KEYS.dir, "stranger")],
                              capture_output=True, text=True, check=True).stdout.split()
    # The push swaps its own key in as release-backup (a third line would not even parse).
    swapped = f'release-backup namespaces="teaport-manifest,{BOOTSTRAP_NAMESPACE}" {stranger[0]} {stranger[1]}'
    added = "".join((swapped if ln.startswith("release-backup ") else ln) + "\n"
                    for ln in pathlib.Path(KEYS.signers).read_text().splitlines())
    d = tempdir("teaport-added-key-")
    signers = os.path.join(d, "allowed_signers")
    pathlib.Path(signers).write_text(added)
    check.SIGNERS = signers
    text = pathlib.Path(r.script).read_text()
    script = os.path.join(d, "install-bootstrap.sh")
    pathlib.Path(script).write_text(text.replace(_embedded(text), added))
    body = pathlib.Path(script).read_bytes()
    # What is published, as git records it at --base: a repository whose base commit has the
    # bootstrap carrying main and backup.
    repo = tempdir("teaport-boot-base-")
    git(repo, "init", "-q")
    pathlib.Path(repo, "README").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    root = git(repo, "rev-parse", "HEAD")
    check.HERE = os.path.join(repo, "manifest")

    def base_with(bootstrap_bytes):
        check.BASE[0] = commit_files(repo, root, {"install-bootstrap.sh": bootstrap_bytes} if bootstrap_bytes else {})

    def run(against_base=True):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = check.bootstrap(script, against_base)
        return ok, out.getvalue()
    base_with(pathlib.Path(r.script).read_bytes())
    pathlib.Path(script + ".sig").write_bytes(KEYS.sign(body, "stranger", namespace=BOOTSTRAP_NAMESPACE))
    assert run(False)[0]   # this commit's keys alone would take it
    ok, out = run()
    assert not ok and "not by a key the published bootstrap" in out and f"+ {swapped}" in out, out
    # Signed by a key already trusted: the change is the signer's, and it is said.
    pathlib.Path(script + ".sig").write_bytes(KEYS.sign(body, namespace=BOOTSTRAP_NAMESPACE))
    ok, out = run()
    assert ok and "::warning" in out and f"+ {swapped}" in out, out
    # Nothing trusted yet (no bootstrap at --base, the old one without keys, or the
    # placeholder): the first bootstrap.
    pathlib.Path(script + ".sig").write_bytes(KEYS.sign(body, "stranger", namespace=BOOTSTRAP_NAMESPACE))
    base_with(None)
    assert run()[0]
    base_with(b"#!/bin/bash\necho old bootstrap\n")
    assert run()[0]
    base_with(pathlib.Path(placeholder_bootstrap(os.path.join(d, "placeholder"))).read_bytes())
    assert run()[0]
    wf = (ROOT / ".github/workflows/publish.yml").read_text()
    assert wf.count("--boxes-signers --bootstrap install-bootstrap.sh") == 2, wf


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
