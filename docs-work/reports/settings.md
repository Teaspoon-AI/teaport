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
- The lint ERRORs and the history in the generated rows are fixed upstream, in `config_schema.toml` on the product branch `docs/schema-help-style` (commit 5a98d87): `docs = false` keeps the six retired names out of the tables, and the generator no longer prints "Named X before". Every row here now equals the generator's output. Update the `@5a98d87` citation to the merge commit after the product PR lands.
- The 72 remaining WARNs are "device" versus "box" (the product's own config page uses "box"), "once"/"since", and "above"/"below". They live in schema help text, so a wording change there also changes the config page; decide that in the product repo.

## Lint
0 error(s), 72 warning(s) in 1 file(s).
