#
# docs/CONFIG.md's generated region matches config_schema.toml.
#
# The tables in CONFIG.md are rendered from the schema, and nothing else keeps
# them honest: a schema edit that is not followed by
#     python -m teaport_brain.config_schema
# would ship a doc that contradicts the code's own table. This is the same
# check the module's --check flag runs, so the fix is always that one command.
#
# Skipped when docs/ is absent (a wheel install has the package, not the repo).
#
# Run: python test_config_docs.py   (or via pytest)
#
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


def main() -> int:
    check_empty_note_on_every_default_cell()
    check_docs_false_and_former_stay_out_of_the_tables()
    if not config_schema.DOCS_PATH.exists():
        print(f"SKIP: {config_schema.DOCS_PATH} not present (not a repo checkout)")
        return appliance.SKIP_EXIT
    return config_schema.main(["--check"])


if __name__ == "__main__":
    sys.exit(main())
