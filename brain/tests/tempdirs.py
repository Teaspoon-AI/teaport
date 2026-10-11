#
# tempdirs.py — temp dirs a test script gets rid of when it exits (issue #135).
#
# The scripts used to mkdtemp and leave the dirs behind (thousands of teaport-etc-,
# -home-, -mig-, -apply- in a desktop's /tmp). Each dir here is a TemporaryDirectory
# whose cleanup runs at exit, and that cleanup puts the permissions back first: a
# mode-000 file or a read-only dir a test staged goes too. Gone on a normal exit,
# sys.exit or an uncaught exception; not on SIGKILL or os._exit.
# A forked child that exits through sys.exit would delete its parent's dirs (nothing forks).
#
# test_suite.py runs each script with a TMPDIR of its own and fails it if anything is
# left there.
#
import atexit
import os
import tempfile

# AF_UNIX paths stop at 107 bytes; keep room under the dir for a socket's name.
_SOCKET_DIR_MAX = 90


def tempdir(prefix: str = "teaport-test-", dir: str | None = None) -> str:
    """A fresh dir (mode 0700), removed with everything in it when the script exits."""
    d = tempfile.TemporaryDirectory(prefix=prefix, dir=dir, ignore_cleanup_errors=True)
    atexit.register(d.cleanup)
    return d.name


def short_tempdir(prefix: str = "teaport-sock-") -> str:
    """A tempdir() short enough to bind AF_UNIX sockets in: under $TMPDIR when that
    leaves room (a test_suite.py run adds ~23 bytes to it), else under /tmp."""
    base = tempfile.gettempdir()
    if len(os.path.join(base, prefix)) + 8 > _SOCKET_DIR_MAX:   # mkdtemp adds 8 characters
        base = "/tmp"
    return tempdir(prefix, dir=base)
