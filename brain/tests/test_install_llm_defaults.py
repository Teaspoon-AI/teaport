#!/usr/bin/env python3
# test_install_llm_defaults.py — what a fresh install writes for the LLM and TTS pacing.
#
# A box nobody named an endpoint for gets OpenRouter, the endpoint the brain's account
# features (first-run sign-in, the shared OpenClaw key, the credit checks) expect, and the
# model name OpenRouter takes (openai/gpt-oss-120b). The bare gpt-oss-120b stays the
# default for every other endpoint (Groq, Cerebras), and OpenRouter
# rejects it. Both are seeds: a repair keeps what brain.env or the config page holds.
# TTS pacing (lead / low_lead, #83) is seeded the same way.
#
# The functions are taken out of install.sh as they are and run by bash with the commands
# around them stubbed, against a temp dir. Stdin is not a terminal, so phase_credentials
# takes the unattended path. Nothing outside the temp dir is touched.
#
# Run: python test_install_llm_defaults.py   (or via pytest test_suite.py)
#
import os
import pathlib
import subprocess

from tempdirs import tempdir

INSTALL = pathlib.Path(__file__).resolve().parents[2] / "install.sh"

STUBS = r'''
set -euo pipefail
DRY_RUN=0 ETC="$T/etc" SECRETS="$T/secrets" RUN_USER=nobody SANDBOX=""
ENGINE_PORT=8000 BRAIN_PORT=7861 GATEWAY_PORT=18789 SETTINGS_PORT=8443
FRONTDOOR_HOST=teaport.local FRONTDOOR_CA_NAME="teaport CA" TLS_CERT="" AGENT_MODE=""
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
die()  { printf 'DIE %s\n' "$*"; exit 1; }
run()  { "$@"; }
resolve_gateway_token() { GATEWAY_TOKEN=tok; }
render_unit() { :; }
brain_activate() { :; }
brain_prune() { :; }
brain_deb_prune() { :; }
brain_retire_old_tools() { :; }
brain_sudoers() { :; }
install_config_sudoers() { :; }
sudo() { "$@"; }
SUDO() { case "$1" in tee) command tee "$2" ;; mkdir|chmod) "$@" ;; *) : ;; esac; }
'''


def _fn(name: str) -> str:
    """One function's text out of install.sh, from `name() {` to its closing brace."""
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _line(prefix: str) -> str:
    """The one top-level line of install.sh that starts with `prefix`."""
    found = [ln for ln in INSTALL.read_text().splitlines() if ln.startswith(prefix)]
    assert len(found) == 1, (prefix, found)
    return found[0] + "\n"


def _globals() -> str:
    return "".join(_line(p) for p in ('LLM_BASE_URL=""', "LLM_DEFAULT_URL=", "TTS_PACING_SEED="))


def _install(env: dict | None = None, brain_env: str | None = None) -> tuple[dict, str]:
    """phase_credentials then phase_services, unattended, over a brain.env holding
    `brain_env` (None = a first install). Returns the brain.env it wrote, as a dict."""
    tmp = pathlib.Path(tempdir("teaport-llm-defaults-"))
    (tmp / "etc").mkdir()
    (tmp / "secrets").mkdir()
    if brain_env is not None:
        (tmp / "etc" / "brain.env").write_text(brain_env)
    fns = ("read_env_file", "write_env", "engine_env_lines", "llm_default_model",
           "phase_credentials", "phase_services")
    body = STUBS + _globals() + "".join(_fn(f) for f in fns) + "phase_credentials\nphase_services\n"
    clean = {k: v for k, v in os.environ.items() if not k.startswith("TEAPORT_LLM_")}
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                       env={**clean, "T": str(tmp), **(env or {})}, stdin=subprocess.DEVNULL)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    written = {}
    for ln in (tmp / "etc" / "brain.env").read_text().splitlines():
        if "=" in ln and not ln.lstrip().startswith("#"):
            k, v = ln.split("=", 1)
            written[k] = v
    return written, out


def _model_for(url: str) -> str:
    body = _fn("llm_default_model") + f'llm_default_model "{url}"\n'
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, check=True).stdout.strip()


def test_the_model_name_follows_the_endpoint():
    for url in ("https://openrouter.ai/api/v1", "https://OpenRouter.ai/api/v1/",
                "https://eu.openrouter.ai/api/v1", "https://openrouter.ai:443/api/v1"):
        assert _model_for(url) == "openai/gpt-oss-120b", url
    for url in ("https://api.groq.com/openai/v1", "https://api.cerebras.ai/v1",
                "https://notopenrouter.ai/v1",
                "https://openrouter.ai.example.com/v1"):
        assert _model_for(url) == "gpt-oss-120b", url


def test_an_unattended_install_gets_openrouter():
    env, out = _install()
    assert env["LLM_BASE_URL"] == "https://openrouter.ai/api/v1", out
    assert env["LLM_MODEL"] == "openai/gpt-oss-120b", out


def test_a_named_endpoint_gets_its_own_model_name():
    env, out = _install({"TEAPORT_LLM_BASE_URL": "https://api.groq.com/openai/v1"})
    assert env["LLM_BASE_URL"] == "https://api.groq.com/openai/v1", out
    assert env["LLM_MODEL"] == "gpt-oss-120b", out
    env, out = _install({"TEAPORT_LLM_BASE_URL": "https://openrouter.ai/api/v1"})
    assert env["LLM_MODEL"] == "openai/gpt-oss-120b", out


def test_a_named_model_wins():
    env, out = _install({"TEAPORT_LLM_MODEL": "some/model"})
    assert env["LLM_BASE_URL"] == "https://openrouter.ai/api/v1", out
    assert env["LLM_MODEL"] == "some/model", out


def test_a_repair_keeps_the_llm_it_has():
    # Nobody named an endpoint this run: the defaults are seeds, and lose to brain.env.
    env, out = _install(brain_env="LLM_BASE_URL=https://api.groq.com/openai/v1\nLLM_MODEL=custom\n")
    assert env["LLM_BASE_URL"] == "https://api.groq.com/openai/v1", out
    assert env["LLM_MODEL"] == "custom", out


def test_a_fresh_install_paces_tts():
    env, out = _install()
    assert env["TTS_PACING"] == "lead" and env["TTS_EAGER"] == "low_lead", out


def test_a_repair_keeps_the_pacing_it_has():
    env, out = _install(brain_env="TTS_PACING=greedy\nTTS_EAGER=always\n")
    assert env["TTS_PACING"] == "greedy" and env["TTS_EAGER"] == "always", out


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
