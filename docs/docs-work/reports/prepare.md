# prepare

## Changed files
- src/content/docs/getting-started/prepare-the-jetson.md

## What changed
- Restyled to STYLE.md as a task page: numbered headings removed, reader addressed as "you", structure is now intro, Before you begin, task sections, What's next.
- The version check now reads `/etc/nv_tegra_release` and accepts L4T R38 or R39, the same gate the installer runs (install.sh:224-239); the `apt-cache policy nvidia-jetpack` check is gone.
- The swap section is now "keep zram, add no SD-card swap file" (docs/INSTALL.md:12-14); the 16 GB swapfile and `vm.swappiness=10` procedures are gone.
- The desktop section keeps only the change the source supports: `sudo systemctl set-default multi-user.target` plus a reboot (docs/INSTALL.md:12-14).
- Power mode, `jetson_clocks`, `nvargus-daemon`, and the measured-results table are removed: nothing in the pinned tree mentions them (grep across /tmp/teaport-pin at 1e5604c).
- Every remaining fact carries a source comment.

## Removed content
- Power-mode section (`nvpmodel -m 2`, MAXN SUPER, the 25 W default, the JetPack ISO mode-2 note): `grep -rniE 'nvpmodel|MAXN|power mode' /tmp/teaport-pin` matches only the systemd `WantedBy=multi-user.target` lines and docs/INSTALL.md:13 (plus an unrelated `maxNotes` hit in plugin/README.md:242); no pinned source states a power-mode step.
- The `jetson_clocks` advice and its 2.5% result: no `jetson_clocks` reference in the pin.
- The 16 GB swapfile procedure and `vm.swappiness=10`: superseded by docs/INSTALL.md:13-14. install.sh sets up no swap or zram anywhere; its only swap code is the brain venv swap (install.sh:899-955).
- The `nvargus-daemon` disable, the 600 MB figure, the Result checks, and the temperature table: none appear in the pin.
- `apt-cache policy nvidia-jetpack`: the pin never references that package; the installer checks L4T instead (install.sh:231-239) and installs its own system deps (install.sh:265-282).
- The two external NVIDIA links: dropped with the sections they supported; no pinned source carries the URLs.

## Open questions
1. Is a power-mode change (nvpmodel / MAXN SUPER) still required before installation? The old page set mode 2; no pinned source mentions power modes (grep in Removed content). If it is, the page needs a source for the mode number.
2. May the old measured results return (desktop plus nvargus about 600 MB; `jetson_clocks` 2.5% on speech recognition; 47/62 C)? They appear nowhere in the pin; they need a new home or a re-measurement.
3. The old page claimed JetPack 7.2 reports only R39 ("Do not look for R38"); the installer accepts R38 and R39 (install.sh:237). Confirm R38 devices are supported in the field, not just by the shipped binary (install.sh:224-227).
4. The JetPack ISO missing-mode-2 failure and its reflash advice: unverifiable at 1e5604c; dropped.

## Lint
`0 error(s), 0 warning(s) in 1 file(s)` - no WARNs left. Build gate (`flock /tmp/teaport-docs-build.flock npx astro build`): exit 0.
