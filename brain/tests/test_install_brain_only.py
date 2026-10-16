#
# install.sh's --only brain follow-ups (issue #59): it refuses a box with no brain venv
# (main_brain_only), its freshness check looks only at what it installs
# (check_brain_source), the three privileged reads of an env file share one helper
# (read_env_file: resolve_gateway_token, write_env, brain_verify), and a release's
# teaport CLI and config helper go live and come back with its venv (brain_stage_tools,
# tools_point_at, from brain_point_at).
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
import re
import subprocess

import appliance
from tempdirs import tempdir

INSTALL = pathlib.Path(__file__).resolve().parents[2] / "install.sh"

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
    if not readable:
        appliance.require_modes_bind()   # root reads it anyway: skipped, by name
    path = pathlib.Path(tmp, "brain.env")
    path.write_text(text)
    path.chmod(0o640 if readable else 0o000)
    return path


# --- read_env_file and its three callers ---------------------------------------------

def test_a_readable_env_file_is_read_without_sudo():
    tmp = tempdir("teaport-env-")
    path = _env_file(tmp, "GATEWAY_TOKEN=tok\nBRAIN_PORT=9001\n")
    rc, out = _run(tmp, f'read_env_file "{path}"; read_env_file "{path}" --lenient', ["read_env_file"])
    assert rc == 0 and out.count("GATEWAY_TOKEN=tok") == 2, out
    assert _sudo_calls(tmp) == []


def test_an_unreadable_env_file_is_read_with_sudo_and_a_refusal_is_the_callers_call():
    tmp = tempdir("teaport-env-")
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
    tmp = tempdir("teaport-env-")
    _env_file(tmp, "BRAIN_PORT=1\nGATEWAY_TOKEN=keepme\n", readable=False)
    script = f'ETC="{tmp}"; resolve_gateway_token; echo "TOKEN $GATEWAY_TOKEN"'
    rc, out = _run(tmp, script, ["read_env_file", "resolve_gateway_token"])
    assert rc == 0 and "TOKEN keepme" in out, out
    # sudo refused: a fresh token, not an installer that dies with no message.
    rc, out = _run(tmp, script, ["read_env_file", "resolve_gateway_token"], {"SUDO_REFUSED": "1"})
    assert rc == 0 and "TOKEN " in out and "TOKEN keepme" not in out, out


def test_no_brain_env_mints_a_token_without_asking_sudo():
    tmp = tempdir("teaport-env-")
    rc, out = _run(tmp, f'ETC="{tmp}"; resolve_gateway_token; echo "TOKEN $GATEWAY_TOKEN"',
                   ["read_env_file", "resolve_gateway_token"])
    tok = out.split("TOKEN ", 1)[1].strip()
    assert rc == 0 and len(tok) == 36, out
    assert _sudo_calls(tmp) == []


def test_write_env_keeps_operator_settings_from_a_root_only_file():
    tmp = tempdir("teaport-env-")
    path = _env_file(tmp, "GATEWAY_TOKEN=old\nOPERATOR_KNOB=7\n", readable=False)
    rc, out = _run(tmp, f'write_env "{path}" GATEWAY_TOKEN=new', ["read_env_file", "write_env"])
    assert rc == 0, out
    written = pathlib.Path(f"{path}.written").read_text()
    assert written == "GATEWAY_TOKEN=new\nOPERATOR_KNOB=7\n", written


def test_write_env_refuses_to_overwrite_a_file_it_cannot_read():
    tmp = tempdir("teaport-env-")
    path = _env_file(tmp, "OPERATOR_KNOB=7\n", readable=False)
    rc, out = _run(tmp, f'write_env "{path}" GATEWAY_TOKEN=new', ["read_env_file", "write_env"],
                   {"SUDO_REFUSED": "1"})
    assert rc != 0 and "DIE cannot read" in out, out
    assert not pathlib.Path(f"{path}.written").exists()


def test_write_env_dry_run_does_not_sudo_and_says_what_a_real_run_reads():
    tmp = tempdir("teaport-env-")
    path = _env_file(tmp, "OPERATOR_KNOB=7\n", readable=False)
    rc, out = _run(tmp, f'DRY_RUN=1; write_env "{path}" GATEWAY_TOKEN=new', ["read_env_file", "write_env"])
    assert rc == 0 and "a real run reads it with sudo" in out, out
    assert _sudo_calls(tmp) == []


