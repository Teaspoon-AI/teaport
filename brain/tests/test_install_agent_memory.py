#!/usr/bin/env python3
# test_install_agent_memory.py — the embedding model install.sh gives a host OpenClaw's
# memory_search (write_memory_embed_patch, openclaw_memory_embeddings): bge-m3 on OpenRouter,
# only where the owner chose no provider, with OpenClaw's own OpenRouter provider when it has
# one and the brain's OpenRouter key otherwise; that key is kept current on every install. The functions are taken out of install.sh as they
# are and run by bash against a stub `openclaw`. Keys are synthetic.
#
# Run: python test_install_agent_memory.py   (or via pytest test_suite.py)
#
import hashlib
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


def sha(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _openclaw_json(path, written):
    """An openclaw.json whose memory.search key, as written, is `written` (None: no file)."""
    if written is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"memory": {"search": {"remote": {"apiKey": written}}}}))


def _patch(providers, llm_url, key="sk-or-v1-synthetic", current="", record=None, lines=False, written=None):
    """write_memory_embed_patch's (rc, provider id, memory.search or None, patch file mode);
    record: the installer's record (a key's sha256), None for none; lines: the whole stdout;
    written: the key as openclaw.json has it (`config get` shows it redacted, a ${VAR} resolved)."""
    t = pathlib.Path(tempdir())
    out, keyfile, rec, cfg = t / "patch.json", t / "llm_key", t / "record", t / "openclaw.json"
    if key is not None:
        keyfile.write_text(key + "\n")
    if record is not None:
        rec.write_text(record + "\n")
    _openclaw_json(cfg, written)
    script = ("set -euo pipefail\n" + _helpers() + _fn("write_memory_embed_patch")
              + 'write_memory_embed_patch "$1" "$2" "$3" "$4" "$5" "$6" "$7"\n')
    p = subprocess.run(["bash", "-c", script, "-", str(out), current, providers, llm_url, str(keyfile), str(rec), str(cfg)],
                       capture_output=True, text=True)
    stdout = p.stdout.split() if lines else (p.stdout.split() or [""])[0]
    if not out.exists():
        return p.returncode, stdout, None, None
    return (p.returncode, stdout, json.loads(out.read_text())["memory"]["search"],
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
    # The key's sha256 comes back for the installer's record; never the key itself.
    rc, out, _, _ = _patch("", OR_URL, lines=True)
    assert out == ["openai-compatible", sha("sk-or-v1-synthetic")], out
    rc, out, _, _ = _patch(json.dumps({"openrouter": {"baseUrl": OR_URL}}), OR_URL, lines=True)
    assert out == ["openrouter"], out   # OpenClaw's own provider: no key of the brain's


def test_no_openrouter_no_embeddings():
    for llm_url in ("https://api.example.com/v1", "https://openrouter.ai.example.com/v1", ""):
        assert _patch("", llm_url)[0] == 3, llm_url
    assert _patch("", OR_URL, key="")[0] == 3       # OpenRouter, but no key yet
    assert _patch("", OR_URL, key=None)[0] == 3     # no key file at all
    rc, out, search, _ = _patch(json.dumps({"or": {"baseUrl": "https://eu.openrouter.ai/api/v1"}}), "")
    assert rc == 0 and search["provider"] == "or", search   # a subdomain is OpenRouter's


OURS = json.dumps({"provider": "openai-compatible", "model": "baai/bge-m3",
                   "remote": {"baseUrl": OR_URL, "apiKey": "__OPENCLAW_REDACTED__"}})


def test_the_setting_an_install_wrote_gets_the_brains_current_key():
    # The owner rotated the OpenRouter key: the brain has the new one, and so must OpenClaw,
    # or memory search fails closed from then on.
    old = sha("sk-or-v1-old")
    rc, out, search, mode = _patch("", OR_URL, key="sk-or-v1-rotated", current=OURS, record=old, lines=True,
                                   written="sk-or-v1-old")
    assert rc == 5 and out == ["openai-compatible", sha("sk-or-v1-rotated")], (rc, out)
    assert search == {"remote": {"apiKey": "sk-or-v1-rotated"}}, search
    assert mode == 0o600, oct(mode)
    # The same key as recorded: nothing to do.
    assert _patch("", OR_URL, key="sk-or-v1-old", current=OURS, record=old, written="sk-or-v1-old")[:3] == (4, "openai-compatible", None)
    # The brain moved off OpenRouter, or has no key: the setting is left as it is.
    assert _patch("", "https://api.example.com/v1", current=OURS, record=old, written="sk-or-v1-old")[:3] == (4, "openai-compatible", None)
    assert _patch("", OR_URL, key=None, current=OURS, record=old, written="sk-or-v1-old")[0] == 4


def test_a_setting_the_installer_has_no_record_of_is_the_owners():
    # The same provider, URL and model, and `config get` showing a plain key (it resolves a
    # ${VAR} before it redacts), but not the key the record says the installer wrote: no
    # record, a ${VAR} or a SecretRef in openclaw.json, or the owner's own key put in its
    # place. Never overwritten with the brain's key.
    old = sha("sk-or-v1-old")
    assert _patch("", OR_URL, key="sk-or-v1-new", current=OURS, written="sk-or-v1-old")[:3] == (4, "openai-compatible", None)
    for written in ("${OPENROUTER_API_KEY}", {"source": "env", "id": "OPENROUTER_API_KEY"}, "sk-or-v1-owners-own"):
        assert _patch("", OR_URL, key="sk-or-v1-new", current=OURS, record=old, written=written)[:3] == \
            (4, "openai-compatible", None), written
    assert _patch("", OR_URL, key="sk-or-v1-new", current=OURS, record=old)[0] == 4   # no openclaw.json to read


def test_the_owners_setting_is_never_touched():
    owners = [{"provider": "none"}, {"provider": "openai", "model": "text-embedding-3-small"},
              {"provider": "openrouter", "model": "baai/bge-m3"},   # by id: no key of ours in it
              {"provider": "openai-compatible", "model": "baai/bge-m3",
               "remote": {"baseUrl": "https://embed.example.com/v1"}},
              {"provider": "openai-compatible", "model": "other/model", "remote": {"baseUrl": OR_URL}}]
    for cur in owners:
        rc, out, search, _ = _patch(json.dumps({"openrouter": {"baseUrl": OR_URL}}), OR_URL,
                                    current=json.dumps(cur), record=sha("sk-or-v1-synthetic"))
        assert rc == 4 and out == cur["provider"] and search is None, (cur, rc, search)


def _phase(current="", fail=(), providers='{"openrouter": {"baseUrl": "%s"}}' % OR_URL, key=None, record=None,
           written=None):
    t = pathlib.Path(tempdir())
    _openclaw_json(t / ".openclaw/openclaw.json", written)
    if key is not None:
        (t / "llm_key").write_text(key + "\n")
    if record is not None:
        (t / "openclaw-memory-embeddings").write_text(record + "\n")
    calls, patches = t / "calls", t / "patches"
    oc = t / "openclaw"
    fails = " ".join(f'"{f}"' for f in fail)
    oc.write_text("#!/bin/bash\n"
                  f'echo "$*" >> "{calls}"\n'
                  f'for f in {fails}; do [ "$1 $2" = "$f" ] && exit 1; done\n'
                  f'if [ "$*" = "config get memory.search --json" ]; then [ -n {json.dumps(current)} ] && {{ printf %s {json.dumps(current)}; exit 0; }}; exit 1; fi\n'
                  f'if [ "$*" = "config get models.providers --json" ]; then printf %s {json.dumps(providers)}; exit 0; fi\n'
                  f'if [ "$1 $2" = "config patch" ]; then cat "$4" >> "{patches}"; echo >> "{patches}"; fi\n'
                  "exit 0\n")
    oc.chmod(0o755)
    script = ("set -euo pipefail\nDRY_RUN=0\n" + _helpers() + _fn("write_memory_embed_patch")
              + _fn("openclaw_memory_embeddings")
              + f'OPENCLAW="{oc}" LLM_BASE_URL={OR_URL} SECRETS="{t}" RUN_HOME="{t}" MEMORY_EMBED_RECORD="{t}/openclaw-memory-embeddings"\n'
              + "openclaw_memory_embeddings\n")
    env = {k: v for k, v in os.environ.items() if k != "OPENCLAW_CONFIG_PATH"}
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**env, "TMPDIR": str(t)})
    read = lambda f: f.read_text().splitlines() if f.exists() else []
    return p.returncode, p.stdout + p.stderr, read(calls), [json.loads(x) for x in read(patches) if x], t


