#!/usr/bin/env python3
# test_docs_wiki_sync.py — packaging/deb/docs-wiki-sync: the teaport docs as OpenClaw memory-wiki
# pages, one per "## " section, written into the vault next to whatever else is there.
#
# The splitting and the vault writes are run in-process; the CLI flow runs the script with a
# stub `openclaw` that answers `wiki status --json` and records `wiki compile`. All page
# text is synthetic.
#
# Run: python test_docs_wiki_sync.py   (or via pytest test_suite.py)
#
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import appliance
from tempdirs import tempdir

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "packaging/deb/docs-wiki-sync"


def _load():
    loader = importlib.machinery.SourceFileLoader("docs_wiki_sync", str(SCRIPT))
    spec = importlib.util.spec_from_loader("docs_wiki_sync", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sync_mod = _load()

PAGE = """---
title: Set up the widget
description: How to set up the widget.
---

Read this first. See [the settings](/reference/settings/#widget) and [below](#check-it).

## Before you begin

You need a widget.

```sh
## not a heading: a comment in a code sample
widget --check
```

## Check it

Run the check.

## Check it

A second section with the same heading.
"""


def _docs(tmp, pages):
    root = pathlib.Path(tmp, "docs")
    for rel, text in pages.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return root


def test_one_page_per_section_with_the_docs_site_links():
    docs = _docs(tempdir(), {"guides/widget.md": PAGE, "index.mdx": "---\ntitle: Home\n---\n\nHi.\n"})
    pages = sync_mod.pages(docs)
    assert sorted(pages) == [
        "teaport-docs-guides-widget-before-you-begin.md",
        "teaport-docs-guides-widget-check-it-2.md",
        "teaport-docs-guides-widget-check-it.md",
        "teaport-docs-guides-widget-overview.md",
    ], sorted(pages)   # index.mdx is not Markdown the wiki reads; a repeated heading gets -2
    before = pages["teaport-docs-guides-widget-before-you-begin.md"]
    fields, body = sync_mod.frontmatter(before)
    assert fields["pageType"] == "source" and fields["sourceType"] == "teaport-docs", fields
    assert fields["title"] == "teaport docs: Set up the widget — Before you begin", fields
    assert fields["id"] == "source.teaport-docs-guides-widget-before-you-begin", fields
    assert fields["sourceUrl"] == "https://docs.teaspoon.tech/guides/widget/", fields
    assert fields["updatedAt"].endswith("Z"), fields
    # A "## " inside a code fence stays in its section.
    assert "## not a heading" in body and "widget --check" in body, body
    overview = pages["teaport-docs-guides-widget-overview.md"]
    assert "](https://docs.teaspoon.tech/reference/settings/#widget)" in overview, overview
    assert "](https://docs.teaspoon.tech/guides/widget/#check-it)" in overview, overview
    assert "description:" not in sync_mod.frontmatter(overview)[1]   # the site's frontmatter is gone


def _vault(tmp):
    sources = pathlib.Path(tmp, "vault", "sources")
    sources.mkdir(parents=True)
    return sources


def test_the_vault_gets_our_pages_and_keeps_everyone_elses():
    tmp = tempdir()
    docs = _docs(tmp, {"guides/widget.md": PAGE})
    sources = _vault(tmp)
    # The owner's own pages: one ingested under a title that slugs like ours.
    theirs = sources / "teaport-docs-guides-widget-overview.md"
    theirs.write_text('---\npageType: "source"\nsourceType: "local-file"\n---\n\nmine\n')
    other = sources / "alpha.md"
    other.write_text("---\npageType: source\n---\n\nalpha\n")
    want = sync_mod.pages(docs)
    assert sync_mod.sync(sources, want) == 3   # all of ours but the name that is theirs
    assert theirs.read_text().endswith("mine\n") and other.read_text().endswith("alpha\n")
    assert sync_mod.sync(sources, want) == 0   # nothing changed: nothing rewritten
    # The blocks `wiki compile` adds to a page (as OpenClaw 2026.9.7 writes them) are not a change.
    page = sources / "teaport-docs-guides-widget-before-you-begin.md"
    page.write_text(page.read_text() + "\n## Related\n<!-- openclaw:wiki:related:start -->\n"
                    "- No related pages yet.\n<!-- openclaw:wiki:related:end -->\n")
    assert sync_mod.sync(sources, want) == 0
    # A new package (new mtimes, so a new updatedAt) with the same docs is not a change.
    newer = {n: c.replace('updatedAt: "', 'updatedAt: "2099-') for n, c in want.items()}
    assert newer != want and sync_mod.sync(sources, newer) == 0
    # A section removed from the docs goes; theirs stays.
    del want["teaport-docs-guides-widget-check-it-2.md"]
    assert sync_mod.sync(sources, want) == 1
    assert not (sources / "teaport-docs-guides-widget-check-it-2.md").exists()
    assert theirs.exists() and other.exists()


def _run(tmp, *args, status=None, compile_rc=0, docs=None, record=None, env_oc=True):
    """Run the script with a stub openclaw. status None = `wiki status` fails (plugin off).
    record: the text of ~/.config/teaport/openclaw-cli; env_oc False: no $OPENCLAW."""
    tmp = pathlib.Path(tmp)
    calls = tmp / "calls"
    oc = tmp / "openclaw"
    home = tmp / "home"
    (home / ".config/teaport").mkdir(parents=True, exist_ok=True)
    if record is not None:
        (home / ".config/teaport/openclaw-cli").write_text(record.replace("{oc}", str(oc)))
    st = json.dumps(status) if status is not None else ""
    oc.write_text("#!/bin/bash\n"
                  f'echo "$*" >> "{calls}"\n'
                  f'if [ "$1 $2" = "wiki status" ]; then [ -n {json.dumps(st)} ] || exit 1; printf "%s\\n" {json.dumps(st)}; exit 0; fi\n'
                  f'if [ "$1 $2" = "wiki status" ]; then :; fi\n'
                  f'if [ "$1 $2" = "wiki compile" ]; then echo "STATE=$OPENCLAW_STATE_DIR" >> "{calls}.env"; exit {compile_rc}; fi\n'
                  "exit 0\n")
    oc.chmod(0o755)
    argv = [sys.executable, "-I", str(SCRIPT), *args]
    if docs is not None:
        argv += ["--docs", str(docs)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENCLAW")}
    env.update({"PATH": "/usr/bin:/bin", "HOME": str(home)})
    if env_oc:
        env["OPENCLAW"] = str(oc)
    p = subprocess.run(argv, capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr, (calls.read_text().splitlines() if calls.exists() else [])


def _status(vault, scope="global"):
    return {"vaultScope": scope, "vaultPath": str(vault), "vaultExists": True}


def test_the_cli_writes_the_pages_and_compiles_only_on_a_change():
    tmp = tempdir()
    docs = _docs(tmp, {"guides/widget.md": PAGE})
    sources = _vault(tmp)
    rc, out, calls = _run(tmp, status=_status(sources.parent), docs=docs)
    assert rc == 0 and calls == ["wiki status --json", "wiki compile"], (out, calls)
    assert len(list(sources.glob("teaport-docs-*.md"))) == 4
    pathlib.Path(tmp, "calls").unlink()
    rc, out, calls = _run(tmp, status=_status(sources.parent), docs=docs)
    assert rc == 0 and calls == ["wiki status --json"], (out, calls)   # nothing changed
    # A compile that fails is a failure.
    (docs / "guides/widget.md").write_text(PAGE + "\n## More\n\nNew.\n")
    rc, out, _ = _run(tmp, status=_status(sources.parent), compile_rc=1, docs=docs)
    assert rc == 1 and "compile failed" in out, out


def test_a_failed_compile_is_retried_on_the_next_run():
    tmp = tempdir()
    docs = _docs(tmp, {"guides/widget.md": PAGE})
    sources = _vault(tmp)
    rc, out, _ = _run(tmp, status=_status(sources.parent), compile_rc=1, docs=docs)
    assert rc == 1 and "compile failed" in out, out
    # The pages are on disk and match, but the vault was never compiled with them.
    pathlib.Path(tmp, "calls").unlink()
    rc, out, calls = _run(tmp, "--if-present", status=_status(sources.parent), docs=docs)
    assert rc == 0 and calls[-1] == "wiki compile" and "0 changed" in out, (out, calls)
    pathlib.Path(tmp, "calls").unlink()
    rc, out, calls = _run(tmp, "--if-present", status=_status(sources.parent), docs=docs)
    assert rc == 0 and calls == ["wiki status --json"], (out, calls)   # now compiled: nothing


def test_the_openclaw_install_sh_recorded_is_found_without_path_or_env():
    # postinst: a plain PATH, no $OPENCLAW. The record names the CLI and OpenClaw's state.
    tmp = tempdir()
    docs = _docs(tmp, {"guides/widget.md": PAGE})
    sources = _vault(tmp)
    rec = "cli={oc}\npath=/opt/node/bin\nOPENCLAW_STATE_DIR=/srv/oc-state\n"
    rc, out, calls = _run(tmp, status=_status(sources.parent), docs=docs, record=rec, env_oc=False)
    assert rc == 0 and calls == ["wiki status --json", "wiki compile"], (out, calls)
    assert pathlib.Path(tmp, "calls.env").read_text() == "STATE=/srv/oc-state\n"
    # A recorded CLI that is gone is said, and the search goes on (here: nothing found).
    pathlib.Path(tmp, "calls").unlink()
    rc, out, calls = _run(tmp, "--if-present", status=_status(sources.parent), docs=docs,
                          record="cli=/nonexistent/openclaw\n", env_oc=False)
    assert rc == 0 and calls == [] and "is gone" in out, (out, calls)


def test_if_present_only_refreshes_what_an_install_put_there():
    tmp = tempdir()
    docs = _docs(tmp, {"guides/widget.md": PAGE})
    sources = _vault(tmp)
    # The wiki off, or no pages of ours yet: nothing, and no failure (postinst).
    for status in (None, _status(sources.parent)):
        rc, out, calls = _run(tmp, "--if-present", status=status, docs=docs)
        assert rc == 0 and "wiki compile" not in calls and not list(sources.glob("teaport-*")), (status, out)
    # Without --if-present, the wiki off is a failure install.sh reports.
    rc, out, _ = _run(tmp, status=None, docs=docs)
    assert rc == 1 and "not available" in out, out
    # Once installed, --if-present refreshes.
    assert _run(tmp, status=_status(sources.parent), docs=docs)[0] == 0
    (docs / "guides/widget.md").write_text(PAGE.replace("Run the check.", "Run the new check."))
    rc, out, calls = _run(tmp, "--if-present", status=_status(sources.parent), docs=docs)
    assert rc == 0 and calls[-1] == "wiki compile", (out, calls)
    assert "Run the new check." in (sources / "teaport-docs-guides-widget-check-it.md").read_text()


def test_one_vault_per_agent_is_left_to_the_owner():
    tmp = tempdir()
    docs = _docs(tmp, {"guides/widget.md": PAGE})
    sources = _vault(tmp)
    rc, out, calls = _run(tmp, status=_status(sources.parent, scope="agent"), docs=docs)
    assert rc == 1 and "one vault per agent" in out and "wiki compile" not in calls, out
    rc, out, _ = _run(tmp, "--if-present", status=_status(sources.parent, scope="agent"), docs=docs)
    assert rc == 0, out
    assert not list(sources.glob("teaport-*"))


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
