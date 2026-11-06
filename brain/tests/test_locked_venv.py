#
# locked_venv.py (#220): the set the suite's preflight expects is what `uv sync --locked`
# installs on this interpreter, and a venv that differs from it gets one line naming
# the venv, the difference, and the command that puts it right.
#
# The locks here are small hand-written ones in uv.lock's shape; the venv is a fake
# site-packages of dist-info records, as in test_lockcheck.py.
#
# Run: python test_locked_venv.py   (or via pytest test_suite.py)
#
import os
import sys

import locked_venv
from tempdirs import tempdir

PROJECT = {"project": {"name": "teaport_brain"}}
NOWHERE = "sys_platform == 'no-such-platform'"


def _lock(*packages):
    return {"package": [dict(p) for p in packages]}


ROOT = {"name": "teaport-brain", "version": "0.0.0", "source": {"editable": "."},
        "dependencies": [{"name": "app", "extra": ["web"]}, {"name": "winonly", "marker": NOWHERE}],
        "dev-dependencies": {"dev": [{"name": "pytest"}], "docs": [{"name": "sphinx"}]}}


def test_the_set_follows_markers_extras_and_the_default_groups():
    lock = _lock(ROOT,
                 {"name": "app", "version": "1.0", "dependencies": [{"name": "core"}],
                  "optional-dependencies": {"web": [{"name": "server"}], "cli": [{"name": "click"}]}},
                 {"name": "core", "version": "2.0"}, {"name": "server", "version": "3.0"},
                 {"name": "click", "version": "8.0"}, {"name": "winonly", "version": "1.0"},
                 {"name": "pytest", "version": "9.0", "dependencies": [{"name": "core"}]},
                 {"name": "sphinx", "version": "7.0"})
    # The project under its own name however pyproject spells it; app with the extra its
    # edge asks for and not the other; nothing behind a marker that fails here; the dev
    # group, which uv syncs by default, and not the docs group.
    assert locked_venv.locked(lock, PROJECT) == {
        "teaport-brain": "0.0.0", "app": "1.0", "core": "2.0", "server": "3.0", "pytest": "9.0"}
    groups = {**PROJECT, "tool": {"uv": {"default-groups": "all"}}}
    assert locked_venv.locked(lock, groups)["sphinx"] == "7.0"
    assert "pytest" not in locked_venv.locked(lock, {**PROJECT, "tool": {"uv": {"default-groups": []}}})


def test_an_extra_reached_after_its_package_is_still_followed():
    root = {**ROOT, "dependencies": [{"name": "a"}, {"name": "b"}], "dev-dependencies": {}}
    lock = _lock(root, {"name": "a", "version": "1", "dependencies": [{"name": "lib"}]},
                 {"name": "b", "version": "1", "dependencies": [{"name": "lib", "extra": ["x"]}]},
                 {"name": "lib", "version": "1", "optional-dependencies": {"x": [{"name": "plug"}]}},
                 {"name": "plug", "version": "1"})
    assert set(locked_venv.locked(lock, PROJECT)) == {"teaport-brain", "a", "b", "lib", "plug"}


def test_a_package_locked_at_two_versions_takes_the_one_its_edge_names():
    root = {**ROOT, "dependencies": [{"name": "numpy", "version": "2.0", "marker": NOWHERE},
                                     {"name": "numpy", "version": "1.26"}],
            "dev-dependencies": {}}
    lock = _lock(root, {"name": "numpy", "version": "2.0"}, {"name": "numpy", "version": "1.26"})
    assert locked_venv.locked(lock, PROJECT)["numpy"] == "1.26"


def _site(*records):
    d = tempdir("teaport-locked-")
    for name, version in records:
        info = os.path.join(d, f"{name}-{version}.dist-info")
        os.makedirs(info)
        with open(info + "/METADATA", "w") as f:
            f.write(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return d


def _checkout(root, *packages):
    """A brain/ of its own: a uv.lock and pyproject.toml in the shape of the real ones."""
    brain = tempdir("teaport-locked-brain-")
    with open(os.path.join(brain, "pyproject.toml"), "w") as f:
        f.write('[project]\nname = "teaport-brain"\n')
    with open(os.path.join(brain, "uv.lock"), "w") as f:
        f.write("version = 1\n")
        for p in (root, *packages):
            f.write(f'\n[[package]]\nname = "{p[0]}"\nversion = "{p[1]}"\n')
            if p[2:]:
                f.write("dependencies = [" + ", ".join(f'{{ name = "{d}" }}' for d in p[2]) + "]\n")
    return brain


def _problem(brain, site, prefix):
    names = ("BRAIN", "LOCK", "PYPROJECT", "site_dirs")
    saved = [getattr(locked_venv, n) for n in names] + [sys.prefix]
    locked_venv.BRAIN, locked_venv.LOCK = brain, os.path.join(brain, "uv.lock")
    locked_venv.PYPROJECT = os.path.join(brain, "pyproject.toml")
    locked_venv.site_dirs = lambda: [site]
    sys.prefix = prefix
    try:
        return locked_venv.problem()
    finally:
        for n, v in zip(names, saved):
            setattr(locked_venv, n, v)
        sys.prefix = saved[-1]


def test_a_venv_off_the_lock_gets_one_line_naming_it_and_the_fix():
    brain = _checkout(("teaport-brain", "0.0.0", ["h2", "numpy", "vadersentiment"]),
                      ("h2", "4.4.1"), ("numpy", "2.1.0"), ("vadersentiment", "3.3.2"))
    whole = _site(("teaport_brain", "0.0.0"), ("h2", "4.4.1"), ("numpy", "2.1.0"),
                  ("vaderSentiment", "3.3.2"))
    assert _problem(brain, whole, os.path.join(brain, ".venv")) is None
    # Two locked packages lost (#220's venv), one at another version, one the lock has
    # never heard of: all drift, and the project's own venv is fixed by a plain sync.
    stale = _site(("teaport_brain", "0.0.0"), ("numpy", "1.26.4"), ("six", "1.17.0"))
    line = _problem(brain, stale, os.path.join(brain, ".venv"))
    assert "\n" not in line, line
    assert line.endswith(f"is out of sync with {locked_venv._shown(os.path.join(brain, 'uv.lock'))} "
                         f"(numpy 2.1.0 -> 1.26.4, +six 1.17.0, -h2 4.4.1, -vadersentiment 3.3.2): "
                         f"run `uv sync --locked --project {locked_venv._shown(brain)}`"), line
    # Any other venv has to be named to uv.
    other = tempdir("teaport-locked-venv-")
    line = _problem(brain, stale, other)
    assert line.startswith(f"{other} is out of sync"), line
    assert f"run `UV_PROJECT_ENVIRONMENT={other} uv sync --locked" in line, line


def test_a_tests_dir_away_from_its_checkout_is_not_checked():
    brain = tempdir("teaport-locked-detached-")
    assert _problem(brain, _site(), brain) is None


def test_this_venv_holds_the_locks_set():
    # What the suite's conftest checks, here for a run of this script on its own.
    assert locked_venv.problem() is None, locked_venv.problem()


def main():
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
