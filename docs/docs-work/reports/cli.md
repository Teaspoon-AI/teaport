# cli

## Changed files
- src/content/docs/reference/cli.md

## What changed
- Replaced the 118-line legacy stub with the full reference: one H2 per command (status, doctor, update, logs, sip) and one H3 per sip subcommand (configure, status, aec, restart, disable).
- Every entry has a synopsis, description, options, the exit behavior where the source defines one, and one example; every command, option, and argument value cli/teaport parses is listed, nothing else.
- Documented the dispatch facts: bare, -h, --help, and help print the usage; unknown commands, subcommands, and flags exit 1.
- Named the TEAPORT_* overrides in prose and pointed the phone-call setup to /guides/phone-calls/.
- Cite comments carry cli/teaport line ranges at 1e5604c, plus install.sh:1018,1027 for the launcher path.

## Removed content
- The fabricated doctor transcript: the real check list differs (lock drift, pacing, SIP, NemoClaw; cli/teaport:176-277 @1e5604c).
- The Where the files are table: the installer reference packet owns the layout facts.
- The Environment variables table: superseded by the prose note; each default appears in the section where the CLI uses it.
- The doctor caution aside and the Related links block: the check list now states what doctor covers, and links sit where they guide (phone-calls under teaport sip).
- The stale comment source: teaport repo cli/teaport, which had no line numbers.

## Open questions
1. cli/teaport:1035-1054: a hand-launched gateway process makes configure and configure --conf exit 1 (sip_refuse_hand_launched); the page says only that a check can refuse the install. More detail can be added from those lines.
2. cli/teaport:305-308: the update apply path is a stated TODO in the source (wire it once the release host serves artifacts); the page documents the current die-and-pointer behavior as-is.
3. cli/teaport:732-841 (sip_conf_install, sip_conf_force) were not read line by line; the --conf claims rest on their call sites and die messages (1106-1117, 1202-1254).

## Lint

0 error(s), 0 warning(s) in 1 file(s)

No WARN left: the single above-or-below warning (line 264) was fixed by rewording to accepted values.
