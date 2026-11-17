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
#     is not a plain repo and ref, a manifest from anywhere but the download host, a state
#     directory that is not root's own, and a brain tree it would run as root that is not
#     root's throughout, has a file dpkg does not list, or one dpkg says changed.
#   - install.sh keeps the install trees root's (own_install_trees: a symlink or special
#     file in them is removed) and writes there only as root; the run user's own part goes
#     through as_user (setpriv) when root runs it for them. render_unit takes values from the
#     run user's home literally (no sed injection, no extra unit line); a sandbox name that
#     is not a plain name is dropped. wait_for_idle holds a restart while the brain says
#     someone is talking, up to its limit, with the token off curl's command line.
#   - `teaport update` asks, starts the unit with plain systemctl (polkit lets the brain's
#     user), and follows that run by its InvocationID to its end; --status says how the
#     last one went.
#   - The package ships the helper and the unit, depends on git and polkitd, and
#     sudoers-sync writes the polkit rule that lets the run user start it and nothing else.
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
    mod.untrusted_brain = lambda: None
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
    # Root's own HOME: no tool the root run starts reads a config from the run user's home.
    assert env["TEAPORT_USER"] == user and env["HOME"] == "/root", env
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


class _P:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_the_brains_code_runs_as_root_only_when_dpkg_vouches_for_all_of_it():
    tmp = tempdir("teaport-apply-trust-")
    mod = _helper(_state(tmp))
    spec = importlib.util.spec_from_file_location("ua_trust", HELPER)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    prefix = os.path.join(tmp, "opt")
    tree = os.path.join(prefix, "brain")
    os.makedirs(os.path.join(tree, "bin"))
    pathlib.Path(tree, "bin", "teaport-brain").write_text("#!/bin/sh\n")
    os.symlink("/usr/bin/python3", os.path.join(tree, "bin", "python"))
    for d in (prefix, tree, os.path.join(tree, "bin")):
        os.chmod(d, 0o755)
    os.chmod(os.path.join(tree, "bin", "teaport-brain"), 0o755)
    real.ROOT_UID, real.PREFIX, real.BRAIN_TREE = os.getuid(), prefix, tree
    real.above = lambda p: [p]
    listed = "\n".join(["/.", prefix, tree + "/", os.path.join(tree, "bin"),
                        os.path.join(tree, "bin", "teaport-brain"), os.path.join(tree, "bin", "python")])
    verify = {"out": ""}

    def runner(cmd, **k):
        if cmd[0] == "dpkg-query":
            return _P(0, listed)
        return _P(0, verify["out"])
    assert real.untrusted_brain(runner) is None
    # A symlink the package lists, but leading out of the tree (a directory swapped for one
    # while the tree was the run user's): what it leads to is not checked, so it is refused.
    link = os.path.join(tree, "bin", "python")
    os.unlink(link)
    os.symlink(tmp, link)
    assert "leads out of the package's tree" in real.untrusted_brain(runner)
    os.unlink(link)
    os.symlink("/usr/bin/python3", link)
    # A file the package does not list (a .pth, say, which every python start runs).
    stray = pathlib.Path(tree, "evil.pth")
    stray.write_text("import os\n")
    stray.chmod(0o644)
    assert "is not the package's" in real.untrusted_brain(runner)
    stray.unlink()
    # One another user could write.
    os.chmod(os.path.join(tree, "bin", "teaport-brain"), 0o775)
    assert "is not root's alone" in real.untrusted_brain(runner)
    os.chmod(os.path.join(tree, "bin", "teaport-brain"), 0o755)
    # A file dpkg says changed.
    verify["out"] = "??5?????? /opt/teaport/brain/lib/x.py\n"
    assert "changed after it was installed" in real.untrusted_brain(runner)
    verify["out"] = ""
    # The directory above it writable by others, or no package at all.
    os.chmod(prefix, 0o777)
    assert "is not root's own directory" in real.untrusted_brain(runner)
    os.chmod(prefix, 0o755)
    assert "not installed" in real.untrusted_brain(lambda cmd, **k: _P(1))
    # main() stops on it before the plan is read, and says how to repair it.
    mod.untrusted_brain = lambda: "/opt/teaport/brain/evil.pth is not the package's"
    mod.read_plan = lambda: (_ for _ in ()).throw(AssertionError("must not run the brain's code"))
    assert mod.main() == 1
    msg = _status(mod)["message"]
    assert "evil.pth" in msg and "install.sh --only brain" in msg, msg


