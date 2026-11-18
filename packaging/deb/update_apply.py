#!/usr/bin/python3
#
# teaport — install a newer release: what teaport-update.service runs, as root (the
# updater's apply step). Started by `teaport update` (and, later, by the brain when the
# owner says yes), through a polkit rule that lets the run user start this one unit and
# nothing else (packaging/deb/sudoers-sync). It takes no arguments: what to install is read
# here, from the release manifest of the box's channel, so whoever may start the unit can
# choose when, never what.
#
# Nothing it runs as root is the run user's to change (the run user is who the agent runs
# as): the install trees are root's (install.sh's own_install_trees), and the brain's own
# code never runs as root here.
#
#   1. The plan: the brain's own comparison (teaport_brain.updates check --json, under the
#      installed brain's venv), run AS THE RUN USER, so whatever that tree holds runs with
#      no more than their rights. Its answer is data, and only two things in it are used: the
#      steps (each one of STEPS) and the channel (stable or beta). Nothing to do: done.
#      The manifest is then read here, once, from the download host's fixed address for
#      that channel (both steps install from this one copy), and the installer is the one
#      it names in REPO (brain.tag): nothing the
#      brain says can point the run at another manifest or another repository. The most a
#      lying answer does is install the release the channel names anyway.
#   2. The installer: a shallow clone of REPO at brain.tag. A box keeps no checkout; this is what
#      `bash <(curl -fsSL https://get.teaspoon.tech/teaport)` does too. Only a release's:
#      brain.tag must be a tag (a branch is whatever was pushed last), and the installer must
#      say UPDATE_PROTOCOL=1, which an installer that kept the install trees root's and the
#      run user's home as data does. An older one is never run as root from here.
#   3. Each step, in order: install.sh --only engine, then --only brain, for the brain's run
#      user (TEAPORT_USER: the User= of teaport-brain.service), against the manifest read in
#      1 (the channel's), with root's own HOME: no tool it runs as root reads its configuration from the run
#      user's home. Each puts the previous engine or brain back by itself if the new one does not come
#      up. TEAPORT_WAIT_IDLE_SECS makes each wait, before its restart, until nobody is
#      talking (install.sh's wait_for_idle); the downloads come first, while they still are.
#      A step that fails stops the run; what it installed is already rolled back.
#
# What it leaves, in its own root-owned state directory (the unit's StateDirectory=, never
# one the run user can write: a symlink there would aim a root write anywhere):
# /var/lib/teaport-update/run.json, the last run's state (running, done or failed, the step,
# a message), and log, its output, both readable by the run user's group: `teaport update`
# follows them, `teaport update --status` prints them. The journal has the same output
# (journalctl -u teaport-update).
#
# The SYSTEM python3, stdlib only, and nothing imported from the brain: the brain step
# replaces the brain's venv, and this file with it, while it runs.
#
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

STATE = "/var/lib/teaport-update"
STATUS = os.path.join(STATE, "run.json")
LOG = os.path.join(STATE, "log")
BRAIN_PYTHON = "/opt/teaport/brain/bin/python"
CHANNELS = ("stable", "beta")
# The manifest of a channel: the download host's (updates.MANIFEST_BASE), nothing else.
MANIFEST_BASE = "https://get.teaspoon.tech/manifest"
# The download host refuses urllib's default agent (updates.USER_AGENT says the same).
USER_AGENT = "teaport-update (+https://github.com/Teaspoon-AI/teaport)"
# The only repository whose installer runs here.
REPO = "Teaspoon-AI/teaport"
BRAIN_UNIT = "teaport-brain.service"
# How long a step waits for the conversation in progress to end before it gives up, its
# downloads kept for the next try (install.sh's wait_for_idle).
WAIT_IDLE_SECS = 3600
CLONE_TIMEOUT_SECS = 300
STEPS = ("engine", "brain")   # the only values install.sh --only takes from here
ROOT_UID = 0                  # who must own STATE (a test runs as itself)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Run:
    """One run's record: run.json, rewritten whole at every change."""

    def __init__(self, user_gid=None):
        self.gid = user_gid
        # The unit's InvocationID (systemd sets $INVOCATION_ID): how `teaport update` tells
        # this run from an earlier one, whatever the clock says.
        self.state = {"state": "running", "started": time.time(), "finished": None,
                      "step": "check", "steps": [], "channel": None, "message": "",
                      "pid": os.getpid(), "invocation": os.environ.get("INVOCATION_ID", "")}

    def set(self, **changes):
        self.state.update(changes)
        if changes.get("state") in ("done", "failed"):
            self.state["finished"] = time.time()
        _write_shared(STATUS, json.dumps(self.state) + "\n", self.gid)


