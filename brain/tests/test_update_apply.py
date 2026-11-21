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
#   - It takes nothing it is not sure of: the brain's comparison runs as the run user and
#     only its steps and channel are used; the manifest is read here from the download
#     host's address for that channel; only Teaspoon-AI/teaport's release tag is cloned, at
#     the tag's commit, with UPDATE_PROTOCOL=1; the state directory must be root's own.
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
    def read_manifest(channel, dest):
        manifest = {"channel": channel, "brain": {"repo": "Teaspoon-AI/teaport", "tag": "brain-v0.1.1",
                    "deb": {"url": "https://get.teaspoon.tech/dl/brain/teaport-brain_0.1.1_arm64.deb"}}}
        pathlib.Path(dest).write_text(json.dumps(manifest))
        return manifest
    mod.read_manifest = read_manifest
    mod.box_brain = lambda: "0.1.0"   # the brain on the box, in any state: older than the release
    mod.box_engine = lambda: None     # no installed engine record root alone can write
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

def _sudo_is_rs() -> bool:
    """This machine's sudo is sudo-rs (sudoers-sync leaves NOLOG_INPUT out for it)."""
    out = ""
    for tool in ("sudo", "visudo"):
        try:
            out += subprocess.run([tool, "--version"], capture_output=True, text=True).stdout
        except OSError:
            pass
    return "sudo-rs" in out


def test_a_run_installs_the_steps_in_order_for_the_brains_user():
    tmp = tempdir("teaport-apply-")
    mod = _helper(_state(tmp))
    clones, ran = [], []
    mod.read_plan = lambda *a: _plan(["engine", "brain"])

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
    # This run's own copy of the channel's manifest (read once, in the work directory).
    assert env["TEAPORT_MANIFEST_URL"].endswith("/manifest.json"), env
    assert int(env["TEAPORT_WAIT_IDLE_SECS"]) > 0, env
    # The channel is the run user's to pick: an update never installs an older brain.
    assert env["TEAPORT_NEWER_ONLY"] == "1", env
    # The installer the channel's manifest names (read_source), not one the plan names.
    assert clones[0][0] == {"repo": "Teaspoon-AI/teaport", "tag": "brain-v0.1.1"}
    assert not os.path.exists(clones[0][1]), "the clone is removed after the run"
    with open(mod.LOG) as f:
        assert f.read() == "output of engine\noutput of brain\n"
    assert os.stat(mod.STATUS).st_mode & 0o777 == 0o640


def test_a_failed_step_stops_the_run_and_says_why():
    tmp = tempdir("teaport-apply-")
    mod = _helper(_state(tmp))
    ran = []
    mod.read_plan = lambda *a: _plan(["engine", "brain"])
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
    mod.read_plan = lambda *a: _plan([], manual=["plugin"])
    mod.clone = lambda *a: (_ for _ in ()).throw(AssertionError("nothing to clone"))
    assert mod.main() == 0
    st = _status(mod)
    assert st["state"] == "done" and "plugin" in st["message"] and "full install.sh" in st["message"], st
    # A plan with steps but a manifest that names no installer this takes fails before anything.
    mod.read_plan = lambda *a: _plan(["brain"])

    mod.read_manifest = lambda channel, dest: {"brain": {"repo": "evil/repo", "tag": "v1"}}
    assert mod.main() == 1 and "names no installer" in _status(mod)["message"]


def test_a_state_directory_that_is_not_roots_own_is_refused():
    tmp = tempdir("teaport-apply-")
    state = _state(tmp)
    mod = _helper(state)
    mod.read_plan = lambda *a: (_ for _ in ()).throw(AssertionError("must not get this far"))
    os.chmod(state, 0o777)
    assert mod.main() == 1 and not os.path.exists(mod.STATUS)
    os.chmod(state, 0o755)
    mod.ROOT_UID = os.getuid() + 1   # somebody else's
    assert mod.main() == 1 and not os.path.exists(mod.STATUS)
    # A symlink to a directory that would pass is refused too.
    link = os.path.join(tmp, "link")
    os.symlink(state, link)
    mod = _helper(link)
    mod.read_plan = lambda *a: (_ for _ in ()).throw(AssertionError("must not get this far"))
    assert mod.main() == 1 and not os.path.exists(os.path.join(state, "run.json"))


