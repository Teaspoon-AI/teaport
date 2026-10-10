#
# install.sh's --only brain follow-ups (issue #59): it refuses a box with no brain venv
# (main_brain_only), its freshness check looks only at what it installs
# (check_brain_source), and the three privileged reads of an env file share one helper
# (read_env_file: resolve_gateway_token, write_env, brain_verify).
#
# The functions are taken out of install.sh as they are and run by bash with the
# commands around them stubbed (sudo, systemctl, curl, the phases), against a temp dir.
# The freshness tests build a real git checkout with an upstream to be behind.
# Nothing outside a temp dir is touched.
#
# Run: python test_install_brain_only.py   (or via pytest test_suite.py)
#
import os
import pathlib
import subprocess
import tempfile

INSTALL = pathlib.Path(__file__).resolve().parents[2] / "install.sh"
AS_ROOT = os.geteuid() == 0   # root reads anything: the unreadable-file cases need a user

STUBS = r'''
set -euo pipefail
DRY_RUN=0 ONLY="" BRAIN_SRC="" ALLOW_STALE_SOURCE=0 BRAIN_PORT=7861 GATEWAY_TOKEN="" RUN_USER=nobody
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
die()  { printf 'DIE %s\n' "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
contains() { case "$2" in *"$1"*) return 0 ;; *) return 1 ;; esac; }
# sudo can read the unreadable (as root would), or is refused (SUDO_REFUSED=1). Every
# call is recorded in $T/sudo.log, so a test can tell when it was not asked at all.
sudo() {
  printf '%s\n' "$*" >> "$T/sudo.log"
  [ "${SUDO_REFUSED:-0}" = 0 ] || { echo "sudo: a password is required" >&2; return 1; }
  [ "$1" = cat ] || return 1
  [ -e "$2" ] || { echo "cat: $2: No such file or directory" >&2; return 1; }
  chmod u+r "$2"; command cat "$2"; chmod 000 "$2"
}
# write_env's privileged writes, into a side file the test reads back.
SUDO() { case "$1" in tee) command tee "$2.written" ;; *) : ;; esac; }
'''


def _fn(name: str) -> str:
    """One function's text out of install.sh, from `name() {` to its closing brace."""
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _line(prefix: str) -> str:
    """One top-level assignment out of install.sh (e.g. BRAIN_ONLY_PATHS=...)."""
    return next(ln for ln in INSTALL.read_text().splitlines() if ln.startswith(prefix)) + "\n"


def _run(tmp, script, fns=(), env=None):
    body = STUBS + "".join(_fn(f) for f in fns) + script
    e = {**os.environ, "T": str(tmp), **(env or {})}
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=e)
    return p.returncode, p.stdout + p.stderr


def _sudo_calls(tmp):
    log = pathlib.Path(tmp, "sudo.log")
    return log.read_text().splitlines() if log.exists() else []


def _env_file(tmp, text, readable=True):
    path = pathlib.Path(tmp, "brain.env")
    path.write_text(text)
    path.chmod(0o640 if readable else 0o000)
    return path


# --- read_env_file and its three callers ---------------------------------------------

def test_a_readable_env_file_is_read_without_sudo():
    tmp = tempfile.mkdtemp(prefix="teaport-env-")
    path = _env_file(tmp, "GATEWAY_TOKEN=tok\nBRAIN_PORT=9001\n")
    rc, out = _run(tmp, f'read_env_file "{path}"; read_env_file "{path}" --lenient', ["read_env_file"])
    assert rc == 0 and out.count("GATEWAY_TOKEN=tok") == 2, out
    assert _sudo_calls(tmp) == []


def test_an_unreadable_env_file_is_read_with_sudo_and_a_refusal_is_the_callers_call():
    if AS_ROOT:
        return
    tmp = tempfile.mkdtemp(prefix="teaport-env-")
    path = _env_file(tmp, "GATEWAY_TOKEN=tok\n", readable=False)
    rc, out = _run(tmp, f'read_env_file "{path}"', ["read_env_file"])
    assert rc == 0 and "GATEWAY_TOKEN=tok" in out and _sudo_calls(tmp) == [f"cat {path}"], out
    # Refused: strict fails; --lenient prints nothing, says nothing, and succeeds.
    rc, out = _run(tmp, f'read_env_file "{path}"', ["read_env_file"], {"SUDO_REFUSED": "1"})
    assert rc != 0, out
    rc, out = _run(tmp, f'out="$(read_env_file "{path}" --lenient)"; echo "[$out]"',
                   ["read_env_file"], {"SUDO_REFUSED": "1"})
    assert rc == 0 and out.strip() == "[]", out


