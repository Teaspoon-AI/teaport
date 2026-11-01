# installation

## Changed files
- src/content/docs/getting-started/installation.md

## What changed
- Retitled to "Install teaport" and restructured to the STYLE task template: intro, Before you begin, verb-first unnumbered H2s, closing What's end; dropped the numbered headings and the Pre-launch note (STYLE 1.4, 8).
- JetPack/L4T corrected to the verified gate: the engine ships for JetPack 7.2 on L4T R38 or R39 with CUDA 13; the installer dies otherwise (`install.sh:224-238,429-431`); kept the R39.2/CUDA 13.2 rootfs note.
- Disk requirement corrected from "21 GB / 4.6 GB of models" to about 15 GB for the engine and its models, with the installer's low-space warning (`install.sh:245-246,283`).
- Agent choice: added that NemoClaw wins when both agents are present, the voice-only path when neither is, and the re-run-to-add-later escape (`install.sh:1189-1191,1289-1297`); kept both option procedures and the hard-won gateway-restart caution.
- Installer prompts rewritten to the verified set: engine license (type `accept`, or `TEAPORT_ACCEPT_EULA=1`), LLM endpoint/model/key with the `gpt-oss-120b` default and blank-key option, and Discord questions only under `TEAPORT_ENABLE_BRIDGE=1` (`install.sh:207-211,1510-1517,1917-1919`); added the env-preset escape and the Installer reference link.
- Engine release named: 1.6, from the manifest, with the G2P pronunciation dictionary and the `/opt/teaport` prefix; the tasks list now ends "starts the services and checks the installation" per `phase_verify` (`manifest/stable.json:5-21`, `install.sh:59,2412-2424`).
- Added the three opt-in components (phone calls, Discord bridge, local audio) one sentence each with links; kept the closing verification but reordered it before the first conversation, with the doctor's actual check scope (`cli/teaport:5-6,183-213`).

## Removed content
- The Pre-launch note: STYLE 1.4 forbids pre-release framing; the manifest's own note covers artifact volatility.
- "21 GB" and "4.6 GB of models": replaced by the verified ~15 GB (`install.sh:245-246,283`); the old swap-file composition is unverified at the pin.
- "The installer asks three things / nothing else" framing and the OpenRouter answer table: folded into the prose and the kept OpenRouter caution; the table duplicated them.
- "These questions come after the download... The installer waits for you": prompt-to-phase ordering not verifiable at the pin (license helper at `install.sh:207-211`, LLM helper at `1497-1522`, phase list at `2560-2576`).
- "It installs the teaport command into /usr/local/bin": no `usr/local/bin` reference found at the pin (only `TOOLS_DIR=/usr/local/lib/teaport`, `install.sh:1026`).
- The GitHub-issue sentence ("open an issue... include the output"): the repo URL is not in the pin.
- "If something does not work" H2: folded into the check step and What's end links.

## Open questions
1. The Marlin log line `Detected Marlin INT4 model (single-file format)` (`sudo journalctl -u teaport-engine | grep -i marlin`): engine-runtime output, absent from the pin (only an attribution mention at `install.sh:470`). Kept from the old page as a measured observation; uncited.
2. The Option A commands (NodeSource setup_22.x, `npm install -g openclaw`) and the `openclaw gateway status` fields `Runtime:` / `Connectivity probe:`: agent-side behavior, not in the pin; partially corroborated by `install.sh:1294`. Carried from the old page.
3. OpenRouter base URL `https://openrouter.ai/api/v1` and the `openai/gpt-oss-120b` requirement: provider-side, not in the pin (`install.sh:1511` shows the Groq example; `1512` the default). Carried from the old page.
4. Where the license and LLM prompts sit relative to the ~15 GB download: not derivable from the pin; the old "after the download" claim was removed.
5. "Allow 30 to 45 minutes": carried from the old page as measured; with the ~15 GB download (`install.sh:283`) it deserves a re-check on hardware. Same for the JetPack 7.2 ISO 25 W/MAXN SUPER caveat (NVIDIA-side, kept uncited).

## Lint
0 error(s), 2 warning(s) in 1 file(s)
- WARN once (line 66, gateway recovery): "run `openclaw gateway start` once" means a single occurrence, not "after"; the word-list exception applies.
- WARN once (line ~216, certificate): "Accept the certificate warning once" means one time; the word-list exception applies.
