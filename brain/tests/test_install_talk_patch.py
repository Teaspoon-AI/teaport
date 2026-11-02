#!/usr/bin/env python3
# test_install_talk_patch.py — the OpenClaw config install.sh patches in (write_talk_patch).
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


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
