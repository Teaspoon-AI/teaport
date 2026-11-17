#
# teaport update's apply step (the updater, part 2): teaport-update.service runs
# packaging/deb/update_apply.py as root; `teaport update` starts it and follows it.
#
# What has to hold:
#   - The helper installs what the brain's comparison lists ("steps"), in its order, with
#     install.sh --only <step> from a clone of the manifest's installer, for the brain's
#     run user, against the manifest the plan was read from, each restart waiting for
#     nobody to be talking. A failed step stops the run; nothing to do is done. The run's
#     state (run.json) says which, and why.
#   - It takes nothing it is not sure of: a step it does not know, an installer source that
#     is not a plain repo and ref, a state directory that is not root's own.
#   - install.sh, run as root for another user, does the run user's part as that user
#     (as_user: setpriv), so no root write lands where the run user could aim it; and the
#     engine's file operations all go through it. wait_for_idle holds a restart while the
#     brain says someone is talking, up to its limit, with the token off curl's command line.
#   - `teaport update` asks, starts the unit through sudo with exactly the arguments the
#     sudoers line names, and follows the run to its end; --status says how the last one went.
#   - The package ships the helper and the unit, depends on git, and sudoers-sync writes the
#     one line that starts it.
#
# Everything runs against temp dirs with the commands around it stubbed; nothing is
# installed and no unit is started.
#
# Run: python test_update_apply.py   (or via pytest test_suite.py)
#
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import time

import appliance
from tempdirs import tempdir

ROOT = pathlib.Path(__file__).resolve().parents[2]
HELPER = ROOT / "packaging/deb/update_apply.py"
INSTALL = ROOT / "install.sh"
CLI = ROOT / "cli/teaport"


