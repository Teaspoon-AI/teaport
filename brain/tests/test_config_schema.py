#
# Schema drift gate: every environment variable the brain reads has a row in
# teaport_brain/config_schema.toml, and every row is well-formed.
#
# The schema is the ONE table the config UI, `teaport doctor` and the CONFIG.md
# generator read, so an os.getenv() added without a row is a setting nobody can
# see, document or validate — exactly the state docs/CONFIG.md was in when the
# table was first drafted (25 knobs read by the code and documented nowhere).
# The read-site regex mirrors settings.setting and the bare os.getenv / os.environ
# forms, either quote (a read inside an f-string is single-quoted); a new reader needs
# adding here too. Comments are not reads: a name mentioned in one must not stand in
# for a read that has gone.
#
# A row the brain reads needs no pointer to where: `setting("NAME")` is the read, and
# a search for the name finds it. Rows it does not read (the installer's, the units',
# the CLI's, the Discord bridge's, a library's) name the file that does in `source` --
# a file, not a line, because line numbers went stale under every edit above them
# (17 of 113 when they were first checked, and ~60 per refactor after that). A setting
# read or written in more than one place (the engine unit reads ENGINE_DELAY,
# install.sh writes it) lists every file; config_schema.normalise makes a lone string
# a one-file list, so the check below always walks a list.
# Each file must also name the setting, as a whole word outside a comment line (the
# sources are shell, JS, a systemd template and Python: `#` and `//` cover them), so a
# rename or removal there fails here instead of leaving the row pointing at a file
# that no longer reads it. A sip_conf key must start a `key=` line, as the conf
# template in cli/teaport writes it: the bare words (`aec`, `register`, `password`)
# are also subcommands, flags and prompts there. A library's row (`consumer`) names
# our file that sets it, which names it too.
# The check is that each listed file NAMES the setting, not that it reads or writes
# it: install.sh also names ENGINE_PORT as its own shell variable and BRIDGE_GUILD_ID
# in a sed read-back and a warning, so deleting the line that writes either one to
# its env file still passes here.
#
# Run: python test_config_schema.py   (or via pytest)
#
import collections
import io
import pathlib
import re
import sys
import tokenize

PKG = pathlib.Path(__file__).resolve().parent.parent / "teaport_brain"
REPO = PKG.parent.parent

sys.path.insert(0, str(PKG.parent))
from teaport_brain import config_schema  # noqa: E402  (stdlib-only: tomllib)

READ_RE = re.compile(
    r'(?:getenv|\bsetting|environ\.get|environ\[)\(?\s*(["\'])([A-Z][A-Z0-9_]+)\1'
)
TYPES = {"string", "url", "ws_url", "path", "dir", "int", "float", "flag", "enum",
         "json", "secret", "snowflake"}
TIERS = {"setup", "tuning", "diag", "installer"}
READS = {"startup", "session", "unit"}


# Read by the code but not settings: sudo's own variables, seen by config_apply, and
# the readiness socket systemd hands a Type=notify unit (sdnotify).
NOT_SETTINGS = {"SUDO_GID", "SUDO_UID", "SUDO_USER", "NOTIFY_SOCKET"}


def empty_problem(r: dict) -> str | None:
    """What is wrong with a row's `empty` field, or None."""
    if "empty" not in r:
        return None
    # setting() returns "" for it: only a text row may (an enum lists "" in values).
    if (r["type"] not in ("string", "path", "dir", "url", "ws_url")
            or not isinstance(r["empty"], str) or not r["empty"]):
        return "empty is the words for what \"\" means, on a text row"
    # Unset reads as the default and set-but-empty as "": with default "" they are one
    # value, and `empty` would name a difference there is not. `required` may go with
    # `empty`: the operator must choose, and off is a choice.
    if r.get("default") == "":
        return "empty with default \"\": unset already reads as \"\""
    return None


def without_comments(source: str) -> str:
    lines = source.splitlines(keepends=True)
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            (row, col), (_, end) = tok.start, tok.end
            lines[row - 1] = lines[row - 1][:col] + " " * (end - col) + lines[row - 1][end:]
    return "".join(lines)


def reads_in(source: str) -> set[str]:
    return {m.group(2) for m in READ_RE.finditer(without_comments(source))}


def env_reads_in_code() -> set[str]:
    found: set[str] = set()
    for p in PKG.glob("*.py"):
        found |= reads_in(p.read_text())
    return found - NOT_SETTINGS


def source_problem(name: str, src: str, store: str) -> str | None:
    """Why `src` cannot be the source of the row `name` in `store`, or None when it can."""
    path = REPO / src
    if re.search(r":\d+$", src) or not path.is_file():
        return f"source {src!r} must be an existing file, repo-relative, no line"
    text = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith(("#", "//")))
    if store == "sip_conf":
        pattern = rf"^{re.escape(name)}="
    else:
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])"
    if not re.search(pattern, text, re.M):
        return f"source {src!r} never names it"
    return None


def sources_problems(row: dict) -> list[str]:
    """Why the files a (normalised) row's `source` lists cannot all be its sources."""
    srcs = row["source"]
    if not isinstance(srcs, list) or not srcs or not all(isinstance(s, str) for s in srcs) \
            or len(set(srcs)) < len(srcs):
        return [f"source must be a file or a list of distinct files, not {srcs!r}"]
    return [why for src in srcs if (why := source_problem(row["name"], src, row["store"]))]


