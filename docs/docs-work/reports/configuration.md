# configuration

## Changed files
- src/content/docs/guides/configuration.md

## What changed
- Replaced the old single-section page with the full task page: changing a setting in the configuration page (gateway-token access, stale-value display), changing a setting in a configuration file over SSH, where secrets live and why a set environment variable overrides its secret file, applying a change (per-file restart table and what each restart interrupts), and the LLM setup walkthrough by page and by file.
- Every section carries source comments to the pinned commit; the two load-bearing claims (nothing hot-reloads; stale-config detection) were re-verified against docs/CONFIG.md:44-45 and brain/teaport_brain/config_ui.py:27-28.
- Points at [Settings reference](/reference/settings/) for individual settings instead of copying any table.

## Removed content
- The old page's only section, "Important settings", a hand-maintained table of settings: its content is now the generated Settings reference, which this page links to (per the packet: never repeat the settings tables here). Old page source: git show HEAD:src/content/docs/guides/configuration.md.

## Open questions
- None. Every retained fact cites the pinned commit; no contradiction between sources surfaced.

## Lint
0 error(s), 5 warning(s) in 1 file(s). All five kept WARNs are "once" used in the single-occurrence sense ("reads its key once per session", "loaded once", "once at startup"), not "once" meaning "after"; the word-list exception applies.