def _helper(state):
    """A fresh copy of the helper module, its state directory in <state>, owned by us."""
    spec = importlib.util.spec_from_file_location(f"update_apply_{time.monotonic_ns()}", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATE, mod.STATUS, mod.LOG = state, os.path.join(state, "run.json"), os.path.join(state, "log")
    mod.ROOT_UID = os.getuid()
    mod.run_user = lambda: os.environ.get("USER") or __import__("pwd").getpwuid(os.getuid()).pw_name
    return mod


def _plan(steps, manual=(), source=None):
    return {"channel": "stable", "url": "https://get.teaspoon.tech/manifest/stable.json",
            "steps": list(steps), "manual": list(manual),
            "source": {"repo": "Teaspoon-AI/teaport", "tag": "main"} if source is None else source}


def _state(tmp):
    d = os.path.join(tmp, "state")
    os.makedirs(d, mode=0o755, exist_ok=True)
    os.chmod(d, 0o755)
    return d


def _status(mod):
    with open(mod.STATUS) as f:
        return json.load(f)


# --- the helper ----------------------------------------------------------------------

def test_a_run_installs_the_steps_in_order_for_the_brains_user():
    tmp = tempdir("teaport-apply-")
    mod = _helper(_state(tmp))
    clones, ran = [], []
    mod.read_plan = lambda: _plan(["engine", "brain"])

    def clone(source, into):
        clones.append((source, into))
        os.makedirs(os.path.join(into, "teaport"))
        return os.path.join(into, "teaport")
    mod.clone = clone

    def run_step(installer, step, env, log):
        ran.append((step, env))
        assert _status(mod)["step"] == step and _status(mod)["state"] == "running"
        log.write(f"output of {step}\n")
        return 0, ""
    mod.run_step = run_step
    assert mod.main() == 0
    st = _status(mod)
    assert st["state"] == "done" and st["message"] == "installed: engine, brain", st
    assert st["steps"] == ["engine", "brain"] and st["channel"] == "stable" and st["finished"], st
    assert [s for s, _ in ran] == ["engine", "brain"], ran
    env = ran[0][1]
    user = mod.run_user()
    home = __import__("pwd").getpwnam(user).pw_dir
    assert env["TEAPORT_USER"] == user and env["HOME"] == home, env
    assert env["TEAPORT_SECRETS_DIR"] == os.path.join(home, ".config", "teaport"), env
    assert env["TEAPORT_MANIFEST_URL"] == "https://get.teaspoon.tech/manifest/stable.json"
    assert int(env["TEAPORT_WAIT_IDLE_SECS"]) > 0, env
    assert clones[0][0] == {"repo": "Teaspoon-AI/teaport", "tag": "main"}
    assert not os.path.exists(clones[0][1]), "the clone is removed after the run"
    with open(mod.LOG) as f:
        assert f.read() == "output of engine\noutput of brain\n"
    assert os.stat(mod.STATUS).st_mode & 0o777 == 0o640


def test_a_failed_step_stops_the_run_and_says_why():
    tmp = tempdir("teaport-apply-")
    mod = _helper(_state(tmp))
    ran = []
    mod.read_plan = lambda: _plan(["engine", "brain"])
    mod.clone = lambda source, into: into
    mod.run_step = lambda installer, step, env, log: (ran.append(step), (1, "the engine did not come back"))[1]
    assert mod.main() == 1
    st = _status(mod)
    assert ran == ["engine"], ran
    assert st["state"] == "failed" and st["step"] == "engine", st
    assert st["message"] == "install.sh --only engine failed: the engine did not come back", st
    # A stop (systemctl stop: SIGTERM) is a failed run that says so.
    mod.run_step = lambda *a: (_ for _ in ()).throw(SystemExit("stopped (SIGTERM)"))
    assert mod.main() == 1 and _status(mod)["message"] == "stopped (SIGTERM)"


def test_nothing_to_do_is_done_and_names_what_only_a_full_run_updates():
    tmp = tempdir("teaport-apply-")
    mod = _helper(_state(tmp))
    mod.read_plan = lambda: _plan([], manual=["plugin"])
    mod.clone = lambda *a: (_ for _ in ()).throw(AssertionError("nothing to clone"))
    assert mod.main() == 0
    st = _status(mod)
    assert st["state"] == "done" and "plugin" in st["message"] and "full install.sh" in st["message"], st
    # A plan with steps but no installer to fetch fails before anything.
    mod.read_plan = lambda: _plan(["brain"], source=False)
    assert mod.main() == 1 and "names no installer" in _status(mod)["message"]


def test_a_state_directory_that_is_not_roots_own_is_refused():
    tmp = tempdir("teaport-apply-")
    state = _state(tmp)
    mod = _helper(state)
    mod.read_plan = lambda: (_ for _ in ()).throw(AssertionError("must not get this far"))
    os.chmod(state, 0o777)
    assert mod.main() == 1 and not os.path.exists(mod.STATUS)
    os.chmod(state, 0o755)
    mod.ROOT_UID = os.getuid() + 1   # somebody else's
    assert mod.main() == 1 and not os.path.exists(mod.STATUS)
    # A symlink to a directory that would pass is refused too.
    link = os.path.join(tmp, "link")
    os.symlink(state, link)
    mod = _helper(link)
    mod.read_plan = lambda: (_ for _ in ()).throw(AssertionError("must not get this far"))
    assert mod.main() == 1 and not os.path.exists(os.path.join(state, "run.json"))


def test_the_plan_and_the_clone_take_nothing_they_are_not_sure_of():
    mod = _helper(tempdir("teaport-apply-"))

    class P:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    plan = mod.read_plan(runner=lambda *a, **k: P(0, json.dumps(_plan(["engine"]))))
    assert plan["steps"] == ["engine"]
    for steps in (["rm -rf /"], ["--only"], "engine"):
        try:
            mod.read_plan(runner=lambda *a, **k: P(0, json.dumps({**_plan([]), "steps": steps})))
        except RuntimeError as e:
            assert "cannot run" in str(e)
        else:
            raise AssertionError(steps)
    try:
        mod.read_plan(runner=lambda *a, **k: P(1, err="cannot read the release manifest: offline"))
    except RuntimeError as e:
        assert str(e) == "the release check failed: cannot read the release manifest: offline"
    else:
        raise AssertionError("a failed check")
    seen = []
    for repo, tag in (("x/y", "-u"), ("x/y/z", "main"), ("x y/z", "main"), ("x/y", "a b")):
        try:
            mod.clone({"repo": repo, "tag": tag}, "/tmp", runner=lambda *a, **k: seen.append(a))
        except RuntimeError:
            pass
        else:
            raise AssertionError((repo, tag))
    assert seen == []
    calls = []
    got = mod.clone({"repo": "Teaspoon-AI/teaport", "tag": "brain-v0.1.1"}, "/w",
                    runner=lambda cmd, **k: (calls.append((cmd, k["env"])), P(0))[1])
    cmd, env = calls[0]
    assert got == "/w/teaport" and cmd[-3:] == ["brain-v0.1.1", "https://github.com/Teaspoon-AI/teaport.git", "/w/teaport"]
    assert "--depth" in cmd and env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    try:
        mod.clone({"repo": "a/b", "tag": "main"}, "/w", runner=lambda *a, **k: P(128, err="fatal: not found"))
    except RuntimeError as e:
        assert "fatal: not found" in str(e)


def test_a_step_reports_installs_last_error_line():
    mod = _helper(tempdir("teaport-apply-"))
    out = "\x1b[1;36m==>\x1b[0m downloading\n\x1b[1;31merror:\x1b[0m the brain did not come back\n"

    class Proc:
        def __init__(self, cmd, **k):
            self.cmd, self.k = cmd, k
            self.stdout = iter(out.splitlines(keepends=True))

        def wait(self):
            return 1
    seen = []

    def popen(cmd, **k):
        seen.append((cmd, k))
        return Proc(cmd, **k)
    log = open(os.path.join(tempdir("teaport-apply-log-"), "log"), "w+")
    rc, why = mod.run_step("/w/teaport", "brain", {"X": "1"}, log, popen=popen)
    assert rc == 1 and why == "the brain did not come back", (rc, why)
    assert seen[0][0] == ["bash", "/w/teaport/install.sh", "--only", "brain"]
    assert seen[0][1]["stdin"] == subprocess.DEVNULL and seen[0][1]["env"] == {"X": "1"}
    log.seek(0)
    assert log.read() == out


# --- install.sh: as_user and wait_for_idle ---------------------------------------------

def _fn(name: str) -> str:
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _line(prefix: str) -> str:
    return next(ln for ln in INSTALL.read_text().splitlines() if ln.startswith(prefix)) + "\n"


def _bash(tmp, script, env=None):
    stubs = r'''
set -euo pipefail
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
die()  { printf 'DIE %s\n' "$*"; exit 1; }
'''
    lines = INSTALL.read_text().splitlines()
    helpers = "\n".join(ln for ln in lines if ln.startswith(("run()  {", "as_user() {")))
    sudo_start = next(i for i, ln in enumerate(lines) if ln.startswith("SUDO() {"))
    sudo_end = next(i for i in range(sudo_start, len(lines)) if lines[i] == "}")
    body = (stubs + helpers.split("\n")[0] + "\n" + "\n".join(lines[sudo_start:sudo_end + 1]) + "\n"
            + _fn("as_user") + _fn("read_env_file") + script)
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                       env={**os.environ, "T": str(tmp), **(env or {})})
    return p.returncode, p.stdout + p.stderr


