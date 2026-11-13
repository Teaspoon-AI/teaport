"""What the device is licensed under, read from what is installed.

One source for the two places that show it: the Settings page (config_ui: GET /licenses,
/api/licenses) and `teaport licenses` (main, run under the brain's own venv like
`teaport update --check`). Nothing here is a copy of a license text: the documents are the
files the teaport-brain package installs under /usr/share/doc/teaport-brain/ and the
engine's notices in the install root, and a Python package's text is whatever license files
it ships in its .dist-info directory.
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import os
import re
import sys

PREFIX = "/opt/teaport"
DOC_DIR = "/usr/share/doc/teaport-brain"

# (id, title, directory it is in: "doc" or "prefix", file name), in the order they are shown.
DOCUMENTS = (
    ("third-party", "Third-party components and their licenses", "doc", "THIRD_PARTY_LICENSES.md"),
    ("copyright", "teaport's license (the package's copyright file)", "doc", "copyright"),
    ("engine", "The engine's notices", "prefix", "THIRD_PARTY_NOTICES.txt"),
)

MAX_BYTES = 256 * 1024  # of any one file: the page and the terminal show text, not archives
_LICENSE_FILE = re.compile(r"^(licen[sc]e|copying|notice|copyright|unlicense)", re.I)


def normalize(name: str) -> str:
    """A package name as pip compares it (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _read(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_BYTES + 1)
    except OSError:
        return None
    text = data[:MAX_BYTES].decode("utf-8", errors="replace")
    return text + "\n\n[... cut here: the file is longer ...]\n" if len(data) > MAX_BYTES else text


def documents(doc_dir: str | None = None, prefix: str | None = None) -> list[dict]:
    """The license documents this device has, each {id, title, path, text}. A file that is
    not there (a brain not installed from the package, an engine not installed) is left out."""
    where = {"doc": doc_dir or DOC_DIR, "prefix": prefix or PREFIX}
    out = []
    for key, title, base, name in DOCUMENTS:
        path = os.path.join(where[base], name)
        text = _read(path)
        if text is not None:
            out.append({"id": key, "title": title, "path": path, "text": text})
    return out


def _license_of(meta) -> str:
    """The license a package's metadata names: its SPDX expression, a short License field,
    or its license classifiers (a License field can hold a whole license text: not that)."""
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    field = (meta.get("License") or "").strip()
    if field and "\n" not in field and len(field) <= 80 and field.upper() != "UNKNOWN":
        return field
    names = [c.split("::")[-1].strip() for c in (meta.get_all("Classifier") or [])
             if c.startswith("License ::") and not c.endswith(":: OSI Approved")]
    return "; ".join(names) or "not stated"


# Packages whose wheel bundles an LGPL library under a license of its own: the package is
# not under the LGPL, the library inside it is (THIRD_PARTY_LICENSES.md says which).
BUNDLES_LGPL = {"soundfile": "libsndfile, LGPL-2.1-or-later"}

# A file is a license text by its name, whatever directory it is in; code with such a name
# (a module called license.py) is not.
_NOT_TEXT = {".py", ".pyc", ".pyi", ".so", ".pyd", ".h", ".c", ".cpp", ".js", ".json"}
MAX_FILES = 40


def _dists(path: list[str] | None):
    seen = set()
    for d in md.distributions(path=path) if path is not None else md.distributions():
        meta = d.metadata
        name = meta.get("Name") or ""
        if name and normalize(name) not in seen:
            seen.add(normalize(name))
            yield name, d, meta


def packages(path: list[str] | None = None) -> list[dict]:
    """Every Python package of this environment: {name, version, license, lgpl}, by name.
    `lgpl` marks the ones that are under the LGPL or bundle an LGPL library (the notice in
    THIRD_PARTY_LICENSES.md says what that asks)."""
    out = []
    for name, d, meta in _dists(path):
        lic = _license_of(meta)
        lgpl = "lgpl" in lic.lower() or "lesser general public" in lic.lower()
        bundled = BUNDLES_LGPL.get(normalize(name))
        if bundled:
            lic, lgpl = f"{lic}; bundles {bundled}", True
        out.append({"name": name, "version": d.version or "?", "license": lic, "lgpl": lgpl})
    return sorted(out, key=lambda p: p["name"].lower())


def _is_license_file(parts: tuple[str, ...]) -> bool:
    """A file listed in a package's RECORD that is a license text: in its .dist-info, a
    LICENSE/COPYING/NOTICE... file or anything under licenses/; anywhere else, one named like
    that (a library bundled in the package keeps its own). Nothing that climbs out with `..`."""
    if not parts or ".." in parts or parts[0].startswith(("/", "~")):
        return False
    name = parts[-1]
    if os.path.splitext(name)[1].lower() in _NOT_TEXT:
        return False
    if len(parts) >= 2 and parts[0].endswith(".dist-info") and "licenses" in parts[1:-1]:
        return True
    return bool(_LICENSE_FILE.match(name))


def package_text(name: str, path: list[str] | None = None) -> dict | None:
    """One package's license: {name, version, license, text}. The text is each license file
    the package lists as installed (its .dist-info's LICENSE, COPYING, NOTICE..., what is
    under a licenses/ directory there, and such files of a library bundled in it), one after
    another with its path above it. None when no such package is installed."""
    want = normalize(name)
    for pkg, d, meta in _dists(path):
        if normalize(pkg) != want:
            continue
        try:
            listed = d.files or []
        except (TypeError, ValueError, OSError):  # a RECORD that does not parse: no files, not a failure
            listed = []
        found = []
        for f in listed:
            if not _is_license_file(f.parts):
                continue
            text = _read(str(d.locate_file(f)))
            if text is not None:
                shown = f.parts[1:] if f.parts[0].endswith(".dist-info") else f.parts
                found.append(f"==== {'/'.join(shown)} ====\n\n{text.strip()}\n")
        more = len(found) - MAX_FILES
        found = found[:MAX_FILES] + ([f"[... {more} more license files not shown ...]\n"] if more > 0 else [])
        lic = _license_of(meta)
        body = "\n".join(found) or (f"The package lists no license file as installed. Its metadata says: {lic}.\n")
        return {"name": pkg, "version": d.version or "?", "license": lic, "text": body}
    return None


# ------------------------------------------------------------------ the command

def _table(pkgs: list[dict]) -> str:
    width = max([len(p["name"]) for p in pkgs] + [7])
    vwidth = max([len(p["version"]) for p in pkgs] + [7])
    lines = [f"  {'package':<{width}}  {'version':<{vwidth}}  license"]
    for p in pkgs:
        mark = "   (LGPL: see `teaport licenses --notices`)" if p["lgpl"] else ""
        lines.append(f"  {p['name']:<{width}}  {p['version']:<{vwidth}}  {p['license']}{mark}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`teaport licenses`: no argument lists the packages; a name shows that package's
    license text; --notices shows the documents; --json is the list as JSON."""
    ap = argparse.ArgumentParser(prog="teaport licenses", add_help=True,
                                 description="The licenses of the software on this device.")
    ap.add_argument("package", nargs="?", help="show this Python package's license text")
    ap.add_argument("--notices", action="store_true",
                    help="show the third-party notices, teaport's license and the engine's notices")
    ap.add_argument("--json", action="store_true", help="the package list (or the notices) as JSON")
    ap.add_argument("--prefix", default=PREFIX)
    ap.add_argument("--doc-dir", default=DOC_DIR)
    a = ap.parse_args(argv)
    if a.package and a.notices:
        ap.error("a package name and --notices do not go together")

    if a.notices:
        docs = documents(a.doc_dir, a.prefix)
        if not docs:
            print(f"no license documents found in {a.doc_dir} or {a.prefix} "
                  "(this brain was not installed from the teaport-brain package)", file=sys.stderr)
            return 1
        if a.json:
            print(json.dumps(docs, indent=2))
        else:
            for d in docs:
                print(f"{'=' * 78}\n{d['title']}  [{d['path']}]\n{'=' * 78}\n\n{d['text'].rstrip()}\n")
        return 0

    if a.package:
        got = package_text(a.package)
        if got is None:
            print(f"error: no Python package named {a.package!r} in the brain "
                  "(run `teaport licenses` for the list)", file=sys.stderr)
            return 1
        if a.json:
            print(json.dumps(got, indent=2))
        else:
            print(f"{got['name']} {got['version']}  ({got['license']})\n\n{got['text'].rstrip()}")
        return 0

    pkgs = packages()
    if a.json:
        print(json.dumps(pkgs, indent=2))
        return 0
    print("teaport's brain and plugin are MIT licensed; the engine has terms of its own. "
          f"The brain holds {len(pkgs)} Python packages, each under its own license:\n")
    print(_table(pkgs))
    print("\n  teaport licenses <package>   the license text of one package"
          "\n  teaport licenses --notices   the third-party notices, teaport's license and the engine's notices")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
