#!/usr/bin/python3
#
# teaport — the install trees, rebuilt as root's (install.sh's own_install_trees runs this
# as root, once per box):
#
#     python3 -I root_layout.py migrate <prefix> <state> <brain-package> [<uid> <gid>]
#
# /opt/teaport ($PREFIX) and /var/lib/teaport ($STATE) are root's: the units run as the run
# user and only read there, and `teaport update` runs install.sh in them as root, started
# by that user without a password. Installers before this one gave both trees to the run
# user, who is also who the agent runs as. A tree that was theirs cannot be made root's in
# place: an ACL they set stays on it (and passes to what root makes there later), a file
# they hold open stays writable through that handle, a directory they add to while
# `chown -R` walks it escapes, and anything they planted is kept. So it is not taken over:
# it is REBUILT, next to the old one, from what can be checked, and the two are swapped by
# rename. What goes into the new tree:
#   - the engine's files and the models, hard-linked (they are large; the engine runs as
#     the run user and root only reads them, so a handle kept on one gains nothing), each
#     taken only if it is a regular file reached without a symlink, and stripped of ACLs:
#     the binary and its notices, and every file in the engine's data directories (bin/g2p,
#     bin/watermark, models/voxtral, models/kokoro), not only the ones this release names -- an engine
#     reads more of its own there than an older installer knew (a dictionary per language);
#   - the installed manifest, copied the same way (root reads it as data);
#   - the brain package's tree, copied file by file from what dpkg lists: a regular file
#     only if its MD5 is the one dpkg recorded, a symlink only if it stays in the tree or
#     points into /usr/bin (the venv's bin/python), each directory made new. Nothing it
#     does not list comes along;
#   - what is the run user's own and that root never reads or runs (the Discord bridge's
#     npm tree, a pre-package venv still in use), moved over unchanged;
#   - of $STATE, the record and text of the engine license accepted, and the journal and ARP
#     drop-ins' "applied" notes (small, read as data); downloads are fetched again.
# The engine's staged files and rollback copies are not carried (a copy's unit file would
# be written back as root): the next engine update keeps a new one. The old trees are then
# removed (shutil.rmtree, which does not follow symlinks). A marker, .root-layout, says the
# rebuild is done; install.sh skips it from then on.
#
# Every path into the old tree is walked one component at a time from a directory handle,
# never through a symlink (O_NOFOLLOW), so nothing planted there can make this read or link
# a file elsewhere.
#
# Stdlib only, the system python3 (-I), nothing imported from teaport.
#
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

MARK = ".root-layout"
ENGINE = ("bin/voxtral", "bin/g2p/kokoro_g2p.v2.dict", "bin/g2p/kokoro_pos.bin",
          "bin/watermark/perth_implicit.v2.bin", "THIRD_PARTY_NOTICES.txt",
          "models/voxtral/consolidated.safetensors", "models/voxtral/tekken.json", "models/voxtral/params.json",
          "models/kokoro/Kokoro_espeak_F16.gguf")
# The engine's data directories: every regular file in them is the engine's, carried with
# ENGINE (the dictionaries and models a later engine reads are not all named above). Not a
# download in progress: install.sh's download() resumes a <file>.part, and one carried
# over would put bytes the run user wrote into a file root installs.
ENGINE_DIRS = ("bin/g2p", "bin/watermark", "models/voxtral", "models/kokoro")
PARTIAL = ".part"
COPIED = ("manifest.json",)
# Names at the top of $PREFIX this rebuilds, or leaves behind on purpose; anything else
# there is the run user's own (bridge/, venvs/, venv, ...) and moves over as it is.
MANAGED = {"bin", "models", "brain", "manifest.json", "THIRD_PARTY_NOTICES.txt",
           "engine.staged", "engine.rollback", "engine.swapping", MARK}
STATE_KEPT_JSON = ("eula-accepted.json",)
STATE_KEPT_TEXT = ("journald.applied", "arp-multihome.applied")
SMALL = 1 << 20   # a kept $STATE note larger than this is not one of ours
ACL_XATTRS = ("system.posix_acl_access", "system.posix_acl_default")
# A failure writing the new tree (not reading the old one): the rebuild stops, nothing swapped.
WRITE_ERRORS = (errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS)
# The only symlinks a brain tree carries over: the venv's own, exactly as uv makes them
# (the released package has bin/python -> /usr/bin/python3.12, bin/python3 and
# bin/python3.12 -> python, lib64 -> lib). A bare target has no "/" to bend it out of its
# directory; the one absolute target allowed is the system interpreter itself.
VENV_LINKS = {
    "brain/bin/python": re.compile(r"/usr/bin/python3(\.\d+)?|python3(\.\d+)?"),
    "brain/bin/python3": re.compile(r"/usr/bin/python3(\.\d+)?|python(3(\.\d+)?)?"),
    "brain/lib64": re.compile(r"lib"),
}
VENV_VERSIONED_LINK = re.compile(r"brain/bin/python3\.\d+")
# Exit code: the trees are root's, but the brain package's tree lacks files it should have
# (left out by the rebuild) and no install of the package has put them back yet: install.sh
# then refuses what would restart the brain on it (--only engine, --rollback engine).
INCOMPLETE = 3