def test_write_env_on_a_first_install_reads_nothing():
    tmp = tempdir("teaport-env-")
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
    tmp = tempdir("teaport-env-")
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
         install_busy_lamp install_dns_resilience brain_prune; do eval "$f() { echo STEP $f; }"; done
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
    rc, out = _brain_only(tempdir("teaport-only-"))
    assert rc != 0 and "DIE no brain venv at" in out and "without --only" in out, out
    assert "STEP" not in out, out   # refused before anything was built or swapped


def test_only_brain_dry_run_warns_and_shows_the_plan():
    rc, out = _brain_only(tempdir("teaport-only-"), dry_run=True)
    assert rc == 0 and "WARN no brain venv at" in out and "STEP brain_swap" in out, out


def test_only_brain_updates_a_live_venv_a_pre_uv_one_and_a_dangling_link():
    for venv in ("link", "dir", "dangling"):
        rc, out = _brain_only(tempdir("teaport-only-"), venv=venv)
        assert rc == 0 and "no brain venv" not in out and "STEP brain_swap" in out, (venv, out)
        # The resolver's drop-in rides the brain update too, after the verified swap.
        assert out.index("STEP brain_swap") < out.index("STEP install_dns_resilience"), out


# --- the resolver's serve-stale drop-in ------------------------------------------------

def _dns(tmp, resolved_active=True, stub=True, systemd=255):
    """install_dns_resilience against a temp drop-in and resolv.conf; systemctl and sudo
    recorded."""
    src = pathlib.Path(tmp, "src/packaging/resolved/60-teaport-serve-stale.conf")
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text((INSTALL.parent / "packaging/resolved/60-teaport-serve-stale.conf").read_text())
    dropin = pathlib.Path(tmp, "etc/resolved.conf.d/60-teaport-serve-stale.conf")
    resolv = pathlib.Path(tmp, "resolv.conf")
    resolv.write_text("nameserver 127.0.0.53\noptions edns0 trust-ad\n" if stub
                      else "nameserver 192.168.1.254\n")
    active = 0 if resolved_active else 3
    script = (f'SRC_DIR="{tmp}/src/brain"; HERE="{tmp}/none"; RESOLVED_DROPIN="{dropin}"; '
              f'RESOLV_CONF="{resolv}"\n'
              f'systemctl() {{ printf "%s\\n" "$*" >> "$T/systemctl.log"; '
              f'[ "$1" = --version ] && {{ echo "systemd {systemd} ({systemd}.4-1ubuntu8)"; return 0; }}; '
              f'[ "$1" = is-active ] && return {active}; return 0; }}\n'
              'SUDO() { printf "%s\\n" "$*" >> "$T/sudo.log"; '
              '[ "$1" = install ] && { mkdir -p "$(dirname "${@: -1}")"; cp "${@: -2:1}" "${@: -1}"; }; '
              'return 0; }\n'
              'install_dns_resilience; echo DONE\n')
    return _run(tmp, script, ["packaging_src", "install_dns_resilience"]), dropin


def test_the_dns_drop_in_is_written_once_and_the_resolver_reloaded_only_then():
    tmp = tempdir("teaport-dns-")
    (rc, out), dropin = _dns(tmp)
    assert rc == 0 and "DONE" in out, out
    assert "StaleRetentionSec=1d" in dropin.read_text()
    log = pathlib.Path(tmp, "systemctl.log").read_text()
    assert "try-reload-or-restart systemd-resolved" in "".join(_sudo_calls(tmp)), _sudo_calls(tmp)
    # A second brain update with the same drop-in touches nothing: the cache stays.
    pathlib.Path(tmp, "sudo.log").unlink()
    (rc, out), _ = _dns(tmp)
    assert rc == 0 and _sudo_calls(tmp) == [], (_sudo_calls(tmp), out)
    assert "is-active" in log


