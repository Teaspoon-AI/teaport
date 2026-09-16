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
# Run: python test_config_schema.py   (or via pytest)
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


def env_reads_in_code() -> set[str]:
    found: set[str] = set()
    for p in PKG.glob("*.py"):
        found |= set(READ_RE.findall(p.read_text()))
    return found


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

    for p in problems:
        print("FAIL", p)
    print(f"{len(rows)} rows, {len(code)} env reads in code, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
