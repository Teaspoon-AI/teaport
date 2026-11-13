#
# install.sh's brain package path (#60) and the --only brain follow-ups (issue #59):
# --only brain refuses a box with no appliance (main_brain_only) and installs the
# teaport-brain .deb (brain_deb_fetch, brain_deb_keep, brain_swap), putting the package
# that was installed before back when the brain does not come up; --rollback brain does
# the same by hand (main_rollback_brain); the package cache keeps three (brain_deb_prune);
# the pre-package CLI and helper are retired only once the package's are in place
# (brain_retire_old_tools); its freshness check looks only at what it installs
# (check_brain_source); and the three privileged reads of an env file share one helper
# (read_env_file: resolve_gateway_token, write_env, brain_verify).
#
# The functions are taken out of install.sh as they are and run by bash with the
# commands around them stubbed (sudo, systemctl, curl, the phases), against a temp dir.
# The freshness tests build a real git checkout with an upstream to be behind.
# Nothing outside a temp dir is touched.
#
# Run: python test_install_brain_only.py   (or via pytest test_suite.py)
#
import json
import os
import pathlib
import re
import subprocess

import appliance
from tempdirs import tempdir

INSTALL = pathlib.Path(__file__).resolve().parents[2] / "install.sh"

STUBS = r'''
set -euo pipefail
DRY_RUN=0 ONLY="" ALLOW_STALE_SOURCE=0 BRAIN_PORT=7861 GATEWAY_TOKEN="" RUN_USER=nobody
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


# --- --only brain refuses a box with no appliance ------------------------------------

ONLY_BRAIN_STUBS = r'''
BRAIN_DEB_CACHED=/cache/x.deb BRAIN_DEB_NEW=2.0
systemctl() { [ "$1" = cat ] && [ "${NO_UNIT:-0}" = 0 ]; }   # the brain unit is installed
for f in check_brain_source brain_deb_fetch brain_deb_keep sip_brain_detach brain_swap sip_brain_retire \
         brain_retire_old_tools brain_retire_old_venvs install_busy_lamp install_dns_resilience install_arp_multihome \
         brain_deb_prune; do eval "$f() { echo STEP $f; }"; done
