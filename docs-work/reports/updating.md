# updating

## Changed files
- src/content/docs/guides/updating.md (new)

## What changed
- New task page: teaport update --check as a read-only comparison against the release manifest (the apply path is not wired; the installer is the update path, per cmd_update's own message), the brain-only update (install.sh --only brain: new venv beside the live one, health-judged swap of teaport-brain only, the local audio bridge restarted but not judged, SIP untouched and replaying), what a full run adds and its lack of automatic rollback, the rollback procedure (venv.previous, tools travel with the environment), the release channels via TEAPORT_MANIFEST_URL with no channel flag on either parser, and retention (newest three plus live and previous).
- Every citation verified against the pin: cli/teaport cmd_update and constants, install.sh header/parser/brain_units/brain_swap/brain_prune, both manifests, docs/INSTALL.md. One draft WARN (once meaning after) fixed; none kept.

## Removed content
None.

## Open questions
None. No contradiction surfaced between cmd_update, install.sh, and docs/INSTALL.md; where the installer header and the code could diverge (retention, auto-rollback) the code was checked and agrees.

## Lint
0 error(s), 0 warning(s) in 1 file(s). None kept.
