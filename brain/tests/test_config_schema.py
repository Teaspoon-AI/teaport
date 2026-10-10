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
# (17 of 113 when they were first checked, and ~60 per refactor after that).
# The file must also name the setting, as a whole word (a plain search: the sources are
# shell, JS, a systemd template and Python), so a rename or removal there fails here
# instead of leaving the row pointing at a file that no longer reads it. A library's
# row (`consumer`) names our file that sets it, which names it too.
#
# Run: python test_config_schema.py   (or via pytest)
#
import collections
import io
import pathlib
import re
import sys
import tokenize
import tomllib

PKG = pathlib.Path(__file__).resolve().parent.parent / "teaport_brain"
SCHEMA = PKG / "config_schema.toml"
REPO = PKG.parent.parent

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


def source_problem(name: str, src: str) -> str | None:
    """Why `src` cannot be the source of the row `name`, or None when it can."""
    path = REPO / src
    if re.search(r":\d+$", src) or not path.is_file():
        return f"source {src!r} must be an existing file, repo-relative, no line"
    if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", path.read_text()):
        return f"source {src!r} never names it"
    return None


def main() -> int:
    schema = tomllib.load(open(SCHEMA, "rb"))
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
        if r["type"] == "secret" and r["store"] != "sip_conf" and "file" not in r \
                and r["tier"] != "installer":
            problems.append(f"{n}: operator secret must name the file the UI writes")
        if "min" in r and "max" in r and r["min"] > r["max"]:
            problems.append(f"{n}: min > max")

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
        n, src = r["name"], r.get("source")
        if n in code and src is not None:
            problems.append(f"{n}: the brain reads it, so no source (searching the name finds it)")
        elif n not in code and src is None:
            problems.append(f"{n}: not read by the brain, so source must name the file that reads it")
        elif src is not None and (why := source_problem(n, src)):
            problems.append(f"{n}: {why}")

    # The source check must be able to fail: a real file that reads other settings, a
    # name that is only a prefix of one the file reads (TTS_CTX), and a line suffix.
    for n, src in (("ENGINE_PORT", "bridge/discord/index.js"),
                   ("TTS_CT", "systemd/teaport-engine.service.in"),
                   ("ENGINE_PORT", "systemd/teaport-engine.service.in:1")):
        if source_problem(n, src) is None:
            problems.append(f"self-test: source {src!r} accepted for {n}")

    for p in problems:
        print("FAIL", p)
    print(f"{len(rows)} rows, {len(code)} env reads in code, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
