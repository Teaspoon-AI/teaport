#!/usr/bin/env python3
# test_install_engine_env.py — the engine memory reserve install.sh writes (#264).
#
# phase_services writes KOKORO_RESERVE_FPT into engine.env on every full run: 8 with an
# agent resident (a host OpenClaw gateway or a NemoClaw sandbox), 12 voice-only. It wrote
# 6 with an agent until 2026-11, and every full install put the appliance's 8 back to 6,
# restored by hand each time. Unset, the engine's own default (50) OOMs an 8 GB box, so
# the line has to be there either way.
#
# phase_services is taken out of install.sh as it is, with write_env and read_env_file,
# and run by bash with the commands around it stubbed (sudo writes the file itself,
# systemctl and the units are no-ops), against a temp dir. Nothing outside it is touched.
#
# Run: python test_install_engine_env.py   (or via pytest test_suite.py)
#
import os
import pathlib
import subprocess

import appliance
from tempdirs import tempdir

INSTALL = pathlib.Path(__file__).resolve().parents[2] / "install.sh"

STUBS = r'''
set -euo pipefail
DRY_RUN=0 ETC="$T/etc" SECRETS="$T/secrets" RUN_USER=nobody SANDBOX="${SANDBOX:-}"
ENGINE_PORT=8000 BRAIN_PORT=7861 GATEWAY_PORT=18789 SETTINGS_PORT=8443
FRONTDOOR_HOST=teaport.local FRONTDOOR_CA_NAME="teaport CA" TLS_CERT=""
LLM_URL_SEED="?" LLM_BASE_URL=http://127.0.0.1:8080/v1 LLM_MODEL_SEED="?" LLM_MODEL=local
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
die()  { printf 'DIE %s\n' "$*"; exit 1; }
resolve_gateway_token() { GATEWAY_TOKEN=tok; }
render_unit() { :; }
brain_deb_prune() { :; }
brain_retire_old_tools() { :; }
brain_retire_old_venvs() { :; }
brain_sudoers() { :; }
sudo() { "$@"; }
SUDO() { case "$1" in tee) command tee "$2" ;; mkdir|chmod) "$@" ;; *) : ;; esac; }
'''


def _fn(name: str) -> str:
    """One function's text out of install.sh, from `name() {` to its closing brace."""
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _services(agent_mode: str, engine_env: str | None = None, sandbox: str = "") -> tuple[str, str]:
    """Run phase_services for an install whose agent is `agent_mode` ("" = voice-only),
    over an engine.env holding `engine_env` (None = a first install). Returns the
    engine.env it wrote and the run's output."""
    tmp = pathlib.Path(tempdir("teaport-engine-env-"))
    (tmp / "etc").mkdir()
    (tmp / "secrets").mkdir()
    if engine_env is not None:
        (tmp / "etc" / "engine.env").write_text(engine_env)
    body = (STUBS + "".join(_fn(f) for f in ("read_env_file", "write_env", "engine_env_lines", "phase_services"))
            + f'AGENT_MODE="{agent_mode}"\nphase_services\n')
    env = {**os.environ, "T": str(tmp), "SANDBOX": sandbox}
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    return (tmp / "etc" / "engine.env").read_text(), out


def _reserve(engine_env: str) -> list[str]:
    return [ln for ln in engine_env.splitlines() if ln.startswith("KOKORO_RESERVE_FPT=")]


def test_an_agent_install_reserves_8():
    for mode, sandbox in (("openclaw", ""), ("nemoclaw", "my-sandbox")):
        written, out = _services(mode, sandbox=sandbox)
        assert _reserve(written) == ["KOKORO_RESERVE_FPT=8"], (mode, written, out)


def test_a_voice_only_install_reserves_12():
    written, out = _services("")
    assert _reserve(written) == ["KOKORO_RESERVE_FPT=12"], (written, out)


def test_a_repair_moves_the_old_agent_figure_to_8_and_says_so():
    # A box the installer put on 6 is carried to 8 by the next full run: the key is the
    # installer's (written plain), so the file's 6 does not survive, and the run names it.
    # An operator's own setting in the file is kept.
    written, out = _services("openclaw", "KOKORO_RESERVE_FPT=6\nOPERATOR_KNOB=7\n")
    assert _reserve(written) == ["KOKORO_RESERVE_FPT=8"], (written, out)
    assert "OPERATOR_KNOB=7" in written.splitlines(), written
    assert "changing 1 previously-set value(s)" in out and "KOKORO_RESERVE_FPT" in out, out


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