class _P:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_the_brains_code_runs_as_the_run_user_and_its_answer_is_only_data():
    mod = _helper(tempdir("teaport-apply-"))
    import pwd as _pwd
    entry = _pwd.getpwuid(os.getuid())
    seen = []

    def runner(cmd, **k):
        seen.append((cmd, k))
        return _P(0, json.dumps({**_plan(["brain", "engine", "brain"]),
                                 "url": "https://evil.example/m.json",
                                 "source": {"repo": "evil/repo", "tag": "x"},
                                 "manual": ["plugin", "$(id)"]}))
    plan = mod.read_plan(entry, runner=runner)
    cmd, k = seen[0]
    # As the run user (setpriv to their ids), never root; root's HOME is not theirs.
    assert cmd[:5] == ["setpriv", f"--reuid={entry.pw_uid}", f"--regid={entry.pw_gid}", "--init-groups", "--"], cmd
    assert cmd[5:] == [mod.BRAIN_PYTHON, "-I", "-m", "teaport_brain.updates", "check", "--json"], cmd
    assert k["env"]["HOME"] == entry.pw_dir and k["cwd"] == "/", k
    # Only the steps (in STEPS order, once each), the channel and plain part names come out.
    assert plan == {"steps": ["engine", "brain"], "channel": "stable", "manual": ["plugin"]}, plan
    for bad in ({"steps": ["rm"], "channel": "stable"}, {"steps": [], "channel": "nightly"},
                {"steps": "engine", "channel": "stable"}):
        try:
            mod.read_plan(entry, runner=lambda *a, **k: _P(0, json.dumps(bad)))
        except RuntimeError:
            pass
        else:
            raise AssertionError(bad)
    for rc, out in ((1, ""), (0, "not json")):
        try:
            mod.read_plan(entry, runner=lambda *a, **k: _P(rc, out, "boom"))
        except RuntimeError:
            pass
        else:
            raise AssertionError((rc, out))


def test_the_installer_is_read_from_the_channels_manifest_and_only_teaports():
    spec = importlib.util.spec_from_file_location("ua_src", HELPER)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    asked = []
    w = tempdir("teaport-apply-man-")

    def fetch(manifest):
        def f(url):
            asked.append(url)
            return json.dumps(manifest).encode()
        return f
    got = real.read_manifest("beta", os.path.join(w, "m1.json"),
                             fetch=fetch({"brain": {"repo": "Teaspoon-AI/teaport", "tag": "brain-v0.2.0"}}))
    assert asked == ["https://get.teaspoon.tech/manifest/beta.json"], asked
    assert json.loads(pathlib.Path(w, "m1.json").read_text()) == got   # the copy the steps install from
    assert real.read_source(got) == {"repo": "Teaspoon-AI/teaport", "tag": "brain-v0.2.0"}
    for manifest in ({"brain": {"repo": "evil/teaport", "tag": "v1"}}, {"brain": {"repo": "Teaspoon-AI/teaport", "tag": "TODO"}},
                     {"brain": {"repo": "Teaspoon-AI/teaport"}}, {}):
        try:
            real.read_source(manifest)
        except RuntimeError as e:
            assert "names no installer" in str(e), e
        else:
            raise AssertionError(manifest)
    for i, bad in enumerate((lambda url: (_ for _ in ()).throw(OSError("offline")), lambda url: b"not json",
                             lambda url: b"[]")):
        try:
            real.read_manifest("stable", os.path.join(w, f"x{i}.json"), fetch=bad)
        except RuntimeError as e:
            assert "release manifest" in str(e), e
        else:
            raise AssertionError(i)
    # main(): one copy of the manifest, in the run's work directory, for every step.
    state = _state(tempdir("teaport-apply-"))
    mod = _helper(state)
    mod.read_plan = lambda *a: {"steps": ["engine", "brain"], "channel": "beta", "manual": []}
    seen, envs = [], []

    def read_manifest(channel, dest):
        seen.append(channel)
        pathlib.Path(dest).write_text("{}")
        return {"brain": {"repo": "Teaspoon-AI/teaport", "tag": "brain-v0.2.0",
                          "deb": {"url": "https://get.teaspoon.tech/dl/brain/teaport-brain_0.2.0_arm64.deb"}}}
    mod.read_manifest = read_manifest
    mod.clone = lambda source, into: (seen.append(source["tag"]), into)[1]
    mod.run_step = lambda installer, step, env, log: (envs.append(dict(env)), (0, ""))[1]
    assert mod.main() == 0
    assert seen == ["beta", "brain-v0.2.0"], seen
    assert {e["TEAPORT_MANIFEST_URL"] for e in envs} == {envs[0]["TEAPORT_MANIFEST_URL"]}
    assert envs[0]["TEAPORT_MANIFEST_URL"].endswith("/manifest.json"), envs[0]


def test_an_absent_brain_unit_is_not_taken_for_a_root_brain():
    mod = _helper(tempdir("teaport-apply-"))
    spec = importlib.util.spec_from_file_location("ua_user", HELPER)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    show = lambda out: (lambda *a, **k: _P(0, out))  # noqa: E731
    assert real.run_user(show("LoadState=loaded\nUser=bob\n")) == "bob"
    assert real.run_user(show("LoadState=loaded\nUser=\n")) == "root"   # a brain that runs as root
    for out in ("LoadState=masked\nUser=\n", "LoadState=not-found\nUser=\n", ""):
        try:
            real.run_user(show(out))
        except RuntimeError as e:
            assert "not installed here" in str(e)
        else:
            raise AssertionError(out)
    real.run_user = lambda: (_ for _ in ()).throw(RuntimeError("teaport-brain.service is not installed here"))
    real.STATE = tempdir("teaport-apply-")
    assert real.main() == 1