def test_the_dns_drop_in_is_skipped_where_it_would_do_nothing():
    # Not running, not in the system's lookup path (resolv.conf naming the router
    # itself), or a systemd that predates the key: the drop-in would change nothing, and
    # the log must not claim otherwise.
    for case, kw, why in (("not running", dict(resolved_active=False), "not running"),
                          ("not the stub", dict(stub=False), "does not use systemd-resolved's stub"),
                          ("old systemd", dict(systemd=249), "systemd 249 predates serve-stale")):
        tmp = tempdir("teaport-dns-")
        (rc, out), dropin = _dns(tmp, **kw)
        assert rc == 0 and why in out and "skipped" in out, (case, out)
        assert not dropin.exists() and _sudo_calls(tmp) == [], (case, out)


# --- the freshness check looks at what is installed ----------------------------------

def _git(cwd, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, env=env,
                   check=True, capture_output=True)


def _behind(touching):
    """A checkout one upstream commit behind, that commit touching <touching>."""
    tmp = tempdir("teaport-fresh-")
    up, here, other = (os.path.join(tmp, d) for d in ("up.git", "here", "other"))
    _git(tmp, "init", "--bare", up)
    _git(tmp, "clone", up, other)
    for f in ("brain/uv.lock", "cli/teaport", "packaging/xvf3800/60-teaport-xvf3800.rules",
              "packaging/resolved/60-teaport-serve-stale.conf",
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
    for touching in ("docs/x.md", "plugin/x.ts", "packaging/wifi-setup/x"):
        tmp, here = _behind(touching)
        rc, out = _check(tmp, here, "brain")
        assert rc == 0 and "CHECKED" in out and "behind" not in out, (touching, out)
        # The full install consumes all of it: still refused.
        rc, out = _check(tmp, here, "")
        assert rc != 0 and "1 commit(s) behind origin/main —" in out, (touching, out)


def test_only_brain_is_refused_for_upstream_commits_to_what_it_installs():
    # install.sh too: how the brain is built, and some of what --only brain writes, is in it.
    for touching in ("brain/uv.lock", "cli/teaport", "packaging/xvf3800/60-teaport-xvf3800.rules",
                     "packaging/resolved/60-teaport-serve-stale.conf", "install.sh"):
        tmp, here = _behind(touching)
        rc, out = _check(tmp, here, "brain")
        assert rc != 0, (touching, out)
        assert ("1 commit(s) behind origin/main in what --only brain installs "
                "(brain cli packaging/xvf3800 packaging/resolved install.sh)") in out, (touching, out)


def test_uncommitted_changes_in_any_installed_path_are_named():
    tmp, here = _behind("docs/x.md")   # behind only in what --only brain does not install
    pathlib.Path(here, "cli/teaport").write_text("local\n")
    rc, out = _check(tmp, here, "brain")
    assert rc == 0 and f"uncommitted changes in {here} are used too: cli\n" in out, out
    pathlib.Path(here, "brain/uv.lock").write_text("local\n")
    rc, out = _check(tmp, here, "brain")
    assert rc == 0 and ": brain cli (the venv's revision is marked -dirty)" in out, out


# The checkout paths --only brain's functions read, as they spell them. SRC_DIR (and
# brain_stage's $src) is the checkout's brain/ (check_brain_source).
_READS = {
    r'\$\(dirname "\$SRC_DIR"\)/([\w./-]+)': "",
    r'packaging_src ([\w./-]+)': "packaging/",
    r'\$SRC_DIR/([\w./-]+)': "brain/",
    r'\$src/([\w./-]+)': "brain/",
    r'\$HERE/([\w./-]+)': "",
}


def test_every_checkout_path_only_brain_reads_is_in_its_freshness_check():
    paths = re.findall(r"\((.*)\)", _line("BRAIN_ONLY_PATHS="))[0].split()
    read = set()
    for fn in ("brain_stage", "brain_stage_tools", "install_busy_lamp", "install_dns_resilience"):
        body = _fn(fn)
        for pattern, base in _READS.items():
            read.update(base + m for m in re.findall(pattern, body))
    # Found at all: a respelled reference must not make this pass by matching nothing.
    assert {"cli/teaport", "brain/uv.lock", "packaging/xvf3800/60-teaport-xvf3800.rules",
            "packaging/resolved/60-teaport-serve-stale.conf"} <= read, read
    uncovered = sorted(r for r in read if not any(r == p or r.startswith(p + "/") for p in paths))
    assert not uncovered, f"read by --only brain but not in BRAIN_ONLY_PATHS: {uncovered}"
    assert "install.sh" in paths


# --- the operator tools follow the venv ----------------------------------------------
# brain_stage_tools / tools_point_at / tools_adopt with brain_point_at, brain_swap and
# brain_prune around them, against a prefix, a tools dir and two brain sources in a temp
# dir. SUDO runs what it is given, minus the -o root -g root a user cannot do, and logs
# it: the ownership itself is root's to check (on a box, as root).

TOOLS_STUBS = r'''
sudo() { return 0; }
run()  { "$@"; }
SUDO() {
  printf 'SUDO %s\n' "$*" >> "$T/sudo.log"
  local a=() x skip=0
  for x in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$x" in -o|-g) skip=1 ;; *) a+=("$x") ;; esac
  done
  "${a[@]}"
}
PREFIX="$T/opt" BRAIN_KEEP=3 TOOLS_DIR="$T/lib" TOOLS_CLI="$T/bin/teaport"
BRAIN_VENVS="$PREFIX/venvs" BRAIN_LINK="$PREFIX/venv" BRAIN_PREV="$PREFIX/venv.previous"
BRAIN_LEGACY="$PREFIX/venv.pre-uv"
mkdir -p "$BRAIN_VENVS" "$T/bin"
# build <release> <source>: a venv dir and its tools, from $T/<source>
build() { mkdir -p "$BRAIN_VENVS/$1"; SRC_DIR="$T/$2/brain"; brain_stage_tools "$BRAIN_VENVS/$1"; }
'''
TOOLS_FNS = ["tools_id", "brain_stage_tools", "tools_point_at", "tools_adopt", "tools_link", "tools_has",
             "brain_point_at", "brain_prune"]


def _tools_box(*sources):
    tmp = tempdir("teaport-tools-")
    for s in sources:
        for rel, text in (("cli/teaport", f"cli {s}\n"), ("brain/teaport_brain/config_apply.py", f"helper {s}\n")):
            p = pathlib.Path(tmp, s, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    return tmp


def _tools_run(tmp, script, extra_fns=()):
    return _run(tmp, TOOLS_STUBS + _line("CONFIG_HELPER=") + _line("STAGED=") + script, TOOLS_FNS + list(extra_fns))


def _installed(tmp):
    """The CLI and helper as a caller reaches them: through the fixed paths."""
    cli, helper = pathlib.Path(tmp, "bin/teaport"), pathlib.Path(tmp, "lib/config_apply.py")
    return cli.read_text().strip(), helper.read_text().strip()


def test_a_swap_and_a_rollback_by_hand_move_the_tools_with_the_venv():
    tmp = _tools_box("v1", "v2")
    rc, out = _tools_run(tmp, 'build r1 v1; brain_point_at "$BRAIN_VENVS/r1"; build r2 v2; brain_point_at "$BRAIN_VENVS/r2"')
    assert rc == 0 and _installed(tmp) == ("cli v2", "helper v2"), out
    # What --rollback brain does (main_rollback_brain -> brain_swap -> brain_point_at).
    rc, out = _tools_run(tmp, 'brain_point_at "$(readlink "$BRAIN_PREV")"')
    assert rc == 0 and _installed(tmp) == ("cli v1", "helper v1"), out
    # ... and again, to undo it.
    rc, out = _tools_run(tmp, 'brain_point_at "$(readlink "$BRAIN_PREV")"')
    assert rc == 0 and _installed(tmp) == ("cli v2", "helper v2"), out
    # The fixed paths (the sudoers line's, PATH's) are links through `current`, into the
    # tools dir and never into the run user's prefix; every write there went through
    # SUDO, as root:root.
    lib = pathlib.Path(tmp, "lib")
    assert os.readlink(lib / "config_apply.py") == "current/config_apply.py"
    assert os.readlink(pathlib.Path(tmp, "bin/teaport")) == f"{lib}/current/teaport"
    assert os.readlink(lib / "current") == "releases/r2"
    for f in ("bin/teaport", "lib/config_apply.py"):
        assert os.path.realpath(pathlib.Path(tmp, f)).startswith(f"{lib}/releases/"), f
    installs = [c for c in _sudo_calls(tmp) if c.startswith("SUDO install ")]
    assert installs and all("-o root -g root" in c for c in installs), installs


def test_an_automatic_rollback_puts_the_previous_tools_back():
    tmp = _tools_box("v1", "v2")
    rc, out = _tools_run(tmp, 'build r1 v1; brain_point_at "$BRAIN_VENVS/r1"')
    assert rc == 0, out
    # The new brain does not come back; the previous one does.
    script = r'''
DRY_RUN=0 SWAP_ALSO=""
brain_units() { SWAP_JUDGE=teaport-brain.service; }
brain_restart() { [ -e "$T/restarted" ] && return 0; touch "$T/restarted"; FAILED_UNIT=teaport-brain.service; return 1; }
sip_brain_restore() { :; }
build r2 v2; brain_swap "$BRAIN_VENVS/r2" --auto-rollback'''
    rc, out = _tools_run(tmp, script, ["brain_swap"])
    assert rc != 0 and "DIE rolled back" in out and "config helper are back too" in out, out
    assert _installed(tmp) == ("cli v1", "helper v1"), out
    assert os.readlink(pathlib.Path(tmp, "opt/venv")).endswith("/r1")


def test_tools_installed_before_releases_had_their_own_become_the_live_releases():
    tmp = _tools_box("v1")
    # A box from before: plain files, and a live venv with no tools of its own.
    pathlib.Path(tmp, "lib").mkdir()
    pathlib.Path(tmp, "bin/teaport").parent.mkdir()
    pathlib.Path(tmp, "bin/teaport").write_text("cli old\n")
    pathlib.Path(tmp, "lib/config_apply.py").write_text("helper old\n")
    rc, out = _tools_run(tmp, 'mkdir "$BRAIN_VENVS/r0"; ln -s "$BRAIN_VENVS/r0" "$BRAIN_LINK"; '
                              'build r1 v1; brain_point_at "$BRAIN_VENVS/r1"')
    assert rc == 0 and _installed(tmp) == ("cli v1", "helper v1"), out
    assert "kept as r0's" in out, out
    rc, out = _tools_run(tmp, 'brain_point_at "$(readlink "$BRAIN_PREV")"')
    assert rc == 0 and _installed(tmp) == ("cli old", "helper old"), out
    assert os.path.islink(pathlib.Path(tmp, "bin/teaport")), out


def test_a_release_with_no_tools_or_a_repointed_link_leaves_the_tools_alone():
    tmp = _tools_box("v1", "v2")
    rc, out = _tools_run(tmp, 'build r1 v1; brain_point_at "$BRAIN_VENVS/r1"; build r2 v2; brain_point_at "$BRAIN_VENVS/r2"')
    assert rc == 0, out
    for prev in ('mkdir "$BRAIN_VENVS/r0"; echo "$BRAIN_VENVS/r0"',   # built before per-release tools
                 'mkdir -p "$T/x/.."; echo "$T/x/.."',                 # not a release name at all
                 'echo "$TOOLS_DIR"'):                                 # a dir that is not a release
        rc, out = _tools_run(tmp, f'tools_point_at "$({prev})"')
        assert rc == 0 and "has no operator tools of its own" in out, (prev, out)
        assert _installed(tmp) == ("cli v2", "helper v2"), (prev, out)


def test_a_source_without_the_cli_or_helper_keeps_the_installed_one():
    tmp = _tools_box("v1", "v2", "v3")
    pathlib.Path(tmp, "v2/cli/teaport").unlink()                                  # a bare brain/
    pathlib.Path(tmp, "v3/brain/teaport_brain/config_apply.py").unlink()          # predates the page
    rc, out = _tools_run(tmp, 'build r1 v1; brain_point_at "$BRAIN_VENVS/r1"; build r2 v2; brain_point_at "$BRAIN_VENVS/r2"')
    assert rc == 0 and _installed(tmp) == ("cli v1", "helper v2"), out
    rc, out = _tools_run(tmp, 'build r3 v3; brain_point_at "$BRAIN_VENVS/r3"')
    assert rc == 0 and _installed(tmp) == ("cli v3", "helper v2"), out


def test_an_automatic_rollback_to_a_release_without_tools_says_they_stayed():
    tmp = _tools_box("v2")
    script = r'''
DRY_RUN=0 SWAP_ALSO=""
brain_units() { SWAP_JUDGE=teaport-brain.service; }
brain_restart() { [ -e "$T/restarted" ] && return 0; touch "$T/restarted"; FAILED_UNIT=teaport-brain.service; return 1; }
sip_brain_restore() { :; }
mkdir "$BRAIN_VENVS/r0"; ln -s "$BRAIN_VENVS/r0" "$BRAIN_LINK"
build r2 v2; brain_swap "$BRAIN_VENVS/r2" --auto-rollback'''
    rc, out = _tools_run(tmp, script, ["brain_swap"])
    assert rc != 0 and "DIE rolled back" in out, out
    assert "are still /" in out and "/r2's (" in out and "are back too" not in out, out
    # A dry run says the same before anything moves.
    rc, out = _tools_run(tmp, 'DRY_RUN=1; brain_units() { SWAP_JUDGE=""; }; brain_swap "$BRAIN_VENVS/r0" ""',
                         ["brain_swap"])
    assert rc == 0 and "operator tools left as they are (it has none of its own)" in out, out


def test_a_dry_run_shows_the_tools_it_would_stage_keep_and_swap_and_touches_nothing():
    tmp = _tools_box("v1")
    pathlib.Path(tmp, "lib").mkdir()
    pathlib.Path(tmp, "bin/teaport").parent.mkdir()
    pathlib.Path(tmp, "bin/teaport").write_text("cli old\n")
    pathlib.Path(tmp, "lib/config_apply.py").write_text("helper old\n")
    script = r'''
DRY_RUN=1 SWAP_ALSO=""
SUDO() { printf 'DRY sudo %s\n' "$*"; }   # install.sh's SUDO under --dry-run
brain_units() { SWAP_JUDGE=teaport-brain.service; }
mkdir "$BRAIN_VENVS/r0"; ln -s "$BRAIN_VENVS/r0" "$BRAIN_LINK"
STAGED="$BRAIN_VENVS/r1"; SRC_DIR="$T/v1/brain"
brain_stage_tools "$STAGED"; brain_swap "$STAGED" --auto-rollback'''
    rc, out = _tools_run(tmp, script, ["brain_swap"])
    assert rc == 0, out
    assert "DRY sudo install -m 0755 -o root -g root" in out and "releases/r1/teaport" in out, out
    assert "the installed teaport CLI and config helper are kept as r0's" in out, out
    assert "its operator tools too; restart teaport-brain.service" in out, out
    assert "none of its own" not in out, out
    assert sorted(os.listdir(pathlib.Path(tmp, "lib"))) == ["config_apply.py"], out
    assert _installed(tmp) == ("cli old", "helper old") and not os.path.islink(pathlib.Path(tmp, "bin/teaport"))


def test_pruning_removes_the_tools_of_removed_venvs_and_keeps_the_live_ones():
    tmp = _tools_box("v1")
    script = ('for r in r1 r2 r3 r4 r5 r6; do build "$r" v1; done; '
              'brain_point_at "$BRAIN_VENVS/r1"; brain_point_at "$BRAIN_VENVS/r2"; '
              'rm -rf "$BRAIN_VENVS/r2"; brain_prune')   # the live venv's dir gone: its tools still stay
    rc, out = _tools_run(tmp, script)
    assert rc == 0, out
    kept = sorted(os.listdir(pathlib.Path(tmp, "lib/releases")))
    # Newest three venvs (r4-r6), the rollback target (r1), and current (r2).
    assert kept == ["r1", "r2", "r4", "r5", "r6"], (kept, out)
    assert sorted(os.listdir(pathlib.Path(tmp, "opt/venvs"))) == ["r1", "r4", "r5", "r6"]


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
