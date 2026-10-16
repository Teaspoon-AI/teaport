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
die()  { printf 'DIE %s\n' "$*"; exit 1; }
systemctl() {
  case "$*" in
    "--user show -p FragmentPath --value openclaw-gateway.service") printf '%s\n' "${UNIT:-}" ;;
    "--user daemon-reload") echo RELOAD; [ -z "${RELOAD_FAILS:-}" ] ;;
    *) echo "unexpected systemctl $*" >&2; return 1 ;;
  esac
}
'''


def _fn(name: str) -> str:
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _heap_lines() -> str:
    """The limit's assignment and the check after it, as install.sh runs them at the top."""
    lines = INSTALL.read_text().splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("OPENCLAW_HEAP_MIB="))
    check = next(ln for ln in lines[i:] if ln.startswith("[[ $OPENCLAW_HEAP_MIB"))
    return lines[i] + "\n" + check + "\n"


def _cap(unit_text: str | None, env=None):
    """(rc, output, the unit afterwards) for one cap_openclaw_heap run, called as
    agent_openclaw calls it: inside an && / || list, where set -e does not reach. The
    output ends CHANGED or KEPT, the return code the caller acts on."""
    tmp = tempdir("teaport-oom-")
    unit = pathlib.Path(tmp, "openclaw-gateway.service")
    if unit_text is not None:
        unit.write_text(unit_text)
    body = (STUBS + _heap_lines() + _fn("cap_openclaw_heap")
            + "cap_openclaw_heap && echo CHANGED || echo KEPT\n")
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
    assert rc == 0 and "RELOAD" in out and out.endswith("CHANGED\n"), out
    assert _exec_start(unit) == ("ExecStart=/usr/bin/node --max-old-space-size=1536 "
                                 "/usr/lib/node_modules/openclaw/dist/index.js gateway --port 18789")
    assert "mentioning --max-old-space-size=3773 stays" in unit, "only ExecStart is edited"


def test_a_unit_already_capped_is_left_alone():
    text = GENERATED.format(flag="--max-old-space-size=1536 ")
    rc, out, unit = _cap(text)
    assert out.endswith("KEPT\n") and "RELOAD" not in out, out
    assert unit == text


def test_a_unit_with_no_heap_flag_gets_one_ahead_of_the_script():
    rc, out, unit = _cap(GENERATED.format(flag=""))
    assert rc == 0 and "RELOAD" in out and out.endswith("CHANGED\n"), out
    assert _exec_start(unit).startswith(
        "ExecStart=/usr/bin/node --max-old-space-size=1536 /usr/lib/node_modules/openclaw/"), unit


def test_the_limit_can_be_set_at_install_time():
    rc, out, unit = _cap(GENERATED.format(flag="--max-old-space-size=3773 "),
                         env={"TEAPORT_OPENCLAW_HEAP_MIB": "2048"})
    assert out.endswith("CHANGED\n"), out
    assert " --max-old-space-size=2048 " in _exec_start(unit), unit


def test_a_wrapper_or_bun_unit_is_left_alone():
    # OpenClaw leaves the heap flag out when a wrapper or bun runs the gateway: a flag after
    # the first word would be the wrapper's argument, and the gateway would crash-loop.
    for first in ("/opt/wrap/openclaw-wrapper", "/usr/local/bin/bun", '"/opt/node dir/node"'):
        text = GENERATED.format(flag="").replace("/usr/bin/node", first)
        rc, out, unit = _cap(text)
        assert out.endswith("KEPT\n") and "does not run node directly" in out and "RELOAD" not in out, (first, out)
        assert unit == text, first


def test_a_heap_value_node_would_refuse_stops_the_install():
    for bad in ("2G", "1536MiB", "0", "15/36", ""):
        text = GENERATED.format(flag="--max-old-space-size=3773 ")
        rc, out, unit = _cap(text, env={"TEAPORT_OPENCLAW_HEAP_MIB": bad})
        if bad == "":
            # Empty is unset: the default.
            assert out.endswith("CHANGED\n") and "=1536 " in _exec_start(unit), out
            continue
        # Stopped at the top, before the function runs.
        assert rc == 1 and "DIE TEAPORT_OPENCLAW_HEAP_MIB must be a whole number" in out, (bad, out)
        assert "CHANGED" not in out and "KEPT" not in out, (bad, out)
        assert unit == text, bad


def test_a_failed_reload_is_not_reported_as_done():
    # OpenClaw rewrites the unit from what systemd has loaded: an edit never loaded would
    # be lost, so the caller must not hear it worked.
    rc, out, _ = _cap(GENERATED.format(flag="--max-old-space-size=3773 "), env={"RELOAD_FAILS": "1"})
    assert out.endswith("KEPT\n") and "daemon-reload failed" in out and "heap limit ->" not in out, out


def test_no_unit_warns_and_changes_nothing():
    rc, out, _ = _cap(None)
    assert out.endswith("KEPT\n") and "WARN no openclaw-gateway unit file" in out and "RELOAD" not in out, out


def test_the_cold_cli_does_not_inherit_the_brains_protection():
    # A brain process whose score is raised (it cannot be lowered without privilege, so
    # +300 stands in for the unit's -500): the CLI it spawns must come out at the default.
    if not os.path.exists("/proc/self/oom_score_adj"):
        raise appliance.Unavailable("no /proc/<pid>/oom_score_adj (not Linux)")
    # A session started above 0 by a privileged hand (systemd's user@.service sets 100)
    # cannot come back down to 0 at all: nothing to test there.
    probe = subprocess.run(["sh", "-c", "echo 300 > /proc/self/oom_score_adj && "
                            "echo 0 > /proc/self/oom_score_adj"], capture_output=True)
    if probe.returncode != 0:
        raise appliance.Unavailable("this session cannot lower its OOM score back to 0")
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