def test_the_gateway_token_is_reused_from_a_root_only_brain_env():
    if AS_ROOT:
        return
    tmp = tempfile.mkdtemp(prefix="teaport-env-")
    _env_file(tmp, "BRAIN_PORT=1\nGATEWAY_TOKEN=keepme\n", readable=False)
    script = f'ETC="{tmp}"; resolve_gateway_token; echo "TOKEN $GATEWAY_TOKEN"'
    rc, out = _run(tmp, script, ["read_env_file", "resolve_gateway_token"])
    assert rc == 0 and "TOKEN keepme" in out, out
    # sudo refused: a fresh token, not an installer that dies with no message.
    rc, out = _run(tmp, script, ["read_env_file", "resolve_gateway_token"], {"SUDO_REFUSED": "1"})
    assert rc == 0 and "TOKEN " in out and "TOKEN keepme" not in out, out


def test_no_brain_env_mints_a_token_without_asking_sudo():
    tmp = tempfile.mkdtemp(prefix="teaport-env-")
    rc, out = _run(tmp, f'ETC="{tmp}"; resolve_gateway_token; echo "TOKEN $GATEWAY_TOKEN"',
                   ["read_env_file", "resolve_gateway_token"])
    tok = out.split("TOKEN ", 1)[1].strip()
    assert rc == 0 and len(tok) == 36, out
    assert _sudo_calls(tmp) == []


def test_write_env_keeps_operator_settings_from_a_root_only_file():
    if AS_ROOT:
        return
    tmp = tempfile.mkdtemp(prefix="teaport-env-")
    path = _env_file(tmp, "GATEWAY_TOKEN=old\nOPERATOR_KNOB=7\n", readable=False)
    rc, out = _run(tmp, f'write_env "{path}" GATEWAY_TOKEN=new', ["read_env_file", "write_env"])
    assert rc == 0, out
    written = pathlib.Path(f"{path}.written").read_text()
    assert written == "GATEWAY_TOKEN=new\nOPERATOR_KNOB=7\n", written


def test_write_env_refuses_to_overwrite_a_file_it_cannot_read():
    if AS_ROOT:
        return
    tmp = tempfile.mkdtemp(prefix="teaport-env-")
    path = _env_file(tmp, "OPERATOR_KNOB=7\n", readable=False)
    rc, out = _run(tmp, f'write_env "{path}" GATEWAY_TOKEN=new', ["read_env_file", "write_env"],
                   {"SUDO_REFUSED": "1"})
    assert rc != 0 and "DIE cannot read" in out, out
    assert not pathlib.Path(f"{path}.written").exists()


def test_write_env_dry_run_does_not_sudo_and_says_what_a_real_run_reads():
    if AS_ROOT:
        return
    tmp = tempfile.mkdtemp(prefix="teaport-env-")
    path = _env_file(tmp, "OPERATOR_KNOB=7\n", readable=False)
    rc, out = _run(tmp, f'DRY_RUN=1; write_env "{path}" GATEWAY_TOKEN=new', ["read_env_file", "write_env"])
    assert rc == 0 and "a real run reads it with sudo" in out, out
    assert _sudo_calls(tmp) == []


def test_write_env_on_a_first_install_reads_nothing():
    tmp = tempfile.mkdtemp(prefix="teaport-env-")
    path = pathlib.Path(tmp, "engine.env")
    rc, out = _run(tmp, f'write_env "{path}" A=1 "?B=2"', ["read_env_file", "write_env"])
    assert rc == 0 and pathlib.Path(f"{path}.written").read_text() == "A=1\nB=2\n", out
    assert _sudo_calls(tmp) == []


BRAIN_VERIFY_STUBS = r'''
curl() { printf 'CURL %s\n' "${@: -1}"; }
sleep() { :; }
systemctl() { case "$1" in is-active) return 0 ;; show) echo inv-1 ;; esac; }
'''


def test_brain_verify_polls_the_port_from_a_root_only_brain_env():
    if AS_ROOT:
        return
    tmp = tempfile.mkdtemp(prefix="teaport-env-")
    _env_file(tmp, 'BRAIN_PORT="9123"\n', readable=False)
    script = f'{BRAIN_VERIFY_STUBS}ETC="{tmp}"; brain_verify teaport-brain.service; echo VERIFIED'
    rc, out = _run(tmp, script, ["read_env_file", "brain_verify"])
    assert rc == 0 and "CURL http://127.0.0.1:9123/health" in out and "VERIFIED" in out, out
    # sudo refused: the default port, and still no failure from the read itself.
    rc, out = _run(tmp, script, ["read_env_file", "brain_verify"], {"SUDO_REFUSED": "1"})
    assert rc == 0 and "CURL http://127.0.0.1:7861/health" in out, out


