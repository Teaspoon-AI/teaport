## Source of truth

This site is `docs/` in the teaport product repo (`github.com/Teaspoon-AI/teaport`), and it
is canonical for teaport's user-facing documentation. Code and pages change in the same pull
request. Run site commands (`astro dev`, `node docs-work/lint.mjs`) from `docs/`.

Facts about ports, paths, defaults, and environment variables are defined in the repo's
`install.sh`, `cli/teaport`, `systemd/*.service.in`, and `manifest/stable.json`. When a
page states one, cite the file, the lines, and a fingerprint of those lines, so the next
drift is detectable:

```markdown
<!-- source: install.sh:375-386#3f9a2c1b; brain/teaport_brain/tools.py:18-19,24#aa11bb22 -->
```

Each item is `path:ranges#HASH`, and items are separated by `; `. Paths are relative to the
repo root, not to `docs/`. Ranges are comma-separated `N` or `N-M` line numbers, 1-based and
inclusive. `HASH` is the first 8 hex characters of the SHA-256 of the cited lines: read the
file as UTF-8, split it on newlines, take the cited lines in the order written, and join them
with `\n` and no trailing newline. In `.mdx` pages the comment is `{/* source: ... */}`.

`node docs-work/lint.mjs` checks every item against the working tree. It fails on an
unparseable item, a missing file, a range past the end of the file, a missing hash, and a
hash mismatch. A mismatch means the cited lines changed: re-check that the page's claim is
still true, then update the item. `node docs-work/lint.mjs --fix-sources [PATH...]` helps:

- It rewrites an old-form item (`path:ranges @commit`) to the new ranges and hash, by finding
  the lines it cited at that commit as one contiguous block in the current file.
- It relocates an item whose hash no longer matches, when an earlier version of the file in
  git history has the cited lines with that hash and the block still exists in the file.
- It leaves any item it cannot place as it was and lists it. Do not accept a listed item
  without reading the current code: if the cited content changed, the page's claim can be
  wrong even though a new hash would make the lint pass.

The generated tables in `src/content/docs/reference/settings.md` carry the same citation
format, written by the generator from `brain/teaport_brain/config_schema.toml`.

## Versions

The docs describe the software the release manifest pins — one version, no history.
No release is cut yet (`manifest/stable.json` still carries placeholders); the first
one is the first version these docs describe. Versions before it have no users, so the
prose carries no compatibility notes, rename footnotes, or upgrade paths; runtime
compatibility shims keep their schema rows but stay out of the generated tables via
`docs = false` in `config_schema.toml`. When a second documented version exists, give
it its own section and revisit this rule.

## Writing style

Every page follows the [Google developer documentation style guide](https://developers.google.com/style),
condensed for this site in `docs-work/STYLE.md` (word list, teaport terminology, page
types, and Starlight markup). `node docs-work/lint.mjs` checks the mechanical rules; a clean
lint is required but does not prove a sentence is true. The documentation rewrite is
organized as agent packets in `docs-work/PROMPT.md`.

## Development

When starting the dev server, use background mode:

```
astro dev --background
```

Manage the background server with `astro dev stop`, `astro dev status`, and `astro dev logs`.

## Documentation

Full documentation: https://docs.astro.build

Consult these guides before working on related tasks:

- [Adding pages, dynamic routes, or middleware](https://docs.astro.build/en/guides/routing/)
- [Working with Astro components](https://docs.astro.build/en/basics/astro-components/)
- [Using React, Vue, Svelte, or other framework components](https://docs.astro.build/en/guides/framework-components/)
- [Adding or managing content](https://docs.astro.build/en/guides/content-collections/)
- [Adding styles or using Tailwind](https://docs.astro.build/en/guides/styling/)
- [Supporting multiple languages](https://docs.astro.build/en/guides/internationalization/)

## Pipecat context hub

teaport's brain is a [pipecat](https://docs.pipecat.ai/) pipeline, pinned to a specific
release (`brain/tests/pinned_pipecat.py` in the product repo is the guard). Pipecat's API
moves fast enough that answering from memory produces code for the wrong version — during
the 1.7.0 upgrade, guesses about `UserTurnController` and `BaseSmartTurn` were wrong in
ways that only a live board caught.

So do not guess at pipecat APIs. The
[context hub](https://docs.pipecat.ai/api-reference/context-hub) is a local MCP server
that indexes pipecat's documentation, examples, and framework **source** into a vector
database, and it is registered at user scope so it is available in the product repo too:

```
claude mcp add pipecat-context-hub --scope user -- uvx pipecat-ai-context-hub serve
```

It exposes `search_docs`, `search_examples`, `search_api`, `get_doc`, `get_example`,
`get_code_snippet`, `check_deprecation`, and `get_hub_status`. `search_api` and
`get_code_snippet` are the ones that matter most here: they return the actual class and
method source, so a claim about what a processor does can be checked rather than assumed.
Run `check_deprecation` before keeping an import that a version bump might have moved.

**Keep the index pinned to the release the brain runs.** By default the hub indexes
pipecat's `main`, which is not what teaport executes — the first build here reported
`indexed_framework_version: 1.7.0` with `indexed_framework_commits_ahead: 883`, so
`search_api` would have been answering from 883 commits of drift. That is the same
failure the hub is meant to prevent, just with better provenance. Re-pin whenever the
brain's pin moves:

```
pipecat-ai-context-hub refresh --framework-version v1.7.0   # match pinned_pipecat.py
pipecat-ai-context-hub refresh --force                      # rebuild a corrupt index
pipecat-ai-context-hub status                               # index health, as JSON
```

`status` is the check that matters: it reports `indexed_framework_version`,
`indexed_framework_commits_ahead`, `total_records`, and `last_refresh_at`. A non-zero
commits-ahead means the answers no longer describe the code on the board. A full refresh
takes about 30 minutes and indexes ~17,600 records across the pipecat repos and
`docs.pipecat.ai`; the tool is installed with `uv tool install pipecat-ai-context-hub`.

The index goes stale after `PIPECAT_HUB_STALE_AFTER_DAYS` (default 7); `PIPECAT_HUB_CHECK=0`
silences the notice. `PIPECAT_HUB_EXTRA_REPOS` takes a comma-separated list of GitHub
`org/repo` slugs to index alongside pipecat's own.
