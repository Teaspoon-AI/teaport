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
#   A flag needs ONE truth table — and it is pipecat's, which this process already uses
#   for PIPECAT_* flags; env_flag wraps it rather than competing with it. Three modules
#   had grown three different ones: an
#   empty value (`TEAPORT_LLM_TEXT_GUARD=`, a plausible hand-edit) DISABLED the
#   degeneracy guard while the same empty value ENABLED the thinking sound, and "no"
#   disabled one but not the other. Here an empty value means "not set" — take the
#   default — and a flag that is off says so in the journal, because a safety guard
#   must never be silently absent.
#
import os

from loguru import logger

from pipecat.utils.env import InvalidEnvVarValueError, env_truthy


def env_flag(name: str, default: bool = True) -> bool:
    """Read a boolean knob. Empty/unset -> `default`; unrecognized -> `default` + warning.

    Delegates the actual truth table to pipecat's env_truthy, which is already in this
    process (local_smart_turn_v3 reads PIPECAT_* flags through it). A second hand-rolled
    table here meant the brain had TWO: pipecat's accepted "y"/"n" and this one did not,
    so `PIPECAT_SMART_TURN_LOG_DATA=y` worked while `TEAPORT_LLM_TEXT_GUARD=y` silently
    meant "default" — one brain.env file, two answers, which is the exact failure this
    module was written to end.

    Two teaport-specific behaviours wrap it, and they are the reason this is not just an
    alias. An EMPTY value means "not set" here, where env_truthy reads it as False: an
    operator who writes `TEAPORT_LLM_TEXT_GUARD=` has not asked for a safety guard to be
    off. And an unparseable value warns and falls back instead of raising, because these
    are read at import time and an exception would crash-loop the service.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = env_truthy(name, default)
    except InvalidEnvVarValueError:
        logger.warning(f"{name}={raw.strip()!r} is not a boolean; using default {default}")
        return default
    if not value:
        logger.info(f"{name}={raw.strip().lower()} — disabled")
    return value


def env_num(name: str, default, cast):
    """Read a numeric knob, warning and falling back rather than raising."""
    raw = (os.getenv(name) or "").strip()
    try:
        return cast(raw or default)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; using default {default}")
        return cast(default)
