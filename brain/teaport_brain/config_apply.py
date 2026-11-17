"""The privileged half of the config UI: write an env file, restart a unit.

/etc/teaport/*.env are root:<run-user> 0640 (install.sh write_env), so the brain
— which runs as the run user — can read them but not write them, and a restart
is systemctl. Rather than widen the brain's privileges, config_ui.py shells out
to THIS module under sudo for exactly those two actions, and the teaport-brain
package installs the one sudoers line that allows it (packaging/deb/sudoers-sync):

    <run-user> ALL=(root) NOPASSWD: /usr/bin/python3 /usr/lib/teaport/config_apply.py *

The package ships this file as /usr/lib/teaport/config_apply.py, root:root 0755,
so the brain and the line it runs under always come from the same package
version. The venv under /opt/teaport/brain is root's too, but the line still
names the system python and this one script: a venv is a large tree of code that
is not meant to run as root, and the package's own files are the only ones a
rollback is guaranteed to put back together. Everything after the script path
is argv to THIS code, which only ever touches the files and units named below.
sudo's env_reset strips PYTHONPATH, and python puts the script's own root-owned
directory first on sys.path.

    python3 config_apply.py write brain.env   < new-content
    python3 config_apply.py restart teaport-brain
    python3 config_apply.py stop teaport-wifi-setup

The brain (wifi_voice.py) uses restart to START teaport-wifi-setup, which is a
stopped on-demand unit (restart starts a stopped unit), and stop to cancel it:
only that unit is STOPPABLE. stop also cancels a start restart() has scheduled
and not yet run (its two-second timer).

write: keeps a .bak beside the file (a bounded ring of BACKUPS, newest last —
the files carry tokens, so they must not pile up in /etc), writes a temp file
in the same directory with the original owner and mode, and renames it into
place, so a crash mid-write leaves the old file intact. The content is checked
line-by-line: only KEY=value, comments and blank lines — a stray shell line
cannot reach an EnvironmentFile through here — and no key that changes how a
program loads code (LD_PRELOAD, PYTHONPATH, PATH, BASH_ENV and their kind,
UNSAFE_KEY). These files are the environment of units that run as the run user,
on whose behalf this writes, so such a key gains that user nothing; it is refused
all the same, in case a command that runs as root ever loads one of them. None
does (teaport-wifi-setup's root side is its own unit, teaport-wifi-setup-net, with
no environment file; tests/test_wifi_root_env.py checks every unit). Whatever a
value says is the run user's choice: install.sh, which reads a few back as root
(the front door's name), checks each before it uses it.

restart: `systemd-run --on-active=2 systemctl restart <unit>`. Deferred and
detached because the brain restarting ITSELF from a request handler would be
killed before the HTTP response left the socket; two seconds is enough for the
reply, and the transient timer unit survives the brain's exit.

Stdlib only, never imports the rest of the package: it runs as root, from
outside the package, on whatever python3 the OS ships (3.8+).
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
import time

ETC = "/etc/teaport"
FILES = ("engine.env", "brain.env", "bridge.env", "local-audio.env")
UNITS = ("teaport-engine", "teaport-brain", "teaport-sip",
         "teaport-discord-bridge", "teaport-local-audio", "teaport-wifi-setup")
# Units the brain may also stop: Wi-Fi setup, which the user can cancel by voice.
STOPPABLE = ("teaport-wifi-setup",)
MAX_BYTES = 64 * 1024
BACKUPS = 5
# Only backups THIS code named are ever pruned. Operators keep their own beside
# them (brain.env.bak-gptoss, brain.env.bak-20260901-154338-stopsecs on the dev
# box) and those are not ours to rotate out.
BACKUP_RE = re.compile(r"\.bak-\d{8}-\d{6}(-\d+)?$")
# What a line may be: blank, a comment, or KEY=value. The KEY=value shape MUST be
# exactly what settings.ENV_LINE accepts (leading whitespace, an `export `, spaces
# around `=`): the UI preserves every line it did not touch verbatim, so a line
# its parser reads but this check refuses would make the whole file unsaveable.
LINE_RE = re.compile(r"^(?:\s*(?:#.*)?|\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=.*)$")
KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
# Variables that make a program load or run other code: the dynamic loader's, the
# interpreters' and the shells'. No teaport setting is one of them.
UNSAFE_KEY = re.compile(r"^(LD_\w*|GCONV_PATH|LOCPATH|PATH|IFS|BASH_ENV|ENV|SHELLOPTS|BASHOPTS"
                        r"|PYTHONPATH|PYTHONHOME|PYTHONSTARTUP|PYTHONUSERBASE|PYTHONEXECUTABLE"
                        r"|PYTHONWARNINGS|NODE_OPTIONS|NODE_PATH|PERL5OPT|PERL5LIB|PERLLIB|RUBYOPT"
                        r"|RUBYLIB|XTABLES_LIBDIR|GIO_MODULE_DIR|GIO_EXTRA_MODULES|OPENSSL_CONF"
                        r"|OPENSSL_MODULES|OPENSSL_ENGINES)$")


def write(name: str, content: str) -> str:
    if name not in FILES:
        raise SystemExit(f"refusing to write {name!r}: not one of {FILES}")
    if len(content.encode()) > MAX_BYTES:
        raise SystemExit("content too large")
    for i, line in enumerate(content.splitlines(), 1):
        if not LINE_RE.match(line):
            raise SystemExit(f"line {i} is not KEY=value or a comment: {line[:60]!r}")
        key = KEY_RE.match(line)
        if key and UNSAFE_KEY.match(key.group(1)):
            raise SystemExit(f"line {i}: {key.group(1)} changes how programs load code; "
                             "it is not a teaport setting and is not written here")
    if content and not content.endswith("\n"):
        content += "\n"
    path = os.path.join(ETC, name)
    mode, uid, gid = 0o640, 0, int(os.environ.get("SUDO_GID", "0"))
    if os.path.exists(path):
        st = os.stat(path)
        mode, uid, gid = stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid
        _backup(path, mode, uid, gid)
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=ETC)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.chown(tmp, uid, gid)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return path


def _backup(path: str, mode: int, uid: int, gid: int) -> None:
    """Copy `path` to path.bak-<stamp>; two saves in one second (the page PUTs
    each store separately) get distinct names, and only the newest BACKUPS of
    the ones with that exact shape survive."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak-{stamp}"
    n = 1
    while os.path.exists(backup):
        n += 1
        backup = f"{path}.bak-{stamp}-{n}"
    with open(path, "rb") as src, open(backup, "wb") as dst:
        dst.write(src.read())
    os.chmod(backup, mode)
    os.chown(backup, uid, gid)
    base = os.path.basename(path)
    older = sorted(f for f in os.listdir(os.path.dirname(path))
                   if f.startswith(base + ".bak-") and BACKUP_RE.search(f))
    for name in older[:-BACKUPS]:
        os.unlink(os.path.join(os.path.dirname(path), name))


