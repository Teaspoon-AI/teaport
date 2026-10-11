#
# tempdirs.py — temp dirs a test script gets rid of when it exits (issue #135).
#
# The scripts used to mkdtemp and leave the dirs behind (thousands of teaport-etc-,
# -home-, -mig-, -apply- in a desktop's /tmp). A TemporaryDirectory held here until the
# interpreter exits is removed then, by tempfile's own finalizer, which also puts the
# permissions back first: a mode-000 file or a read-only dir a test staged goes too.
# Gone on a normal exit, sys.exit or an uncaught exception; not on SIGKILL.
#
# test_suite.py runs each script with a TMPDIR of its own and fails it if anything
# named teaport-* is left there, so keep the default prefix (or another teaport-one).
#
import tempfile

_HELD: list[tempfile.TemporaryDirectory] = []


def tempdir(prefix: str = "teaport-test-") -> str:
    """A fresh dir (mode 0700), removed with everything in it when the script exits."""
    d = tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)
    _HELD.append(d)
    return d.name