def say(msg: str) -> None:
    print(f"root layout: {msg}", flush=True)


class Tree:
    """The old tree, entered only through directory handles, never through a symlink."""

    def __init__(self, path: str):
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.uid = os.fstat(self.fd).st_uid

    def _parent(self, rel: str):
        parts = [p for p in rel.split("/") if p]
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if part in (".", ".."):
                    raise OSError(errno.EINVAL, rel)
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
        except BaseException:
            os.close(fd)
            raise
        return fd, parts[-1]

    def lstat(self, rel: str):
        """os.lstat of <rel>, or None when it is not there or the way to it is not plain."""
        try:
            fd, name = self._parent(rel)
        except OSError:
            return None
        try:
            return os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError:
            return None
        finally:
            os.close(fd)

    def open_file(self, rel: str):
        """A read handle on <rel> when it is a regular file reached without a symlink; None
        otherwise."""
        try:
            fd, name = self._parent(rel)
        except OSError:
            return None
        try:
            f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except OSError:
            return None
        finally:
            os.close(fd)
        if not stat.S_ISREG(os.fstat(f).st_mode):
            os.close(f)
            return None
        os.set_blocking(f, True)
        return f

    def readlink(self, rel: str):
        try:
            fd, name = self._parent(rel)
        except OSError:
            return None
        try:
            return os.readlink(name, dir_fd=fd)
        except OSError:
            return None
        finally:
            os.close(fd)

    def names(self, rel: str) -> list[str]:
        """The names in directory <rel> when it is reached without a symlink; [] otherwise.
        Only plain names (no dot files); what each is, link_into checks."""
        try:
            fd, name = self._parent(rel + "/x")
        except OSError:
            return []
        try:
            return sorted(n for n in os.listdir(fd)
                          if not n.startswith(".") and "/" not in n and not n.endswith(PARTIAL))
        except OSError:
            return []
        finally:
            os.close(fd)

    def link_into(self, rel: str, dest: str) -> bool:
        """Hard-link the regular file <rel> to <dest> (a path in the new tree, which only root
        can change). The new name is checked after the link: a symlink swapped in meanwhile
        is unlinked again, not kept."""
        try:
            fd, name = self._parent(rel)
        except OSError:
            return False
        try:
            os.link(name, dest, src_dir_fd=fd, follow_symlinks=False)
        except OSError:
            return False
        finally:
            os.close(fd)
        st = os.lstat(dest)
        if not stat.S_ISREG(st.st_mode):
            os.unlink(dest)
            return False
        try:
            xattrs = os.listxattr(dest, follow_symlinks=False)
        except OSError:
            xattrs = ["?"]
        if not _carry_owner_ok(st.st_uid, st.st_gid, st.st_mode, xattrs, self.uid):
            os.unlink(dest)
            say(f"not carried over: {rel} (uid {st.st_uid}, mode {stat.S_IMODE(st.st_mode):o}: "
                "neither the old tree owner's nor a root file that can be carried as it is)")
            return False
        return True


def _carry_owner_ok(uid: int, gid: int, mode: int, xattrs, tree_uid: int) -> bool:
    """Whose file a carried link may be. A carried inode is the same file under every name
    it has, so what the rebuild does to it, it does to every other name too.
      - The old tree's owner's (the run user's) is made root's with a fixed mode (_finish):
        theirs to give.
      - Root's is carried only as it already is, and never changed: root:root, readable by
        everyone, writable by nobody else, no set-id or sticky bit, no extended attribute
        (no ACL, no file capability). Such a file the run user could read anyway, so
        carrying it exposes nothing, however it got into their tree (root put it there, or
        they linked or renamed it in), and leaving it untouched changes nothing of anyone
        else's.
      - Anyone else's: not carried.
    Checked on the new name, after the link: the inode is fixed once linked."""
    if uid == tree_uid and uid != 0:
        return True
    if uid != 0:
        return False
    return (gid == 0 and bool(mode & stat.S_IROTH) and not mode & 0o7022 and not xattrs)