def restart(unit: str) -> None:
    if unit not in UNITS:
        raise SystemExit(f"refusing to restart {unit!r}: not one of {UNITS}")
    subprocess.run(
        ["systemd-run", "--quiet", "--on-active=2", "--timer-property=AccuracySec=100ms",
         "--unit", f"teaport-config-restart-{unit}-{int(time.time())}",
         "systemctl", "restart", unit],
        check=True,
    )


def _restart_timer(unit: str) -> str:
    """The glob that names restart()'s transient timers for `unit`."""
    return f"teaport-config-restart-{unit}-*.timer"


def stop(unit: str) -> None:
    """Stop `unit`, and a start of it restart() has scheduled but not yet run: a "cancel"
    within restart's two seconds would otherwise stop nothing, and the start would come
    after it. systemctl expands the glob itself (no shell), among loaded units only."""
    if unit not in STOPPABLE:
        raise SystemExit(f"refusing to stop {unit!r}: not one of {STOPPABLE}")
    subprocess.run(["systemctl", "stop", "--no-block", "--", _restart_timer(unit), unit], check=True)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "write":
        print(write(argv[1], sys.stdin.read()))
        return 0
    if len(argv) == 2 and argv[0] == "restart":
        restart(argv[1])
        return 0
    if len(argv) == 2 and argv[0] == "stop":
        stop(argv[1])
        return 0
    print(__doc__.split("\n\n")[0], file=sys.stderr)
    print("usage: write <engine.env|brain.env|bridge.env|local-audio.env> < content | restart <unit> | stop <unit>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