'''


def _brain_only(tmp, unit=True, dry_run=False, logs_capped=True):
    prefix = pathlib.Path(tmp, "opt")
    prefix.mkdir()
    # logrotate and the journal drop-in, under tmp: there (a box a full run has set up since
    # setup_logs) or not
    logrotate, dropin = pathlib.Path(tmp, "logrotate"), pathlib.Path(tmp, "journald-teaport.conf")
    applied = pathlib.Path(tmp, "journald.applied")
    if logs_capped:
        logrotate.write_text("#!/bin/sh\n")
        logrotate.chmod(0o755)
        dropin.write_text("[Journal]\n")
        applied.write_text("Storage=persistent\n")
    script = (f'{ONLY_BRAIN_STUBS}PREFIX="{prefix}"; '
              f'LOGROTATE_BIN="{logrotate}"; JOURNAL_DROPIN="{dropin}"; JOURNAL_APPLIED="{applied}"; '
              f'DRY_RUN={int(dry_run)}; ')
    if not unit:
        script += 'NO_UNIT=1; '
    script += 'main_brain_only'
    return _run(tmp, script, ["main_brain_only"])


def test_only_brain_refuses_a_box_with_no_appliance():
    rc, out = _brain_only(tempdir("teaport-only-"), unit=False)
    assert rc != 0 and "DIE no installed appliance here" in out and "without it first" in out, out
    assert "STEP" not in out, out   # refused before any package was fetched or installed


def test_only_brain_dry_run_warns_and_shows_the_plan():
    rc, out = _brain_only(tempdir("teaport-only-"), unit=False, dry_run=True)
    assert rc == 0 and "WARN no installed appliance here" in out and "STEP brain_swap" in out, out


def test_only_brain_says_when_the_box_has_no_capped_logs_yet():
    # --only brain installs no system packages: a box no full run has set up since
    # setup_logs (no logrotate) keeps its unrotated /var/log, and is told so; it goes on.
    rc, out = _brain_only(tempdir("teaport-only-"), logs_capped=False)
    assert rc == 0 and "WARN logs may not be rotated or capped on this box yet" in out, out
    assert "without --only once" in out and "STEP brain_swap" in out, out
    rc, out = _brain_only(tempdir("teaport-only-"))
    assert rc == 0 and "may not be rotated" not in out, out


def test_only_brain_installs_the_package_first_and_the_rest_only_after_the_verified_swap():
    rc, out = _brain_only(tempdir("teaport-only-"))
    assert rc == 0, out
    order = [ln.split()[1] for ln in out.splitlines() if ln.startswith("STEP ")]
    assert order == ["check_brain_source", "brain_deb_fetch", "brain_deb_keep", "sip_brain_detach",
                     "brain_swap", "sip_brain_retire", "brain_retire_old_tools", "brain_retire_old_venvs",
                     "install_busy_lamp",
                     "install_dns_resilience", "install_arp_multihome", "brain_deb_prune"], order


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
    script = (f'HERE="{tmp}/src"; RESOLVED_DROPIN="{dropin}"; '
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


# --- the ARP drop-in for Ethernet and Wi-Fi on one subnet (#225) -----------------------

def _arp(tmp, jetson=True, dry_run=False, fail="", sysctl_rc=0, takes=True):
    """install_arp_multihome against a temp drop-in, marker and /proc conf/all; the real
    write_root, sudo recorded. install writes the file (FAIL=install fails it); sysctl
    exits SYSCTL_RC and, when TAKES=1, leaves 1 and 2 in the fake /proc (FAIL=sysctl:
    exit 1, nothing applied)."""
    proc = pathlib.Path(tmp, "proc")
    if not proc.exists():
        proc.mkdir()
        (proc / "arp_ignore").write_text("0\n")
        (proc / "arp_announce").write_text("0\n")
    tegra = pathlib.Path(tmp, "nv_tegra_release")
    if jetson:
        tegra.write_text("# R39 (release)\n")
    dropin = pathlib.Path(tmp, "etc/sysctl.d/60-teaport-multihome.conf")
    applied = pathlib.Path(tmp, "state/arp-multihome.applied")
    script = (f'ARP_SYSCTL_DROPIN="{dropin}"; ARP_APPLIED="{applied}"; ARP_PROC="{proc}"; '
              f'NV_TEGRA_RELEASE="{tegra}"; DRY_RUN={int(dry_run)}; FAIL="{fail}"; '
              f'SYSCTL_RC={sysctl_rc}; TAKES={int(takes)}\n'
              'SUDO() { printf "%s\\n" "$*" >> "$T/sudo.log"; [ "$DRY_RUN" = 1 ] && return 0; '
              '[ "$1" = "$FAIL" ] && return 1; '
              'case "$1" in install) mkdir -p "$(dirname "${@: -1}")"; cat > "${@: -1}" ;; '
              'rm) command rm -f "${@: -1}" ;; '
              'sysctl) [ "$TAKES" = 1 ] && { echo 1 > "$ARP_PROC/arp_ignore"; '
              'echo 2 > "$ARP_PROC/arp_announce"; }; return "$SYSCTL_RC" ;; esac; }\n'
              'install_arp_multihome; echo DONE\n')
    return _run(tmp, script, ["write_root", "install_arp_multihome"]), dropin, applied


def _sysctls(tmp):
    return [c for c in _sudo_calls(tmp) if c.startswith("sysctl")]


def test_the_arp_drop_in_is_written_applied_and_recorded_once():
    tmp = tempdir("teaport-arp-")
    (rc, out), dropin, applied = _arp(tmp)
    assert rc == 0 and "DONE" in out and "WARN" not in out, out
    text = dropin.read_text()
    assert "net.ipv4.conf.all.arp_ignore = 1\n" in text, text
    assert "net.ipv4.conf.all.arp_announce = 2\n" in text, text
    assert applied.read_text() == text, applied.read_text()
    calls = _sudo_calls(tmp)
    # Any old record goes first, then the file, then the apply, then the new record.
    assert calls[0] == f"rm -f {applied}", calls
    assert calls[1] == f"install -D -m 0644 -o root -g root /dev/stdin {dropin}", calls
    # Applied the way boot applies it: every sysctl.d file in order, so an operator's
    # drop-in that sorts after this one still wins.
    assert calls[2] == "sysctl -q --system", calls
    # A re-run with the file and the record in place touches nothing, whatever the live
    # values are: an operator's later drop-in (99-local.conf) is not overridden.
    pathlib.Path(tmp, "sudo.log").unlink()
    (pathlib.Path(tmp, "proc") / "arp_ignore").write_text("2\n")
    (rc, out), _, _ = _arp(tmp)
    assert rc == 0 and _sudo_calls(tmp) == [], (_sudo_calls(tmp), out)


def test_the_arp_drop_in_is_rewritten_and_reapplied_when_it_changed():
    tmp = tempdir("teaport-arp-")
    _, dropin, _ = _arp(tmp)
    dropin.write_text("net.ipv4.conf.all.arp_ignore = 0\n")   # an edit to our file
    pathlib.Path(tmp, "sudo.log").unlink()
    (rc, out), _, _ = _arp(tmp)
    calls = _sudo_calls(tmp)
    assert rc == 0 and "arp_ignore = 1" in dropin.read_text(), out
    assert any(c.endswith(str(dropin)) for c in calls) and "sysctl -q --system" in calls, calls


def test_an_unrelated_key_failing_sysctl_is_not_a_failed_apply():
    # sysctl --system exits 1 for another file's key the kernel lacks (net.bridge.* with
    # no br_netfilter) after applying ours: the live values decide, and they are right.
    tmp = tempdir("teaport-arp-")
    (rc, out), _, applied = _arp(tmp, sysctl_rc=1)
    assert rc == 0 and "WARN" not in out and "DONE" in out, out
    assert applied.exists(), out


def test_an_apply_that_did_not_take_is_retried_by_the_next_run():
    tmp = tempdir("teaport-arp-")
    (rc, out), dropin, applied = _arp(tmp, fail="sysctl")
    assert rc == 0 and "WARN network: ARP is arp_ignore=0 arp_announce=0" in out, out
    assert "the next run tries again" in out and dropin.exists() and not applied.exists(), out
    # The next run finds the file as it wants it but no record: it applies again (the
    # file is not rewritten), and this time it takes.
    pathlib.Path(tmp, "sudo.log").unlink()
    (rc, out), _, _ = _arp(tmp)
    calls = _sudo_calls(tmp)
    assert rc == 0 and "WARN" not in out and applied.exists(), out
    assert not any(c.endswith(str(dropin)) for c in calls) and _sysctls(tmp) == ["sysctl -q --system"], calls
    # and then it is done
    pathlib.Path(tmp, "sudo.log").unlink()
    _arp(tmp)
    assert _sudo_calls(tmp) == [], _sudo_calls(tmp)


def test_a_rewritten_drop_in_whose_apply_fails_is_retried_despite_an_old_record():
    # An earlier run applied and recorded; then the file was edited from outside. This
    # run rewrites it and the apply fails: the old record must not make the next run skip.
    tmp = tempdir("teaport-arp-")
    _, dropin, applied = _arp(tmp)
    assert applied.exists()
    dropin.write_text("net.ipv4.conf.all.arp_ignore = 0\n")
    for name in ("arp_ignore", "arp_announce"):
        (pathlib.Path(tmp, "proc") / name).write_text("0\n")
    (rc, out), _, _ = _arp(tmp, fail="sysctl")
    assert rc == 0 and "the next run tries again" in out and not applied.exists(), out
    assert "arp_ignore = 1" in dropin.read_text()
    pathlib.Path(tmp, "sudo.log").unlink()
    (rc, out), _, _ = _arp(tmp)
    assert rc == 0 and "WARN" not in out and applied.exists(), out
    assert _sysctls(tmp) == ["sysctl -q --system"], _sudo_calls(tmp)


def test_an_operators_later_drop_in_is_said_once_and_left_alone():
    # sysctl --system applied every file cleanly and the values are not ours: a drop-in
    # sorting after ours (99-local.conf: arp_ignore = 2) sets them on purpose. One log
    # line, recorded, and the next run neither re-applies nor says anything.
    tmp = tempdir("teaport-arp-")
    proc = pathlib.Path(tmp, "proc")
    proc.mkdir()
    (proc / "arp_ignore").write_text("2\n")
    (proc / "arp_announce").write_text("2\n")
    (rc, out), _, applied = _arp(tmp, takes=False)
    assert rc == 0 and "WARN" not in out and applied.exists(), out
    lines = [ln for ln in out.splitlines() if "sets ARP to arp_ignore=2 arp_announce=2" in ln]
    assert len(lines) == 1 and lines[0].startswith("LOG network: a sysctl.d drop-in after"), out
    pathlib.Path(tmp, "sudo.log").unlink()
    (rc, out), _, _ = _arp(tmp, takes=False)
    assert rc == 0 and out.strip() == "DONE" and _sudo_calls(tmp) == [], (out, _sudo_calls(tmp))


def test_a_failed_write_warns_and_goes_on():
    tmp = tempdir("teaport-arp-")
    (rc, out), dropin, applied = _arp(tmp, fail="install")
    assert rc == 0 and "WARN network: could not write" in out and "DONE" in out, out
    assert _sysctls(tmp) == [] and not applied.exists(), _sudo_calls(tmp)


def test_the_arp_drop_in_leaves_a_non_jetson_alone_but_a_dry_run_shows_it():
    tmp = tempdir("teaport-arp-")
    (rc, out), dropin, _ = _arp(tmp, jetson=False)
    assert rc == 0 and "not a Jetson" in out and not dropin.exists(), out
    assert _sudo_calls(tmp) == [], out
    (rc, out), dropin, applied = _arp(tmp, jetson=False, dry_run=True)
    assert rc == 0 and f"[dry-run] write {dropin}" in out and not dropin.exists(), out
    assert "WARN" not in out and not applied.exists(), out
    assert _sysctls(tmp) == ["sysctl -q --system"], _sudo_calls(tmp)


# --- the systemd units are root's and 0644, whatever the umask (#199) ------------------

def _render(tmp, dry_run=False, masked=""):
    """render_unit for two of the real templates, under umask 077, with the real write_root;
    sudo recorded. SUDO install writes under $T/root with the -m mode set (as install sets
    it), SUDO tee under $T/root as the umask leaves it."""
    script = (f'HERE="{INSTALL.parent}" RUN_USER=op PREFIX=/opt/teaport ETC=/etc/teaport '
              f'SANDBOX=sb SECRETS=/home/op/.config/teaport DRY_RUN={int(dry_run)}; umask 077\n'
              'SUDO() { printf "%s\\n" "$*" >> "$T/sudo.log"; local dest="$T/root${*: -1}" mode="" prev="" a\n'
              '  mkdir -p "$(dirname "$dest")"\n'
              '  case "$1" in\n'
              '    install) for a in "$@"; do if [ "$prev" = -m ]; then mode="$a"; fi; prev="$a"; done\n'
              '             cat > "$dest"; chmod "$mode" "$dest" ;;\n'
              '    tee) command tee "$dest" >/dev/null ;;\n'
              '  esac; }\n'
              f'readlink() {{ [ "$1" = "/etc/systemd/system/{masked}" ] && echo /dev/null; }}\n'
              'render_unit teaport-brain.service.in teaport-brain.service\n'
              'render_unit teaport-engine.service.in teaport-engine.service\n')
    return _run(tmp, script, ["write_root", "render_unit"])


def test_a_unit_is_written_through_write_root_and_reads_back_as_anyone():
    # systemd reads a 0600 unit as root, but `systemctl cat` reads it as the caller: the
    # CLI (installed) and --only brain took a unit sudo's umask left 0600 for none at all.
    tmp = tempdir("teaport-units-")
    rc, out = _render(tmp)
    assert rc == 0, out
    assert _sudo_calls(tmp) == [f"install -D -m 0644 -o root -g root /dev/stdin /etc/systemd/system/{u}"
                                for u in ("teaport-brain.service", "teaport-engine.service")]
    for u in ("teaport-brain.service", "teaport-engine.service"):
        unit = pathlib.Path(tmp, "root/etc/systemd/system", u)
        assert oct(unit.stat().st_mode & 0o777) == "0o644", u
        text = unit.read_text()
        assert "User=op\n" in text and "@USER@" not in text and "ExecStart=" in text, text
    # A unit left 0600 by an earlier run is put right by the next: install replaces the file
    # and sets its mode (tee into it kept the 0600).
    unit = pathlib.Path(tmp, "root/etc/systemd/system/teaport-brain.service")
    unit.chmod(0o600)
    rc, out = _render(tmp)
    assert rc == 0 and oct(unit.stat().st_mode & 0o777) == "0o644", out


def test_a_masked_unit_stays_masked():
    # An operator's `systemctl mask` is a link to /dev/null; install -D would replace it
    # with the unit. It is left as it is, said once, and the other unit is written.
    tmp = tempdir("teaport-units-")
    rc, out = _render(tmp, masked="teaport-engine.service")
    assert rc == 0 and "teaport-engine.service is masked on this machine" in out, out
    assert _sudo_calls(tmp) == ["install -D -m 0644 -o root -g root /dev/stdin "
                                "/etc/systemd/system/teaport-brain.service"], _sudo_calls(tmp)


def test_a_dry_run_names_each_unit_once_and_writes_none():
    tmp = tempdir("teaport-units-")
    rc, out = _render(tmp, dry_run=True)
    assert rc == 0 and out.count("[dry-run]") == 2, out
    assert "[dry-run] render teaport-brain.service.in -> /etc/systemd/system/teaport-brain.service" in out
    assert _sudo_calls(tmp) == [] and not pathlib.Path(tmp, "root").exists(), out


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
    for f in ("brain/uv.lock", "cli/teaport", "systemd/teaport-brain.service.in",
              "packaging/xvf3800/60-teaport-xvf3800.rules",
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
    # The brain itself (brain/, the CLI) is the package now, not the checkout.
    for touching in ("docs/x.md", "plugin/x.ts", "packaging/wifi-setup/x", "brain/uv.lock", "cli/teaport"):
        tmp, here = _behind(touching)
        rc, out = _check(tmp, here, "brain")
        assert rc == 0 and "CHECKED" in out and "behind" not in out, (touching, out)
        # The full install consumes all of it: still refused.
        rc, out = _check(tmp, here, "")
        assert rc != 0 and "1 commit(s) behind origin/main —" in out, (touching, out)


def test_only_brain_is_refused_for_upstream_commits_to_what_it_installs():
    # install.sh too: how the package is installed, and some of what --only brain writes, is in it.
    for touching in ("systemd/teaport-brain.service.in", "packaging/xvf3800/60-teaport-xvf3800.rules",
                     "packaging/resolved/60-teaport-serve-stale.conf", "install.sh"):
        tmp, here = _behind(touching)
        rc, out = _check(tmp, here, "brain")
        assert rc != 0, (touching, out)
        assert ("1 commit(s) behind origin/main in what --only brain installs "
                "(systemd packaging/xvf3800 packaging/resolved install.sh)") in out, (touching, out)


def test_uncommitted_changes_in_any_installed_path_are_named():
    tmp, here = _behind("docs/x.md")   # behind only in what --only brain does not install
    pathlib.Path(here, "systemd/teaport-brain.service.in").write_text("local\n")
    rc, out = _check(tmp, here, "brain")
    assert rc == 0 and f"uncommitted changes in {here} are used too: systemd\n" in out, out
    pathlib.Path(here, "install.sh").write_text("local\n")
    rc, out = _check(tmp, here, "brain")
    assert rc == 0 and f"are used too: systemd install.sh\n" in out, out


# The checkout paths --only brain's functions read, as they spell them: the unit
# templates render_unit takes from $HERE/systemd, and packaging_src's files.
_READS = {
    r'render_unit ([\w.-]+\.in)': "systemd/",
    r'packaging_src ([\w./-]+)': "packaging/",
    r'\$HERE/([\w./-]+)': "",
}


def test_every_checkout_path_only_brain_reads_is_in_its_freshness_check():
    paths = re.findall(r"\((.*)\)", _line("BRAIN_ONLY_PATHS="))[0].split()
    read = set()
    for fn in ("brain_render_units", "install_busy_lamp", "install_dns_resilience"):
        body = _fn(fn)
        for pattern, base in _READS.items():
            read.update(base + m for m in re.findall(pattern, body))
    # Found at all: a respelled reference must not make this pass by matching nothing.
    assert {"systemd/teaport-brain.service.in", "packaging/xvf3800/60-teaport-xvf3800.rules",
            "packaging/resolved/60-teaport-serve-stale.conf"} <= read, read
    uncovered = sorted(r for r in read if not any(r == p or r.startswith(p + "/") for p in paths))
    assert not uncovered, f"read by --only brain but not in BRAIN_ONLY_PATHS: {uncovered}"
    assert "install.sh" in paths


# --- the package: fetch, keep, swap, roll back, prune ---------------------------------
# brain_deb_* and brain_swap run by bash with dpkg, apt (SUDO) and the restart stubbed,
# against a temp cache. A .deb here is a file plus a `.fields` file the dpkg-deb stub reads.

DEB_STUBS = r'''
PREFIX=/opt/teaport STATE="$T/state" BRAIN_DEBS="$T/cache" BRAIN_DEB_KEEP=3 BRAIN_DEB_SRC="" MF=""
BRAIN_PKG=teaport-brain DRY_RUN=0 BRAIN_DEB_SHA="" BRAIN_DEB_DOWNLOADED=0 BRAIN_DEB_ADDED=0
mkdir -p "$BRAIN_DEBS" "$STATE"
sudo() { return 0; }
run()  { "$@"; }
SUDO() {
  printf 'SUDO %s\n' "$*" >> "$T/sudo.log"
  local a=() x skip=0
  for x in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$1:$x" in install:-o|install:-g) skip=1 ;; *) a+=("$x") ;; esac   # root:root of install
  done
  case "${a[0]}" in
    env) printf 'APT %s\n' "${a[*]:2}" >> "$T/apt.log"
         local f="${a[-1]##*/}" v; v="${f#teaport-brain_}"; v="${v%%_*}"
         # apt that never reaches the package (the lock) changes nothing; a postinst that
         # fails leaves the new version half-configured; otherwise it is installed.
         if [ "${APT_LOCKED:-}" = "$f" ]; then return 1; fi
         if [ "${APT_FAILS:-}" = "$f" ]; then echo "half-configured $v" > "$T/dq"; return 1; fi
         echo "installed $v" > "$T/dq" ;;
    chown) : ;;
    install) "${a[@]}"; if [ -f "${a[-2]}.fields" ]; then cp "${a[-2]}.fields" "${a[-1]}.fields"; fi ;;
    mv) "${a[@]}"; if [ -f "${a[-2]}.fields" ]; then mv "${a[-2]}.fields" "${a[-1]}.fields"; fi ;;
    *) "${a[@]}" ;;
  esac
}
dpkg() { [ "$1" = --print-architecture ] && echo arm64; }
dpkg-deb() { sed -n "s/^$3: //p" "$2.fields"; }
dpkg-query() {
  if [ -f "$T/dq" ]; then cat "$T/dq"
  elif [ -n "${INSTALLED:-}" ]; then printf '%s %s\n' "${STATUS:-installed}" "$INSTALLED"; fi
}
download() { cp "$T/remote/$(basename "$1")" "$2"; cp "$T/remote/$(basename "$1").fields" "$2.fields"; }
mget() { python3 -c "import json,sys; d=json.load(open('$MF'))
for k in sys.argv[1].split('.'): d=d[k]
print(d)" "$1"; }
'''
DEB_FNS = ["brain_deb_arch", "brain_installed_version", "brain_package_version", "brain_deb_path",
           "brain_deb_fetch", "brain_deb_verify", "brain_deb_keep", "brain_deb_apply", "brain_deb_prune"]


def _deb(path, version, package="teaport-brain", arch="arm64"):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"deb {version}\n")
    pathlib.Path(str(path) + ".fields").write_text(
        f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\n")
    return path


def _cached(tmp, version, age=0):
    p = _deb(pathlib.Path(tmp, "cache", f"teaport-brain_{version}_arm64.deb"), version)
    os.utime(p, (1_700_000_000 + age, 1_700_000_000 + age))
    os.utime(str(p) + ".fields", (1_700_000_000 + age, 1_700_000_000 + age))
    return p


def _debrun(tmp, script, fns=(), env=None):
    return _run(tmp, DEB_STUBS + script, DEB_FNS + list(fns), env)


def test_a_given_package_is_checked_and_kept_under_its_canonical_name():
    tmp = tempdir("teaport-deb-")
    deb = _deb(pathlib.Path(tmp, "in/whatever.deb"), "1.2.3")
    rc, out = _debrun(tmp, f'BRAIN_DEB_SRC="{deb}"; brain_deb_fetch; brain_deb_keep; echo "$BRAIN_DEB_NEW $BRAIN_DEB_CACHED"')
    assert rc == 0 and f"1.2.3 {tmp}/cache/teaport-brain_1.2.3_arm64.deb" in out, out
    assert pathlib.Path(tmp, "cache/teaport-brain_1.2.3_arm64.deb").read_text() == "deb 1.2.3\n"
    # Something that is not the package, or is for another machine, is refused.
    for fields, why in (({"package": "other"}, "is not a teaport-brain package"),
                        ({"arch": "amd64"}, "is for amd64, this machine is arm64")):
        bad = _deb(pathlib.Path(tmp, "in/bad.deb"), "1.0", **fields)
        rc, out = _debrun(tmp, f'BRAIN_DEB_SRC="{bad}"; brain_deb_fetch')
        assert rc != 0 and why in out, out
    rc, out = _debrun(tmp, f'BRAIN_DEB_SRC="{tmp}/in/missing.deb"; brain_deb_fetch')
    assert rc != 0 and "is not a file" in out, out


def test_without_a_package_the_manifest_names_one_or_the_run_says_how_to_give_one():
    tmp = tempdir("teaport-deb-")
    mf = pathlib.Path(tmp, "m.json")
    mf.write_text('{"brain": {"repo": "x/y", "tag": "main"}}')
    rc, out = _debrun(tmp, f'MF="{mf}"; brain_deb_fetch')
    assert rc != 0 and "names no brain package" in out and "TEAPORT_BRAIN_DEB=" in out, out
    # A dry run says so and goes on; a real run stops.
    rc, out = _debrun(tmp, f'MF="{mf}"; DRY_RUN=1; brain_deb_fetch; echo "NEW $BRAIN_DEB_NEW"')
    assert rc == 0 and "WARN the release manifest names no brain package yet" in out and "NEW (none named)" in out, out
    # It names one: downloaded, checked against its fields.
    _deb(pathlib.Path(tmp, "remote/teaport-brain_0.1.0_arm64.deb"), "0.1.0")
    mf.write_text('{"brain": {"deb": {"url": "https://x/teaport-brain_0.1.0_arm64.deb", "sha256": "aa"}}}')
    rc, out = _debrun(tmp, f'MF="{mf}"; brain_deb_fetch; echo "GOT $BRAIN_DEB_NEW"')
    assert rc == 0 and "GOT 0.1.0" in out, out


def test_the_package_wants_the_default_prefix():
    tmp = tempdir("teaport-deb-")
    rc, out = _debrun(tmp, 'PREFIX=/srv/x; brain_deb_fetch')
    assert rc != 0 and "installs to /opt/teaport" in out, out


def test_what_apt_installs_is_the_root_copy_and_the_copy_is_what_was_checked():
    # The download directory is the run user's: the original can change after the check, so
    # the sha256 and the package fields are checked on the root-owned copy.
    import hashlib
    tmp = tempdir("teaport-deb-")
    _deb(pathlib.Path(tmp, "remote/teaport-brain_0.1.0_arm64.deb"), "0.1.0")
    sha = hashlib.sha256(b"deb 0.1.0\n").hexdigest()
    mf = pathlib.Path(tmp, "m.json")
    mf.write_text(json.dumps({"brain": {"deb": {"url": "https://x/teaport-brain_0.1.0_arm64.deb", "sha256": sha}}}))
    cached = pathlib.Path(tmp, "cache/teaport-brain_0.1.0_arm64.deb")
    rc, out = _debrun(tmp, f'MF="{mf}"; brain_deb_fetch; orig="$BRAIN_DEB"; brain_deb_keep; test ! -e "$orig" && echo ORIG_GONE')
    assert rc == 0 and "ORIG_GONE" in out and cached.read_text() == "deb 0.1.0\n", out   # the download is not kept twice
    rc, out = _debrun(tmp, f'MF="{mf}"; brain_deb_fetch; brain_deb_keep; echo "ADDED=$BRAIN_DEB_ADDED"')
    assert rc == 0 and "ADDED=0" in out, out   # the second time the cache already had it
    assert not pathlib.Path(tmp, "cache/incoming.partial").exists()
    for how, change in (("its content", 'echo tampered > "$BRAIN_DEB"'),
                        ("its fields", 'echo "Package: other" > "$BRAIN_DEB.fields"')):
        tmp = tempdir("teaport-deb-")
        _deb(pathlib.Path(tmp, "remote/teaport-brain_0.1.0_arm64.deb"), "0.1.0")
        mf = pathlib.Path(tmp, "m.json")
        mf.write_text(json.dumps({"brain": {"deb": {"url": "https://x/teaport-brain_0.1.0_arm64.deb", "sha256": sha}}}))
        rc, out = _debrun(tmp, f'MF="{mf}"; brain_deb_fetch; {change}; brain_deb_keep')
        assert rc != 0 and "nothing was installed" in out, (how, out)
        left = [f for f in os.listdir(pathlib.Path(tmp, "cache")) if not f.endswith(".fields")]   # (stub sidecars)
        assert left == [], (how, left)


def test_the_cache_keeps_the_newest_three_and_the_installed_one():
    tmp = tempdir("teaport-deb-")
    for i, v in enumerate(("1.0", "1.1", "1.2", "1.3", "1.4")):
        _cached(tmp, v, age=i)
    # 1.0 is the oldest and the installed one: it stays beside the newest three.
    rc, out = _debrun(tmp, 'INSTALLED=1.0; brain_deb_prune')
    left = sorted(f for f in os.listdir(pathlib.Path(tmp, "cache")) if f.endswith(".deb"))
    assert rc == 0, out
    assert left == [
        f"teaport-brain_{v}_arm64.deb" for v in ("1.0", "1.2", "1.3", "1.4")], left


SWAP_STUBS = r'''
SWAP_JUDGE="teaport-brain.service" SWAP_ALSO=""
brain_units() { :; }
brain_render_units() { echo RENDER; }
brain_sudoers() { echo SUDOERS; }
sip_brain_restore() { echo SIP_RESTORE; }
sudo_ready() { :; }
brain_restart() { echo "RESTART $*"; n=$(( $(cat "$T/restarts" 2>/dev/null || echo 0) + 1 )); echo $n > "$T/restarts"; FAILED_UNIT=teaport-brain.service; [ "$n" -gt "${RESTARTS_FAIL:-0}" ]; }
'''


def _swap(tmp, installed=None, new="2.0", mode="--auto-rollback", env=None, extra=""):
    """brain_swap of a fresh 2.0 over <installed>, whose package is kept."""
    if installed:
        _cached(tmp, installed)
    new_deb = _cached(tmp, new, age=10)
    script = (f'{SWAP_STUBS}{"INSTALLED=" + installed + chr(10) if installed else ""}'
              f'BRAIN_DEB_NEW={new} BRAIN_DEB=/in/{new}.deb BRAIN_DEB_ADDED=1\n{extra}brain_swap "{new_deb}" {mode}')
    return _debrun(tmp, script, ["brain_swap"], env)


def test_a_swap_installs_renders_restarts_and_keeps_going_when_the_brain_comes_back():
    tmp = tempdir("teaport-swap-")
    rc, out = _swap(tmp, installed="1.0")
    assert rc == 0 and "teaport-brain 1.0 -> 2.0" in out, out
    assert out.index("RENDER") < out.index("SUDOERS") < out.index("RESTART"), out
    assert pathlib.Path(tmp, "apt.log").read_text().count("APT ") == 1
    assert "install -y --reinstall --allow-downgrades" in pathlib.Path(tmp, "apt.log").read_text()


def test_a_brain_that_does_not_come_back_gets_the_previous_package_back():
    tmp = tempdir("teaport-swap-")
    rc, out = _swap(tmp, installed="1.0", env={"RESTARTS_FAIL": "1"})
    assert rc != 0 and "putting 1.0 back" in out and "SIP_RESTORE" in out, out
    assert "rolled back: the brain is running on 1.0 again" in out, out
    # The package that failed is out of the cache, so --rollback cannot pick it later.
    assert "no longer kept" in out and not pathlib.Path(tmp, "cache/teaport-brain_2.0_arm64.deb").exists(), out
    assert pathlib.Path(tmp, "cache/teaport-brain_1.0_arm64.deb").exists()
    apt = pathlib.Path(tmp, "apt.log").read_text().splitlines()
    assert [a.rsplit("/", 1)[-1] for a in apt] == ["teaport-brain_2.0_arm64.deb", "teaport-brain_1.0_arm64.deb"], apt


def test_a_package_whose_postinst_fails_is_a_brain_that_did_not_come_back():
    # postinst restarts the Type=notify brain: a brain that never says ready fails apt.
    tmp = tempdir("teaport-swap-")
    rc, out = _swap(tmp, installed="1.0", env={"APT_FAILS": "teaport-brain_2.0_arm64.deb"})
    assert rc != 0 and "installing" in out and "failed" in out and "putting 1.0 back" in out, out
    apt = pathlib.Path(tmp, "apt.log").read_text().splitlines()
    assert apt[-1].endswith("teaport-brain_1.0_arm64.deb"), apt


def test_with_no_earlier_package_a_failed_swap_says_so_and_does_not_guess():
    tmp = tempdir("teaport-swap-")
    rc, out = _swap(tmp, installed=None, env={"RESTARTS_FAIL": "1"})
    assert rc != 0 and "no earlier package is kept to go back to" in out and "journalctl -u teaport-brain" in out, out
    assert "putting" not in out
    assert pathlib.Path(tmp, "apt.log").read_text().count("APT ") == 1


def test_a_failed_reinstall_of_the_installed_version_keeps_its_one_copy():
    tmp = tempdir("teaport-swap-")
    rc, out = _swap(tmp, installed="2.0", new="2.0", env={"RESTARTS_FAIL": "1"})
    assert rc != 0 and "the reinstall of 2.0" in out and "no longer kept" not in out, out
    # The installed package and the rollback target are one file: it stays, so the next
    # failed upgrade still has something to go back to.
    assert pathlib.Path(tmp, "cache/teaport-brain_2.0_arm64.deb").exists()


def test_apt_that_never_reached_the_package_leaves_the_box_alone():
    tmp = tempdir("teaport-swap-")
    rc, out = _swap(tmp, installed="1.0", env={"APT_LOCKED": "teaport-brain_2.0_arm64.deb"})
    assert rc != 0 and "left the box as it was" in out and "still runs 1.0" in out, out
    assert "RESTART" not in out and "putting" not in out and "SIP_RESTORE" not in out, out
    assert "DPkg::Lock::Timeout" in pathlib.Path(tmp, "apt.log").read_text()
    assert pathlib.Path(tmp, "apt.log").read_text().count("APT ") == 1
    # The package that was never installed is not a rollback target either; 1.0 stays.
    assert not pathlib.Path(tmp, "cache/teaport-brain_2.0_arm64.deb").exists()
    assert pathlib.Path(tmp, "cache/teaport-brain_1.0_arm64.deb").exists()


def test_a_package_kept_before_stays_when_apt_never_reached_it():
    # A rollback target installed again: this run added nothing to the cache, so nothing goes.
    tmp = tempdir("teaport-swap-")
    rc, out = _swap(tmp, installed="1.0", env={"APT_LOCKED": "teaport-brain_2.0_arm64.deb"}, extra="BRAIN_DEB_ADDED=0\n")
    assert rc != 0 and "left the box as it was" in out, out
    assert pathlib.Path(tmp, "cache/teaport-brain_2.0_arm64.deb").exists()


def test_the_first_package_failing_on_a_pre_package_box_says_how_to_go_back_to_its_venv():
    tmp = tempdir("teaport-swap-")
    extra = f'PREFIX="{tmp}/opt"\nmkdir -p "$PREFIX/venvs/r1"; ln -s venvs/r1 "$PREFIX/venv"\n'
    rc, out = _swap(tmp, installed=None, env={"RESTARTS_FAIL": "1"}, extra=extra)
    assert rc != 0 and "no earlier package is kept to go back to" in out, out
    assert f"The pre-package venv (venvs/r1) is still on the box; to run on it again: sudo ln -sfn venvs/r1 {tmp}/opt/venv" in out, out


def test_a_render_that_fails_after_the_install_is_a_failed_swap():
    tmp = tempdir("teaport-swap-")
    rc, out = _swap(tmp, installed="1.0", extra="brain_render_units() { return 1; }\n")
    assert rc != 0 and "installing or configuring" in out and "putting 1.0 back" in out, out


def test_a_swap_by_hand_that_fails_just_fails():
    tmp = tempdir("teaport-swap-")
    rc, out = _swap(tmp, installed="1.0", mode='""', env={"RESTARTS_FAIL": "1"})
    assert rc != 0 and "the brain did not come back on 2.0" in out and "putting" not in out, out
    assert pathlib.Path(tmp, "apt.log").read_text().count("APT ") == 1


def test_a_dry_run_swap_names_the_package_and_installs_nothing():
    tmp = tempdir("teaport-swap-")
    rc, out = _swap(tmp, installed="1.0", extra="DRY_RUN=1\n")
    assert rc == 0 and "[dry-run] apt-get install" in out and "was 1.0" in out, out
    assert not pathlib.Path(tmp, "apt.log").exists()


ROLLBACK_STUBS = r'''
brain_swap() { echo "SWAP $1 [$2]"; }
'''


def test_rolling_back_installs_the_newest_other_package_and_the_next_one_goes_forward_again():
    tmp = tempdir("teaport-rollback-")
    _cached(tmp, "1.0", age=1)
    _cached(tmp, "1.1", age=2)
    _cached(tmp, "1.2", age=3)
    rc, out = _debrun(tmp, f'{ROLLBACK_STUBS}INSTALLED=1.2; main_rollback_brain', ["main_rollback_brain"])
    assert rc == 0 and f"SWAP {tmp}/cache/teaport-brain_1.1_arm64.deb []" in out, out
    assert "brain rolled back to 1.1" in out, out
    # After that rollback 1.1 is installed and 1.2 is the newest other one.
    rc, out = _debrun(tmp, f'{ROLLBACK_STUBS}INSTALLED=1.1; main_rollback_brain', ["main_rollback_brain"])
    assert rc == 0 and f"SWAP {tmp}/cache/teaport-brain_1.2_arm64.deb []" in out, out


def test_a_half_configured_package_is_still_the_one_being_rolled_back_from():
    tmp = tempdir("teaport-rollback-")
    _cached(tmp, "1.0", age=1)
    _cached(tmp, "1.1", age=2)
    rc, out = _debrun(tmp, f'{ROLLBACK_STUBS}STATUS=half-configured INSTALLED=1.1; main_rollback_brain', ["main_rollback_brain"])
    assert rc == 0 and f"SWAP {tmp}/cache/teaport-brain_1.0_arm64.deb []" in out, out
    # And the cache pruning does not drop it as "not installed".
    for i, v in enumerate(("1.2", "1.3", "1.4", "1.5")):
        _cached(tmp, v, age=10 + i)
    rc, out = _debrun(tmp, 'STATUS=half-configured INSTALLED=1.0; brain_deb_prune')
    assert rc == 0 and pathlib.Path(tmp, "cache/teaport-brain_1.0_arm64.deb").exists(), out


def test_rolling_back_with_nothing_kept_or_nothing_installed_says_so():
    tmp = tempdir("teaport-rollback-")
    _cached(tmp, "1.0")
    rc, out = _debrun(tmp, f'{ROLLBACK_STUBS}INSTALLED=1.0; main_rollback_brain', ["main_rollback_brain"])
    assert rc != 0 and "no earlier brain package is kept" in out, out
    rc, out = _debrun(tmp, f'{ROLLBACK_STUBS}main_rollback_brain', ["main_rollback_brain"])
    assert rc != 0 and "is not installed" in out, out


# --- retiring the pre-package CLI and helper ------------------------------------------

def _old_tools(tmp, package_in_place=True, sudoers=True, cli_is_link=True):
    old_lib, old_cli = pathlib.Path(tmp, "uselocal/lib"), pathlib.Path(tmp, "uselocal/bin/teaport")
    (old_lib / "releases/r1").mkdir(parents=True)
    (old_lib / "releases/r1/teaport").write_text("old cli\n")
    (old_lib / "current").symlink_to("releases/r1")
    (old_lib / "config_apply.py").symlink_to("current/config_apply.py")
    (old_lib / "wifi-setup-net").write_text("not ours to remove\n")
    old_cli.parent.mkdir(parents=True)
    if cli_is_link:
        old_cli.symlink_to(old_lib / "current/teaport")
    else:
        old_cli.write_text("#!/bin/sh\n# the operator's own wrapper\n")
    pkg_cli, pkg_helper = pathlib.Path(tmp, "usr/bin/teaport"), pathlib.Path(tmp, "usr/lib/teaport/config_apply.py")
    if package_in_place:
        for f in (pkg_cli, pkg_helper):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("new\n")
            f.chmod(0o755)
    sudoers_file = pathlib.Path(tmp, "sudoers.d/teaport-config")
    sudoers_file.parent.mkdir()
    if sudoers:
        sudoers_file.write_text(f"alice ALL=(root) NOPASSWD: /usr/bin/python3 {pkg_helper} *\n")
    else:
        sudoers_file.write_text("alice ALL=(root) NOPASSWD: /usr/bin/python3 /usr/local/lib/teaport/config_apply.py *\n")
    script = (f'OLD_TOOLS_DIR="{old_lib}"; OLD_TOOLS_CLI="{old_cli}"; SUDOERS_FILE="{sudoers_file}"\n'
              f'brain_retire_old_tools_in() {{ :; }}\n')
    # The function names /usr/bin/teaport and /usr/lib/teaport/config_apply.py: point them
    # into the temp tree by running its text through sed.
    body = _fn("brain_retire_old_tools").replace("/usr/bin/teaport", str(pkg_cli)).replace(
        "/usr/lib/teaport/config_apply.py", str(pkg_helper))
    return old_lib, old_cli, script + body + "brain_retire_old_tools\n"


def test_the_old_cli_and_helper_go_once_the_packages_are_in_place_and_not_before():
    tmp = tempdir("teaport-retire-")
    old_lib, old_cli, script = _old_tools(tmp)
    rc, out = _run(tmp, TOOLS_STUBS + script)
    assert rc == 0 and "retiring the pre-package CLI" in out, out
    assert not os.path.lexists(old_cli) and not os.path.lexists(old_lib / "current")
    assert not os.path.lexists(old_lib / "releases") and not os.path.lexists(old_lib / "config_apply.py")
    assert (old_lib / "wifi-setup-net").exists()   # other files in that directory stay
    # Run again: nothing left, nothing said.
    rc, out = _run(tmp, TOOLS_STUBS + script)
    assert rc == 0 and "retiring" not in out, out
    tmp = tempdir("teaport-retire-")
    old_lib, old_cli, script = _old_tools(tmp, package_in_place=False)
    rc, out = _run(tmp, TOOLS_STUBS + script)
    assert rc == 0 and "WARN the package's CLI and config helper are not in place" in out, out
    assert os.path.lexists(old_cli) and os.path.lexists(old_lib / "releases")


def test_the_old_helper_stays_while_the_sudoers_line_still_names_it_and_a_foreign_cli_stays():
    tmp = tempdir("teaport-retire-")
    old_lib, old_cli, script = _old_tools(tmp, sudoers=False)
    rc, out = _run(tmp, TOOLS_STUBS + script)
    assert rc == 0 and "does not name the package's config helper" in out and "retiring" not in out, out
    assert os.path.lexists(old_cli) and os.path.lexists(old_lib / "releases")
    # /usr/local/bin/teaport that is not the old installer's link is somebody's own.
    tmp = tempdir("teaport-retire-")
    old_lib, old_cli, script = _old_tools(tmp, cli_is_link=False)
    rc, out = _run(tmp, TOOLS_STUBS + script)
    assert rc == 0 and "is not the pre-package CLI's link" in out, out
    assert old_cli.read_text().startswith("#!/bin/sh") and not os.path.lexists(old_lib / "releases")


TOOLS_STUBS = r'''
run()  { "$@"; }
SUDO() { printf 'SUDO %s\n' "$*" >> "$T/sudo.log"; "$@"; }
systemctl() { [ "$1" = is-active ] && [ "${BRAIN_STOPPED:-0}" = 0 ]; }   # the brain unit is running
'''


# --- retiring the pre-package venvs ----------------------------------------------------

def _old_venvs(tmp, package_in_place=True, venv_is_dir=False):
    prefix = pathlib.Path(tmp, "opt/teaport")
    (prefix / "venvs/20260901-aaa/bin").mkdir(parents=True)
    (prefix / "venvs/20260901-aaa/bin/python").write_text("old\n")
    (prefix / "venv.previous").symlink_to("venvs/20260901-aaa")
    (prefix / "venv.pre-uv/bin").mkdir(parents=True)
    (prefix / "engine").mkdir()
    (prefix / "engine/teagram-engine").write_text("not ours to remove\n")
    if venv_is_dir:
        (prefix / "venv").mkdir()
    else:
        (prefix / "venv").symlink_to("brain")
    if package_in_place:
        (prefix / "brain/bin").mkdir(parents=True)
        (prefix / "brain/bin/teaport-brain").write_text("new\n")
        (prefix / "brain/bin/teaport-brain").chmod(0o755)
    return prefix, f'PREFIX="{prefix}"\n' + _fn("brain_retire_old_venvs") + "brain_retire_old_venvs\n"


def test_the_old_venvs_go_once_the_packages_brain_is_in_place_and_not_before():
    tmp = tempdir("teaport-retire-venvs-")
    prefix, script = _old_venvs(tmp)
    rc, out = _run(tmp, TOOLS_STUBS + script)
    assert rc == 0 and "removing the pre-package brain venvs" in out, out
    for gone in ("venvs", "venv.previous", "venv.pre-uv"):
        assert not os.path.lexists(prefix / gone), gone
    assert os.path.islink(prefix / "venv") and (prefix / "brain/bin/teaport-brain").exists()   # the package's
    assert (prefix / "engine/teagram-engine").exists()   # nothing else under the prefix
    rc, out = _run(tmp, TOOLS_STUBS + script)   # again: nothing left, nothing said
    assert rc == 0 and "removing" not in out, out
    tmp = tempdir("teaport-retire-venvs-")
    prefix, script = _old_venvs(tmp, package_in_place=False)
    rc, out = _run(tmp, TOOLS_STUBS + script)
    assert rc == 0 and "WARN the package's brain is not in place" in out, out
    assert os.path.lexists(prefix / "venvs") and os.path.lexists(prefix / "venv.pre-uv")


def test_the_old_venvs_stay_while_the_brain_is_not_running_and_on_a_dry_run():
    # A stopped brain (brain_swap restarts nothing then) has not shown the package works.
    tmp = tempdir("teaport-retire-venvs-")
    prefix, script = _old_venvs(tmp)
    rc, out = _run(tmp, TOOLS_STUBS + "BRAIN_STOPPED=1\n" + script)
    assert rc == 0 and "WARN teaport-brain is not running" in out and "removing" not in out, out
    assert os.path.lexists(prefix / "venvs") and os.path.lexists(prefix / "venv.pre-uv")
    # and once it runs, the next run takes them
    rc, out = _run(tmp, TOOLS_STUBS + script)
    assert rc == 0 and "removing the pre-package brain venvs" in out, out
    assert not os.path.lexists(prefix / "venvs")
    # A dry run says what it would do, not that the package is missing, and removes nothing.
    tmp = tempdir("teaport-retire-venvs-")
    prefix, script = _old_venvs(tmp, package_in_place=False)
    rc, out = _run(tmp, TOOLS_STUBS + "DRY_RUN=1\n" + script)
    assert rc == 0 and "[dry-run] would remove the pre-package brain venvs" in out and "WARN" not in out, out
    assert os.path.lexists(prefix / "venvs")


def test_a_venv_directory_where_the_link_belongs_is_named_and_left():
    tmp = tempdir("teaport-retire-venvs-")
    prefix, script = _old_venvs(tmp, venv_is_dir=True)
    rc, out = _run(tmp, TOOLS_STUBS + script)
    assert rc == 0 and "is a directory, not the package's link" in out, out
    assert os.path.isdir(prefix / "venv") and not os.path.islink(prefix / "venv")


# --- the package's own scripts ---------------------------------------------------------

DEB_DIR = INSTALL.parent / "packaging/deb"


def test_the_maintainer_scripts_parse_and_the_sudoers_writer_refuses_a_bad_user_name():
    for name in ("postinst", "postrm", "sudoers-sync"):
        assert subprocess.run(["sh", "-n", str(DEB_DIR / name)]).returncode == 0, name
    # A name that is not a plain login is refused before anything is written, and root
    # (or no unit at all) needs no line.
    for bad in ("bad user", "-oops", "a;b", "x$y"):
        p = subprocess.run(["sh", str(DEB_DIR / "sudoers-sync"), bad], capture_output=True, text=True)
        assert p.returncode == 1 and "refusing the user name" in p.stderr, (bad, p.stderr)
    assert subprocess.run(["sh", str(DEB_DIR / "sudoers-sync"), "root"]).returncode == 0


def test_postinst_only_configures_and_the_package_ships_what_it_runs():
    p = subprocess.run(["sh", str(DEB_DIR / "postinst"), "abort-upgrade"], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr   # anything but `configure` does nothing
    recipe = (INSTALL.parent / "packaging/nfpm.yaml").read_text()
    assert "postinstall: packaging/deb/postinst" in recipe and "postremove: packaging/deb/postrm" in recipe
    assert "\n  - sudo\n" in recipe   # visudo checks the sudoers line
    # The license material the package distributes with the LGPL parts of its tree.
    for src, dst in (("packaging/deb/copyright", "/usr/share/doc/teaport-brain/copyright"),
                     ("THIRD_PARTY_LICENSES.md", "/usr/share/doc/teaport-brain/THIRD_PARTY_LICENSES.md")):
        assert f"- src: {src}\n    dst: {dst}\n" in recipe, src
        assert (INSTALL.parent / src).is_file(), src
    copyright_text = (INSTALL.parent / "packaging/deb/copyright").read_text()
    assert copyright_text.startswith("Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/")
    assert "Permission is hereby granted" in copyright_text and "LGPL-2.1-or-later" in copyright_text
    licenses = (INSTALL.parent / "THIRD_PARTY_LICENSES.md").read_text()
    assert "stefano@teaspoon.tech" in licenses and "never frozen or bundled into a single closed binary" in licenses
    assert "dst: /usr/lib/teaport/sudoers-sync" in recipe and "dst: /usr/lib/teaport/config_apply.py" in recipe
    # The unit that restarts it waits for it.
    unit = (INSTALL.parent / "systemd/teaport-brain.service.in").read_text()
    assert "Type=notify" in unit and "ExecStart=@PREFIX@/brain/bin/teaport-brain" in unit



def _script(tmp, name, replace):
    """A maintainer script with its absolute paths moved into <tmp>."""
    text = (DEB_DIR / name).read_text()
    for old, new in replace:
        assert old in text, (name, old)
        text = text.replace(old, new)
    p = pathlib.Path(tmp, name)
    p.write_text(text)
    return p


def test_postinst_links_the_old_venv_path_and_a_sudoers_failure_does_not_fail_the_package():
    tmp = tempdir("teaport-postinst-")
    opt = pathlib.Path(tmp, "opt/teaport")
    opt.mkdir(parents=True)
    failing = pathlib.Path(tmp, "sudoers-sync")
    failing.write_text("#!/bin/sh\nexit 1\n")
    failing.chmod(0o755)
    old_cli = pathlib.Path(tmp, "usr-local-bin-teaport")
    script = _script(tmp, "postinst", [("/usr/lib/teaport/sudoers-sync", str(failing)),
                                       ("/usr/local/bin/teaport", str(old_cli)),
                                       ("/opt/teaport", str(opt)),
                                       ("/run/systemd/system", str(pathlib.Path(tmp, "no-systemd")))])
    p = subprocess.run(["sh", str(script), "configure"], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "sudoers line was not written" in p.stderr
    assert "shadows" not in p.stderr   # no old CLI: nothing to say
    old_cli.write_text("#!/bin/sh\n")
    p = subprocess.run(["sh", str(script), "configure"], capture_output=True, text=True)
    assert p.returncode == 0 and "shadows the package's /usr/bin/teaport" in p.stderr, p.stderr
    old_cli.unlink()
    assert os.path.islink(opt / "venv") and os.readlink(opt / "venv") == str(opt / "brain")
    # Anything but `configure` does nothing, and a real directory where the link goes stays.
    (opt / "venv").unlink()
    (opt / "venv").mkdir()
    p = subprocess.run(["sh", str(script), "configure"], capture_output=True, text=True)
    assert p.returncode == 0 and (opt / "venv").is_dir() and not os.path.islink(opt / "venv")


def test_postrm_takes_the_link_on_remove_and_the_sudoers_line_on_purge_and_nothing_on_upgrade():
    tmp = tempdir("teaport-postrm-")
    opt = pathlib.Path(tmp, "opt/teaport")
    opt.mkdir(parents=True)
    sudoers = pathlib.Path(tmp, "teaport-config")
    script = _script(tmp, "postrm", [("/opt/teaport", str(opt)), ("/etc/sudoers.d/teaport-config", str(sudoers))])

    def run(action, link_to="brain"):
        if os.path.lexists(opt / "venv"):
            (opt / "venv").unlink()
        os.symlink(str(opt / link_to), opt / "venv")
        sudoers.write_text("line\n")
        assert subprocess.run(["sh", str(script), action]).returncode == 0
        return os.path.lexists(opt / "venv"), sudoers.exists()
    assert run("upgrade") == (True, True)
    assert run("remove") == (False, True)
    assert run("purge") == (False, False)
    # A link that is not the package's own is not ours to remove.
    assert run("remove", link_to="venvs-x") == (True, True)


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