def test_as_root_for_another_user_the_run_users_work_is_done_as_them():
    tmp = tempdir("teaport-asuser-")
    stub = r'''
id() { case "$*" in "-u") echo "${FAKE_UID:-0}" ;; "-g "*) echo 4242 ;; *) command id "$@" ;; esac; }
setpriv() { printf 'SETPRIV %s\n' "$*"; }
DRY_RUN=0
'''
    rc, out = _bash(tmp, stub + 'RUN_USER=bob; as_user touch x; run mkdir y; SUDO echo ROOT z\n')
    assert rc == 0, out
    assert "SETPRIV --reuid=bob --regid=4242 --init-groups -- touch x" in out, out
    assert "SETPRIV --reuid=bob --regid=4242 --init-groups -- mkdir y" in out, out
    assert "\nROOT z" in out and "SETPRIV" not in out.split("mkdir y")[1], out   # SUDO is root's own
    # Root for root, or not root at all: nothing changes hands.
    rc, out = _bash(tmp, stub + 'RUN_USER=root; as_user true; run true\n')
    assert rc == 0 and "SETPRIV" not in out, out
    rc, out = _bash(tmp, stub + 'RUN_USER=bob; as_user true; run true\n', {"FAKE_UID": "1000"})
    assert rc == 0 and "SETPRIV" not in out, out
    # A dry run prints, and runs nothing.
    rc, out = _bash(tmp, stub + 'RUN_USER=bob; DRY_RUN=1; run rm -rf /x; SUDO rm -rf /y\n')
    assert rc == 0 and "SETPRIV" not in out and "[dry-run] rm -rf /x" in out and "[dry-run] rm -rf /y" in out, out


# The functions that write the run user's files under $PREFIX (the engine) and $STATE (the
# brain's download): every write, rename, link and removal in them goes through as_user
# (or run/SUDO), never a bare one that root would do in a directory the run user owns.
RUN_USER_FUNCTIONS = ("download", "engine_fetch", "engine_keep_live", "engine_swap_in",
                      "engine_swap_abort", "engine_swap_recover", "engine_restore",
                      "manifest_take_py", "brain_deb_keep", "main_rollback_engine")