def _strip_acl(path: str) -> None:
    for name in ACL_XATTRS:
        try:
            os.removexattr(path, name, follow_symlinks=False)
        except OSError as e:
            if e.errno not in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise


def _mkdirs(base: str, rel: str, owner) -> None:
    path = base
    for part in [p for p in os.path.dirname(rel).split("/") if p]:
        path = os.path.join(path, part)
        if not os.path.isdir(path):
            os.mkdir(path, 0o755)
            os.chown(path, *owner)
            os.chmod(path, 0o755)


def _finish(path: str, executable: bool, owner) -> None:
    os.chown(path, *owner, follow_symlinks=False)
    _strip_acl(path)
    os.chmod(path, 0o755 if executable else 0o644)


def _copy(old: Tree, rel: str, dest: str, owner, limit=None, md5=None) -> bool:
    f = old.open_file(rel)
    if f is None:
        return False
    digest = hashlib.md5() if md5 else None  # noqa: S324 -- dpkg's own checksum, not security
    size = 0
    try:
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        src = os.fstat(f)
        executable = bool(src.st_mode & 0o111)
        try:
            while chunk := os.read(f, 1 << 20):
                size += len(chunk)
                if limit is not None and size > limit:
                    raise OSError(errno.EFBIG, rel)
                if digest:
                    digest.update(chunk)
                os.write(out, chunk)
        finally:
            os.close(out)
    except OSError as e:
        if os.path.lexists(dest):
            os.unlink(dest)
        if e.errno in WRITE_ERRORS:
            raise   # the new tree could not be written: nothing is swapped in
        return False
    finally:
        os.close(f)
    if digest and digest.hexdigest() != md5:
        os.unlink(dest)
        return False
    _finish(dest, executable, owner)
    # The source's times: a .py newer than its packaged .pyc would make python recompile it
    # on every start (the run user cannot write the cache in a root tree).
    os.utime(dest, ns=(src.st_atime_ns, src.st_mtime_ns))
    return True


def _dpkg_files(package: str):
    """(paths dpkg lists for <package>, {path: md5}); both empty when it is not installed."""
    p = subprocess.run(["dpkg-query", "-L", package], capture_output=True, text=True)
    if p.returncode != 0:
        return [], {}
    paths = [ln.rstrip("/") for ln in p.stdout.splitlines() if ln.startswith("/")]
    sums = {}
    for name in (f"{package}.md5sums", f"{package}:arm64.md5sums", f"{package}:amd64.md5sums"):
        try:
            with open(os.path.join("/var/lib/dpkg/info", name), encoding="utf-8") as fh:
                for line in fh:
                    md5, _, path = line.rstrip("\n").partition("  ")
                    sums["/" + path] = md5
            break
        except OSError:
            continue
    return paths, sums


def _rebuild_brain(old: Tree, prefix: str, new: str, package: str, owner) -> tuple[int, int]:
    """The brain package's tree in <new>/brain from what dpkg lists: (taken, left out).
    Every directory is made new (a real one, whatever the old tree had there); a regular
    file is copied when its MD5 is dpkg's; of symlinks, only the venv's own, each to the
    one target it has (VENV_LINKS). Any other symlink is left out: a
    link that leads anywhere but where dpkg put it would have dpkg itself write through it,
    as root, on the next install of the package."""
    paths, sums = _dpkg_files(package)
    tree = prefix + "/brain"
    taken = left = 0
    for path in sorted(p for p in paths if p == tree or p.startswith(tree + "/")):
        rel = os.path.relpath(path, prefix)
        dest = os.path.join(new, rel)
        st = old.lstat(rel)
        if st is None:
            left += 1
            continue
        _mkdirs(new, rel, owner)
        if stat.S_ISDIR(st.st_mode):
            if not os.path.isdir(dest):
                os.mkdir(dest, 0o755)
                os.chown(dest, *owner)
                os.chmod(dest, 0o755)
            taken += 1
        elif stat.S_ISLNK(st.st_mode):
            target = old.readlink(rel)
            allowed = VENV_LINKS.get(rel) or (VENV_LINKS["brain/bin/python3"]
                                              if VENV_VERSIONED_LINK.fullmatch(rel) else None)
            if target and allowed and allowed.fullmatch(target):
                os.symlink(target, dest)
                os.chown(dest, *owner, follow_symlinks=False)
                taken += 1
            else:
                left += 1
        elif stat.S_ISREG(st.st_mode) and path in sums and _copy(old, rel, dest, owner, md5=sums[path]):
            taken += 1
        else:
            left += 1
    return taken, left


