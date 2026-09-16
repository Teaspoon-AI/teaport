"""The privileged half of the config UI: write an env file, restart a unit.

/etc/teaport/*.env are root:<run-user> 0640 (install.sh write_env), so the brain
— which runs as the run user — can read them but not write them, and a restart
is systemctl. Rather than widen the brain's privileges, config_ui.py shells out
to THIS module under sudo for exactly those two actions, and install.sh installs
the one sudoers line that allows it:

    <run-user> ALL=(root) NOPASSWD: <prefix>/venv/bin/python -m teaport_brain.config_apply *

Everything after the module name is argv to THIS code, which only ever touches
the files and units named below. sudo's env_reset strips PYTHONPATH, so the
module resolving is the installed one, not a checkout on the caller's path.

    python -m teaport_brain.config_apply write brain.env   < new-content
    python -m teaport_brain.config_apply restart teaport-brain

write: keeps a timestamped .bak beside the file (the way operators already do
by hand on the box), writes a temp file in the same directory with the original
owner and mode, and renames it into place, so a crash mid-write leaves the old
file intact. The content is checked line-by-line: only KEY=value, comments and
blank lines — a stray shell line cannot reach an EnvironmentFile through here.

restart: `systemd-run --on-active=2 systemctl restart <unit>`. Deferred and
detached because the brain restarting ITSELF from a request handler would be
killed before the HTTP response left the socket; two seconds is enough for the
reply, and the transient timer unit survives the brain's exit.

Stdlib only, never imports the rest of the package: it runs as root.
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
LINE_RE = re.compile(r"^(?:\s*(?:#.*)?|[A-Za-z_][A-Za-z0-9_]*=.*)$")


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
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        os.chmod(backup, mode)
        os.chown(backup, uid, gid)
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