BARE = re.compile(r"(?:^|[;&|{(]\s*|\bthen\s+|\belse\s+|\bdo\s+)(mv|ln|cp|mkdir|rm|ldd|python3|touch|chmod)\s")


def test_the_run_users_files_are_never_written_by_a_bare_root_command():
    text = INSTALL.read_text()
    for name in RUN_USER_FUNCTIONS:
        for raw in _fn(name).splitlines()[1:]:
            ln = raw.split(" #")[0].strip()
            if ln.startswith("#"):
                continue
            for m in BARE.finditer(ln):
                before = ln[:m.start(1)]
                assert re.search(r"(as_user|run|SUDO)\s*$", before), (name, raw)
            assert not re.search(r"[^2&]>\s*\"\$", ln) or "as_user tee" in ln or "/dev/null" in ln, (name, raw)
    # ldd can run what it looks at: never as root on the run user's binary.
    assert "as_user ldd \"$bin\"" in text
    # The restarts wait for idle, right before they happen.
    assert "  wait_for_idle\n  engine_swap\n" in text
    assert "  wait_for_idle\n  sip_brain_detach\n" in text


def _idle_script(tmp, answers, secs="60"):
    """wait_for_idle against a stubbed curl that answers <answers> in turn: (http code, body)."""
    with open(os.path.join(tmp, "answers"), "w") as f:
        for code, body in answers:
            f.write(f"{code}\t{json.dumps(body)}\n")
    env_file = os.path.join(tmp, "brain.env")
    with open(env_file, "w") as f:
        f.write('BRAIN_PORT=7999\nGATEWAY_TOKEN="s3cret"\n')
    return (r'''
curl() {
  printf 'ARGV %s\n' "$*" >> "$T/curl.log"
  printf 'STDIN %s\n' "$(cat)" >> "$T/curl.log"
  local n out line; n="$(cat "$T/n" 2>/dev/null || echo 0)"; echo $((n + 1)) > "$T/n"
  line="$(sed -n "$((n + 1))p" "$T/answers")"; [ -n "$line" ] || line="$(tail -1 "$T/answers")"
  while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift ;; esac; shift; done
  printf '%s' "${line#*$'\t'}" > "$out"; printf '%s' "${line%%$'\t'*}"
}
'''
            + f'ETC="{tmp}"; BRAIN_PORT=7861; DRY_RUN=0\n'
            + _line("WAIT_IDLE_SECS=") + "WAIT_IDLE_POLL_SECS=0\n"
            + _fn("wait_for_idle") + "wait_for_idle; echo RETURNED\n"), {"TEAPORT_WAIT_IDLE_SECS": secs}


def test_a_restart_waits_while_someone_is_talking_and_goes_ahead_when_nobody_is():
    tmp = tempdir("teaport-idle-")
    script, env = _idle_script(tmp, [(200, {"live": True}), (200, {"live": True}), (200, {"live": False})])
    rc, out = _bash(tmp, script, env)
    assert rc == 0 and "RETURNED" in out, out
    assert "waiting for the conversation to end" in out and "nobody is talking now" in out, out
    calls = pathlib.Path(tmp, "curl.log").read_text()
    assert calls.count("ARGV") == 3 and "http://127.0.0.1:7999/talk/status" in calls, calls
    # The token goes on stdin, never on the command line.
    assert all("s3cret" not in ln for ln in calls.splitlines() if ln.startswith("ARGV")), calls
    assert 'STDIN header = "Authorization: Bearer s3cret"' in calls, calls


def test_a_conversation_that_outlasts_the_wait_stops_the_run_and_nothing_else_holds_it():
    tmp = tempdir("teaport-idle-")
    script, env = _idle_script(tmp, [(200, {"live": True})], secs="1")
    rc, out = _bash(tmp, script, env)
    assert rc == 1 and "nothing was swapped" in out and "RETURNED" not in out, out
    # No brain (curl reaches nothing: 000), or nobody talking: no wait, nothing said.
    for answers in ([("000", {})], [(200, {"live": False, "asleep": True})]):
        tmp = tempdir("teaport-idle-")
        script, env = _idle_script(tmp, answers)
        rc, out = _bash(tmp, script, env)
        assert rc == 0 and "RETURNED" in out and "waiting" not in out, (answers, out)
    # An answer that cannot be read is said, and does not hold the update.
    tmp = tempdir("teaport-idle-")
    script, env = _idle_script(tmp, [(401, {"detail": "bad or missing token"})])
    rc, out = _bash(tmp, script, env)
    assert rc == 0 and "RETURNED" in out and "cannot tell whether anyone is talking" in out, out
    # Off (0, unset, junk) or a dry run: the brain is not asked.
    for secs, dry in (("0", "0"), ("", "0"), ("soon", "0"), ("60", "1")):
        tmp = tempdir("teaport-idle-")
        script, env = _idle_script(tmp, [(200, {"live": True})], secs=secs)
        rc, out = _bash(tmp, script.replace("DRY_RUN=0", f"DRY_RUN={dry}"), env)
        assert rc == 0 and "RETURNED" in out and not os.path.exists(os.path.join(tmp, "curl.log")), (secs, dry, out)


