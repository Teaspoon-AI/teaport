#
# locked_venv.py (#220): the set the suite's preflight expects is what `uv sync --locked`
# installs on this interpreter, and a venv that differs from it gets one line naming
# the venv, the difference, and what to do: a sync only into a venv of this project's,
# kept --no-editable if it was; `teaport doctor` for an installed brain; `uv run` for
# anyone else's venv. A `uv run --with` overlay is judged by the venv it extends, and a
# package the lock does not have is reported without stopping the run (review round 1).
#
# The locks here are small hand-written ones in uv.lock's shape; the venvs are fake ones,
# a pyvenv.cfg and a site-packages of dist-info records, as in test_lockcheck.py.
#
# Run: python test_locked_venv.py   (or via pytest test_suite.py)
#
import json
import os
import shutil

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


def _records(site, *records):
    for name, version in records:
        info = os.path.join(site, f"{name}-{version}.dist-info")
        os.makedirs(info)
        with open(os.path.join(info, "METADATA"), "w") as f:
            f.write(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return site


def _venv(*records, at=None, cfg="", build_record=False, editable=None):
    """A venv: its pyvenv.cfg, <records> in its site-packages, and as asked, an install's
    build record and teaport-brain's PEP 610 record."""
    venv = at or tempdir("teaport-locked-venv-")
    site = locked_venv.site_dirs(venv)[0]
    os.makedirs(site, exist_ok=True)
    with open(os.path.join(venv, "pyvenv.cfg"), "w") as f:
        f.write("include-system-site-packages = false\n" + cfg)
    _records(site, *records)
    if build_record:
        with open(os.path.join(venv, "teaport-build.json"), "w") as f:
            f.write("{}")
    if editable is not None:
        with open(os.path.join(site, "teaport_brain-0.0.0.dist-info", "direct_url.json"), "w") as f:
            json.dump({"url": "file:///src/brain", "dir_info": {"editable": editable}}, f)
    return venv


LOCKED = (("h2", "4.4.1"), ("numpy", "2.1.0"), ("pytest", "9.1.1"), ("vadersentiment", "3.3.2"))
WHOLE = (("teaport_brain", "0.0.0"), ("h2", "4.4.1"), ("numpy", "2.1.0"), ("pytest", "9.1.1"),
         ("vaderSentiment", "3.3.2"))
# #220's venv: two locked packages lost, and one at another version.
STALE = (("teaport_brain", "0.0.0"), ("numpy", "1.26.4"), ("pytest", "9.1.1"))
PROJECT_CFG = "prompt = teaport-brain\n"


def _checkout():
    """A brain/ of its own: a uv.lock and pyproject.toml in the shape of the real ones."""
    brain = tempdir("teaport-locked-brain-")
    with open(os.path.join(brain, "pyproject.toml"), "w") as f:
        f.write('[project]\nname = "teaport-brain"\n')
    with open(os.path.join(brain, "uv.lock"), "w") as f:
        f.write('version = 1\n\n[[package]]\nname = "teaport-brain"\nversion = "0.0.0"\n')
        f.write("dependencies = [" + ", ".join(f'{{ name = "{n}" }}' for n, _ in LOCKED) + "]\n")
        for name, version in LOCKED:
            f.write(f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n')
    return brain


def _check(brain, prefix, path=None):
    """check() as the conftest runs it, against <brain>'s lock, in the venv <prefix> on
    <path> (default: the venv's own site-packages, as for a run without an overlay)."""
    names = ("BRAIN", "LOCK", "PYPROJECT", "SUITE")
    saved = [getattr(locked_venv, n) for n in names]
    locked_venv.BRAIN, locked_venv.LOCK = brain, os.path.join(brain, "uv.lock")
    locked_venv.PYPROJECT = os.path.join(brain, "pyproject.toml")
    locked_venv.SUITE = os.path.join(brain, "tests", "test_suite.py")
    try:
        return locked_venv.check(prefix, locked_venv.site_dirs(prefix) if path is None else path)
    finally:
        for n, v in zip(names, saved):
            setattr(locked_venv, n, v)


def _line(brain, prefix, path=None):
    c = _check(brain, prefix, path)
    saved = locked_venv.BRAIN, locked_venv.LOCK, locked_venv.SUITE
    locked_venv.BRAIN, locked_venv.LOCK = brain, os.path.join(brain, "uv.lock")
    locked_venv.SUITE = os.path.join(brain, "tests", "test_suite.py")
    try:
        line = locked_venv.problem(c)
    finally:
        locked_venv.BRAIN, locked_venv.LOCK, locked_venv.SUITE = saved
    assert line is None or "\n" not in line, line
    return line


def test_the_projects_own_venv_off_the_lock_is_told_to_sync():
    brain = _checkout()
    own = os.path.join(brain, ".venv")
    _venv(*WHOLE, at=own, cfg=PROJECT_CFG)
    assert _line(brain, own) is None
    shutil.rmtree(own)
    _venv(*STALE, at=own, cfg=PROJECT_CFG)
    shown = locked_venv._shown
    assert _line(brain, own) == (
        f"{shown(own)} is out of sync with {shown(os.path.join(brain, 'uv.lock'))} "
        f"(numpy 2.1.0 -> 1.26.4, -h2 4.4.1, -vadersentiment 3.3.2): "
        f"run `uv sync --locked --project {shown(brain)}`")
    # Another venv of this project's (UV_PROJECT_ENVIRONMENT) has to be named to uv.
    other = _venv(*STALE, cfg=PROJECT_CFG)
    line = _line(brain, other)
    assert line.startswith(f"{other} is out of sync"), line
    assert f"run `UV_PROJECT_ENVIRONMENT={other} uv sync --locked --project" in line, line


def test_a_no_editable_venv_is_synced_no_editable():
    # CI's install, and install.sh's: a sync that dropped the flag would leave teaport-brain
    # an editable .pth into this checkout.
    brain = _checkout()
    line = _line(brain, _venv(*STALE, cfg=PROJECT_CFG, editable=False))
    assert "uv sync --locked --no-editable --project" in line, line
    line = _line(brain, _venv(*STALE, cfg=PROJECT_CFG, editable=True))
    assert "uv sync --locked --project" in line, line


def test_an_installed_brain_is_pointed_at_teaport_doctor_not_synced():
    # What an appliance venv looks like with pytest added by hand: --no-dev, --no-editable,
    # a build record. A sync into it would make teaport-brain editable against this
    # checkout and add the dev group.
    brain = _checkout()
    box = (("teaport_brain", "0.0.0"), ("h2", "4.4.1"), ("numpy", "2.1.0"), ("pytest", "8.0.0"),
           ("vaderSentiment", "3.3.2"))
    lines = [_line(brain, _venv(*box, cfg=PROJECT_CFG, build_record=True, editable=False))]
    # The .deb's venv, /opt/teaport/brain, has no build record: under the install prefix.
    prefix = tempdir("teaport-locked-opt-")
    saved = os.environ.get("TEAPORT_PREFIX")
    os.environ["TEAPORT_PREFIX"] = prefix
    try:
        lines.append(_line(brain, _venv(*box, at=os.path.join(prefix, "brain"), editable=False)))
    finally:
        if saved is None:
            os.environ.pop("TEAPORT_PREFIX")
        else:
            os.environ["TEAPORT_PREFIX"] = saved
    for line in lines:
        assert "is an installed brain's venv" in line and "`teaport doctor`" in line, line
        assert "uv sync" not in line and "uv run --locked --project" in line, line


def test_someone_elses_venv_is_not_synced_into():
    # pipx's pytest, or another project's venv: not this suite's to change.
    brain = _checkout()
    for cfg in ("", "prompt = some-other-project\n"):
        line = _line(brain, _venv(("pytest", "9.1.1"), ("pluggy", "1.6.0"), cfg=cfg))
        assert "is not this project's venv" in line and "uv sync" not in line, line
        shown = locked_venv._shown(brain)
        assert line.endswith(f"with `uv run --locked --project {shown} "
                             f"python -m pytest {shown}/tests/test_suite.py`"), line


def test_a_package_the_lock_lacks_is_reported_and_does_not_stop_the_run():
    brain = _checkout()
    c = _check(brain, _venv(*WHOLE, ("ipdb", "0.13.13"), cfg=PROJECT_CFG))
    assert c.problems == [] and c.extras == ["+ipdb 0.13.13"], c
    note = locked_venv.extras(c)
    assert note.endswith("does not install: +ipdb 0.13.13 (allowed)"), note
    assert locked_venv.header(c).endswith("(5 packages, 1 not in the lock)"), locked_venv.header(c)
    # Two records of one package is damage, not an extra.
    c = _check(brain, _venv(*WHOLE, ("numpy", "1.26.4"), cfg=PROJECT_CFG))
    assert c.problems == ["numpy 2.1.0 -> 1.26.4 + 2.1.0"], c


def test_an_overlay_is_judged_by_the_venv_it_extends():
    # `uv run --with pytest-timeout`: a temporary venv whose pyvenv.cfg names the real one,
    # and the --with packages in a cached env of their own, all on sys.path. It used to
    # be read as a venv missing every locked package, with a sync into the overlay.
    brain = _checkout()
    base = _venv(*WHOLE, cfg=PROJECT_CFG)
    for real in (base, _venv(*STALE, cfg=PROJECT_CFG)):
        overlay = _venv(cfg=f"extends-environment = {real}\n")
        added = _records(os.path.join(tempdir("teaport-locked-with-"), "site-packages"),
                         ("pytest_timeout", "2.4.0"), ("pytest", "8.3.0"))
        path = [locked_venv.site_dirs(overlay)[0], added, locked_venv.site_dirs(real)[0]]
        c = _check(brain, overlay, path)
        assert c.venv == real, c
        if real == base:
            # What the overlay adds, a pytest of its own choosing included, is its own.
            assert c.problems == [] and c.extras == [] and c.overlay == 2, c
            assert locked_venv.header(c).endswith("(5 packages, 2 more from a `uv run --with` overlay)")
        else:
            line = _line(brain, overlay, path)
            assert f"run `UV_PROJECT_ENVIRONMENT={real} uv sync" in line and overlay not in line, line


def test_a_tests_dir_away_from_its_checkout_is_not_checked():
    brain = tempdir("teaport-locked-detached-")
    assert _check(brain, _venv()) is None
    assert locked_venv.header(None) == "brain venv: not checked (no uv.lock beside tests/)"


def test_this_venv_holds_the_locks_set():
    # What the suite's conftest checks, here for a run of this script on its own.
    c = locked_venv.check()
    assert c.problems == [], locked_venv.problem(c)


def main():
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