def test_an_unset_provider_gets_bge_m3_and_the_index_is_rebuilt():
    rc, log, calls, patches, t = _phase()
    assert rc == 0, log
    assert patches == [{"memory": {"search": {"provider": "openrouter", "model": "baai/bge-m3"}}}], patches
    assert calls[-1] == "memory index --force", calls
    assert not [f for f in t.iterdir() if f.name.startswith("tmp")], "the patch file was left behind"


def test_the_owners_provider_stays():
    for chosen in ("none", "openai", "openrouter"):
        rc, log, calls, patches, _ = _phase(current=json.dumps({"provider": chosen}))
        assert rc == 0 and patches == [] and "memory index --force" not in calls, (chosen, calls)
        assert "keeps its provider" in log, log


def test_the_brains_key_is_recorded_once_the_index_is_built():
    rc, log, calls, patches, t = _phase(providers="{}", key="sk-or-v1-first")
    rec = t / "openclaw-memory-embeddings"
    assert rc == 0 and calls[-1] == "memory index --force", (log, calls)
    assert rec.read_text().strip() == sha("sk-or-v1-first") and stat.S_IMODE(rec.stat().st_mode) == 0o600
    assert "sk-or-v1-first" not in rec.read_text()
    # OpenClaw's own provider: no record (no key of the brain's to follow).
    rc, log, calls, patches, t = _phase(key="sk-or-v1-first", record=sha("stale"))
    assert rc == 0 and not (t / "openclaw-memory-embeddings").exists(), log


