#!/usr/bin/python3
#
# teaport — install a newer release: what teaport-update.service runs, as root (the
# updater's apply step). Started by `teaport update` (and, later, by the brain when the
# owner says yes), through one sudoers line that allows exactly
#     systemctl start --no-block teaport-update.service
# and nothing else (packaging/deb/sudoers-sync). It takes no arguments: what to install is
# read here, from the release manifest of the box's channel, so whoever may start the
# unit can choose when, never what.
#
#   1. The plan: the brain's own comparison (teaport_brain.updates check --json, under the
#      installed brain's venv): which install.sh runs ("steps": engine, brain), the manifest
#      it read, and where the installer comes from ("source": brain.repo at brain.tag, what
#      the one-line installer clones). Nothing to do: done.
#   2. The installer: a shallow clone of that source. A box keeps no checkout; this is what
#      `bash <(curl -fsSL https://get.teaspoon.tech/teaport)` does too.
#   3. Each step, in order: install.sh --only engine, then --only brain, for the brain's run
#      user (TEAPORT_USER: the User= of teaport-brain.service), against the manifest read in
#      1. Each puts the previous engine or brain back by itself if the new one does not come
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

STATE = "/var/lib/teaport-update"
STATUS = os.path.join(STATE, "run.json")
LOG = os.path.join(STATE, "log")
BRAIN_PYTHON = "/opt/teaport/brain/bin/python"
BRAIN_UNIT = "teaport-brain.service"
# How long a step waits for the conversation in progress to end before it gives up, its
# downloads kept for the next try (install.sh's wait_for_idle).
WAIT_IDLE_SECS = 3600
CLONE_TIMEOUT_SECS = 300
STEPS = ("engine", "brain")   # the only values install.sh --only takes from here
ROOT_UID = 0                  # who must own STATE (a test runs as itself)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Run:
    """One run's record: update-run.json, rewritten whole at every change."""

    def __init__(self, user_gid=None):
        self.gid = user_gid
        self.state = {"state": "running", "started": time.time(), "finished": None,
                      "step": "check", "steps": [], "channel": None, "message": "",
                      "pid": os.getpid()}

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
    """Who the brain runs as: the User= of its unit (as sudoers-sync reads it)."""
    out = systemctl(["systemctl", "show", "-p", "User", "--value", BRAIN_UNIT],
                    capture_output=True, text=True)
    return out.stdout.strip() or "root"


def read_plan(runner=subprocess.run) -> dict:
    """The brain's comparison, as `teaport update --check --json` prints it."""
    p = runner([BRAIN_PYTHON, "-I", "-m", "teaport_brain.updates", "check", "--json"],
               capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        said = (p.stderr or p.stdout).strip().splitlines()
        raise RuntimeError("the release check failed: " + (said[-1] if said else f"exit {p.returncode}"))
    plan = json.loads(p.stdout)
    steps = plan.get("steps") or []
    if not isinstance(steps, list) or any(s not in STEPS for s in steps):
        raise RuntimeError(f"the check named steps this cannot run: {steps!r}")
    return plan


def clone(source: dict, into: str, runner=subprocess.run) -> str:
    """A shallow clone of github.com/<repo> at <tag> (anonymous, never a prompt): the
    installer of the release. Returns the checkout's path."""
    repo, tag = source["repo"], source["tag"]
    # updates.source() checked both; checked again, as they reach git's command line.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) \
            or not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*", tag):
        raise RuntimeError(f"the manifest names no installer this can fetch ({repo!r} at {tag!r})")
    dest = os.path.join(into, "teaport")
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "HOME": into}
    p = runner(["git", "clone", "--quiet", "--depth", "1", "--branch", tag,
                f"https://github.com/{repo}.git", dest],
               capture_output=True, text=True, env=env, timeout=CLONE_TIMEOUT_SECS)
    if p.returncode != 0:
        raise RuntimeError(f"could not fetch the installer (github.com/{repo} at {tag}): "
                           + ((p.stderr.strip().splitlines() or ["?"])[-1]))
    return dest


def step_env(user: str, home: str, manifest_url: str) -> dict:
    """install.sh's environment: the brain's user's, not root's (RUN_USER, their secrets
    and home, which detect_agent reads), the manifest the plan was read from, and the wait
    for nobody to be talking."""
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8", "HOME": home, "TEAPORT_USER": user,
            "TEAPORT_SECRETS_DIR": os.path.join(home, ".config", "teaport"),
            "TEAPORT_MANIFEST_URL": manifest_url,
            "TEAPORT_WAIT_IDLE_SECS": str(WAIT_IDLE_SECS)}


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
    """One run. Its parts are module functions, so a test replaces them (run_user, read_plan,
    clone, run_step) and STATE/ROOT_UID."""
    user = run_user()
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
        plan = read_plan()
        steps = plan.get("steps") or []
        record.set(channel=plan.get("channel"), steps=steps)
        if not steps:
            manual = plan.get("manual") or []
            record.set(state="done", step=None, message=(
                "nothing teaport update installs" + (f"; a full install.sh run would update: "
                                                     f"{', '.join(manual)}" if manual else "")))
            return 0
        if not plan.get("source"):
            raise RuntimeError("the release manifest names no installer to fetch (brain.repo, brain.tag)")
        record.set(step="fetch")
        work = tempfile.mkdtemp(prefix="teaport-update-")
        installer = clone(plan["source"], work)
        env = step_env(user, entry.pw_dir, plan["url"])
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
