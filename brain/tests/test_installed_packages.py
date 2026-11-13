#
# locked_venv.installed / drift (issue #59): what a venv holds, as the test suite's lock
# check reads it. Two records of one package (a half-finished upgrade leaves the new
# dist-info beside the old one's) must show as drift, not collapse into whichever was read
# last; so must a nameless record, which an interrupted `pip install` can leave.
#
# The venvs are fake site-packages dirs of dist-info records, read through the same
# importlib.metadata the module uses.
#
# Run: python test_installed_packages.py   (or via pytest test_suite.py)
#
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import locked_venv  # noqa: E402
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
    assert locked_venv.installed([site]) == {"numpy": "2.1.0", "typing-extensions": "4.12.2"}
    assert locked_venv.drift({"numpy": "2.1.0", "typing-extensions": "4.12.2"}, [site]) == []
    # The same record reached twice (its dir twice on the path, or through a link such
    # as a venv's lib64 -> lib) is still one record.
    link = os.path.join(tempdir("teaport-lock-"), "lib64")
    os.symlink(site, link)
    assert locked_venv.installed([site, site, link]) == locked_venv.installed([site])


def test_two_records_of_one_package_are_drift_whichever_is_read_last():
    for order in ((("numpy", "1.26.4"), ("numpy", "2.1.0")), (("numpy", "2.1.0"), ("numpy", "1.26.4"))):
        site = _site(*order)
        assert locked_venv.installed([site]) == {"numpy": "1.26.4 + 2.1.0"}, order
        assert locked_venv.drift({"numpy": "2.1.0"}, [site]) == ["numpy 2.1.0 -> 1.26.4 + 2.1.0"], order
    # The same version twice (a re-install that left its old record) counts too.
    assert locked_venv.drift({"numpy": "2.1.0"}, [_site(("numpy", "2.1.0"), ("numpy", "2.1.0"))]) \
        == ["numpy 2.1.0 -> 2.1.0 + 2.1.0"]


def test_a_local_version_label_is_not_a_duplicate():
    have = locked_venv.installed([_site(("torch", "2.5.0+cpu"))])
    assert have == {"torch": "2.5.0+cpu"} and locked_venv.drift({"torch": "2.5.0+cpu"}, [_site(("torch", "2.5.0+cpu"))]) == []


def test_a_nameless_record_is_drift():
    site = _site(("numpy", "2.1.0"), ("", "0"))
    d = locked_venv.drift({"numpy": "2.1.0"}, [site])
    assert len(d) == 1 and d[0].startswith("+?broken:") and d[0].endswith(" ?"), d


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
