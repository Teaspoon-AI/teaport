#
# teaport — is there a newer release, and what would installing it take?
#
# A release is a manifest (manifest/stable.json, manifest/beta.json: engine release, brain
# package, plugin version, OpenClaw version, models). install.sh copies the one it installed
# from to $PREFIX/manifest.json. This module diffs that against the channel's current one:
#   - the brain reads it once a day (Monitor, TEAPORT_UPDATE_CHECK_SECS), keeps the answer
#     in the state directory so a restart does not forget it, and shows it in /health's
#     "update" and `teaport status`;
#   - `teaport update --check` runs the same code on demand:
#         python -I -m teaport_brain.updates check [--json] [--channel stable|beta]
#     (the CLI's one comparison; there is no second copy of it in bash);
#   - `teaport update` applies what it can (plan(): the engine and the models with
#     `install.sh --only engine`, the brain package with `--only brain`), through the root
#     unit teaport-update.service (packaging/deb/update_apply.py), which reads the plan
#     from here too.
#
# A component the channel's manifest has not released yet ("TODO", empty, absent) is not an
# update, and is not shown as one. The brain is the teaport-brain package: the installed
# one is dpkg's, the latest the version in brain.deb's file name, and only a newer one is
# an update (a box on a newer dev build, or one a rollback took back, is offered nothing
# older than it has).
#
# What looking costs: one GET of a small JSON file from get.teaspoon.tech, the same address
# the installer reads. Never in a turn. A failed read keeps the last answer, marked stale.
#
# No Pipecat import (the CLI runs this under the brain venv's python, isolated).
#
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse

from loguru import logger

from teaport_brain.settings import parse_env, setting, state_dir

CHANNELS = ("stable", "beta")
MANIFEST_BASE = "https://get.teaspoon.tech/manifest"
STATE_FILE = "update.json"
FETCH_TIMEOUT_SECS = 20.0
# The download host refuses urllib's default agent ("Python-urllib/3.x") with a 403 and
# serves the same file to any other, so the look says who it is.
USER_AGENT = "teaport-brain (+https://github.com/Teaspoon-AI/teaport)"
# A read that failed is tried again after this, not after a day.
RETRY_SECS = 3600.0

# What a release is made of, in the order they are shown: (name, how to read it).
# "models" is the two model files' hashes: a model change rides an engine release, but the
# manifest can change one alone, and then the box needs it.
COMPONENTS = ("version", "engine", "brain", "plugin", "openclaw", "models")

# What `teaport update` installs by itself, in this order, and the components each covers:
# install.sh's two incremental runs, each of which puts the previous one back if the new
# one does not come up. The engine first: its restart takes the brain down and back up, and
# a new brain should come up on the engine it was released with. Any other change (the
# plugin, the OpenClaw version) is a full install.sh run, by hand.
STEPS = (("engine", ("engine", "models")), ("brain", ("brain",)))

BRAIN_PACKAGE = "teaport-brain"
# packaging/RELEASING.md names the file so: teaport-brain_<version>_<arch>.deb.
# An engine release dpkg can order ("1.6", "1.7"); update_apply.VERSION is the same.
_ENGINE_VERSION = re.compile(r"[0-9][A-Za-z0-9.+~-]*")
_DEB_NAME = re.compile(r"^teaport-brain_([0-9][A-Za-z0-9.+~:-]*)_[a-z0-9]+\.deb$")


# Where install.sh puts a box; the CLI passes its own (--prefix, --etc) when TEAPORT_PREFIX or
# TEAPORT_ETC moved them. They are not settings: the brain never reads them.
PREFIX = "/opt/teaport"
ETC = "/etc/teaport"


def installed_path() -> str:
    return os.path.join(PREFIX, "manifest.json")


def read_installed(path: str | None = None) -> dict:
    """The manifest the box was installed from: {} when there is none (not installed, or
    unreadable)."""
    try:
        with open(path or installed_path(), encoding="utf-8") as f:
            found = json.load(f)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def channel_of(configured: str | None, installed: dict) -> str:
    """The channel to look at: the setting, else the channel the installed manifest came
    from, else stable."""
    if configured in CHANNELS:
        return configured
    named = installed.get("channel")
    return named if named in CHANNELS else "stable"


