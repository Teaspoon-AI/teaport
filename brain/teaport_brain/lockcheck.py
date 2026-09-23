#
# lockcheck.py — what a brain venv was built from, and whether it still holds it.
#
# install.sh builds every brain venv with `uv sync --locked` and, run by that venv's own
# interpreter, records what it holds (teaport-build.json). `teaport doctor` compares the
# live venv against that record: drift is a hand `pip install` into the venv — the
# failure that used to accumulate unseen for weeks while CI tested the lock and the box
# ran something else. One module writes and reads it, so the name normalization cannot
# differ between the two.
#
# Stdlib only, and run from / by the caller: it must work in any venv the installer
# builds, and `python -m` must never import a teaport_brain/ from the operator's cwd.
#
#   python -m teaport_brain.lockcheck record OUT --source S --revision R --lock-sha X --uv V
#   python -m teaport_brain.lockcheck diff RECORD
#       line 1: "<revision>, built <date>"; line 2: number of changes; line 3: the first few
#
import argparse
import datetime
import importlib.metadata as md
import json
import re
import sys


def installed() -> dict[str, str]:
    # A dist-info with no Name — what an interrupted `pip install` can leave behind — is
    # itself drift: listed under its path rather than crashing the check that exists to
    # report exactly that kind of damage.
    out = {}
    for d in md.distributions():
        name = d.metadata.get("Name")
        if name:
            out[re.sub(r"[-_.]+", "-", name).lower()] = d.version
        else:
            out[f"?broken:{getattr(d, '_path', '?')}"] = "?"
    return out


def drift(want: dict[str, str]) -> list[str]:
    have = installed()
    out = [f"{n} {want[n]} -> {have[n]}" for n in sorted(want.keys() & have.keys()) if want[n] != have[n]]
    out += [f"+{n} {have[n]}" for n in sorted(have.keys() - want.keys())]
    out += [f"-{n} {want[n]}" for n in sorted(want.keys() - have.keys())]
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="teaport_brain.lockcheck")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("out")
    for flag in ("--source", "--revision", "--lock-sha", "--uv"):
        rec.add_argument(flag, required=True)
    sub.add_parser("diff").add_argument("record")
    a = ap.parse_args(argv)

    if a.cmd == "record":
        with open(a.out, "w") as f:
            json.dump({"built": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                       "source": a.source, "revision": a.revision, "uv_lock_sha256": a.lock_sha,
                       "uv": a.uv, "installed": dict(sorted(installed().items()))}, f, indent=1)
        return 0

    with open(a.record) as f:
        r = json.load(f)
    d = drift(r["installed"])
    print(f"{r.get('revision', '?')}, built {r.get('built', '?')[:10]}")
    print(len(d))
    if d:
        print(", ".join(d[:8]) + (f", … (+{len(d) - 8} more)" if len(d) > 8 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
