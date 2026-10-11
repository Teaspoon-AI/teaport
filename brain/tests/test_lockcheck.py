#
# teaport_brain.lockcheck (issue #59): what a brain venv holds, recorded at build time and
# diffed by `teaport doctor`. Two records of one package (a half-finished upgrade leaves
# the new dist-info beside the old one's) must show as drift, not collapse into whichever
# was read last; and a build that holds them must not be recorded as good.
#
# The venvs are fake site-packages dirs of dist-info records, read through the same
# importlib.metadata the module uses.
#
# Run: python test_lockcheck.py   (or via pytest test_suite.py)
#
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from teaport_brain import lockcheck  # noqa: E402
from tempdirs import tempdir  # noqa: E402


def _site(*records: tuple[str, str]) -> str:
    """A site-packages dir holding one dist-info per (name, version); "" = no Name."""
    d = tempdir("teaport-lock-")
    for i, (name, version) in enumerate(records):
        info = os.path.join(d, f"{name or 'nameless'}-{version}-{i}.dist-info")
        os.makedirs(info)
        with open(os.path.join(info, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\n" + (f"Name: {name}\n" if name else "") + f"Version: {version}\n")
    return d


def test_one_record_per_package_is_read_as_it_is():
    site = _site(("NumPy", "2.1.0"), ("typing_extensions", "4.12.2"))
    assert lockcheck.installed([site]) == {"numpy": "2.1.0", "typing-extensions": "4.12.2"}
    assert lockcheck.drift({"numpy": "2.1.0", "typing-extensions": "4.12.2"}, [site]) == []
    # The same record reached twice (its dir twice on the path, or through a link such
    # as a venv's lib64 -> lib) is still one record.
    link = os.path.join(tempdir("teaport-lock-"), "lib64")
    os.symlink(site, link)
    assert lockcheck.installed([site, site, link]) == lockcheck.installed([site])


def test_two_records_of_one_package_are_drift_whichever_is_read_last():
    for order in ((("numpy", "1.26.4"), ("numpy", "2.1.0")), (("numpy", "2.1.0"), ("numpy", "1.26.4"))):
        site = _site(*order)
        assert lockcheck.installed([site]) == {"numpy": "1.26.4 + 2.1.0"}, order
        assert lockcheck.drift({"numpy": "2.1.0"}, [site]) == ["numpy 2.1.0 -> 1.26.4 + 2.1.0"], order
    # The same version twice (a re-install that left its old record) counts too.
    assert lockcheck.drift({"numpy": "2.1.0"}, [_site(("numpy", "2.1.0"), ("numpy", "2.1.0"))]) \
        == ["numpy 2.1.0 -> 2.1.0 + 2.1.0"]


def test_a_local_version_label_is_not_a_duplicate():
    have = lockcheck.installed([_site(("torch", "2.5.0+cpu"))])
    assert have == {"torch": "2.5.0+cpu"} and lockcheck.damaged(have) == []


def test_a_nameless_record_is_drift_and_damage():
    site = _site(("numpy", "2.1.0"), ("", "0"))
    d = lockcheck.drift({"numpy": "2.1.0"}, [site])
    assert len(d) == 1 and d[0].startswith("+?broken:") and d[0].endswith(" ?"), d
    assert len(lockcheck.damaged(lockcheck.installed([site]))) == 1


def _record(site):
    """`record` in a venv holding <site> alone (it reads sys.path, as in a real venv)."""
    out = os.path.join(tempdir("teaport-lock-"), "teaport-build.json")
    saved = sys.path[:]
    sys.path[:] = [site]
    try:
        rc = lockcheck.main(["record", out, "--source", "s", "--revision", "r", "--lock-sha", "x", "--uv", "u"])
    finally:
        sys.path[:] = saved
    return rc, out


def test_a_build_holding_two_records_of_a_package_is_not_recorded():
    rc, out = _record(_site(("teaport-test-pkg", "1.0"), ("teaport-test-pkg", "2.0")))
    assert rc == 1 and not os.path.exists(out)


def test_a_clean_build_is_recorded_and_matches_itself():
    rc, out = _record(_site(("teaport-test-pkg", "1.0")))
    assert rc == 0
    with open(out) as f:
        rec = json.load(f)
    assert rec["installed"]["teaport-test-pkg"] == "1.0" and rec["revision"] == "r"


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
