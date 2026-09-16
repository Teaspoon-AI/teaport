#
# agent_backend.py — which agent, if any, this box has beside the brain.
#
# The brain was written assuming a co-resident OpenClaw gateway, and the installer
# has never assumed that: its voice-only path (no plugin, no front door) leaves the
# box with a SIP or Discord front-end and no gateway at all. Nothing crashed — the
# gateway client is best-effort by design — but the brain still registered five tools
# the model could not use, told it in the persona to use them, and spent up to
# TEAPORT_RECALL_TIMEOUT per turn on a memory search that returned nothing. This is
# the one place that says which kind of box it is; everything gateway-shaped keys on
# it here rather than re-reading the variable.
#
#   TEAPORT_AGENT=openclaw   a gateway is co-resident (host OpenClaw or a NemoClaw
#                            sandbox); the default, so an upgraded box is unchanged
#   TEAPORT_AGENT=none       voice-only: local tools only, no recall, persona from
#                            ~/.config/teaport/persona.md
#
# What `none` does NOT change: /talk, the consult plumbing (thinking bed, follow-up
# gate and injector) which stays wired and never fires because no tool triggers it,
# and the OpenClaw plugin, which simply has nothing to connect to. One code path.
#
import os

from loguru import logger

OPENCLAW = "openclaw"
NONE = "none"
MODES = (OPENCLAW, NONE)


def _read() -> str:
    raw = (os.getenv("TEAPORT_AGENT") or "").strip().lower()
    if not raw:
        return OPENCLAW
    if raw in MODES:
        return raw
    # Read at import time from a hand-edited file: fall back, never raise (see env.py).
    logger.warning(f"TEAPORT_AGENT={raw!r} is not one of {'|'.join(MODES)}; "
                   f"using {OPENCLAW}")
    return OPENCLAW


AGENT = _read()
# True when the gateway tools, memory recall and the workspace persona are available.
HAS_AGENT = AGENT == OPENCLAW


def startup_line() -> str:
    """The operator-facing evidence for the journal, same as the other flags' 'off'
    lines: which mode, and in `none` what that switched off."""
    if HAS_AGENT:
        return "agent backend: openclaw (gateway tools, memory recall, workspace persona)"
    return ("agent backend: none — no web_search/web_fetch/search_memory/remember/"
            "ask_openclaw, no memory recall; persona from the persona file")