def test_the_plan_and_the_clone_take_nothing_they_are_not_sure_of():
    mod = _helper(tempdir("teaport-apply-"))
    P = _P
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
                       "phase_engine", "eula_gate", "own_dir", "own_install_trees",
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
    assert "  brain_deb_fetch\n  wait_for_idle\n" in text and "  brain_deb_keep\n  brain_swap " in text


LAYOUT = ROOT / "packaging/install/root_layout.py"


def _layout():
    spec = importlib.util.spec_from_file_location(f"root_layout_{time.monotonic_ns()}", LAYOUT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_trees_an_older_installer_gave_away_are_rebuilt_not_taken_over():
    """A box from before root owned the install, with what the agent (as the run user) could
    have left there: links out, special files, a planted .pth, a changed package file, a
    rollback copy with a unit of its own, a default ACL."""
    import hashlib
    tmp = tempdir("teaport-layout-")
    prefix, state = pathlib.Path(tmp, "opt"), pathlib.Path(tmp, "state")
    owner = (os.getuid(), os.getgid())
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/voxtral").write_text("engine\n")
    (prefix / "bin/voxtral").chmod(0o755)
    os.symlink("/etc", prefix / "bin/g2p")                          # the dict, reached through a link out
    (prefix / "models/voxtral").mkdir(parents=True)
    (prefix / "models/voxtral/consolidated.safetensors").write_text("model\n")
    (prefix / "models/kokoro").mkdir(parents=True)
    os.symlink("/etc/passwd", prefix / "models/kokoro/Kokoro_espeak_F16.gguf")
    (prefix / "manifest.json").write_text('{"engine": {"release": "1.6"}}\n')
    (prefix / "engine.rollback/000009-x").mkdir(parents=True)
    (prefix / "engine.rollback/000009-x/teaport-engine.service").write_text("[Service]\nUser=root\n")
    (prefix / "engine.swapping").write_text(str(prefix / "engine.rollback/000009-x") + "\n")
    (prefix / "bridge").mkdir()
    (prefix / "bridge/index.js").write_text("bridge\n")
    brain = prefix / "brain"
    (brain / "bin").mkdir(parents=True)
    (brain / "lib").mkdir()
    (brain / "bin/teaport-brain").write_text("#!/bin/sh\n")
    (brain / "lib/ok.py").write_text("ok = 1\n")
    os.utime(brain / "lib/ok.py", ns=(1_000_000_000, 1_000_000_000))
    (brain / "lib/changed.py").write_text("changed = 1\n")
    (brain / "lib/zz.pth").write_text("import os\n")                  # not the package's
    os.symlink("/usr/bin/python3", brain / "bin/python")
    os.symlink(tmp, brain / "lib2")                                   # listed as a directory, swapped for a link out
    os.mkfifo(brain / "lib/pipe")
    acl = shutil.which("setfacl") and subprocess.run(
        ["setfacl", "-d", "-m", f"u:{os.getuid()}:rwx", str(prefix)], capture_output=True).returncode == 0
    state.mkdir()
    (state / "eula-accepted.json").write_text('{"version": "1.0"}\n')
    (state / "EULA-v1.0.txt").write_text("license\n")
    (state / "journald.applied").write_text("SystemMaxUse=2G\n")
    (state / "brain-deb").mkdir()
    (state / "brain-deb/teaport-brain_9_arm64.deb").write_text("plant\n")
    (state / "..evil").write_text("x\n")
    md5 = lambda t: hashlib.md5(t.encode()).hexdigest()  # noqa: E731,S324
    listed = [str(prefix), str(brain), str(brain / "bin"), str(brain / "bin/teaport-brain"), str(brain / "bin/python"),
              str(brain / "lib"), str(brain / "lib/ok.py"), str(brain / "lib/changed.py"), str(brain / "lib2"),
              str(brain / "lib/pipe")]
    sums = {str(brain / "bin/teaport-brain"): md5("#!/bin/sh\n"), str(brain / "lib/ok.py"): md5("ok = 1\n"),
            str(brain / "lib/changed.py"): md5("as shipped\n")}
    layout = _layout()
    layout._dpkg_files = lambda package: (listed, sums)
    assert not layout.done(str(prefix), str(state), owner)
    # Rebuilt, but the brain package's tree lacks what was left out: said, until an install
    # of the package marks it whole.
    assert layout.migrate(str(prefix), str(state), "teaport-brain", owner) == layout.INCOMPLETE
    assert layout.done(str(prefix), str(state), owner)
    names = sorted(os.listdir(tmp))
    assert names == ["opt", "state"], names                             # the old trees are gone
    # Engine: the regular files only, not what a link led to; no rollback copy, no marker.
    assert (prefix / "bin/voxtral").read_text() == "engine\n" and os.access(prefix / "bin/voxtral", os.X_OK)
    assert (prefix / "models/voxtral/consolidated.safetensors").read_text() == "model\n"
    assert not (prefix / "bin/g2p/kokoro_g2p.v2.dict").exists() and not os.path.islink(prefix / "bin/g2p")
    assert not os.path.lexists(prefix / "models/kokoro/Kokoro_espeak_F16.gguf")
    assert not os.path.lexists(prefix / "engine.rollback") and not os.path.lexists(prefix / "engine.swapping")
    assert json.loads((prefix / "manifest.json").read_text()) == {"engine": {"release": "1.6"}}
    # The run user's own tree moves over as it is.
    assert (prefix / "bridge/index.js").read_text() == "bridge\n"
    # Brain: what dpkg lists and vouches for, with its times; nothing else.
    assert (brain / "lib/ok.py").read_text() == "ok = 1\n" and os.stat(brain / "lib/ok.py").st_mtime_ns == 1_000_000_000
    assert os.readlink(brain / "bin/python") == "/usr/bin/python3" and (brain / "bin/teaport-brain").exists()
    for gone in ("lib/changed.py", "lib/zz.pth", "lib2", "lib/pipe"):
        assert not os.path.lexists(brain / gone), gone
    # $STATE: the license records and the notes; no download, no stray name.
    assert sorted(os.listdir(state)) == [".root-layout", "EULA-v1.0.txt", "eula-accepted.json", "journald.applied"], os.listdir(state)
    # No ACL anywhere in the new trees.
    if acl:
        out = subprocess.run(["getfacl", "-Rsp", str(prefix), str(state)], capture_output=True, text=True).stdout
        assert "default:" not in out and f"user:{os.getuid()}:" not in out, out
    for top, dirs, files in os.walk(prefix):
        for n in dirs + files:
            st = os.lstat(os.path.join(top, n))
            if not os.path.islink(os.path.join(top, n)) and "bridge" not in top and n != "bridge":
                assert not st.st_mode & 0o022, os.path.join(top, n)
    # Done once: a second run changes nothing, and still says the brain is incomplete until
    # install.sh writes the marker whole (brain_tree_complete).
    before = sorted(os.listdir(prefix))
    assert layout.migrate(str(prefix), str(state), "teaport-brain", owner) == layout.INCOMPLETE
    assert sorted(os.listdir(prefix)) == before
    (prefix / ".root-layout").write_text("rebuilt as root's\n")
    assert layout.migrate(str(prefix), str(state), "teaport-brain", owner) == 0


def test_every_file_in_the_engines_data_directories_is_carried_over():
    """An engine reads more of its own than an older installer named (a dictionary per
    language next to the en-us one): every regular file in bin/g2p and the model
    directories comes along, and nothing that is not a plain file, nor a rollback copy."""
    tmp = tempdir("teaport-layout-extra-")
    prefix, state = pathlib.Path(tmp, "opt"), pathlib.Path(tmp, "state")
    owner = (os.getuid(), os.getgid())
    (prefix / "bin/g2p").mkdir(parents=True)
    (prefix / "bin/voxtral").write_text("engine\n")
    (prefix / "bin/voxtral.rollback-377508d").write_text("old engine\n")   # a hand-kept copy
    (prefix / "bin/g2p/kokoro_g2p.v2.dict").write_text("en\n")
    (prefix / "bin/g2p/kokoro_zh.v1.dict").write_text("zh\n")               # the Mandarin dict
    (prefix / "bin/g2p/kokoro_zh.v2.dict").write_text("zh2\n")              # not named in ENGINE
    (prefix / "bin/g2p/kokoro_zh.v2.dict").chmod(0o666)
    os.symlink("/etc/passwd", prefix / "bin/g2p/kokoro_ja.v1.dict")         # a link out
    os.mkfifo(prefix / "bin/g2p/pipe")
    (prefix / "bin/g2p/sub").mkdir()
    (prefix / "bin/g2p/sub/x.dict").write_text("x\n")
    (prefix / "bin/g2p/.hidden").write_text("h\n")
    (prefix / "bin/g2p/kokoro_zh.v2.dict.part").write_text("half\n")      # a download in progress
    (prefix / "bin/watermark").mkdir()
    (prefix / "bin/watermark/perth_implicit.v2.bin").write_text("wm\n")    # the AI watermark's weights
    (prefix / "bin/watermark/perth_implicit.v3.bin").write_text("wm3\n")   # not named in ENGINE
    (prefix / "models/voxtral").mkdir(parents=True)
    (prefix / "models/voxtral/consolidated.safetensors").write_text("model\n")
    (prefix / "models/voxtral/extra.bin").write_text("extra\n")
    state.mkdir()
    layout = _layout()
    layout._dpkg_files = lambda package: ([], {})
    layout.migrate(str(prefix), str(state), "teaport-brain", owner)
    assert (prefix / "bin/g2p/kokoro_zh.v1.dict").read_text() == "zh\n"
    assert (prefix / "bin/g2p/kokoro_zh.v2.dict").read_text() == "zh2\n"
    assert not os.stat(prefix / "bin/g2p/kokoro_zh.v2.dict").st_mode & 0o022   # made root's mode
    assert (prefix / "models/voxtral/extra.bin").read_text() == "extra\n"
    assert (prefix / "bin/g2p/kokoro_g2p.v2.dict").read_text() == "en\n"
    assert (prefix / "bin/watermark/perth_implicit.v2.bin").read_text() == "wm\n"
    assert (prefix / "bin/watermark/perth_implicit.v3.bin").read_text() == "wm3\n"
    for gone in ("bin/g2p/kokoro_ja.v1.dict", "bin/g2p/pipe", "bin/g2p/sub", "bin/g2p/.hidden",
                 "bin/g2p/kokoro_zh.v2.dict.part", "bin/voxtral.rollback-377508d"):
        assert not os.path.lexists(prefix / gone), gone
    # A data directory reached through a link carries nothing, even one the run user owns
    # with plain files in it (they would link fine: only the walk refuses them).
    tmp2 = tempdir("teaport-layout-extra2-")
    p2, s2, elsewhere = pathlib.Path(tmp2, "opt"), pathlib.Path(tmp2, "state"), pathlib.Path(tmp2, "elsewhere")
    (p2 / "bin").mkdir(parents=True)
    elsewhere.mkdir()
    (elsewhere / "kokoro_zh.v1.dict").write_text("planted\n")
    os.symlink(elsewhere, p2 / "bin/g2p")
    s2.mkdir()
    layout.migrate(str(p2), str(s2), "teaport-brain", owner)
    assert os.listdir(p2 / "bin/g2p") == []


def test_a_link_to_someone_elses_file_is_not_carried():
    """Carrying makes the inode root's with a fixed mode under every name it has: a link the
    run user made to another user's file is left behind, and root's own only where the
    kernel lets nobody link what they could not write."""
    layout = _layout()
    me = os.getuid()
    import stat
    ok = layout._carry_owner_ok
    reg = stat.S_IFREG
    assert ok(me, me, reg | 0o600, [], me)                     # the run user's own: made root's
    assert ok(me, me, reg | 0o600, ["user.x"], me)
    assert not ok(me + 1, me + 1, reg | 0o644, [], me)        # another user's
    # Root's, carried untouched only when it exposes and changes nothing: the engine binary
    # and its dictionaries as root installed them.
    assert ok(0, 0, reg | 0o755, [], me) and ok(0, 0, reg | 0o644, [], me)
    for gid, mode, xattrs in ((0, 0o600, []),                 # not everyone could read it
                              (0, 0o640, []),
                              (0, 0o664, []),                 # others may write it
                              (0, 0o646, []),
                              (0, 0o4755, []), (0, 0o2755, []), (0, 0o1644, []),   # set-id, sticky
                              (5, 0o644, []),                 # a group's
                              (0, 0o644, ["system.posix_acl_access"]),   # an ACL (maybe a deny)
                              (0, 0o755, ["security.capability"])):      # a file capability
        assert not ok(0, gid, reg | mode, xattrs, me), (gid, oct(mode), xattrs)
    # An old tree root owns: its root files go by the same rule, not as the owner's.
    assert not ok(0, 0, reg | 0o600, [], 0)
    # link_into checks it on the new name, and leaves nothing behind when it refuses.
    tmp = tempdir("teaport-layout-owner-")
    old = pathlib.Path(tmp, "old")
    old.mkdir()
    (old / "f").write_text("x\n")
    new = pathlib.Path(tmp, "new")
    new.mkdir()
    tree = layout.Tree(str(old))
    try:
        tree.uid = me + 1                                    # as if the tree were someone else's
        assert not tree.link_into("f", str(new / "f")) and not os.path.lexists(new / "f")
        tree.uid = me
        assert tree.link_into("f", str(new / "f"))
    finally:
        os.close(tree.fd)


def _brain_box(tmp):
    prefix, state = pathlib.Path(tmp, "opt"), pathlib.Path(tmp, "state")
    brain = prefix / "brain"
    (brain / "bin").mkdir(parents=True)
    (brain / "lib").mkdir()
    (brain / "lib/a.py").write_text("a = 1\n")
    state.mkdir()
    return prefix, state, brain


def test_an_older_release_is_refused_before_anything_of_it_is_fetched_or_run():
    # The run user picks the channel; the other may name an older release, whose
    # install.sh this would run as root, and whose engine goes in before the brain step.
    tmp = tempdir("teaport-apply-older-")
    mod = _helper(_state(tmp))
    mod.read_plan = lambda *a: _plan(["engine", "brain"])
    mod.clone = lambda *a: (_ for _ in ()).throw(AssertionError("an older release was cloned"))
    mod.box_brain = lambda: "0.1.2"   # newer than the channel's 0.1.1 (half-configured counts)
    assert mod.main() == 1
    st = _status(mod)
    assert st["state"] == "failed" and "older release (brain 0.1.1) than this box has (0.1.2)" in st["message"], st
    # A manifest naming no brain package cannot be told from an older one.
    mod.read_manifest = lambda channel, dest: {"brain": {"repo": "Teaspoon-AI/teaport", "tag": "brain-v0.1.1"}}
    mod.box_brain = lambda: "0.1.0"
    assert mod.main() == 1 and "names no brain package" in _status(mod)["message"]


def test_the_floor_takes_no_engine_back_and_lets_nothing_unreadable_past():
    real = _real_module()

    def m(brain="0.1.1", engine="1.6"):
        return {"channel": "stable", "engine": {"release": engine},
                "brain": {"deb": {"url": f"https://x/teaport-brain_{brain}_arm64.deb"}}}
    real.not_older(m(), "0.1.1", "1.6")                 # the same release
    real.not_older(m(engine="1.7"), "0.1.1", "1.6")     # a newer engine
    real.not_older(m(engine="TODO"), "0.1.1", "1.6")    # an engine the channel has not released
    real.not_older(m(), "0.1.1", None)                  # no root-owned record of the engine
    for args, why in (((m(), "0.1.1", "1.7"), "older engine (1.6)"),          # beta -> stable
                      ((m(engine="next"), "0.1.1", "1.6"), "older engine"),   # cannot be ordered
                      ((m(), None, "1.6"), "cannot read the brain package's version"),
                      ((m(brain="0.1:"), "0.1.1", "1.6"), "cannot compare")):  # dpkg rc 2
        try:
            real.not_older(*args)
        except RuntimeError as e:
            assert why in str(e), (args, e)
        else:
            raise AssertionError(args)


def test_the_engine_record_counts_only_when_root_alone_can_write_it():
    real = _real_module()
    assert real.ENGINE_RECORD == "/var/lib/teaport/engine.release"   # install.sh's ENGINE_RECORD
    assert 'ENGINE_RECORD="$STATE/engine.release"' in INSTALL.read_text()
    # The rebuild never carries one over from the run user's old tree.
    layout = _layout()
    assert "engine.release" not in layout.STATE_KEPT_TEXT + layout.STATE_KEPT_JSON
    real.ROOT_UID = os.getuid()   # "root" is whoever runs the test
    d = pathlib.Path(tempdir("teaport-apply-record-"))
    os.chmod(d, 0o755)
    f = d / "engine.release"
    f.write_text("1.7\n")
    os.chmod(f, 0o644)
    assert real.box_engine(str(f)) == "1.7"
    for mode, target in ((0o664, f), (0o775, d)):   # writable by others: says nothing
        os.chmod(target, mode)
        assert real.box_engine(str(f)) is None, (oct(mode), target)
        os.chmod(target, 0o644 if target == f else 0o755)
    link = d / "link"
    link.symlink_to(f)
    assert real.box_engine(str(link)) is None        # not through a link
    f.write_text("1.7; rm\n")
    assert real.box_engine(str(f)) is None           # not a version
    assert real.box_engine(str(d / "absent")) is None
    real.ROOT_UID = 0
    f.write_text("1.7\n")
    assert real.box_engine(str(f)) is None           # not root's (the test is not root)


def _real_module():
    spec = importlib.util.spec_from_file_location(f"update_apply_real_{time.monotonic_ns()}", HELPER)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    return real


def test_root_reads_the_brain_on_the_box_in_any_state_and_the_name_as_the_brain_does():
    from teaport_brain import updates
    mod = _helper(_state(tempdir("teaport-apply-floor-")))
    for out, want in (("installed 0.1.1", "0.1.1"), ("half-configured 0.1.1", "0.1.1"),
                      ("triggers-awaiting 0.1.1", "0.1.1"), ("config-files 0.1.1", None), ("", None)):
        runner = lambda *a, **k: subprocess.CompletedProcess(a, 0 if out else 1, out, "")  # noqa: E731
        assert _real_box_brain(mod)(runner) == want, out
    for url in ("https://x/dl/brain/teaport-brain_0.1.1_arm64.deb", "https://x/teaport-brain_1.0~rc1_amd64.deb?x=1",
                "https://x/brain.deb", "TODO", None, "https://x/teaport-brain__arm64.deb"):
        m = {"brain": {"deb": {"url": url}}}
        assert mod.channel_brain(m) == updates.deb_version(m), url


def _real_box_brain(mod):
    spec = importlib.util.spec_from_file_location(f"update_apply_real_{time.monotonic_ns()}", HELPER)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    return real.box_brain


def test_the_cli_runs_every_python_isolated():
    # `sudo teaport ...` started in a directory the run user owns: a python3 without -I
    # puts that directory first on sys.path, and a json.py planted there runs as root.
    lines = [ln for ln in (ROOT / "cli/teaport").read_text().splitlines() if not ln.lstrip().startswith("#")]
    runs = [ln for ln in lines if re.search(r"(^|[\s|;(&])python3(\s|$)", ln)]
    assert runs, "the CLI runs no python3?"
    bare = [ln.strip() for ln in runs if not re.search(r"python3 -I\b", ln)]
    assert not bare, bare


def test_only_the_venvs_own_interpreter_links_come_over():
    """A link the package lists, re-pointed while the tree was the run user's, must not come
    over: the next install of the package would have dpkg write through it as root."""
    import hashlib
    tmp = tempdir("teaport-layout-links-")
    prefix, state, brain = _brain_box(tmp)
    os.symlink("/usr/bin/python3.12", brain / "bin/python")          # the venv's: kept
    os.symlink("python", brain / "bin/python3")                      # relative, the venv's own: kept
    os.symlink("/usr/bin/X11", brain / "bin/python3.12")             # /usr/bin/X11 -> . (Ubuntu): refused
    os.symlink("/opt/teaport/brain/bin/python3/../../home/u", brain / "lib2")   # listed as a directory
    listed = [str(brain), str(brain / "bin"), str(brain / "bin/python"), str(brain / "bin/python3"),
              str(brain / "bin/python3.12"), str(brain / "lib"), str(brain / "lib/a.py"), str(brain / "lib2"),
              str(brain / "lib2/x.py")]
    layout = _layout()
    layout._dpkg_files = lambda package: (listed, {str(brain / "lib/a.py"): hashlib.md5(b"a = 1\n").hexdigest()})  # noqa: S324
    owner = (os.getuid(), os.getgid())
    assert layout.migrate(str(prefix), str(state), "teaport-brain", owner) == layout.INCOMPLETE
    assert os.readlink(brain / "bin/python") == "/usr/bin/python3.12"
    assert os.readlink(brain / "bin/python3") == "python"
    for gone in ("bin/python3.12", "lib2"):
        assert not os.path.lexists(brain / gone), gone
    links = [os.path.join(t, n) for t, ds, fs in os.walk(prefix) for n in ds + fs if os.path.islink(os.path.join(t, n))]
    assert sorted(links) == [str(brain / "bin/python"), str(brain / "bin/python3")], links


def test_an_unusual_tree_is_refused_and_a_stopped_run_is_finished_or_cleared():
    layout = _layout()
    layout._dpkg_files = lambda package: ([], {})
    owner = (os.getuid(), os.getgid())
    # A symlinked tree: not rebuilt (what is behind it would be orphaned), and said.
    tmp = tempdir("teaport-layout-odd-")
    real = pathlib.Path(tmp, "real")
    real.mkdir()
    (real / "keep").write_text("x\n")
    os.symlink(real, pathlib.Path(tmp, "opt"))
    try:
        layout.migrate(os.path.join(tmp, "opt"), os.path.join(tmp, "state"), "teaport-brain", owner)
    except SystemExit as e:
        assert "symlink or a mount point" in str(e), e
    else:
        raise AssertionError("a symlinked tree was rebuilt")
    assert (real / "keep").exists()
    # A run stopped between its two renames: the finished new tree is put in place; other
    # leftovers (an unfinished new tree, an old tree set aside) are removed.
    tmp = tempdir("teaport-layout-stopped-")
    done_new = pathlib.Path(tmp, "opt.new-20261117000000")
    done_new.mkdir()
    (done_new / ".root-layout").write_text("rebuilt as root's\n")
    (done_new / "bin").mkdir()
    pathlib.Path(tmp, "opt.new-20261116000000").mkdir()
    pathlib.Path(tmp, "opt.pre-root-20261117000000").mkdir()
    pathlib.Path(tmp, "state").mkdir()
    layout.migrate(os.path.join(tmp, "opt"), os.path.join(tmp, "state"), "teaport-brain", owner)
    assert sorted(os.listdir(tmp)) == ["opt", "state"], os.listdir(tmp)
    assert pathlib.Path(tmp, "opt/bin").is_dir()
    # A run stopped after the run user's own entries moved into the new tree, before the
    # swap: they go back to the tree they came from, not out with the unfinished one.
    tmp = tempdir("teaport-layout-stopped-mid-")
    prefix, state, brain = _brain_box(tmp)
    unfinished = pathlib.Path(f"{prefix}.new-20261117000000")
    (unfinished / "bridge").mkdir(parents=True)
    (unfinished / "bridge/settings.json").write_text("{}\n")
    (unfinished / "bin").mkdir()   # ours: goes with the unfinished tree
    layout.migrate(str(prefix), str(state), "teaport-brain", owner)
    assert not unfinished.exists() and sorted(os.listdir(tmp)) == ["opt", "state"], os.listdir(tmp)
    assert (prefix / "bridge/settings.json").read_text() == "{}\n"


def test_a_tree_that_cannot_be_written_is_not_swapped_in():
    import errno
    import hashlib
    tmp = tempdir("teaport-layout-enospc-")
    prefix, state, brain = _brain_box(tmp)
    layout = _layout()
    layout._dpkg_files = lambda package: ([str(brain), str(brain / "lib"), str(brain / "lib/a.py")],
                                          {str(brain / "lib/a.py"): hashlib.md5(b"a = 1\n").hexdigest()})  # noqa: S324
    real_write = layout.os.write

    class FullOs:
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def write(fd, data):
            raise OSError(errno.ENOSPC, "No space left on device")
    layout.os = FullOs()
    try:
        layout.migrate(str(prefix), str(state), "teaport-brain", (os.getuid(), os.getgid()))
    except OSError as e:
        assert e.errno == errno.ENOSPC
    else:
        raise AssertionError("a full disk went unnoticed")
    finally:
        layout.os = os
        assert real_write is os.write
    # The old tree is where it was, untouched; no half-built tree is left.
    assert (brain / "lib/a.py").read_text() == "a = 1\n" and sorted(os.listdir(tmp)) == ["opt", "state"]


def test_a_fresh_box_gets_root_trees_and_the_marker():
    tmp = tempdir("teaport-layout-")
    layout = _layout()
    layout._dpkg_files = lambda package: ([], {})
    owner = (os.getuid(), os.getgid())
    prefix, state = os.path.join(tmp, "opt"), os.path.join(tmp, "state")
    assert layout.migrate(prefix, state, "teaport-brain", owner) == 0
    assert layout.done(prefix, state, owner) and os.path.isdir(os.path.join(prefix, "bin/g2p"))
    assert os.path.isdir(os.path.join(prefix, "bin/watermark"))
    assert layout.main(["migrate", prefix, state]) == 2


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
    # the three helpers' lines (the config page's, the Wi-Fi's, the owner's login's, #421).
    tmp = tempdir("teaport-sudoers-")
    text = (ROOT / "packaging/deb/sudoers-sync").read_text()
    text = text.replace("f=/etc/sudoers.d/teaport-config", f"f={tmp}/out")
    text = text.replace("helper=/usr/lib/teaport/config_apply.py", f"helper={tmp}/helper")
    text = text.replace("wifi=/usr/lib/teaport/wifi_apply.py", f"wifi={tmp}/wifi")
    text = text.replace("login=/usr/lib/teaport/login_apply.py", f"login={tmp}/login")
    text = text.replace("update_rules=/etc/polkit-1/rules.d/50-teaport-update.rules", f"update_rules={tmp}/rules.d/50-teaport-update.rules")
    text = text.replace("wifi_rules=/etc/polkit-1/rules.d/50-teaport-wifi-setup.rules", f"wifi_rules={tmp}/no-wifi-setup.rules")
    assert "/etc/polkit-1" not in text.split("set -eu", 1)[1], "a path left pointing at the real box"
    text = text.replace("install -m 0440 -o root -g root", "install -m 0440").replace("install -m 0644 -o root -g root", "install -m 0644")
    pathlib.Path(tmp, "helper").write_text("")
    pathlib.Path(tmp, "wifi").write_text("")
    pathlib.Path(tmp, "login").write_text("")
    pathlib.Path(tmp, "rules.d").mkdir()
    pathlib.Path(tmp, "s").write_text(text)
    p = subprocess.run(["sh", f"{tmp}/s", "bob"], capture_output=True, text=True)
    if p.returncode != 0 and "visudo" in p.stderr + p.stdout:
        appliance.skip("no visudo here")
    assert p.returncode == 0, p.stderr
    lines = [ln for ln in pathlib.Path(tmp, "out").read_text().splitlines() if not ln.startswith("#")]
    assert lines == ["bob ALL=(root) NOPASSWD: /usr/bin/python3 " + f"{tmp}/helper *",
                     "bob ALL=(root) NOPASSWD: /usr/bin/python3 " + f"{tmp}/wifi *",
                     "bob ALL=(root) NOPASSWD:" + ("" if _sudo_is_rs() else "NOLOG_INPUT:")
                     + " /usr/bin/python3 " + f"{tmp}/login *"], lines   # the tag: classic sudo only
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
    # Run as root, the CLI never runs the brain's venv as root.
    assert '"$venv/bin/python"' not in re.sub(r"brain_python\(\) \{.*?\n\}", "", cli, flags=re.S).replace(
        '[ -x "$venv/bin/python" ]', ""), "a venv call outside brain_python"


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