def _write_shared(path, text, gid):
    """Root's file, readable by the run user's group (0640), replaced whole."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if gid is not None:
        os.chown(tmp, -1, gid)   # root's file (this runs as root), the run user's group
    os.chmod(tmp, 0o640 if gid is not None else 0o644)
    os.replace(tmp, path)


def run_user(systemctl=subprocess.run) -> str:
    """Who the brain runs as: the User= of its unit (as sudoers-sync reads it); root for a
    loaded unit that names none. A unit that is not there (masked, never installed) names
    nobody: then this refuses, rather than run the brain's comparison as root."""
    out = systemctl(["systemctl", "show", "-p", "LoadState,User", BRAIN_UNIT],
                    capture_output=True, text=True)
    props = dict(ln.partition("=")[::2] for ln in out.stdout.splitlines())
    if props.get("LoadState") != "loaded":
        raise RuntimeError(f"{BRAIN_UNIT} is not installed here ({props.get('LoadState') or '?'}): "
                           "nothing to update")
    return props.get("User") or "root"


def read_plan(entry, runner=subprocess.run) -> dict:
    """The brain's comparison, as `teaport update --check --json` prints it, run as the run
    user (<entry>, their passwd entry): {"steps", "channel", "manual"}, the only parts used,
    each checked. Raises RuntimeError."""
    p = runner(["setpriv", f"--reuid={entry.pw_uid}", f"--regid={entry.pw_gid}", "--init-groups",
                "--", BRAIN_PYTHON, "-I", "-m", "teaport_brain.updates", "check", "--json"],
               capture_output=True, text=True, timeout=120, cwd="/",
               env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
                    "HOME": entry.pw_dir, "USER": entry.pw_name})
    if p.returncode != 0:
        said = (p.stderr or p.stdout).strip().splitlines()
        raise RuntimeError("the release check failed: " + (said[-1] if said else f"exit {p.returncode}"))
    try:
        plan = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError("the release check printed no plan") from None
    steps, channel = plan.get("steps") or [], plan.get("channel")
    if not isinstance(steps, list) or any(s not in STEPS for s in steps):
        raise RuntimeError(f"the check named steps this cannot run: {steps!r}")
    if channel not in CHANNELS:
        raise RuntimeError(f"the check named no channel this installs from: {channel!r}")
    manual = [m for m in (plan.get("manual") or []) if isinstance(m, str) and re.fullmatch(r"[a-z]+", m)]
    # In STEPS order, once each, whatever order the answer gave.
    return {"steps": [s for s in STEPS if s in steps], "channel": channel, "manual": manual}


def manifest_url(channel: str) -> str:
    return f"{MANIFEST_BASE}/{channel}.json"