# --- the CLI ----------------------------------------------------------------------------

def _cli(tmp, *args, active="inactive", check_rc=10, run_json=None, log="", tty=False):
    """`teaport update ...` with the brain's python, systemctl and sudo stubbed in PATH."""
    bindir = pathlib.Path(tmp, "bin")
    bindir.mkdir(exist_ok=True)
    venv = pathlib.Path(tmp, "opt/brain/bin")
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "python").write_text(
        "#!/bin/sh\necho \"PY $*\" >> \"$T/calls\"\necho 'changed: brain'\n"
        f"case \"$*\" in *--exit-code*) exit {check_rc} ;; esac\nexit 0\n")
    state = pathlib.Path(tmp, "state")
    state.mkdir(exist_ok=True)
    (bindir / "systemctl").write_text(
        "#!/bin/sh\necho \"SYSTEMCTL $*\" >> \"$T/calls\"\n"
        "case \"$1\" in show) cat \"$T/active\" ;; start)\n"
        # The unit runs: it writes its state (the test's, with a fresh start time) and ends.
        "  [ ! -e \"$T/run.next\" ] || { sed \"s/STARTED/$(date +%s)/\" \"$T/run.next\" > \"$T/state/run.json\"; cp \"$T/log.next\" \"$T/state/log\"; } ;;\n"
        "esac\n")
    (bindir / "sudo").write_text("#!/bin/sh\necho \"SUDO $*\" >> \"$T/calls\"\nexec \"$@\"\n")
    for f in bindir.iterdir():
        f.chmod(0o755)
    (venv / "python").chmod(0o755)
    pathlib.Path(tmp, "active").write_text(active + "\n")
    if run_json is not None:
        pathlib.Path(tmp, "run.next").write_text(json.dumps(run_json))
        pathlib.Path(tmp, "log.next").write_text(log)
    env = {**os.environ, "T": str(tmp), "PATH": f"{bindir}:{os.environ['PATH']}",
           "TEAPORT_PREFIX": os.path.join(tmp, "opt"), "TEAPORT_ETC": os.path.join(tmp, "etc"),
           "TEAPORT_UPDATE_STATE": str(state), "HOME": str(tmp)}
    cmd = ["bash", str(CLI), "update", *args]
    if tty:
        cmd = ["script", "-qec", " ".join(cmd), "/dev/null"]
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, input="y\n" if tty else "",
                       timeout=60)
    calls = pathlib.Path(tmp, "calls")
    return p.returncode, re.sub(r"\x1b\[[\d;]+m", "", p.stdout + p.stderr), \
        calls.read_text() if calls.exists() else ""


def test_update_starts_the_unit_with_the_sudoers_lines_arguments_and_follows_it_to_its_end():
    tmp = tempdir("teaport-cli-update-")
    run = {"state": "done", "started": "STARTED", "message": "installed: brain", "channel": "stable"}
    rc, out, calls = _cli(tmp, "--yes", run_json=run, log="==> download teaport-brain_0.1.1\n==> done\n")
    assert rc == 0, out
    assert "SUDO systemctl start --no-block teaport-update.service" in calls, calls
    assert "==> download teaport-brain_0.1.1" in out and "update: installed: brain" in out, out
    # The check ran once, asking whether there is a step.
    assert calls.count("PY ") == 1 and "check --exit-code" in calls, calls


def test_a_failed_run_fails_the_command_and_names_the_reason():
    tmp = tempdir("teaport-cli-update-")
    run = {"state": "failed", "started": "STARTED", "message": "install.sh --only brain failed: rolled back"}
    rc, out, calls = _cli(tmp, "--yes", run_json=run, log="error: rolled back\n")
    assert rc == 1 and "update failed: install.sh --only brain failed: rolled back" in out, out


