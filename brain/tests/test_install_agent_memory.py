#!/usr/bin/env python3
# test_install_agent_memory.py — the embedding model install.sh gives a host OpenClaw's
# memory_search (write_memory_embed_patch, phase_agent_memory): bge-m3 on OpenRouter, only
# where the owner chose no provider, with OpenClaw's own OpenRouter provider when it has one
# and the brain's OpenRouter key otherwise. The functions are taken out of install.sh as they
# are and run by bash against a stub `openclaw`. Keys are synthetic.
#
# Run: python test_install_agent_memory.py   (or via pytest test_suite.py)
#
import json
import os
import pathlib
import stat
import subprocess

import appliance
from tempdirs import tempdir

INSTALL = pathlib.Path(__file__).resolve().parents[2] / "install.sh"
OR_URL = "https://openrouter.ai/api/v1"


def _fn(name: str) -> str:
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _helpers() -> str:
    return "".join(ln + "\n" for ln in INSTALL.read_text().splitlines()
                   if ln.startswith(("log() ", "warn() ", "MEMORY_EMBED_MODEL=")))


def _patch(providers, llm_url, key="sk-or-v1-synthetic"):
    """write_memory_embed_patch's (rc, stdout, memory.search or None, patch file mode)."""
    t = pathlib.Path(tempdir())
    out, keyfile = t / "patch.json", t / "llm_key"
    if key is not None:
        keyfile.write_text(key + "\n")
    script = ("set -euo pipefail\n" + _helpers() + _fn("write_memory_embed_patch")
              + 'write_memory_embed_patch "$1" "$2" "$3" "$4"\n')
    p = subprocess.run(["bash", "-c", script, "-", str(out), providers, llm_url, str(keyfile)],
                       capture_output=True, text=True)
    if not out.exists():
        return p.returncode, p.stdout.strip(), None, None
    return (p.returncode, p.stdout.strip(), json.loads(out.read_text())["memory"]["search"],
            stat.S_IMODE(out.stat().st_mode))


def test_openclaws_own_openrouter_provider_is_used_by_id():
    # As `config get models.providers --json` prints it: the key redacted, never needed here.
    providers = json.dumps({"dgx": {"baseUrl": "http://192.0.2.10:8888/v1"},
                            "openrouter": {"baseUrl": OR_URL, "apiKey": "__OPENCLAW_REDACTED__"}})
    rc, out, search, _ = _patch(providers, "https://api.example.com/v1")
    assert rc == 0 and out == "openrouter", (rc, out)
    assert search == {"provider": "openrouter", "model": "baai/bge-m3"}, search


def test_the_brains_openrouter_key_otherwise():
    for providers in ("", "{}", json.dumps({"dgx": {"baseUrl": "http://192.0.2.10:8888/v1"}}), "not json"):
        rc, out, search, mode = _patch(providers, OR_URL)
        assert rc == 0 and out == "openai-compatible", (providers, rc, out)
        assert search == {"provider": "openai-compatible", "model": "baai/bge-m3",
                          "remote": {"baseUrl": OR_URL, "apiKey": "sk-or-v1-synthetic"}}, search
        assert mode == 0o600, oct(mode)   # it holds the key


def test_no_openrouter_no_embeddings():
    for llm_url in ("https://api.example.com/v1", "https://openrouter.ai.example.com/v1", ""):
        assert _patch("", llm_url)[0] == 3, llm_url
    assert _patch("", OR_URL, key="")[0] == 3       # OpenRouter, but no key yet
    assert _patch("", OR_URL, key=None)[0] == 3     # no key file at all
    rc, out, search, _ = _patch(json.dumps({"or": {"baseUrl": "https://eu.openrouter.ai/api/v1"}}), "")
    assert rc == 0 and search["provider"] == "or", search   # a subdomain is OpenRouter's


def _phase(provider_get="", fail=(), agent="openclaw", providers='{"openrouter": {"baseUrl": "%s"}}' % OR_URL):
    t = pathlib.Path(tempdir())
    calls, patches = t / "calls", t / "patches"
    oc = t / "openclaw"
    fails = " ".join(f'"{f}"' for f in fail)
    oc.write_text("#!/bin/bash\n"
                  f'echo "$*" >> "{calls}"\n'
                  f'for f in {fails}; do [ "$1 $2" = "$f" ] && exit 1; done\n'
                  f'if [ "$*" = "config get memory.search.provider --json" ]; then [ -n {json.dumps(provider_get)} ] && {{ printf %s {json.dumps(provider_get)}; exit 0; }}; exit 1; fi\n'
                  f'if [ "$*" = "config get models.providers --json" ]; then printf %s {json.dumps(providers)}; exit 0; fi\n'
                  f'if [ "$1 $2" = "config patch" ]; then cat "$4" >> "{patches}"; echo >> "{patches}"; fi\n'
                  "exit 0\n")
    oc.chmod(0o755)
    script = ("set -euo pipefail\nDRY_RUN=0\n" + _helpers() + _fn("write_memory_embed_patch")
              + _fn("phase_agent_memory")
              + f'AGENT_MODE={agent} OPENCLAW="{oc}" LLM_BASE_URL={OR_URL} SECRETS="{t}"\nphase_agent_memory\n')
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**os.environ, "TMPDIR": str(t)})
    read = lambda f: f.read_text().splitlines() if f.exists() else []
    return p.returncode, p.stdout + p.stderr, read(calls), [json.loads(x) for x in read(patches) if x], t


def test_an_unset_provider_gets_bge_m3_and_the_index_is_rebuilt():
    rc, log, calls, patches, t = _phase()
    assert rc == 0, log
    assert patches == [{"memory": {"search": {"provider": "openrouter", "model": "baai/bge-m3"}}}], patches
    assert calls[-1] == "memory index --force", calls
    assert not [f for f in t.iterdir() if f.name.startswith("tmp")], "the patch file was left behind"


def test_the_owners_provider_stays():
    for chosen in ('"none"', '"openai"', '"openrouter"'):
        rc, log, calls, patches, _ = _phase(provider_get=chosen)
        assert rc == 0 and patches == [] and "memory index --force" not in calls, (chosen, calls)
        assert "keeps its provider" in log, log


def test_nothing_to_embed_with_or_a_failure_never_fails_the_install():
    rc, log, calls, patches, _ = _phase(providers="{}")   # and the stub has no key file
    assert rc == 0 and patches == [] and "keyword-only" in log, log
    rc, log, calls, patches, _ = _phase(fail=("config patch",))
    assert rc == 0 and "memory index --force" not in calls and "warn:" in log, log
    # An index that cannot be built (a dead key, no credit) takes the provider back out: a
    # remote provider fails closed, so leaving it would break memory_search altogether.
    rc, log, calls, patches, _ = _phase(fail=("memory index",))
    assert rc == 0 and "stays keyword-only" in log, log
    assert patches[-1] == {"memory": {"search": {"provider": None, "model": None, "remote": None}}}, patches
    rc, log, calls, patches, _ = _phase(agent="nemoclaw")
    assert rc == 0 and calls == [], calls


def test_it_runs_after_the_credentials():
    main = INSTALL.read_text()
    assert "  phase_agent\n  phase_credentials\n  phase_agent_memory" in main


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
