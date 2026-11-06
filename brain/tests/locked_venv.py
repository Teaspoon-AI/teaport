#
# Refuse to run the suite in a venv that does not hold what brain/uv.lock installs (#220).
#
# A venv that has drifted from the lock fails wherever a missing package is first
# imported: the main checkout's brain/.venv had lost h2 and vaderSentiment, both locked,
# and ten tests failed with ModuleNotFoundError (llm_http2.py, the VADER mood path) with
# nothing pointing at the venv. Each one looked like a regression. conftest.py asks
# problem() before pytest runs anything, so the suite stops at the start with one line
# that names the venv, what differs, and the command that rebuilds it.
#
# "What the lock installs" is what `uv sync --locked` puts in a venv on this interpreter:
# the project, the packages reachable from it along the edges whose markers hold here,
# the extras those edges ask for, and the dependency groups uv syncs by default (dev, for
# the locked pytest). lockcheck.drift compares that with the venv's site-packages, by the
# name normalization `teaport doctor` uses, so a package missing, at another version,
# recorded twice, or not in the lock at all is drift here as it is there. Versions only:
# an editable and a --no-editable (CI's) install of teaport-brain are both 0.0.0.
#
import importlib.util
import os
import re
import sys
import sysconfig
import tomllib
from functools import cache

from packaging.markers import Marker  # pytest's own dependency, so it is here whenever pytest is

BRAIN = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
LOCK = os.path.join(BRAIN, "uv.lock")
PYPROJECT = os.path.join(BRAIN, "pyproject.toml")
LOCKCHECK = os.path.join(BRAIN, "teaport_brain", "lockcheck.py")


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


def site_dirs() -> list[str]:
    """The running venv's own site-packages. Not sys.path, which can hold the checkout:
    the brain/teaport_brain.egg-info a setuptools build leaves there would read as a
    second record of teaport-brain."""
    return sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})


def _shown(path: str) -> str:
    rel = os.path.relpath(path)
    return path if rel.startswith("..") else rel


def problem() -> str | None:
    """One line saying how this venv differs from the lock and what rebuilds it, or None
    when it holds exactly the lock's set. None too for a tests/ copied away from the
    checkout (to a box, say), where there is no lock to compare with."""
    if not os.path.isfile(LOCK):
        return None
    with open(LOCK, "rb") as f:
        lock = tomllib.load(f)
    with open(PYPROJECT, "rb") as f:
        pyproject = tomllib.load(f)
    drift = _lockcheck().drift(locked(lock, pyproject), site_dirs())
    if not drift:
        return None
    # The command for this venv: brain/.venv is the project's own, any other (a
    # UV_PROJECT_ENVIRONMENT one) has to be named to uv, by its absolute path.
    venv = _shown(sys.prefix)
    own = os.path.realpath(sys.prefix) == os.path.realpath(os.path.join(BRAIN, ".venv"))
    env = "" if own else f"UV_PROJECT_ENVIRONMENT={os.path.abspath(sys.prefix)} "
    shown = ", ".join(drift[:5]) + (f", … (+{len(drift) - 5} more)" if len(drift) > 5 else "")
    return (f"{venv} is out of sync with {_shown(LOCK)} ({shown}): "
            f"run `{env}uv sync --locked --project {_shown(BRAIN)}`")
