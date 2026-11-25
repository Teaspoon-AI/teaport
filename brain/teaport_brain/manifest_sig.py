"""Is this release manifest the one the release keys signed? The one check, for everything
that reads a manifest.

A release manifest (manifest/<channel>.json) names every artifact a box installs, and
`teaport update` and install.sh install them as root. So a manifest is trusted for what it
signs, not for where it was read: it is signed with an OpenSSH key kept offline
(`ssh-keygen -Y sign -n teaport-manifest`, see packaging/RELEASING.md), its signature sits
beside it as <manifest>.sig, and the keys it may be signed with are the ones in the signers
file (manifest/allowed_signers in the repo, which the brain package ships as
/usr/lib/teaport/allowed_signers): a main key and a backup. Nothing is read out of a
manifest before its exact bytes have verified (`ssh-keygen -Y verify`), and a manifest
named <channel>.json must say it is that channel's: a signed stable manifest is no beta one.
A signed manifest names a release candidate's tag (brain-vX.Y.Z-rcN) only on the beta
channel, and a release tag's brain.deb is that version's package, X.Y.Z or X.Y.Z~rcN
(packaging/build-deb.sh's version, which nfpm puts in the file name).

Who calls it:
  - packaging/deb/update_apply.py, the updater's root step, loads this file from
    /usr/lib/teaport/manifest_sig.py (a package file, root's) by its path;
  - install.sh runs it from its own checkout:
        python3 -I brain/teaport_brain/manifest_sig.py check <manifest> <sig> <signers> \
            --source <where the manifest was read> [--unsigned-ok]
  - the brain's release check (updates.py) imports it, so a manifest that does not
    verify shows as an error, never as an update;
  - manifest/check.py, before the publish workflow uploads a manifest.

A local manifest with no signature beside it is accepted only with --unsigned-ok (install.sh:
TEAPORT_MANIFEST_UNSIGNED=1, set by the person running it); a URL never is.

Stdlib only, never imports the rest of the package: the root step runs it with the system
python3 from outside the brain's venv.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

# The signatures' namespaces: a key in the signers file signs nothing else for us. A
# manifest's signature is never a bootstrap's, nor the other way round.
NAMESPACE = "teaport-manifest"
BOOTSTRAP_NAMESPACE = "teaport-bootstrap"
# The trusted keys the brain package ships (the repo's manifest/allowed_signers).
SIGNERS = "/usr/lib/teaport/allowed_signers"
SSH_KEYGEN = "/usr/bin/ssh-keygen"
CHANNELS = ("stable", "beta")
# ssh-keygen with root's PATH and nothing of the caller's.
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
TIMEOUT_SECS = 30
# An SSH signature is a few hundred bytes; anything far larger is not one.
MAX_SIG_BYTES = 16 << 10
# The mark the signers file carries until the real public keys are in it.
PLACEHOLDER = "PLACEHOLDER"


class SignatureError(Exception):
    """The manifest is not one the release keys signed (or it cannot be told)."""


# The one shape a key line may have: a release principal (no pattern: ssh-keygen would read
# "*" or "a,b" as several), the two namespaces, an Ed25519 key, nothing else (no other option,
# no comment). ssh-keygen splits the file on "\n" only and skips only spaces and tabs before a
# "#", where Python's splitlines()/strip() also break on \x1c-\x1e, \x85, U+2028/9: so any
# byte outside printable ASCII and "\n" is refused before anything else is read, and the
# file is split on "\n" alone. What this accepts is what ssh-keygen trusts, line for line.
SIGNER_LINE = re.compile(rb'release-(main|backup) namespaces="teaport-manifest,teaport-bootstrap" '
                         rb'ssh-ed25519 [A-Za-z0-9+/]+={0,2}')
_PRINTABLE = re.compile(rb"[ -~\n]*")
# A signers file is two lines; one over this is refused whole, never read in part.
MAX_SIGNERS_BYTES = 1 << 20


def keys(signers) -> list[str]:
    """The key lines of a signers file (bytes or str), read strictly: printable ASCII and "\n"
    only, split on "\n" alone; a line is blank (spaces only), a comment ("#" first), or exactly
    SIGNER_LINE, each principal at most once. Raises SignatureError for anything else, so a
    file that fails is refused everywhere it is read."""
    data = signers.encode("utf-8", "surrogateescape") if isinstance(signers, str) else bytes(signers)
    if len(data) > MAX_SIGNERS_BYTES:
        raise SignatureError(f"the signers file is over {MAX_SIGNERS_BYTES} bytes: refused")
    if not _PRINTABLE.fullmatch(data):
        bad = next(i for i, b in enumerate(data) if not (32 <= b <= 126 or b == 10))
        raise SignatureError(f"the signers file has a byte that is not printable ASCII "
                             f"(0x{data[bad]:02x} at offset {bad}): refused")
    found, seen = [], set()
    for number, line in enumerate(data.split(b"\n"), 1):
        if not line.strip(b" ") or line.startswith(b"#"):
            continue
        m = SIGNER_LINE.fullmatch(line)
        if not m:
            raise SignatureError(f"line {number} of the signers file is not a release key line "
                                 f"(release-main or release-backup, the two namespaces, one "
                                 f"ssh-ed25519 key): {line[:80].decode('ascii')!r}")
        if m.group(1) in seen:
            raise SignatureError(f"line {number} of the signers file names release-"
                                 f"{m.group(1).decode()} a second time: refused")
        seen.add(m.group(1))
        found.append(line.decode("ascii"))
    return found


def is_local(source: str) -> bool:
    """An absolute path or a file:// URL, as install.sh's fetch_manifest tells them from a
    URL (anything else it hands to curl, so "host/path" is not local)."""
    return source.startswith("/") or urlparse(source).scheme == "file"


def channel_named(source: str) -> str | None:
    """The channel a manifest's name says it is (<channel>.json), else None."""
    name = os.path.basename(urlparse(source).path if not source.startswith("/") else source)
    stem, ext = os.path.splitext(name)
    return stem if ext == ".json" and stem in CHANNELS else None


