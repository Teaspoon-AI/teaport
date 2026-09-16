#
# Schema drift gate: every environment variable the brain reads has a row in
# teaport_brain/config_schema.toml, and every row is well-formed.
#
# The schema is the ONE table the config UI, `teaport doctor` and the CONFIG.md
# generator read, so an os.getenv() added without a row is a setting nobody can
# see, document or validate — exactly the state docs/CONFIG.md was in when the
# table was first drafted (25 knobs read by the code and documented nowhere).
# The read-site regex mirrors the helper shapes in teaport_brain/env.py plus the
# bare os.getenv / os.environ forms; a new helper needs adding here too.
#
# Every row's `source` ("file.py:N") is checked too: the named line must be the read
# site (the setting's name, quoted). Lines move under every edit above them and the
# pointer is only ever fixed by hand for the setting being touched -- 17 of 113 were
# stale when this check was added. `--fix` rewrites the stale ones in place.
#
# Run: python test_config_schema.py [--fix]   (or via pytest)
#
import collections
import os
import pathlib
import re
import sys
import tomllib

PKG = pathlib.Path(__file__).resolve().parent.parent / "teaport_brain"
SCHEMA = PKG / "config_schema.toml"

READ_RE = re.compile(
    r'(?:getenv|env_flag|env_num|env_json|environ\.get|environ\[)\(?\s*"([A-Z][A-Z0-9_]+)"'
)
TYPES = {"string", "url", "ws_url", "path", "dir", "int", "float", "flag", "enum",
         "json", "secret", "snowflake"}
TIERS = {"setup", "tuning", "diag", "installer"}
READS = {"startup", "session", "unit"}


# Read by the code but not settings: sudo's own variables, seen by config_apply.
NOT_SETTINGS = {"SUDO_GID", "SUDO_UID", "SUDO_USER"}


def env_reads_in_code() -> set[str]:
    found: set[str] = set()
    for p in PKG.glob("*.py"):
        found |= set(READ_RE.findall(p.read_text()))
    return found - NOT_SETTINGS


SOURCE_RE = re.compile(r"(.+?):(\d+)$")


def source_drift(rows) -> list[tuple[dict, str, int | None]]:
    """(row, file, correct line) for every row whose source points at the wrong line
    of a Python module; the correct line is None when the read site is not found."""
    stale = []
    for r in rows:
        m = SOURCE_RE.fullmatch(r["source"])
        if not m or not m.group(1).endswith(".py"):
            continue
        path, n = PKG / m.group(1), int(m.group(2))
        if not path.exists():
            stale.append((r, m.group(1), None))
            continue
        lines = path.read_text().splitlines()
        quoted = f'"{r["name"]}"'
        if 0 < n <= len(lines) and quoted in lines[n - 1]:
            continue
        hits = [i + 1 for i, line in enumerate(lines) if quoted in line]
        stale.append((r, m.group(1), hits[0] if hits else None))
    return stale


def fix_sources(stale) -> int:
    """Rewrite each stale row's source line in place -- located from the row's own
    `name =` line, not by the old value, which another row may legitimately hold."""
    lines = SCHEMA.read_text().splitlines(keepends=True)
    fixed = 0
    for r, path, n in stale:
        if n is None:
            continue
        at = lines.index(f'name = "{r["name"]}"\n')
        while not lines[at].startswith("source = "):
            at += 1
        lines[at] = f'source = "{path}:{n}"\n'
        fixed += 1
    SCHEMA.write_text("".join(lines))
    return fixed


def main(argv=()) -> int:
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
        if "help" not in r or "source" not in r:
            problems.append(f"{n}: needs help and source")
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

    stale = source_drift(rows)
    if stale and "--fix" in argv:
        print(f"fixed {fix_sources(stale)} stale source line(s) in {SCHEMA.name}")
        stale = [t for t in stale if t[2] is None]
    for r, path, n in stale:
        problems.append(f"{r['name']}: source {r['source']} is not the read site"
                        + (f" (it is {path}:{n}; --fix rewrites it)" if n else
                           " (no read site found)"))

    for p in problems:
        print("FAIL", p)
    print(f"{len(rows)} rows, {len(code)} env reads in code, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