def read_manifest(channel: str, dest: str, fetch=None) -> dict:
    """The channel's manifest, from the download host's fixed address, saved to <dest> (in
    this run's root-only work directory): both steps install from this one copy, so a
    release published while the run goes on cannot mix with this one. Raises RuntimeError."""
    def load(url):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 -- a fixed https URL
            return r.read(4 << 20)
    try:
        body = (fetch or load)(manifest_url(channel))
        manifest = json.loads(body)
    except Exception as e:  # noqa: BLE001 -- unreadable, not JSON
        raise RuntimeError(f"cannot read the release manifest: {e}") from None
    if not isinstance(manifest, dict):
        raise RuntimeError("the release manifest is not a JSON object")
    with open(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(body if isinstance(body, bytes) else body.encode())
    return manifest


# The brain package's file name in brain.deb.url, read as updates.py reads it (its
# _DEB_NAME; test_update_apply checks the two agree). Root does not import the brain's code.
DEB_NAME = re.compile(r"^teaport-brain_([0-9][A-Za-z0-9.+~:-]*)_[a-z0-9]+\.deb$")
BRAIN_PACKAGE = "teaport-brain"
# dpkg states that leave none of the package's files on the box.
GONE = ("not-installed", "config-files")
# dpkg's own tools, with root's PATH and nothing of the caller's.
ROOT_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


def channel_brain(manifest: dict) -> str | None:
    """The brain package version the release manifest names (brain.deb.url's file name)."""
    url = ((manifest.get("brain") or {}).get("deb") or {}).get("url") if isinstance(manifest, dict) else None
    if not isinstance(url, str):
        return None
    m = DEB_NAME.match(os.path.basename(urllib.parse.urlparse(url).path))
    return m.group(1) if m else None


def box_brain(runner=subprocess.run) -> str | None:
    """The brain package's version on this box in any state that leaves its files here —
    half-configured too: a brain the run user keeps from coming up (it runs as that
    user) leaves dpkg there, and must not hide the version from the floor below."""
    try:
        p = runner(["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", BRAIN_PACKAGE],
                   capture_output=True, text=True, timeout=30, env=ROOT_ENV)
    except (OSError, subprocess.SubprocessError):
        return None
    status, _, version = p.stdout.strip().partition(" ")
    return version if p.returncode == 0 and status not in GONE and version else None


# The engine release install.sh itself last put live (its engine_record): the engine's
# floor. Root writes it from the release manifest it fetched and checked, never from
# $PREFIX/manifest.json, which an older installer let the run user write and the rebuild
# (root_layout.py) carried over as it was; the rebuild does not carry this file. None
# (no floor) until install.sh first installs an engine on the box.
ENGINE_RECORD = "/var/lib/teaport/engine.release"
# A version dpkg can order (engine releases are "1.6", "1.7").
VERSION = re.compile(r"[0-9][A-Za-z0-9.+~-]*")


def box_engine(path: str = ENGINE_RECORD) -> str | None:
    """The installed engine release, from the record root alone can write; else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, encoding="utf-8") as f:
            st, parent = os.fstat(f.fileno()), os.stat(os.path.dirname(path))
            if any(s.st_uid != ROOT_UID or s.st_mode & 0o022 for s in (st, parent)):
                return None
            release = f.read(256).strip()
    except (OSError, ValueError):
        return None
    return release if VERSION.fullmatch(release) else None


def _older(new: str, have: str, runner) -> bool:
    """`new` sorts before `have` (dpkg's order). Anything dpkg cannot order raises: a
    version the floor cannot read is not let past it."""
    rc = runner(["dpkg", "--compare-versions", new, "lt", have], env=ROOT_ENV, timeout=30).returncode
    if rc not in (0, 1):
        raise RuntimeError(f"cannot compare the release's {new!r} with this box's {have!r}; nothing was done")
    return rc == 0


def not_older(manifest: dict, have: str | None, engine: str | None = None,
              runner=subprocess.run) -> None:
    """Refuse a release older than what is on the box, before anything of it is fetched or
    run: the run user picks the channel, and the other one may name an older release — one
    whose engine a newer release fixed, or whose install.sh a later one hardened, and this
    runs that install.sh as root. Going back is install.sh --rollback, by hand.

    `have` is the brain package on the box in any state (box_brain), `engine` the installed
    engine release (box_engine). The brain must not go back, and nor may the engine: two
    channels can name the same brain with different engines (beta testing one). A release
    whose engine dpkg cannot order against the box's is refused too. Raises RuntimeError."""
    channel = manifest.get("channel") or "chosen"
    if have is None:
        # This runs from the brain package, so dpkg has one: it could not say which.
        raise RuntimeError("cannot read the brain package's version on this box (dpkg-query); "
                           "nothing was done")
    new = channel_brain(manifest)
    if new is None:
        raise RuntimeError("the release manifest names no brain package, so it cannot be told "
                           "from an older release than this box has; nothing was done")
    if _older(new, have, runner):
        raise RuntimeError(f"the {channel} channel names an older release (brain {new}) than this "
                           f"box has ({have}); teaport update never goes back (install.sh "
                           "--rollback does, by hand)")
    new_engine = (manifest.get("engine") or {}).get("release")
    if engine is None or not isinstance(new_engine, str) or "TODO" in new_engine.upper() \
            or new_engine == engine:
        return
    if not VERSION.fullmatch(new_engine) or _older(new_engine, engine, runner):
        raise RuntimeError(f"the {channel} channel names an older engine ({new_engine}) than this "
                           f"box has ({engine}); teaport update never goes back (install.sh "
                           "--rollback engine does, by hand)")


def read_source(manifest: dict) -> dict:
    """{repo, tag}: the installer <manifest> names (brain.repo, brain.tag), only REPO's.
    Raises RuntimeError."""
    brain = manifest.get("brain") if isinstance(manifest, dict) else None
    repo, tag = (brain or {}).get("repo"), (brain or {}).get("tag")
    if repo != REPO or not isinstance(tag, str) or "TODO" in tag.upper():
        raise RuntimeError(f"the release manifest names no installer this installs from "
                           f"({repo!r} at {tag!r}; only {REPO}'s release tags)")
    return {"repo": repo, "tag": tag}


def clone(source: dict, into: str, runner=subprocess.run) -> str:
    """A shallow clone of github.com/<repo> at the release tag <tag> (anonymous, never a
    prompt): the installer of the release. Returns the checkout's path. Refuses a <tag> that
    is not a tag there (a branch moves), a checkout that is not at that tag's commit, and an
    installer that does not declare UPDATE_PROTOCOL=1."""
    repo, tag = source["repo"], source["tag"]
    # Checked again, as they reach git's command line.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) \
            or not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*", tag):
        raise RuntimeError(f"the manifest names no installer this can fetch ({repo!r} at {tag!r})")
    url = f"https://github.com/{repo}.git"
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "HOME": into}

    def git(*args, cwd=None):
        p = runner(["git", *args], capture_output=True, text=True, env=env, cwd=cwd,
                   timeout=CLONE_TIMEOUT_SECS)
        if p.returncode != 0:
            raise RuntimeError(f"could not fetch the installer (github.com/{repo} at {tag}): "
                               + ((p.stderr.strip().splitlines() or ["?"])[-1]))
        return p.stdout
    refs = {}
    for line in git("ls-remote", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}").splitlines():
        sha, _, ref = line.partition("\t")
        refs[ref] = sha
    commit = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if not commit:
        raise RuntimeError(f"the release manifest names {tag!r}, which is not a release tag of "
                           f"github.com/{repo} (an update installs only a release's installer)")
    dest = os.path.join(into, "teaport")
    git("clone", "--quiet", "--depth", "1", "--branch", tag, url, dest)
    head = git("rev-parse", "HEAD", cwd=dest).strip()
    if head != commit:
        raise RuntimeError(f"the installer fetched is not at {tag} ({head[:12]}, the tag is {commit[:12]})")
    with open(os.path.join(dest, "install.sh"), encoding="utf-8", errors="replace") as f:
        if "\nUPDATE_PROTOCOL=1\n" not in f.read():
            raise RuntimeError(f"the installer of {tag} predates teaport update; install it by hand "
                               "(install.sh --only brain / --only engine)")
    return dest


def step_env(user: str, home: str, manifest: str) -> dict:
    """install.sh's environment: the brain's user (RUN_USER; install.sh finds their home
    itself, as data) and their secrets directory, root's own HOME (no tool root runs reads
    its configuration from the run user's home: a ~/.curlrc, a Python user site, a
    ~/.gitconfig), this run's copy of the manifest, and the wait for nobody to be talking."""
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8", "HOME": "/root", "TEAPORT_USER": user,
            "TEAPORT_SECRETS_DIR": os.path.join(home, ".config", "teaport"),
            "TEAPORT_MANIFEST_URL": manifest,
            "TEAPORT_WAIT_IDLE_SECS": str(WAIT_IDLE_SECS),
            # Never an older brain package than the installed one: the channel is the run
            # user's to pick, and the other one may name an older release.
            "TEAPORT_NEWER_ONLY": "1"}