def manifest_url(channel: str, env=os.environ) -> str:
    """Where the channel's manifest is. TEAPORT_MANIFEST_URL (the installer's override: a
    URL or a path) replaces it, for either channel."""
    return env.get("TEAPORT_MANIFEST_URL") or f"{MANIFEST_BASE}/{channel}.json"


def load(source: str) -> dict:
    """A manifest from a URL, a file:// URL or a path. Raises OSError/ValueError."""
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})  # noqa: S310
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECS) as r:  # noqa: S310
            data = json.load(r)
    else:
        path = parsed.path if parsed.scheme == "file" else source
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("the manifest is not a JSON object")
    return data


def _get(manifest: dict, *keys):
    node = manifest
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node


def _model_shas(manifest: dict) -> dict:
    """{model name: sha256} for the model files a manifest names."""
    models = _get(manifest, "models")
    if not isinstance(models, dict):
        return {}
    return {n: str(m["sha256"]) for n, m in sorted(models.items())
            if isinstance(m, dict) and m.get("sha256")}


def _models(manifest: dict) -> str | None:
    """The model files' sha256s, in one string; None when a manifest names none."""
    return ",".join(_model_shas(manifest).values()) or None


def deb_version(manifest: dict) -> str | None:
    """The brain package's version, from the file name brain.deb.url names; None when it
    names none (or a placeholder, or a name that is not a package's)."""
    url = _get(manifest, "brain", "deb", "url")
    if not isinstance(url, str):
        return None
    m = _DEB_NAME.match(os.path.basename(urlparse(url).path))
    return m.group(1) if m else None


def _brain_status() -> tuple[str, str]:
    """dpkg's (status, version) for teaport-brain; ("", "") when it cannot say."""
    try:
        out = subprocess.run(["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}",
                              BRAIN_PACKAGE], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "", ""
    status, _, version = out.stdout.strip().partition(" ")
    return (status, version) if out.returncode == 0 and version else ("", "")


def installed_brain() -> str | None:
    """The installed teaport-brain package's version (dpkg's); None when there is none."""
    status, version = _brain_status()
    return version if status == "installed" else None


def box_brain() -> str | None:
    """teaport-brain's version in any state that leaves its files on the box (half-configured
    too): the floor no update goes below (compare(); update_apply.not_older does the same as
    root, before it runs anything)."""
    status, version = _brain_status()
    return version if status and status not in ("not-installed", "config-files") else None


def _version_part(a: str, b: str) -> int:
    """Debian's comparison of one upstream or revision string (deb-version(7)): non-digits
    by character, where ~ sorts before anything, even the end, and letters before other
    characters; then digits as numbers; alternating."""
    def order(c: str) -> int:
        if c == "~":
            return -1
        if c.isalpha():
            return ord(c)
        return ord(c) + 256

    while a or b:
        na = re.match(r"[^0-9]*", a).group()
        nb = re.match(r"[^0-9]*", b).group()
        a, b = a[len(na):], b[len(nb):]
        for i in range(max(len(na), len(nb))):
            ca = order(na[i]) if i < len(na) else 0
            cb = order(nb[i]) if i < len(nb) else 0
            if ca != cb:
                return -1 if ca < cb else 1
        da = re.match(r"[0-9]*", a).group()
        db = re.match(r"[0-9]*", b).group()
        a, b = a[len(da):], b[len(db):]
        if int(da or 0) != int(db or 0):
            return -1 if int(da or 0) < int(db or 0) else 1
    return 0


def version_cmp(a: str, b: str) -> int:
    """-1, 0 or 1 as Debian orders package versions a and b ([epoch:]upstream[-revision]),
    the order apt installs by: 0.1.1~dev5+gabc < 0.1.1 < 0.1.1+fix < 0.1.2."""
    def split(v: str) -> tuple[int, str, str]:
        epoch, _, rest = v.partition(":") if ":" in v else ("0", "", v)
        upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "")
        return int(epoch or 0), upstream, revision

    ea, ua, ra = split(a)
    eb, ub, rb = split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _version_part(ua, ub) or _version_part(ra, rb)


