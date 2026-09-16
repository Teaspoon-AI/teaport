"""The privileged half of the config UI: write an env file, restart a unit.

/etc/teaport/*.env are root:<run-user> 0640 (install.sh write_env), so the brain
— which runs as the run user — can read them but not write them, and a restart
is systemctl. Rather than widen the brain's privileges, config_ui.py shells out
to THIS module under sudo for exactly those two actions, and install.sh installs
the one sudoers line that allows it:

    <run-user> ALL=(root) NOPASSWD: /usr/bin/python3 /usr/local/lib/teaport/config_apply.py *

install.sh copies this file to /usr/local/lib/teaport/ as root:root 0755 — NOT
the copy inside $PREFIX/venv. The venv, its bin/python and every module in it
are owned by the run user (install.sh chowns the prefix so uv can populate it),
so a sudoers line that trusted the venv's interpreter or the venv's copy of
this module would let the run user edit either and run anything as root. The
system python and a root-owned script are the two things that user cannot
touch. Everything after the script path is argv to THIS code, which only ever
touches the files and units named below. sudo's env_reset strips PYTHONPATH,
and python puts the script's own (root-owned) directory first on sys.path.

    python3 config_apply.py write brain.env   < new-content
    python3 config_apply.py restart teaport-brain

write: keeps a .bak beside the file (a bounded ring of BACKUPS, newest last —
the files carry tokens, so they must not pile up in /etc), writes a temp file
in the same directory with the original owner and mode, and renames it into
place, so a crash mid-write leaves the old file intact. The content is checked
line-by-line: only KEY=value, comments and blank lines — a stray shell line
cannot reach an EnvironmentFile through here.

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
FILES = ("engine.env", "brain.env", "bridge.env")
UNITS = ("teaport-engine", "teaport-brain", "teaport-sip", "teaport-sip-brain",
         "teaport-discord-bridge")
MAX_BYTES = 64 * 1024
BACKUPS = 5
# What a line may be: blank, a comment, or KEY=value. The KEY=value shape MUST be
# exactly what config_ui._KV_RE accepts (leading whitespace, an `export `, spaces
# around `=`): the UI preserves every line it did not touch verbatim, so a line
# its parser reads but this check refuses would make the whole file unsaveable.
LINE_RE = re.compile(r"^(?:\s*(?:#.*)?|\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=.*)$")


def write(name: str, content: str) -> str:
    if name not in FILES:
        raise SystemExit(f"refusing to write {name!r}: not one of {FILES}")
    if len(content.encode()) > MAX_BYTES:
        raise SystemExit("content too large")
    for i, line in enumerate(content.splitlines(), 1):
        if not LINE_RE.match(line):
            raise SystemExit(f"line {i} is not KEY=value or a comment: {line[:60]!r}")
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
    each store separately) get distinct names, and only the newest BACKUPS
    survive."""
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
    prefix = os.path.basename(path) + ".bak-"
    older = sorted(f for f in os.listdir(os.path.dirname(path)) if f.startswith(prefix))
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


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "write":
        print(write(argv[1], sys.stdin.read()))
        return 0
    if len(argv) == 2 and argv[0] == "restart":
        restart(argv[1])
        return 0
    print(__doc__.split("\n\n")[0], file=sys.stderr)
    print("usage: write <engine.env|brain.env|bridge.env> < content | restart <unit>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
