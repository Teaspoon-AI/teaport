#
# The Discord bridge reads bridge.env by the brain's rule (bridge/discord/env.js), so the
# config page, which shows a value as the brain reads it (settings.canonical), shows what
# the bridge runs on. Before, a blank BRIDGE_PRIME_MS was 0 to the bridge and the default
# (40) to the page, "abc" was NaN, and a quoted " 123 " guild id was used padded.
#
# For every bridge.env row and a matrix of hand-written values (blank, padded with each
# whitespace character Python strips, and U+FEFF, which it does not; numbers in several
# spellings; values not of the type), node's reading must be the brain's: not set where
# the brain reads not set or ignores the value, else the same text -- set in node's own
# environment, read through env.js's setting() as index.js calls it. And index.js must
# import that setting() and read each row through it with the row's own type.
#
# Run: python test_bridge_env.py   (or via pytest test_suite.py). Skipped without node.
#
import json
import os
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


def node_reading(rows: dict[str, str | None], value: str | None) -> dict[str, str | None]:
    """{NAME: what setting(NAME, type) returns} in a node whose environment holds `value`
    for every name in `rows` ({NAME: type}), or none of them for None."""
    env = {k: v for k, v in os.environ.items() if k not in rows}
    if value is not None:
        env.update(dict.fromkeys(rows, value))
    script = (f"import {{ setting }} from {json.dumps((BRIDGE / 'env.js').as_uri())};\n"
              f"const rows = {json.dumps(rows)};\n"
              "process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(rows).map(\n"
              "  ([name, type]) => [name, setting(name, type) ?? null]))));\n")
    r = subprocess.run(["node", "--input-type=module", "-e", script], env=env,
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
    # The setting() it calls is env.js's, the one tested below: imported, not its own.
    assert re.search(r"""import\s*\{[^}]*\bsetting\b[^}]*\}\s*from\s*["']\./env\.js["']""", src)
    assert not re.search(r"\b(?:function|const|let|var)\s+setting\b", src)


def test_the_bridge_reads_a_value_as_the_brain_does():
    rows = [r for r in settings.ROWS.values() if r["store"] == "bridge_env"]
    types = bridge_reads()
    wrong = []
    for value in [None] + VALUES:
        got = node_reading({r["name"]: types[r["name"]] for r in rows}, value)
        for row in rows:
            text, _ = settings.canonical(row, value)
            want = None if text is None else settings.normalize(row, value)
            if got[row["name"]] != want:
                wrong.append((row["name"], value, got[row["name"]], want))
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