def component(manifest: dict, name: str) -> str | None:
    """What a manifest says `name` is, as text; None when it says nothing."""
    value = {"version": lambda: _get(manifest, "version"),
             "engine": lambda: _get(manifest, "engine", "release"),
             "brain": lambda: deb_version(manifest),
             "plugin": lambda: _get(manifest, "plugin", "version"),
             "openclaw": lambda: _get(manifest, "openclaw", "version"),
             "models": lambda: _models(manifest)}[name]()
    return None if value in (None, "") else str(value)


def released(value: str | None) -> bool:
    """A value a channel has actually released: not absent, not a placeholder."""
    return value is not None and "TODO" not in value.upper()


def compare(installed: dict, latest: dict, brain: str | None = None, floor: str | None = None) -> dict:
    """The installed manifest against the channel's. {"components": {name: {"installed",
    "latest", "changed"}}, "changed": [names], "available": bool, "steps": [...],
    "manual": [...], "older": bool} (plan()). A component the channel has not released is
    never changed, nor is the version on its own. One the box has no record of, and the
    channel has, is (the box was installed before the manifest named it). `brain` is the
    installed brain package's version (installed_brain()): the brain is changed when the
    channel's package is newer than it, or when the box has none.

    `floor` is the brain package's version on the box in any state (box_brain()). A
    channel whose brain is older than that, or that names none, offers nothing ("older"):
    its engine and models are an older release's too. Nor does one whose engine release is
    older than the installed one, or one dpkg cannot order against it: two channels can
    name the same brain with different engines. teaport update runs nothing from either
    (update_apply.not_older, as root)."""
    channel_brain = deb_version(latest)
    older = floor is not None and (channel_brain is None or version_cmp(channel_brain, floor) < 0)
    engine_have, engine_new = component(installed, "engine"), component(latest, "engine")
    if engine_have and _ENGINE_VERSION.fullmatch(engine_have) and released(engine_new) \
            and engine_new != engine_have:
        older = older or not _ENGINE_VERSION.fullmatch(engine_new) \
            or version_cmp(engine_new, engine_have) < 0
    rows, changed = {}, []
    for name in COMPONENTS:
        have, new = component(installed, name), component(latest, name)
        if name == "brain":
            have = brain
            differs = released(new) and (have is None or version_cmp(new, have) > 0)
            rows[name] = {"installed": have, "latest": new if released(new) else None,
                          "changed": differs}
            if differs:
                changed.append(name)
            continue
        if name == "models":
            # Per file: a file the channel has not released (a placeholder hash) must not
            # hide a real change in another.
            now = {n: h for n, h in _model_shas(latest).items() if released(h)}
            mine = {n: h for n, h in _model_shas(installed).items() if n in now}
            have = ",".join(mine.values()) or None
            new = ",".join(now.values()) or None
            differs = any(mine.get(n) != h for n, h in now.items())
            rows[name] = {"installed": have, "latest": new, "changed": differs}
            if differs:
                changed.append(name)
            continue
        # The version names the release; it is shown, but a release whose parts are all the
        # box's own has nothing to install (a beta that is only a placeholder, say).
        differs = name != "version" and released(new) and have != new
        rows[name] = {"installed": have, "latest": new if released(new) else None,
                      "changed": differs}
        if differs:
            changed.append(name)
    if older:
        for row in rows.values():
            row["changed"] = False
        changed = []
    return {"components": rows, "changed": changed, "available": bool(changed), "older": older,
            **plan(changed)}


def plan(changed: list[str]) -> dict:
    """What `teaport update` would do about `changed`: {"steps": install.sh --only runs in
    order (STEPS), "manual": the changed components none of them installs}."""
    steps = [step for step, covers in STEPS if any(c in changed for c in covers)]
    covered = {c for _, covers in STEPS for c in covers}
    return {"steps": steps, "manual": [c for c in changed if c not in covered]}