def _plain_dir(path: str) -> bool:
    """<path> is a directory of its own: not a symlink, not a mount point (a rename and a
    hard link must stay on its parent's filesystem)."""
    st = os.lstat(path)
    return stat.S_ISDIR(st.st_mode) and not os.path.ismount(path) \
        and st.st_dev == os.lstat(os.path.dirname(path) or "/").st_dev


def _settle(path: str, owner) -> None:
    """What a run stopped half way left beside <path>: a finished new tree (it has its
    marker) with no <path> is put in place (a stop between the two renames); every other
    .new-* is removed (it is root's own, never swapped in), and every .pre-root-* (the old
    tree, already replaced)."""
    parent, base = os.path.dirname(path) or "/", os.path.basename(path)
    for name in sorted(os.listdir(parent), reverse=True):
        full = os.path.join(parent, name)
        if name.startswith(base + ".new-") and not os.path.lexists(path):
            st = os.lstat(full)
            mark = os.path.join(full, MARK)
            if stat.S_ISDIR(st.st_mode) and st.st_uid == owner[0] and os.path.isfile(mark) \
                    and not os.path.islink(mark):
                os.rename(full, path)
                say(f"put {path} back from a run that stopped between its renames")
                continue
        if name.startswith(base + ".new-") and os.path.isdir(full) and not os.path.islink(full) \
                and os.path.isdir(path) and not os.path.islink(path):
            # Stopped after the run user's entries moved in and before the swap: they go
            # back where they were, not out with the unfinished tree.
            for entry in sorted(os.listdir(full)):
                if entry not in MANAGED and not entry.startswith(".") \
                        and not os.path.lexists(os.path.join(path, entry)):
                    os.rename(os.path.join(full, entry), os.path.join(path, entry))
        if name.startswith(base + ".new-") or name.startswith(base + ".pre-root-"):
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                os.unlink(full)


def _fresh(path: str, stamp: str, owner) -> str:
    """A new, empty tree beside <path>, root's and 0700 until it is swapped in: nobody else
    can even look into it while it is built."""
    new = f"{path}.new-{stamp}"
    os.mkdir(new, 0o700)
    os.chown(new, *owner)
    os.chmod(new, 0o700)
    return new


def _swap(path: str, new: str, stamp: str) -> str | None:
    """<new> becomes <path> (opened up to 0755 first); the old one, if any, is renamed aside
    and returned."""
    os.chmod(new, 0o755)
    aside = None
    if os.path.lexists(path):
        aside = f"{path}.pre-root-{stamp}"
        os.rename(path, aside)
    os.rename(new, path)
    return aside


def _tree_done(path: str, owner) -> bool:
    """<path> is root's own directory with this script's marker (root's regular file)."""
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or (st.st_uid, st.st_gid) != owner or st.st_mode & 0o022:
            return False
        st = os.lstat(os.path.join(path, MARK))
        return stat.S_ISREG(st.st_mode) and st.st_uid == owner[0]
    except OSError:
        return False


def done(prefix: str, state: str, owner=(0, 0)) -> bool:
    return _tree_done(prefix, owner) and _tree_done(state, owner)


COMPLETE, INCOMPLETE_TEXT = "rebuilt as root's\n", "rebuilt as root's; brain incomplete\n"


def _mark(new: str, owner, text: str = COMPLETE) -> None:
    """The marker. Its text says whether the brain package's tree came over whole; install.sh
    writes COMPLETE over it once it has installed the package (brain_tree_complete)."""
    with open(os.open(os.path.join(new, MARK), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), "w") as f:
        f.write(text)
    os.chown(os.path.join(new, MARK), *owner)


def _brain_incomplete(prefix: str) -> bool:
    try:
        with open(os.path.join(prefix, MARK), encoding="utf-8") as f:
            return f.read() == INCOMPLETE_TEXT
    except OSError:
        return False


