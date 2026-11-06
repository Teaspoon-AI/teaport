#
# Refuse to run the suite in a venv that does not hold what brain/uv.lock installs (#220).
#
# A venv that has drifted from the lock fails wherever a missing package is first
# imported: the main checkout's brain/.venv had lost h2 and vaderSentiment, both locked,
# and ten tests failed with ModuleNotFoundError (llm_http2.py, the VADER mood path) with
# nothing pointing at the venv. Each one looked like a regression. conftest.py runs
# check() before pytest runs anything, so the suite stops at the start with one line
# that names the venv, what differs, and what to do about it.
#
# "What the lock installs" is what `uv sync --locked` puts in a venv on this interpreter:
# the project, the packages reachable from it along the edges whose markers hold here,
# the extras those edges ask for, and the dependency groups uv syncs by default (dev, for
# the locked pytest). lockcheck.drift compares that with the venv's site-packages, by the
# name normalization `teaport doctor` uses. A package missing, at another version, or
# recorded twice stops the run. One the lock does not have is only reported: a debugger
# a developer added does not change what the tests test, and the one way past a stop
# would be a sync that deletes it. Versions only: an editable and a --no-editable (CI's)
# install of teaport-brain are both 0.0.0.
#
# Which venv, and which fix, are decided with care (review round 1), because the command
# the line suggests gets run:
#   - `uv run --with X` runs in a temporary overlay venv that extends the real one
#     (pyvenv.cfg's extends-environment). The real one is checked, and what the overlay
#     adds, at whatever version, is the run's own choice.
#   - An installed brain (a teaport-build.json beside it, or under /opt/teaport) is never
#     synced into: `teaport doctor` checks it against its own build record, and a sync
#     would make teaport-brain editable against this checkout and add the dev group, the
#     failure install.sh's --no-editable exists to prevent.
#   - A venv of this project's keeps how its teaport-brain was installed (--no-editable).
#   - Any other venv (pipx's pytest, another project's) is not this suite's to change:
#     the line says to run the suite in the project's own, through `uv run`.
#
import glob
import importlib.util
import json
import os
import re
import sys
import sysconfig
import tomllib
from dataclasses import dataclass
from functools import cache

from packaging.markers import Marker  # pytest's own dependency, so it is here whenever pytest is

BRAIN = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
LOCK = os.path.join(BRAIN, "uv.lock")
PYPROJECT = os.path.join(BRAIN, "pyproject.toml")
LOCKCHECK = os.path.join(BRAIN, "teaport_brain", "lockcheck.py")
SUITE = os.path.join(BRAIN, "tests", "test_suite.py")