def run_step(installer: str, step: str, env: dict, log, popen=subprocess.Popen) -> tuple[int, str]:
    """install.sh --only <step>, its output into the log and the journal (this process's
    stdout). Returns its exit code and its last error line."""
    p = popen(["bash", os.path.join(installer, "install.sh"), "--only", step],
              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
              env=env, cwd=installer, text=True, errors="replace")
    last_error = ""
    for line in p.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        log.write(line)
        log.flush()
        plain = ANSI.sub("", line).strip()
        if plain.startswith("error:"):
            last_error = plain[len("error:"):].strip()
    return p.wait(), last_error


def main() -> int:
    """One run. Its parts are module functions, so a test replaces them (run_user,
    read_plan, read_manifest, clone, run_step) and STATE/ROOT_UID."""
    try:
        user = run_user()
    except RuntimeError as e:
        print(f"teaport-update: {e}", file=sys.stderr)
        return 1
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        print(f"teaport-update: the brain's user {user!r} does not exist", file=sys.stderr)
        return 1
    gid = entry.pw_gid if user != "root" else None
    os.makedirs(STATE, mode=0o755, exist_ok=True)
    st = os.lstat(STATE)
    if not os.path.isdir(STATE) or os.path.islink(STATE) or st.st_uid != ROOT_UID or st.st_mode & 0o022:
        print(f"teaport-update: {STATE} is not root's own directory; nothing was done", file=sys.stderr)
        return 1
    record = Run(gid)
    work = None

    def stopped(signum, _frame):
        raise SystemExit(f"stopped ({signal.Signals(signum).name})")
    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    try:
        _write_shared(LOG, "", gid)
        record.set()
        plan = read_plan(entry)
        steps = plan.get("steps") or []
        record.set(channel=plan.get("channel"), steps=steps)
        if not steps:
            manual = plan.get("manual") or []
            record.set(state="done", step=None, message=(
                "nothing teaport update installs" + (f"; a full install.sh run would update: "
                                                     f"{', '.join(manual)}" if manual else "")))
            return 0
        record.set(step="fetch")
        work = tempfile.mkdtemp(prefix="teaport-update-")
        manifest_copy = os.path.join(work, "manifest.json")
        manifest = read_manifest(plan["channel"], manifest_copy)
        source = read_source(manifest)
        not_older(manifest, box_brain(), box_engine())   # before anything of it is fetched or run
        installer = clone(source, work)
        env = step_env(user, entry.pw_dir, manifest_copy)
        with open(LOG, "a", encoding="utf-8") as log:
            for step in steps:
                record.set(step=step)
                rc, why = run_step(installer, step, env, log)
                if rc != 0:
                    record.set(state="failed", message=(
                        f"install.sh --only {step} failed" + (f": {why}" if why else f" (exit {rc})")))
                    return 1
        record.set(state="done", step=None, message="installed: " + ", ".join(steps))
        return 0
    except SystemExit as e:
        record.set(state="failed", message=str(e.code))
        return 1
    except Exception as e:  # noqa: BLE001 -- every failure is recorded, then said
        record.set(state="failed", message=str(e) or type(e).__name__)
        print(f"teaport-update: {record.state['message']}", file=sys.stderr)
        return 1
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
