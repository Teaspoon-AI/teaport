#
# teaport — is there a newer release? (the updater, part 1: looking; nothing is installed)
#
# A release is a manifest (manifest/stable.json, manifest/beta.json: engine release, brain
# tag, plugin version, OpenClaw version, models). install.sh copies the one it installed
# from to $PREFIX/manifest.json. This module diffs that against the channel's current one:
#   - the brain reads it once a day (Monitor, TEAPORT_UPDATE_CHECK_SECS), keeps the answer
#     in the state directory so a restart does not forget it, and shows it in /health's
#     "update" and `teaport status`;
#   - `teaport update --check` runs the same code on demand:
#         python -I -m teaport_brain.updates check [--json] [--channel stable|beta]
#     (the CLI's one comparison; there is no second copy of it in bash).
#
# A component the channel's manifest has not released yet ("TODO", empty, absent) is not an
# update, and is not shown as one. The brain entry is a git tag; a channel that names a
# moving ref (the manifest's placeholder "main") can never differ from itself, so a brain
# change is only seen once releases carry real tags.
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
# A read that failed is tried again after this, not after a day.
RETRY_SECS = 3600.0

# What a release is made of, in the order they are shown: (name, how to read it).
# "models" is the two model files' hashes: a model change rides an engine release, but the
# manifest can change one alone, and then the box needs it.
COMPONENTS = ("version", "engine", "brain", "plugin", "openclaw", "models")


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
        with urllib.request.urlopen(source, timeout=FETCH_TIMEOUT_SECS) as r:  # noqa: S310
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


def component(manifest: dict, name: str) -> str | None:
    """What a manifest says `name` is, as text; None when it says nothing."""
    value = {"version": lambda: _get(manifest, "version"),
             "engine": lambda: _get(manifest, "engine", "release"),
             "brain": lambda: _get(manifest, "brain", "tag"),
             "plugin": lambda: _get(manifest, "plugin", "version"),
             "openclaw": lambda: _get(manifest, "openclaw", "version"),
             "models": lambda: _models(manifest)}[name]()
    return None if value in (None, "") else str(value)


def released(value: str | None) -> bool:
    """A value a channel has actually released: not absent, not a placeholder."""
    return value is not None and "TODO" not in value.upper()


def compare(installed: dict, latest: dict) -> dict:
    """The installed manifest against the channel's. {"components": {name: {"installed",
    "latest", "changed"}}, "changed": [names], "available": bool}. A component the channel
    has not released is never changed, nor is the version on its own. One the box has no record of, and the channel has,
    is (the box was installed before the manifest named it)."""
    rows, changed = {}, []
    for name in COMPONENTS:
        have, new = component(installed, name), component(latest, name)
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
    return {"components": rows, "changed": changed, "available": bool(changed)}


def check(channel: str | None = None, *, fetch=load, installed: dict | None = None,
          clock=time.time, env=os.environ) -> dict:
    """One look: the status dict (see Monitor.status). Raises what `fetch` raises."""
    installed = read_installed() if installed is None else installed
    ch = channel_of(channel, installed)
    url = manifest_url(ch, env)
    latest = fetch(url)
    return {"channel": ch, "url": url, "checked": clock(), "fresh": True,
            **compare(installed, latest)}


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
        look was answered), changed (component names) and components."""
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
                "components": last["components"]}

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
        lines.append(f"  {name:9} {have}  {'->' if row['changed'] else '=='}  {new}")
    lines.append("changed: " + ", ".join(status["changed"]) if status["changed"]
                 else "up to date.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "check":
        print("usage: python -m teaport_brain.updates check [--json] [--channel stable|beta]"
              " [--prefix DIR] [--etc DIR]",
              file=sys.stderr)
        return 2
    global PREFIX, ETC
    as_json, channel, rest = False, None, argv[1:]
    while rest:
        arg = rest.pop(0)
        if arg == "--json":
            as_json = True
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