def test_a_changed_key_is_refreshed_and_checked_like_a_new_one():
    rc, log, calls, patches, t = _phase(current=OURS, key="sk-or-v1-new", record=sha("sk-or-v1-old"), written="sk-or-v1-old")
    assert rc == 0 and patches == [{"memory": {"search": {"remote": {"apiKey": "sk-or-v1-new"}}}}], (log, patches)
    assert calls[-1] == "memory index --force" and "current key" in log, (log, calls)
    assert (t / "openclaw-memory-embeddings").read_text().strip() == sha("sk-or-v1-new")
    assert not [f for f in t.iterdir() if f.name.startswith("tmp")], "the patch file was left behind"
    # A new key that cannot embed (a typo, a key at its limit) is taken back out, record and all.
    rc, log, calls, patches, t = _phase(current=OURS, key="sk-or-v1-bad", record=sha("sk-or-v1-old"), written="sk-or-v1-old",
                                        fail=("memory index",))
    assert rc == 0 and "stays keyword-only" in log, log
    assert patches[-1] == {"memory": {"search": {"provider": None, "model": None, "remote": None}}}, patches
    assert not (t / "openclaw-memory-embeddings").exists()
    # The key as recorded: nothing written, no rebuild.
    rc, log, calls, patches, _ = _phase(current=OURS, key="sk-or-v1-old", record=sha("sk-or-v1-old"), written="sk-or-v1-old")
    assert rc == 0 and patches == [] and "memory index --force" not in calls and "keeps its provider" in log, log


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


def test_it_runs_with_the_credentials_known():
    main = INSTALL.read_text()
    assert "\n  phase_credentials   # before phase_agent" in main and "\n  phase_agent\n" in main
    assert main.index("\n  phase_credentials   #") < main.index("\n  phase_agent\n")
    assert "phase_agent_memory" not in main


