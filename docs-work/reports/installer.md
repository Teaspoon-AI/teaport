# installer

## Changed files
- src/content/docs/reference/installer.md
- docs-work/reports/installer.md

## What changed
- Completed the 85-line partial draft into the full reference (210 lines). Every flag is verified in the argument parser (install.sh:77-95), not only the header comment: exit-2 conditions, the equals forms, and the brain-only component constraint are now stated from the parser.
- Added the environment-override table: every variable in the header comment, including the paired guild/follow and OLED port/addr entries, with each default re-verified in the code, not just the header (install.sh:58-70,1530,1901,1908,2356,2375-2376,2388,2403).
- Added the files-and-directories section: /opt/teaport and the brain venv layout (venv symlink, venv.previous rollback target, keep-3 pruning), /etc/teaport env files at mode 640, ~/.config/teaport secrets at mode 700/600, /var/lib/teaport scratch space, /usr/local/lib/teaport helpers, plus the journald and systemd-resolved drop-ins and the Wi-Fi polkit rule.
- Added the systemd-units section from the render and enable call sites: per-unit install and enable conditions, the ConditionPathExists gating on teaport-sip and teaport-, the teaport-sip-brain retirement, the OLED unit's separate installer, and the avahi/caddy enables.
- Corrected draft facts: the OpenClaw plugin is conditional (header install.sh:5-6 "(optionally)"), and a full install restarts engine and brain together with no automatic rollback (install.sh:27-31).
- Grew the page from 7 citations to 29; every line range was grep-verified against /tmp/teaport-pin at 1e5604c.

## Removed content
- The draft's unconditional "the OpenClaw plugin" in the intro and in the no-flag table row: the header (install.sh:5-6) shows the plugin is optional, so both now carry the condition.
- The standalone citations install.sh:77-88,95 and install.sh:82-86: merged into 77-95. No facts lost; component validation (install.sh:92-94) is now cited instead.

## Open questions
1. The header says TEAPORT_ALLOW_NON_JETSON means "no real install" (install.sh:42); the code read only shows the gate warning at install.sh:241. The page says "development and dry runs" from :241 and omits the rest.
2. The full tenant list of /usr/local/lib/teaport is not enumerated anywhere: the page names the Wi-Fi helper and captive DNS conf (install.sh:2320) and the versioned-release layout (install.sh:1053,1092,1098-1102), but not every tool that lands there.
3. "Rendered on every run" for teaport-sip.service rests on the plain phase_sip call in main (install.sh:2569) and no gate inside phase_sip (install.sh:2263-2267); the main dispatch was not read in full.

## Lint
0 error(s), 0 warning(s) in 1 file(s)
No WARNs left. Build: flock acquired immediately, `npx astro build` exit 0, 16 pages built.
