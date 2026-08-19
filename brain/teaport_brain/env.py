#
# env.py — one truth table for the brain's environment knobs.
#
# These values live in /etc/teaport/brain.env, which installer repairs preserve
# verbatim. Two consequences drive this module:
#
#   A bare int()/float() at import time turns one operator typo ("", "off", "2.5")
#   into an import-time ValueError that crash-loops the whole brain service, and
#   re-running the installer cannot clear it. So every cast warns and falls back.
#
#   A flag needs ONE truth table. Three modules had grown three different ones: an
#   empty value (`TEAPORT_LLM_TEXT_GUARD=`, a plausible hand-edit) DISABLED the
#   degeneracy guard while the same empty value ENABLED the thinking sound, and "no"
#   disabled one but not the other. Here an empty value means "not set" — take the
#   default — and a flag that is off says so in the journal, because a safety guard
#   must never be silently absent.
#
import os

from loguru import logger

_FALSE = ("0", "false", "no", "off")
_TRUE = ("1", "true", "yes", "on")


def env_flag(name: str, default: bool = True) -> bool:
    """Read a boolean knob. Empty/unset -> `default`; unrecognized -> `default` + warning."""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in _FALSE:
        logger.info(f"{name}={raw} — disabled")
        return False
    if raw in _TRUE:
        return True
    logger.warning(f"{name}={raw!r} is not a boolean; using default {default}")
    return default


def env_num(name: str, default, cast):
    """Read a numeric knob, warning and falling back rather than raising."""
    raw = (os.getenv(name) or "").strip()
    try:
        return cast(raw or default)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; using default {default}")
        return cast(default)