def _agent(state, fail=""):
    """agent_openclaw with every step it calls stubbed: (rc, log, the ordered calls). state:
    the gateway unit's ActiveState; fail: an openclaw subcommand ("plugins install") that fails."""
    t = pathlib.Path(tempdir())
    calls = t / "calls"
    bin_ = t / "bin"
    bin_.mkdir()
    for name, body in (("systemctl", f'echo "systemctl $*" >> "{calls}"\n'
                                     f'if [ "$2" = show ]; then echo {state}; fi\nexit 0\n'),
                       ("loginctl", "echo Linger=yes\n")):
        (bin_ / name).write_text("#!/bin/bash\n" + body)
        (bin_ / name).chmod(0o755)
    oc = t / "openclaw"
    oc.write_text(f'#!/bin/bash\necho "openclaw $*" >> "{calls}"\n'
                  f'[ "$1 $2" = {json.dumps(fail)} ] && exit 7\nexit 0\n')
    oc.chmod(0o755)
    stubs = "".join(f'{fn}() {{ echo "{fn}" >> "{calls}"; }}\n' for fn in (
        "resolve_gateway_token", "write_talk_patch", "add_docs_to_openclaw_wiki",
        "openclaw_memory_embeddings"))
    stubs += ('plugin_tgz() { :; }\nplugin_consent_flag() { echo --accept-capabilities; }\n'
              'cap_openclaw_heap() { return 1; }\nas_user() { "$@"; }\ncontains() { case "$2" in *"$1"*) return 0;; esac; return 1; }\n')
    script = ("set -euo pipefail\nDRY_RUN=0\n" + _helpers()
              + "".join(ln + "\n" for ln in INSTALL.read_text().splitlines() if ln.startswith("run()  "))
              + _fn("agent_openclaw") + stubs
              + f'OPENCLAW="{oc}" BRAIN_PORT=7861 GATEWAY_PORT=18789 FRONTDOOR_HOST=x.local '
              + f'PLUGIN_NPM_SPEC=p RUN_USER=u RUN_HOME="{t}" SECRETS="{t}/s" OPENCLAW_HEAP_MIB=1536\n'
              + "agent_openclaw\n")
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "TMPDIR": str(t)})
    return p.returncode, p.stdout + p.stderr, calls.read_text().splitlines()


def test_a_running_gateway_is_stopped_for_the_changes_and_started_once():
    # OpenClaw applies the plugin, gateway.* and memory.search changes by restarting a running
    # gateway, each a moment after the change: so none of them may meet a running gateway,
    # and the one start comes after the last of them.
    for state in ("active", "activating"):   # activating: on its way up, or about to restart
        rc, log, calls = _agent(state)
        assert rc == 0, log
        i = lambda c: next(n for n, x in enumerate(calls) if x.startswith(c))
        # OpenClaw's own start: it repairs a unit whose --port no longer matches gateway.port.
        stop, start = i("systemctl --user stop openclaw-gateway.service"), i("openclaw gateway start")
        for change in ("openclaw plugins install", "openclaw plugins enable", "openclaw config patch",
                       "add_docs_to_openclaw_wiki", "openclaw_memory_embeddings"):
            assert stop < i(change) < start, (state, change, calls)
        assert not [c for c in calls if "gateway restart" in c or c.startswith("systemctl --user start")], calls
    # Not running: nothing stopped, and the unit is restarted as before.
    rc, log, calls = _agent("inactive")
    assert rc == 0 and not [c for c in calls if " stop " in c] and "openclaw gateway restart" in calls, calls


def test_a_run_that_stops_on_the_way_starts_the_gateway_again():
    rc, log, calls = _agent("active", fail="plugins install")
    assert rc != 0, log
    assert calls.index("systemctl --user stop openclaw-gateway.service") < \
        calls.index("systemctl --user start openclaw-gateway.service"), calls   # the EXIT trap


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