def test_the_plan_and_the_clone_take_nothing_they_are_not_sure_of():
    mod = _helper(tempdir("teaport-apply-"))
    P = _P
    plan = mod.read_plan(runner=lambda *a, **k: P(0, json.dumps(_plan(["engine"]))))
    assert plan["steps"] == ["engine"]
    for steps in (["rm -rf /"], ["--only"], "engine"):
        try:
            mod.read_plan(runner=lambda *a, **k: P(0, json.dumps({**_plan([]), "steps": steps})))
        except RuntimeError as e:
            assert "cannot run" in str(e)
        else:
            raise AssertionError(steps)
    # A plan against a manifest from anywhere but the download host is not installed.
    for url in ("https://evil.example/manifest/stable.json", "/tmp/m.json",
                "https://get.teaspoon.tech/manifest/stable.json.evil"):
        try:
            mod.read_plan(runner=lambda *a, **k: P(0, json.dumps({**_plan(["brain"]), "url": url})))
        except RuntimeError as e:
            assert "does not install from" in str(e), e
        else:
            raise AssertionError(url)
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
    # A release tag, the checkout at its commit, an installer that says UPDATE_PROTOCOL=1.
    def git_fake(tags, head="c0ffee", protocol=True):
        calls = []

        def runner(cmd, **k):
            calls.append((cmd, k))
            if cmd[1] == "ls-remote":
                return P(0, "".join(f"{sha}\t{ref}\n" for ref, sha in tags.items()))
            if cmd[1] == "clone":
                os.makedirs(cmd[-1])
                pathlib.Path(cmd[-1], "install.sh").write_text(
                    "#!/bin/bash\nset -e\n" + ("UPDATE_PROTOCOL=1\n" if protocol else "") + "main\n")
                return P(0)
            if cmd[1] == "rev-parse":
                return P(0, head + "\n")
            raise AssertionError(cmd)
        return runner, calls
    w = tempdir("teaport-clone-")
    runner, calls = git_fake({"refs/tags/brain-v0.1.1": "aaa", "refs/tags/brain-v0.1.1^{}": "c0ffee"})
    got = mod.clone({"repo": "Teaspoon-AI/teaport", "tag": "brain-v0.1.1"}, w, runner=runner)
    assert got == os.path.join(w, "teaport"), got
    clone_cmd, k = next((c, k) for c, k in calls if c[1] == "clone")
    assert clone_cmd[-3:] == ["brain-v0.1.1", "https://github.com/Teaspoon-AI/teaport.git", got], clone_cmd
    assert "--depth" in clone_cmd and k["env"]["GIT_TERMINAL_PROMPT"] == "0" and k["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    # A branch (no such tag: "main" moves), a checkout not at the tag, an older installer.
    for tags, head, protocol, why in (
            ({}, "c0ffee", True, "not a release tag"),
            ({"refs/tags/brain-v0.1.1": "c0ffee"}, "beef", True, "is not at brain-v0.1.1"),
            ({"refs/tags/brain-v0.1.1": "c0ffee"}, "c0ffee", False, "predates teaport update")):
        runner, calls = git_fake(tags, head, protocol)
        try:
            mod.clone({"repo": "Teaspoon-AI/teaport", "tag": "brain-v0.1.1"}, tempdir("teaport-clone-"), runner=runner)
        except RuntimeError as e:
            assert why in str(e), (why, e)
        else:
            raise AssertionError(why)
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


# The functions that write the install trees ($PREFIX's engine files, $STATE's downloads):
# the trees are root's, so every write, rename, link and removal there is SUDO's, never the
# run user's (run/as_user) and never a bare one. ldd is the run user's: it can run what it
# looks at.
ROOT_TREE_FUNCTIONS = ("download", "engine_fetch", "engine_keep_live", "engine_swap_in",
                       "engine_swap_abort", "engine_swap_recover", "engine_restore", "engine_prune",
                       "manifest_take_py", "brain_deb_fetch", "brain_deb_keep", "main_rollback_engine",
                       "phase_engine", "eula_gate", "own_dir", "own_tree", "own_install_trees",
                       "installer_lock")
TEMP_WRITE = re.compile(r'-o "\$(txt|tmp)" ')
WRITE = re.compile(r"(?:^|[;&|{(!]\s*|\bthen\s+|\belse\s+|\bdo\s+|\|\|\s*|&&\s*)(mv|ln|cp|mkdir|rm|touch|chmod|chown|tee|curl|install)\s")


def test_the_install_trees_are_written_only_as_root():
    text = INSTALL.read_text()
    for name in ROOT_TREE_FUNCTIONS:
        for raw in _fn(name).splitlines()[1:]:
            ln = raw.split(" #")[0].strip()
            if ln.startswith("#"):
                continue
            if TEMP_WRITE.search(ln):
                continue   # into this run's own temp file (mktemp), not the trees
            for m in WRITE.finditer(ln):
                assert re.search(r"SUDO\s+(env\s+\S+\s+)?$", ln[:m.start(1)]), (name, raw)
            assert not re.search(r"[^2&]>\s*\"\$", ln) or "/dev/null" in ln, (name, raw)
            assert "as_user" not in ln or "as_user ldd" in ln, (name, raw)
            assert not re.search(r"\brun\s+(mv|ln|cp|mkdir|rm|touch|chmod|curl)\b", ln), (name, raw)
            assert "chown" not in ln or "RUN_USER" not in ln, (name, raw)
    # Nothing hands the engine's files or the downloads to the run user any more; the one
    # tree that stays theirs is the bridge's.
    handed = [ln for ln in text.splitlines() if re.search(r"chown\b.*RUN_USER", ln) and not ln.lstrip().startswith("#")]
    assert handed == ['  SUDO mkdir -p "$PREFIX/bridge"; SUDO chown -R -h -P "$RUN_USER" "$PREFIX/bridge"'], handed
    assert "as_user ldd \"$bin\"" in text
    # The trees are taken back first thing; the restarts wait for idle, before engine.env.
    assert "  installer_lock\n  own_install_trees\n" in text
    assert "  wait_for_idle\n  log \"engine.env:" in text
    assert "  wait_for_idle\n  brain_deb_keep\n" in text


OWN_STUB = r"""
SUDO() { printf 'SUDO %s\n' "$*" >> "$T/sudo.log"; case "$1" in chown|chmod) : ;; *) "$@" ;; esac; }
sudo_ready() { :; }
DRY_RUN=0 BRAIN_PKG=teaport-brain
"""


def test_taking_a_tree_back_removes_what_an_install_never_makes_there():
    tmp = tempdir("teaport-owntree-")
    root = pathlib.Path(tmp, "opt")
    (root / "bin/g2p").mkdir(parents=True)
    (root / "bin/voxtral").write_text("engine\n")
    os.symlink("/etc/shadow", root / "bin/g2p/kokoro_g2p.v2.dict")
    os.mkfifo(root / "bin/pipe")
    os.symlink("/etc", root / "models")
    rc, out = _bash(tmp, OWN_STUB + _fn("own_tree") + f'own_tree "{root}/bin"; own_tree "{root}/models"; own_tree "{root}/missing"\n')
    assert rc == 0, out
    assert (root / "bin/voxtral").read_text() == "engine\n"
    assert not os.path.lexists(root / "bin/g2p/kokoro_g2p.v2.dict") and not os.path.lexists(root / "bin/pipe"), out
    assert not os.path.lexists(root / "models") and os.path.isdir("/etc"), out
    calls = pathlib.Path(tmp, "sudo.log").read_text()
    # Twice: a file made in a directory before the first pass reached it is caught by the second.
    assert calls.count(f"chown -R -h -P root:root {root}/bin") == 2 and f"chmod -R go-w {root}/bin" in calls, calls


def test_what_the_run_user_made_in_the_trees_is_dropped_not_adopted():
    """A box from before root owned the install: everything in it is the run user's (here,
    the test's own user, so every path counts as "not root's")."""
    tmp = tempdir("teaport-owninstall-")
    prefix, state = pathlib.Path(tmp, "opt"), pathlib.Path(tmp, "state")
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/voxtral").write_text("engine\n")
    (prefix / "manifest.json").write_text("{}\n")
    snap = prefix / "engine.rollback/999999-x"
    snap.mkdir(parents=True)
    (snap / "teaport-engine.service").write_text("[Service]\nUser=root\n")
    (prefix / "engine.swapping").write_text(str(snap) + "\n")
    brain = prefix / "brain"
    (brain / "bin").mkdir(parents=True)
    (brain / "bin/teaport-brain").write_text("#!/bin/sh\n")
    os.symlink("/usr/bin/python3", brain / "bin/python")
    os.symlink(tmp, brain / "lib")                       # a listed directory, swapped for a link out
    evil = brain / "bin" / "x\n"                          # a name with a newline...
    (evil / "etc").mkdir(parents=True)                    # ...that reads "/etc" on its own line
    state.mkdir()
    (state / "eula-accepted.json").write_text('{"version": "1.0"}\n')
    (state / "brain-deb").mkdir()
    (state / "brain-deb/teaport-brain_9_arm64.deb").write_text("plant\n")
    listed = "\n".join(["/.", str(prefix), str(brain), str(brain / "bin"), str(brain / "bin/teaport-brain"),
                        str(brain / "bin/python"), str(brain / "lib")])
    pathlib.Path(tmp, "listed").write_text(listed + "\n")
    stub = OWN_STUB + (f'PREFIX="{prefix}" STATE="{state}"\n'
                       + _line("PREFIX_KEPT=") + _line("PREFIX_DROPPED=") + _line("STATE_KEPT=")
                       + "brain_package_version() { echo 0.1.0; }\n"
                       + 'dpkg-query() { cat "$T/listed"; }\n'
                       + "die() { printf 'DIE %s\\n' \"$*\"; exit 1; }\n")
    fns = "".join(_fn(f) for f in ("own_dir", "not_roots", "own_tree", "own_install_trees"))
    rc, out = _bash(tmp, stub + fns + "own_install_trees\n")
    assert rc == 0, out
    # The engine's staged and kept copies and the swap marker: removed, not adopted.
    assert not (prefix / "engine.rollback").exists() and not (prefix / "engine.swapping").exists(), out
    # The live engine and the manifest: taken over.
    assert (prefix / "bin/voxtral").exists() and (prefix / "manifest.json").exists(), out
    # The brain's tree: the venv's link to /usr stays; the link out goes; the name with a
    # newline goes as itself, and nothing named /etc is touched.
    assert os.path.islink(brain / "bin/python") and not os.path.lexists(brain / "lib"), out
    assert not os.path.lexists(evil) and (brain / "bin/teaport-brain").exists(), out
    calls = pathlib.Path(tmp, "sudo.log").read_text()
    assert "rm -rf -- /etc\n" not in calls, calls
    # $STATE: downloads the run user made go; the license receipt is taken over.
    assert not (state / "brain-deb").exists() and (state / "eula-accepted.json").exists(), out


def test_a_root_run_reads_no_configuration_from_a_home():
    """install.sh as root for the run user (teaport update) runs python, curl and git; none
    of them may read a config from a home the run user writes."""
    code = [ln for ln in INSTALL.read_text().splitlines() if not ln.lstrip().startswith("#")]
    for ln in code:
        if re.search(r"\bpython3 -(c|) ", ln) and "python3 -I" not in ln:
            raise AssertionError(f"python3 without -I: {ln}")
        for m in re.finditer(r"\bcurl (-\S+)", ln):
            if 'warn "' in ln:
                continue   # an instruction for a person, not a command
            assert m.group(1) == "-q", f"curl without -q first: {ln}"
    git = [ln for ln in code if re.search(r"\bgit -C", ln)]
    assert all("fetch" in ln or "config core.sshCommand" in ln for ln in git), git   # the operator's own fetch only
    homes = [ln for ln in code if "$HOME" in ln]
    assert homes == ['RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6 || true)"; RUN_HOME="${RUN_HOME:-$HOME}"'], homes
    assert "\nUPDATE_PROTOCOL=1\n" in INSTALL.read_text()


def test_a_unit_takes_values_from_the_run_users_home_literally():
    tmp = tempdir("teaport-render-")
    (pathlib.Path(tmp, "systemd")).mkdir()
    pathlib.Path(tmp, "systemd/x.service.in").write_text("[Service]\nUser=@USER@\nExecStart=/bin/nemoclaw @SANDBOX@ connect\n")
    stub = (r'''
write_root() { printf '%s\n' "$2" > "$T/out.service"; }
DRY_RUN=0 RUN_USER=bob PREFIX=/opt/teaport ETC=/etc/teaport SECRETS=/home/bob/.config/teaport RUN_HOME=/home/bob
''' + f'HERE="{tmp}"\n')
    evil = "x#;s#^User=.*#touch $T/pwned#e;s#zz#zz"
    rc, out = _bash(tmp, stub + _fn("sed_value") + _fn("render_unit") + f"SANDBOX='{evil}'; render_unit x.service.in x.service\n")
    assert rc == 0, out
    unit = pathlib.Path(tmp, "out.service").read_text()
    assert "User=bob\n" in unit and f"ExecStart=/bin/nemoclaw {evil} connect" in unit, unit
    assert not os.path.exists(os.path.join(tmp, "pwned")), "sed ran the injected command"
    # A value with a line break would add a line to the unit: refused.
    rc, out = _bash(tmp, stub + _fn("sed_value") + _fn("render_unit") + "SANDBOX=$'a\\nUser=root'; render_unit x.service.in x.service\n")
    assert rc == 1 and "spans lines" in out, out
    # The sandbox name itself must be a plain name, or there is none.
    home = pathlib.Path(tmp, "home")
    (home / ".nemoclaw").mkdir(parents=True)
    (home / ".local/bin").mkdir(parents=True)
    (home / ".local/bin/nemoclaw").write_text("#!/bin/sh\n")
    (home / ".local/bin/nemoclaw").chmod(0o755)
    for name, want in (("my-sandbox_1.2", "my-sandbox_1.2"), (evil, ""), ("-x", ""), ("a b", "")):
        (home / ".nemoclaw/sandboxes.json").write_text(json.dumps({"defaultSandbox": name}))
        rc, out = _bash(tmp, f"have() {{ false; }}\nSANDBOX=\nRUN_HOME='{home}'\n" + _fn("detect_sandbox") + 'detect_sandbox; echo "SB[$SANDBOX]"\n',
                        {"HOME": str(home)})
        assert rc == 0 and f"SB[{want}]" in out, (name, out)


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

def _cli(tmp, *args, active="inactive", check_rc=10, run_json=None, log="", tty=False, inv=None, delay=0):
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
        "case \"$*\" in\n"
        "  *ActiveState*) cat \"$T/active\" ;;\n"
        "  *InvocationID*) cat \"$T/inv\" 2>/dev/null || true ;;\n"
        # The unit runs: a new invocation, which writes its state (the test's) and ends.
        "  *list-jobs*) [ ! -e \"$T/queued\" ] || echo '1 teaport-update.service start waiting' ;;\n"
        "  start*) [ ! -e \"$T/run.next\" ] || {\n"
        "      run() { sed \"s/STARTED/$(date +%s)/\" \"$T/run.next\" > \"$T/state/run.json\"; cp \"$T/log.next\" \"$T/state/log\"; }\n"
        # Delayed: the job waits in the queue (the unit not loaded yet: no InvocationID), then runs.
        "      if [ \"${DELAY:-0}\" = 0 ]; then echo inv-new > \"$T/inv\"; run\n"
        "      else : > \"$T/queued\"; ( sleep \"$DELAY\"; rm -f \"$T/queued\"; run ) & fi; } ;;\n"
        "esac\n")
    (bindir / "sudo").write_text("#!/bin/sh\necho \"SUDO $*\" >> \"$T/calls\"\nexec \"$@\"\n")
    for f in bindir.iterdir():
        f.chmod(0o755)
    (venv / "python").chmod(0o755)
    pathlib.Path(tmp, "active").write_text(active + "\n")
    if run_json is not None:
        run_json = {"invocation": "inv-new", **run_json}
        pathlib.Path(tmp, "run.next").write_text(json.dumps(run_json))
        pathlib.Path(tmp, "log.next").write_text(log)
    if inv is not None:
        pathlib.Path(tmp, "inv").write_text(inv + "\n")
    env = {**os.environ, "T": str(tmp), "PATH": f"{bindir}:{os.environ['PATH']}", "DELAY": str(delay),
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


def test_update_starts_the_unit_and_follows_it_to_its_end():
    tmp = tempdir("teaport-cli-update-")
    run = {"state": "done", "started": "STARTED", "message": "installed: brain", "channel": "stable"}
    rc, out, calls = _cli(tmp, "--yes", run_json=run, log="==> download teaport-brain_0.1.1\n==> done\n")
    assert rc == 0, out
    assert "SYSTEMCTL start --no-block teaport-update.service" in calls and "SUDO" not in calls, calls
    assert "==> download teaport-brain_0.1.1" in out and "update: installed: brain" in out, out
    # The check ran once, asking whether there is a step.
    assert calls.count("PY ") == 1 and "check --exit-code" in calls, calls


def test_an_earlier_run_is_never_taken_for_this_one():
    # The last run's run.json, with a start time from a clock ahead of this one (a box with no
    # RTC): the new run is told by its invocation, not by its time.
    tmp = tempdir("teaport-cli-update-")
    state = pathlib.Path(tmp, "state")
    state.mkdir()
    (state / "run.json").write_text(json.dumps({"state": "done", "started": time.time() + 86400,
                                                "invocation": "inv-old", "message": "installed: engine"}))
    run = {"state": "failed", "started": "STARTED", "message": "install.sh --only brain failed: x"}
    rc, out, calls = _cli(tmp, "--yes", run_json=run, inv="inv-old")
    assert rc == 1 and "install.sh --only brain failed: x" in out and "installed: engine" not in out, out
    # The start waits in systemd's queue, and the unit (unloaded since its last run) has no
    # InvocationID yet: the old run.json is still not taken for this run, and the wait is
    # not "did not start".
    tmp = tempdir("teaport-cli-update-")
    state = pathlib.Path(tmp, "state")
    state.mkdir()
    (state / "run.json").write_text(json.dumps({"state": "done", "started": 1.0, "invocation": "inv-old",
                                                "message": "installed: engine"}))
    run = {"state": "done", "started": "STARTED", "message": "installed: brain"}
    rc, out, calls = _cli(tmp, "--yes", run_json=run, inv="", delay=3)
    assert rc == 0 and "update: installed: brain" in out and "installed: engine" not in out, out


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

def test_the_package_ships_the_helper_its_unit_and_the_polkit_rule_that_starts_it():
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
    assert "\n  - polkitd\n" in recipe
    # sudoers-sync: the polkit rule names the unit, the verb and the user; sudoers has only
    # the config page's line.
    tmp = tempdir("teaport-sudoers-")
    text = (ROOT / "packaging/deb/sudoers-sync").read_text()
    text = text.replace("f=/etc/sudoers.d/teaport-config", f"f={tmp}/out")
    text = text.replace("helper=/usr/lib/teaport/config_apply.py", f"helper={tmp}/helper")
    text = text.replace("rules=/etc/polkit-1/rules.d/50-teaport-update.rules", f"rules={tmp}/rules.d/50-teaport-update.rules")
    text = text.replace("install -m 0440 -o root -g root", "install -m 0440").replace("install -m 0644 -o root -g root", "install -m 0644")
    pathlib.Path(tmp, "helper").write_text("")
    pathlib.Path(tmp, "rules.d").mkdir()
    pathlib.Path(tmp, "s").write_text(text)
    p = subprocess.run(["sh", f"{tmp}/s", "bob"], capture_output=True, text=True)
    if p.returncode != 0 and "visudo" in p.stderr + p.stdout:
        appliance.skip("no visudo here")
    assert p.returncode == 0, p.stderr
    lines = [ln for ln in pathlib.Path(tmp, "out").read_text().splitlines() if not ln.startswith("#")]
    assert lines == ["bob ALL=(root) NOPASSWD: /usr/bin/python3 " + f"{tmp}/helper *"], lines
    rule = pathlib.Path(tmp, "rules.d/50-teaport-update.rules").read_text()
    for want in ('action.id == "org.freedesktop.systemd1.manage-units"', 'action.lookup("unit") == "teaport-update.service"',
                 'action.lookup("verb") == "start"', 'subject.user == "bob"', "return polkit.Result.YES"):
        assert want in rule, (want, rule)
    if shutil.which("node"):   # the rule parses as JavaScript
        js = "var polkit={addRule:function(f){var r=f({id:'org.freedesktop.systemd1.manage-units',lookup:function(k){return {unit:'teaport-update.service',verb:'start'}[k];}},{user:'bob'}); if(r!=='yes') process.exit(3);},Result:{YES:'yes'}};" + rule
        assert subprocess.run(["node", "-e", js]).returncode == 0
    # No polkit on the box: no rule, said, and the config page's line all the same.
    pathlib.Path(tmp, "rules.d/50-teaport-update.rules").unlink()
    pathlib.Path(tmp, "rules.d").rmdir()
    p = subprocess.run(["sh", f"{tmp}/s", "bob"], capture_output=True, text=True)
    assert p.returncode == 0 and "no polkit here" in p.stderr, p.stderr
    cli = CLI.read_text()
    assert 'systemctl start --no-block "$UPDATE_UNIT"' in cli and "SUDO systemctl start" not in cli


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