def test_update_asks_first_and_starts_nothing_when_there_is_nothing_to_start():
    # Nothing teaport update installs: no question, no unit.
    tmp = tempdir("teaport-cli-update-")
    rc, out, calls = _cli(tmp, check_rc=0)
    assert rc == 0 and "start" not in calls, (out, calls)
    # Not on a terminal and no --yes: it does not guess.
    tmp = tempdir("teaport-cli-update-")
    rc, out, calls = _cli(tmp)
    assert rc == 1 and "teaport update --yes" in out and "start" not in calls, (out, calls)
    # On a terminal, a yes starts it.
    if shutil.which("script"):
        tmp = tempdir("teaport-cli-update-")
        rc, out, calls = _cli(tmp, run_json={"state": "done", "started": "STARTED", "message": "installed: brain"}, tty=True)
        assert rc == 0 and "Install it now?" in out and "start --no-block" in calls, (out, calls)
    # The channel is the box's: --channel only goes with --check.
    tmp = tempdir("teaport-cli-update-")
    rc, out, calls = _cli(tmp, "--yes", "--channel", "beta")
    assert rc == 1 and "--channel goes with --check" in out and "PY" not in calls, out


def test_status_says_how_the_last_run_went():
    tmp = tempdir("teaport-cli-status-")
    rc, out, _ = _cli(tmp, "--status")
    assert rc == 0 and "no update has been run" in out, out
    state = pathlib.Path(tmp, "state")
    (state / "run.json").write_text(json.dumps({"state": "running", "started": time.time(),
                                                "step": "engine", "channel": "beta", "message": ""}))
    rc, out, _ = _cli(tmp, "--status", active="activating")
    assert rc == 0 and "update   running" in out and "now      engine" in out and "channel beta" in out, out
    # Running, by a file a power cut left: the unit is not.
    rc, out, _ = _cli(tmp, "--status")
    assert rc == 0 and "update   interrupted" in out, out
    rc, out, _ = _cli(tmp, "--status", "--json")
    assert rc == 0 and json.loads(out)["state"] == "interrupted", out


# --- the package ------------------------------------------------------------------------

def test_the_package_ships_the_helper_and_its_unit_and_the_one_line_that_starts_it():
    recipe = (ROOT / "packaging/nfpm.yaml").read_text()
    assert "- src: packaging/deb/update_apply.py\n    dst: /usr/lib/teaport/update_apply.py\n" in recipe
    assert ("- src: packaging/deb/teaport-update.service\n"
            "    dst: /usr/lib/systemd/system/teaport-update.service\n") in recipe
    assert "\n  - git\n" in recipe
    unit = (ROOT / "packaging/deb/teaport-update.service").read_text()
    assert "Type=oneshot" in unit and "StateDirectory=teaport-update" in unit
    assert "ExecStart=/usr/bin/python3 -I /usr/lib/teaport/update_apply.py" in unit
    assert "[Install]" not in unit.splitlines()   # started on request, never at boot
    helper = HELPER.read_text()
    assert 'STATE = "/var/lib/teaport-update"' in helper   # the unit's StateDirectory, root's
    # sudoers-sync: the update's line is exactly the command the CLI runs, no wildcard.
    tmp = tempdir("teaport-sudoers-")
    text = (ROOT / "packaging/deb/sudoers-sync").read_text()
    text = text.replace("f=/etc/sudoers.d/teaport-config", f"f={tmp}/out")
    text = text.replace("helper=/usr/lib/teaport/config_apply.py", f"helper={tmp}/helper")
    text = text.replace("install -m 0440 -o root -g root", "install -m 0440")
    pathlib.Path(tmp, "helper").write_text("")
    pathlib.Path(tmp, "s").write_text(text)
    p = subprocess.run(["sh", f"{tmp}/s", "bob"], capture_output=True, text=True)
    if p.returncode != 0 and "visudo" in p.stderr + p.stdout:
        appliance.skip("no visudo here")
    assert p.returncode == 0, p.stderr
    lines = [ln for ln in pathlib.Path(tmp, "out").read_text().splitlines() if not ln.startswith("#")]
    assert lines[1] == "bob ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block teaport-update.service", lines
    cli = CLI.read_text()
    assert 'SUDO systemctl start --no-block "$UPDATE_UNIT"' in cli and "UPDATE_UNIT=teaport-update.service" in cli


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
