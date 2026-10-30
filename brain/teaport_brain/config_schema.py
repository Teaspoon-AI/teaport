"""The configuration schema: loader + docs renderer.

config_schema.toml is the one table every consumer of teaport's settings reads —
the config UI, `teaport doctor`, and the generated part of the Settings reference
page of the docs site. This module is deliberately stdlib-only (tomllib) so
`python -m teaport_brain.config_schema` runs anywhere the repo is checked out, with
no venv and no pipecat.

    python -m teaport_brain.config_schema            # rewrite the Settings reference in place
    python -m teaport_brain.config_schema --check    # exit 1 if it is stale

The page is docs/src/content/docs/reference/settings.md (published at
https://docs.teaspoon.tech/reference/settings/). It keeps its hand-written frontmatter and
introduction; the region from the source comment to the closing marker is owned here. The
source comment fingerprints the schema, so a page cut from an older schema is stale even
when its tables happen to match. tests/test_config_docs.py runs --check so a schema edit
without a docs regeneration fails the suite instead of shipping stale docs.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import pathlib
import re
import sys
import tomllib

SCHEMA_PATH = pathlib.Path(__file__).with_name("config_schema.toml")
DOCS_PATH = pathlib.Path(__file__).resolve().parents[2] / "docs" / "src" / "content" / "docs" / "reference" / "settings.md"

EMPTY = '`""`'  # how an empty-string value/enum member is shown

BEGIN = "<!-- generated from brain/teaport_brain/config_schema.toml — edit that, then: python -m teaport_brain.config_schema -->"
END = "<!-- end generated -->"

# Written by hand into the page's prose, so they live here with the rest of what
# render() prints, not in the schema.
STORES_SUMMARY = ("Every setting lives in one of the files in this table; the second column names "
                  "the service that reads it, and the last says how a change takes effect.")
CONSTRAINTS_SUMMARY = ("The settings in this section depend on one another; each item names the "
                       "others that must change with it.")

# The source comment that opens the generated region, just above BEGIN: any citation of the schema, so a
# comment in an older form is replaced, not left above a new one.
SOURCE_RE = re.compile(r"<!-- source: brain/teaport_brain/config_schema\.toml:[^>]*-->\n\n\Z")


def source_comment() -> str:
    """Cite the schema by line count and content fingerprint: `1-N#HASH`.

    N counts real lines (a final newline ends one, it does not start another); HASH is
    the first 8 hex digits of sha256 over lines 1..N joined with newlines, none trailing.
    The docs lint computes the same fingerprint to tell whether the page still matches.
    """
    lines = SCHEMA_PATH.read_text(encoding="utf-8").split("\n")
    if lines[-1] == "":
        lines.pop()
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:8]
    return f"<!-- source: brain/teaport_brain/config_schema.toml:1-{len(lines)}#{digest} -->"


@functools.cache
def load() -> dict:
    with open(SCHEMA_PATH, "rb") as f:
        return normalise(tomllib.load(f))


def normalise(schema: dict) -> dict:
    """Give every consumer one shape: a row's `source`, when present, is a list of files.

    The TOML writes one file as a plain string and a setting read or written in more
    than one place as a list; nothing past this point has to tell the two apart.
    """
    for row in schema["settings"]:
        if isinstance(row.get("source"), str):
            row["source"] = [row["source"]]
    return schema


def settings_by_group(schema: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {g["id"]: [] for g in schema["groups"]}
    for row in schema["settings"]:
        out[row["group"]].append(row)
    return out


# ----------------------------------------------------------------- rendering

def _md(text: str) -> str:
    """Escape the one character that breaks a table cell."""
    return text.replace("|", "\\|")


def _default_cell(row: dict) -> str:
    cell = _default_value_cell(row)
    if "empty" in row:  # set-but-empty is a value, whatever unset means for the row
        cell += f" ({EMPTY} = {row['empty']})"
    return cell


def _default_value_cell(row: dict) -> str:
    """What the row reads as when unset."""
    t = row["type"]
    if "default_by" in row:
        (key, table), = row["default_by"].items()
        parts = ", ".join(f"{v} at {f'`{k}`' if k else EMPTY}" for k, v in table.items())
        return f"by `{key}`: {parts}"
    if row.get("required"):
        return "**required**"
    if t == "secret":
        return f"file `{row['file']}`" if "file" in row else "—"
    if "default" not in row:
        return "—"
    d = row["default"]
    if t == "flag":
        return "**on**" if d else "**off**"
    if t in ("int", "float"):
        cell = f"**{d}**"
        if "unit" in row:
            cell += f" {row['unit']}"
        lo, hi = row.get("min"), row.get("max")
        if lo is not None and hi is not None:
            cell += f" ({lo}–{hi})"
        elif lo is not None:
            cell += f" (≥ {lo})"
        elif hi is not None:
            cell += f" (≤ {hi})"
        return cell
    if d == "":
        return EMPTY
    return f"`{d}`"


def _desc_cell(row: dict) -> str:
    parts = [row.get("help", "").strip()]
    if row["type"] == "enum" and "values" in row:
        parts.append("One of " + ", ".join(f"`{v}`" if v else EMPTY for v in row["values"]) + ".")
    if "note" in row:
        parts.append(row["note"].strip())
    if row.get("tier") == "installer":
        parts.append("*Set by the installer.*")
    if row.get("kept"):
        parts.append("*Set by hand: `teaport sip configure` never writes it, and keeps it.*")
    return _md(" ".join(p for p in parts if p))


def _store_for_group(schema: dict, rows: list[dict]) -> str | None:
    stores = {r["store"] for r in rows}
    return stores.pop() if len(stores) == 1 else None


def render(schema: dict) -> str:
    lines: list[str] = [source_comment(), "", BEGIN, ""]

    lines += ["## Where settings live", "", STORES_SUMMARY, "",
              "| File | Read by | To apply a change |", "|---|---|---|"]
    for sid, st in schema["stores"].items():
        if not st["services"]:
            continue
        lines.append(f"| `{st['path']}` | {', '.join(f'`{s}`' for s in st['services'])} | {_md(st['apply'])} |")
    lines += ["",
              "Nothing hot-reloads: every value is fixed when its service starts, so a change",
              "is a restart of the service(s) in the second column.", ""]

    by_group = settings_by_group(schema)
    for g in schema["groups"]:
        rows = [r for r in by_group[g["id"]] if r.get("docs", True)]
        if not rows:
            continue
        lines.append(f"## {g['title']}")
        lines.append("")
        if "summary" in g:
            lines += [g["summary"], ""]
        store = _store_for_group(schema, rows)
        sub = []
        if store:
            sub.append(f"In `{schema['stores'][store]['path']}`.")
        if "blurb" in g:
            sub.append(g["blurb"])
        if sub:
            lines += [" ".join(sub), ""]
        lines += ["| Setting | Default | Description |", "|---|---|---|"]
        for r in rows:
            lines.append(f"| `{r['name']}` | {_default_cell(r)} | {_desc_cell(r)} |")
        lines.append("")

    cons = schema.get("constraints", [])
    if cons:
        lines += ["## Settings that constrain each other", "", CONSTRAINTS_SUMMARY, ""]
        for c in cons:
            lines.append(f"- {_md(c['message'])}")
        lines.append("")

    lines.append(END)
    return "\n".join(lines) + "\n"


def splice(doc: str, generated: str) -> str:
    """Replace the generated region of the Settings page with `generated`.

    The region runs from the source comment, when one sits just above BEGIN, to END.
    """
    begin = doc.find(BEGIN)
    end = doc.find(END)
    if begin < 0 or end < 0 or end < begin:
        raise ValueError(f"{DOCS_PATH}: missing the generated-region markers")
    start = begin
    m = SOURCE_RE.search(doc[:begin])
    if m:
        start = m.start()
    end += len(END)
    # Consume the newline after END so the splice is idempotent.
    if doc[end:end + 1] == "\n":
        end += 1
    return doc[:start] + generated + doc[end:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", nargs="?", default=DOCS_PATH, type=pathlib.Path)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the file is not what the schema renders; write nothing")
    args = ap.parse_args(argv)
    current = args.path.read_text(encoding="utf-8")
    wanted = splice(current, render(load()))
    if wanted == current:
        print(f"{args.path}: up to date")
        return 0
    if args.check:
        print(f"{args.path}: STALE — run: python -m teaport_brain.config_schema", file=sys.stderr)
        return 1
    args.path.write_text(wanted, encoding="utf-8")
    print(f"{args.path}: regenerated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
