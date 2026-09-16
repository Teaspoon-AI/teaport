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


def main() -> int:
    if not config_schema.DOCS_PATH.exists():
        print(f"SKIP: {config_schema.DOCS_PATH} not present (not a repo checkout)")
        return appliance.SKIP_EXIT
    return config_schema.main(["--check"])


if __name__ == "__main__":
    sys.exit(main())
