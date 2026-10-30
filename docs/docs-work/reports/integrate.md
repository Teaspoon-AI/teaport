# integrate

## Consolidated open questions

Every Open question from the packet reports, grouped by packet, for operator review. The settings reference generator-owned lint exceptions are documented in that report.

### architecture

1. The SIP gateway's listening port: no port number appears in `systemd/teaport-sip.service.in:3-7`; the gateway binds only what the trunk configuration names, and that config lives in the separate teaport-sip repo. The page therefore omits a SIP row from the table.
2. The Wi-Fi setup page's own port: `systemd/teaport-wifi-setup.service.in:37-38` references `PAGE_PORT` (defined in `brain/teaport_brain/wifi_setup.py`, outside this packet's cite set) without a value; only the port-80 redirect is documented.
3. The owner of TCP 80 outside the Wi-Fi setup window: `systemd/teaport-wifi-setup.service.in:36` calls it "the front door's (Caddy)" in passing, but no teaport unit or allowed source defines that listener.

### cli

4. cli/teaport:1035-1054: a hand-launched gateway process makes configure and configure --conf exit 1 (sip_refuse_hand_launched); the page says only that a check can refuse the install. More detail can be added from those lines.
5. cli/teaport:305-308: the update apply path is a stated TODO in the source (wire it once the release host serves artifacts); the page documents the current die-and-pointer behavior as-is.
6. cli/teaport:732-841 (sip_conf_install, sip_conf_force) were not read line by line; the --conf claims rest on their call sites and die messages (1106-1117, 1202-1254).

### configuration

7. None. Every retained fact cites the pinned commit; no contradiction between sources surfaced.

### discord

8. Canonical on-device invocation of the installer: the page shows `./install.sh` with env vars; no existing docs page documents how `install.sh` is fetched/run on the Jetson, and `install.sh:1888-1956` only assumes a repo checkout. Cross-check against the installation page when it exists; adjust the command block if the documented path differs (page section "Install and enable the bridge").

### faq

9. None. Every claim traces to a page or a pinned citation.

### index

10. None.

### installation

11. The Marlin log line `Detected Marlin INT4 model (single-file format)` (`sudo journalctl -u teaport-engine | grep -i marlin`): engine-runtime output, absent from the pin (only an attribution mention at `install.sh:470`). Kept from the old page as a measured observation; uncited.
12. The Option A commands (NodeSource setup_22.x, `npm install -g openclaw`) and the `openclaw gateway status` fields `Runtime:` / `Connectivity probe:`: agent-side behavior, not in the pin; partially corroborated by `install.sh:1294`. Carried from the old page.
13. OpenRouter base URL `https://openrouter.ai/api/v1` and the `openai/gpt-oss-120b` requirement: provider-side, not in the pin (`install.sh:1511` shows the Groq example; `1512` the default). Carried from the old page.
14. Where the license and LLM prompts sit relative to the ~15 GB download: not derivable from the pin; the old "after the download" claim was removed.
15. "Allow 30 to 45 minutes": carried from the old page as measured; with the ~15 GB download (`install.sh:283`) it deserves a re-check on hardware. Same for the JetPack 7.2 ISO 25 W/MAXN SUPER caveat (NVIDIA-side, kept uncited).

### installer

16. The header says TEAPORT_ALLOW_NON_JETSON means "no real install" (install.sh:42); the code read only shows the gate warning at install.sh:241. The page says "development and dry runs" from :241 and omits the rest.
17. The full tenant list of /usr/local/lib/teaport is not enumerated anywhere: the page names the Wi-Fi helper and captive DNS conf (install.sh:2320) and the versioned-release layout (install.sh:1053,1092,1098-1102), but not every tool that lands there.
18. "Rendered on every run" for teaport-sip.service rests on the plain phase_sip call in main (install.sh:2569) and no gate inside phase_sip (install.sh:2263-2267); the main dispatch was not read in full.

### local-audio

   None. Every statement cites the pinned commit.

### phone-calls

19. How the reader obtains or builds the gateway binary: the teaport-sip
      repository is outside the pin; install.sh:2255-2256 and cli/teaport:28 give
      only the expected path (`~/teaport-sip/build/teaport-sip`), so the page names
      no URL or build steps.
20. docs/CONFIG.md:525 continues past the page's coverage with further
      `fake_sip_gateway.py` invocation variants ("Other calls:"); unverified, left
      out.

### prepare

21. Is a power-mode change (nvpmodel / MAXN SUPER) still required before installation? The old page set mode 2; no pinned source mentions power modes (grep in Removed content). If it is, the page needs a source for the mode number.
22. May the old measured results return (desktop plus nvargus about 600 MB; `jetson_clocks` 2.5% on speech recognition; 47/62 C)? They appear nowhere in the pin; they need a new home or a re-measurement.
23. The old page claimed JetPack 7.2 reports only R39 ("Do not look for R38"); the installer accepts R38 and R39 (install.sh:237). Confirm R38 devices are supported in the field, not just by the shipped binary (install.sh:224-227).
24. The JetPack ISO missing-mode-2 failure and its reflash advice: unverifiable at 1e5604c; dropped.

### settings

25. Generator-owned help text breaks STYLE.md in places. The lint counts 14 ERRORs and 67 WARNs, all inside generated table rows that the packet says to keep verbatim; the wordings belong in brain/teaport_brain/config_schema.toml, so they should be fixed in the schema and the page regenerated. Patterns seen: "may" for ability/possibility (rows at page lines 41, 82, 149, 163, 229, 274, 328, 329), "setting" used for a value (93, 286), "Teaport"/product casing (108), "config UI" instead of "configuration page" (159, 329), "e.g." (362), "device" term and "once"/"since"/"above" usages across many rows (full list: node docs-work/lint.mjs src/content/docs/reference/settings.md).

### troubleshooting

26. None.

### updating

   None. No contradiction surfaced between cmd_update, install.sh, and docs/INSTALL.md; where the installer header and the code could diverge (retention, auto-rollback) the code was checked and agrees.

### wifi

27. brain/teaport_brain/config_ui.py:720 defines a wifi_setup_start endpoint; I could not verify that the configuration page exposes a visible control for it, so the page omits that path (the unit comment names "the config page" only as a way to stop a running setup, which the page does mention).
28. Whether the setup page itself displays the password is not visible from the sources read; the page text relies on the announcement and the paper insert. If the page shows it, a sentence belongs in "Join the setup network".