def _keygen(args: list[str], data: bytes, keygen: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([keygen, *args], input=data, capture_output=True,
                              timeout=TIMEOUT_SECS, env=ENV, cwd="/")
    except FileNotFoundError:
        raise SignatureError(f"cannot check the release manifest's signature: {keygen} is "
                             "missing (the openssh-client package)") from None
    except (OSError, subprocess.SubprocessError) as e:
        raise SignatureError(f"cannot check the release manifest's signature: {e}") from None


def verify(body: bytes, sig: bytes, signers: str, keygen: str = SSH_KEYGEN,
           namespace: str = NAMESPACE) -> str:
    """The principal of the signers file whose key signed exactly <body> in <namespace>
    (a manifest's, unless said). Raises SignatureError."""
    try:
        with open(signers, "rb") as f:
            signers_bytes = f.read(MAX_SIGNERS_BYTES + 1)   # one byte over is refused by keys()
    except OSError as e:
        raise SignatureError(f"cannot read the trusted signing keys ({signers}): {e}") from None
    trusted = keys(signers_bytes)   # strict: a file that fails is refused (SignatureError)
    if not trusted:
        raise SignatureError(f"no trusted signing key in {signers}: nothing can be verified")
    if not sig or len(sig) > MAX_SIG_BYTES or b"-----BEGIN SSH SIGNATURE-----" not in sig:
        raise SignatureError(f"the release {what(namespace)}'s signature is not an SSH signature")
    with tempfile.TemporaryDirectory(prefix="teaport-sig-") as tmp:
        sig_file = os.path.join(tmp, "manifest.sig")
        with open(os.open(sig_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(sig)
        # ssh-keygen reads the bytes just checked, from a copy of its own, not the path again.
        checked = os.path.join(tmp, "allowed_signers")
        with open(os.open(checked, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(signers_bytes)
        found = _keygen(["-Y", "find-principals", "-s", sig_file, "-f", checked], b"", keygen)
        principals = [p for p in found.stdout.decode("ascii", "replace").split("\n") if p] \
            if found.returncode == 0 else []
        if not principals:
            raise SignatureError(f"the release {what(namespace)} is not signed by a trusted key ({signers})")
        why = ""
        for principal in principals:
            p = _keygen(["-Y", "verify", "-f", checked, "-I", principal, "-n", namespace,
                         "-s", sig_file], body, keygen)
            if p.returncode == 0:
                return principal
            said = (p.stderr or p.stdout).decode(errors="replace").strip().splitlines()
            why = why or (said[0] if said else f"exit {p.returncode}")
    raise SignatureError(f"the release {what(namespace)}'s signature does not verify ({why})")


def what(namespace: str) -> str:
    return "installer (install-bootstrap.sh)" if namespace == BOOTSTRAP_NAMESPACE else "manifest"


def parse(body: bytes, channel: str | None) -> dict:
    """The manifest's JSON object; when <channel> is given, one that says it is that
    channel's. Raises SignatureError (a channel that does not match) or ValueError."""
    manifest = json.loads(body)
    if not isinstance(manifest, dict):
        raise ValueError("the release manifest is not a JSON object")
    if channel is not None and manifest.get("channel") != channel:
        raise SignatureError(f"the release manifest is signed for the {manifest.get('channel')!r} "
                             f"channel, not {channel!r}")
    bad = bad_hashes(manifest)
    if bad:
        raise ValueError(f"the release manifest has a sha256 that is not 64 lowercase hex "
                         f"characters ({', '.join(bad)}): sha256sum would take it as more than one line")
    return manifest


_SHA256 = re.compile(r"[0-9a-f]{64}")
# A release's tag, and the version of the package built from it (packaging/build-deb.sh): a
# candidate's brain-vX.Y.Z-rcN builds X.Y.Z~rcN, named teaport-brain_X.Y.Z~rcN_<arch>.deb.
RELEASE_TAG = re.compile(r"brain-v([0-9]+\.[0-9]+\.[0-9]+)(?:-rc([1-9][0-9]*))?")
_COMMIT = re.compile(r"[0-9a-f]{40}")
DEB_FILE = re.compile(r"teaport-brain_([0-9A-Za-z.~+-]+)_([a-z0-9]+)\.deb")


def release_version(tag) -> str | None:
    """The package version <tag> builds (X.Y.Z, or X.Y.Z~rcN for brain-vX.Y.Z-rcN), or None
    when it is no release tag."""
    m = RELEASE_TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not m:
        return None
    return m.group(1) + (f"~rc{m.group(2)}" if m.group(2) else "")


def release_problem(manifest: dict, channel: str | None = None) -> str | None:
    """Why the signed <manifest> names a release this channel may not install, or None: a
    manifest that names a release (brain.commit a full commit id) whose brain.tag is no
    release tag; a release candidate's tag (brain-vX.Y.Z-rcN) anywhere but on beta; a
    brain.deb whose file name is not the version its release tag builds. <channel> defaults
    to the manifest's own. A manifest that names no commit (the repository's placeholders)
    names no release, and nothing installs from it."""
    channel = channel or manifest.get("channel")
    brain = manifest.get("brain") if isinstance(manifest.get("brain"), dict) else {}
    tag = brain.get("tag")
    version = release_version(tag)
    if version is None:
        if isinstance(brain.get("commit"), str) and _COMMIT.fullmatch(brain["commit"]):
            return (f"brain.tag {tag!r} is not a release tag (brain-vX.Y.Z, or brain-vX.Y.Z-rcN on the "
                    "beta channel)")
        return None
    if "~rc" in version and channel != "beta":
        return (f"brain.tag {tag} is a release candidate: only the beta channel names one, and the "
                f"{channel} channel names a final brain-vX.Y.Z")
    deb = brain.get("deb") if isinstance(brain.get("deb"), dict) else {}
    url = deb.get("url")
    if not isinstance(url, str) or "TODO" in url:
        return None
    name = url.rsplit("/", 1)[-1]
    m = DEB_FILE.fullmatch(name)
    if not m or m.group(1) != version:
        return (f"brain.deb {name!r} is not teaport-brain_{version}_<arch>.deb, the package {tag} "
                "builds")
    return None


def bad_hashes(node, path="") -> list[str]:
    """Where <node> (a manifest) has a sha256 value (a "sha256" or "*_sha256" key, or an entry
    of an "*_sha256" map) that is neither 64 lowercase hex characters nor the "TODO"
    placeholder. install.sh hands these to sha256sum -c, which reads a value with a newline
    in it as several lines and passes once one of them checks out."""
    out = []
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and (key == "sha256" or key.endswith("_sha256")):
                for sub, v in (value.items() if isinstance(value, dict) else [("", value)]):
                    if v != "TODO" and not (isinstance(v, str) and _SHA256.fullmatch(v)):
                        out.append(f"{where}.{sub}" if sub else where)
            else:
                out += bad_hashes(value, where)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out += bad_hashes(value, f"{path}[{i}]")
    return out


def accept(body: bytes, sig: bytes | None, source: str, signers: str = SIGNERS, *,
           channel: str | None = None, unsigned_ok: bool = False,
           keygen: str = SSH_KEYGEN) -> tuple[dict, str]:
    """The manifest read from <source>, once its signature <sig> (None: there is none)
    has verified, and what was checked, to say. <channel> defaults to the one the
    source's name says. Only a local source may go without a signature, and only with
    <unsigned_ok>. Raises SignatureError or ValueError (not JSON)."""
    if channel is None:
        channel = channel_named(source)
    if sig is None:
        if not (unsigned_ok and is_local(source)):
            raise SignatureError(f"the release manifest has no signature ({source}.sig): nothing "
                                 "is installed from an unsigned manifest")
        return parse(body, channel), f"{source} is not signed: accepted as given (unsigned allowed)"
    principal = verify(body, sig, signers, keygen)
    manifest = parse(body, channel)
    problem = release_problem(manifest, channel)
    if problem:
        raise SignatureError(f"the signed release manifest is refused: {problem}")
    return manifest, f"signed by {principal} (trusted keys: {signers})"


def main(argv: list[str]) -> int:
    """check <manifest> <sig> <signers> [--source S] [--channel C] [--unsigned-ok]:
    exit 0 and say who signed it, or 1 and why not. A <sig> that does not exist is no
    signature. signers <file>: the number of keys in it, read as verify() reads it (exit 1
    when it is refused)."""
    usage = ("usage: manifest_sig.py check <manifest> <sig> <signers> [--source S] "
             "[--channel stable|beta] [--unsigned-ok]\n"
             "       manifest_sig.py signers <signers>   (read it strictly; print its key count)")
    if len(argv) == 2 and argv[0] == "signers":
        try:
            with open(argv[1], "rb") as f:
                print(len(keys(f.read(MAX_SIGNERS_BYTES + 1))))
        except (OSError, SignatureError) as e:
            print(str(e), file=sys.stderr)
            return 1
        return 0
    if len(argv) < 4 or argv[0] != "check":
        print(usage, file=sys.stderr)
        return 2
    manifest, sig_path, signers = argv[1:4]
    source, channel, unsigned_ok, rest = manifest, None, False, argv[4:]
    while rest:
        arg = rest.pop(0)
        if arg == "--unsigned-ok":
            unsigned_ok = True
        elif arg in ("--source", "--channel") and rest:
            value = rest.pop(0)
            if arg == "--source":
                source = value
            elif value in CHANNELS:
                channel = value
            else:
                print(f"unknown channel: {value}", file=sys.stderr)
                return 2
        else:
            print(usage, file=sys.stderr)
            return 2
    try:
        with open(manifest, "rb") as f:
            body = f.read()
        sig = None
        if os.path.exists(sig_path):
            with open(sig_path, "rb") as f:
                sig = f.read(MAX_SIG_BYTES + 1)
        _, said = accept(body, sig, source, signers, channel=channel, unsigned_ok=unsigned_ok)
    except (SignatureError, ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(said)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
