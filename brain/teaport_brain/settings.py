#
# settings.py — read a setting through its config_schema.toml row.
#
# The schema declares every knob once: its type, its default, its bounds. A read with
# its own literal default — `float(os.getenv("TEAPORT_FOLLOWUP_QUIET_S", "0.7"))` —
# declared it a second time, kept in step with the row by nothing but care, and a
# bare cast like that one crash-loops the service on a single typo (see env.py).
# setting("TEAPORT_FOLLOWUP_QUIET_S") takes the type and the default from the row, so
# there is one declaration, and the fall-back-don't-raise contract holds by
# construction rather than by each call site remembering it.
#
# parse() is the ONE answer to "is this text a value of this row's type", pure data
# in and out. The config UI validates with it and setting() reads with it, so a value
# the page accepts is exactly a value the brain reads. Bounds are a separate question
# (out_of_bounds): the page enforces them; at runtime they are not enforced yet.
#
# A name with no row is a KeyError at import — a setting nobody can see, document or
# validate fails the first test that imports its module instead of shipping.
#
import json
import math
import os
import re

from loguru import logger

from teaport_brain import config_schema

ROWS: dict[str, dict] = {r["name"]: r for r in config_schema.load()["settings"]}

# pipecat's env_truthy table, which env_flag delegates to (tests/test_settings.py pins
# the two together, so a flag reads the same through either). Empty is not in it here:
# an empty value means "not set" everywhere in teaport.
FLAG_TRUE = frozenset({"1", "true", "yes", "y", "on"})
FLAG_FALSE = frozenset({"0", "false", "no", "n", "off"})


def _int(text: str, row: dict):
    if not re.fullmatch(r"[+-]?\d+", text):
        return None, "must be a whole number"
    return int(text), None


def _float(text: str, row: dict):
    try:
        n = float(text)
    except ValueError:
        return None, "must be a number"
    if not math.isfinite(n):
        return None, "must be a finite number"
    return n, None


def _flag(text: str, row: dict):
    word = text.lower()
    if "accepts" in row:  # a flag whose reader is not env_flag (LEDGER_TRACE)
        if word in row["accepts"]:
            return True, None
        return None, f"must be one of {', '.join(sorted(row['accepts']))}"
    if word in FLAG_TRUE or word in FLAG_FALSE:
        return word in FLAG_TRUE, None
    return None, f"must be one of {', '.join(sorted(FLAG_TRUE | FLAG_FALSE))}"


def _enum(text: str, row: dict):
    # Case-insensitive, as env_choice reads it; the value is the schema's spelling.
    by_word = {v.lower(): v for v in row["values"]}
    if text.lower() in by_word:
        return by_word[text.lower()], None
    return None, f"must be one of {', '.join(repr(v) for v in row['values'])}"


def _json(text: str, row: dict):
    try:
        value = json.loads(text)
    except ValueError as e:
        return None, f"not valid JSON: {e}"
    if not isinstance(value, dict):
        return None, "must be a JSON object"
    return value, None


def _matching(pattern: str, problem: str):
    def parse_text(text: str, row: dict):
        return (text, None) if re.fullmatch(pattern, text) else (None, problem)
    return parse_text


def _text(text: str, row: dict):
    return text, None


PARSERS = {
    "int": _int,
    "float": _float,
    "flag": _flag,
    "enum": _enum,
    "json": _json,
    "url": _matching(r"https?://\S+", "must start with http:// or https://"),
    "ws_url": _matching(r"wss?://\S+", "must start with ws:// or wss://"),
    "snowflake": _matching(r"\d+", "must be a Discord id (digits only)"),
    "string": _text,
    "path": _text,
    "dir": _text,
    "secret": _text,
}


def parse(row: dict, text: str):
    """(value, None) when `text` is a value of `row`'s type, else (None, the reason).

    `text` is a non-empty value as it stands in the file. What an empty one means is
    the caller's business: "not set" to setting(), refused by the page."""
    return PARSERS[row["type"]](text, row)


def out_of_bounds(row: dict, n) -> str | None:
    """The reason `n` is outside the row's min/max, or None."""
    lo, hi = row.get("min"), row.get("max")
    if lo is not None and n < lo:
        return f"must be at least {lo}"
    if hi is not None and n > hi:
        return f"must be at most {hi}"
    return None


def default_of(row: dict):
    """The row's default as setting() returns it (None when the row has none).
    TOML writes a float row's whole-number default as an int; parsing its text gives
    the type every other read of the setting has. A default the schema DESCRIBES
    ("<ENGINE_TTS_URL base>/...") is computed in code, so its read passes default=."""
    if "default" not in row:
        return None
    d = row["default"]
    if isinstance(d, bool):
        return d
    value, problem = parse(row, str(d))
    if problem is not None:
        raise ValueError(f"{row['name']}: schema default {d!r} is not a value ({problem}); "
                         f"its read must pass default=")
    return value


_SCHEMA_DEFAULT = object()


def setting(name: str, default=_SCHEMA_DEFAULT, env=os.environ):
    """The value of setting `name`: its environment text, parsed by its schema row.

    Unset or empty -> the row's default, or `default` for the few rows whose default
    is computed in code. A value that does not parse warns and falls back the same
    way: these are read at import, and an exception would crash-loop the service."""
    row = ROWS[name]
    fallback = default_of(row) if default is _SCHEMA_DEFAULT else default
    raw = (env.get(name) or "").strip()
    if not raw:
        return fallback
    value, problem = parse(row, raw)
    if problem is not None:
        logger.warning(f"{name}={raw!r} {problem}; using default {fallback!r}")
        return fallback
    if row["type"] == "flag" and not value:
        logger.info(f"{name}={raw.lower()} — disabled")  # as env_flag: never silently off
    return value