def source(latest: dict) -> dict | None:
    """Where the installer for `latest` comes from: the {repo, tag} the one-line installer
    (install-bootstrap.sh) and install.sh's own clones use, brain.repo at brain.tag. None
    when the manifest names no usable one (update_apply.py then installs nothing)."""
    repo, tag = _get(latest, "brain", "repo"), _get(latest, "brain", "tag")
    ok = (isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
          and isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*", tag)
          and released(repo + tag))
    return {"repo": repo, "tag": tag} if ok else None


def check(channel: str | None = None, *, fetch=load, installed: dict | None = None,
          clock=time.time, env=os.environ, brain=None, floor=None) -> dict:
    """One look: the status dict (see Monitor.status), with "url" (the manifest read) and
    "source" (source()). `brain()` is the installed brain package's version (default
    installed_brain), `floor()` its version in any state (default box_brain, or `brain`
    when that is given). Raises what `fetch` raises."""
    installed = read_installed() if installed is None else installed
    ch = channel_of(channel, installed)
    url = manifest_url(ch, env)
    latest = fetch(url)
    return {"channel": ch, "url": url, "checked": clock(), "fresh": True,
            "source": source(latest),
            **compare(installed, latest, (brain or installed_brain)(),
                      (floor or brain or box_brain)())}


def configured_channel() -> str:
    """TEAPORT_UPDATE_CHANNEL as the brain has it, or -- in the CLI, which is not run
    under the brain's environment -- as brain.env sets it."""
    value = setting("TEAPORT_UPDATE_CHANNEL")
    if value:
        return value
    try:
        with open(os.path.join(ETC, "brain.env"), encoding="utf-8") as f:
            return parse_env(f.read()).get("TEAPORT_UPDATE_CHANNEL", "").strip()
    except OSError:
        return ""


CHECK_SECS = setting("TEAPORT_UPDATE_CHECK_SECS")


