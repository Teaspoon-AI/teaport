# wifi

## Changed files
- src/content/docs/guides/wifi-setup.md (new)

## What changed
- New task page covering the offline setup flow: starting by voice ("set up Wi-Fi" / "connect to Wi-Fi", offline-only auto-start; online, the agent's Wi-Fi setup tool), joining the teaport-XXXX setup network, the captive page at 10.42.0.1, the password's origin (paper insert / flash-time WIFI_SETUP_PASSWORD, else per-run spoken random digits), success, join failure with the profile dropped, the 10-minute give-up with the 15-minute unit hard stop, and cancellation (spoken, configuration page, systemctl stop).
- Eight source comments, all line-verified against the staged pin (wifi_setup.py header and status/exit paths, wifi_voice.py phrase and outcome lines, the unit's exit-code and RuntimeMaxSec comments, config_schema.toml stores and blurb, install.sh:2301).

## Removed content
None.

## Open questions
- brain/teaport_brain/config_ui.py:720 defines a wifi_setup_start endpoint; I could not verify that the configuration page exposes a visible control for it, so the page omits that path (the unit comment names "the config page" only as a way to stop a running setup, which the page does mention).
- Whether the setup page itself displays the password is not visible from the sources read; the page text relies on the announcement and the paper insert. If the page shows it, a sentence belongs in "Join the setup network".

## Lint
0 error(s), 0 warning(s) in 1 file(s). None kept.
