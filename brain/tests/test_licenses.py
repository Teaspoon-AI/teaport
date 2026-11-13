#
# The license page and `teaport licenses` (licenses.py, config_ui's /licenses routes).
#
# What has to hold:
#   - A package's license is what its metadata names: the SPDX expression, a short License
#     field, else its license classifiers; a License field that holds a whole license text is
#     not a license name. The ones under the LGPL are marked.
#   - A package's text is the license files in its .dist-info (and under licenses/ there), each
#     with its name above it; a package without one says so, and names the license it states.
#     Names match the way pip matches them (case, "-", "_" and ".").
#   - The documents are the files the package installs and the engine's notices; one that is
#     not on the device is left out, and a long one is cut rather than served whole.
#   - The routes answer with the token when one is set, 404 for a package the brain does not
#     have, and serve the page; the command lists, shows one package, and shows the notices,
#     and says why when there is nothing to show.
#
# Synthetic packages and documents only (a temp site directory).
#
# Run: python test_licenses.py   (or via pytest)
#
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import appliance  # noqa: E402
from tempdirs import tempdir  # noqa: E402

from teaport_brain import config_ui, licenses  # noqa: E402

CLI = pathlib.Path(__file__).resolve().parents[2] / "cli" / "teaport"


def _dist(site, name, version, metadata="", files=None, outside=None, raw=()):
    """A package in a temp site directory: its METADATA, `files` (name -> text) in its
    .dist-info and `outside` (path -> text) elsewhere in the site directory, all listed in its
    RECORD as an installer writes it; `raw` are further RECORD lines as they are."""
    info = pathlib.Path(site, f"{name.replace('-', '_')}-{version}.dist-info")
    info.mkdir(parents=True)
    ver = f"Version: {version}\n" if version else ""
    (info / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {name}\n{ver}{metadata}")
    record = list(raw)
    for rel, text in (files or {}).items():
        (info / rel).parent.mkdir(parents=True, exist_ok=True)
        (info / rel).write_bytes(text if isinstance(text, bytes) else text.encode())
        record.append(f"{info.name}/{rel},,")
    for rel, text in (outside or {}).items():
        (pathlib.Path(site) / rel).parent.mkdir(parents=True, exist_ok=True)
        (pathlib.Path(site) / rel).write_text(text)
        record.append(f"{rel},,")
    (info / "RECORD").write_text("\n".join(record) + "\n")


def _site():
    site = tempdir("teaport-licenses-site-")
    site = [site]
    _dist(site[0], "plain-mit", "1.0", "License-Expression: MIT\n", {"licenses/LICENSE": "MIT TEXT\n"})
    _dist(site[0], "Old_Style.Pkg", "2.1", "License: BSD-3-Clause\n", {"LICENSE.txt": "BSD TEXT\n", "top_level.txt": "x\n"})
    # A License field holding a whole text is folded over lines, as METADATA folds it.
    _dist(site[0], "whole-text", "3", "License: Permission is hereby granted, free of charge,\n"
          "        to any person obtaining a copy of this software.\n"
          "Classifier: License :: OSI Approved\nClassifier: License :: OSI Approved :: Apache Software License\n")
    _dist(site[0], "soxr-like", "0.5", "License-Expression: LGPL-2.1-or-later\n",
          {"licenses/LICENSE.txt": "LGPL TEXT\n", "licenses/libsoxr/COPYING": "LIBSOXR TEXT\n"})
    _dist(site[0], "bare", "0.1", "")  # its RECORD is one blank line, which importlib cannot read
    # A wheel that bundles an LGPL library keeps that library's text outside its .dist-info; a
    # module called license.py is code; a RECORD line that climbs out of the directory is not read.
    (pathlib.Path(site[0]) / "secret.txt").write_text("NOT A LICENSE\n")
    _dist(site[0], "bundler", "4", "License: BSD 3-Clause License\n", {"LICENSE": "BSD TEXT\n"},
          outside={"_bundled/COPYING": "BUNDLED LGPL TEXT\n", "bundler/license.py": "x = 1\n"},
          raw=["bundler-4.dist-info/licenses/../../secret.txt,,"])
    _dist(site[0], "soundfile", "0.13", "License: BSD 3-Clause License\n", {"LICENSE": "BSD TEXT\n"},
          outside={"_soundfile_data/COPYING": "LIBSNDFILE LGPL TEXT\n"})
    _dist(site[0], "no-version", "", "License-Expression: MIT\n")
    _dist(site[0], "latin", "1", "License-Expression: MIT\n", {"LICENSE": b"caf\xe9 license\n"})
    return site


def test_a_package_names_its_license_from_its_metadata():
    got = {p["name"]: p for p in licenses.packages(_site())}
    assert got["plain-mit"]["license"] == "MIT" and got["plain-mit"]["version"] == "1.0", got
    assert got["Old_Style.Pkg"]["license"] == "BSD-3-Clause", got
    # A License field that is a whole license text is not a name: the classifier is.
    assert got["whole-text"]["license"] == "Apache Software License", got
    assert got["bare"]["license"] == "not stated", got
    assert [p["name"] for p in licenses.packages(_site())] == sorted(got, key=str.lower)


def test_the_lgpl_ones_are_marked():
    got = {p["name"]: p["lgpl"] for p in licenses.packages(_site())}
    assert got == {"plain-mit": False, "Old_Style.Pkg": False, "whole-text": False, "soxr-like": True,
                   "bare": False, "bundler": False, "soundfile": True, "no-version": False,
                   "latin": False}, got
    # soundfile is BSD; the library in its wheel is the LGPL one, and the list says so.
    lic = {p["name"]: p["license"] for p in licenses.packages(_site())}["soundfile"]
    assert lic == "BSD 3-Clause License; bundles libsndfile, LGPL-2.1-or-later", lic


def test_a_packages_text_is_its_license_files():
    site = _site()
    got = licenses.package_text("soxr_like", site)  # pip's matching: case, "-" and "_"
    assert got["name"] == "soxr-like" and got["license"] == "LGPL-2.1-or-later", got
    assert "LGPL TEXT" in got["text"] and "LIBSOXR TEXT" in got["text"], got["text"]
    assert "==== licenses/LICENSE.txt ====" in got["text"] and "==== licenses/libsoxr/COPYING ====" in got["text"]
    old = licenses.package_text("old-style-pkg", site)
    assert "BSD TEXT" in old["text"] and "top_level" not in old["text"] and "x\n" not in old["text"], old
    bare = licenses.package_text("bare", site)
    assert "lists no license file" in bare["text"] and "not stated" in bare["text"], bare
    assert licenses.package_text("nope", site) is None


def test_a_license_text_outside_the_dist_info_is_shown_and_code_and_climbing_paths_are_not():
    site = _site()
    got = licenses.package_text("soundfile", site)["text"]
    assert "LIBSNDFILE LGPL TEXT" in got and "==== _soundfile_data/COPYING ====" in got, got
    assert "BSD TEXT" in got and "==== LICENSE ====" in got, got
    got = licenses.package_text("bundler", site)["text"]
    assert "BUNDLED LGPL TEXT" in got and "x = 1" not in got, got  # license.py is code
    assert "NOT A LICENSE" not in got, got  # ../ out of the directory is not followed


def test_a_package_without_a_version_and_a_file_that_is_not_utf8():
    site = _site()
    got = {p["name"]: p for p in licenses.packages(site)}
    assert got["no-version"]["version"] == "?", got["no-version"]
    assert "no-version" in licenses._table(list(got.values()))  # the list prints with it
    assert licenses.package_text("no-version", site)["version"] == "?"
    assert "caf\ufffd license" in licenses.package_text("latin", site)["text"]


def test_the_documents_are_the_ones_on_the_device_and_a_long_one_is_cut():
    doc, prefix = tempdir("teaport-licenses-doc-"), tempdir("teaport-licenses-prefix-")
    (pathlib.Path(doc) / "THIRD_PARTY_LICENSES.md").write_text("third party\n")
    (pathlib.Path(prefix) / "THIRD_PARTY_NOTICES.txt").write_text("x" * (licenses.MAX_BYTES + 10))
    got = {d["id"]: d for d in licenses.documents(doc, prefix)}
    assert list(got) == ["third-party", "engine"], got.keys()  # no copyright file here: left out
    assert got["third-party"]["text"] == "third party\n"
    assert got["third-party"]["path"] == os.path.join(doc, "THIRD_PARTY_LICENSES.md")
    assert "cut here" in got["engine"]["text"] and len(got["engine"]["text"]) < licenses.MAX_BYTES + 100
    assert licenses.documents(tempdir("teaport-licenses-empty-"), tempdir("teaport-licenses-empty-")) == []


def _client(token="t0k3n"):
    os.environ["GATEWAY_TOKEN"] = token
    app = FastAPI()
    app.include_router(config_ui.router)
    return TestClient(app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1:7861")


def test_the_routes_answer_with_the_token_and_404_for_an_unknown_package():
    doc = tempdir("teaport-licenses-doc-")
    (pathlib.Path(doc) / "copyright").write_text("MIT text\n")
    real = (licenses.DOC_DIR, licenses.PREFIX)
    licenses.DOC_DIR, licenses.PREFIX = doc, tempdir("teaport-licenses-prefix-")
    try:
        c = _client()
        page = c.get("/licenses")  # the page is static, like the configuration page's
        assert page.status_code == 200 and "Open-source licenses" in page.text
        assert c.get("/api/licenses").status_code == 401
        assert c.get("/api/licenses/pytest", headers={"authorization": "Bearer wrong"}).status_code == 401
        auth = {"authorization": "Bearer t0k3n"}
        got = c.get("/api/licenses", headers=auth).json()
        assert [d["id"] for d in got["documents"]] == ["copyright"] and got["documents"][0]["text"] == "MIT text\n"
        assert got["packages"] and {"name", "version", "license", "lgpl"} <= set(got["packages"][0])
        name = got["packages"][0]["name"]
        one = c.get(f"/api/licenses/{name}", headers=auth)
        assert one.status_code == 200 and one.json()["name"] == name and one.json()["text"], one.text
        assert c.get("/api/licenses/not-a-package-here", headers=auth).status_code == 404
    finally:
        licenses.DOC_DIR, licenses.PREFIX = real


def test_the_configuration_page_links_to_it():
    page = (pathlib.Path(config_ui.STATIC_DIR) / "config.html").read_text()
    assert '<a href="licenses">' in page  # relative: /config -> /licenses, /settings/config -> /settings/licenses


def _run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = licenses.main(list(argv))
        except SystemExit as e:  # argparse's own usage errors
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def test_the_command_lists_shows_a_package_and_shows_the_notices():
    rc, out, err = _run()
    assert rc == 0 and "MIT licensed" in out and "teaport licenses <package>" in out, (rc, out, err)
    rc, out, err = _run("--json")
    assert rc == 0 and isinstance(json.loads(out), list) and json.loads(out)[0]["name"], out
    name = json.loads(out)[0]["name"]
    rc, out, err = _run(name)
    assert rc == 0 and out.startswith(name), (rc, out[:80], err)
    assert _run("not-a-package-here")[0] == 1 and "no Python package named" in _run("not-a-package-here")[2]
    assert _run("pytest", "--notices")[0] == 2  # argparse: not both

    doc = tempdir("teaport-licenses-doc-")
    (pathlib.Path(doc) / "THIRD_PARTY_LICENSES.md").write_text("the notices text\n")
    rc, out, err = _run("--notices", "--doc-dir", doc, "--prefix", tempdir("teaport-licenses-prefix-"))
    assert rc == 0 and "the notices text" in out and "THIRD_PARTY_LICENSES.md" in out, (rc, out, err)
    rc, out, err = _run("--notices", "--doc-dir", tempdir("teaport-licenses-e-"), "--prefix", tempdir("teaport-licenses-e-"))
    assert rc == 1 and "not installed from the teaport-brain package" in err, (rc, err)
    rc, out, err = _run("--notices", "--json", "--doc-dir", tempdir("teaport-licenses-e-"), "--prefix", tempdir("teaport-licenses-e-"))
    assert rc == 1 and out == "", (rc, out)  # the same answer with --json as without it


def _cmd(prefix, *args):
    """`teaport licenses <args>` as the CLI function runs it, against a brain at `prefix`."""
    fn = subprocess.run(["sed", "-n", "/^cmd_licenses() {/,/^}/p", str(CLI)],
                        capture_output=True, text=True, check=True).stdout
    assert fn.startswith("cmd_licenses() {"), "cmd_licenses not found in cli/teaport"
    body = ('set -euo pipefail\ndie() { echo "error: $*" >&2; exit 1; }\nPREFIX="$1"; shift\n'
            + fn + 'cmd_licenses "$@"')
    return subprocess.run(["bash", "-c", body, "teaport", prefix, *args], capture_output=True, text=True)


def test_the_cli_function_runs_the_brains_python_and_says_when_there_is_none():
    p = _cmd(tempdir("teaport-licenses-none-"), "--json")
    assert p.returncode == 1 and "no brain at" in p.stderr, (p.returncode, p.stderr)
    prefix = tempdir("teaport-licenses-brain-")
    py = pathlib.Path(prefix, "brain", "bin", "python")
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\necho "python $*"\nexit 3\n')
    py.chmod(0o755)
    p = _cmd(prefix, "soxr", "--json")
    assert p.returncode == 3, p  # the command's own exit status comes through
    assert p.stdout.strip() == f"python -I -m teaport_brain.licenses --prefix {prefix} soxr --json", p.stdout


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