# --- --only brain refuses a box with no brain venv -----------------------------------

ONLY_BRAIN_STUBS = r'''
STAGED=/opt/teaport/venvs/new
systemctl() { [ "$1" = cat ]; }   # the brain unit is installed
for f in check_brain_source phase_brain sip_brain_detach brain_swap sip_brain_retire \
         brain_install_tools brain_prune; do eval "$f() { echo STEP $f; }"; done
'''


def _brain_only(tmp, venv=None, dry_run=False):
    prefix = pathlib.Path(tmp, "opt")
    prefix.mkdir()
    if venv == "dir":
        (prefix / "venv").mkdir()
    elif venv == "link":
        (prefix / "venvs/r1").mkdir(parents=True)
        (prefix / "venv").symlink_to(prefix / "venvs/r1")
    elif venv == "dangling":
        (prefix / "venv").symlink_to(prefix / "venvs/gone")
    script = (f'{ONLY_BRAIN_STUBS}PREFIX="{prefix}"; BRAIN_LINK="$PREFIX/venv"; '
              f'DRY_RUN={int(dry_run)}; main_brain_only')
    return _run(tmp, script, ["main_brain_only"])


def test_only_brain_refuses_a_box_with_no_brain_venv():
    rc, out = _brain_only(tempfile.mkdtemp(prefix="teaport-only-"))
    assert rc != 0 and "DIE no brain venv at" in out and "without --only" in out, out
    assert "STEP" not in out, out   # refused before anything was built or swapped


def test_only_brain_dry_run_warns_and_shows_the_plan():
    rc, out = _brain_only(tempfile.mkdtemp(prefix="teaport-only-"), dry_run=True)
    assert rc == 0 and "WARN no brain venv at" in out and "STEP brain_swap" in out, out


def test_only_brain_updates_a_live_venv_a_pre_uv_one_and_a_dangling_link():
    for venv in ("link", "dir", "dangling"):
        rc, out = _brain_only(tempfile.mkdtemp(prefix="teaport-only-"), venv=venv)
        assert rc == 0 and "no brain venv" not in out and "STEP brain_swap" in out, (venv, out)


# --- the freshness check looks at what is installed ----------------------------------

def _git(cwd, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, env=env,
                   check=True, capture_output=True)


def _behind(touching):
    """A checkout one upstream commit behind, that commit touching <touching>."""
    tmp = tempfile.mkdtemp(prefix="teaport-fresh-")
    up, here, other = (os.path.join(tmp, d) for d in ("up.git", "here", "other"))
    _git(tmp, "init", "--bare", up)
    _git(tmp, "clone", up, other)
    for f in ("brain/uv.lock", "cli/teaport", "packaging/xvf3800/60-teaport-xvf3800.rules",
              "packaging/wifi-setup/x", "docs/x.md", "plugin/x.ts", "install.sh"):
        p = pathlib.Path(other, f)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("v1\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "v1")
    _git(other, "push", "origin", "HEAD:main")
    _git(tmp, "clone", up, here)
    pathlib.Path(other, touching).write_text("v2\n")
    _git(other, "commit", "-am", "v2")
    _git(other, "push", "origin", "HEAD:main")
    # Fetched here too, so the result does not hang on the fetch (skipped under root).
    _git(here, "fetch", "--quiet")
    return tmp, here


def _check(tmp, here, only):
    env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    script = f'{_line("BRAIN_ONLY_PATHS=")}HERE="{here}"; ONLY="{only}"; check_brain_source; echo CHECKED'
    return _run(tmp, script, ["brain_src_fetch", "check_brain_source"], env)


def test_only_brain_is_not_refused_for_upstream_commits_it_does_not_install():
    for touching in ("docs/x.md", "plugin/x.ts", "packaging/wifi-setup/x", "install.sh"):
        tmp, here = _behind(touching)
        rc, out = _check(tmp, here, "brain")
        assert rc == 0 and "CHECKED" in out and "behind" not in out, (touching, out)
        # The full install consumes all of it: still refused.
        rc, out = _check(tmp, here, "")
        assert rc != 0 and "1 commit(s) behind origin/main —" in out, (touching, out)


def test_only_brain_is_refused_for_upstream_commits_to_what_it_installs():
    for touching in ("brain/uv.lock", "cli/teaport", "packaging/xvf3800/60-teaport-xvf3800.rules"):
        tmp, here = _behind(touching)
        rc, out = _check(tmp, here, "brain")
        assert rc != 0, (touching, out)
        assert ("1 commit(s) behind origin/main in what --only brain installs "
                "(brain cli packaging/xvf3800)") in out, (touching, out)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
