#
# release_keys.py — throwaway release keys for the tests that check a manifest's signature.
#
# Real ssh-keygen, real Ed25519 keys, made fresh in a temp dir each time a script asks (no
# passphrase: they sign a test's manifest and are gone when the script exits). The signers
# file they write trusts "main" and "backup", as manifest/allowed_signers trusts the release
# keys; "stranger" is a key nobody trusts. Never the repo's signers file.
#
import json
import os
import shutil
import subprocess

import appliance
from tempdirs import tempdir

# A signers file with no keys yet, as the repository's was before the release keys existed:
# comment lines only (tests build it rather than read the repository's, which gains keys).
PLACEHOLDER_SIGNERS = (
    "# PLACEHOLDER: the release keys do not exist yet.\n"
    '# PLACEHOLDER release-main namespaces="teaport-manifest,teaport-bootstrap" ssh-ed25519 <main public key>\n'
    '# PLACEHOLDER release-backup namespaces="teaport-manifest,teaport-bootstrap" ssh-ed25519 <backup public key>\n')

NAMESPACE = "teaport-manifest"
BOOTSTRAP_NAMESPACE = "teaport-bootstrap"
KEYGEN = shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"


class Keys:
    def __init__(self):
        if not os.path.exists(KEYGEN):
            appliance.skip("no ssh-keygen here (openssh-client)")
        self.dir = tempdir("teaport-keys-")
        lines = ["# throwaway test keys"]
        for name in ("main", "backup", "stranger"):
            key = os.path.join(self.dir, name)
            subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", f"test-{name}", "-f", key],
                           check=True, stdin=subprocess.DEVNULL)
            if name != "stranger":
                pub = open(key + ".pub").read().split()
                lines.append(f'release-{name} namespaces="{NAMESPACE},{BOOTSTRAP_NAMESPACE}" {pub[0]} {pub[1]}')
        self.signers = os.path.join(self.dir, "allowed_signers")
        with open(self.signers, "w") as f:
            f.write("\n".join(lines) + "\n")

    def sign(self, body: bytes, key: str = "main", namespace: str = NAMESPACE) -> bytes:
        """The SSH signature of <body> with one of the keys, as `ssh-keygen -Y sign` makes it."""
        work = tempdir("teaport-sign-")
        path = os.path.join(work, "m.json")
        with open(path, "wb") as f:
            f.write(body)
        subprocess.run([KEYGEN, "-Y", "sign", "-f", os.path.join(self.dir, key), "-n", namespace, path],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)
        with open(path + ".sig", "rb") as f:
            return f.read()

    def write(self, path: str, manifest, key: str = "main", namespace: str = NAMESPACE) -> bytes:
        """<manifest> (a dict, or bytes as they are) at <path>, signed at <path>.sig;
        key=None leaves it unsigned. Returns the bytes written."""
        body = manifest if isinstance(manifest, bytes) else json.dumps(manifest, indent=2).encode()
        with open(path, "wb") as f:
            f.write(body)
        if key is not None:
            with open(path + ".sig", "wb") as f:
                f.write(self.sign(body, key, namespace))
        return body


def git(repo, *args, data=None):
    """git in <repo>, with no user or system configuration; stdout (text, stripped)."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    env.update(data[1] if isinstance(data, tuple) else {})
    p = subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, env=env,
                       input=data[0] if isinstance(data, tuple) else data)
    return p.stdout.decode().strip()


def commit_files(repo, parent, files, message="base"):
    """A commit on <parent> with <files> ({path: bytes, or None to remove}) changed, made with
    git's plumbing: HEAD and the work tree stay as they are. Its id."""
    index = os.path.join(tempdir("teaport-index-"), "index")
    extra = {"GIT_INDEX_FILE": index}
    git(repo, "read-tree", parent, data=(None, extra))
    for path, content in files.items():
        if content is None:
            git(repo, "update-index", "--force-remove", path, data=(None, extra))
            continue
        blob = git(repo, "hash-object", "-w", "--stdin", data=(content, extra))
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", data=(None, extra))
    tree = git(repo, "write-tree", data=(None, extra))
    return git(repo, "commit-tree", tree, "-p", parent, "-m", message, data=(None, extra))