def _lockcheck():
    # Loaded from the checkout by path, not imported from teaport_brain: a venv that lacks
    # the project itself is one of the states this reports, and lockcheck is stdlib only.
    spec = importlib.util.spec_from_file_location("_teaport_lockcheck", LOCKCHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@cache
def _holds(marker: str) -> bool:
    return Marker(marker).evaluate()


def locked(lock: dict, pyproject: dict) -> dict[str, str]:
    """{name: version} of what `uv sync --locked` installs from <lock> (a parsed uv.lock)
    on this interpreter, for the project <pyproject> declares."""
    by_name: dict[str, list[dict]] = {}
    for pkg in lock["package"]:
        by_name.setdefault(pkg["name"], []).append(pkg)
    root, = by_name[re.sub(r"[-_.]+", "-", pyproject["project"]["name"]).lower()]
    groups = pyproject.get("tool", {}).get("uv", {}).get("default-groups", ["dev"])
    if groups == "all":
        groups = list(root.get("dev-dependencies", {}))
    want: dict[str, str] = {}
    done = set()
    todo = [{"name": root["name"]}]
    todo += [d for g in groups for d in root.get("dev-dependencies", {}).get(g, [])]
    while todo:
        dep = todo.pop()
        if "marker" in dep and not _holds(dep["marker"]):
            continue
        # A package locked at two versions (a fork by Python version) is named on each
        # edge with the version that edge takes.
        pkg, = (p for p in by_name[dep["name"]] if dep.get("version", p["version"]) == p["version"])
        want[pkg["name"]] = pkg["version"]
        for extra in (None, *dep.get("extra", ())):
            if (pkg["name"], pkg["version"], extra) in done:
                continue
            done.add((pkg["name"], pkg["version"], extra))
            if extra is None:
                todo += pkg.get("dependencies", [])
            else:
                todo += pkg.get("optional-dependencies", {}).get(extra, [])
    return want


def _cfg(venv: str) -> dict[str, str]:
    """A venv's pyvenv.cfg, as key = value pairs ({} when it has none)."""
    try:
        with open(os.path.join(venv, "pyvenv.cfg"), encoding="utf-8") as f:
            return {k.strip(): v.strip() for k, sep, v in (ln.partition("=") for ln in f) if sep}
    except OSError:
        return {}


def base_venv(prefix: str) -> str:
    """The venv the lock is about: <prefix>, or, for a uv overlay (`uv run --with`), the
    venv its pyvenv.cfg says it extends."""
    seen = {prefix}
    while (up := _cfg(prefix).get("extends-environment")) and up not in seen:
        seen.add(up)
        prefix = up
    return prefix


def site_dirs(venv: str) -> list[str]:
    """<venv>'s own site-packages. Not sys.path, which can hold the checkout: the
    brain/teaport_brain.egg-info a setuptools build leaves there would read as a second
    record of teaport-brain."""
    where = {"base": venv, "platbase": venv}
    return sorted({sysconfig.get_path(k, vars=where) for k in ("purelib", "platlib")})


def _editable(dirs: list[str]) -> bool | None:
    """Whether teaport-brain in <dirs> was installed editable (its PEP 610 record), or None
    when that cannot be told."""
    for d in dirs:
        for info in glob.glob(os.path.join(d, "teaport_brain-*.dist-info")):
            try:
                with open(os.path.join(info, "direct_url.json"), encoding="utf-8") as f:
                    return bool(json.load(f).get("dir_info", {}).get("editable"))
            except (OSError, ValueError):
                return None
    return None


@dataclass
class Check:
    venv: str              # the venv checked (an overlay's base)
    kind: str              # "project", "installed" or "foreign": which fix to name
    locked: int            # packages the lock installs here
    problems: list[str]    # missing, at another version, recorded twice: these stop the run
    extras: list[str]      # in the venv but not in the lock: reported only
    overlay: int           # packages a `uv run --with` overlay adds
    editable: bool | None  # how the venv's teaport-brain was installed


def check(prefix: str | None = None, path: list[str] | None = None) -> Check | None:
    """The venv running this (<prefix>, on <path>) against the lock, or None for a tests/
    copied away from the checkout (to a box, say), where there is no lock to compare with."""
    if not os.path.isfile(LOCK):
        return None
    with open(LOCK, "rb") as f:
        lock = tomllib.load(f)
    with open(PYPROJECT, "rb") as f:
        pyproject = tomllib.load(f)
    project = re.sub(r"[-_.]+", "-", pyproject["project"]["name"]).lower()
    venv = base_venv(sys.prefix if prefix is None else prefix)
    base = site_dirs(venv)
    real = {os.path.realpath(d) for d in base}
    # What an overlay puts in front of the venv: every other site-packages on the path.
    added = [p for p in (sys.path if path is None else path)
             if os.path.basename(os.path.normpath(p)) in ("site-packages", "dist-packages")
             and os.path.realpath(p) not in real]
    lockcheck = _lockcheck()
    over = lockcheck.installed(added) if added else {}
    want = locked(lock, pyproject)
    problems, extras = [], []
    for d in lockcheck.drift({n: v for n, v in want.items() if n not in over}, base):
        name = d[1:].split(" ")[0]
        if d[0] != "+" or name.startswith("?broken:") or " + " in d:
            problems.append(d)
        elif name not in over:   # one the overlay shadows is not the run's
            extras.append(d)
    installs = os.path.realpath(os.environ.get("TEAPORT_PREFIX", "/opt/teaport")) + os.sep
    if os.path.isfile(os.path.join(venv, "teaport-build.json")) or \
            os.path.realpath(venv).startswith(installs):
        kind = "installed"
    elif (_own(venv) or _cfg(venv).get("prompt") == project
          or f"-{project} {want.get(project)}" not in problems):
        kind = "project"   # brain/.venv, a uv venv of this project's, or one holding it
    else:
        kind = "foreign"
    return Check(venv, kind, len(want), problems, extras, len(over), _editable(base))


def _own(venv: str) -> bool:
    return os.path.realpath(venv) == os.path.realpath(os.path.join(BRAIN, ".venv"))


def _shown(path: str) -> str:
    rel = os.path.relpath(path)
    return path if rel.startswith("..") else rel


def _some(entries: list[str]) -> str:
    return ", ".join(entries[:5]) + (f", … (+{len(entries) - 5} more)" if len(entries) > 5 else "")


def problem(c: Check) -> str | None:
    """One line saying how the venv differs from the lock and what to do, or None when
    nothing stops the run."""
    if not c.problems:
        return None
    venv, lock, what = _shown(c.venv), _shown(LOCK), _some(c.problems)
    run = f"`uv run --locked --project {_shown(BRAIN)} python -m pytest {_shown(SUITE)}`"
    if c.kind == "installed":
        return (f"{venv} is an installed brain's venv, not the set {lock} installs for the tests "
                f"({what}): check it with `teaport doctor` rather than syncing into it, and run "
                f"the suite with {run}")
    if c.kind == "foreign":
        return (f"{venv} is not this project's venv and does not hold what {lock} installs "
                f"({what}): run the suite in the project's own, with {run}")
    env = "" if _own(c.venv) else f"UV_PROJECT_ENVIRONMENT={os.path.abspath(c.venv)} "
    flag = " --no-editable" if c.editable is False else ""   # as it was installed
    return (f"{venv} is out of sync with {lock} ({what}): "
            f"run `{env}uv sync --locked{flag} --project {_shown(BRAIN)}`")


def extras(c: Check) -> str | None:
    """The line for packages the venv holds and the lock does not, or None."""
    if not c.extras:
        return None
    return (f"{_shown(c.venv)} also holds what {_shown(LOCK)} does not install: "
            f"{_some(c.extras)} (allowed)")


def header(c: Check | None) -> str:
    """pytest's header line: that the check ran, and what it found."""
    if c is None:
        return "brain venv: not checked (no uv.lock beside tests/)"
    if c.problems:
        return f"brain venv: {_shown(c.venv)} does not match {_shown(LOCK)}"
    more = f", {c.overlay} more from a `uv run --with` overlay" if c.overlay else ""
    more += f", {len(c.extras)} not in the lock" if c.extras else ""
    return f"brain venv: {_shown(c.venv)} matches {_shown(LOCK)} ({c.locked} packages{more})"
