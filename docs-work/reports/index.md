# index

## Changed files
- src/content/docs/index.mdx

## What changed
- Six cards, each linking a page that exists and was verified under src/content/docs/: Install teaport, Prepare the Jetson, Configure teaport, Set up phone calls, Connect the device to Wi-Fi, Architecture.
- Tagline and description rewritten in the present tense per the pinned README voice ("A local voice assistant for the NVIDIA Jetson Orin... Your voice does not leave the device").
- Card claims carry source comments (installer self-test, nothing hot-reloads, sip configure, the Wi-Fi setup flow, the engine/brain/plugin trio); components, layout, and the splash template untouched.

## Removed content
- The old Installation card's "three steps... about 45 to 60 minutes" estimate and the separate install-NemoClaw step wording: the installation page owns the actual flow and agent choice, and the measured-time claim is not in the pin. The old standalone Troubleshooting card: troubleshooting remains reachable from the guides and the sidebar; the six-card cap replaced it.

## Open questions
- None.

## Lint
0 error(s), 0 warning(s) in 1 file(s). None kept.