def main() -> int:
    schema = config_schema.load()
    rows = schema["settings"]
    names = [r["name"] for r in rows]
    stores = set(schema["stores"])
    groups = {g["id"] for g in schema["groups"]}
    problems: list[str] = []

    for n, c in collections.Counter(names).items():
        if c > 1:
            problems.append(f"duplicate row: {n}")
    for r in rows:
        n = r["name"]
        if r["store"] not in stores:
            problems.append(f"{n}: unknown store {r['store']}")
        if r["group"] not in groups:
            problems.append(f"{n}: unknown group {r['group']}")
        if r["type"] not in TYPES:
            problems.append(f"{n}: unknown type {r['type']}")
        if r["tier"] not in TIERS:
            problems.append(f"{n}: unknown tier {r['tier']}")
        if "help" not in r:
            problems.append(f"{n}: needs help")
        if r["store"] in ("brain_env", "engine_env") and r.get("read") not in READS:
            problems.append(f"{n}: env rows need read = startup|session|unit")
        if r["type"] == "enum":
            vals = r.get("values")
            if not vals:
                problems.append(f"{n}: enum without values")
            elif "default" in r and str(r["default"]) not in vals:
                problems.append(f"{n}: default {r['default']!r} not in values")
        if why := empty_problem(r):
            problems.append(f"{n}: {why}")
        if r["type"] == "secret" and r["store"] != "sip_conf" and "file" not in r \
                and r["tier"] != "installer":
            problems.append(f"{n}: operator secret must name the file the UI writes")
        if "min" in r and "max" in r and r["min"] > r["max"]:
            problems.append(f"{n}: min > max")

    for bad in ({"type": "int", "empty": "off"}, {"type": "path", "empty": ""},
                {"type": "path", "empty": "off", "default": ""}):
        if empty_problem(bad) is None:
            problems.append(f"self-test: empty accepted on {bad}")
    if empty_problem({"type": "path", "empty": "off", "required": True}) is not None:
        problems.append("self-test: empty refused on a required row")

    for c in schema.get("constraints", []):
        refs = []
        for k in ("lhs", "rhs", "names"):
            v = c.get(k, [])
            refs += [v] if isinstance(v, str) else list(v)
        for ref in refs:
            if ref not in names:
                problems.append(f"constraint references unknown setting {ref}")

    code = env_reads_in_code()
    for n in sorted(code - set(names)):
        problems.append(f"read in code, no schema row: {n}")
    brain_rows = {r["name"] for r in rows
                  if r["store"] == "brain_env" and r.get("read") != "unit" and "consumer" not in r}
    for n in sorted(brain_rows - code):
        problems.append(f"schema row, never read by the brain: {n}")

    for r in rows:
        n, srcs = r["name"], r.get("source")
        if n in code and srcs is not None:
            problems.append(f"{n}: the brain reads it, so no source (searching the name finds it)")
        elif n not in code and srcs is None:
            problems.append(f"{n}: not read by the brain, so source must name the file that reads it")
        elif srcs is not None:
            problems += [f"{n}: {why}" for why in sources_problems(r)]

    # The source check must be able to fail: a real file that reads other settings, a
    # name that is only a prefix or a suffix of one the file reads, a name only a
    # comment mentions, a sip key that is a bare word but no `key=` line, a missing
    # file and a line suffix. They lean on what those files hold today (TTS_CTX in the
    # engine unit, MALLOC_ARENA_MAX in install.sh, SIP_ANSWER_AFTER_SECS in a comment
    # of cli/teaport): if one starts failing after an edit there, pick a new example.
    for n, src, store in (("ENGINE_PORT", "bridge/discord/index.js", "engine_env"),
                          ("TTS_CT", "systemd/teaport-engine.service.in", "engine_env"),
                          ("ARENA_MAX", "install.sh", "brain_env"),
                          ("SIP_ANSWER_AFTER_SECS", "cli/teaport", "brain_env"),
                          ("status", "cli/teaport", "sip_conf"),
                          ("ENGINE_PORT", "install.sh.missing", "engine_env"),
                          ("ENGINE_PORT", "systemd/teaport-engine.service.in:1", "engine_env")):
        if source_problem(n, src, store) is None:
            problems.append(f"self-test: source {src!r} accepted for {n}")

    # A list is checked file by file: one listed file that never names the setting fails
    # the row wherever it sits (install.sh writes HF_HUB_OFFLINE, the Discord bridge
    # never names it); an empty or repeating list, or a TOML value that is not a list
    # (an inline table, an int, a bool), is not a list of sources; a lone string is one.
    def row(source):
        r = {"name": "HF_HUB_OFFLINE", "store": "brain_env", "source": source}
        return config_schema.normalise({"settings": [r]})["settings"][0]
    for bad in (["install.sh", "bridge/discord/index.js"], ["bridge/discord/index.js", "install.sh"],
                [], ["install.sh", "install.sh"], {"file": "install.sh"}, 1, True):
        if not sources_problems(row(bad)):
            problems.append(f"self-test: source {bad!r} accepted for HF_HUB_OFFLINE")
    if why := sources_problems(row("install.sh")):
        problems.append(f"self-test: a single-file source is rejected: {why}")

    for p in problems:
        print("FAIL", p)
    print(f"{len(rows)} rows, {len(code)} env reads in code, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
