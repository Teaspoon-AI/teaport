#
# Refuse to report a pass against the wrong pipecat.
#
# The turn-strategy tests assert on pipecat internals — when a strategy resets, what an
# emptied analyzer buffer returns, which frames reach a stop strategy. Those differ
# between releases, so a green run against a different version means nothing at all.
#
# The appliance pins pipecat-ai==1.5.0 (brain/pyproject.toml). That release is not on
# the public index, so a development machine silently ends up on 0.0.108 instead — and a
# test written and "verified" there can fail the moment it runs where it matters. That
# happened on 2026-08-20 and cost a debugging cycle, which is why this is enforced
# rather than remembered.
#
import os
import re

import pipecat

_PYPROJECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pyproject.toml")


def require_pinned():
    """Raise unless the installed pipecat matches the pin. Call at import time."""
    try:
        with open(_PYPROJECT) as f:
            pin = re.search(r'pipecat-ai\[[^\]]*\]==([0-9][^"\']*)', f.read())
    except OSError:
        return  # copied somewhere without the manifest; nothing to enforce
    if not pin:
        return  # no pin to check against
    want, have = pin.group(1), pipecat.__version__
    if have != want:
        raise SystemExit(
            f"\nWRONG PIPECAT: installed {have}, this suite is only meaningful against "
            f"{want} (brain/pyproject.toml).\nRun it on the appliance:\n"
            f"  scp tests/*.py teaspoon@<box>:/tmp/btests/ && "
            f"/opt/teaport/venv/bin/python3 /tmp/btests/<test>.py\n"
        )
