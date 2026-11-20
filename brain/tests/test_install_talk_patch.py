#!/usr/bin/env python3
# test_install_talk_patch.py — the OpenClaw config install.sh patches in (write_talk_patch),
# and the teaport docs it puts in OpenClaw's memory wiki (add_docs_to_openclaw_wiki).
#
# talk.realtime.brain must be "agent-consult": OpenClaw (2026.9.x) runs a realtime Talk
# session with no other brain, and the iOS app's Talk falls back to on-device speech for
# any other value -- the installer wrote "none" until 2026-11-01, and a full install put
# the box's OpenClaw app back on its own voice (plugin/README.md, "Why brain must be
# agent-consult"). The function is taken out of install.sh as it is and run by bash.
#
# Run: python test_install_talk_patch.py   (or via pytest test_suite.py)
#
import json
import os
import pathlib
import subprocess

import appliance
from tempdirs import tempdir

INSTALL = pathlib.Path(__file__).resolve().parents[2] / "install.sh"


def _fn(name: str) -> str:
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def test_the_talk_patch_is_json_with_the_agent_consult_brain():
    t = tempdir()
    out = pathlib.Path(t, "patch.json")
    script = ("set -euo pipefail\nGATEWAY_TOKEN=tok FRONTDOOR_HOST=teaport.local\n"
              + _fn("write_talk_patch") + f'write_talk_patch "{out}" ws://127.0.0.1:7861/talk\n')
    subprocess.run(["bash", "-c", script], check=True, env=os.environ)
    patch = json.loads(out.read_text())
    realtime = patch["talk"]["realtime"]
    assert realtime["brain"] == "agent-consult", realtime
    assert (realtime["provider"], realtime["mode"], realtime["transport"]) == (
        "teaport", "realtime", "gateway-relay")
    assert realtime["providers"]["teaport"] == {"url": "ws://127.0.0.1:7861/talk", "token": "tok"}
    assert patch["plugins"]["entries"]["device-pair"]["config"]["publicUrl"] == "wss://teaport.local"


def test_the_talk_patch_lets_the_teaport_plugin_add_its_docs_hint():
    # before_prompt_build needs hooks.allowConversationAccess for a non-bundled plugin
    # (plugin/docs-hint.js); the patch merges, so device-pair's entry stays beside it.
    t = tempdir()
    out = pathlib.Path(t, "patch.json")
    script = ("set -euo pipefail\nGATEWAY_TOKEN=tok FRONTDOOR_HOST=teaport.local\n"
              + _fn("write_talk_patch") + f'write_talk_patch "{out}" ws://127.0.0.1:7861/talk\n')
    subprocess.run(["bash", "-c", script], check=True, env=os.environ)
    entries = json.loads(out.read_text())["plugins"]["entries"]
    assert entries["teaport-realtime"] == {"hooks": {"allowConversationAccess": True}}, entries
    assert entries["device-pair"]["enabled"] is True


def _add_docs(enabled_get="", fail=(), docs=True, sync_rc=0):
    """Run add_docs_to_openclaw_wiki against a stub openclaw and a stub docs-wiki-sync.
    Returns (rc, log, openclaw calls, sync calls)."""
    t = pathlib.Path(tempdir())
    calls, synced = t / "calls", t / "synced"
    if docs:
        (t / "docs").mkdir()
    oc = t / "openclaw"
    fails = " ".join(f'"{f}"' for f in fail)
    oc.write_text("#!/bin/bash\n"
                  f'echo "$*" >> "{calls}"\n'
                  f'for f in {fails}; do [ "$1 $2" = "$f" ] && exit 1; done\n'
                  f'if [ "$1 $2" = "config get" ]; then [ -n {json.dumps(enabled_get)} ] && {{ printf %s {json.dumps(enabled_get)}; exit 0; }}; exit 1; fi\n'
                  "exit 0\n")
    sync = t / "docs-wiki-sync"
    sync.write_text(f'#!/bin/bash\necho "OPENCLAW=$OPENCLAW $*" >> "{synced}"\nexit {sync_rc}\n')
    for f in (oc, sync):
        f.chmod(0o755)
    script = ("set -euo pipefail\nDRY_RUN=0\n"
              + "".join(ln + "\n" for ln in INSTALL.read_text().splitlines()
                        if ln.startswith(("log() ", "warn() ")))
              + _fn("add_docs_to_openclaw_wiki")
              + f'TEAPORT_DOCS_DIR="{t}/docs" DOCS_WIKI_SYNC="{sync}" OPENCLAW="{oc}"\nadd_docs_to_openclaw_wiki\n')
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    read = lambda f: f.read_text().splitlines() if f.exists() else []
    return p.returncode, p.stdout + p.stderr, read(calls), read(synced), str(oc)


def test_the_install_enables_the_wiki_and_writes_the_docs_into_it():
    rc, log, calls, synced, oc = _add_docs()
    assert rc == 0, log
    docs = str(pathlib.Path(oc).parent / "docs")
    assert calls == ["config get plugins.entries.memory-wiki.enabled --json",
                     "plugins enable memory-wiki", "wiki init"], calls
    assert synced == [f"OPENCLAW={oc} --docs {docs}"], synced   # install.sh's openclaw and docs
    rc, log, calls, synced, _ = _add_docs(enabled_get="true")
    assert rc == 0 and len(synced) == 1, log


def test_an_owner_who_turned_the_wiki_off_keeps_it_off():
    rc, log, calls, synced, _ = _add_docs(enabled_get="false")
    assert rc == 0 and synced == [] and "plugins enable memory-wiki" not in calls, (log, calls)
    assert "turned off" in log, log


def test_a_docs_failure_is_said_and_never_fails_the_install():
    for kw in ({"docs": False}, {"fail": ("plugins enable",)}, {"fail": ("wiki init",)}, {"sync_rc": 1}):
        rc, log, calls, synced, _ = _add_docs(**kw)
        assert rc == 0 and "warn:" in log, (kw, log)
        if kw.get("fail") or not kw.get("docs", True):
            assert synced == [], (kw, synced)


def test_the_brain_package_ships_the_docs_and_the_script_install_sh_runs():
    root = INSTALL.parent
    recipe = (root / "packaging/nfpm.yaml").read_text()
    assert "  - src: docs/src/content/docs/\n    dst: /usr/share/teaport/docs/\n    type: tree\n" in recipe
    assert "  - src: packaging/deb/docs-wiki-sync\n    dst: /usr/lib/teaport/docs-wiki-sync\n" in recipe
    text = INSTALL.read_text()
    assert "\nTEAPORT_DOCS_DIR=/usr/share/teaport/docs\n" in text
    assert "\nDOCS_WIKI_SYNC=/usr/lib/teaport/docs-wiki-sync\n" in text
    # Upgrades refresh the pages an install put there, as the run user, and never fail on it.
    postinst = (root / "packaging/deb/postinst").read_text()
    assert "/usr/lib/teaport/docs-wiki-sync --if-present" in postinst
    assert postinst.rstrip().endswith("exit 0")


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
