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
# left there. That TMPDIR is made short enough for short_tempdir() (short_base), so
# under the suite every dir here lands inside it, where the check sees it.
#
import atexit
import tempfile

# AF_UNIX paths stop at 107 bytes; keep room under the dir for a socket's name.
_SOCKET_DIR_MAX = 90
_PREFIX_MAX = 20
# The longest base a short_tempdir() fits under: its prefix, a slash, mkdtemp's 8 characters.
_BASE_MAX = _SOCKET_DIR_MAX - _PREFIX_MAX - 9


def tempdir(prefix: str = "teaport-test-", dir: str | None = None) -> str:
    """A fresh dir (mode 0700), removed with everything in it when the script exits."""
    d = tempfile.TemporaryDirectory(prefix=prefix, dir=dir, ignore_cleanup_errors=True)
    atexit.register(d.cleanup)
    return d.name


def short_base(room: int = 0) -> str:
    """$TMPDIR when a short_tempdir() fits under it with `room` more characters of path
    on the way (test_suite.py's own dir), else /tmp."""
    base = tempfile.gettempdir()
    return base if len(base) + room <= _BASE_MAX else "/tmp"


def short_tempdir(prefix: str = "teaport-sock-") -> str:
    """A tempdir() short enough to bind AF_UNIX sockets in (see short_base)."""
    assert len(prefix) <= _PREFIX_MAX, f"short_tempdir prefix longer than {_PREFIX_MAX}: {prefix}"
    return tempdir(prefix, dir=short_base())