class Monitor:
    """The last look, kept across restarts. `fetch(url)` is load(), replaced in tests."""

    def __init__(self, clock=time.time, fetch=load, path: str | None = None):
        self._clock, self._fetch, self._path = clock, fetch, path
        self._last: dict | None = None   # the last answered look
        self._failed = False             # the look after it failed
        self._wake: asyncio.Event | None = None

    def _file(self) -> str:
        return self._path or os.path.join(state_dir(), STATE_FILE)

    def status(self) -> dict:
        """/health's "update": {"state": "off" | "unknown" | "current" | "available", ...}.
        `off`: the check is switched off (TEAPORT_UPDATE_CHECK_SECS = 0). `unknown`: no look
        has been answered yet. Otherwise channel, checked (wall clock), fresh (the last
        look was answered), changed (component names), components, and what `teaport
        update` would do about them: steps and manual (plan())."""
        if not CHECK_SECS:
            return {"state": "off"}
        if self._last is None:
            self._load()
        if self._last is None:
            return {"state": "unknown", "fresh": False}
        last = self._last
        return {"state": "available" if last["available"] else "current",
                "channel": last["channel"], "checked": last["checked"],
                "fresh": not self._failed, "changed": last["changed"],
                "older": bool(last.get("older")),
                "components": last["components"], **plan(last["changed"])}

    def _load(self) -> None:
        try:
            with open(self._file(), encoding="utf-8") as f:
                found = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(found, dict) and isinstance(found.get("components"), dict) \
                and isinstance(found.get("changed"), list) and "channel" in found \
                and isinstance(found.get("checked"), (int, float)):
            found["available"] = bool(found["changed"])
            self._last = found

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._file()), exist_ok=True)
            with open(self._file() + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self._last, f)
            os.replace(self._file() + ".tmp", self._file())
        except OSError as e:
            logger.warning(f"updates: could not keep the last look ({e})")

    async def refresh(self) -> bool:
        """One look. True when it was answered; a failed one keeps the last answer."""
        try:
            got = await asyncio.to_thread(check, configured_channel(), fetch=self._fetch,
                                          clock=self._clock)
        except Exception as e:  # noqa: BLE001 -- a bad read never ends the loop
            self._failed = True
            logger.info(f"updates: could not read the release manifest ({e!r})")
            return False
        self._failed = False
        previous = self._last
        self._last = got
        self._save()
        if got["available"] and (previous is None or previous["changed"] != got["changed"]):
            logger.info(f"updates: a new release is out on {got['channel']}: "
                        f"{', '.join(got['changed'])}")
        return True

    def poke(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def run(self) -> None:
        """The brain's loop (gateway_server's lifespan): a look at start, then every
        TEAPORT_UPDATE_CHECK_SECS (a tenth of jitter, so boxes switched on together do not
        ask together), or RETRY_SECS after a failed one. Nothing when the check is off."""
        if not CHECK_SECS:
            logger.info("updates: the check is off (TEAPORT_UPDATE_CHECK_SECS=0)")
            return
        self._wake = asyncio.Event()
        while True:
            ok = await self.refresh()
            wait = CHECK_SECS * random.uniform(0.9, 1.1) if ok else min(RETRY_SECS, CHECK_SECS)
            try:
                await asyncio.wait_for(self._wake.wait(), wait)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()


MONITOR = Monitor()


# --- the CLI's side: python -I -m teaport_brain.updates check [--json] [--channel C] -------

def render(status: dict) -> str:
    """The text `teaport update --check` prints."""
    lines = [f"channel  {status['channel']}  ({status['url']})"]
    for name in COMPONENTS:
        row = status["components"][name]
        have = (row["installed"] or "-")
        if name == "models":
            have, new = have[:12] or "-", (row["latest"] or "-")[:12]
        else:
            new = row["latest"] or "not released"
        # -> what teaport update would install; == the same; != different, but not offered
        # (an older release, or one a full install.sh run installs).
        mark = "->" if row["changed"] else "==" if (row["installed"] or None) == (row["latest"] or None) else "!="
        lines.append(f"  {name:9} {have}  {mark}  {new}")
    if status.get("older"):
        lines.append(f"the {status['channel']} channel names an older release than this box has: "
                     "teaport update does not go back (install.sh --rollback does, by hand).")
        return "\n".join(lines)
    if not status["changed"]:
        lines.append("up to date.")
        return "\n".join(lines)
    lines.append("changed: " + ", ".join(status["changed"]))
    if status["steps"]:
        lines.append("teaport update installs: " + ", ".join(status["steps"])
                     + " (the download can take a while; restarts wait until nobody is talking)")
    if status["manual"]:
        lines.append("needs a full install.sh run: " + ", ".join(status["manual"]))
    return "\n".join(lines)


# `check --exit-code`: what the CLI's `teaport update` asks before it offers to install.
EXIT_STEPS = 10   # there is something `teaport update` installs (status["steps"])


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "check":
        print("usage: python -m teaport_brain.updates check [--json] [--channel stable|beta]"
              " [--exit-code] [--prefix DIR] [--etc DIR]",
              file=sys.stderr)
        return 2
    global PREFIX, ETC
    as_json, exit_code, channel, rest = False, False, None, argv[1:]
    while rest:
        arg = rest.pop(0)
        if arg == "--json":
            as_json = True
        elif arg == "--exit-code":
            # 0 also when only a full install.sh run would do: `teaport update` has
            # nothing to start then.
            exit_code = True
        elif arg == "--channel" and rest:
            channel = rest.pop(0)
            if channel not in CHANNELS:
                print(f"unknown channel: {channel} (stable or beta)", file=sys.stderr)
                return 2
        elif arg in ("--prefix", "--etc") and rest:
            if arg == "--prefix":
                PREFIX = rest.pop(0)
            else:
                ETC = rest.pop(0)
        else:
            print(f"unknown argument: {arg}", file=sys.stderr)
            return 2
    installed = read_installed()
    if not installed:
        print(f"not installed: {installed_path()} is missing or unreadable", file=sys.stderr)
        return 1
    try:
        status = check(channel or configured_channel(), installed=installed)
    except Exception as e:  # noqa: BLE001
        print(f"cannot read the release manifest: {e}", file=sys.stderr)
        return 1
    print(json.dumps(status) if as_json else render(status))
    return EXIT_STEPS if exit_code and status["steps"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
