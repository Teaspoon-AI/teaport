#
# When memory runs out, the voice path goes last: the engine, the brain, the SIP gateway
# and the local audio bridge carry OOMScoreAdjust=-500, the host OpenClaw gateway's JS
# heap is capped (install.sh cap_openclaw_heap) instead of OpenClaw's half-the-RAM, and
# the cold `openclaw agent --local` the brain spawns does not inherit the brain's
# protection (openclaw_client._not_voice).
#
# cap_openclaw_heap is taken out of install.sh as it is and run by bash against a unit
# file in a temp dir, with systemctl stubbed. Nothing outside a temp dir is touched.
#
# Run: python test_voice_oom_priority.py   (or via pytest test_suite.py)
#
import os
import pathlib
import re
import subprocess
import sys

import appliance
from tempdirs import tempdir

ROOT = pathlib.Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"
UNITS = ROOT / "systemd"

VOICE = ("teaport-engine", "teaport-brain", "teaport-sip", "teaport-local-audio")

GENERATED = """[Unit]
Description=OpenClaw Gateway

[Service]
ExecStart=/usr/bin/node {flag}/usr/lib/node_modules/openclaw/dist/index.js gateway --port 18789
Restart=always
Environment=NODE_OPTIONS=
# a note mentioning --max-old-space-size=3773 stays as it is
"""

STUBS = r'''
set -euo pipefail
DRY_RUN=0
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
systemctl() {
  case "$*" in
    "--user show -p FragmentPath --value openclaw-gateway.service") printf '%s\n' "${UNIT:-}" ;;
    "--user daemon-reload") echo RELOAD ;;
    *) echo "unexpected systemctl $*" >&2; return 1 ;;
  esac
}
'''


def _fn(name: str) -> str:
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _heap_line() -> str:
    return next(ln for ln in INSTALL.read_text().splitlines()
                if ln.startswith("OPENCLAW_HEAP_MIB=")) + "\n"


def _cap(unit_text: str | None, env=None):
    """(rc, output, the unit afterwards) for one cap_openclaw_heap run."""
    tmp = tempdir("teaport-oom-")
    unit = pathlib.Path(tmp, "openclaw-gateway.service")
    if unit_text is not None:
        unit.write_text(unit_text)
    body = STUBS + _heap_line() + _fn("cap_openclaw_heap") + "cap_openclaw_heap\n"
    e = {k: v for k, v in os.environ.items() if k != "TEAPORT_OPENCLAW_HEAP_MIB"}
    e.update(UNIT=str(unit) if unit_text is not None else "", **(env or {}))
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=e)
    return p.returncode, p.stdout + p.stderr, (unit.read_text() if unit.exists() else None)


def _exec_start(text: str) -> str:
    return next(ln for ln in text.splitlines() if ln.startswith("ExecStart="))


def test_the_voice_units_go_last_and_the_others_do_not():
    for path in sorted(UNITS.glob("*.service.in")):
        name = path.name.removesuffix(".service.in")
        adj = re.findall(r"^OOMScoreAdjust=(-?\d+)$", path.read_text(), re.M)
        if name in VOICE:
            assert adj == ["-500"], (name, adj)
        else:
            assert adj == [], (name, adj)


def test_openclaws_heap_flag_is_replaced_and_the_unit_reloaded():
    rc, out, unit = _cap(GENERATED.format(flag="--max-old-space-size=3773 "))
    assert rc == 0 and "RELOAD" in out, out
    assert _exec_start(unit) == ("ExecStart=/usr/bin/node --max-old-space-size=1536 "
                                 "/usr/lib/node_modules/openclaw/dist/index.js gateway --port 18789")
    assert "mentioning --max-old-space-size=3773 stays" in unit, "only ExecStart is edited"


def test_a_unit_already_capped_is_left_alone():
    text = GENERATED.format(flag="--max-old-space-size=1536 ")
    rc, out, unit = _cap(text)
    assert rc == 1 and "RELOAD" not in out, out
    assert unit == text


def test_a_unit_with_no_heap_flag_gets_one_ahead_of_the_script():
    rc, out, unit = _cap(GENERATED.format(flag=""))
    assert rc == 0 and "RELOAD" in out, out
    assert _exec_start(unit).startswith(
        "ExecStart=/usr/bin/node --max-old-space-size=1536 /usr/lib/node_modules/openclaw/"), unit


def test_the_limit_can_be_set_at_install_time():
    rc, out, unit = _cap(GENERATED.format(flag="--max-old-space-size=3773 "),
                         env={"TEAPORT_OPENCLAW_HEAP_MIB": "2048"})
    assert rc == 0, out
    assert " --max-old-space-size=2048 " in _exec_start(unit), unit


def test_no_unit_warns_and_changes_nothing():
    rc, out, _ = _cap(None)
    assert rc == 1 and "WARN no openclaw-gateway unit file" in out and "RELOAD" not in out, out


def test_the_cold_cli_does_not_inherit_the_brains_protection():
    # A brain process whose score is raised (it cannot be lowered without privilege, so
    # +300 stands in for the unit's -500): the CLI it spawns must come out at the default.
    if not os.path.exists("/proc/self/oom_score_adj"):
        raise appliance.Unavailable("no /proc/<pid>/oom_score_adj (not Linux)")
    tmp = tempdir("teaport-oom-")
    cli = pathlib.Path(tmp, "openclaw")
    # It reports its score once the parent has had its moment to change it.
    cli.write_text("#!/bin/sh\nsleep 0.3\ncat /proc/self/oom_score_adj\n")
    cli.chmod(0o755)
    brain = pathlib.Path(__file__).resolve().parents[1]
    code = (
        "import asyncio, teaport_brain.openclaw_client as oc\n"
        "open('/proc/self/oom_score_adj', 'w').write('300')\n"
        f"oc.OPENCLAW_BIN = {str(cli)!r}\n"
        "rc, out, err = asyncio.run(oc._run_openclaw([], timeout=5, capture=True))\n"
        "print(open('/proc/self/oom_score_adj').read().strip(), out.decode().strip())\n"
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(brain)}, timeout=30)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == ["300", "0"], (p.stdout, p.stderr)


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
