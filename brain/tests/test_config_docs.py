#
# The Settings reference (docs/src/content/docs/reference/settings.md)'s generated
# region matches config_schema.toml.
#
# The tables on that page are rendered from the schema, and nothing else keeps
# them honest: a schema edit that is not followed by
#     python -m teaport_brain.config_schema
# would ship a doc that contradicts the code's own table. The region's source comment
# fingerprints the schema, so the page is stale after any edit to the schema, even one
# that renders the same. This is the same check the module's --check flag runs, so the
# fix is always that one command.
#
# Skipped when docs/ is absent (a wheel install has the package, not the repo).
#
# Run: python test_config_docs.py   (or via pytest)
#
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appliance  # noqa: E402
from teaport_brain import config_schema  # noqa: E402


def check_empty_note_on_every_default_cell() -> None:
    # A row's `empty` meaning shows whichever way its default is given (#136).
    base = {"name": "X", "type": "path", "empty": "off"}
    cells = {
        "default": {**base, "default": "/run/x.sock"},
        "default_by": {**base, "default_by": {"MODE": {"a": "/run/a", "": "/run/b"}}},
        "required": {**base, "required": True},
        "none": base,
    }
    for branch, row in cells.items():
        cell = config_schema._default_cell(row)
        assert cell.endswith(f"({config_schema.EMPTY} = off)"), (branch, cell)
    assert config_schema._default_cell(cells["default"]) == '`/run/x.sock` (`""` = off)'
    assert config_schema._default_cell(cells["required"]) == '**required** (`""` = off)'
    assert "= off" not in config_schema._default_cell({"name": "X", "type": "path", "default": "/x"})


def check_docs_false_and_former_stay_out_of_the_tables() -> None:
    # A retired or renamed name the brain still tolerates has no row in the docs; the
    # sentence "Named X before" is not rendered either, so the page describes one release.
    schema = {
        "stores": {"s": {"path": "/etc/x.env", "services": ["x"], "apply": "restart"}},
        "groups": [{"id": "g", "title": "G"}],
        "settings": [
            {"name": "KEEP", "group": "g", "store": "s", "type": "flag", "help": "Kept.",
             "former": "OLD_KEEP"},
            {"name": "OLD_KEEP", "group": "g", "store": "s", "type": "flag", "docs": False,
             "help": "Renamed KEEP."},
        ],
    }
    out = config_schema.render(schema)
    assert "`KEEP`" in out and "`OLD_KEEP`" not in out, out
    assert "Named" not in out and "before" not in out, out


def check_source_comment_fingerprints_the_schema() -> None:
    # `1-N#HASH`: N real lines, HASH = sha256[:8] of those lines joined with "\n" — the
    # docs lint recomputes it, so the two must agree on every byte.
    raw = config_schema.SCHEMA_PATH.read_bytes().decode("utf-8")
    lines = raw[:-1].split("\n") if raw.endswith("\n") else raw.split("\n")
    want = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:8]
    cite = f"config_schema.toml:1-{len(lines)}#{want} -->"
    assert config_schema.source_comment().endswith(cite), (config_schema.source_comment(), cite)
    # The comment is part of the region the generator owns: an older form above BEGIN is
    # replaced by splice(), not left standing beside the new one.
    old = "<!-- source: brain/teaport_brain/config_schema.toml:1-9 @abc1234 -->\n\n"
    doc = f"intro\n\n{old}{config_schema.BEGIN}\nold\n{config_schema.END}\n"
    out = config_schema.splice(doc, config_schema.render(config_schema.load()))
    assert out.startswith("intro\n\n" + config_schema.source_comment()), out[:200]
    assert "@abc1234" not in out and out.count("<!-- source:") == 1, out[:300]
    assert config_schema.splice(out, config_schema.render(config_schema.load())) == out


def main() -> int:
    check_empty_note_on_every_default_cell()
    check_docs_false_and_former_stay_out_of_the_tables()
    check_source_comment_fingerprints_the_schema()
    if not config_schema.DOCS_PATH.exists():
        print(f"SKIP: {config_schema.DOCS_PATH} not present (not a repo checkout)")
        return appliance.SKIP_EXIT
    return config_schema.main(["--check"])


if __name__ == "__main__":
    sys.exit(main())