def _rebuild_prefix(prefix: str, package: str, stamp: str, owner) -> int:
    """$PREFIX rebuilt and swapped in. Returns how many brain package paths were left out."""
    new = _fresh(prefix, stamp, owner)
    moved, left = [], 0
    try:
        if os.path.lexists(prefix):
            old = Tree(prefix)
            try:
                n = 0
                extra = [f"{d}/{n}" for d in ENGINE_DIRS for n in old.names(d)]
                for rel in dict.fromkeys(ENGINE + tuple(extra)):
                    _mkdirs(new, rel, owner)
                    if old.link_into(rel, os.path.join(new, rel)):
                        st = os.lstat(os.path.join(new, rel))
                        if st.st_uid != 0:   # root's own is carried as it is (_carry_owner_ok)
                            _finish(os.path.join(new, rel), bool(st.st_mode & 0o111), owner)
                        n += 1
                manifest = sum(_copy(old, rel, os.path.join(new, rel), owner, limit=SMALL) for rel in COPIED)
                taken, left = _rebuild_brain(old, prefix, new, package, owner)
                say(f"carried over {n} engine file(s){' and the manifest' if manifest else ''}, and "
                    f"{taken} path(s) of {package}"
                    + (f"; {left} of its paths were missing, not as dpkg recorded them, or links "
                       "elsewhere (install.sh --only brain puts them back)" if left else ""))
                moved = [name for name in sorted(os.listdir(prefix))
                         if name not in MANAGED and not name.startswith(".")]
            finally:
                os.close(old.fd)
        for d in ENGINE_DIRS:
            _mkdirs(new, d + "/x", owner)
        _mark(new, owner, INCOMPLETE_TEXT if left else COMPLETE)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    # The run user's own entries move over as they are, right before the swap (a running
    # pre-package brain finds its venv again a moment later). A failure here is put back.
    done_moves = []
    try:
        for name in moved:
            os.rename(os.path.join(prefix, name), os.path.join(new, name))
            done_moves.append(name)
    except BaseException:
        for name in done_moves:
            os.rename(os.path.join(new, name), os.path.join(prefix, name))
        shutil.rmtree(new, ignore_errors=True)
        raise
    if moved:
        say(f"left as the run user's: {', '.join(moved)}")
    aside = _swap(prefix, new, stamp)
    if aside:
        shutil.rmtree(aside, ignore_errors=True)
    return left


def _rebuild_state(state: str, stamp: str, owner) -> None:
    new = _fresh(state, stamp, owner)
    try:
        if os.path.lexists(state):
            old = Tree(state)
            try:
                for name in STATE_KEPT_JSON:
                    dest = os.path.join(new, name)
                    if _copy(old, name, dest, owner, limit=SMALL):
                        try:
                            with open(dest, encoding="utf-8") as fh:
                                ok = isinstance(json.load(fh), dict)
                        except ValueError:
                            ok = False
                        if not ok:
                            os.unlink(dest)
                for name in STATE_KEPT_TEXT:
                    _copy(old, name, os.path.join(new, name), owner, limit=SMALL)
                for name in sorted(os.listdir(state)):
                    if name.startswith("EULA-v") and name.endswith(".txt"):
                        _copy(old, name, os.path.join(new, name), owner, limit=SMALL)
            finally:
                os.close(old.fd)
        _mark(new, owner)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    aside = _swap(state, new, stamp)
    if aside:
        shutil.rmtree(aside, ignore_errors=True)


def migrate(prefix: str, state: str, package: str, owner=(0, 0)) -> int:
    """0 when both trees are root's (now or already), INCOMPLETE when $PREFIX was rebuilt
    but the brain package's tree lacks files. Raises on a layout it does not rebuild (a
    symlinked or separately mounted tree) and on a failure writing the new tree; nothing is
    swapped in then."""
    for path in (prefix, state):
        _settle(path, owner)
    if done(prefix, state, owner):
        return INCOMPLETE if _brain_incomplete(prefix) else 0
    for path in (prefix, state):
        if os.path.lexists(path) and not _plain_dir(path):
            raise SystemExit(f"root layout: {path} is a symlink or a mount point; this does not "
                             "rebuild it. Make it a plain directory (or move its contents into one) "
                             "and run the installer again")
    stamp = time.strftime("%Y%m%d%H%M%S")
    say(f"rebuilding {prefix} and {state} as root's (installs before this one gave them "
        "to the run user; only what can be checked is carried over)")
    left = 0
    if not _tree_done(prefix, owner):
        left = _rebuild_prefix(prefix, package, stamp, owner)
    if not _tree_done(state, owner):
        _rebuild_state(state, stamp, owner)
    say("done")
    return INCOMPLETE if _brain_incomplete(prefix) else 0


def main(argv: list[str]) -> int:
    if len(argv) not in (4, 6) or argv[0] != "migrate":
        print("usage: root_layout.py migrate <prefix> <state> <brain-package> [<uid> <gid>]", file=sys.stderr)
        return 2
    owner = (int(argv[4]), int(argv[5])) if len(argv) == 6 else (0, 0)
    return migrate(argv[1].rstrip("/"), argv[2].rstrip("/"), argv[3], owner)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
