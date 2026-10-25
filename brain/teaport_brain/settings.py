#
# settings.py — read a setting through its config_schema.toml row.
#
# The schema declares every knob once: its type, its default, its bounds. A read with
# its own literal default — `float(os.getenv("TEAPORT_FOLLOWUP_QUIET_S", "0.7"))` —
# declared it a second time, kept in step with the row by nothing but care.
# setting("TEAPORT_FOLLOWUP_QUIET_S") takes the type and the default from the row, so
# there is one declaration.
#
# These values live in /etc/teaport/brain.env (and its siblings), which installer
# repairs preserve verbatim. Three rules follow, and setting() keeps them for every
# read rather than each call site remembering them:
#
#   An unreadable value warns and falls back to the default; it never raises. A bare
#   int()/float() at import turned one operator typo ("", "off", "2.5") into an
#   import-time ValueError that crash-loops the whole service, and re-running the
#   installer cannot clear it. A bad JSON value is worse, not better: LLM_EXTRA_BODY is
#   read inside build_agent_session(), so the process stays up and healthy-looking
#   while EVERY session dies at construction (observed 2026-08-28, from a wrapper that
#   `source`d brain.env and stripped the quotes).
#
#   An EMPTY value means "not set". `TEAPORT_LLM_TEXT_GUARD=` is a plausible hand-edit,
#   and it must not switch a safety guard off. The exception is a row whose schema says
#   empty is a value (empty_is_value): an enum that lists "" among its values
#   (`LLM_REASONING_EFFORT=""` sends no effort at all), or any other row with an `empty`
#   field naming what it means (`TEAPORT_SIP_SOCKET=""` is "off"). There a set-but-empty
#   value reads as "", unset is still the default, and the config UI writes "" as a value.
#
#   A flag has ONE truth table — pipecat's env_truthy, which this process already uses
#   for its PIPECAT_* flags — and a flag that is off says so in the journal. Three
#   modules had once grown three different tables: the same empty value disabled the
#   degeneracy guard and enabled the thinking sound, and "no" disabled one but not the
#   other.
#
# normalize() is the ONE answer to "what text does this file value read as" (stripped;
# blank is unset or ""), parse() to "is this text a value of this row's type", and
# canonical() to "what is that value, in its one spelling" -- pure data in and out.
# setting() reads with normalize() and parse(); the config UI shows a value as canonical()
# gives it and writes what canonical() makes of what was typed, so a value the page saves
# is exactly a value the brain reads, and the page shows the brain's reading, not the
# file's spelling (TRUNCATE is truncate, yes is 1), with no parser of its own. Bounds
# are a separate question (out_of_bounds): the page enforces them; at runtime they are
# not enforced yet.
#
#   A secret is read by the same rule, from its env var or from its file (secret_file).
#   Whitespace around one was never part of it: every other reader strips it (the
#   OpenClaw token, the Discord bridge's, the SIP gateway's conf parser, the Wi-Fi
#   setup's password check, /talk's bearer header), and a padded key sent in an HTTP
#   header is refused outright (h11: "Illegal header value"), which is what a padded
#   LLM_API_KEY read verbatim did to every LLM request.
#
# A name with no row is a KeyError at import — a setting nobody can see, document or
# validate fails the first test that imports its module instead of shipping.
#
# A RENAMED setting keeps a row under its old name (tier diag, its help naming the new
# one), and its read names both: setting("NEW", former="OLD"). While NEW is unset and OLD
# is set, OLD's value is read, with a warning naming the rename, so an env file written
# before it keeps meaning what it said -- a switch turned off stays off -- instead of
# being silently ignored. A RETIRED one (nothing reads it any more) keeps a row the same
# way, and the module that dropped it warns when it is still set (reply_hold.py's
# TEAPORT_REPLY_HOLD).
#
import json
import math
import os
import re

from loguru import logger

from teaport_brain import config_schema

ROWS: dict[str, dict] = {r["name"]: r for r in config_schema.load()["settings"]}

# pipecat's env_truthy table (tests/test_settings.py pins the two together, so a
# TEAPORT_* flag and a PIPECAT_* flag read alike). Empty is not in it here: an empty
# value means "not set" everywhere in teaport.
FLAG_TRUE = frozenset({"1", "true", "yes", "y", "on"})
FLAG_FALSE = frozenset({"0", "false", "no", "n", "off"})


def _int(text: str, row: dict):
    # ASCII digits, as every other reader of these files takes them (atoi, the bridge's
    # Number()); Python's \d and int() would also take "\u0665" as 5.
    if not re.fullmatch(r"[+-]?[0-9]+", text):
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
    if "accepts" in row:  # a flag read as these words alone (LEDGER_TRACE)
        if word in row["accepts"]:
            return True, None
        if word in FLAG_FALSE:  # off is off; only the other "on" words are suspect
            return False, None
        return None, f"must be one of {', '.join(sorted(row['accepts']))} (or an off word)"
    if word in FLAG_TRUE or word in FLAG_FALSE:
        return word in FLAG_TRUE, None
    return None, f"must be one of {', '.join(sorted(FLAG_TRUE | FLAG_FALSE))}"


def _enum(text: str, row: dict):
    # Case-insensitive; the value is the schema's spelling.
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


def _path(text: str, row: dict):
    # systemd's EnvironmentFile does not expand "~", and neither does a file read.
    return os.path.expanduser(text), None


