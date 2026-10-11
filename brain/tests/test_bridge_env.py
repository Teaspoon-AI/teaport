#
# The Discord bridge reads bridge.env by the brain's rule (bridge/discord/env.js), so the
# config page, which shows a value as the brain reads it (settings.canonical), shows what
# the bridge runs on. Before, a blank BRIDGE_PRIME_MS was 0 to the bridge and the default
# (40) to the page, "abc" was NaN, and a quoted " 123 " guild id was used padded.
#
# For every bridge.env row and a matrix of hand-written values (blank, padded with each
# whitespace character Python strips, and U+FEFF, which it does not; numbers in several
# spellings; values not of the type), node's reading must be the brain's: not set where
# the brain reads not set or ignores the value, else the same text. And index.js must read
# each row through env.js with the row's own type.
#
# Run: python test_bridge_env.py   (or via pytest test_suite.py). Skipped without node.
#
import json
import pathlib
import re
import shutil
import subprocess
import sys

import appliance

BRAIN = pathlib.Path(__file__).resolve().parent.parent
BRIDGE = BRAIN.parent / "bridge" / "discord"
sys.path.insert(0, str(BRAIN))

from teaport_brain import settings  # noqa: E402

CHECKED_TYPES = {"int", "snowflake", "ws_url"}  # env.js TYPES; any other type is text

VALUES = ["", " ", "\t", "40", " 40 ", "+5", "007", "-3", "1.5", "1e2", "0x10", "abc",
          "١٢", "123", "12a", "ws://127.0.0.1:7861/talk", " wss://h/x ", "ws://a b",
          "http://x", "ws://", "~/tok", "af_bella", "two words", "﻿5", "5﻿"]
VALUES += [f"{c}5{c}" for c in map(chr, range(0x110000)) if c.isspace()]


def bridge_reads() -> dict[str, str | None]:
    """{NAME: the type index.js passes to setting(), or None} for its setting() calls."""
    src = (BRIDGE / "index.js").read_text(encoding="utf-8")
    return {m.group(1): m.group(2) for m in
            re.finditer(r'\bsetting\("([A-Z_]+)"(?:,\s*"(\w+)")?\)', src)}


def node_reading(cases: list[dict]) -> list[str | None]:
    script = (f"import {{ setting }} from {json.dumps((BRIDGE / 'env.js').as_uri())};\n"
              "let s = ''; process.stdin.on('data', (d) => (s += d)).on('end', () => {\n"
              "  const out = JSON.parse(s).map(({ name, type, value }) =>\n"
              "    setting(name, type, value === null ? {} : { [name]: value }) ?? null);\n"
              "  process.stdout.write(JSON.stringify(out));\n"
              "});\n")
    r = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(cases),
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def test_index_reads_each_row_through_env_js_by_its_type():
    reads = bridge_reads()
    rows = [r for r in settings.ROWS.values() if r["store"] == "bridge_env"]
    assert rows
    for row in rows:
        assert row["name"] in reads, f"index.js does not read {row['name']} through env.js"
        want = row["type"] if row["type"] in CHECKED_TYPES else None
        assert reads[row["name"]] == want, (row["name"], reads[row["name"]], want)
    src = (BRIDGE / "index.js").read_text(encoding="utf-8")
    assert "process.env.BRIDGE_" not in src and "process.env.DISCORD_BOT_TOKEN" not in src


def test_the_bridge_reads_a_value_as_the_brain_does():
    rows = [r for r in settings.ROWS.values() if r["store"] == "bridge_env"]
    types = bridge_reads()
    cases, want = [], []
    for row in rows:
        for value in [None] + VALUES:
            cases.append({"name": row["name"], "type": types[row["name"]], "value": value})
            text, _ = settings.canonical(row, value)
            want.append(None if text is None else settings.normalize(row, value))
    got = node_reading(cases)
    wrong = [(c["name"], c["value"], g, w) for c, g, w in zip(cases, got, want) if g != w]
    assert not wrong, f"{len(wrong)} readings differ, e.g. {wrong[:5]}"


def main() -> int:
    if not shutil.which("node"):
        appliance.skip("node is not installed: the Discord bridge's reader cannot run here")
    for fn in (test_index_reads_each_row_through_env_js_by_its_type,
               test_the_bridge_reads_a_value_as_the_brain_does):
        fn()
        print("ok", fn.__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
