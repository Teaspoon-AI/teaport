"""The configuration schema: loader + docs renderer.

config_schema.toml is the one table every consumer of teaport's settings reads —
the config UI, `teaport doctor`, and the generated part of docs/CONFIG.md. This
module is deliberately stdlib-only (tomllib) so `python -m teaport_brain.config_schema`
runs anywhere the repo is checked out, with no venv and no pipecat.

    python -m teaport_brain.config_schema            # rewrite docs/CONFIG.md in place
    python -m teaport_brain.config_schema --check    # exit 1 if docs/CONFIG.md is stale

docs/CONFIG.md keeps its hand-written prose (where config lives, SIP telephony,
the single STT slot); only the region between the two marker comments is owned
here. tests/test_config_docs.py runs --check so a schema edit without a docs
regeneration fails the suite instead of shipping stale docs.
"""
from __future__ import annotations

import argparse
import functools
import pathlib
import sys
import tomllib

SCHEMA_PATH = pathlib.Path(__file__).with_name("config_schema.toml")
DOCS_PATH = pathlib.Path(__file__).resolve().parents[2] / "docs" / "CONFIG.md"

EMPTY = '`""`'  # how an empty-string value/enum member is shown

BEGIN = "<!-- generated from brain/teaport_brain/config_schema.toml — edit that, then: python -m teaport_brain.config_schema -->"
END = "<!-- end generated -->"


@functools.cache
def load() -> dict:
    with open(SCHEMA_PATH, "rb") as f:
        return tomllib.load(f)


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
    return _md(" ".join(p for p in parts if p))


def _store_for_group(schema: dict, rows: list[dict]) -> str | None:
    stores = {r["store"] for r in rows}
    return stores.pop() if len(stores) == 1 else None


def render(schema: dict) -> str:
    lines: list[str] = [BEGIN, ""]

    lines += ["## Where settings live", "",
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
        rows = by_group[g["id"]]
        if not rows:
            continue
        lines.append(f"## {g['title']}")
        lines.append("")
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
        lines += ["## Settings that constrain each other", ""]
        for c in cons:
            lines.append(f"- {_md(c['message'])}")
        lines.append("")

    lines.append(END)
    return "\n".join(lines) + "\n"


def splice(doc: str, generated: str) -> str:
    """Replace the marked region of a CONFIG.md with `generated`."""
    start = doc.find(BEGIN)
    end = doc.find(END)
    if start < 0 or end < 0 or end < start:
        raise ValueError(f"{DOCS_PATH}: missing the generated-region markers")
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