PARSERS = {
    "int": _int,
    "float": _float,
    "flag": _flag,
    "enum": _enum,
    "json": _json,
    "url": _matching(r"https?://\S+", "must start with http:// or https://"),
    "ws_url": _matching(r"wss?://\S+", "must start with ws:// or wss://"),
    "snowflake": _matching(r"[0-9]+", "must be a Discord id (digits only)"),
    "string": _text,
    "path": _path,
    "dir": _path,
    "secret": _text,
}


def empty_is_value(row: dict) -> bool:
    """Whether a set-but-empty value is the value "" rather than "not set". An enum says
    so by listing "" (the page offers it with the others); any other row by its `empty`
    field, the words for what "" means there."""
    return "empty" in row or "" in row.get("values", ())


def normalize(row: dict, given: str | None) -> str | None:
    """The text a value as it stands in the env file (or a secret's file) is read as:
    None for "not set" (absent, or blank where empty is not a value), "" where
    empty_is_value(row) and it is blank, else the text stripped. setting() parses this
    text; canonical() spells it for the config page."""
    if given is None or not given.strip():
        return "" if given is not None and empty_is_value(row) else None
    return given.strip()


def parse(row: dict, text: str):
    """(value, None) when `text` is a value of `row`'s type, else (None, the reason).

    `text` is a non-empty normalize()d value. What a blank one means is normalize's
    answer: "not set", or "" where the row says empty is a value."""
    return PARSERS[row["type"]](text, row)


def canonical(row: dict, given: str | None) -> tuple[str | None, str | None]:
    """The brain's reading of env-file text `given`, as text: (text, None), where text is
    the value's one spelling and reads back as the same value -- an enum in the schema's
    case, a flag 1 or 0 (an `accepts` flag's own on word), a number without padding, a
    sign or a trailing .0, JSON compact on one line, any other text as normalize() gives
    it; ("", None) where empty is a value; (None, None) when it is not set; (None, the
    reason) when it is set but not a value, which setting() warns about and reads as not
    set. The config page shows this and writes it, so it needs no parser of its own."""
    text = normalize(row, given)
    if not text:
        return text, None
    value, problem = parse(row, text)
    if problem is not None:
        return None, problem
    t = row["type"]
    if t == "flag":
        return (row.get("accepts", ["1"])[0] if value else "0"), None
    if t == "float":
        return repr(value).removesuffix(".0"), None
    if t == "json":
        return json.dumps(value, separators=(",", ":")), None
    return (str(value) if t in ("int", "enum") else text), None


def secret_file(name: str) -> str | None:
    """Secret `name` from the file its row names (`file`), the one its env var shadows:
    read by normalize() like any value, so None when the row has no file, or the file is
    missing, blank or unreadable (that one with a warning)."""
    row = ROWS[name]
    if "file" not in row:
        return None
    path = os.path.expanduser(row["file"])
    try:
        with open(path, encoding="utf-8") as f:
            return normalize(row, f.read())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning(f"{name}: cannot read {path}: {e}")
        return None


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
    ("<ENGINE_TTS_URL base>/...") is computed in code, so its read passes default=;
    one that parses anyway (a path, a URL) is refused all the same, or a read that
    forgot default= would get the description as its value."""
    if "default" not in row:
        return None
    d = row["default"]
    if isinstance(d, bool):
        return d
    if "<" in str(d) or "$" in str(d):
        raise ValueError(f"{row['name']}: schema default {d!r} describes a value the code "
                         f"computes; its read must pass default=")
    value, problem = parse(row, str(d))
    if problem is not None:
        raise ValueError(f"{row['name']}: schema default {d!r} is not a value ({problem}); "
                         f"its read must pass default=")
    return value


_SCHEMA_DEFAULT = object()


def setting(name: str, default=_SCHEMA_DEFAULT, env=os.environ, *, former: str | None = None):
    """The value of setting `name`: its environment text, parsed by its schema row.

    Unset or empty -> the row's default, or `default` for the few rows whose default
    is computed in code; set-but-empty is "" instead where empty_is_value(row). A value
    that does not parse warns and falls back the same way: these are read at import,
    and an exception would crash-loop the service. normalize() says what text a value
    is read as (stripped). `former`: the name it had before a rename (module comment),
    read in its place, with a warning, while `name` is unset."""
    row = ROWS[name]
    fallback = default_of(row) if default is _SCHEMA_DEFAULT else default
    raw = normalize(row, env.get(name))
    if raw is None and former is not None \
            and normalize(ROWS[former], env.get(former)) is not None:
        logger.warning(f"{former} is renamed {name}: its value is read for now; rename it "
                       "in the env file")
        return setting(former, fallback, env)
    if raw is None:
        return fallback
    if raw == "":
        return ""  # set, and the schema says empty is a value
    value, problem = parse(row, raw)
    if problem is not None:
        if row["type"] == "json":  # not echoed: an extra_body can carry a credential
            logger.warning(f"{name} ({len(raw)} chars) {problem}; using default "
                           f"{fallback!r}. If it came from a wrapper that `source`d the env "
                           f"file, the quotes were stripped: parse the file literally instead.")
            return fallback
        logger.warning(f"{name}={raw!r} {problem}; using default {fallback!r}.")
        return fallback
    if row["type"] == "flag" and not value:
        logger.info(f"{name}={raw.lower()} — disabled")  # never silently off
    return value
