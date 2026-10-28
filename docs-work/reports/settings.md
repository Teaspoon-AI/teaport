# settings

## Changed files
- src/content/docs/reference/settings.md (new)

## What changed
- Created the Settings reference from the generator's own output in docs/CONFIG.md: every group table kept row-for-row verbatim, under sentence-case H2s with one-line introductions.
- Wrote the page introduction and the cross-link to Configure teaport for all procedures; kept the generated stores table ("Where settings live") and the generated "Settings that constrain each other" section.
- Kept the generated marker comment and added the source comment citing brain/teaport_brain/config_schema.toml @1e5604c.

## Removed content
None.

## Open questions
- Generator-owned help text breaks STYLE.md in places. The lint counts 14 ERRORs and 67 WARNs, all inside generated table rows that the packet says to keep verbatim; the wordings belong in brain/teaport_brain/config_schema.toml, so they should be fixed in the schema and the page regenerated. Patterns seen: "may" for ability/possibility (rows at page lines 41, 82, 149, 163, 229, 274, 328, 329), "setting" used for a value (93, 286), "Teaport"/product casing (108), "config UI" instead of "configuration page" (159, 329), "e.g." (362), "device" term and "once"/"since"/"above" usages across many rows (full list: node docs-work/lint.mjs src/content/docs/reference/settings.md).

## Lint
14 error(s), 67 warning(s) in 1 file(s). Every ERROR and WARN is inside generator-owned table rows kept verbatim per the packet; see Open questions for the schema-side fix.
